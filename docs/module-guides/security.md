# 安全模块指南 · JWT / 密码 / 限流 / 审计 / 密钥

> 本文档描述 AI 客服系统的安全机制:JWT 鉴权流程、密码存储、限流策略、审计日志、密钥管理。
> 代码位于 `src/app/core/security.py`、`src/app/core/middleware.py`、`src/app/services/auth_service.py`、`src/app/services/audit_service.py`。

## 目录

- [1. JWT 鉴权流程](#1-jwt-鉴权流程)
- [2. 密码存储](#2-密码存储)
- [3. 限流策略](#3-限流策略)
- [4. 审计日志](#4-审计日志)
- [5. 密钥管理](#5-密钥管理)
- [6. 其他安全措施](#6-其他安全措施)

---

## 1. JWT 鉴权流程

### 1.1 选型理由

采用 **JWT(JSON Web Token)** 无状态鉴权,而非服务端 Session:

- **无状态**:Token 自包含用户信息,服务端无需查 session 存储,水平扩展零共享状态。
- **跨域友好**:Bearer Token 适合前后端分离 + 多子域,CORS 配置简单。
- **标准化**:RFC 7519,生态成熟,PyJWT 库稳定。
- **权衡**:Token 难主动失效,通过短有效期 + 刷新机制 + 黑名单(审计日志)缓解。

### 1.2 Token 结构

JWT 三段:Header.Payload.Signature。Payload 含以下 claim:

| claim | 含义 | 示例 |
|-------|------|------|
| `sub` | 用户唯一标识(user_id),OpenID Connect 约定 | `8f3c1a2e-...` |
| `role` | 用户角色,用于权限判断 | `user` / `admin` |
| `exp` | 过期时间(UTC 时间戳) | `1752825600` |
| `iat` | 签发时间(可选) | `1752739200` |

> `sub` 是必填,缺失视为非法 token(`AUTH_005`)。不放敏感信息(如密码哈希),JWT Payload 仅 Base64 编码非加密。

### 1.3 签发流程

`POST /api/v1/auth/login` 成功后,`AuthService.login()` 调用 `security.create_access_token()`:

```python
# src/app/core/security.py
def create_access_token(data: dict, *, expires_delta=None) -> str:
    settings = get_settings()
    to_encode = dict(data)                         # 复制入参防改原字典
    if "sub" not in to_encode:
        raise AuthenticationError("创建 token 缺少 sub", error_code="AUTH_002")
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.jwt.access_token_expire_minutes)
    )
    to_encode["exp"] = expire
    return jwt.encode(
        to_encode,
        settings.jwt.secret_key.get_secret_value(),
        algorithm=settings.jwt.algorithm,           # 默认 HS256
    )
```

- 算法 `HS256`(HMAC-SHA256),对称签名,签发与校验同一密钥。
- 有效期默认 24 小时(`JWT_ACCESS_TOKEN_EXPIRE_MINUTES=1440`),生产建议缩短 + 刷新机制。
- 密钥从 `JWT_SECRET_KEY` 读取,绝不硬编码。

### 1.4 校验流程

每个受保护接口经 `get_current_user` 依赖项校验:

```text
请求带 Authorization: Bearer <token>
   │
   ▼
提取 token(去 "Bearer " 前缀)
   │
   ▼
jwt.decode(token, secret, algorithms=[HS256])
   │
   ├─ ExpiredSignatureError -> AUTH_003(登录已过期)
   ├─ InvalidTokenError     -> AUTH_004(凭据无效,不暴露具体原因防枚举)
   └─ 成功 -> payload
   │
   ▼
payload 必须含 sub -> 否则 AUTH_005
   │
   ▼
查库确认用户存在且 is_active=True
   │
   ├─ 不存在/停用 -> AUTH_012(账号已停用)
   └─ 通过 -> 注入 current_user 到路由
```

**安全要点**:

- 过期单独提示(`AUTH_003`),前端据此引导重新登录。
- 其他无效原因统一为 `AUTH_004`,不区分"用户不存在/签名错/格式错",防枚举攻击。
- `algorithms` 显式传 `[HS256]`,防 `alg: none` 攻击。
- 校验后仍查库确认 `is_active`,支持软禁用(不改密钥即停用账号)。

### 1.5 权限控制

- **角色判断**:`require_admin` 依赖项检查 `current_user.role == admin`,否则 `403 AUTHZ_001`。
- **资源归属**:会话/消息接口检查 `session.user_id == current_user.id`,防越权访问他人会话。
- **审计**:敏感操作(登录、转人工、知识库变更)记审计日志(见 §4)。

### 1.6 Token 失效

JWT 无服务端状态,失效方式:

| 方式 | 适用 | 代价 |
|------|------|------|
| 自然过期 | 常规 | 等有效期到 |
| 更换 `JWT_SECRET_KEY` | 紧急(密钥泄露) | 全用户被踢,需重新登录 |
| 黑名单(审计日志) | 单 token 失效 | 需查库,牺牲部分无状态性 |
| 短有效期 + 刷新 | 生产推荐 | 需实现 refresh token |

生产推荐:access token 短(如 15 分钟)+ refresh token 长(如 7 天),refresh 可吊销。

---

## 2. 密码存储

### 2.1 哈希算法

用 **passlib + bcrypt** 哈希存储密码,绝不存明文:

```python
# src/app/core/security.py
from passlib.context import CryptContext
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(plain: str) -> str:
    return _pwd_context.hash(plain)

def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _pwd_context.verify(plain, hashed)
    except (ValueError, TypeError):
        return False
```

### 2.2 为什么 bcrypt

| 算法 | 是否选 | 原因 |
|------|--------|------|
| **bcrypt** | 选 | 自带 salt、可调 cost factor(默认 12 轮)、抗 GPU/ASIC、业界标准 |
| MD5/SHA1 | 否 | 太快,易暴力破解,无 salt |
| SHA256+salt | 否 | 太快,需手动管 salt |
| argon2 | 备选 | 更现代抗 GPU,但依赖复杂,bcrypt 够用 |
| scrypt | 备选 | 也不错,生态弱于 bcrypt |

bcrypt 的 cost factor 每提高 1 翻倍,可随硬件升级调整,抗未来算力增长。

### 2.3 存储与校验

- `users.password_hash` 存 bcrypt 哈希(固定 60 字符,列长 128 容纳算法升级)。
- 注册:`hash_password(plain)` -> 存库。
- 登录:`verify_password(plain, hashed)` -> 比对,不抛异常统一返回 `False` 简化调用。
- `verify_password` 用 passlib 内部**常量时间比较**,防时序侧信道攻击(通过响应耗时推断密码是否正确)。

### 2.4 密码策略

- 长度 >= 8 字符(注册校验)。
- 鼓励大小写+数字+符号(前端提示,不强校验,平衡体验)。
- **不存明文、不记日志、不返回响应**:密码字段只在请求体出现一次,落库前即哈希。
- 密码错误不区分"用户名错"还是"密码错"(`AUTH_011`),防枚举。

---

## 3. 限流策略

### 3.1 实现

用 **slowapi**(基于 limits 库)做限流,在 `middleware.py` 集中配置:

```python
def create_limiter() -> Limiter:
    settings = get_settings()
    return Limiter(
        key_func=get_remote_address,                      # 按客户端 IP
        default_limits=[settings.rate_limit.limiter_limit],  # 如 "60/minute"
    )
```

- `key_func=get_remote_address`:按真实客户端 IP 限流,已正确处理 `X-Forwarded-For`(经反向代理时取真实 IP)。
- 默认全局限流 `60/minute`(`RATE_LIMIT_PER_MINUTE` 可配)。
- 超限返回 `429 RL_001`,响应体 `{"error": {"code": "RL_001", "message": "请求过于频繁,请稍后再试"}}`。

### 3.2 限流维度

| 维度 | 实现 | 适用 |
|------|------|------|
| 全局 IP | `default_limits` | 默认,防刷 |
| 接口级 | `@limiter.limit("10/minute")` 装饰器 | 敏感接口加严(如登录) |
| 用户级 | 自定义 key_func 取 user_id | 已登录用户按账号限流(防单账号刷) |

**建议加严的接口**:

- `POST /auth/login`:防暴力破解,如 `10/minute/IP` + 失败 5 次锁账号。
- `POST /auth/register`:防批量注册,如 `5/minute/IP`。
- `POST /chat`:防 LLM 成本攻击,如 `30/minute/user`。

### 3.3 分布式限流

slowapi 默认内存计数,单进程有效。多 worker/多实例部署需换共享存储:

- **Redis**:slowapi 支持 `storage_uri="redis://..."`,跨进程共享计数。
- 配置:`Limiter(storage_uri=os.getenv("REDIS_URL"))`。

生产多实例必须上 Redis,否则每实例各自 60/分钟,实际是 60*N/分钟,限流失效。

### 3.4 反向代理层补充

应用层限流之外,反向代理(Nginx/ALB/CDN)层也加限流,两层防御:

- Nginx:`limit_req_zone $binary_remote_addr zone=aics:10m rate=30r/m;`
- CDN/WAF:防 CC、防 DDoS,在边缘拦截大流量。

---

## 4. 审计日志

### 4.1 审计范围

记录敏感操作,用于合规、追溯、风控。`action` 字段对齐初始迁移 `0001_initial` 中 `audit_action` 枚举:

| 动作 | action(枚举值) | 触发接口 | 关键字段 |
|------|---------------|---------|---------|
| 登录成功 | `login` | POST /auth/login | user_id, ip_address |
| 登出 | `logout` | (计划)POST /auth/logout | user_id |
| 上传知识库 | `upload_doc` | POST /knowledge/documents | target=doc_id, detail.title |
| 转人工 | `transfer_human` | POST /sessions/{id}/close | target=session_id, detail.reason |
| 工具调用 | `call_tool` | Pipeline Function Calling | target=tool_name, detail.arguments |

> 登录失败、内容拦截等可后续通过迁移扩展 `audit_action` 枚举补充;在补枚举值前,此类事件先走 structlog 日志(`event=login_failed` / `content_blocked`)。

### 4.2 存储

- 审计日志表 `audit_logs`(已由 `0001_initial` 创建):字段 `id, user_id(FK, ondelete=SET NULL), action(枚举), target, detail(JSONB), ip_address, created_at`。
- `user_id` 为可空外键,删用户时置 NULL 保留审计记录(符合合规"审计不可丢"要求)。
- 也可投递到日志系统(ELK/Loki)做查询,数据库存关键操作便于关联业务。
- **只追加不删改**:审计日志不可篡改;定期归档冷存。
- 索引:`user_id`、`action`、`created_at`,支持按用户/动作/时间范围高效查询。

### 4.3 查询

`GET /api/v1/admin/audit-logs`(admin 角色),支持按 user_id/action/时间范围过滤,分页返回(见 `api.md` §7.2)。

### 4.4 日志与审计的区别

| 项 | structlog 日志 | 审计日志 |
|----|---------------|---------|
| 目的 | 运维排查 | 合规追溯 |
| 内容 | 全部请求/错误 | 敏感操作 |
| 存储 | stdout -> ELK | 数据库表 + 可选 ELK |
| 保留 | 7-30 天 | 1 年+(合规要求) |
| 访问 | 运维 | 管理员经接口 |

两者互补:日志管"出了什么问题",审计管"谁在何时做了什么敏感操作"。

---

## 5. 密钥管理

### 5.1 密钥清单

| 密钥 | 来源 | 用途 | 泄露后果 |
|------|------|------|---------|
| `JWT_SECRET_KEY` | 环境变量 | JWT 签名 | 可伪造任意用户 token |
| `ARK_API_KEY` | 环境变量 | 调方舟 LLM | 被盗用产生费用 |
| `POSTGRES_PASSWORD` | 环境变量 | 数据库连接 | 数据库被拖库 |

### 5.2 铁律

1. **绝不入代码**:密钥只走环境变量,代码中用 `SecretStr` 包装,`get_secret_value()` 显式取。
2. **绝不入 Git**:`.env` 在 `.gitignore`,绝不提交;`.env.example` 只放占位符。
3. **绝不记日志**:`SecretStr` 的 repr 不显示明文;日志中密钥字段脱敏。
4. **绝不返回响应**:API 响应不含密钥;错误详情(detail)生产关闭防泄露。
5. **生产必须改默认值**:`Settings.validate_production()` 启动硬校验,默认值直接拒绝启动。

```python
# src/app/config.py
def validate_production(self) -> None:
    if not self.app.is_production:
        return
    errors = []
    if self.jwt.secret_key.get_secret_value() in {"", "change_me_to_a_random_64_char_string"}:
        errors.append("生产环境 JWT_SECRET_KEY 必须为随机长串")
    if self.llm.api_key.get_secret_value() in {"", "your_ark_api_key_here"}:
        errors.append("生产环境 ARK_API_KEY 必须填写真实值")
    if self.database.password.get_secret_value() in {"", "change_me_in_production"}:
        errors.append("生产环境 POSTGRES_PASSWORD 必须修改默认值")
    if errors:
        raise ValueError("生产环境配置校验失败:\n" + "\n".join(f"  - {e}" for e in errors))
```

### 5.3 生产密钥存储方案

| 方案 | 适用 | 说明 |
|------|------|------|
| 环境变量(最低要求) | 小规模 | 容器/进程 env 注入,`.env` 不入 Git |
| 阿里云 KMS / 凭据管家 | 阿里云 | 托管密钥,应用启动拉取,自动轮转 |
| AWS Secrets Manager / Parameter Store | AWS | 同上 |
| K8s Sealed Secrets | K8s | 加密存 Git,解密在集群 |
| K8s External Secrets Operator | K8s | 对接云 KMS,密钥不入集群配置 |

### 5.4 密钥轮转

- **定期轮转**:`JWT_SECRET_KEY` 建议 3-6 个月一换;轮转后旧 token 失效,用户需重新登录。
- **紧急轮转**:密钥泄露立即换,并审计泄露期间的操作。
- **无停机轮转**:短暂支持新旧两密钥(校验时两个都试),平滑过渡后再去旧密钥。
- **数据库密码轮转**:PG `ALTER USER` 改密码,同步更新应用配置,滚动重启。

### 5.5 生成强密钥

```bash
# JWT_SECRET_KEY:64 字符随机 URL 安全字符串
python -c "import secrets; print(secrets.token_urlsafe(48))"

# 或用 openssl
openssl rand -base64 48
```

---

## 6. 其他安全措施

### 6.1 SQL 注入防护

- 全部用 **SQLAlchemy ORM 参数绑定**,不拼 SQL 字符串。
- 即使原生 SQL(如 pgvector 检索)也用 `text()` + `:param` 绑定参数。
- 用户输入绝不直接进 SQL 字符串。

### 6.2 CORS

- 白名单制,`CORS_ORIGINS` 只列真实前端域名。
- **禁止 `*`**,尤其 `allow_credentials=True` 时 `*` 会被浏览器拒绝。
- 生产收紧为具体域名(如 `https://cs.example.com`)。

### 6.3 HTTPS

- 生产全站 HTTPS,Token/密码不裸传。
- 反向代理层终止 TLS,应用层走 HTTP(内网)。
- HSTS 头强制 HTTPS。

### 6.4 输入校验

- Pydantic schema 校验请求体(类型、长度、格式)。
- 文件上传限制大小与类型(`VAL_010`)。
- 用户消息长度上限防 prompt 注入与成本攻击。

### 6.5 输出脱敏

- 错误响应生产不返 `detail`,防泄露内部状态。
- 用户电话/身份证在画像中存脱敏值(如 `138****1234`)。
- LLM 输出可过内容审核(可选),防生成敏感内容。

### 6.6 依赖安全

- 定期 `pip audit` / `safety check` 扫依赖漏洞。
- 锁版本(`pyproject.toml` 用 `>=` 但配合 lock 文件)。
- CI 集成依赖扫描。

### 6.7 请求 ID 与链路追踪

- `RequestIdMiddleware` 为每请求生成 `X-Request-ID`,透传到日志与响应头。
- 便于安全事件追溯:一个 request_id 串起该请求的全部日志与审计。

### 6.8 错误处理不泄露

- 全局异常处理器(`register_exception_handlers`)把未捕获异常统一转 `500 INT_001`,不返回堆栈。
- 堆栈只进日志,不进响应。
- 生产 `include_detail=False`,422 等也不返回具体错误细节给客户端。
