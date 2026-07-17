"""对话上下文 ``DialogContext``。

贯穿一次对话请求全流程的可变载体:Pipeline 的每个阶段都读取其中的字段、
产出新字段写回,下游阶段据此消费。本质上是一个"带类型注解的共享黑板
(blackboard)"。

设计要点:
- **可变 dataclass**:阶段间传递同一实例并就地修改,避免每阶段深拷贝带来的开销
  与字段漂移风险。所有可变集合字段显式使用 ``default_factory``,否则会被所有
  实例共享(mutable default 是 Python 经典陷阱)。
- **字段语义分四区**:
  1. 请求标识(session_id / user_id)+ 原始/清洗输入;
  2. 中间产出(is_safe / intent / entities / emotion / retrieved_docs / strategy);
  3. 执行轨迹(tool_calls / llm_usage / stage_metrics),供可观测性与计费;
  4. 短路控制(short_circuit / short_circuit_reply):ContentGuard 不通过时
     直接跳过后续阶段并返回拒答,省 LLM 调用成本。
- ``messages`` 采用 OpenAI 兼容的 ``{role, content}``` 结构,便于直接喂给
  方舟 OpenAI 兼容接口与构造提示词,无需二次转换。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DialogContext:
    """一次对话请求的完整上下文载体。

    Attributes:
        session_id: 会话 ID,关联持久化 ``sessions`` 表,跨轮记忆以此聚合。
        user_id: 用户 ID,用于鉴权审计与用户画像查询。
        user_input: 用户原始输入(未做清洗/归一化),保留原文以便排查与回放。
        cleaned_input: 经 InputParser 清洗/归一化后的文本,后续阶段统一消费它。
        messages: OpenAI 兼容的消息序列 ``[{role, content, ...}]``,承载对话历史
            与本轮新增消息;阶段产出可直接追加,生成阶段据此构造 prompt。
        is_safe: ContentGuard 判定结果。False 时流水线短路返回拒答。
        short_circuit: 是否触发短路(内容不安全、兜底降级等)。短路后
            ``short_circuit_reply`` 即为最终回复,后续阶段不再执行。
        short_circuit_reply: 短路时直接下发给用户的回复文本。
        intent: 意图标签(chat/faq/order/complaint/transfer/...),决定路由与检索策略。
        entities: 本轮抽取到的实体,``{order_no, name, ...}``,供工具调用与画像更新。
        emotion: 情绪标签(可选,用于策略层调整语气,如愤怒用户优先转人工)。
        retrieved_docs: RAG 检索命中的知识片段,``[{content, source, score, ...}]``。
        strategy: 策略注入阶段的产出(prompt 模板、系统提示、温度覆盖等),供生成阶段使用。
        tool_calls: 本轮触发的 Function Calling 调用记录,供审计与落库。
        llm_usage: 累计 token 用量 ``{prompt_tokens, completion_tokens, ...}``,供计费与限流。
        stage_metrics: 各阶段耗时与状态,``{stage_name: {duration_ms, status}}``。
    """

    # ---- 1. 请求标识与输入 ----
    session_id: str = ""
    user_id: str = ""
    user_input: str = ""
    cleaned_input: str = ""

    # ---- 2. 对话历史(OpenAI 兼容结构)----
    messages: list[dict[str, Any]] = field(default_factory=list)

    # ---- 3. 中间产出 ----
    is_safe: bool = True
    intent: str = ""
    entities: dict[str, Any] = field(default_factory=dict)
    emotion: str = ""
    retrieved_docs: list[dict[str, Any]] = field(default_factory=list)
    strategy: dict[str, Any] = field(default_factory=dict)

    # ---- 4. 执行轨迹与可观测性 ----
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    llm_usage: dict[str, Any] = field(default_factory=dict)
    stage_metrics: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ---- 5. 短路控制 ----
    short_circuit: bool = False
    short_circuit_reply: str = ""

    # ---- 6. 阶段补充产出(向后兼容追加,带默认值不影响已有逻辑)----
    # 意图分类置信度:IntentClassifier 写入,0.0-1.0;低于阈值时已降级为闲聊。
    intent_confidence: float = 0.0
    # 最终完整回复文本:StreamGenerator 累积(流式逐段拼、非流式整段)。
    # 注:非流式 Runner 也会读 ``strategy["reply"]`` 作为兜底来源,两者择一即可。
    full_reply: str = ""
    # 阶段间杂项传递:避免频繁扩字段;非结构化补充信息放此处。
    metadata: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # 消息追加:统一走这两个方法,确保 role/content 结构一致,
    # 避免各阶段手写 dict 导致字段名漂移(如 content 拼成 text)。
    # ------------------------------------------------------------------
    def add_user_message(self, content: str) -> None:
        """追加一条 user 消息到历史。

        Args:
            content: 用户消息正文。
        """
        self.messages.append({"role": "user", "content": content})

    def add_assistant_message(self, content: str) -> None:
        """追加一条 assistant 消息到历史。

        在生成结束后调用,使下一轮请求能拿到上一轮回复保持上下文连贯。

        Args:
            content: 助手消息正文(完整回复文本,非流式片段)。
        """
        self.messages.append({"role": "assistant", "content": content})

    def summary(self) -> str:
        """返回可读的上下文摘要,用于日志/排障,非业务逻辑返回值。

        只拼接关键字段与历史消息条数,避免把完整 messages(可能很长)灌进日志。
        """
        return (
            "DialogContext("
            f"session_id={self.session_id!r}, "
            f"user_id={self.user_id!r}, "
            f"intent={self.intent!r}, "
            f"is_safe={self.is_safe}, "
            f"short_circuit={self.short_circuit}, "
            f"emotion={self.emotion!r}, "
            f"messages={len(self.messages)}, "
            f"retrieved_docs={len(self.retrieved_docs)}, "
            f"tool_calls={len(self.tool_calls)}"
            ")"
        )
