"""火山方舟 LLM 服务封装。

把方舟 OpenAI 兼容接口的调用细节(认证、重试、SSE 解析、错误归一)集中在此,
上层(Pipeline 阶段、RAG、记忆服务)只面向 ``LLMService`` 的方法编程,不感知
httpx/tenacity/SSE 细节。这样:
1. 切换 LLM 供应商(或换接入点)只改本文件,业务零改动;
2. 单测时用 fake 替换,避免发真实网络请求。

关键设计:
- **双模式构造**:`__init__` 第一个参数既可传 ``LLMConfig``(业务层 deps 走配置
  注入,``LLMService(settings.llm)``),也可传散装参数(api_key/model/base_url...,
  便于测试与历史调用方)。通过判断首参类型分发,避免重复实现两套构造逻辑。
- **httpx.AsyncClient 进程内复用**:每次调用重建 client 会反复 TCP/TLS 握手,
  耗时与资源浪费。client 由 ``__init__`` 创建,生命周期与 service 一致;
  应用关闭时调 :meth:`aclose` 释放连接池。
- **tenacity 重试只针对可恢复错误**:网络异常与 5xx 重试有意义(可能下次成功),
  4xx(参数错/鉴权失败)重试无意义,故 ``retry_if_exception_type`` 只列网络错,
  HTTP 状态码在响应解析里判断 5xx 后手工 raise 网络错以触发重试。
- **SSE 解析手写**:方舟流式响应遵循 OpenAI 的 ``data: {json}\\n\\n`` 格式,
  末尾 ``data: [DONE]``。用逐行扫描 + JSON 解析,避免引入额外 SSE 依赖。
- **错误归一为 ``LLMError``**:上层只需 catch ``LLMError``,不用区分 httpx 超时
  还是方舟 5xx,统一映射为 502 返回前端。

接口契约(被 Pipeline stages / RagService / MemoryService 依赖):
- ``await llm.chat(messages, tools=None) -> {"content","tool_calls","usage"}``
- ``await llm.stream_chat(messages, tools=None) -> AsyncIterator[str]``
- ``await llm.embed(text) -> list[float]``
- ``await llm.embed_batch(texts) -> list[list[float]]``
- ``await llm.summarize(text, *, instruction=None) -> str``
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.exceptions import LLMError
from app.core.logging import get_logger

if TYPE_CHECKING:
    # 仅类型注解用,运行期不强依赖,避免循环 import。
    from app.config import LLMConfig

_logger = get_logger(__name__)

# ------------------------------------------------------------------
# 类型别名:让签名更可读
# ------------------------------------------------------------------
# OpenAI 兼容消息结构:{role, content, tool_calls?, tool_role_id?, name?}
Message = dict[str, Any]
# OpenAI Function Calling 工具声明:{type: "function", function: {...}}
ToolSpec = dict[str, Any]

# 默认 Embedding 维度:当未通过 config 提供时使用(方舟常见 1024 维)。
# 通过 LLMConfig 注入时以配置为准。
_DEFAULT_EMBEDDING_DIMENSION = 1024


class LLMService:
    """火山方舟 LLM 服务(对话 / 流式对话 / Embedding / 摘要)。

    所有方法均为 ``async``,在事件循环中调用。重试与超时在内部处理,
    成功返回业务数据,失败抛 ``LLMError``。

    双模式构造(二选一,通过首参类型判断分发):

    1. **配置对象模式**(业务层推荐)::

           LLMService(settings.llm)          # settings.llm 是 LLMConfig

    2. **散装参数模式**(测试/历史调用方)::

           LLMService(api_key, model, base_url,
                      max_tokens=2048, temperature=0.7,
                      timeout=60.0, max_retries=3, embedding_model="")

    Args:
        config_or_api_key: ``LLMConfig`` 实例(配置模式)或 api_key 字符串(散装模式)。
        model: 散装模式下的默认对话模型(如 ``deepseek-v4-flash``);配置模式忽略。
        base_url: 散装模式下方舟 OpenAI 兼容 API 根;配置模式忽略。
        max_tokens: 散装模式下单次生成上限。
        temperature: 散装模式下采样温度,0 更确定、2 更发散。
        timeout: 散装模式下单次 HTTP 调用超时(秒)。
        max_retries: 散装模式下失败重试次数(不含首次),0 表示不重试。
        embedding_model: 散装模式下 Embedding 接入点 ID(方舟 endpoint)。
        embedding_dimension: 散装模式下向量维度(默认 1024)。
    """

    def __init__(
        self,
        config_or_api_key: "str | LLMConfig",
        model: str = "",
        base_url: str = "",
        max_tokens: int = 2048,
        temperature: float = 0.7,
        timeout: float = 60.0,
        max_retries: int = 3,
        embedding_model: str = "",
        embedding_dimension: int = _DEFAULT_EMBEDDING_DIMENSION,
    ) -> None:
        # ------------------------------------------------------------------
        # 双模式分发:首参为 LLMConfig -> 从配置取值;否则按散装参数处理。
        # 用 hasattr 而非 isinstance,避免运行期 import LLMConfig 形成硬依赖,
        # 也兼容 duck-typing 的配置对象(便于测试传 dataclass/namespace)。
        # ------------------------------------------------------------------
        cfg = config_or_api_key
        if hasattr(cfg, "api_key") and hasattr(cfg, "base_url") and hasattr(cfg, "model"):
            # 配置模式:从 LLMConfig 取值,散装参数被忽略
            self._api_key = _secret_value(cfg.api_key)
            self._model = cfg.model
            self._max_tokens = getattr(cfg, "max_tokens", max_tokens)
            self._temperature = getattr(cfg, "temperature", temperature)
            self._max_retries = getattr(cfg, "max_retries", max_retries)
            self._embedding_model = getattr(cfg, "embedding_model", "") or ""
            self._embedding_dimension = getattr(
                cfg, "embedding_dimension", _DEFAULT_EMBEDDING_DIMENSION
            )
            cfg_base_url = getattr(cfg, "base_url", base_url)
        else:
            # 散装模式:首参当作 api_key 字符串
            self._api_key = str(cfg)
            self._model = model
            self._max_tokens = max_tokens
            self._temperature = temperature
            self._max_retries = max_retries
            self._embedding_model = embedding_model
            self._embedding_dimension = embedding_dimension
            cfg_base_url = base_url

        # 规范化 base_url:去尾部斜杠,后续路径自带前导斜杠,拼接结果唯一
        self._base_url = cfg_base_url.rstrip("/")

        # 连接池复用:limits 控制连接数,默认即可;timeout 单独传更清晰。
        # Authorization 头在每个请求都带,放 default_headers 避免重复设置。
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        self._logger = _logger.bind(model=self._model, base_url=self._base_url)

    # ------------------------------------------------------------------
    # 构造便捷入口
    # ------------------------------------------------------------------
    @classmethod
    def from_settings(cls, config: "LLMConfig") -> "LLMService":
        """从 ``LLMConfig`` 构造(等价于 ``LLMService(config)``,语义更明确)。

        供 deps.get_llm_service 使用,显式表达"基于配置创建"的意图。
        """
        return cls(config)

    @property
    def embedding_dimension(self) -> int:
        """当前 Embedding 向量维度(供 EmbeddingService 等下游消费)。

        从 config 或散装参数透传,默认 1024。零向量占位等场景按此维度构造。
        """
        return self._embedding_dimension

    # ------------------------------------------------------------------
    # 公开 API:对话
    # ------------------------------------------------------------------
    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> dict[str, Any]:
        """非流式对话,一次性返回完整结果。

        Args:
            messages: OpenAI 兼容消息序列(system/user/assistant/tool)。
            tools: 可选的 Function Calling 工具声明;为空或 None 表示不启用。

        Returns:
            归一化结果字典::

                {
                  "content": str,            # 文本回复(可能为空,当模型选择调工具时)
                  "tool_calls": list[dict],  # 工具调用请求,无则为 []
                  "usage": dict,             # token 用量,无则为 {}
                }

            归一化而不是直接透传原始 JSON,是为了让上层不依赖方舟具体字段路径。

        Raises:
            LLMError: 网络/超时/5xx/响应解析失败时统一抛出。
        """
        payload = self._build_payload(messages, tools, stream=False)
        data = await self._request_with_retry("POST", "/chat/completions", payload)
        return self._parse_chat_response(data)

    async def stream_chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> AsyncIterator[str]:
        """流式对话,逐段 yield 文本 content。

        解析 SSE 帧 ``data: {json}\\n\\n``,取 ``choices[0].delta.content``。
        遇到 ``data: [DONE]`` 结束。usage 在最后一帧(若开启 include_usage),
        但本方法只 yield content,usage 由调用方自行从 chunk 收集--保持单一职责。

        注意:流式下工具调用以增量 tool_calls 形式出现在 delta 中,
        本方法不聚合 tool_calls(只产出文本),工具调用场景请用 :meth:`chat`。
        这样简化了流式实现,工具调用走非流式更可控。

        Args:
            messages: OpenAI 兼容消息序列。
            tools: 可选工具声明。

        Yields:
            文本片段字符串(可能为空字符串,调用方应过滤)。

        Raises:
            LLMError: 网络/超时/5xx/SSE 解析失败时抛出。
        """
        payload = self._build_payload(messages, tools, stream=True)

        # 流式重试:tenacity 的 AsyncRetrying 包裹整个流读取过程,
        # 一旦中途网络中断会重试整个请求(已 yield 的内容无法收回,
        # 这是流式重试的固有限制;调用方 Pipeline 在重试时重新开流即可)。
        async for attempt in self._retrying():
            with attempt:
                # stream=True 让 httpx 不缓冲完整响应,边收边给
                async with self._client.stream(
                    "POST", "/chat/completions", json=payload
                ) as response:
                    if response.status_code >= 500:
                        # 5xx 视为可重试错误:读完 body 释放连接再抛
                        await response.aread()
                        self._logger.warning(
                            "llm.stream.5xx",
                            status_code=response.status_code,
                        )
                        raise httpx.HTTPError(
                            f"方舟返回 {response.status_code}"
                        )
                    if response.status_code >= 400:
                        # 4xx 不重试:直接转 LLMError(参数/鉴权错误重试无意义)
                        body = await response.aread()
                        raise LLMError(
                            "LLM 请求被拒绝",
                            detail={
                                "status": response.status_code,
                                "body": body.decode("utf-8", "ignore")[:500],
                            },
                        )

                    async for chunk in self._iter_sse_content(response):
                        yield chunk
                    return  # 成功走完流,退出重试循环

    # ------------------------------------------------------------------
    # 公开 API:Embedding
    # ------------------------------------------------------------------
    async def embed(self, text: str) -> list[float]:
        """调用 embedding 接口,返回单条文本向量。

        用于 RAG 文档入库向量化与检索时查询向量化。模型由构造时(config 或
        散装参数)的 ``embedding_model`` 决定。未配置时直接报错而非发空模型请求。

        空文本不调上游,返回零向量(按 ``embedding_dimension`` 维度),避免浪费配额。

        Args:
            text: 待向量化的文本。

        Returns:
            浮点向量(list[float])。

        Raises:
            LLMError: 调用失败时抛出。
        """
        if not text or not text.strip():
            # 空文本返回零向量,维度对齐配置,避免浪费配额
            return [0.0] * self._embedding_dimension

        if not self._embedding_model:
            # 未配置 embedding 模型时直接报错,避免发出无意义的请求
            raise LLMError(
                "未配置 Embedding 模型(ARK_EMBEDDING_MODEL 为空)",
                detail={"hint": "请在 .env 设置 ARK_EMBEDDING_MODEL"},
            )

        payload = {
            "model": self._embedding_model,
            "input": text,
        }
        data = await self._request_with_retry("POST", "/embeddings", payload)
        try:
            return [float(x) for x in data["data"][0]["embedding"]]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(
                "Embedding 响应解析失败",
                detail={"raw": str(data)[:500]},
            ) from exc

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量向量化(单次请求传多条 input,减少 round-trip)。

        上游按 input 列表顺序返回 data[*].embedding,这里按 index 排序后取值,
        保证顺序与输入一致(部分实现不保证顺序)。空串位置回填零向量。

        Args:
            texts: 待向量化的文本列表。

        Returns:
            与输入等长的向量列表;空输入返回空列表。
        """
        if not texts:
            return []

        # 过滤空串为占位,避免上游拒绝空 input;结果再回填零向量。
        non_empty_indices = [i for i, t in enumerate(texts) if t.strip()]
        if not non_empty_indices:
            return [[0.0] * self._embedding_dimension for _ in texts]
        if not self._embedding_model:
            raise LLMError(
                "未配置 Embedding 模型(ARK_EMBEDDING_MODEL 为空)",
                detail={"hint": "请在 .env 设置 ARK_EMBEDDING_MODEL"},
            )

        payload_input = [texts[i] for i in non_empty_indices]
        payload = {
            "model": self._embedding_model,
            "input": payload_input,
        }
        data = await self._request_with_retry("POST", "/embeddings", payload)
        try:
            # 按 index 排序保证顺序对齐输入
            items = sorted(data["data"], key=lambda d: d.get("index", 0))
            vecs = [[float(x) for x in it["embedding"]] for it in items]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(
                "Embedding 批量响应解析失败",
                detail={"raw": str(data)[:500]},
            ) from exc

        # 回填空串位置为零向量
        result: list[list[float]] = [
            [0.0] * self._embedding_dimension for _ in texts
        ]
        for idx, vec in zip(non_empty_indices, vecs):
            result[idx] = vec
        return result

    # ------------------------------------------------------------------
    # 公开 API:摘要(供 MemoryService.summarize_if_too_long 使用)
    # ------------------------------------------------------------------
    async def summarize(
        self, text: str, *, instruction: str | None = None, max_tokens: int = 512
    ) -> str:
        """用 LLM 把长文本压缩成摘要。

        Args:
            text: 待摘要文本(如多轮对话历史)。
            instruction: 自定义摘要指令;None 用默认"客观保留关键信息"。
            max_tokens: 摘要最大长度。

        Returns:
            摘要文本。
        """
        sys = (
            instruction
            or "你是客服对话摘要助手。请把以下对话历史压缩成一段简洁摘要,"
            "保留用户诉求、已提供的方案、待办事项等关键信息,不要编造。"
        )
        messages = [
            {"role": "system", "content": sys},
            {"role": "user", "content": text},
        ]
        # 复用 chat:取归一化结果的 content 字段
        result = await self.chat(messages)
        return result.get("content", "") or ""

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def aclose(self) -> None:
        """关闭底层 HTTP 连接池。应用关闭时调用,避免连接泄漏。"""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # 内部:请求构造与解析
    # ------------------------------------------------------------------
    def _build_payload(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        """构造发往方舟的请求体。

        stream=True 时额外加 ``stream_options.include_usage``,让最后一帧带上
        token 用量,供计费与限流统计;非流式响应本身已含 usage 字段。
        tools 仅在非空时加入,避免给方舟传空数组导致行为不确定。
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
            "stream": stream,
        }
        if stream:
            # 让流式最后一帧返回 usage,否则流式拿不到 token 用量
            payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = tools
        return payload

    def _parse_chat_response(self, data: dict[str, Any]) -> dict[str, Any]:
        """把方舟非流式响应归一化为 {content, tool_calls, usage}。

        防御性解析:每层都用 .get 并给默认值,避免字段缺失抛 KeyError。
        """
        try:
            choice = data["choices"][0]
            message = choice.get("message", {})
            content = message.get("content") or ""
            tool_calls = message.get("tool_calls") or []
            usage = data.get("usage") or {}
            return {
                "content": content,
                "tool_calls": tool_calls,
                "usage": usage,
            }
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(
                "LLM 响应解析失败",
                detail={"raw": str(data)[:500]},
            ) from exc

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """发非流式请求并返回 JSON,带重试。

        5xx 与网络错会触发重试(由 tenacity 控制),4xx 直接转 ``LLMError`` 不重试。
        """
        async for attempt in self._retrying():
            with attempt:
                try:
                    response = await self._client.request(method, url, json=payload)
                except httpx.HTTPError:
                    # 网络层错误(连接/读/写超时)交给 tenacity 重试
                    self._logger.warning("llm.request.network_error", url=url)
                    raise

                if response.status_code >= 500:
                    # 5xx 视为可重试:抛 HTTPError 触发 tenacity
                    self._logger.warning(
                        "llm.request.5xx",
                        url=url,
                        status_code=response.status_code,
                    )
                    raise httpx.HTTPError(
                        f"方舟返回 {response.status_code}"
                    )
                if response.status_code >= 400:
                    # 4xx 不可恢复:直接抛 LLMError,不进重试
                    raise LLMError(
                        "LLM 请求被拒绝",
                        detail={
                            "status": response.status_code,
                            "body": response.text[:500],
                        },
                    )
                return response.json()

        # tenacity 正常情况下会在循环内 return;走到这里说明重试耗尽仍未成功
        raise LLMError("LLM 调用重试耗尽")

    async def _iter_sse_content(
        self, response: httpx.Response
    ) -> AsyncIterator[str]:
        """解析 SSE 流,逐段 yield ``delta.content`` 文本。

        SSE 帧格式:``data: {json}\\n\\n``。可能跨 chunk 边界,httpx 的
        ``aiter_lines`` 已按换行切分,但一个 ``data:`` 行内 JSON 不会被拆开,
        故逐行处理即可。``data: [DONE]`` 表示流结束。
        """
        async for line in response.aiter_lines():
            # 空行是帧分隔,跳过;非 ``data:`` 前缀的事件(如 event:/id:)忽略
            if not line or not line.startswith("data:"):
                continue
            data_str = line[len("data:"):].strip()
            if data_str == "[DONE]":
                return
            if not data_str:
                continue
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                # 偶发畸形帧:跳过而非中断整个流,保证可用性
                self._logger.warning("llm.sse.bad_json", line=data_str[:120])
                continue
            # 取 choices[0].delta.content;usage 帧可能无 choices,故 .get 兜底
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            content = delta.get("content")
            if content:
                yield content

    def _retrying(self) -> AsyncRetrying:
        """构造 tenacity 重试策略:指数退避,只重试网络/5xx 错误。

        - ``stop_after_attempt``:首次 + max_retries 次,共 max_retries+1 次尝试。
        - ``wait_exponential``:1s, 2s, 4s... 上限 10s,避免退避过久卡住请求。
        - ``retry_if_exception_type(httpx.HTTPError)``:只对网络层错误重试;
          5xx 在调用处被手工转成 ``httpx.HTTPError`` 以纳入重试。
          ``LLMError``(4xx 等)不在重试列表,会直接抛出。
        """
        return AsyncRetrying(
            stop=stop_after_attempt(max(1, self._max_retries + 1)),
            wait=wait_exponential(multiplier=1, max=10),
            retry=retry_if_exception_type(httpx.HTTPError),
            reraise=True,
        )


def _secret_value(value: Any) -> str:
    """从 ``SecretStr`` 或普通字符串/None 安全取出明文。

    pydantic ``SecretStr`` 的实际值用 ``get_secret_value()`` 取;裸字符串直接用;
    None 兜底为空串。集中处理避免调用方到处判断类型。
    """
    if value is None:
        return ""
    get_secret = getattr(value, "get_secret_value", None)
    if callable(get_secret):
        return get_secret()
    return str(value)
