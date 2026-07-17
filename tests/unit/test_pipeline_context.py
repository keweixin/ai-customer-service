"""DialogContext 单元测试。

被测模块: ``app.pipeline.context``(DialogContext)

DialogContext 是贯穿一次对话请求的可变 dataclass 载体(blackboard 模式):
Pipeline 各阶段读取其字段、产出新字段写回。本测试覆盖消息追加、摘要方法、
默认值与字段可变性。

被测对象是纯 dataclass,无外部依赖,直接实例化测试。
"""

from __future__ import annotations

import pytest

from app.pipeline.context import DialogContext


def _make_context(**kwargs) -> DialogContext:
    """构造一个 DialogContext,提供合理默认值。"""
    defaults = {"session_id": "sess-1", "user_id": "user-1", "user_input": "你好"}
    defaults.update(kwargs)
    return DialogContext(**defaults)


class TestAddMessages:
    """add_user_message / add_assistant_message 行为。"""

    def test_add_user_message(self) -> None:
        """追加 user 消息后,messages 末尾应为 {role:user, content:文本}。"""
        ctx = _make_context()
        ctx.add_user_message("你好")

        msgs = ctx.messages
        assert len(msgs) >= 1
        last = msgs[-1]
        assert last["role"] == "user"
        assert last["content"] == "你好"

    def test_add_assistant_message(self) -> None:
        """追加 assistant 消息后,messages 末尾应为 {role:assistant, content:文本}。"""
        ctx = _make_context()
        ctx.add_user_message("你好")
        ctx.add_assistant_message("您好,有什么可以帮您?")

        msgs = ctx.messages
        last = msgs[-1]
        assert last["role"] == "assistant"
        assert last["content"] == "您好,有什么可以帮您?"

    def test_alternating_user_assistant_order(self) -> None:
        """多轮对话消息顺序应与追加顺序一致。"""
        ctx = _make_context()
        ctx.add_user_message("Q1")
        ctx.add_assistant_message("A1")
        ctx.add_user_message("Q2")
        ctx.add_assistant_message("A2")

        # 取最后 4 条(可能前面有构造时未清空的,但新追加的应连续)
        roles = [m["role"] for m in ctx.messages[-4:]]
        assert roles == ["user", "assistant", "user", "assistant"]
        contents = [m["content"] for m in ctx.messages[-4:]]
        assert contents == ["Q1", "A1", "Q2", "A2"]

    def test_add_message_dict_structure(self) -> None:
        """追加的消息字典应只含 role 与 content 两个键(OpenAI 兼容)。"""
        ctx = _make_context()
        ctx.add_user_message("测试")
        last = ctx.messages[-1]
        assert set(last.keys()) == {"role", "content"}


class TestSummary:
    """summary() 方法返回可读摘要字符串。"""

    def test_summary_returns_string(self) -> None:
        """summary() 应返回非空字符串,用于日志/排障。"""
        ctx = _make_context()
        s = ctx.summary()
        assert isinstance(s, str)
        assert len(s) > 0

    def test_summary_contains_key_fields(self) -> None:
        """summary 应包含 session_id / user_id / intent 等关键字段。"""
        ctx = _make_context(session_id="s-99", user_id="u-88")
        ctx.intent = "查询订单"
        s = ctx.summary()
        assert "s-99" in s
        assert "u-88" in s
        assert "查询订单" in s

    def test_summary_reflects_message_count(self) -> None:
        """summary 应反映当前 messages 条数。"""
        ctx = _make_context()
        ctx.add_user_message("a")
        ctx.add_assistant_message("b")
        s = ctx.summary()
        # summary 含 messages=N
        assert "2" in s

    def test_summary_does_not_dump_full_messages(self) -> None:
        """summary 不应把完整消息内容灌入(避免日志爆炸)。"""
        ctx = _make_context()
        long_text = "x" * 500
        ctx.add_user_message(long_text)
        s = ctx.summary()
        assert long_text not in s, "summary 不应包含完整消息正文"


class TestDefaultValues:
    """新构造 context 的默认值。"""

    def test_default_messages_empty(self) -> None:
        """不传 messages 时默认为空列表。"""
        ctx = DialogContext()
        assert ctx.messages == []

    def test_default_intent_empty(self) -> None:
        """新 context 的 intent 默认为空字符串。"""
        ctx = DialogContext()
        assert ctx.intent == ""

    def test_default_entities_empty_dict(self) -> None:
        """entities 默认为空字典(非 None,避免下游 .get 崩)。"""
        ctx = DialogContext()
        assert ctx.entities == {}

    def test_default_retrieved_docs_empty(self) -> None:
        """retrieved_docs 默认为空列表。"""
        ctx = DialogContext()
        assert ctx.retrieved_docs == []

    def test_default_is_safe_true(self) -> None:
        """is_safe 默认为 True(安全前置,ContentGuard 会覆写)。"""
        ctx = DialogContext()
        assert ctx.is_safe is True

    def test_default_short_circuit_false(self) -> None:
        """short_circuit 默认 False(未短路)。"""
        ctx = DialogContext()
        assert ctx.short_circuit is False

    def test_default_strategy_empty_dict(self) -> None:
        """strategy 默认为空字典。"""
        ctx = DialogContext()
        assert ctx.strategy == {}

    def test_context_holds_user_and_session_id(self) -> None:
        """构造时传入的 session_id / user_id 应被保留。"""
        ctx = _make_context(session_id="s-1", user_id="u-1")
        assert ctx.session_id == "s-1"
        assert ctx.user_id == "u-1"

    def test_default_collections_are_independent_per_instance(self) -> None:
        """可变默认字段(messages/entities 等)不应在实例间共享(dataclass field_factory)。"""
        c1 = DialogContext()
        c2 = DialogContext()
        c1.add_user_message("only-c1")
        assert c1.messages != c2.messages, "实例间 messages 不应共享"
        c1.entities["k"] = "v"
        assert c2.entities == {}, "实例间 entities 不应共享"


class TestMutableFields:
    """中间产出字段可被阶段读写。"""

    def test_intent_can_be_set(self) -> None:
        """intent 可被 IntentClassifier 写入。"""
        ctx = DialogContext()
        ctx.intent = "投诉"
        assert ctx.intent == "投诉"

    def test_entities_can_be_updated(self) -> None:
        """entities 可被 EntityTracker 更新。"""
        ctx = DialogContext()
        ctx.entities["order_id"] = "ORD-1"
        assert ctx.entities == {"order_id": "ORD-1"}

    def test_short_circuit_flow(self) -> None:
        """ContentGuard 可设 short_circuit=True 与 short_circuit_reply。"""
        ctx = DialogContext()
        ctx.is_safe = False
        ctx.short_circuit = True
        ctx.short_circuit_reply = "抱歉,内容无法处理。"
        assert ctx.is_safe is False
        assert ctx.short_circuit is True
        assert ctx.short_circuit_reply == "抱歉,内容无法处理。"

    def test_retrieved_docs_can_be_set(self) -> None:
        """RagRetriever 可写入 retrieved_docs。"""
        ctx = DialogContext()
        ctx.retrieved_docs = [{"content": "退换货政策", "score": 0.9}]
        assert len(ctx.retrieved_docs) == 1
        assert ctx.retrieved_docs[0]["content"] == "退换货政策"

    def test_strategy_can_be_set(self) -> None:
        """StrategyInjector 可写入 strategy 字典。"""
        ctx = DialogContext()
        ctx.strategy = {"tone": "apologetic", "use_tools": True}
        assert ctx.strategy["tone"] == "apologetic"
        assert ctx.strategy["use_tools"] is True
