"""输入解析阶段单元测试。

被测模块: ``app.pipeline.stages.input_parser``(InputParser)

阶段 1:把用户原始输入归一化为干净文本(strip -> 去控制字符 -> 折叠空白 -> 截断)。
实际实现是 ``BaseStage`` 子类,核心清洗逻辑在静态方法 ``_clean``,阶段入口
``async run(ctx)`` 读 ``ctx.user_input`` 写 ``ctx.cleaned_input``。

本测试直接对 ``_clean`` 静态方法做纯函数测试,并对 ``run`` 做集成式断言。
无外部依赖(纯函数式),无需 mock。
"""

from __future__ import annotations

import pytest

from app.pipeline.context import DialogContext
from app.pipeline.stages.input_parser import InputParser, _MAX_INPUT_CHARS


class TestStripWhitespace:
    """首尾空白去除(_clean 第 1 步)。"""

    def test_strip_leading_trailing_whitespace(self) -> None:
        """首尾空格/制表符应被去除。"""
        assert InputParser._clean("   你好世界   ") == "你好世界"

    def test_strip_newlines_at_edges(self) -> None:
        """首尾换行应被去除。"""
        assert InputParser._clean("\n\n你好\n\n") == "你好"

    def test_strip_tabs(self) -> None:
        """首尾制表符应被去除。"""
        assert InputParser._clean("\t\t你好\t") == "你好"

    def test_empty_string_returns_empty(self) -> None:
        """空串清洗后仍为空。"""
        assert InputParser._clean("") == ""

    def test_only_whitespace_returns_empty(self) -> None:
        """纯空白串清洗后为空。"""
        assert InputParser._clean("   \n\t  ") == ""


class TestCollapseWhitespace:
    """连续空白折叠(_clean 第 3 步,保留换行)。"""

    def test_collapse_multiple_spaces(self) -> None:
        """多个连续空格应折叠为单个。"""
        result = InputParser._clean("你好    世界")
        assert "  " not in result
        assert result == "你好 世界"

    def test_collapse_mixed_spaces_and_tabs(self) -> None:
        """空格与制表符混合应折叠为单个空格。"""
        result = InputParser._clean("你好 \t  世界")
        assert "  " not in result
        assert result == "你好 世界"

    def test_preserves_single_newline_in_middle(self) -> None:
        """中间单个换行应被保留(段落语义)。"""
        result = InputParser._clean("第一行\n第二行")
        assert "\n" in result
        assert "第一行" in result
        assert "第二行" in result

    def test_collapse_multiple_newlines_kept(self) -> None:
        """中间换行被保留(实现只折叠空格/制表,换行原样保留)。

        实现策略:把 \\n 暂存为占位符,用 \\s+ 折叠空格/制表,再还原 \\n。
        因此多换行不会被折叠,但换行间的空格/制表会被清理。
        """
        result = InputParser._clean("第一行\n\n\n第二行")
        # 换行被保留(段落语义),结果仍含换行
        assert "\n" in result
        assert "第一行" in result
        assert "第二行" in result


class TestTruncateLongInput:
    """超长输入截断(_clean 第 4 步)。"""

    def test_truncate_long_input(self) -> None:
        """超过 _MAX_INPUT_CHARS 的输入应被截断到上限。"""
        long_text = "a" * (_MAX_INPUT_CHARS + 1000)
        result = InputParser._clean(long_text)
        assert len(result) <= _MAX_INPUT_CHARS

    def test_short_input_not_truncated(self) -> None:
        """短于上限的输入不截断。"""
        result = InputParser._clean("短文本")
        assert result == "短文本"

    def test_exact_max_length_not_truncated(self) -> None:
        """恰好等于上限的输入不截断。"""
        text = "a" * _MAX_INPUT_CHARS
        result = InputParser._clean(text)
        assert len(result) == _MAX_INPUT_CHARS

    def test_truncate_keeps_prefix(self) -> None:
        """截断应保留前缀(丢弃后缀),保证语义连续。"""
        text = "abcdefghij" * 300  # 远超上限
        result = InputParser._clean(text)
        assert result.startswith("abcdefghij")
        assert len(result) <= _MAX_INPUT_CHARS


class TestRemoveControlChars:
    """控制字符移除(_clean 第 2 步,保留 \\n \\r \\t)。"""

    def test_remove_null_byte(self) -> None:
        """NULL 字节应被移除。"""
        result = InputParser._clean("你好\x00世界")
        assert "\x00" not in result
        assert "你好" in result
        assert "世界" in result

    def test_remove_bell_and_backspace(self) -> None:
        """响铃/退格等控制字符应被移除。"""
        result = InputParser._clean("你好\x07\x08世界")
        assert "\x07" not in result
        assert "\x08" not in result

    def test_remove_ansi_escape_sequence(self) -> None:
        """ANSI 转义 ESC 字符应被移除(防终端注入)。

        注:ESC(\x1b) 属 Cc 类控制字符,会被剔除,残留的 [31m 等字面量
        会被空白折叠处理,结果不含 ESC。
        """
        text = "\x1b[31m红色\x1b[0m文字"
        result = InputParser._clean(text)
        assert "\x1b" not in result
        assert "红色" in result
        assert "文字" in result

    def test_keep_normal_punctuation(self) -> None:
        """正常标点(含中文标点)应保留。"""
        result = InputParser._clean("你好,世界!今天天气如何?")
        assert result == "你好,世界!今天天气如何?"

    def test_newline_preserved_tab_collapsed_to_space(self) -> None:
        """\\n 应被保留;\\t 被 \\s+ 折叠为空格(实现把换行暂存后用 \\s+ 折叠)。

        实现策略:把 \\n 暂存为 \\x00,用 ``\\s+``(含 \\t)折叠为单空格,再还原 \\n。
        所以 \\t 不会原样保留,而是变成空格;\\n 保留。
        """
        result = InputParser._clean("行1\n行2\t缩进")
        assert "\n" in result
        # \t 被折叠成空格
        assert "\t" not in result
        assert "行1" in result
        assert "行2" in result
        assert "缩进" in result


class TestRunStage:
    """InputParser.run(ctx) 集成行为(BaseStage 入口)。"""

    @pytest.mark.asyncio
    async def test_run_writes_cleaned_input_to_ctx(self) -> None:
        """run 应把清洗结果写入 ctx.cleaned_input,并保留原始 user_input。"""
        parser = InputParser()
        ctx = DialogContext(user_input="  你好\x00 世界  ")
        result = await parser.run(ctx)

        assert result is ctx, "run 应返回同一 ctx 实例(就地修改)"
        assert ctx.user_input == "  你好\x00 世界  ", "原始输入不应被修改"
        assert "\x00" not in ctx.cleaned_input
        assert ctx.cleaned_input.startswith("你好")

    @pytest.mark.asyncio
    async def test_run_empty_input(self) -> None:
        """空输入 run 后 cleaned_input 应为空字符串。"""
        parser = InputParser()
        ctx = DialogContext(user_input="")
        await parser.run(ctx)
        assert ctx.cleaned_input == ""

    @pytest.mark.asyncio
    async def test_run_truncates_long_input(self) -> None:
        """超长输入 run 后 cleaned_input 不超过上限。"""
        parser = InputParser()
        ctx = DialogContext(user_input="a" * (_MAX_INPUT_CHARS + 500))
        await parser.run(ctx)
        assert len(ctx.cleaned_input) <= _MAX_INPUT_CHARS
