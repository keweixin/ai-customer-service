# RAG 模块指南 · 知识库检索增强生成

> 本文档描述 AI 客服系统的 RAG(Retrieval-Augmented Generation)模块:知识库管理、切块策略、向量化、检索与重排序,以及如何提升召回率。
> 代码位于 `src/app/services/rag_service.py`、`src/app/services/embedding_service.py`,数据表 `knowledge_docs` / `knowledge_chunks`(由初始迁移 `0001_initial` 创建)。

## 目录

- [1. 知识库管理流程](#1-知识库管理流程)
- [2. 切块策略](#2-切块策略)
- [3. 向量化](#3-向量化)
- [4. 检索 + 重排序](#4-检索--重排序)
- [5. 如何提升召回率](#5-如何提升召回率)

---

## 1. 知识库管理流程

### 1.1 数据模型

| 表 | 关键字段 | 说明 |
|----|---------|------|
| `knowledge_docs` | id, title, source_type(file/url/text), content, chunks_count, metadata, created_at | 文档元信息 + 原文 |
| `knowledge_chunks` | id, doc_id(FK), chunk_index, content, embedding(VECTOR), metadata, created_at | 切块 + 向量 |

关系:`knowledge_docs 1:N knowledge_chunks`(`doc_id` 外键),`ondelete=CASCADE` 删文档连带删切块与向量,事务一致无孤儿。

### 1.2 文档生命周期与状态机

```text
上传(POST /knowledge/documents)
   │
   ▼
┌──────────┐  切块+向量化成功   ┌──────────┐
│processing│ ─────────────────> │  ready   │  可被检索
└────┬─────┘                    └──────────┘
     │ 切块/向量化失败
     ▼
┌──────────┐
 │  failed  │  记录错误,可重试
└──────────┘

任意状态:DELETE -> 物理删除(连带 chunks)
```

> 注:`status` 为应用层逻辑状态(计划在 `knowledge_docs.metadata` 中维护,或后续迁移加列)。当前表 `knowledge_docs` 暂未含 `status` 列,仅存 `chunks_count`;检索时通过 `embedding IS NOT NULL` 过滤尚未向量化的块。

- `processing`:上传后异步切块+向量化中,检索会跳过该文档的块(embedding 为空)。
- `ready`:可被检索(全部切块已向量化)。
- `failed`:向量化失败,记录错误(写 `metadata.error`),可重试。

### 1.3 上传流程(写入路径)

```text
管理员 POST /knowledge/documents (file 或 content + title)
   │
   ▼
RagService.upload_document()
   1. 写 knowledge_docs (chunks_count=0)
   2. 切块:按策略把 content 切成 chunks[]
   3. 批量调 EmbeddingService.embed(chunks) -> vectors[]
   4. 批量 INSERT knowledge_chunks (content + embedding)
   5. UPDATE knowledge_docs SET chunks_count = :n
   6. 返回 {doc_id, chunks_count}
```

**事务与一致性**

- 切块 + 向量化可能耗时(大文档几十秒),走**异步任务**(后台线程或队列),接口立即返回 `status=processing`。
- 切块与向量写入用一个事务,失败整体回滚,文档置 `failed`。
- 删除走单事务,文档与全部 chunks + 向量一起删。

### 1.4 支持的源类型

| source_type | 处理 |
|-------------|------|
| `text` | 纯文本,按字符切块 |
| `url` | 抓取 URL 内容后按 text 处理 |
| (计划)`markdown` | 按 `#`/`##` 标题分节,节内再按字符切块,保留标题上下文 |
| (计划)`pdf`/`docx` | 抽取文本后按 text 处理 |

---

## 2. 切块策略

切块质量直接决定检索上限--切得不好,再强的检索也召回不到完整语义。

### 2.1 核心参数(来自 `.env`)

| 变量 | 默认 | 说明 |
|------|------|------|
| `RAG_CHUNK_SIZE` | 500 | 每块目标字符数 |
| `RAG_CHUNK_OVERLAP` | 50 | 相邻块重叠字符数(必须 < `RAG_CHUNK_SIZE`)|

校验在 `RAGConfig._overlap_less_than_size`,违反直接报错,防死循环。

### 2.2 切块算法(递归字符切分)

采用"递归分隔符"策略,优先按语义边界切,边界找不到再按字符硬切:

```text
分隔符优先级(从高到低):
  1. 段落 "\n\n"      (最优先,保段落完整)
  2. 换行 "\n"
  3. 句号 "。"/"."    (中文/英文句末)
  4. 分号 ";"/";"
  5. 逗号 ","/","
  6. 空格 " "
  7. 字符(兜底硬切)
```

**算法**:

1. 用最高优先级分隔符把文本分成段。
2. 累积段直到长度 >= `RAG_CHUNK_SIZE`,封一块。
3. 若单段超 `RAG_CHUNK_SIZE`,降级用下一级分隔符递归切该段。
4. 块之间保留 `RAG_CHUNK_OVERLAP` 字符重叠(取上一块尾部),防语义在边界被截断。

### 2.3 Markdown 特化

对 `markdown` 源类型,先按 `#`/`##` 标题切块,每块**前置标题路径**作为上下文:

```text
原文:
## 退货政策
### 7 天无理由
商品签收 7 天内可无理由退货...
### 例外商品
生鲜、定制商品不支持退货...

切块结果:
chunk_0: "[退货政策 > 7 天无理由] 商品签收 7 天内可无理由退货..."
chunk_1: "[退货政策 > 例外商品] 生鲜、定制商品不支持退货..."
```

标题路径让向量与检索结果都带上下文,避免"例外商品不支持退货"被脱离上下文误用。

### 2.4 切块质量原则

- **语义完整**:优先在自然边界切,避免半句话被切两半。
- **大小适中**:太小(如 50 字)语义不足,太大(如 2000 字)稀释相似度且费 token;客服知识库 300-800 字为宜。
- **重叠防漏**:overlap 让边界信息在相邻块都出现,检索任一块都能命中。
- **保留元信息**:标题、来源、章节号附在块上,便于溯源与重排序。

---

## 3. 向量化

### 3.1 模型与维度

| 配置 | 说明 |
|------|------|
| `ARK_EMBEDDING_MODEL` | 方舟 embedding 接入点 ID(如 `ep-embedding-xxx`) |
| `EMBEDDING_DIMENSION` | 向量维度,默认 1024,**必须与模型实际输出一致** |

向量列类型 `VECTOR(1024)`,维度错了插入报错,故配置要与模型对齐。

### 3.2 EmbeddingService 接口

```python
class EmbeddingService:
    async def embed(self, text: str) -> list[float]:
        """单条文本 -> 向量(检索 query 用)。"""

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量文本 -> 向量列表(上传文档切块用,省调用次数)。"""
```

- 走方舟 `/embeddings`(OpenAI 兼容)。
- 批量接口减少 HTTP 往返,大文档切块一次性 embed。
- 输入文本做**截断**:超模型最大输入(如 512 token)截断,防报错。
- **归一化**:方舟返回的向量通常已归一化,余弦相似度等价点积;若未归一化则显式归一化后再算。

### 3.3 写入与索引

- 切块向量批量 `INSERT` 到 `knowledge_chunks.embedding`。
- 索引:`HNSW`(近似最近邻,pgvector 推荐),建索引语句见 `migrations/versions/0001_initial.py`:

```sql
CREATE INDEX ix_knowledge_chunks_embedding_hnsw
ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
```

- `m` 与 `ef_construction`:pgvector 推荐默认 `m=16`、`ef_construction=64`,平衡召回率与建索引速度;`ef_construction` 越大召回越准但建索引越慢。
- 索引建好后 `ANALYZE knowledge_chunks;` 更新统计信息,优化查询计划。
- 查询时可临时调大 `hnsw.ef_search`(如 `SET hnsw.ef_search = 100;`)提升召回,默认 40。

---

## 4. 检索 + 重排序

### 4.1 在线检索流程(Pipeline 阶段5)

```text
query_text
   │
   ▼
EmbeddingService.embed(query) -> query_vector  (~100ms)
   │
   ▼
pgvector 检索(余弦相似度,top_k * 2 候选)  (~50ms)
   SELECT id, content, 1 - (embedding <=> :q) AS score
   FROM knowledge_chunks
   WHERE doc_id = ANY(:doc_ids)         -- 可选限定文档
   ORDER BY embedding <=> :q
   LIMIT :top_k * 2;
   │
   ▼
过滤 score < min_similarity              (~0ms)
   │
   ▼
重排序(rerank,可选) -> top_k 最终块    (~100-300ms)
   │
   ▼
写 ctx.retrieved_chunks / ctx.context_sources
   │
   ▼
SSE event: sources 透传给前端
```

### 4.2 相似度计算

pgvector 操作符 `<=>` 返回**余弦距离**(`1 - 余弦相似度`),所以:

- `ORDER BY embedding <=> query_vector`:按距离升序,最近的在前。
- `score = 1 - (embedding <=> query_vector)`:转回相似度,范围 0~1,越高越相关。
- `min_similarity` 默认 0.7,低于阈值的丢弃,防噪声。

### 4.3 重排序(Rerank)

向量检索召回快但精度有限(可能召回字面相似但语义不符的块),重排序提升精度:

| 重排方式 | 实现 | 延迟 | 适用 |
|---------|------|------|------|
| 不重排 | 直接取 top_k | 0 | 简单场景 / 低延迟要求 |
| Cross-encoder | 用 bge-reranker 等模型对 (query, chunk) 打分重排 | 100-300ms | 精度优先 |
| LLM rerank | 让 LLM 对候选块排序 | 500ms+ | 最高精度但最贵,少用 |

**流程**:

1. 向量检索召回 `top_k * 2` 候选(如 top_k=5 召回 10 个)。
2. 重排模型对每个 (query, candidate) 打分。
3. 按重排分排序,取前 `top_k`。

### 4.4 检索降级

- **Embedding 服务故障**:抛 `LLM_001`,Pipeline 阶段5 标记 RAG 失败但**不阻塞对话**,生成阶段无知识上下文继续,记告警。
- **无命中**:返回空 chunks,策略注入阶段用兜底话术(如"该问题暂无知识库依据,建议转人工")。
- **超时**:embedding 与检索各有超时,超时走降级而非阻塞。

---

## 5. 如何提升召回率

召回率 = 命中的相关块 / 全部相关块。提升召回率从**切块、检索、查询**三方面入手。

### 5.1 切块层

| 优化 | 做法 | 收益 |
|------|------|------|
| 语义切块 | 优先按段落/句号切,而非纯字符 | 块内语义完整,embedding 质量高 |
| 重叠窗口 | `RAG_CHUNK_OVERLAP` 设 50-100 | 边界信息不丢失 |
| 标题上下文 | markdown 块前置标题路径 | 脱离上下文也能理解 |
| 块大小调优 | 300-800 字,太小稀释、太大模糊 | 平衡召回与精度 |
| 元数据切块 | 按文档类型/章节加 tag,检索时过滤 | 减少噪声,提升精度 |

### 5.2 检索层

| 优化 | 做法 | 收益 |
|------|------|------|
| 多召 top_k | 召回 `top_k * 2` 再 rerank | 不漏相关块 |
| 调低阈值 | `RAG_MIN_SIMILARITY` 从 0.7 降到 0.6 | 召回更多,配合 rerank 提精度 |
| 混合检索 | 向量检索 + BM25 关键词检索,RRF 融合 | 向量补语义、关键词补精确匹配(如订单号) |
| 过滤优化 | 按文档分类/时间过滤,减少候选 | 提精度降成本 |
| 索引调优 | `HNSW` `ef_search` 调对、定期 ANALYZE | 查询快且计划优 |
| HNSW 索引 | 数据量大时换 HNSW | 召回率与速度都更好 |

### 5.3 查询层

| 优化 | 做法 | 收益 |
|------|------|------|
| Query 改写 | 用 LLM 把口语化 query 改写为检索友好的表述 | "咋退货" -> "退货流程条件" |
| 多查询融合 | 生成多个改写 query 分别检索,结果去重合并 | 覆盖不同表述 |
| HyDE | 让 LLM 先生成假设答案,用假设答案做检索 | 答案与答案比答案与问题更相似 |
| 意图拼接 | `intent=order` 时 query 拼接"订单查询政策" | 缩小检索范围 |
| 历史补全 | 多轮对话时用上一轮补全当前 query(如"那退货呢") | 解决指代缺失 |

### 5.4 评测与迭代

建立评测集(典型问题 + 应命中的块),量化指标:

| 指标 | 定义 | 目标 |
|------|------|------|
| Recall@k | 前 k 个结果包含相关块的比例 | > 0.9 |
| Precision@k | 前 k 个结果中相关块占比 | > 0.7 |
| MRR | 第一个相关块的倒数排名 | > 0.6 |

迭代流程:改切块/检索策略 -> 跑评测集 -> 看指标变化 -> 上线灰度。

### 5.5 兜底策略

当 RAG 召回不足时,逐级兜底:

1. 无命中 -> 降级用 LLM 自身知识回答 + 提示"未找到知识库依据"。
2. 低置信 -> 建议转人工(触发 `transfer` 意图)。
3. 高频无命中问题 -> 收集分析,补知识库文档(闭环优化)。

> 记录每次检索的 `query` 与 `retrieved_chunks`(空也记),定期分析无命中 query,反向补全知识库,是提升整体效果最有效的方式。
