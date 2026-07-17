"""Function Calling 工具层。

LLM 通过 OpenAI 兼容的 Function Calling 协议声明可调工具,本包定义工具的
抽象基类与具体实现。工具被注册到全局 ``TOOL_REGISTRY``,Pipeline 的
``StreamGenerator`` 据此构造 ``tools`` 参数下发给 LLM,并在 LLM 决定调用时
执行对应工具把结果回填。

为什么不直接用裸函数 + JSON Schema:
- 工具需要描述(parameters schema)、执行逻辑、错误处理三件事,用一个类聚合更内聚;
- 注册表模式让"新增工具"=新增一个子类并 self-register,无需改动调用方;
- 便于后续加权限校验、审计日志、调用频率限制等横切关注点。
"""

from app.tools.base import Tool, TOOL_REGISTRY
from app.tools.push_card import PushCardTool
from app.tools.query_order import QueryOrderTool
from app.tools.transfer_human import TransferHumanTool

__all__ = [
    "Tool",
    "TOOL_REGISTRY",
    "QueryOrderTool",
    "PushCardTool",
    "TransferHumanTool",
]
