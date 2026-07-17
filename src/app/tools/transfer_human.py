"""转人工工具。

当 LLM 判断无法解决(投诉升级、复杂纠纷、用户明确要求人工)时调用,创建
一张工单并把会话标记为待接入人工。返回工单号供用户留存查询。

"创建工单"这里用内存自增 ID 模拟,真实环境接入工单系统/客服派单系统即可。
"""

from __future__ import annotations

import itertools
from typing import Any, ClassVar

from app.core.logging import get_logger
from app.tools.base import Tool

_logger = get_logger(__name__)

# 进程内自增工单号生成器,仅模拟用;真实环境由工单系统分配全局唯一 ID。
# itertools.count 线程安全(单次 next 原子),但协程并发下仍需注意:这里
# 仅演示,生产应用 DB 序列或分布式 ID。
_ticket_seq = itertools.count(1)


class TransferHumanTool(Tool):
    """转接人工客服并创建工单。

    调用后会话进入"转人工"状态,工单号返回给用户。LLM 应在调用前已尝试
    自助解决,本工具作为最终兜底。
    """

    name: ClassVar[str] = "transfer_human"
    description: ClassVar[str] = (
        "转接人工客服并创建工单。当用户明确要求人工、或投诉/纠纷问题复杂"
        "AI 无法处理时调用。调用后用户将被接入人工坐席。"
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "转人工原因(如:用户要求/投诉升级/超出能力范围)",
            },
            "priority": {
                "type": "string",
                "enum": ["low", "normal", "high", "urgent"],
                "description": "工单优先级,投诉类默认 high",
            },
        },
        "required": ["reason"],
    }

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """创建转人工工单。

        Args:
            reason: 转人工原因。
            priority: 优先级,默认 normal。

        Returns:
            ``{transferred: true, ticket_id, reason, priority}``;
            ``transferred`` 恒为 true(工具成功即代表已转),前端据此切换 UI。
        """
        reason = str(kwargs.get("reason") or "用户请求转人工").strip()
        priority = str(kwargs.get("priority") or "normal").strip()

        # 生成工单号:TK + 6 位零填充序号,便于人工识别
        ticket_id = f"TK{next(_ticket_seq):06d}"

        _logger.info(
            "transfer_human.ticket_created",
            ticket_id=ticket_id,
            reason=reason,
            priority=priority,
        )
        # 真实环境此处应:写工单表 + 推送到客服派单队列 + 更新会话状态
        return {
            "transferred": True,
            "ticket_id": ticket_id,
            "reason": reason,
            "priority": priority,
        }
