"""Prometheus 指标定义。

集中定义业务可观测性指标,供 ``/metrics`` 端点暴露给 Prometheus 抓取。
指标命名遵循 Prometheus 最佳实践(snake_case + 单位后缀)。

指标清单:
- http_requests_total:HTTP 请求计数(method/path/status)
- http_request_duration_seconds:HTTP 请求耗时(method/path)
- llm_calls_total:LLM 调用计数(model/status)
- llm_call_duration_seconds:LLM 调用耗时(model)
- rag_retrieval_total:RAG 检索计数(status)
- pipeline_stage_duration_seconds:对话流水线各阶段耗时(stage)
"""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Histogram,
    REGISTRY,
    generate_latest,
)

# 使用全局 REGISTRY 而非自建,避免与 prometheus_client 默认 /metrics 冲突。
# 模块级单例:Prometheus 指标天然全局,重复注册会抛错,故用 getattr 兜底防重复。

_HTTP_BUCKETS = (
    # 覆盖典型 web 请求耗时分布(从 5ms 到 10s),足够定位 P99
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)

_LLM_BUCKETS = (
    # LLM 调用普遍较慢,桶位整体右移(0.1s ~ 60s)
    0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0,
)


def _get_or_create_counter(name: str, desc: str, labels: tuple[str, ...]) -> Counter:
    """复用已注册的 Counter,避免测试/热重载时重复注册报错。"""
    metric = REGISTRY._names_to_collectors.get(name)  # type: ignore[attr-defined]
    if isinstance(metric, Counter):
        return metric
    return Counter(name, desc, labels)


def _get_or_create_histogram(
    name: str, desc: str, labels: tuple[str, ...], buckets: tuple[float, ...]
) -> Histogram:
    """复用已注册的 Histogram,避免重复注册报错。"""
    metric = REGISTRY._names_to_collectors.get(name)  # type: ignore[attr-defined]
    if isinstance(metric, Histogram):
        return metric
    return Histogram(name, desc, labels, buckets=buckets)


# ------------------------------------------------------------------
# 指标实例
# ------------------------------------------------------------------

http_requests_total: Counter = _get_or_create_counter(
    "http_requests_total",
    "HTTP 请求总数",
    ("method", "path", "status"),
)

http_request_duration_seconds: Histogram = _get_or_create_histogram(
    "http_request_duration_seconds",
    "HTTP 请求处理耗时",
    ("method", "path"),
    _HTTP_BUCKETS,
)

llm_calls_total: Counter = _get_or_create_counter(
    "llm_calls_total",
    "LLM 调用总数",
    ("model", "status"),
)

llm_call_duration_seconds: Histogram = _get_or_create_histogram(
    "llm_call_duration_seconds",
    "LLM 单次调用耗时",
    ("model",),
    _LLM_BUCKETS,
)

rag_retrieval_total: Counter = _get_or_create_counter(
    "rag_retrieval_total",
    "RAG 检索次数",
    ("status",),
)

pipeline_stage_duration_seconds: Histogram = _get_or_create_histogram(
    "pipeline_stage_duration_seconds",
    "对话流水线各阶段耗时",
    ("stage",),
    _HTTP_BUCKETS,
)


def get_metrics() -> bytes:
    """返回 Prometheus 文本格式指标内容,供 ``/metrics`` 端点直接返回。

    返回 bytes 而非 str,因为 prometheus_client 输出为 UTF-8 编码,
    FastAPI ``Response(content=..., media_type=CONTENT_TYPE_LATEST)`` 直接消费。
    """
    return generate_latest(REGISTRY)


def get_registry() -> CollectorRegistry:
    """返回当前使用的 CollectorRegistry,主要用于测试隔离场景。"""
    return REGISTRY
