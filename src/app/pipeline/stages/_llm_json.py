"""阶段间共用的 LLM JSON 解析小工具。

LLM 即便被要求"只返回 JSON",实际输出常带 markdown 围栏(```json ... ```)
或前后缀解释文字,直接 ``json.loads`` 会失败。本模块提供容错解析:
提取首个 ``{...}``/``[...]`` 块再解析,失败返回默认值。

放在 ``stages`` 子包内(下划线前缀表私有),供 ContentGuard / IntentClassifier
/ EntityTracker 复用,避免每个阶段重复实现解析逻辑。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 匹配首个 JSON 对象/数组块(贪婪到最外层闭合括号),容忍前后杂文字。
# 用 DOTALL 让 . 匹配换行,因为 JSON 通常多行。
_JSON_BLOCK_RE = re.compile(r"(\{.*\}|\[.*\])", re.DOTALL)


def parse_llm_json(text: str, default: Any = None) -> Any:
    """从 LLM 输出文本中容错解析 JSON。

    解析顺序:
    1. 直接 ``json.loads``(理想情况,LLM 严格只输出 JSON);
    2. 失败则正则提取首个 JSON 块再解析(应对带围栏/解释的情况);
    3. 仍失败返回 ``default``(不抛异常,调用方按默认值降级)。

    Args:
        text: LLM 原始输出文本。
        default: 解析失败时的返回值(如 ``{}`` 或 ``[]``)。

    Returns:
        解析后的 Python 对象,或 ``default``。
    """
    if not text:
        return default
    text = text.strip()
    # 步骤 1:直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 步骤 2:剥离 markdown 围栏后重试
    fenced = _strip_code_fence(text)
    if fenced != text:
        try:
            return json.loads(fenced)
        except json.JSONDecodeError:
            pass
    # 步骤 3:正则提取首个 JSON 块
    match = _JSON_BLOCK_RE.search(fenced)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    return default


def _strip_code_fence(text: str) -> str:
    """去掉 ```json ... ``` 或 ``` ... ``` 围栏,返回内部内容。

    无围栏时原样返回。只处理首尾各一个围栏,足够覆盖 LLM 常见输出格式。
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        # 去掉首行围栏(可能带语言标识 json/python)
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
        if stripped.endswith("```"):
            stripped = stripped[: -len("```")]
    return stripped.strip()
