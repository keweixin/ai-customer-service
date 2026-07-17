"""阶段 1:输入清洗 ``InputParser``。

职责:把用户原始输入归一化为干净文本,供后续所有阶段统一消费。

为什么要清洗:
- 用户输入可能含多余空白、控制字符、超长内容,直接喂给 LLM 会污染 prompt、
  浪费 token、甚至触发上游限制;
- 清洗后的 ``cleaned_input`` 是后续意图识别/实体抽取/RAG 检索的统一输入源,
  各阶段不必各自处理脏数据,逻辑更纯粹。

本阶段不依赖任何外部服务(纯函数式),故无需注入。
"""

from __future__ import annotations

import re
import unicodedata

from app.core.logging import get_logger
from app.pipeline.context import DialogContext
from app.pipeline.stages.base import BaseStage

_logger = get_logger(__name__)

# 单次输入最大字符数:超长截断,避免恶意超长输入耗尽 LLM token 预算。
# 2000 字对客服场景足够(超出通常是粘贴大段文本,截断后提示即可)。
_MAX_INPUT_CHARS = 2000

# 控制字符(除常见换行/制表符外)正则:用 \p{Cc} 思路,Python re 无 \p,
# 故用 unicodedata.category 过滤更准确;这里预编译空白折叠正则。
_WHITESPACE_RE = re.compile(r"\s+")


class InputParser(BaseStage):
    """清洗用户输入。

    处理步骤:strip -> 去控制字符 -> 折叠连续空白 -> 截断到上限。
    顺序很重要:先去控制字符再折叠,避免控制字符与空白混合时折叠不干净。
    """

    name = "InputParser"

    async def run(self, ctx: DialogContext) -> DialogContext:
        """清洗 ``ctx.user_input`` 并写入 ``ctx.cleaned_input``。

        原始输入保留在 ``user_input`` 不变(用于审计/回放),清洗结果单独存放,
        两者解耦便于排查"清洗是否丢了信息"。

        Args:
            ctx: 对话上下文,读取 ``user_input``,写回 ``cleaned_input``。

        Returns:
            更新后的 ctx(就地修改后返回同一实例)。
        """
        raw = ctx.user_input or ""
        cleaned = self._clean(raw)
        ctx.cleaned_input = cleaned

        # 截断时记日志,便于发现"用户粘贴大段文本"这类异常使用模式
        if len(raw) > _MAX_INPUT_CHARS:
            _logger.info(
                "input_parser.truncated",
                raw_len=len(raw),
                max_chars=_MAX_INPUT_CHARS,
            )
        return ctx

    @staticmethod
    def _clean(text: str) -> str:
        """实际清洗逻辑,抽为静态方法便于单测独立验证。

        Args:
            text: 原始文本。

        Returns:
            清洗后文本。
        """
        # 1. 去首尾空白
        text = text.strip()
        if not text:
            return ""

        # 2. 去控制字符:保留常见换行/制表符(\n \r \t),其余 Cc 类(如 \x00、
        #    零宽字符)剔除。控制字符对语义无贡献且可能干扰 LLM。
        text = "".join(
            ch
            for ch in text
            if ch in ("\n", "\r", "\t")
            or unicodedata.category(ch) != "Cc"
        )

        # 3. 折叠连续空白(含上一步可能残留的多空格)为单个空格,但保留换行。
        #    思路:先把换行临时替换成占位符,折叠空格,再还原换行。
        text = text.replace("\n", "\x00")
        text = _WHITESPACE_RE.sub(" ", text)
        text = text.replace("\x00", "\n")

        # 4. 截断到上限:按字符截,中文按字符数计更直观(非字节)。
        if len(text) > _MAX_INPUT_CHARS:
            text = text[:_MAX_INPUT_CHARS]

        return text.strip()
