# 架构文档 · AI 客服系统

> 本文档描述 AI 客服系统的整体架构、C4 视图、7 阶段 Pipeline 数据流、对话时序、数据模型 ER、RAG 流程、技术选型与关键设计决策。

## 目录

- [1. 系统概览](#1-系统概览)
- [2. C4 Context 图](#2-c4-context-图)
- [3. C4 Container 图](#3-c4-container-图)
- [4. 7 阶段 Pipeline 数据流图](#4-7-阶段-pipeline-数据流图)
- [5. 对话请求时序图](#5-对话请求时序图)
- [6. 数据模型 ER 图](#6-数据模型-er-图)
- [7. RAG 流程图](#7-rag-流程图)
- [8. 技术选型理由表](#8-技术选型理由表)
- [9. 关键设计决策](#9-关键设计决策)

---

## 1. 系统概览

AI 客服系统是一个基于 **FastAPI + 火山方舟(DeepSeek)+ PostgreSQL(pgvector)** 的企业级 AI 客服后端。它对外提供 HTTP/SSE 接口,对内通过 **7 阶段对话 Pipeline** 串联输入解析、内容安全、意图识别、实体追踪、RAG 检索、策略注入、流式生成,实现"可观测、可测试、可部署"的客服大脑。知识库通过文档上传→切块→向量化→pgvector 语义检索→重排序的 RAG 流程增强回答;长期记忆以用户画像(JSONB)+ 对话摘要 + 滑动窗口历史的形式跨会话持久化;鉴权基于 JWT,辅以限流、审计日志、结构化日志与 Prometheus 指标,达到生产可用标准。

---

## 2. C4 Context 图

> 视角:系统边界 + 外部参与者/系统。本系统位于中心,与终端用户、管理员、方舟 LLM、PostgreSQL 交互。

```text
┌──────────────────────────────────────────────────────────────────────────┐
│                              外部环境                                      │
│                                                                          │
│   ┌────────────┐        ┌────────────┐                                   │
│   │  终端用户   │        │   管理员    │                                   │
│   │ (Web/App)  │        │ (运维/运营) │                                   │
│   └─────┬──────┘        └──────┬─────┘                                   │
│         │   HTTPS/SSE          │  HTTPS                                  │
│         │   (聊天/会话)          │  (知识库/统计/审计)                      │
│         │                      │                                         │
│   ╔═════▼══════════════════════▼═════════════════════════════════╗       │
│   ║                                                               ║       │
│   ║            AI 客服系统 (ai-customer-service)                  ║       │
│   ║   ─────────────────────────────────────────────────────       ║       │
│   ║   · FastAPI HTTP/SSE 网关                                      ║       │
│   ║   · 7 阶段对话 Pipeline                                        ║       │
│   ║   · RAG 知识库检索                                             ║       │
│   ║   · 长期记忆(画像/摘要)                                        ║       │
│   ║   · JWT 鉴权 / 限流 / 审计                                     ║       │
│   ║                                                               ║       │
│   ╚   ┬────────────────────────────────────────┬──────────────┬──╝       │
│       │ HTTPS (OpenAI 兼容协议)                  │ TCP (asyncpg) │          │
│       │ LLM 对话 + Embedding 向量化               │ SQL + 向量查询│          │
│       │                                        │              │          │
│   ┌───▼──────────────┐              ┌──────────▼──────────┐   │          │
│   │  火山方舟 (Ark)    │              │   PostgreSQL 15      │   │          │
│   │  · deepseek-v4    │              │   + pgvector 扩展    │   │          │
│   │  · embedding 模型  │              │   (用户/会话/消息/   │   │          │
│   └───────────────────┘              │    画像/知识文档/块) │   │          │
│                                      └─────────────────────┘   │          │
└──────────────────────────────────────────────────────────────────┼────────┘
                                                                   │
                                                            (可选)监控
                                                            Prometheus
                                                            抓取 /metrics
```

**外部系统说明**

| 参与者/系统 | 交互方式 | 职责 |
|------------|---------|------|
| 终端用户 | HTTP REST + SSE | 发起对话、查询历史、关闭会话 |
| 管理员 | HTTP REST(需 admin 角色) | 上传知识库文档、查看统计/审计/用户 |
| 火山方舟 Ark | HTTPS(OpenAI 兼容) | 提供 LLM 对话与 Embedding 向量化 |
| PostgreSQL+pgvector | TCP(asyncpg) | 持久化业务数据 + 向量相似度检索 |
| Prometheus(可选) | HTTP GET /metrics | 抓取应用指标做监控告警 |

---

## 3. C4 Container 图

> 视角:系统内部的可独立部署/运行的容器(进程)及其交互。橙色为应用进程,蓝色为数据存储,绿色为外部 SaaS。

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                        ai-customer-service (系统边界)                     │
│                                                                         │
│  ┌───────────────────────────────────────────────────────────────┐      │
│  │  FastAPI App (Python 3.11, Uvicorn/Gunicorn)                   │      │
│  │  ─────────────────────────────────────────────────────────    │      │
│  │  中间件链 (洋葱模型,后加先执行):                                │      │
│  │    RequestId -> Logging -> CORS -> SlowAPI 限流 -> 路由          │      │
│  │  ┌──────────┐ ┌──────────┐ ┌────────────┐ ┌──────────────┐     │      │
│  │  │ /auth    │ │ /chat    │ │ /knowledge │ │ /admin       │     │      │
│  │  │ 路由组   │ │ 路由组   │ │  路由组    │ │  路由组      │     │      │
│  │  └────┬─────┘ └────┬─────┘ └─────┬──────┘ └──────┬───────┘     │      │
│  │       │            │              │               │             │      │
│  │       ▼            ▼              ▼               ▼             │      │
│  │  ┌──────────────────────────────────────────────────────────┐  │      │
│  │  │              业务服务层 (services/)                       │  │      │
│  │  │   AuthService · RagService · MemoryService · LLMService  │  │      │
│  │  │   EmbeddingService · AuditService                        │  │      │
│  │  └────────────────────────┬─────────────────────────────────┘  │      │
│  │                           │                                    │      │
│  │  ┌────────────────────────▼─────────────────────────────────┐  │      │
│  │  │              7 阶段 Pipeline (pipeline/)                 │  │      │
│  │  │  InputParser -> ContentGuard -> IntentClassifier ->      │  │      │
│  │  │  EntityTracker -> RagRetriever -> StrategyInjector ->    │  │      │
│  │  │  StreamGenerator                                          │  │      │
│  │  └────┬──────────────┬──────────────────┬───────────────────┘  │      │
│  │       │              │                  │                       │      │
│  │  ┌────▼────┐  ┌──────▼──────┐  ┌────────▼────────┐             │      │
│  │  │ tools/  │  │ repositories│  │   models/ ORM    │             │      │
│  │  │ FC 工具 │  │  数据访问层 │  │ SQLAlchemy 2.0   │             │      │
│  │  └─────────┘  └─────────────┘  └──────────────────┘             │      │
│  └───────────────────────────┬───────────────────────────────────┘      │
│                              │ asyncpg                                   │
└──────────────────────────────┼─────────────────────────────────────────┘
                               │
              ┌────────────────▼────────────────┐
              │   PostgreSQL 15 + pgvector      │
              │   ┌──────┐ ┌────────┐ ┌───────┐ │
              │   │users │ │sessions│ │messages│ │
              │   └──────┘ └────────┘ └───────┘ │
              │   ┌────────────┐ ┌───────────┐  │
              │   │user_profiles│ │documents │  │
              │   └────────────┘ └───────────┘  │
              │   ┌─────────┐                    │
              │   │ chunks  │ (含 vector 列)     │
              │   └─────────┘                    │
              └──────────────────────────────────┘

外部依赖:
   ┌─────────────────────┐         ┌──────────────────────┐
   │   火山方舟 Ark API   │         │  Prometheus (可选)    │
   │   · /chat/completions│         │  抓取 GET /metrics    │
   │   · /embeddings      │         │                      │
   └─────────────────────┘         └──────────────────────┘
```

**容器职责**

| 容器 | 技术 | 职责 |
|------|------|------|
| FastAPI App | Python 3.11 + FastAPI + Uvicorn | HTTP/SSE 网关、中间件、路由、依赖注入 |
| 7 阶段 Pipeline | 自研编排器 | 串联 7 个阶段处理对话请求 |
| repositories | SQLAlchemy 2.0 async | 数据访问层,隔离 ORM 与业务 |
| models | SQLAlchemy ORM | 4+2 张表的声明式映射 |
| PostgreSQL+pgvector | pgvector/pgvector:pg15 | 业务数据持久化 + 向量检索 |
| 火山方舟 Ark | 外部 SaaS | LLM 对话与 Embedding |
| tools (FC) | 自研 | Function Calling 工具(查订单/推卡片/转人工)|

---

## 4. 7 阶段 Pipeline 数据流图

> 每个阶段读取 `PipelineContext` 中的字段、产出新字段写回 context,下游阶段消费。`PipelineContext` 是贯穿全流程的载体。

```text
                      用户消息 + session_id + user_id
                                  │
                                  ▼
                ┌─────────────────────────────────────┐
   阶段1         │         InputParser (输入解析)       │
                │  读: raw_text, session_id           │
                │  写: normalized_text, lang,         │
                │      message_type, metadata         │
                └─────────────────┬───────────────────┘
                                  ▼
                ┌─────────────────────────────────────┐
   阶段2         │       ContentGuard (内容安全)        │
                │  读: normalized_text                │
                │  写: is_safe, safety_reason         │
                │  ★ 不安全 -> 短路返回拒答            │
                └─────────────────┬───────────────────┘
                                  ▼ (is_safe=True)
                ┌─────────────────────────────────────┐
   阶段3         │    IntentClassifier (意图识别)      │
                │  读: normalized_text, history       │
                │  写: intent (chat/faq/order/        │
                │       complaint/transfer/...),      │
                │      confidence                     │
                └─────────────────┬───────────────────┘
                                  ▼
                ┌─────────────────────────────────────┐
   阶段4         │     EntityTracker (实体追踪)        │
                │  读: normalized_text, profile       │
                │  写: entities{order_no, name, ...}, │
                │      updated_profile                │
                └─────────────────┬───────────────────┘
                                  ▼
                ┌─────────────────────────────────────┐
   阶段5         │      RagRetriever (RAG 检索)        │
                │  读: normalized_text, intent        │
                │  写: retrieved_chunks[],            │
                │      context_sources[]              │
                │  (intent=闲聊且无知识需求时跳过)     │
                └─────────────────┬───────────────────┘
                                  ▼
                ┌─────────────────────────────────────┐
   阶段6         │    StrategyInjector (策略注入)      │
                │  读: intent, entities, chunks,      │
                │      profile, summary               │
                │  写: system_prompt, tool_schema,    │
                │      tool_choice                     │
                └─────────────────┬───────────────────┘
                                  ▼
                ┌─────────────────────────────────────┐
   阶段7         │     StreamGenerator (流式生成)      │
                │  读: system_prompt, history,        │
                │      normalized_text, tool_schema   │
                │  写: response_tokens[], tool_calls, │
                │      tokens_used, finish_reason     │
                └─────────────────┬───────────────────┘
                                  │
                                  ▼
                        SSE 流式响应 + 持久化
                  (写 messages 表, 更新 user_profiles)
```

**PipelineContext 关键字段总览**

| 字段 | 写入阶段 | 消费阶段 | 说明 |
|------|---------|---------|------|
| `raw_text` | 入口 | 1 | 原始用户输入 |
| `normalized_text` | 1 | 2,3,4,5,6,7 | 去噪/规整后的文本 |
| `lang` | 1 | 6 | 语言,影响 system_prompt |
| `is_safe` | 2 | 短路判断 | False 则直接拒答 |
| `intent` | 3 | 5,6 | 意图分类 |
| `entities` | 4 | 6,7 | 抽取的实体 |
| `retrieved_chunks` | 5 | 6 | 命中的知识片段 |
| `system_prompt` | 6 | 7 | 注入给 LLM 的系统提示 |
| `tool_schema` | 6 | 7 | 本轮允许的工具定义 |
| `response_tokens` | 7 | 出口 | 流式 token 序列 |

---

## 5. 对话请求时序图

> 描述一次 `POST /api/v1/chat` 请求从进入到 SSE 响应返回的完整链路。

```text
 客户端        FastAPI        Pipeline        LLM/Ark       PostgreSQL
   │             │               │               │               │
   │ POST /chat  │               │               │               │
   │ Bearer JWT  │               │               │               │
   ├────────────>│               │               │               │
   │             │ 鉴权(JWT 解码) │               │               │               │
   │             ├──────────────────────────────────────────────>│ 查 session
   │             │<──────────────────────────────────────────────┤ + 历史
   │             │ 构造 Context  │               │               │
   │             ├──────────────>│               │               │
   │             │               │ 阶段1 InputParser             │
   │             │               │ 阶段2 ContentGuard            │
   │             │               │ 阶段3 IntentClassifier        │
   │             │               ├───────────────>│ (意图可走小模型/规则)
   │             │               │<───────────────┤ intent
   │             │               │ 阶段4 EntityTracker           │
   │             │               ├───────────────────────────────>│ 读写画像
   │             │               │<──────────────────────────────┤
   │             │               │ 阶段5 RagRetriever            │
   │             │               ├───────────────>│ embedding     │
   │             │               │<───────────────┤ vector        │
   │             │               ├───────────────────────────────>│ pgvector 检索
   │             │               │<──────────────────────────────┤ chunks
   │             │               │ 阶段6 StrategyInjector        │
   │             │               │ 阶段7 StreamGenerator         │
   │             │               ├───────────────>│ chat stream   │
   │             │               │<═══════════════┤ token 流      │
   │             │ SSE event:token (逐 token 下发)                │
   │ <═══════════┤<══════════════│               │               │
   │ event:done  │               │ 写 message(user+assistant)    │
   │ <───────────┤               ├───────────────────────────────>│ INSERT
   │             │               │               │               │ 更新 summary
   │             │               │<──────────────────────────────┤ done
```

**关键说明**

- `event:token` 逐个/批量下发生成内容,客户端实时拼接。
- `event:done` 携带 `tokens_used` / `message_id` / `tool_calls`,客户端据此结束流并持久化。
- `event:error` 在任意阶段失败时下发,并附带 `error_code`。
- 用户消息与助手消息在生成结束后**一次性落库**,避免半截消息;若中途断流,助手消息标记为 `interrupted`。

---

## 6. 数据模型 ER 图

> 7 张表:`users` / `user_profiles` / `sessions` / `messages` / `knowledge_docs` / `knowledge_chunks` / `audit_logs`。全部由初始迁移 `0001_initial` 创建;`audit_logs` 记录敏感操作(登录/转人工/上传文档/工具调用)。

```text
┌──────────────────────┐         1:1         ┌────────────────────────┐
│       users          │─────────────────────>│    user_profiles       │
│──────────────────────│                      │────────────────────────│
│ id          UUID PK  │                      │ id          UUID PK    │
│ username    VARCHAR   │                      │ user_id   UUID FK,UNIQ │
│ email       VARCHAR   │                      │ profile_data  JSONB   │
│ password_hash VARCHAR│                      │ summary       TEXT    │
│ role        ENUM      │                      │ created_at  TIMESTAMPTZ│
│ is_active   BOOL      │                      │ updated_at  TIMESTAMPTZ│
│ created_at  TIMESTAMPTZ│                     └────────────────────────┘
│ updated_at  TIMESTAMPTZ│
└──────────┬───────────┘
           │ 1:N  (ondelete=CASCADE)
           ▼
┌──────────────────────┐         1:N         ┌────────────────────────┐
│      sessions        │─────────────────────>│       messages         │
│──────────────────────│                      │────────────────────────│
│ id          UUID PK  │                      │ id          UUID PK    │
│ user_id   UUID FK    │                      │ session_id  UUID FK    │
│ status      ENUM      │                      │ role        ENUM      │
│ started_at  TIMESTAMPTZ│                     │ content     TEXT      │
│ ended_at    TIMESTAMPTZ│                     │ tokens_used INT       │
│ created_at  TIMESTAMPTZ│                     │ metadata    JSONB     │
│ updated_at  TIMESTAMPTZ│                     │ created_at  TIMESTAMPTZ│
└──────────────────────┘                      └────────────────────────┘
                                              索引: (session_id, created_at)

┌────────────────────────────┐         1:N         ┌────────────────────────────┐
│      knowledge_docs        │─────────────────────>│     knowledge_chunks       │
│────────────────────────────│                      │────────────────────────────│
│ id          UUID PK        │                      │ id          UUID PK        │
│ title       VARCHAR(512)   │                      │ doc_id      UUID FK        │
│ source_type ENUM(file/url/ │                      │ chunk_index INT            │
│             text)          │                      │ content     TEXT           │
│ content     TEXT           │                      │ embedding  VECTOR(1024)    │
│ chunks_count INT           │                      │ metadata    JSONB          │
│ metadata    JSONB          │                      │ created_at  TIMESTAMPTZ    │
└────────────────────────────┘                      └────────────────────────────┘
                                                    索引: (doc_id, chunk_index)
                                                            HNSW(embedding, cosine)

┌────────────────────────────┐
│       audit_logs           │   (只追加,ondelete=SET NULL 保留历史)
│────────────────────────────│
│ id          UUID PK        │
│ user_id   UUID FK (nullable)│
│ action      ENUM(login/     │
│   logout/upload_doc/        │
│   transfer_human/call_tool) │
│ target      VARCHAR(512)   │
│ detail      JSONB          │
│ ip_address  VARCHAR(45)    │
│ created_at  TIMESTAMPTZ    │
└────────────────────────────┘
   索引: user_id, action, created_at
```

**关系说明**

| 关系 | 基数 | 级联 | 说明 |
|------|------|------|------|
| users → user_profiles | 1:1 | CASCADE | 删用户连带删画像 |
| users → sessions | 1:N | CASCADE | 删用户连带删会话 |
| sessions → messages | 1:N | CASCADE | 删会话连带删消息 |
| knowledge_docs → knowledge_chunks | 1:N | CASCADE | 删文档连带删切块与向量 |
| users → audit_logs | 1:N | SET NULL | 删用户保留审计记录(user_id 置空)|

**主键与时区约定**

- 所有主键用 UUID(`gen_random_uuid()`),避免自增整数在分库/迁移冲突。
- 所有时间字段 `TIMESTAMPTZ`(带时区),跨时区部署不串。
- `updated_at` 由数据库触发器维护,绕过 ORM 的裸 SQL 也能正确刷新。

---

## 7. RAG 流程图

### 7.1 知识上传流程(离线写入)

```text
  管理员            FastAPI         RagService       Embedding       PostgreSQL
    │  POST /knowledge  │              │                │                │
    │  (text/md, title) │              │                │                │
    ├──────────────────>│              │                │                │
    │                   │  upload()    │                │                │
    │                   ├─────────────>│                │                │
    │                   │              │ 切块(chunk_size│                │
    │                   │              │ + overlap)     │                │
    │                   │              ├───────────────>│ 批量 embedding │
    │                   │              │<───────────────┤ vectors[]      │
    │                   │              │ INSERT document│                │
    │                   │              ├───────────────────────────────>│
    │                   │              │ INSERT chunks (含 vector)      │
    │                   │              ├───────────────────────────────>│
    │                   │              │<──────────────────────────────┤ doc_id
    │                   │<─────────────┤ {doc_id, chunks_count}         │
    │  201 {document}   │              │                │                │
    │<──────────────────┤              │                │                │
```

### 7.2 知识检索流程(在线查询,Pipeline 阶段5)

```text
  RagRetriever        Embedding         PostgreSQL(pgvector)        Reranker(可选)
      │ query_text        │                    │                        │
      ├──────────────────>│ embed(query)       │                        │
      │<──────────────────┤ query_vector       │                        │
      ├────────────────────────────────────────>│ ORDER BY embedding     │
      │                   │                    │ <=> query_vector       │
      │                   │                    │ LIMIT top_k * 2        │
      │<────────────────────────────────────────┤ candidate_chunks[]    │
      │ 过滤 similarity < min_similarity        │                        │
      ├─────────────────────────────────────────────────────────────────>│ 重排序
      │<─────────────────────────────────────────────────────────────────┤ top_k chunks
      │ 写 context_sources[] 到 PipelineContext │                        │
```

**召回率优化要点**(详见 `module-guides/rag.md`)

- 切块:按语义边界(段落/句号)+ 固定字符数混合,带 overlap 防边界截断。
- 检索:`top_k * 2` 召回 + 阈值过滤 + 重排序,平衡召回与精度。
- 向量索引:`HNSW` 近似最近邻,百万级块下查询 < 50ms。

---

## 8. 技术选型理由表

| 层 | 选型 | 为什么选它 | 备选与放弃原因 |
|----|------|-----------|---------------|
| Web 框架 | **FastAPI** | 原生 async、Pydantic 类型校验、自动 OpenAPI 文档、SSE 友好 | Flask 同步模型在 LLM 长连接下吞吐差;Django 太重 |
| ASGI 服务器 | **Uvicorn + Gunicorn** | Uvicorn 单进程 async;Gunicorn 管理多 worker 进程,生产稳定 | 纯 Uvicorn 多进程需自管,缺乏平滑重启 |
| LLM | **火山方舟 DeepSeek** | OpenAI 兼容协议、国内访问稳定、成本可控、支持 Function Calling | 直连 OpenAI 国内不可达;自建模型运维成本高 |
| 数据库 | **PostgreSQL 15** | 成熟 RDBMS、JSONB、枚举、触发器、扩展生态 | MySQL 缺原生向量类型;SQLite 不适合并发 |
| 向量检索 | **pgvector** | 与 PG 同库,事务一致,免独立向量库运维 | Milvus/Qdrant 需额外集群,小规模过度设计 |
| ORM | **SQLAlchemy 2.0 async** | 类型友好、asyncpg 高性能、生态成熟 | Tortoise ORM 生态弱;原生 SQL 维护成本高 |
| 迁移 | **Alembic** | SQLAlchemy 官方配套,autogenerate 可靠 | 手写 SQL 迁移易错且难回滚 |
| 鉴权 | **PyJWT + passlib(bcrypt)** | JWT 无状态易水平扩展;bcrypt 抗暴力破解 | Session 需共享存储,水平扩展麻烦 |
| 限流 | **slowapi** | 轻量、装饰器式、基于 IP,够用 | 自研令牌桶重复造轮子 |
| 日志 | **structlog** | 结构化 JSON,contextvars 注入 request_id,ELK 友好 | 标准 logging 无结构化,查询难 |
| 监控 | **prometheus-client** | 业界标配,/metrics 端点 + Grafana 即用 | 自研指标重复造轮子 |
| 测试 | **pytest + pytest-asyncio + aiosqlite** | async 测试零样板;aiosqlite 替代 PG 做单测 | unittest 写法啰嗦;用真 PG 单测慢 |
| 部署 | **Docker + docker-compose** | 环境一致、一键起;pgvector 官方镜像开箱即用 | 裸机部署环境漂移严重 |

---

## 9. 关键设计决策

### 9.1 为什么用 7 阶段 Pipeline 而不是单体函数

- **可测试**:每个阶段是纯函数 `(context) -> context`,单测无需起服务。
- **可替换**:换 LLM 模型只改 `StreamGenerator`;换意图识别只改 `IntentClassifier`。
- **可观测**:每阶段产出写入 context,可在日志/指标中独立追踪耗时与产出。
- **可短路**:内容安全不通过直接跳过后续 5 阶段,省 LLM 调用成本。
- **可编排**:新需求(如多轮澄清)作为新阶段插入,不污染现有逻辑。

### 9.2 为什么用 pgvector 而不是独立向量库

- **事务一致**:文档删除时,切块与向量在同一事务内清理,无孤儿向量。
- **运维减负**:少一个 Milvus/Qdrant 集群,小到中型规模(百万级块)性能足够。
- **查询联合**:可 `JOIN` 业务表过滤(如只检索某分类文档),向量库难以做到。
- **权衡**:亿级向量、毫秒级检索需求出现时,再迁移到专用向量库,接口层屏蔽即可。

### 9.3 为什么用 SSE 而不是 WebSocket

- **单向够用**:客服回复是服务端→客户端的单向流,SSE 语义更贴合。
- **HTTP 友好**:SSE 走标准 HTTP,自动复用鉴权中间件、限流、CORS,无需额外握手。
- **断线重连**:浏览器原生支持 `Last-Event-ID` 续传,实现简单。
- **权衡**:需要客户端实时上行(如语音打断)时再升级 WebSocket。

### 9.4 为什么用 JWT 而不是 Session

- **无状态**:水平扩展无需共享 session 存储,降低依赖。
- **跨域**:Bearer Token 适合前后端分离 + 多子域。
- **权衡**:Token 难主动失效,通过短有效期 + 刷新机制 + 黑名单(审计日志)缓解。

### 9.5 为什么记忆用"画像 + 摘要 + 滑动窗口"三层

- **画像(JSONB)**:结构化偏好/实体,检索快,命中即用,避免每轮重抽。
- **摘要**:长历史压缩成几百字,大幅降低 prompt token 成本。
- **滑动窗口**:最近 N 轮原文保留上下文连贯性,兼顾质量与成本。
- 三层组合在 token 预算内最大化信息密度。

### 9.6 为什么主键用 UUID 而不是自增整数

- **分布式友好**:客户端可预生成 ID,异步写入无需等 RETURNING 回填。
- **迁移无冲突**:分库分表/合并时不冲突。
- **安全**:不暴露业务量(自增 ID 可被爬虫推断用户数)。
- **权衡**:索引体积略大,PG 用 `gen_random_uuid()` 原生支持,可接受。
