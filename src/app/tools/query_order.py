"""订单查询工具。

LLM 在"查询订单"意图且用户提供订单号时调用,返回订单状态、商品、物流。
当前用模拟数据(便于端到端跑通),真实实现预留接口:替换 :meth:`execute`
中的数据来源即可(调订单系统 API / 查 DB)。

模拟而非直接连真实系统:开发期不依赖外部服务可用性,功能可独立验证;
生产切换只需改这一个方法,工具协议不变。
"""

from __future__ import annotations

from typing import Any, ClassVar

from app.core.logging import get_logger
from app.tools.base import Tool

_logger = get_logger(__name__)

# 模拟订单库:order_id -> 订单详情。
# 真实环境替换为 DB/API 调用,这里用字典便于演示与测试覆盖。
_MOCK_ORDERS: dict[str, dict[str, Any]] = {
    "SO20240101001": {
        "order_id": "SO20240101001",
        "status": "已发货",
        "items": [
            {"sku": "iPhone-15-Pro-256G", "name": "iPhone 15 Pro 256G", "qty": 1, "price": 8999.0},
        ],
        "logistics": {
            "carrier": "顺丰速运",
            "tracking_no": "SF1234567890",
            "eta": "2024-01-05",
        },
    },
    "SO20240101002": {
        "order_id": "SO20240101002",
        "status": "待发货",
        "items": [
            {"sku": "AirPods-Pro-2", "name": "AirPods Pro 2", "qty": 1, "price": 1899.0},
        ],
        "logistics": None,
    },
}


class QueryOrderTool(Tool):
    """查询订单详情工具。

    LLM 从用户消息抽取 order_id 后调用本工具,工具返回结构化订单信息,
    LLM 再据此组织自然语言回复给用户。
    """

    name: ClassVar[str] = "query_order"
    description: ClassVar[str] = (
        "根据订单号查询订单状态、商品明细与物流信息。"
        "当用户询问订单进度、物流、商品详情且提供了订单号时调用。"
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "order_id": {
                "type": "string",
                "description": "订单号,如 SO20240101001",
            },
        },
        "required": ["order_id"],
    }

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """查询订单。

        Args:
            order_id: 订单号(必填)。

        Returns:
            ``{order_id, status, items, logistics}``;订单不存在时 status 标记
            ``not_found`` 并把 logistics 置 None,LLM 据此告知用户查无此单。
        """
        order_id = str(kwargs.get("order_id", "")).strip()
        if not order_id:
            # 参数缺失直接返回错误结构,LLM 会向用户索要订单号
            _logger.warning("query_order.missing_order_id")
            return {
                "order_id": "",
                "status": "invalid",
                "items": [],
                "logistics": None,
                "error": "缺少订单号",
            }

        _logger.info("query_order.start", order_id=order_id)
        order = _MOCK_ORDERS.get(order_id)
        if order is None:
            # 查无此单:返回 not_found 让 LLM 礼貌回复,而非抛异常打断流程
            _logger.info("query_order.not_found", order_id=order_id)
            return {
                "order_id": order_id,
                "status": "not_found",
                "items": [],
                "logistics": None,
            }
        # 真实环境此处替换为: await self._order_repo.get(order_id) 或外部 API
        return {
            "order_id": order["order_id"],
            "status": order["status"],
            "items": order["items"],
            "logistics": order["logistics"],
        }
