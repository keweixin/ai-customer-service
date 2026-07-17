"""Function Calling 工具单元测试。

被测模块: ``app.tools``(Tool ABC + 各工具实现 + TOOL_REGISTRY)

工具是 LLM Function Calling 的后端实现。实际契约:
- ``Tool`` ABC:类属性 ``name`` / ``description`` / ``parameters``(JSON Schema),
  抽象方法 ``async execute(**kwargs) -> dict``;子类定义时自动注册到 ``TOOL_REGISTRY``。
- ``QueryOrderTool``:查订单(mock 数据,SO20240101001 / SO20240101002)。
- ``PushCardTool``:推卡片。
- ``TransferHumanTool``:转人工 + 创建工单(自增 ticket_id)。
- ``TOOL_REGISTRY``:name -> 工具类 的全局映射。
- ``get_tool_specs(names)``:返回 OpenAI 兼容的 tools 声明列表。

工具用 mock 数据,不发真实请求/不写真实库。
"""

from __future__ import annotations

import pytest

from app.tools import (
    TOOL_REGISTRY,
    PushCardTool,
    QueryOrderTool,
    TransferHumanTool,
)
from app.tools.base import Tool, get_tool_specs


class TestQueryOrderTool:
    """query_order 工具。"""

    @pytest.mark.asyncio
    async def test_query_order_returns_mock_data(self) -> None:
        """存在的订单号应返回订单数据 dict,含 order_id 与 status。"""
        tool = QueryOrderTool()
        result = await tool.execute(order_id="SO20240101001")

        assert isinstance(result, dict)
        assert result["order_id"] == "SO20240101001"
        assert "status" in result
        assert result["status"] == "已发货"

    @pytest.mark.asyncio
    async def test_query_order_returns_items_and_logistics(self) -> None:
        """订单结果应含 items 与 logistics 字段。"""
        tool = QueryOrderTool()
        result = await tool.execute(order_id="SO20240101001")

        assert "items" in result
        assert isinstance(result["items"], list)
        assert len(result["items"]) >= 1
        assert "logistics" in result
        # 已发货订单 logistics 不为 None
        assert result["logistics"] is not None

    @pytest.mark.asyncio
    async def test_query_order_not_found(self) -> None:
        """查不到的订单号应返回 status=not_found,不抛异常。"""
        tool = QueryOrderTool()
        result = await tool.execute(order_id="NOT-EXIST-9999")

        assert isinstance(result, dict)
        assert result["status"] == "not_found"
        assert result["items"] == []

    @pytest.mark.asyncio
    async def test_query_order_missing_order_id(self) -> None:
        """缺 order_id 参数应返回 status=invalid 与 error 信息,不抛异常。"""
        tool = QueryOrderTool()
        result = await tool.execute(order_id="")

        assert result["status"] == "invalid"
        assert "error" in result

    @pytest.mark.asyncio
    async def test_query_order_pending_order(self) -> None:
        """待发货订单(SO20240101002)status 应为"待发货",logistics 为 None。"""
        tool = QueryOrderTool()
        result = await tool.execute(order_id="SO20240101002")
        assert result["status"] == "待发货"
        assert result["logistics"] is None


class TestPushCardTool:
    """push_card 工具(推送卡片消息)。"""

    @pytest.mark.asyncio
    async def test_push_card_returns_card_data(self) -> None:
        """push_card 应返回卡片数据 dict,含 card_type/title/content。"""
        tool = PushCardTool()
        result = await tool.execute(
            card_type="product",
            title="iPhone 15 Pro",
            content="256G 8999 元",
        )

        assert isinstance(result, dict)
        assert result["card_type"] == "product"
        assert result["title"] == "iPhone 15 Pro"
        assert result["content"] == "256G 8999 元"

    @pytest.mark.asyncio
    async def test_push_card_with_image_url(self) -> None:
        """带 image_url 的卡片应保留图片地址。"""
        tool = PushCardTool()
        result = await tool.execute(
            card_type="product",
            title="商品",
            content="描述",
            image_url="https://example.com/img.png",
        )
        assert result["image_url"] == "https://example.com/img.png"

    @pytest.mark.asyncio
    async def test_push_card_missing_required_falls_back_generic(self) -> None:
        """缺必填字段(title/content)时应降级为 generic 卡片,不抛异常。"""
        tool = PushCardTool()
        result = await tool.execute(card_type="product", title="", content="")

        assert result["card_type"] == "generic"

    @pytest.mark.asyncio
    async def test_push_card_image_url_none_when_absent(self) -> None:
        """未传 image_url 时,结果中 image_url 应为 None。"""
        tool = PushCardTool()
        result = await tool.execute(card_type="generic", title="t", content="c")
        assert result["image_url"] is None


