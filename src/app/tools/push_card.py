"""推卡片工具。

当用户需要可视化信息(商品卡片、订单卡片、活动卡片)时,LLM 调用本工具
返回卡片结构,前端据此渲染富文本卡片而非纯文本。卡片数据与文本回复并行
下发:文本解释 + 卡片展示,体验更好。

返回结构含 ``card_type`` 让前端选择不同渲染模板。
"""

from __future__ import annotations

from typing import Any, ClassVar

from app.core.logging import get_logger
from app.tools.base import Tool

_logger = get_logger(__name__)


class PushCardTool(Tool):
    """向用户推送一张富文本卡片。

    适用场景:商品咨询展示商品图与价格、活动展示优惠券、订单展示关键信息。
    LLM 决定何时推、推什么内容;本工具只负责结构化打包。
    """

    name: ClassVar[str] = "push_card"
    description: ClassVar[str] = (
        "向用户推送一张富文本卡片(商品/活动/订单卡片),"
        "用于需要图文展示的场景。调用后前端会渲染卡片。"
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "card_type": {
                "type": "string",
                "enum": ["product", "promotion", "order", "generic"],
                "description": "卡片类型,决定前端渲染模板",
            },
            "title": {
                "type": "string",
                "description": "卡片标题",
            },
            "content": {
                "type": "string",
                "description": "卡片正文内容(支持简单 markdown)",
            },
            "image_url": {
                "type": "string",
                "description": "卡片配图 URL,可省略",
            },
        },
        "required": ["card_type", "title", "content"],
    }

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """构造卡片数据。

        Args:
            card_type: 卡片类型(product/promotion/order/generic)。
            title: 卡片标题。
            content: 卡片正文。
            image_url: 可选配图 URL。

        Returns:
            ``{card_type, title, content, image_url}`` 结构化卡片;
            缺少必填字段时回退为 generic 类型并保留可用部分,保证不报错。
        """
        card_type = str(kwargs.get("card_type") or "generic").strip()
        title = str(kwargs.get("title") or "").strip()
        content = str(kwargs.get("content") or "").strip()
        image_url = str(kwargs.get("image_url") or "").strip() or None

        if not title or not content:
            # 必填缺失:降级为 generic 卡,避免 LLM 因参数不全导致工具失败
            _logger.warning(
                "push_card.incomplete_args",
                card_type=card_type,
                has_title=bool(title),
                has_content=bool(content),
            )
            card_type = "generic"

        _logger.info("push_card.sent", card_type=card_type, title=title)
        return {
            "card_type": card_type,
            "title": title,
            "content": content,
            "image_url": image_url,
        }
