# API 文档 · AI 客服系统

> 本文档描述 AI 客服系统对外暴露的全部 HTTP/SSE 接口,包括认证方式、接口清单、每个接口的请求/响应示例、错误码,以及 SSE 流式响应格式。
> Base URL:`http://localhost:8000`,所有业务接口前缀 `/api/v1`。交互式文档见 `/docs`(Swagger)与 `/redoc`。

## 目录

- [1. 认证方式](#1-认证方式)
- [2. 通用约定](#2-通用约定)
- [3. 接口列表](#3-接口列表)
- [4. 认证接口](#4-认证接口)
  - [4.1 POST /api/v1/auth/register](#41-post-apiv1authregister)
  - [4.2 POST /api/v1/auth/login](#42-post-apiv1authlogin)
  - [4.3 GET /api/v1/auth/me](#43-get-apiv1authme)
- [5. 对话接口](#5-对话接口)
  - [5.1 POST /api/v1/chat(SSE 流式,重点)](#51-post-apiv1chatsse-流式重点)
  - [5.2 GET /api/v1/chat/sessions](#52-get-apiv1chatsessions)
  - [5.3 GET /api/v1/chat/sessions/{id}/messages](#53-get-apiv1chatsessionsidmessages)
  - [5.4 POST /api/v1/chat/sessions/{id}/close](#54-post-apiv1chatsessionsidclose)
- [6. 知识库接口](#6-知识库接口)
  - [6.1 POST /api/v1/knowledge/documents](#61-post-apiv1knowledgedocuments)
  - [6.2 GET /api/v1/knowledge/documents](#62-get-apiv1knowledgedocuments)
  - [6.3 DELETE /api/v1/knowledge/documents/{id}](#63-delete-apiv1knowledgedocumentsid)
  - [6.4 POST /api/v1/knowledge/search](#64-post-apiv1knowledgesearch)
- [7. 管理接口](#7-管理接口)
  - [7.1 GET /api/v1/admin/stats](#71-get-apiv1adminstats)
  - [7.2 GET /api/v1/admin/audit-logs](#72-get-apiv1adminaudit-logs)
  - [7.3 GET /api/v1/admin/users](#73-get-apiv1adminusers)
- [8. 运维接口](#8-运维接口)
  - [8.1 GET /health](#81-get-health)
  - [8.2 GET /ready](#82-get-ready)
  - [8.3 GET /metrics](#83-get-metrics)
- [9. 错误码总表](#9-错误码总表)

---

## 1. 认证方式

系统使用 **JWT Bearer Token** 进行无状态鉴权。

- **获取 Token**:`POST /api/v1/auth/login` 用用户名+密码换取 `access_token`。
- **使用 Token**:在请求头携带 `Authorization: Bearer <token>`。
- **Token 载荷**:含 `sub`(user_id)、`role`(user/admin)、`exp`(过期时间)。
- **有效期**:默认 24 小时(可由 `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` 配置)。
- **失效方式**:JWT 无服务端状态,自然过期或更换 `JWT_SECRET_KEY` 全量失效。

**示例**

```http
GET /api/v1/auth/me HTTP/1.1
Host: localhost:8000
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOi...
```

无 Token 或 Token 无效返回 `401 AUTH_001`。

---

## 2. 通用约定

### 2.1 请求头

| 头 | 是否必填 | 说明 |
|----|---------|------|
| `Authorization` | 鉴权接口必填 | `Bearer <token>` |
| `Content-Type` | POST/PUT 必填 | `application/json` 或 `multipart/form-data` |
| `X-Request-ID` | 可选 | 客户端透传链路 ID,不传则服务端生成,响应头原样返回 |

### 2.2 统一响应结构

**成功**(直接返回数据,无包装层)

```json
{
  "id": "...",
  "username": "..."
}
```

**失败**(统一 error 包装)

```json
{
  "error": {
    "code": "AUTH_001",
    "message": "认证失败,请重新登录",
    "detail": {}
  }
}
```

> `detail` 仅在非生产环境返回,生产环境省略以防泄露内部状态。

### 2.3 分页约定

列表接口统一使用 query 参数 `page`(从 1 起)、`page_size`(默认 20,最大 100),返回:

```json
{
  "items": [...],
  "total": 123,
  "page": 1,
  "page_size": 20
}
```

### 2.4 限流

全局默认 `60 次/分钟`(可由 `RATE_LIMIT_PER_MINUTE` 配置),按客户端 IP 统计。超限返回 `429 RL_001`。

---

## 3. 接口列表

| Method | Path | 描述 | 鉴权 |
|--------|------|------|------|
| POST | `/api/v1/auth/register` | 注册新用户 | 无 |
| POST | `/api/v1/auth/login` | 登录获取 JWT | 无 |
| GET | `/api/v1/auth/me` | 获取当前用户信息 | Bearer |
| POST | `/api/v1/chat` | 发送对话(SSE 流式响应) | Bearer |
| GET | `/api/v1/chat/sessions` | 获取当前用户的会话列表 | Bearer |
| GET | `/api/v1/chat/sessions/{id}/messages` | 获取某会话的消息历史 | Bearer |
| POST | `/api/v1/chat/sessions/{id}/close` | 关闭/转人工某会话 | Bearer |
| POST | `/api/v1/knowledge/documents` | 上传知识库文档 | Bearer + admin |
| GET | `/api/v1/knowledge/documents` | 知识库文档列表 | Bearer + admin |
| DELETE | `/api/v1/knowledge/documents/{id}` | 删除知识库文档 | Bearer + admin |
| POST | `/api/v1/knowledge/search` | 知识库语义检索 | Bearer + admin |
| GET | `/api/v1/admin/stats` | 系统统计概览 | Bearer + admin |
| GET | `/api/v1/admin/audit-logs` | 审计日志查询 | Bearer + admin |
| GET | `/api/v1/admin/users` | 用户列表 | Bearer + admin |
| GET | `/health` | 存活探针 | 无 |
| GET | `/ready` | 就绪探针 | 无 |
| GET | `/metrics` | Prometheus 指标 | 无(生产可加 IP 白名单)|

---

## 4. 认证接口

### 4.1 POST /api/v1/auth/register

注册新用户。默认角色 `user`,需管理员另行提权为 `admin`。

**请求体**

```json
{
  "username": "alice",
  "email": "alice@example.com",
  "password": "Str0ng!Pass"
}
```

**校验规则**:`username` 3-64 字符、唯一;`email` 合法邮箱、唯一;`password` >= 8 字符。

**响应 · 201 Created**

```json
{
  "id": "8f3c1a2e-...",
  "username": "alice",
  "email": "alice@example.com",
  "role": "user",
  "is_active": true,
  "created_at": "2026-07-17T10:00:00+00:00"
}
```

**错误码**

| HTTP | code | 说明 |
|------|------|------|
| 422 | VAL_002 | 参数格式错误 |
| 409 | AUTH_010 | 用户名或邮箱已存在 |

---

### 4.2 POST /api/v1/auth/login

用用户名(或邮箱)+ 密码换取 JWT。

**请求体**

```json
{
  "username": "alice",
  "password": "Str0ng!Pass"
}
```

**响应 · 200 OK**

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "expires_in": 86400,
  "user": {
    "id": "8f3c1a2e-...",
    "username": "alice",
    "role": "user"
  }
}
```

**错误码**

| HTTP | code | 说明 |
|------|------|------|
| 401 | AUTH_011 | 用户名或密码错误(不区分以防枚举) |
| 403 | AUTH_012 | 账号已停用(is_active=false) |

---

### 4.3 GET /api/v1/auth/me

获取当前登录用户详情。

**请求**:无 body,需 `Authorization` 头。

**响应 · 200 OK**

```json
{
  "id": "8f3c1a2e-...",
  "username": "alice",
  "email": "alice@example.com",
  "role": "user",
  "is_active": true,
  "created_at": "2026-07-17T10:00:00+00:00"
}
```

**错误码**

| HTTP | code | 说明 |
|------|------|------|
| 401 | AUTH_001 | 未携带/无效 Token |

---

## 5. 对话接口

### 5.1 POST /api/v1/chat(SSE 流式,重点)

发送一条用户消息,服务端通过 **Server-Sent Events** 流式返回 AI 回复。这是系统的核心接口,走完整 7 阶段 Pipeline。

**请求头**

```http
POST /api/v1/chat HTTP/1.1
Authorization: Bearer <token>
Content-Type: application/json
Accept: text/event-stream
```

**请求体**

```json
{
  "session_id": "a1b2c3d4-...",
  "message": "我昨天下的订单 #20260716001 还没到,怎么查?"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `session_id` | string(UUID) | 否 | 已有会话 ID;不传则创建新会话 |
| `message` | string | 是 | 用户消息文本 |
| `stream` | bool | 否 | 默认 true;false 则等全部生成后一次性返回 JSON |

**SSE 响应格式**

响应头:`Content-Type: text/event-stream`、`Cache-Control: no-cache`、`Connection: keep-alive`。
每个事件由两行组成:`event: <类型>` 与 `data: <JSON>`,事件之间空行分隔。

```text
event: session
data: {"session_id": "a1b2c3d4-...", "is_new": true}

event: token
data: {"text": "您"}

event: token
data: {"text": "好"}

event: token
data: {"text": ",正在为您查询订单"}

event: tool_call
data: {"name": "query_order", "arguments": {"order_no": "20260716001"}, "id": "call_001"}

event: tool_result
data: {"id": "call_001", "status": "200", "data": {"order_no": "20260716001", "status": "配送中"}}

event: token
data: {"text": "您的订单 #20260716001 当前状态为「配送中」,预计今日送达。"}

event: sources
data: {"chunks": [{"id": "...", "title": "配送时效政策", "score": 0.91}]}

event: done
data: {"message_id": "msg_9f8e...", "tokens_used": 156, "finish_reason": "stop"}
```

**SSE 事件类型**

| event | data 字段 | 说明 |
|-------|----------|------|
| `session` | `session_id`, `is_new` | 会话信息(首个事件) |
| `token` | `text` | 生成的文本片段(逐个或小批量) |
| `tool_call` | `name`, `arguments`, `id` | Pipeline 决定调用 FC 工具 |
| `tool_result` | `id`, `status`, `data` | 工具执行结果回传 |
| `sources` | `chunks[]` | RAG 命中的知识来源(透明化) |
| `done` | `message_id`, `tokens_used`, `finish_reason` | 流结束,携带元信息 |
| `error` | `code`, `message` | 任意阶段失败,流终止 |

**客户端消费示例(JavaScript)**

```javascript
const resp = await fetch("/api/v1/chat", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
    "Accept": "text/event-stream",
  },
  body: JSON.stringify({ session_id: sid, message: text }),
});
const reader = resp.body.getReader();
const decoder = new TextDecoder();
let buffer = "";
while (true) {
  const { value, done } = await reader.read();
  if (done) break;
  buffer += decoder.decode(value, { stream: true });
  const blocks = buffer.split("\n\n");
  buffer = blocks.pop();
  for (const block of blocks) {
    const [evtLine, dataLine] = block.split("\n");
    const event = evtLine.replace(/^event:\s*/, "");
    const data = JSON.parse(dataLine.replace(/^data:\s*/, ""));
    if (event === "token") appendToUI(data.text);
    else if (event === "done") finalize(data);
    else if (event === "error") showError(data);
  }
}
```

**非流式响应(stream=false)**

```json
{
  "session_id": "a1b2c3d4-...",
  "message_id": "msg_9f8e...",
  "content": "您的订单 #20260716001 当前状态为「配送中」...",
  "tokens_used": 156,
  "tool_calls": [],
  "sources": [...],
  "finish_reason": "stop"
}
```

**错误码**

| HTTP | code | 说明 |
|------|------|------|
| 401 | AUTH_001 | 未认证 |
| 403 | AUTHZ_001 | 会话不属于当前用户 |
| 422 | VAL_002 | message 为空或过长 |
| 429 | RL_001 | 触发限流 |
| 502 | LLM_001 | 方舟上游不可用/超时 |
| 500 | PIPE_001 | Pipeline 编排错误 |
| 500 | RAG_001 | 知识库检索失败(非致命时降级继续)|

> 注:错误既可能以 HTTP 状态码返回(请求阶段),也可能以 `event: error` 在流中下发(生成阶段)。

---

### 5.2 GET /api/v1/chat/sessions

获取当前用户的会话列表,按最近活跃倒序。

**Query 参数**

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `page` | int | 1 | 页码 |
| `page_size` | int | 20 | 每页条数 |
| `status` | string | - | 可选过滤:`active`/`closed`/`transferred` |

**响应 · 200 OK**

```json
{
  "items": [
    {
      "id": "a1b2c3d4-...",
      "status": "active",
      "started_at": "2026-07-17T10:00:00+00:00",
      "ended_at": null,
      "last_message_preview": "我的订单还没到...",
      "message_count": 6,
      "created_at": "2026-07-17T10:00:00+00:00"
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 20
}
```

---

### 5.3 GET /api/v1/chat/sessions/{id}/messages

获取某会话的消息历史,按 `created_at` 正序,适合对话回放。

**路径参数**:`id` = 会话 UUID。

**Query 参数**

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `limit` | int | 50 | 单次最多返回条数(<=200) |
| `before` | string | - | 游标:返回此 message_id 之前的消息(用于向上翻页)|

**响应 · 200 OK**

```json
{
  "items": [
    {
      "id": "msg_001-...",
      "session_id": "a1b2c3d4-...",
      "role": "user",
      "content": "我的订单还没到",
      "tokens_used": 8,
      "metadata": null,
      "created_at": "2026-07-17T10:00:01+00:00"
    },
    {
      "id": "msg_002-...",
      "session_id": "a1b2c3d4-...",
      "role": "assistant",
      "content": "正在为您查询...",
      "tokens_used": 156,
      "metadata": {
        "tool_calls": [{"name": "query_order", "id": "call_001"}],
        "sources": [{"id": "...", "title": "配送时效政策", "score": 0.91}]
      },
      "created_at": "2026-07-17T10:00:03+00:00"
    }
  ],
  "has_more": false,
  "next_cursor": null
}
```

**错误码**

| HTTP | code | 说明 |
|------|------|------|
| 404 | NF_001 | 会话不存在 |
| 403 | AUTHZ_001 | 会话不属于当前用户 |

---

### 5.4 POST /api/v1/chat/sessions/{id}/close

关闭会话或标记转人工。

**路径参数**:`id` = 会话 UUID。

**请求体**

```json
{
  "action": "close"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `action` | string | 是 | `close`(正常关闭)或 `transfer`(转人工) |
| `reason` | string | 否 | 关闭/转人工原因,写入审计 |

**响应 · 200 OK**

```json
{
  "id": "a1b2c3d4-...",
  "status": "closed",
  "ended_at": "2026-07-17T10:30:00+00:00"
}
```

**错误码**

| HTTP | code | 说明 |
|------|------|------|
| 404 | NF_001 | 会话不存在 |
| 409 | SESSION_001 | 会话已关闭,不可重复操作 |

---

## 6. 知识库接口

> 所有知识库接口需要 **admin 角色**。普通用户访问返回 `403 AUTHZ_001`。

### 6.1 POST /api/v1/knowledge/documents

上传一个知识库文档,自动切块 + 向量化 + 入库。支持文件上传、URL 抓取或直接传文本。

**请求头**:`Content-Type: multipart/form-data`(上传文件)或 `application/json`(直接传文本/URL)。

**Form 表单**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `file` | file | 三选一 | 上传的 .txt/.md 文件(source_type=`file`)|
| `url` | string | 三选一 | 待抓取的网页/文档 URL(source_type=`url`)|
| `content` | string | 三选一 | 直接传文本内容(source_type=`text`)|
| `title` | string | 是 | 文档标题 |
| `source_type` | string | 否 | `text`(默认)/`file`/`url`,与 `knowledge_docs.source_type` 枚举对齐 |

**响应 · 201 Created**

```json
{
  "id": "doc_001-...",
  "title": "退货政策",
  "source_type": "text",
  "status": "ready",
  "chunks_count": 12,
  "content_length": 5800,
  "created_at": "2026-07-17T10:00:00+00:00"
}
```

**异步说明**:大文件向量化可能耗时,`status` 可能为 `processing`,可通过 `GET /documents` 轮询状态。

**错误码**

| HTTP | code | 说明 |
|------|------|------|
| 403 | AUTHZ_001 | 非 admin 角色 |
| 413 | VAL_010 | 文件过大(超过配置上限) |
| 500 | RAG_001 | 切块/向量化失败 |

---

### 6.2 GET /api/v1/knowledge/documents

知识库文档列表。

**Query 参数**

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `page` | int | 1 | 页码 |
| `page_size` | int | 20 | 每页条数 |
| `status` | string | - | 过滤:`processing`/`ready`/`failed` |
| `q` | string | - | 标题模糊搜索 |

**响应 · 200 OK**

```json
{
  "items": [
    {
      "id": "doc_001-...",
      "title": "退货政策",
      "source_type": "text",
      "status": "ready",
      "chunks_count": 12,
      "created_at": "2026-07-17T10:00:00+00:00",
      "updated_at": "2026-07-17T10:00:05+00:00"
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 20
}
```

---

### 6.3 DELETE /api/v1/knowledge/documents/{id}

删除文档,连带删除其全部切块与向量(同事务)。

**路径参数**:`id` = 文档 UUID。

**响应 · 200 OK**

```json
{
  "id": "doc_001-...",
  "deleted": true,
  "chunks_removed": 12
}
```

**错误码**

| HTTP | code | 说明 |
|------|------|------|
| 404 | NF_001 | 文档不存在 |

---

### 6.4 POST /api/v1/knowledge/search

对知识库做语义检索(调试/验证用),返回命中的切块与相似度。

**请求体**

```json
{
  "query": "退货需要什么条件?",
  "top_k": 5,
  "min_similarity": 0.7,
  "document_ids": []
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `query` | string | 是 | 查询文本 |
| `top_k` | int | 否 | 返回条数,默认取 `RAG_TOP_K` |
| `min_similarity` | float | 否 | 最低相似度阈值,默认取 `RAG_MIN_SIMILARITY` |
| `document_ids` | string[] | 否 | 限定在指定文档内检索 |

**响应 · 200 OK**

```json
{
  "query": "退货需要什么条件?",
  "chunks": [
    {
      "id": "chunk_001-...",
      "document_id": "doc_001-...",
      "title": "退货政策",
      "chunk_index": 0,
      "content": "7 天内无理由退货,需保留吊牌...",
      "score": 0.92
    }
  ]
}
```

**错误码**

| HTTP | code | 说明 |
|------|------|------|
| 502 | LLM_001 | Embedding 服务不可用 |
| 500 | RAG_001 | 向量检索失败 |

---

## 7. 管理接口

> 所有管理接口需要 **admin 角色**。

### 7.1 GET /api/v1/admin/stats

系统统计概览,用于管理后台首页。

**响应 · 200 OK**

```json
{
  "users": {
    "total": 1280,
    "active_today": 320
  },
  "sessions": {
    "total": 9821,
    "active": 12,
    "transferred_today": 8
  },
  "messages": {
    "total": 45230,
    "today": 1820,
    "tokens_today": 312000
  },
  "knowledge": {
    "documents": 56,
    "chunks": 4200
  },
  "generated_at": "2026-07-17T10:00:00+00:00"
}
```

---

### 7.2 GET /api/v1/admin/audit-logs

审计日志查询(登录/转人工/知识库变更等敏感操作)。

**Query 参数**

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `page` | int | 1 | 页码 |
| `page_size` | int | 50 | 每页条数 |
| `user_id` | string | - | 过滤某用户 |
| `action` | string | - | 过滤动作类型(对齐 `audit_action` 枚举):`login`/`logout`/`upload_doc`/`transfer_human`/`call_tool` |
| `start` | string | - | 起始时间 ISO8601 |
| `end` | string | - | 结束时间 ISO8601 |

**响应 · 200 OK**

```json
{
  "items": [
    {
      "id": "log_001-...",
      "user_id": "8f3c1a2e-...",
      "username": "alice",
      "action": "upload_doc",
      "target": "doc_001-...",
      "ip_address": "203.0.113.5",
      "detail": {"title": "退货政策"},
      "created_at": "2026-07-17T10:00:00+00:00"
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 50
}
```

---

### 7.3 GET /api/v1/admin/users

用户列表(管理员查看/提权/停用)。

**Query 参数**:`page`、`page_size`、`q`(用户名/邮箱模糊)、`role`(`user`/`admin`)。

**响应 · 200 OK**

```json
{
  "items": [
    {
      "id": "8f3c1a2e-...",
      "username": "alice",
      "email": "alice@example.com",
      "role": "user",
      "is_active": true,
      "session_count": 15,
      "last_login_at": "2026-07-17T09:00:00+00:00",
      "created_at": "2026-07-01T10:00:00+00:00"
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 20
}
```

---

## 8. 运维接口

### 8.1 GET /health

存活探针(liveness),只要进程在跑就返回 200。**不查依赖**。

**响应 · 200 OK**

```json
{
  "status": "ok",
  "app": "AI Customer Service",
  "version": "0.1.0"
}
```

用于 Kubernetes `livenessProbe` / Docker `HEALTHCHECK`。

---

### 8.2 GET /ready

就绪探针(readiness),检查关键依赖(数据库连通性)是否就绪,决定是否接流量。

**响应 · 200 OK**

```json
{
  "status": "ready",
  "checks": {
    "database": "ok",
    "ark_llm": "ok"
  }
}
```

**响应 · 503 Service Unavailable**(依赖未就绪)

```json
{
  "status": "not_ready",
  "checks": {
    "database": "fail",
    "ark_llm": "ok"
  }
}
```

用于 Kubernetes `readinessProbe`,未就绪时从 Service 端点摘除。

---

### 8.3 GET /metrics

Prometheus 格式指标,供 Prometheus 抓取。

**响应 · 200 OK**(`Content-Type: text/plain; version=0.0.4`)

```text
# HELP http_requests_total HTTP 请求总数
# TYPE http_requests_total counter
http_requests_total{method="POST",path="/api/v1/chat",status="200"} 1234
# HELP http_request_duration_seconds HTTP 请求耗时
# TYPE http_request_duration_seconds histogram
http_request_duration_seconds_bucket{path="/api/v1/chat",le="0.5"} 1100
http_request_duration_seconds_bucket{path="/api/v1/chat",le="1.0"} 1200
# HELP pipeline_stage_duration_seconds Pipeline 各阶段耗时
# TYPE pipeline_stage_duration_seconds histogram
pipeline_stage_duration_seconds_bucket{stage="rag_retriever",le="0.1"} 980
# HELP llm_tokens_total LLM token 消耗
# TYPE llm_tokens_total counter
llm_tokens_total{type="input"} 1200000
llm_tokens_total{type="output"} 380000
```

> 生产环境建议在反向代理层对 `/metrics` 加 IP 白名单,避免暴露内部指标。

---

## 9. 错误码总表

| 错误码 | HTTP | 含义 | 触发场景 |
|--------|------|------|---------|
| `VAL_001` | 422 | 请求参数校验失败 | 业务层校验不通过 |
| `VAL_002` | 422 | 请求参数校验失败 | FastAPI 自动校验不通过 |
| `VAL_010` | 413 | 文件过大 | 上传文档超限 |
| `AUTH_001` | 401 | 认证失败 | 未携带/无效/过期 Token |
| `AUTH_002` | 500 | 创建 token 缺少 sub | 内部错误 |
| `AUTH_003` | 401 | 登录已过期 | JWT exp 过期 |
| `AUTH_004` | 401 | 认证凭据无效 | 签名无效/格式错误 |
| `AUTH_005` | 401 | token 缺少 subject | 防御性校验失败 |
| `AUTH_010` | 409 | 用户名或邮箱已存在 | 注册冲突 |
| `AUTH_011` | 401 | 用户名或密码错误 | 登录失败 |
| `AUTH_012` | 403 | 账号已停用 | is_active=false |
| `AUTHZ_001` | 403 | 无权访问该资源 | 越权/角色不足 |
| `NF_001` | 404 | 资源不存在 | 会话/文档/消息不存在 |
| `SESSION_001` | 409 | 会话已关闭 | 重复关闭会话 |
| `LLM_001` | 502 | AI 服务暂时不可用 | 方舟超时/限流/5xx |
| `RAG_001` | 500 | 知识库检索失败 | 向量检索/embedding 出错 |
| `PIPE_001` | 500 | 对话处理失败 | Pipeline 编排异常 |
| `DB_001` | 500 | 数据服务异常 | 数据库操作失败 |
| `RL_001` | 429 | 请求过于频繁 | 触发限流 |
| `INT_001` | 500 | 服务器内部错误 | 未捕获异常兜底 |

**统一错误响应体**

```json
{
  "error": {
    "code": "LLM_001",
    "message": "AI 服务暂时不可用,请稍后重试",
    "detail": {}
  }
}
```