class TestTransferHumanTool:
    """transfer_human 工具(转人工 + 创建工单)。"""

    @pytest.mark.asyncio
    async def test_transfer_human_creates_ticket(self) -> None:
        """transfer_human 应创建工单,返回含 ticket_id 的 dict。"""
        tool = TransferHumanTool()
        result = await tool.execute(reason="用户要求转人工")

        assert isinstance(result, dict)
        assert result["transferred"] is True
        assert "ticket_id" in result
        assert isinstance(result["ticket_id"], str)
        # 工单号应为 TK + 6 位数字格式
        assert result["ticket_id"].startswith("TK")
        assert len(result["ticket_id"]) == 8  # TK + 6 位

    @pytest.mark.asyncio
    async def test_transfer_human_returns_transferred_status(self) -> None:
        """转人工成功 transferred 应恒为 True。"""
        tool = TransferHumanTool()
        result = await tool.execute(reason="投诉升级", priority="high")
        assert result["transferred"] is True

    @pytest.mark.asyncio
    async def test_transfer_human_default_reason(self) -> None:
        """未传 reason 时应使用默认原因,不抛异常。"""
        tool = TransferHumanTool()
        result = await tool.execute()
        assert "reason" in result
        assert len(result["reason"]) > 0

    @pytest.mark.asyncio
    async def test_transfer_human_priority_stored(self) -> None:
        """priority 参数应被保留在结果中。"""
        tool = TransferHumanTool()
        result = await tool.execute(reason="x", priority="urgent")
        assert result["priority"] == "urgent"

    @pytest.mark.asyncio
    async def test_transfer_human_ticket_id_increments(self) -> None:
        """连续转人工应生成递增的工单号。"""
        tool = TransferHumanTool()
        r1 = await tool.execute(reason="a")
        r2 = await tool.execute(reason="b")
        assert r1["ticket_id"] != r2["ticket_id"], "工单号应每次不同(自增)"


class TestToolRegistry:
    """工具注册表完整性。"""

    def test_tool_registry_has_all_tools(self) -> None:
        """注册表应包含 query_order / push_card / transfer_human 三个工具。"""
        names = set(TOOL_REGISTRY.keys())
        assert "query_order" in names
        assert "push_card" in names
        assert "transfer_human" in names

    def test_registry_maps_name_to_tool_subclass(self) -> None:
        """注册表的值应为 Tool 的子类。"""
        for name, cls in TOOL_REGISTRY.items():
            assert issubclass(cls, Tool), f"{name} 不是 Tool 子类"

    def test_each_tool_has_name_description_parameters(self) -> None:
        """每个工具类应有非空 name / description / parameters。"""
        for name, cls in TOOL_REGISTRY.items():
            assert cls.name == name, f"{name} 的 name 属性不匹配"
            assert cls.description, f"{name} 缺 description"
            assert isinstance(cls.parameters, dict), f"{name} parameters 应为 dict"
            assert cls.parameters, f"{name} parameters 不应为空"

    def test_tool_parameters_is_json_schema(self) -> None:
        """工具 parameters 应是合法 JSON Schema(type=object)。"""
        for name, cls in TOOL_REGISTRY.items():
            params = cls.parameters
            assert params.get("type") == "object", f"{name} parameters.type 应为 object"
            assert "properties" in params, f"{name} parameters 应含 properties"

    def test_to_openai_spec_structure(self) -> None:
        """to_openai_spec 应返回 {type:function, function:{name,description,parameters}}。"""
        tool = QueryOrderTool()
        spec = tool.to_openai_spec()
        assert spec["type"] == "function"
        assert spec["function"]["name"] == "query_order"
        assert "description" in spec["function"]
        assert "parameters" in spec["function"]


class TestGetToolSpecs:
    """get_tool_specs 辅助函数。"""

    def test_get_all_tool_specs(self) -> None:
        """不传 names 时应返回全部已注册工具的 spec。"""
        specs = get_tool_specs()
        assert isinstance(specs, list)
        assert len(specs) >= 3
        names = [s["function"]["name"] for s in specs]
        assert "query_order" in names
        assert "push_card" in names
        assert "transfer_human" in names

    def test_get_specific_tool_specs(self) -> None:
        """传 names 时应只返回指定的工具 spec。"""
        specs = get_tool_specs(["query_order"])
        assert len(specs) == 1
        assert specs[0]["function"]["name"] == "query_order"

    def test_get_tool_specs_skips_unknown(self) -> None:
        """未知工具名应被跳过(容错),不抛异常。"""
        specs = get_tool_specs(["query_order", "nonexistent_tool"])
        assert len(specs) == 1
        assert specs[0]["function"]["name"] == "query_order"
