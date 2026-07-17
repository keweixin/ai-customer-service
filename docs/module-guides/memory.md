# 记忆模块指南 · 短期记忆 / 长期记忆

> 本文档描述 AI 客服系统的记忆机制:短期记忆(会话内历史)、长期记忆(跨会话画像 + 摘要),以及滑动窗口与摘要压缩策略。
> 代码位于 `src/app/services/memory_service.py`,数据表 `messages` / `user_profiles` / `sessions`。

## 目录

- [1. 记忆体系总览](#1-记忆体系总览)
- [2. 短期记忆(会话内)](#2-短期记忆会话内)
- [3. 长期记忆(跨会话)](#3-长期记忆跨会话)
- [4. 用户画像结构](#4-用户画像结构)
- [5. 滑动窗口策略](#5-滑动窗口策略)
- [6. 摘要压缩](#6-摘要压缩)
- [7. 记忆组装与 Token 预算](#7-记忆组装与-token-预算)

---

## 1. 记忆体系总览

系统采用**三层记忆模型**,在 token 预算内最大化上下文信息密度:

```text
┌─────────────────────────────────────────────────────────┐
│  长期记忆 (跨会话,持久化在 user_profiles)                │
│  ┌─────────────────────────────────────────────────┐    │
│  │  profile_data (JSONB):偏好/实体/标签             │    │
│  │  summary (Text):历史对话摘要(压缩长期记忆)       │    │
│  └─────────────────────────────────────────────────┘    │
└───────────────────────────┬─────────────────────────────┘
                            │ 注入到 system_prompt
                            ▼
┌─────────────────────────────────────────────────────────┐
│  短期记忆 (会话内,持久化在 messages)                     │
│  ┌──────────────────┐  ┌──────────────────────────┐     │
│  │ 滑动窗口(最近 N 轮)│  │ 更早的消息(被摘要替代)   │     │
│  │ 原文进 prompt     │  │ 不进 prompt               │     │
│  └──────────────────┘  └──────────────────────────┘     │
└───────────────────────────┬─────────────────────────────┘
                            │ 进 LLM messages
                            ▼
                       LLM 生成回复
```

| 层 | 存储 | 生命周期 | 作用 |
|----|------|---------|------|
| 短期记忆 | `messages` 表 | 会话内 | 保持上下文连贯(多轮指代、上下文承接) |
| 长期-画像 | `user_profiles.profile_data` | 跨会话持久 | 结构化偏好/实体,命中即用免重抽 |
| 长期-摘要 | `user_profiles.summary` | 跨会话持久 | 长历史压缩成几百字,降 token 成本 |

**为什么三层而非一层**:全塞历史消息 token 爆炸且贵;只塞摘要丢细节;只塞画像缺对话连贯性。三层组合在质量与成本间平衡。

---

## 2. 短期记忆(会话内)

### 2.1 存储

- 每条消息存 `messages` 表,字段:`session_id`、`role`(system/user/assistant/tool)、`content`、`tokens_used`、`metadata`(JSONB,存 tool_calls/引用块)、`created_at`。
- 消息一旦写入**不可变**(对话流水只追加),`Message` 模型不继承 `TimestampMixin`(无 updated_at)。
- 复合索引 `(session_id, created_at)` 优化对话回放查询。

### 2.2 读取(组装上下文)

每次对话请求,Pipeline 在构造 `PipelineContext` 时读取该会话历史:

```python
history = await message_repo.list_by_session(
    session_id, limit=WINDOW_SIZE, order="asc"
)
ctx.history = history
```

- 按 `created_at` 正序取最近 N 轮(N 由滑动窗口决定,见 §5)。
- 角色对齐 OpenAI:`system` / `user` / `assistant` / `tool`,可直接喂给 LLM。
- `tool` 角色消息(工具结果)保留,Function Calling 多轮才连贯。

### 2.3 写入

- 用户消息:请求进入时**先写库**(user 角色),保证断流也有用户侧记录。
- 助手消息:生成结束后**一次性写库**(assistant 角色),含 `tokens_used` 与 `metadata`(tool_calls/sources)。
- 中断处理:若 SSE 中断,助手消息标记 `metadata.interrupted=true`,下次请求时组装上下文可据此提示"上次回复未完成"。

---

## 3. 长期记忆(跨会话)

### 3.1 存储

- `user_profiles` 表,与 `users` 一对一(`user_id` UNIQUE)。
- `profile_data`(JSONB):结构化画像(见 §4)。
- `summary`(Text):LLM 生成的对话摘要。
- 懒创建:用户首次有画像信息时才插入记录,避免空画像占行。

### 3.2 写入时机

- **实时写画像**:`EntityTracker` 阶段抽到新实体/偏好,通过 `MemoryService.update_profile()` 异步更新 `profile_data`(合并而非覆盖)。
- **异步写摘要**:会话结束(关闭/超时)或消息数达阈值,异步触发摘要生成,更新 `summary`。
- 写入用 `UPDATE ... SET profile_data = profile_data || :patch`(JSONB merge),并发安全。

### 3.3 读取

- 每次对话请求,`MemoryService.get_profile(user_id)` 读取画像与摘要。
- 注入到 `StrategyInjector` 阶段的 system_prompt。
- 画像中的实体也可被 `EntityTracker` 用作回退("我的订单"-> 取画像 `last_order_no`)。

---

## 4. 用户画像结构

`profile_data` 是 JSONB,schema-less 便于演进。推荐结构:

```json
{
  "preferences": {
    "tone": "formal",              // 偏好语气:formal/casual
    "language": "zh",              // 偏好语言
    "contact_channel": "sms"       // 偏好联系方式
  },
  "entities": {
    "name": "张三",                // 用户姓名(从对话抽取)
    "phone": "138****1234",        // 脱敏电话
    "address": "北京市朝阳区...",
    "last_order_no": "20260716001",// 最近订单号
    "member_level": "gold"         // 会员等级(可从业务系统同步)
  },
  "traits": [
    "price_sensitive",             // 价格敏感
    "night_active",                // 夜间活跃
    "frequent_returner"            // 频繁退货
  ],
  "stats": {
    "total_sessions": 15,
    "total_messages": 230,
    "avg_satisfaction": 4.2
  },
  "updated_fields": ["entities.last_order_no"]  // 最近更新字段,便于审计
}
```

### 4.1 字段说明

| 分组 | 用途 | 写入来源 |
|------|------|---------|
| `preferences` | 调整回复风格 | EntityTracker 抽取 / 用户显式告知 |
| `entities` | 工具调用回退 / 个性化 | EntityTracker 抽取 / 业务系统同步 |
| `traits` | 用户分群 / 策略调整 | 统计推断(如频繁退货触发安抚策略) |
| `stats` | 画像展示 / 风控 | 会话结束统计 |
| `updated_fields` | 审计 / 调试 | 系统自动维护 |

### 4.2 演进原则

- **JSONB 而非定列**:画像结构随业务变,JSONB 免频繁加列迁移。
- **GIN 索引**:对 `profile_data` 建 GIN 索引,支持结构化查询(如"找出所有 gold 会员")。
- **脱敏**:敏感字段(电话、地址)存脱敏值,明文只在业务系统。
- **合并语义**:`update_profile` 用 JSONB merge(`||`),只更新传入字段,不覆盖整对象。

---

## 5. 滑动窗口策略

### 5.1 为什么需要滑动窗口

LLM 上下文窗口有限(如 8k/32k token),把会话全部历史塞进去:

- token 成本随轮数线性增长,多轮对话极贵。
- 超窗口直接报错或被截断,行为不可控。
- 早期消息对当前轮往往已无关(用户已切换话题)。

滑动窗口只保留**最近 N 轮原文**,更早的由摘要替代,兼顾连贯与成本。

### 5.2 窗口定义

窗口以**轮数**或 **token 数**计量,取先达上限者:

| 参数 | 默认 | 说明 |
|------|------|------|
| `MEMORY_WINDOW_TURNS` | 6 | 保留最近 6 轮(12 条消息:user+assistant) |
| `MEMORY_WINDOW_TOKENS` | 2000 | 窗口内总 token 上限 |

> 这两个参数计划加入 `.env`,当前可硬编码在 `MemoryService`。

### 5.3 窗口算法

```text
全部历史(messages 按 created_at 正序)
   │
   ▼
从末尾向前取,直到:
  - 取满 WINDOW_TURNS 轮,或
  - 累计 tokens 达 WINDOW_TOKENS
   │
   ▼
窗口内消息 -> 进 prompt(原文)
窗口外消息 -> 已被 summary 覆盖,不进 prompt
```

**示例**:会话有 20 轮,窗口 6 轮:

- prompt 含:summary(前 14 轮压缩)+ 最近 6 轮原文。
- 第 21 轮时,第 15 轮移出窗口,触发摘要更新(把第 15 轮并入 summary)。

### 5.4 边界处理

- **新会话**:无历史,窗口为空,直接用 user 消息 + system_prompt。
- **窗口内含 tool 消息**:tool 消息必须与对应的 assistant tool_call 相邻保留,否则 LLM 报错;窗口切割按"轮"而非单条,保证 tool_call + tool_result 成对。
- **system 消息**:system 角色消息不占窗口配额,始终保留(或由策略注入阶段重新生成)。

---

## 6. 摘要压缩

### 6.1 触发时机

| 触发条件 | 说明 |
|---------|------|
| 会话关闭 | `POST /sessions/{id}/close`,异步生成完整会话摘要,并入 user_profiles.summary |
| 窗口溢出 | 窗口外积累到 M 轮,触发增量摘要(把溢出部分并入 summary) |
| 定时任务 | 长时间未关闭的活跃会话,定时摘要防 summary 过时 |

### 6.2 摘要生成

用 LLM 把待摘要的消息序列压缩成结构化摘要:

```text
Prompt 示例:
  你是客服对话摘要助手。把以下对话压缩成 200 字以内,保留:
  - 用户核心诉求与已解决方案
  - 关键实体(订单号、商品、金额)
  - 未解决的问题
  对话:
    [user]: 我订单 20260716001 没到
    [assistant]: 已查询,配送中,预计今日送达。
    ...

输出:
  用户咨询订单 20260716001 物流,已知配送中预计今日达。
  诉求:查物流。已解决。无遗留问题。
```

### 6.3 摘要合并

新摘要与已有 `summary` 合并,而非覆盖:

```text
已有 summary: [会话1] 退货咨询,已退...
新摘要:       [会话2] 物流查询,已解决...

合并后 summary:
  [会话1] 退货咨询,已退...
  [会话2] 物流查询,已解决...
```

- 合并后若超长(如 > 1000 字),再过一次 LLM 做二次压缩(摘要的摘要)。
- 摘要带时间标记,便于 LLM 理解时序("上次咨询退货,这次查物流")。

### 6.4 摘要质量保障

- **保留实体**:摘要必须保留订单号、金额等关键实体,这些是工具调用的依据。
- **保留未决问题**:未解决的问题标注,下次对话可主动跟进。
- **去情绪化**:摘要只记事实,不记情绪表达,降噪声。
- **可回溯**:摘要生成时记录源消息 ID 到 `metadata`,需要细节时可回查原文。

---

## 7. 记忆组装与 Token 预算

### 7.1 组装顺序(StrategyInjector 阶段)

```text
system_prompt 拼接顺序:
  1. 角色设定(你是 XX 客服...)
  2. 用户画像(profile_data 的偏好/实体)         <- 长期记忆
  3. 历史摘要(summary)                          <- 长期记忆压缩
  4. 知识片段(retrieved_chunks)                 <- RAG
  5. 行为约束(拒答范围、转人工条件)

LLM messages 序列:
  [system]  system_prompt(上面 5 部分拼接)
  [user/assistant/tool ...]  滑动窗口内最近 N 轮原文  <- 短期记忆
  [user]  当前用户消息
```

### 7.2 Token 预算管理

LLM 上下文有上限,需在组装时做预算分配:

| 组成 | 预算占比(示例) | 备注 |
|------|----------------|------|
| system_prompt(角色+画像+摘要+约束) | 30% | 摘要超长时二次压缩 |
| 知识片段(retrieved_chunks) | 30% | chunks 数量 = min(top_k, 剩余预算) |
| 历史消息(滑动窗口) | 30% | 窗口按 token 动态收缩 |
| 当前用户消息 | 5% | 一般够用,超长则截断 |
| 输出预留(LLM_MAX_TOKENS) | 5% | 留给生成 |

**超预算处理优先级**(先牺牲哪个):

1. 先缩窗口:减少历史轮数(摘要已覆盖早期)。
2. 再裁 chunks:保留 top_k 中 score 最高的几个。
3. 最后压摘要:二次压缩 summary。
4. system 角色设定与当前消息尽量不动(核心)。

### 7.3 Token 计数

- 用 `tiktoken` 或方舟的 tokenizer 精确计数,避免估算偏差导致超限。
- 计数缓存:同一消息 token 数缓存,避免重复算。
- 摘要与 chunks 的 token 在写入时预算好存 `metadata`,组装时直接读,省实时计数开销。

### 7.4 记忆与成本

| 策略 | token 成本 | 质量 | 适用 |
|------|-----------|------|------|
| 全历史 | 极高 | 最好 | 不现实,仅极短会话 |
| 滑动窗口 + 摘要 | 中 | 好 | **推荐默认** |
| 仅摘要 | 低 | 一般 | 成本敏感、低复杂度场景 |
| 仅最近 1 轮 | 极低 | 差 | 一次性问答 |

系统默认"滑动窗口 + 摘要",通过 `MEMORY_WINDOW_*` 与摘要频率可调,适配不同成本/质量诉求。
