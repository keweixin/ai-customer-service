"""阶段 7:流式回复生成 ``StreamGenerator``。

职责:把策略注入 system 消息后,调用 LLM 流式生成回复;若 LLM 请求调用工具,
则执行工具把结果回填,再二次生成(直到无工具调用或达到上限)。

关键设计:
- **流式优先** :meth:`stream` 是 Runner 流式模式的入口,逐 token yield 文本,
  供 SSE 端点实时下发;同时累积完整回复到 ``ctx.full_reply``、记录用量到
  ``ctx.llm_usage``。
- **工具调用循环**:LLM 可能先要调工具(query_order)再生成回复。流程:
  1. stream_chat 产出文本(工具调用走非流式 chat 更可控,见下);
  2. 若非流式 chat 返回 tool_calls,逐个执行工具,把结果作为 tool 消息追加,
     再次调 chat;重复直到无 tool_calls 或超过最大轮数(防死循环)。
- **流式与工具的取舍**:OpenAI 流式下 tool_calls 是增量拼接,聚合复杂易错。
  本实现采用"工具调用走非流式 chat、纯文本回复走 stream_chat"的混合策略:
  先用非流式 chat 探测是否需要工具(带 tools),若无工具调用则改用 stream_chat
  流式产出最终回复,兼顾"工具可控"与"文本实时"。
- **max_tool_rounds**:限制工具循环次数(默认 3),防止 LLM 反复调工具不收敛。

工具执行失败不中断:把错误信息作为 tool 结果回填,LLM 会据此向用户说明,
保证对话连续性优于硬失败。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from app.core.logging import get_logger
from app.pipeline.context import DialogContext
from app.pipeline.stages.base import BaseStage
from app.services.llm import LLMService
from app.tools.base import Tool

_logger = get_logger(__name__)

# 工具调用最大轮数:防止 LLM 反复调工具不收敛导致死循环与成本失控。
_MAX_TOOL_ROUNDS = 3


class StreamGenerator(BaseStage):
    """流式回复生成阶段。

    Args:
        llm: LLM 服务。
        tools: 可用工具实例列表。空列表表示不启用 Function Calling。
            注意传入的是 *实例* 而非类,便于调用 ``execute``;实例可复用。
    """

    name = "StreamGenerator"

    def __init__(self, llm: LLMService, tools: list[Tool] | None = None) -> None:
        self._llm = llm
        self._tools: list[Tool] = list(tools) if tools else []
        # name -> Tool 实例,工具调用回填时按名查
        self._tool_map: dict[str, Tool] = {t.name: t for t in self._tools}

    # ------------------------------------------------------------------
    # BaseStage 契约:run(非流式入口)
    # ------------------------------------------------------------------
    async def run(self, ctx: DialogContext) -> DialogContext:
        """非流式生成:执行工具循环 + 一次性生成完整回复。

        Runner 的非流式模式(``Pipeline.run``)调用本方法。注入策略到 system
        消息后,先做工具调用循环,最后取最终回复写入 ``ctx``。

        本方法不 yield;流式场景请用 :meth:`stream`。

        Args:
            ctx: 读 messages/strategy,写 full_reply/llm_usage/tool_calls。

        Returns:
            更新后的 ctx。
        """
        messages = self._prepare_messages(ctx)
        tool_specs = self._tool_specs(ctx)

        final_content, usage, tool_calls = await self._run_tool_loop(
            messages, tool_specs
        )
        ctx.full_reply = final_content
        ctx.llm_usage = usage
        if tool_calls:
            ctx.tool_calls.extend(tool_calls)
        return ctx

    # ------------------------------------------------------------------
    # 流式入口:Runner 通过 getattr(stage, "stream") 调用
    # ------------------------------------------------------------------
    async def stream(self, ctx: DialogContext) -> AsyncIterator[str]:
        """流式生成:逐 token yield 回复文本。

        流程:
        1. 注入策略到 system 消息;
        2. 工具调用循环(非流式 chat 探测+执行),直至 LLM 不再要工具;
        3. 最后一次调用改用 stream_chat 流式产出文本,yield 每个 chunk;
        4. 累积 full_reply、记录 llm_usage。

        工具调用阶段不流式(用户此时看不到中间过程),最终回复才流式下发,
        体验上"先静默调工具、再实时吐字"是可接受的。

        Args:
            ctx: 读 messages/strategy,写 full_reply/llm_usage/tool_calls。

        Yields:
            回复文本片段。
        """
        messages = self._prepare_messages(ctx)
        tool_specs = self._tool_specs(ctx)

        # ---- 工具调用循环(非流式)----
        # 最多 _MAX_TOOL_ROUNDS 轮,每轮:chat -> 若有 tool_calls 则执行并回填
        accumulated_usage: dict[str, Any] = {}
        accumulated_tool_calls: list[dict[str, Any]] = []
        for _ in range(_MAX_TOOL_ROUNDS):
            if not tool_specs:
                # 无工具声明,跳过循环直接进入流式生成
                break
            result = await self._safe_chat(messages, tool_specs)
            self._merge_usage(accumulated_usage, result["usage"])
            tool_calls = result.get("tool_calls") or []
            if not tool_calls:
                # 无工具调用:本轮 chat 已产出最终文本,直接 yield 并结束
                if result["content"]:
                    ctx.full_reply += result["content"]
                    yield result["content"]
                ctx.llm_usage = accumulated_usage
                return
            # 有工具调用:执行工具,把结果作为 tool 消息追加,进入下一轮
            # 同时把 assistant 的 tool_calls 消息也追加,保持对话结构完整
            messages.append(
                {"role": "assistant", "content": result["content"], "tool_calls": tool_calls}
            )
            await self._execute_and_append_tools(
                tool_calls, messages, accumulated_tool_calls
            )

        # ---- 最终流式生成(不带 tools,纯文本回复)----
        # 工具循环结束(或无工具)后,用 stream_chat 流式产出最终回复。
        # 此时不传 tools,避免 LLM 又想调工具导致流式中断。
        full_reply_parts: list[str] = []
        async for chunk in self._llm.stream_chat(messages, tools=None):
            if chunk:
                full_reply_parts.append(chunk)
                ctx.full_reply += chunk
                yield chunk

        # stream_chat 的 usage 在最后一帧;此处不显式收集(保持简单),
        # 把循环阶段累计的 usage 写回即可。如需流式 usage 可扩展。
        ctx.llm_usage = accumulated_usage
        if accumulated_tool_calls:
            ctx.tool_calls.extend(accumulated_tool_calls)

    # ------------------------------------------------------------------
    # 内部:消息与工具准备
    # ------------------------------------------------------------------
    def _prepare_messages(self, ctx: DialogContext) -> list[dict[str, Any]]:
        """构造发给 LLM 的消息序列:在现有 messages 基础上注入策略到 system。

        策略注入方式:若首条是 system 消息,追加 strategy 的 system_prompt_addition;
        否则新建一条 system 消息放最前。这样不破坏历史结构,且策略每轮生效。

        Args:
            ctx: 对话上下文。

        Returns:
            新的消息列表(浅拷贝,不污染 ctx.messages)。
        """
        messages = [dict(m) for m in ctx.messages]
        strategy = ctx.strategy or {}
        addition = strategy.get("system_prompt_addition") or ""
        extra_instruction = strategy.get("extra_instruction") or ""

        # 合并补充文本:addition + extra_instruction
        supplement = "\n\n".join(p for p in [addition, extra_instruction] if p)
        if not supplement:
            return messages

        if messages and messages[0].get("role") == "system":
            # 追加到已有 system 内容后
            existing = messages[0].get("content") or ""
            messages[0] = {**messages[0], "content": f"{existing}\n\n{supplement}"}
        else:
            # 无 system 消息,新建一条置于最前
            messages.insert(0, {"role": "system", "content": supplement})
        return messages

    def _tool_specs(self, ctx: DialogContext) -> list[dict[str, Any]]:
        """根据策略决定是否提供工具声明给 LLM。

        策略 use_tools=False 或无工具时不返回声明,LLM 不会发起工具调用。
        """
        if not self._tools:
            return []
        if not (ctx.strategy or {}).get("use_tools"):
            return []
        return [t.to_openai_spec() for t in self._tools]

    # ------------------------------------------------------------------
    # 内部:工具调用循环(非流式版,供 run 复用)
    # ------------------------------------------------------------------
    async def _run_tool_loop(
        self,
        messages: list[dict[str, Any]],
        tool_specs: list[dict[str, Any]],
    ) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
        """非流式工具循环,返回 (最终文本, 累计 usage, 工具调用记录)。

        Args:
            messages: 初始消息序列(会被追加 tool 消息)。
            tool_specs: 工具声明;空则不循环。

        Returns:
            (final_content, usage, tool_calls_log)
        """
        accumulated_usage: dict[str, Any] = {}
        tool_calls_log: list[dict[str, Any]] = []

        if not tool_specs:
            # 无工具:单次 chat 直接返回
            result = await self._safe_chat(messages, None)
            return result["content"], result["usage"], tool_calls_log

        for _ in range(_MAX_TOOL_ROUNDS):
            result = await self._safe_chat(messages, tool_specs)
            self._merge_usage(accumulated_usage, result["usage"])
            tool_calls = result.get("tool_calls") or []
            if not tool_calls:
                # 无工具调用:当前 content 即最终回复
                return result["content"], accumulated_usage, tool_calls_log
            # 有工具调用:执行并回填,继续循环
            messages.append(
                {"role": "assistant", "content": result["content"], "tool_calls": tool_calls}
            )
            await self._execute_and_append_tools(tool_calls, messages, tool_calls_log)

        # 循环耗尽仍未收敛:返回最后一次 content(可能为空),记录告警
        _logger.warning("stream_generator.tool_loop_exhausted")
        return "", accumulated_usage, tool_calls_log

    async def _execute_and_append_tools(
        self,
        tool_calls: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tool_calls_log: list[dict[str, Any]],
    ) -> None:
        """执行一批工具调用,把结果作为 tool 消息追加到 messages。

        单个工具失败不中断:把错误信息作为结果回填,LLM 据此向用户说明。

        Args:
            tool_calls: LLM 返回的 tool_calls 列表。
            messages: 消息序列(就地追加 tool 消息)。
            tool_calls_log: 工具调用审计记录(就地追加)。
        """
        for call in tool_calls:
            tool_name, args, call_id = self._parse_tool_call(call)
            tool = self._tool_map.get(tool_name)
            if tool is None:
                # LLM 调了未注册的工具:回填错误,让 LLM 知道不可用
                self._append_tool_result(
                    messages, call_id, {"error": f"未知工具: {tool_name}"}
                )
                tool_calls_log.append(
                    {"tool": tool_name, "args": args, "status": "unknown_tool"}
                )
                continue
            try:
                result = await tool.execute(**args)
                status = "ok"
            except Exception as exc:  # noqa: BLE001
                # 工具执行异常:回填错误而非中断,保持对话连续
                result = {"error": f"工具执行失败: {exc!r}"}
                status = "error"
                _logger.warning(
                    "stream_generator.tool_failed",
                    tool=tool_name,
                    error=str(exc),
                )
            self._append_tool_result(messages, call_id, result)
            tool_calls_log.append(
                {"tool": tool_name, "args": args, "status": status, "result": result}
            )

    # ------------------------------------------------------------------
    # 内部:小工具
    # ------------------------------------------------------------------
    async def _safe_chat(
        self, messages: list[dict[str, Any]], tool_specs: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        """调用 LLMService.chat,异常时返回空结果而非抛出。

        生成阶段位于流水线末端,LLM 失败时上层 Runner 会转 PipelineError,
        但工具循环中我们希望尽量容错(回填错误让 LLM 自行处理),
        故此处捕获并返回结构化空结果,由调用方决定是否中止。
        """
        try:
            return await self._llm.chat(messages, tools=tool_specs)
        except Exception as exc:  # noqa: BLE001
            _logger.error("stream_generator.chat_failed", error=str(exc))
            return {"content": "", "tool_calls": [], "usage": {}}

    @staticmethod
    def _parse_tool_call(call: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
        """解析单个 tool_call,返回 (工具名, 参数字典, 调用ID)。

        OpenAI 格式:``{id, type:"function", function:{name, arguments(JSON字符串)}}``
        arguments 是 JSON 字符串需解析,解析失败给空 dict。
        """
        call_id = str(call.get("id") or "")
        function = call.get("function") or {}
        name = str(function.get("name") or "")
        raw_args = function.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            if not isinstance(args, dict):
                args = {}
        except json.JSONDecodeError:
            args = {}
        return name, args, call_id

    @staticmethod
    def _append_tool_result(
        messages: list[dict[str, Any]], call_id: str, result: dict[str, Any]
    ) -> None:
        """把工具执行结果作为 tool 角色消息追加到 messages。

        OpenAI 格式:``{role:"tool", tool_call_id, content(JSON字符串)}``
        content 必须是字符串,故 json.dumps。
        """
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(result, ensure_ascii=False),
            }
        )

    @staticmethod
    def _merge_usage(
        target: dict[str, Any], source: dict[str, Any] | None
    ) -> None:
        """把单次调用的 usage 累加到 target(多轮工具调用时聚合用量)。

        供计费/限流统计;字段缺失按 0 处理。
        """
        if not source:
            return
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            target[key] = target.get(key, 0) + (source.get(key) or 0)
