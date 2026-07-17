# Pipeline 模块指南 · 7 阶段对话流水线

> 本文档描述 AI 客服系统的核心对话处理流水线:7 个阶段的职责、输入输出、实现要点,以及如何扩展、替换、短路与调优。
> 代码位于 `src/app/pipeline/`。

## 目录

- [1. 设计概览](#1-设计概览)
- [2. PipelineContext 数据载体](#2-pipelinecontext-数据载体)
- [3. 七阶段详解](#3-七阶段详解)
  - [3.1 阶段1 InputParser 输入解析](#31-阶段1-inputparser-输入解析)
  - [3.2 阶段2 ContentGuard 内容安全](#32-阶段2-contentguard-内容安全)
  - [3.3 阶段3 IntentClassifier 意图识别](#33-阶段3-intentclassifier-意图识别)
  - [3.4 阶段4 EntityTracker 实体追踪](#34-阶段4-entitytracker-实体追踪)
  - [3.5 阶段5 RagRetriever RAG 检索](#35-阶段5-ragretriever-rag-检索)
  - [3.6 阶段6 StrategyInjector 策略注入](#36-阶段6-strategyinjector-策略注入)
  - [3.7 阶段7 StreamGenerator 流式生成](#37-阶段7-streamgenerator-流式生成)
- [4. 如何加新阶段](#4-如何加新阶段)
- [5. 如何替换阶段实现](#5-如何替换阶段实现)
- [6. 短路逻辑说明](#6-短路逻辑说明)
- [7. 性能调优](#7-性能调优)

---

## 1. 设计概览

Pipeline 把"处理一条用户消息"拆成 7 个**顺序阶段**,每个阶段是一个纯函数 `(ctx: PipelineContext) -> PipelineContext`。阶段之间通过共享的 `PipelineContext` 传递数据,编排器统一调度。

**设计目标**

- **可测试**:每阶段纯函数,单测只需构造 context,无需起服务或 mock 全链路。
- **可替换**:阶段是接口(`PipelineStage`),换实现(如换 LLM、换意图模型)只改该阶段类。
- **可观测**:每阶段产出与耗时写入 context 与日志/Prometheus,可独立追踪。
- **可短路**:任一阶段可标记 `ctx.short_circuit=True`,编排器跳过后续阶段。
- **可扩展**:新需求(多轮澄清、敏感词二次过滤)作为新阶段插入,不污染现有逻辑。

**阶段编排**

```text
InputParser -> ContentGuard -> IntentClassifier -> EntityTracker ->
RagRetriever -> StrategyInjector -> StreamGenerator
```

每个阶段读 context 中上游写入的字段、产出新字段写回。编排器伪代码:

```python
async def run_pipeline(ctx: PipelineContext) -> PipelineContext:
    for stage in self.stages:            # 顺序执行 7 阶段
        with stage_timer(stage.name):    # Prometheus 计时
            ctx = await stage.process(ctx)
            logger.info("pipeline.stage.done", stage=stage.name, ...)
            if ctx.short_circuit:        # 短路则停止后续
                break
    return ctx
```

---

## 2. PipelineContext 数据载体

`PipelineContext` 是贯穿全流程的 dataclass,所有阶段读写它。集中定义字段避免"魔法键"。

```python
@dataclass
class PipelineContext:
    # ---- 入口输入 ----
    raw_text: str                      # 原始用户消息
    session_id: UUID | None            # 会话 ID(None 表示新建)
    user_id: UUID                      # 当前用户
    history: list[Message]             # 历史消息(滑动窗口)
    profile: UserProfile | None        # 用户画像(长期记忆)

    # ---- 阶段1 InputParser 产出 ----
    normalized_text: str = ""          # 规整后文本
    lang: str = "zh"                   # 语言
    message_type: str = "text"         # text/voice/...
    input_metadata: dict = field(default_factory=dict)

    # ---- 阶段2 ContentGuard 产出 ----
    is_safe: bool = True
    safety_reason: str = ""

    # ---- 阶段3 IntentClassifier 产出 ----
    intent: str = "chat"               # chat/faq/order/complaint/transfer/...
    intent_confidence: float = 0.0

    # ---- 阶段4 EntityTracker 产出 ----
    entities: dict = field(default_factory=dict)   # {order_no, name, ...}
    updated_profile: dict = field(default_factory=dict)

    # ---- 阶段5 RagRetriever 产出 ----
    retrieved_chunks: list[dict] = field(default_factory=list)
    context_sources: list[dict] = field(default_factory=list)

    # ---- 阶段6 StrategyInjector 产出 ----
    system_prompt: str = ""
    tool_schema: list[dict] = field(default_factory=list)
    tool_choice: str = "auto"

    # ---- 阶段7 StreamGenerator 产出 ----
    response_tokens: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    tokens_used: int = 0
    finish_reason: str = ""

    # ---- 控制 ----
    short_circuit: bool = False
    short_circuit_payload: dict | None = None
```

> 新增字段直接加在对应分组,避免散落。所有字段有默认值,保证阶段可独立运行。

---

## 3. 七阶段详解

### 3.1 阶段1 InputParser 输入解析

**职责**:把原始用户输入规整成统一内部表示,做基础清洗与语言/类型识别。

**输入**:`raw_text`、`session_id`、`user_id`

**输出**:`normalized_text`、`lang`、`message_type`、`input_metadata`

**实现要点**

- 去除首尾空白、零宽字符、控制字符;统一全/半角标点(可选)。
- 检测语言:中文/英文/混合,用 `langdetect` 或简单规则(中文字符占比)。
- 识别消息类型:纯文本、含图片 URL、语音转写文本等;预留多模态扩展。
- 长度校验:超长消息(如 > 4000 字)截断或拒绝,防 prompt 注入与成本失控。
- **不做**语义理解,只做字面规整;语义留给下游阶段。

**示例**

```python
class InputParser(PipelineStage):
    async def process(self, ctx: PipelineContext) -> PipelineContext:
        text = ctx.raw_text.strip()
        text = strip_control_chars(text)
        if len(text) > 4000:
            raise ValidationError("消息过长", error_code="VAL_003")
        ctx.normalized_text = text
        ctx.lang = detect_lang(text)
        ctx.message_type = "text"
        return ctx
```

---

### 3.2 阶段2 ContentGuard 内容安全

**职责**:对用户输入做内容安全审核,拦截违规/恶意/越狱内容。

**输入**:`normalized_text`

**输出**:`is_safe`、`safety_reason`;不安全时 `short_circuit=True`

**实现要点**

- 多层审核:
  1. 关键词黑名单(快、便宜,覆盖明显违规词)。
  2. 正则规则(手机号/身份证/银行卡脱敏提示、注入特征如 `ignore previous`)。
  3. 可选调内容安全 API(如方舟内容审核、阿里云绿网)。
- 命中违规设置 `is_safe=False` + `safety_reason`,并 `short_circuit=True`,编排器跳过后续 5 阶段,直接返回固定拒答话术。
- 记录审计日志(谁、何时、触发了哪条规则),便于复盘误判。
- **性能**:关键词/正则几毫秒;外部 API 走异步且有超时,失败默认放行 + 告警(避免安全服务故障导致全站不可用)。

**短路示例**

```python
class ContentGuard(PipelineStage):
    async def process(self, ctx: PipelineContext) -> PipelineContext:
        hit, reason = self._check(ctx.normalized_text)
        if hit:
            ctx.is_safe = False
            ctx.safety_reason = reason
            ctx.short_circuit = True
            ctx.short_circuit_payload = {
                "content": "抱歉,该话题暂不支持,请换个问题。"
            }
            audit.log("content_blocked", user_id=ctx.user_id, reason=reason)
        return ctx
```

---

### 3.3 阶段3 IntentClassifier 意图识别

**职责**:判断用户意图,决定后续走 RAG、调工具还是纯闲聊,影响策略注入。

**输入**:`normalized_text`、`history`

**输出**:`intent`、`intent_confidence`

**实现要点**

- 意图枚举(可配):`chat`(闲聊)、`faq`(知识问答)、`order`(查订单)、`complaint`(投诉)、`transfer`(转人工)、`refund`(退款)...
- 实现可选三档,按成本/准确率权衡:
  1. **规则 + 关键词**(最快,几毫秒):"订单/查物流" -> `order`;"人工/转人工" -> `transfer`。
  2. **小模型分类**(中等):用蒸馏的小 BERT 或方舟小模型,几十毫秒,准确率高。
  3. **LLM few-shot**(最准最贵):构造 few-shot prompt 让大模型分类,仅在前两档低置信度时兜底。
- 输出置信度,低于阈值(如 0.5)可触发澄清追问("您是想查订单还是咨询退货?")。
- **缓存**:相同输入的意图可短期缓存,省重复调用。

---

### 3.4 阶段4 EntityTracker 实体追踪

**职责**:从用户消息抽取业务实体(订单号、商品名、姓名、地址),并与用户画像合并,供工具调用与策略注入使用。

**输入**:`normalized_text`、`profile`(画像中的已知实体)

**输出**:`entities`、`updated_profile`

**实现要点**

- 抽取方式:
  - **正则**:订单号 `\d{8,}`、手机号 `1[3-9]\d{9}` 等强格式实体,快且准。
  - **NER 模型 / LLM**:姓名、地址、商品名等开放实体,用小模型或 LLM 抽取。
- 合并画像:`entities` 优先取消息中的新值,缺失则回退 `profile` 中持久化值(如"我的订单"-> 取画像上次订单号)。
- 写回画像:新实体更新 `updated_profile`,由 MemoryService 异步持久化(见 `memory.md`)。
- **歧义处理**:多订单号时优先取最近提及的,或在策略注入阶段触发澄清。

---

### 3.5 阶段5 RagRetriever RAG 检索

**职责**:根据用户消息(与意图)从知识库检索相关片段,为生成阶段提供知识上下文。

**输入**:`normalized_text`、`intent`

**输出**:`retrieved_chunks`、`context_sources`

**实现要点**

- **跳过条件**:意图为 `chat`(纯闲聊)且无知识需求时,跳过检索省成本。
- **查询构造**:可直接用 `normalized_text`,也可结合意图改写 query(如 `order` 意图拼接"订单查询政策")。
- **检索流程**(详见 `rag.md`):
  1. EmbeddingService 把 query 向量化。
  2. pgvector 余弦相似度检索 `top_k * 2` 候选。
  3. 过滤 `score < min_similarity`。
  4. 可选重排序(cross-encoder 或 LLM rerank)取 `top_k`。
- **降级**:RAG 失败不阻塞对话,记 `RAG_001` 告警,生成阶段无知识上下文继续(可能质量下降但不报错)。
- **透明化**:命中的 chunk 元信息写入 `context_sources`,通过 SSE `event: sources` 下发给前端,展示引用来源。

---

### 3.6 阶段6 StrategyInjector 策略注入

**职责**:把前面所有阶段的产出"组装"成给 LLM 的最终输入--系统提示词、工具定义、上下文。

**输入**:`intent`、`entities`、`retrieved_chunks`、`profile`、`summary`、`history`

**输出**:`system_prompt`、`tool_schema`、`tool_choice`

**实现要点**

- **系统提示词模板**:按意图选择模板(FAQ / 订单 / 投诉 / 闲聊),填充:
  - 角色设定("你是 XX 电商的客服助手...")
  - 知识片段(`retrieved_chunks` 拼接,标注来源)
  - 用户画像(`profile`:偏好、已知实体)
  - 对话摘要(`summary`:长期记忆压缩)
  - 行为约束(拒答范围、转人工条件、语气)
- **工具定义**:按意图决定本轮开放哪些 Function Calling 工具:
  - `intent=order` -> 开放 `query_order`、`query_logistics`
  - `intent=transfer` -> 开放 `transfer_to_human`
  - 默认不开放工具,避免误调
- **tool_choice**:`auto`(模型自决)/`none`(禁用)/`required`(强制调用)。
- **Token 预算**:估算 system_prompt + history + chunks 的 token,超预算时优先压缩历史(留摘要)与裁剪 chunks。

**示例**

```python
class StrategyInjector(PipelineStage):
    async def process(self, ctx: PipelineContext) -> PipelineContext:
        template = self.templates[ctx.intent]
        ctx.system_prompt = template.render(
            knowledge=format_chunks(ctx.retrieved_chunks),
            profile=ctx.profile,
            summary=ctx.profile.summary if ctx.profile else "",
            entities=ctx.entities,
        )
        ctx.tool_schema = self.tools_for_intent(ctx.intent)
        ctx.tool_choice = "auto" if ctx.tool_schema else "none"
        return ctx
```

---

### 3.7 阶段7 StreamGenerator 流式生成

**职责**:调用 LLM 流式生成回复,产出 token 序列与工具调用,驱动 SSE 下发。

**输入**:`system_prompt`、`history`、`normalized_text`、`tool_schema`、`tool_choice`

**输出**:`response_tokens`、`tool_calls`、`tokens_used`、`finish_reason`

**实现要点**

- 调方舟 `chat/completions`(OpenAI 兼容)开启 `stream=True`。
- 逐 chunk 产出 token,通过 `yield` 推给 SSE 适配层,实时下发 `event: token`。
- **Function Calling 处理**:模型返回 `tool_calls` 时:
  1. 暂停生成,下发 `event: tool_call`。
  2. 执行工具(`tools/` 下的 FC 工具),下发 `event: tool_result`。
  3. 把工具结果作为 `tool` 角色消息追加,再次调 LLM 继续生成(可循环多轮工具调用)。
- **重试与超时**:用 `tenacity` 对 5xx/超时做指数退避重试(`LLM_MAX_RETRIES`),耗尽抛 `LLM_001`。
- **成本统计**:`tokens_used` 从流末尾的 `usage` 读取,写 message 与指标。
- **落库**:生成结束后一次性写入 `messages`(user + assistant),避免半截消息;中断流标记 `interrupted`。
- **安全兜底**:生成内容可再过一道输出审核(可选),命中则替换为安全话术。

---

## 4. 如何加新阶段

以新增"情感分析"阶段(在意图识别后)为例。

**步骤**

1. **定义阶段类**:在 `src/app/pipeline/stages/` 新建 `sentiment_analyzer.py`,继承 `PipelineStage`,实现 `process`。

```python
# src/app/pipeline/stages/sentiment_analyzer.py
from app.pipeline.base import PipelineStage, PipelineContext

class SentimentAnalyzer(PipelineStage):
    """阶段3.5:情感分析(在意图识别与实体追踪之间)。"""
    name = "sentiment_analyzer"

    async def process(self, ctx: PipelineContext) -> PipelineContext:
        ctx.sentiment = await self._analyze(ctx.normalized_text)  # angry/sad/neutral
        return ctx
```

2. **扩展 context**:在 `PipelineContext` 加字段 `sentiment: str = "neutral"`(归到合适分组)。

3. **注册到编排器**:在 `src/app/pipeline/orchestrator.py` 的阶段列表中,按位置插入。

```python
self.stages = [
    InputParser(),
    ContentGuard(),
    IntentClassifier(),
    SentimentAnalyzer(),   # 新阶段插这里
    EntityTracker(),
    RagRetriever(),
    StrategyInjector(),
    StreamGenerator(),
]
```

4. **消费产出**:在需要用情感的下游阶段(如 `StrategyInjector`)读 `ctx.sentiment`,调整话术(愤怒客户优先安抚)。

5. **加单测**:构造含 `normalized_text` 的 ctx,断言 `process` 后 `sentiment` 正确;mock 模型验证不阻塞主流程。

6. **加指标**:`stage_timer("sentiment_analyzer")` 自动统计耗时,无需额外代码。

**约定**

- 阶段类放 `stages/` 目录,一个文件一个类。
- 阶段 `name` 用蛇形命名,用于日志/指标标签。
- 阶段尽量**幂等**:重复执行不改变结果,便于重试。
- 阶段间**只通过 context 通信**,禁止直接 import 其他阶段类调用,保持解耦。

---

## 5. 如何替换阶段实现

以"把意图识别从规则换成方舟小模型"为例。

**步骤**

1. **新实现同接口**:新建 `intent_classifier_ark.py`,同样继承 `PipelineStage`,实现 `process`,产出 `intent` + `intent_confidence`。

```python
class ArkIntentClassifier(PipelineStage):
    name = "intent_classifier"
    def __init__(self, ark_client): self.ark = ark_client
    async def process(self, ctx):
        ctx.intent, ctx.intent_confidence = await self.ark.classify(ctx.normalized_text)
        return ctx
```

2. **通过依赖注入切换**:在编排器工厂里按配置选择实现。

```python
def build_pipeline(settings):
    intent = (
        ArkIntentClassifier(ark_client)
        if settings.app.use_ark_intent
        else RuleIntentClassifier()
    )
    return PipelineOrchestrator([
        InputParser(), ContentGuard(), intent, EntityTracker(),
        RagRetriever(), StrategyInjector(), StreamGenerator(),
    ])
```

3. **换 LLM 模型**:`StreamGenerator` 的模型名从 `settings.llm.model` 读,改 `.env` 的 `ARK_MODEL` 即可,无需改代码。若换非 OpenAI 兼容协议,则新写一个 `LLMService` 实现,注入到 `StreamGenerator`。

4. **灰度切换**:可同时注册新旧实现,按用户/流量比例路由(如 `if user_id % 100 < 10: 用新实现`),验证后再全量。

**原则**:阶段是接口,业务编排器只依赖 `PipelineStage` 抽象,不依赖具体类--这就是 Pipeline 架构的可替换性收益。

---

## 6. 短路逻辑说明

### 6.1 短路机制

任一阶段可设置 `ctx.short_circuit = True` 并填充 `ctx.short_circuit_payload`,编排器检测到后**立即停止后续阶段**,直接用 payload 生成响应。

```python
# 编排器
for stage in self.stages:
    ctx = await stage.process(ctx)
    if ctx.short_circuit:
        logger.info("pipeline.short_circuit", at=stage.name, reason=ctx.safety_reason)
        break
# 出口:若 short_circuit,用 payload;否则走 StreamGenerator 的流
```

### 6.2 典型短路场景

| 场景 | 触发阶段 | payload | 收益 |
|------|---------|---------|------|
| 内容违规 | ContentGuard | 固定拒答话术 | 省掉 5 阶段 + LLM 调用,毫秒返回 |
| 明确转人工 | IntentClassifier | 转人工提示 + 标记 transferred | 省掉 RAG 与生成 |
| 命中 FAQ 缓存 | RagRetriever | 缓存的标答 | 省掉 LLM 调用 |
| Token 预算耗尽 | StrategyInjector | 降级提示 | 避免超长 prompt 报错 |

### 6.3 短路与 SSE

短路也要走 SSE 协议,保持客户端体验一致:下发 `event: token`(payload 内容)+ `event: done`,而非裸文本。SSE 适配层统一处理"短路 payload"与"正常流"两种来源。

---

## 7. 性能调优

### 7.1 各阶段耗时画像

通过 `pipeline_stage_duration_seconds` 指标定位瓶颈。典型分布:

```text
InputParser       ~1ms     (纯 CPU)
ContentGuard      ~5ms     (关键词) / ~100ms (含外部审核 API)
IntentClassifier  ~10ms    (规则) / ~200ms (小模型) / ~800ms (LLM)
EntityTracker     ~10ms    (正则) / ~300ms (LLM 抽取)
RagRetriever      ~150ms   (embedding 100ms + pgvector 50ms)
StrategyInjector  ~2ms     (模板渲染)
StreamGenerator   首字 ~500ms, 流式持续数秒
```

**瓶颈通常在 LLM 相关阶段**(IntentClassifier LLM 兜底、EntityTracker LLM 抽取、StreamGenerator)。优化重点在减少 LLM 调用与降低首字延迟。

### 7.2 可并行阶段

部分阶段无数据依赖,可并行执行以降低总延迟:

| 可并行组 | 阶段 | 说明 |
|---------|------|------|
| 并行组 A | IntentClassifier + EntityTracker | 两者都只依赖 `normalized_text`,互不读取对方产出。实体抽取结果在策略注入阶段才消费,故可并行 |
| 并行组 B | (IntentClassifier 完成后)RagRetriever 与 EntityTracker | RAG 只需 `normalized_text` + `intent`;若 IntentClassifier 用规则很快,可先跑完再并行 RAG 与 Entity |

**并行实现**(用 `asyncio.gather`):

```python
# 编排器优化版:IntentClassifier 与 EntityTracker 并行
ctx = await IntentClassifier().process(ctx)
ctx, ent_ctx = await asyncio.gather(
    identity(ctx),                      # 占位
    EntityTracker().process(clone(ctx)) # 并行跑实体追踪
)
ctx.entities = ent_ctx.entities
```

> 注意:并行要求阶段**只读共享字段、写不同字段**,否则有竞态。EntityTracker 写 `entities`,IntentClassifier 写 `intent`,无冲突,可安全并行。

### 7.3 其他优化

- **意图缓存**:相同输入的意图结果短期缓存(如 5 分钟),省重复分类。
- **RAG 预热**:高频问题(退货政策、运费)的检索结果缓存。
- **流式优先**:StreamGenerator 用流式,首字延迟从"全量生成耗时"降到"首 token 耗时",体验显著提升。
- **小模型分流**:简单意图走规则/小模型,复杂才调大模型,降低平均延迟与成本。
- **连接复用**:HTTP 客户端(httpx)用连接池,复用到方舟的 TCP 连接。
- **批量 embedding**:RAG 检索只 embed 一条 query,但上传文档时批量 embed,降低单块成本。
- **超时收紧**:`LLM_TIMEOUT` 按阶段设不同值(IntentClassifier 兜底用 LLM 时给 3s,StreamGenerator 给 60s),避免慢调用拖垮整链路。
