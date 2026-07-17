"""Function Calling 工具抽象基类与全局注册表。

设计要点:
- **ABC + 抽象 execute**:强制子类实现执行逻辑,构造时不可能遗漏。
- **类属性 name/description/parameters**:作为 OpenAI Function Calling 的
  ``function`` 声明字段。``parameters`` 是 JSON Schema,描述参数类型与必填,
  LLM 据此决定如何填参。把这些做成类属性而非方法,因为它们是静态元信息。
- **全局 ``TOOL_REGISTRY``**:``name -> Tool`` 映射,子类在 ``__init_subclass__``
  自动注册(只要实例化或被 import 即可)。注册表让 StreamGenerator 能按名查工具,
  也方便做权限白名单过滤。
- ``to_openai_spec()`` 把工具转成方舟要求的 ``{type, function}`` 结构,避免
  调用方手动拼装,字段集中维护减少漂移。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar


class Tool(ABC):
    """Function Calling 工具抽象基类。

    子类需声明类属性 ``name`` / ``description`` / ``parameters``,并实现
    :meth:`execute。子类定义后自动注册到全局 ``TOOL_REGISTRY``。

    Attributes:
        name: 工具唯一标识,LLM 据此选择调用;应使用 snake_case。
        description: 给 LLM 看的功能描述,写得清楚 LLM 才会在合适时机调用。
        parameters: JSON Schema dict,描述参数结构。
    """

    # ClassVar 标注这些是类级元信息,而非实例字段;子类必须覆盖
    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    parameters: ClassVar[dict[str, Any]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """子类定义时自动注册到 ``TOOL_REGISTRY``。

        用 ``__init_subclass__`` 而非装饰器:子类只要被 import 就自动登记,
        调用方无需记住"要注册",降低遗漏风险。空 name 的中间抽象类跳过。
        """
        super().__init_subclass__(**kwargs)
        # 跳过未设置 name 的类(通常是中间基类),避免覆盖
        if cls.name:
            TOOL_REGISTRY[cls.name] = cls

    @abstractmethod
    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """执行工具逻辑。

        Args:
            **kwargs: LLM 从 ``parameters`` schema 填充的实参。

        Returns:
            结果字典,会被序列化后作为 ``role=tool`` 消息回填给 LLM,
            供其二次生成回复。返回 dict 而非 str,保持结构化便于 LLM 理解。
        """
        raise NotImplementedError

    def to_openai_spec(self) -> dict[str, Any]:
        """转成方舟/OpenAI Function Calling 的 ``tools`` 元素结构。

        返回形如::
            {
              "type": "function",
              "function": {
                "name": "...",
                "description": "...",
                "parameters": {...}  # JSON Schema
              }
            }
        集中在此拼装,避免调用方手写易错。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# 全局工具注册表:类名 -> 工具类。
# 模块级可变字典在多请求并发下只读(注册发生在 import 期),无需加锁。
TOOL_REGISTRY: dict[str, type[Tool]] = {}


def get_tool_specs(names: list[str] | None = None) -> list[dict[str, Any]]:
    """返回指定工具的 OpenAI spec 列表,供 LLMService.chat 的 tools 参数。

    Args:
        names: 要包含的工具名列表;None 表示全部已注册工具。

    Returns:
        OpenAI 兼容的 tools 声明列表。未注册的名字会被跳过并记日志(容错,
        避免一个工具名拼错导致整个对话失败)。
    """
    if names is None:
        targets = list(TOOL_REGISTRY.keys())
    else:
        targets = names
    specs: list[dict[str, Any]] = []
    for n in targets:
        cls = TOOL_REGISTRY.get(n)
        if cls is None:
            # 容错:跳过未知工具而非抛错,保证对话主流程可用
            continue
        specs.append(cls().to_openai_spec())
    return specs
