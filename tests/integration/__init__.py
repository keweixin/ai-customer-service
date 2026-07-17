"""集成测试包。

集成测试用 FastAPI TestClient 走完整 HTTP 请求-响应链路,
DB 用内存 SQLite,LLM / RAG / 向量检索全 mock。
覆盖主流程:鉴权、对话、知识库、健康检查。
"""
