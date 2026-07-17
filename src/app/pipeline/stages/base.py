"""Pipeline 阶段抽象基类 ``BaseStage``。

所有阶段(InputParser / ContentGuard / ... / StreamGenerator)都继承本类,
实现 ``run(ctx)`` 即可获得统一的:
- **structlog 日志**:阶段进出、耗时、上下文摘要自动记录,无需各阶段重复写。
- **耗时埋点**:写入 ``ctx.stage_metrics[name]`` 与 Prometheus
  ``pipeline_stage_duration_seconds``(按 stage label 维度),既能在单次请求
  排障,也能在全局监控里看分布。
- **异常兜底**:阶段内任意异常被捕获并包装抛出,携带阶段名,便于在 runner 层
  统一转为 ``PipelineError`` 对外,而不是让裸堆栈一路冒泡到 HTTP 层。

为什么用 ``__call__`` 包一层而不是直接调 ``run``:
- 横切关注点(日志/指标/异常)只应写一次,分散到 7 个阶段里既重复又易漏。
- 子类只关心"做什么"(``run``),框架负责"怎么记"(``__call__``),符合
  模板方法模式,测试时直接 mock ``run`` 即可。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from app.core.logging import get_logger
from app.core.metrics import pipeline_stage_duration_seconds
from app.pipeline.context import DialogContext


class BaseStage(ABC):
    """所有 Pipeline 阶段的抽象基类。

    子类必须实现 :meth:`run`,框架通过 :meth:`__call__` 统一注入日志/指标/异常处理。

    Attributes:
        name: 阶段名,用于日志标签、Prometheus label 与 ``ctx.stage_metrics`` 的 key。
            子类应覆盖为可读标识(如 ``"ContentGuard"``)。
    """

    name: ClassVar[str] = "BaseStage"

    async def __call__(self, ctx: DialogContext) -> DialogContext:
        """执行阶段(框架入口,请勿在子类覆盖)。

        统一完成:打开始/结束日志 -> 记录耗时到 ctx 与 Prometheus ->
        捕获异常包装为带阶段名的错误。子类应实现 :meth:`run` 而非本方法。

        Args:
            ctx: 当前对话上下文。

        Returns:
            更新后的 ctx(通常即传入实例,就地修改后返回)。

        Raises:
            RuntimeError: 当阶段 ``run`` 抛出异常时,包装后重新抛出,
                携带原始异常与阶段名,供 ``Pipeline`` 统一转 ``PipelineError``。
        """
        logger = get_logger(__name__).bind(stage=self.name)

        # perf_counter 单调且高精度,适合测阶段耗时;注意 start/end 用同一时钟源
        start = time.perf_counter()
        logger.info("pipeline_stage.start", ctx_summary=ctx.summary())

        try:
            result = await self.run(ctx)
        except Exception as exc:
            # 记录失败耗时便于定位"卡在哪一步",status=error 供聚合查询
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            self._record_metrics(ctx, elapsed_ms, status="error")
            logger.exception(
                "pipeline_stage.error",
                elapsed_ms=round(elapsed_ms, 2),
                error_type=type(exc).__name__,
            )
            # 包装而非吞掉:保留原始异常链,runnner 层再转成业务异常
            raise RuntimeError(
                f"阶段 {self.name} 执行失败: {exc!r}"
            ) from exc

        elapsed_ms = (time.perf_counter() - start) * 1000.0
        self._record_metrics(ctx, elapsed_ms, status="ok")
        logger.info(
            "pipeline_stage.end",
            elapsed_ms=round(elapsed_ms, 2),
            short_circuit=ctx.short_circuit,
        )
        return result

    @abstractmethod
    async def run(self, ctx: DialogContext) -> DialogContext:
        """阶段实际逻辑,由子类实现。

        约定:读取所需字段、产出新字段写回 ctx、返回同一 ctx 实例。
        若需要短路(如内容不安全),设 ``ctx.short_circuit = True`` 并填
        ``ctx.short_circuit_reply``,Runner 会据此跳过后续阶段。

        Args:
            ctx: 当前对话上下文(就地修改)。

        Returns:
            更新后的 ctx。
        """
        ...

    def _record_metrics(
        self,
        ctx: DialogContext,
        elapsed_ms: float,
        *,
        status: str,
    ) -> None:
        """把耗时同时写入 ctx(单次请求排障)与 Prometheus(全局监控)。

        写两处而非一处:
        - ``ctx.stage_metrics`` 随请求结束可落审计日志/落库,定位单次问题;
        - Prometheus 指标聚合跨请求分布,做告警与容量规划。两者职责不同,互补。
        """
        ctx.stage_metrics[self.name] = {
            "duration_ms": round(elapsed_ms, 2),
            "status": status,
        }
        # Histogram.observe 不支持 status 维度,这里只用 stage label;
        # 失败次数若需独立计数,后续可在 metrics 模块新增 Counter。
        pipeline_stage_duration_seconds.labels(stage=self.name).observe(
            elapsed_ms / 1000.0
        )
