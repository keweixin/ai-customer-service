# 部署文档 · AI 客服系统

> 本文档覆盖本地开发、Docker 部署、环境变量、数据库迁移、生产注意事项、回滚预案与常见问题排查。

## 目录

- [1. 本地开发部署](#1-本地开发部署)
- [2. Docker 部署(app + postgres)](#2-docker-部署app--postgres)
- [3. 环境变量清单](#3-环境变量清单)
- [4. 数据库迁移](#4-数据库迁移)
- [5. 生产部署注意事项](#5-生产部署注意事项)
- [6. 回滚预案](#6-回滚预案)
- [7. 常见问题排查表](#7-常见问题排查表)

---

## 1. 本地开发部署

本地开发只起 PostgreSQL 容器,应用在宿主机用 `uvicorn --reload` 跑,便于热重载与断点调试。

### 1.1 前置条件

- Python 3.11+
- Docker & Docker Compose(用于起数据库)
- 火山方舟 API Key([申请](https://console.volcengine.com/ark))

### 1.2 步骤

```bash
# 1) 克隆 + 配置
git clone <repo-url>
cd ai-customer-service
cp .env.example .env
# 编辑 .env,至少填入:
#   ARK_API_KEY=<方舟密钥>
#   JWT_SECRET_KEY=<随机 64 字符串>
#   ARK_EMBEDDING_MODEL=<embedding 接入点 ID>

# 2) 启动 PostgreSQL + pgvector
docker compose up -d postgres
# 等待就绪(healthcheck 通过)
docker compose ps   # 看到 aics-postgres 为 healthy

# 3) 安装依赖(含开发工具)
pip install -e ".[dev]"

# 4) 数据库迁移
alembic upgrade head

# 5) 初始化默认管理员(可选)
python scripts/init_db.py

# 6) 启动应用(热重载)
python -m uvicorn src.app.main:app --reload --host 0.0.0.0 --port 8000
```

### 1.3 验证

```bash
# 存活探针
curl http://localhost:8000/health
# 交互式 API 文档
打开 http://localhost:8000/docs
```

### 1.4 常用开发命令

```bash
# 测试
pytest                          # 全部
pytest tests/unit               # 单元测试
pytest tests/integration        # 集成测试(需起 PG)
pytest --cov                    # 覆盖率

# 代码质量
ruff check src tests            # 静态检查
ruff format src tests           # 格式化
mypy src                        # 类型检查

# 数据库
alembic revision --autogenerate -m "add xxx"   # 生成迁移
alembic upgrade head                            # 应用迁移
alembic downgrade -1                            # 回滚一步

# 重置数据库(开发环境)
docker compose down -v          # 删数据卷
docker compose up -d postgres
alembic upgrade head
python scripts/init_db.py
```

---

## 2. Docker 部署(app + postgres)

生产推荐用 Docker 一键起应用 + 数据库。项目根目录已提供 `Dockerfile` 与 `docker-compose.yml`,此处给出一个**同时包含 app 服务**的扩展 compose 文件示例。

### 2.1 创建 `docker-compose.prod.yml`(示例)

```yaml
# 生产部署:app + postgres 一起起。
# 启动:docker compose -f docker-compose.prod.yml up -d
services:
  postgres:
    image: pgvector/pgvector:pg15
    container_name: aics-postgres
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-aics}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB:-ai_customer_service}
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-aics}"]
      interval: 5s
      timeout: 5s
      retries: 10
    restart: unless-stopped

  app:
    build: .
    container_name: aics-app
    environment:
      # 应用读取 .env;容器内通过 env_file 注入
      APP_ENV: production
      APP_DEBUG: "false"
      DATABASE_URL: postgresql+asyncpg://${POSTGRES_USER:-aics}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB:-ai_customer_service}
      ARK_API_KEY: ${ARK_API_KEY}
      JWT_SECRET_KEY: ${JWT_SECRET_KEY}
      CORS_ORIGINS: ${CORS_ORIGINS}
    env_file:
      - .env
    ports:
      - "8000:8000"
    depends_on:
      postgres:
        condition: service_healthy
    # Dockerfile 内 CMD 已包含 alembic upgrade head && uvicorn
    restart: unless-stopped

volumes:
  pgdata:
```

### 2.2 启动

```bash
# 确保 .env 已填好(见 §3)
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml logs -f app
```

### 2.3 验证

```bash
curl http://localhost:8000/health   # 200
curl http://localhost:8000/ready    # 200 且 database=ok
docker compose -f docker-compose.prod.yml ps
```

### 2.4 停止与清理

```bash
docker compose -f docker-compose.prod.yml down            # 停服务,保留数据
docker compose -f docker-compose.prod.yml down -v         # 停服务并删数据卷(慎用)
```

---

## 3. 环境变量清单

> 来源于 `.env.example`,按分组列出。敏感项标注 `[敏感]`,生产必须修改。

### 3.1 应用配置(APP_*)

| 变量 | 默认值 | 说明 | 生产注意 |
|------|--------|------|---------|
| `APP_NAME` | AI Customer Service | 应用名称 | - |
| `APP_ENV` | development | 运行环境:`development`/`staging`/`production` | 必须 `production` |
| `APP_DEBUG` | true | 调试模式 | 必须 `false` |
| `APP_HOST` | 0.0.0.0 | 监听地址 | - |
| `APP_PORT` | 8000 | 监听端口 | - |
| `APP_LOG_LEVEL` | INFO | 日志级别 | 生产建议 INFO 或 WARNING |

### 3.2 火山方舟 LLM 配置(ARK_* / LLM_*)

| 变量 | 默认值 | 说明 | 生产注意 |
|------|--------|------|---------|
| `ARK_API_KEY` | - | 方舟 API Key | `[敏感]` 必须填真实值 |
| `ARK_MODEL` | deepseek-v4-flash | 对话模型 | - |
| `ARK_BASE_URL` | https://ark.cn-beijing.volces.com/api/coding/v3 | 方舟 API 基址 | - |
| `LLM_MAX_TOKENS` | 2048 | 单次生成最大 token | - |
| `LLM_TEMPERATURE` | 0.7 | 采样温度 | 客服场景建议 0.3-0.5 |
| `LLM_TIMEOUT` | 60 | 调用超时(秒) | - |
| `LLM_MAX_RETRIES` | 3 | 失败重试次数 | - |

### 3.3 Embedding 配置(用于 RAG)

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ARK_EMBEDDING_MODEL` | - | embedding 接入点 ID |
| `EMBEDDING_DIMENSION` | 1024 | 向量维度(须与模型一致)|

### 3.4 PostgreSQL 配置(POSTGRES_* / DATABASE_URL)

| 变量 | 默认值 | 说明 | 生产注意 |
|------|--------|------|---------|
| `POSTGRES_HOST` | localhost | 数据库主机 | 容器内为 `postgres` |
| `POSTGRES_PORT` | 5432 | 端口 | - |
| `POSTGRES_USER` | aics | 用户名 | - |
| `POSTGRES_PASSWORD` | change_me_in_production | 密码 | `[敏感]` 必须改 |
| `POSTGRES_DB` | ai_customer_service | 库名 | - |
| `DATABASE_URL` | (自动拼接) | 完整连接串,优先级最高 | 容器内建议显式设置 |

### 3.5 JWT 鉴权(JWT_*)

| 变量 | 默认值 | 说明 | 生产注意 |
|------|--------|------|---------|
| `JWT_SECRET_KEY` | change_me_to_a_random_64_char_string | 签名密钥 | `[敏感]` 必须改随机长串 |
| `JWT_ALGORITHM` | HS256 | 签名算法 | - |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | 1440 | Token 有效期(分钟)| 生产可缩短 + 刷新机制 |

### 3.6 CORS / 限流 / RAG

| 变量 | 默认值 | 说明 | 生产注意 |
|------|--------|------|---------|
| `CORS_ORIGINS` | http://localhost:5173,http://localhost:3000 | 前端白名单(逗号分隔)| 必须收紧为真实域名 |
| `RATE_LIMIT_PER_MINUTE` | 60 | 每分钟限流 | 按业务调整 |
| `RAG_CHUNK_SIZE` | 500 | 切块字符数 | - |
| `RAG_CHUNK_OVERLAP` | 50 | 块重叠字符数 | 必须 < `RAG_CHUNK_SIZE` |
| `RAG_TOP_K` | 5 | 检索返回条数 | - |
| `RAG_MIN_SIMILARITY` | 0.7 | 最低相似度阈值 | - |

### 3.7 生成随机密钥

```bash
# 生成 64 字符随机字符串作为 JWT_SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(48))"
# 或
openssl rand -base64 48
```

---

## 4. 数据库迁移

迁移工具为 **Alembic**,迁移脚本位于 `migrations/versions/`,配置在 `alembic.ini`(由 `Base.metadata` 驱动 autogenerate)。

### 4.1 常用命令

```bash
# 应用所有未执行迁移到最新
alembic upgrade head

# 回滚最近一次迁移
alembic downgrade -1

# 回滚到指定版本
alembic downgrade <revision_id>

# 查看当前版本与历史
alembic current
alembic history

# 根据模型变更自动生成迁移(生成后必须人工 review!)
alembic revision --autogenerate -m "add xxx table"

# 空迁移(手写 SQL 用)
alembic revision -m "manual sql for pgvector index"
```

### 4.2 迁移规范

1. **autogenerate 后必 review**:Alembic 对枚举、JSONB、向量列的识别不完美,需人工补全。
2. **一次迁移只做一件事**:便于回滚与审计,避免"大爆炸"迁移。
3. **提供 upgrade 与 downgrade**:downgrade 必须可执行,否则回滚预案失效。
4. **数据迁移与结构迁移分离**:先建表迁结构,再单独写数据迁移脚本,降低风险。
5. **pgvector 扩展**:首个迁移 `0001_initial.py` 已含 `CREATE EXTENSION IF NOT EXISTS vector`(及 `pgcrypto` 供 `gen_random_uuid()`),`scripts/init_db.py` 也会兜底启用。
6. **触发器**:`0001_initial` 已创建 `refresh_updated_at()` 函数与各表触发器,确保裸 SQL 更新也能刷新时间戳。

### 4.3 pgvector 索引迁移示例

> 项目 `0001_initial.py` 已为 `knowledge_chunks.embedding` 建 HNSW 索引。下方为追加/重建索引的迁移示例。

```python
# migrations/versions/xxxx_rebuild_vector_index.py
def upgrade():
    # 先删旧索引(若存在)
    op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_embedding_hnsw;")
    op.execute(
        "CREATE INDEX ix_knowledge_chunks_embedding_hnsw "
        "ON knowledge_chunks USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64);"
    )
    # 重建后更新统计信息,优化查询计划
    op.execute("ANALYZE knowledge_chunks;")

def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_embedding_hnsw;")
```

> `m` 与 `ef_construction` 为 pgvector HNSW 推荐默认;查询时可用 `SET hnsw.ef_search = 100;` 提升召回(默认 40),值越大召回越准但越慢。

---

## 5. 生产部署注意事项

### 5.1 安全配置(必须)

| 项 | 要求 | 校验位置 |
|----|------|---------|
| `JWT_SECRET_KEY` | 必须为随机 64+ 字符,不得用默认值 | `Settings.validate_production()` 启动时硬校验 |
| `POSTGRES_PASSWORD` | 必须修改默认值 | 同上 |
| `ARK_API_KEY` | 必须填真实值 | 同上 |
| `APP_ENV` | 必须为 `production` | 同上 |
| `APP_DEBUG` | 必须为 `false`,关闭错误详情外泄 | 异常处理器据此决定是否返回 detail |
| `CORS_ORIGINS` | 收紧为真实前端域名,禁止 `*` | CORS 中间件 |

> `Settings.validate_production()` 在应用 startup 事件中调用,任一不满足直接抛错拒绝启动,实现"配置问题早暴露"。

### 5.2 部署平台选择

| 平台 | 适用场景 | 要点 |
|------|---------|------|
| **阿里云 SAE(Serverless App Engine)** | 中小流量、弹性伸缩 | 用容器镜像部署,配健康检查 `/health`+`/ready`,最小实例数 >= 1 防 0 实例冷启动 |
| **阿里云 ECS / AWS EC2** | 稳定流量、成本敏感 | Docker 部署 + 负载均衡,注意安全组只放 8000 给 SLB |
| **Kubernetes** | 大规模、多服务 | Deployment + Service + HPA + Ingress;livenessProbe=/health,readinessProbe=/ready |
| **自建 + Nginx** | 内网/私有化 | Nginx 反代,`proxy_buffering off;` 保证 SSE 实时性 |

### 5.3 进程模型(生产)

Dockerfile 内生产用 **Gunicorn + Uvicorn worker**,而非裸 Uvicorn:

```bash
gunicorn src.app.main:app \
  -k uvicorn.workers.UvicornWorker \
  --workers 4 \
  --bind 0.0.0.0:8000 \
  --timeout 120 \
  --graceful-timeout 30 \
  --access-logfile -
```

- `--workers`:一般设为 `2 * CPU + 1`;LLM 是 IO 密集,可适当上调。
- `--timeout 120`:对话可能耗时较长,默认 30s 不够。
- `--graceful-timeout 30`:优雅停机,等 SSE 流收尾。

### 5.4 SSE 反代注意事项

Nginx / ALB 反代时必须:

```nginx
location /api/v1/chat {
    proxy_pass http://app_upstream;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;          # 关键:关闭缓冲,SSE 才能实时下发
    proxy_read_timeout 300s;      # 长连接超时,覆盖默认 60s
    proxy_cache off;
}
```

### 5.5 日志收集

- 应用通过 **structlog** 输出 JSON 到 stdout(生产),便于日志采集。
- 采集方案:
  - 容器:`docker logs` -> Filebeat/Fluentd -> ELK / Loki
  - K8s:DaemonSet 采集 stdout -> Loki
  - SAE:内置日志服务 SLS,直接接 stdout
- 关键字段:`request_id`(链路追踪)、`user_id`、`session_id`、`level`、`event`、`duration_ms`。
- 告警:对 `level=error` 与 `event=request.error` 配置告警规则。

### 5.6 监控接入

- `/metrics` 暴露 Prometheus 指标,关键指标:
  - `http_requests_total` / `http_request_duration_seconds`
  - `pipeline_stage_duration_seconds`(各阶段耗时)
  - `llm_tokens_total`(token 成本)
  - `llm_request_duration_seconds`(方舟调用耗时)
- 抓取配置(Prometheus):

```yaml
scrape_configs:
  - job_name: 'aics'
    metrics_path: /metrics
    static_configs:
      - targets: ['aics-app:8000']
```

- Grafana 建议面板:QPS、P95 延迟、错误率、各 Pipeline 阶段耗时分布、token 消耗速率、活跃会话数。
- 告警:5xx 错误率 > 1%、P95 > 5s、LLM 调用失败率 > 5%。

### 5.7 数据库运维

- **备份**:每日全量 `pg_dump` + WAL 归档,保留 7-30 天。
- **连接池**:应用侧 SQLAlchemy `pool_size=10`、`max_overflow=20`、`pool_pre_ping=True`;生产可加 PgBouncer。
- **pgvector 索引重建**:数据大量写入后执行 `ANALYZE knowledge_chunks;` 更新统计信息,优化查询计划。
- **慢查询**:开启 `pg_stat_statements`,关注向量检索与历史消息查询。

### 5.8 密钥管理

- **绝不**把密钥写进代码、Dockerfile、compose 文件、Git。
- 推荐方案:
  - 阿里云:KMS / 凭据管家
  - AWS:Secrets Manager / Parameter Store
  - K8s:Sealed Secrets / External Secrets Operator
  - 最低要求:环境变量注入,且 `.env` 不入 Git。

---

## 6. 回滚预案

### 6.1 应用回滚

- **镜像回滚**:保留前 N 个版本镜像,重新 `docker compose up -d` 指定旧 tag。
- **K8s**:`kubectl rollout undo deployment/aics`。
- **SAE**:一键回滚到上一版本。

### 6.2 数据库回滚

```bash
# 回滚最近一次迁移(需确认 downgrade 可执行)
alembic downgrade -1

# 回滚到指定版本
alembic downgrade <target_revision>

# 查看可回滚的版本链
alembic history
```

**注意事项**:

1. **数据丢失风险**:downgrade 删列/删表会丢数据,执行前必须备份。
2. **不可逆操作**:如 `DROP TABLE`、`DROP COLUMN`,downgrade 也无法恢复数据,只能靠备份。
3. **大表迁移**:加列用默认值 + 后台回填;删列先改名标记废弃,观察无影响再真删。
4. **回滚顺序**:先回滚应用镜像,再 downgrade 数据库(应用兼容旧 schema 优先)。

### 6.3 紧急恢复流程

1. 发现故障 -> 立即回滚应用镜像到上一稳定版本。
2. 若 schema 不兼容 -> `alembic downgrade` 到匹配版本。
3. 若数据损坏 -> 从最近备份恢复(`pg_restore`)。
4. 验证 `/ready` 通过 + 抽检核心接口。
5. 复盘 + 补回归测试。

---

## 7. 常见问题排查表

| 现象 | 可能原因 | 排查/解决 |
|------|---------|----------|
| 启动报 `生产环境配置校验失败` | 生产环境密钥未改 | 检查 `JWT_SECRET_KEY`/`POSTGRES_PASSWORD`/`ARK_API_KEY` 是否为默认值 |
| 启动报 `RAG_CHUNK_OVERLAP 必须小于 RAG_CHUNK_SIZE` | overlap >= size | 调小 `RAG_CHUNK_OVERLAP` 或调大 `RAG_CHUNK_SIZE` |
| `asyncpg.exceptions.InterfaceError: too many connections` | 连接池耗尽 | 调大 `pool_size`/`max_overflow`,或加 PgBouncer;检查是否有连接泄漏 |
| `psycopg.OperationalError: connection refused` | PG 未就绪 | `docker compose ps` 看 healthcheck;等 healthy 再启动 app |
| 登录返回 `AUTH_011` | 用户名/密码错 | 不区分用户名错还是密码错(防枚举);检查密码与库中哈希 |
| `/chat` 一直转圈不出 token | SSE 被反代缓冲 | Nginx 加 `proxy_buffering off;`;确认 `Accept: text/event-stream` |
| `/chat` 返回 `LLM_001` | 方舟超时/限流/5xx | 查 `LLM_TIMEOUT`;调小 `LLM_MAX_TOKENS`;加重试;查方舟控制台配额 |
| `/chat` 返回 `RL_001` | 触发限流 | 调高 `RATE_LIMIT_PER_MINUTE`;或前端加节流 |
| 知识检索无结果 | 相似度阈值过高 / 无相关文档 | 调低 `RAG_MIN_SIMILARITY`;用 `POST /knowledge/search` 验证;确认文档 status=ready |
| 知识检索 `RAG_001` | embedding 服务异常 | 检查 `ARK_EMBEDDING_MODEL` 与 `ARK_API_KEY`;查方舟接入点状态 |
| 迁移 `alembic upgrade` 卡住 | 有未关闭事务锁 | `SELECT * FROM pg_stat_activity WHERE state='idle in transaction';` 杀掉长事务 |
| autogenerate 漏检列变更 | Alembic 识别局限 | 人工编辑迁移文件补全;或用 `alembic revision` 手写 |
| `updated_at` 不刷新 | 触发器缺失 | 检查迁移是否创建了 `update_updated_at_column()` 触发器 |
| 容器健康检查失败 | `/health` 不通 | 容器内 `curl -f localhost:8000/health`;确认进程启动、端口对 |
| `401 AUTH_003` 频繁出现 | Token 过期太快 | 调大 `JWT_ACCESS_TOKEN_EXPIRE_MINUTES`;前端实现刷新机制 |
| 内存持续增长 | SSE 连接未释放 | 检查 `finally` 块是否关闭生成器;Gunicorn `--max-requests` 定期重启 worker |
| pgvector 查询慢 | 索引未建 / 数据量增长 | `ANALYZE knowledge_chunks;`;调大 `hnsw.ef_search` 提召回;数据量大可重建 HNSW 索引调 `m`/`ef_construction` |
