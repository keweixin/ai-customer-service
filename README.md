# AI Customer Service · 企业级 AI 客服系统

> 基于 FastAPI + 火山方舟(DeepSeek)+ PostgreSQL 的企业级 AI 客服后端。
> 7 阶段 Pipeline 架构,RAG 知识库,长期记忆,Function Calling,JWT 鉴权,可观测,可测试,可部署。

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-green.svg)](https://fastapi.tiangolo.com)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 🎯 项目定位

**不是 demo,不是玩具**--这是一个按企业级标准构建的 AI 客服后端,具备:

- **7 阶段对话 Pipeline**:输入解析 → 内容安全 → 意图识别 → 实体追踪 → RAG 检索 → 策略注入 → 流式生成
- **RAG 知识库**:文档上传 → 切块 → 向量化 → pgvector 语义检索 → 重排序
- **长期记忆**:用户画像 + 会话历史持久化到 PostgreSQL,跨会话记忆
- **Function Calling**:查订单 / 推卡片 / 转人工,工单流转
- **流式输出**:SSE 流式回复,首字延迟低
- **企业级特性**:JWT 鉴权 / 限流 / 结构化日志 / Prometheus 指标 / 全局异常处理
- **工程化**:类型注解 / pytest 测试 / Alembic 迁移 / Docker 部署 / CI 就绪

## 🏗️ 架构

```
┌─────────────────────────────────────────────────────────┐
│                      客户端(HTTP/SSE)                    │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│  FastAPI(中间件:CORS → 限流 → 请求ID → JWT鉴权)         │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐    │
│  │ /auth    │ │ /chat    │ │/knowledge│ │ /admin   │    │
│  └──────────┘ └────┬─────┘ └──────────┘ └──────────┘    │
└─────────────────────┼───────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────┐
│              7 阶段 Pipeline(对话大脑)                   │
│  InputParser → ContentGuard → IntentClassifier →        │
│  EntityTracker → RagRetriever → StrategyInjector →      │
│  StreamGenerator                                         │
└──┬──────────────┬──────────────┬──────────────┬─────────┘
   │              │              │              │
   ▼              ▼              ▼              ▼
┌──────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
│ LLM  │   │ Embedding│   │PostgreSQL│   │  Tools   │
│Service│   │  Service │   │ +pgvector│   │(FC工单等)│
└──────┘   └──────────┘   └──────────┘   └──────────┘
```

详细架构见 [docs/architecture.md](docs/architecture.md)。

## 🚀 快速开始

### 前置条件

- Python 3.11+
- Docker & Docker Compose
- 火山方舟 API Key([申请](https://console.volcengine.com/ark))

### 三步启动

```bash
# 1. 克隆 + 配置
git clone <repo-url>
cd ai-customer-service
cp .env.example .env
# 编辑 .env,填入 ARK_API_KEY 和 JWT_SECRET_KEY

# 2. 启动数据库 + 安装依赖
docker-compose up -d postgres
pip install -e ".[dev]"
alembic upgrade head

# 3. 启动服务
python -m uvicorn src.app.main:app --reload
```

访问 http://localhost:8000/docs 查看 API 文档。

## 📚 文档

- [架构文档](docs/architecture.md) - C4 图、数据流、Pipeline 时序
- [API 文档](docs/api.md) - 接口说明 + 示例
- [部署文档](docs/deployment.md) - Docker 部署、环境变量、迁移、回滚
- [模块指南](docs/module-guides/) - 每个模块的详细说明

## 🧪 测试

```bash
pytest                    # 全部测试
pytest tests/unit         # 单元测试
pytest tests/integration  # 集成测试
pytest --cov              # 覆盖率报告
```

## 🛠️ 技术栈

| 层 | 技术 |
|----|------|
| Web 框架 | FastAPI + Uvicorn |
| LLM | 火山方舟 deepseek-v4-flash(OpenAI 兼容协议)|
| 数据库 | PostgreSQL 15 + pgvector |
| ORM | SQLAlchemy 2.0(async)+ Alembic |
| 鉴权 | PyJWT + passlib(bcrypt)|
| 限流 | slowapi |
| 日志 | structlog(结构化 JSON)|
| 监控 | prometheus-client |
| 测试 | pytest + pytest-asyncio + aiosqlite |
| 部署 | Docker + docker-compose |

## 📂 项目结构

```
ai-customer-service/
├── src/app/
│   ├── main.py              # FastAPI 入口
│   ├── config.py            # Pydantic Settings 配置
│   ├── core/                # 基础设施(异常/日志/安全/指标)
│   ├── api/v1/              # API 路由(auth/chat/knowledge/admin)
│   ├── models/              # ORM 模型
│   ├── schemas/             # Pydantic DTO
│   ├── repositories/        # 数据访问层
│   ├── services/            # 业务服务(LLM/RAG/记忆/Embedding)
│   ├── pipeline/            # 7 阶段对话 Pipeline
│   └── tools/               # Function Calling 工具
├── tests/                   # 单元测试 + 集成测试
├── migrations/              # Alembic 数据库迁移
├── docs/                    # 架构/API/部署文档
├── scripts/                 # 初始化/上传脚本
├── docker-compose.yml
├── Dockerfile
└── pyproject.toml
```

## 🔒 安全

- API Key / JWT Secret 只走环境变量,不入代码不入 Git
- `.env` 在 `.gitignore` 中,绝不提交
- 密码用 bcrypt 哈希存储
- SQL 全用 ORM 参数绑定,防注入
- CORS 白名单,不开放 `*`
- 限流防刷

## 📄 License

MIT
