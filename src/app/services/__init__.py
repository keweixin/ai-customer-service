"""服务层:封装外部依赖(LLM、RAG、记忆等)的调用细节。

业务/Pipeline 层只面向服务接口编程,不直接触碰 httpx/SQL,便于:
1. 统一重试、超时、错误转换等横切关注点;
2. 单测时用 fake 替换真实服务,避免发真实网络请求。

模块清单:
- llm: LLMService -- 火山方舟对话 + Embedding 调用(httpx + tenacity)
- embedding_service: EmbeddingService -- 对 LLMService.embed 的薄封装,
  叠加 LRU 缓存与批量合并(可选使用,LLMService.embed 已够用时直接用 llm)
- rag_service: RagService -- RAG 知识库管理 + pgvector 语义检索
- memory_service: MemoryService -- 长期记忆(用户画像 + 对话历史 + 摘要压缩)

依赖注入契约(见 app.api.deps):
- LLMService(settings.llm) -- 进程级单例,内部持有 httpx 连接池
- RagService(db=db, llm=llm) -- 请求级,无状态
- MemoryService(db=db, llm=llm) -- 请求级,无状态

关于 EmbeddingService:
LLMService 已提供 embed/embed_batch,满足 RAG 向量化需求。EmbeddingService
作为可选增强(缓存 + 批量合并),在需要重复文本向量化优化时使用;默认
RagService 直接调 llm.embed_batch,未引入 EmbeddingService 以保持简单。
如需启用缓存,在 RagService.__init__ 中把 self.llm 换成
EmbeddingService(llm) 即可,接口兼容。
"""

from __future__ import annotations

from app.services.embedding_service import EmbeddingService
from app.services.llm import LLMService
from app.services.memory_service import MemoryService
from app.services.rag_service import RagService

__all__ = [
    "LLMService",
    "EmbeddingService",
    "RagService",
    "MemoryService",
]
