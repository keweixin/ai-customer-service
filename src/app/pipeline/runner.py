"""Pipeline 编排器。

``Pipeline`` 把一组 ``BaseStage`` 顺序串起来执行,支持两种返回模式:

- **流式** :meth:`run_streaming`:前 n-1 阶段顺序执行做"准备工作",最后阶段
  若是 ``StreamGenerator`` 则调用其 ``stream()`` 把 token 逐段 yield 出去,
  供 SSE 端点实时下发。中途短路直接 yield 短路回复。
- **非流式** :meth:`run`:所有阶段顺序执行,最后阶段产出完整文本后整体返回 ctx,
  供需要完整回复的场景(如离线批处理、语音 TTS 一次性合成)。

设计要点:
- **短路**:任一阶段设 ``ctx.short_circuit=True`` 后,流式版立即 yield 短路回复
  并结束;非流式版跳过剩余阶段、把短路回复作为最终结果返回。这是"内容安全不通过
  跳过 5 阶段省 LLM 成本"这一关键决策的落点。
- **异常归一**:阶段内异常已被 ``BaseStage.__call__`` 包装成 ``RuntimeError``;
  这里再统一转为业务层 ``PipelineError``,避免 HTTP 层看到裸 ``RuntimeError``。
- **不在此 import 具体阶段类**:编排器只依赖 ``BaseStage`` 契约,具体阶段由调用方
  组装传入,保持"编排/实现"解耦,也利于单测用桩阶段替换。

关于最后阶段的流式约定:具体阶段类 ``StreamGenerator``(由其他 agent 补充)在
继承 ``BaseStage`` 之外额外提供 ``async def stream(ctx) -> AsyncIterator[str]``。
本模块用鸭子类型(``hasattr(last_stage, "stream")``)判断,而非在框架层硬编码
``StreamGenerator`` 类型,使框架不耦合到具体阶段实现。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from app.core.exceptions import PipelineError
from app.core.logging import get_logger
from app.pipeline.context import DialogContext
from app.pipeline.stages.base import BaseStage

_logger = get_logger(__name__)


class Pipeline:
    """对话流水线编排器。

    持有一组阶段,按构造顺序依次执行。线程安全:阶段列表在构造后不再变更,
    多个请求并发使用同一 ``Pipeline`` 实例是安全的(每个请求自带独立 ctx)。

    Args:
        stages: 有序阶段列表,最后一个阶段通常是 ``StreamGenerator``(生成阶段)。
    """

    def __init__(self, stages: list[BaseStage]) -> None:
        if not stages:
            # 空流水线没有合法语义,与其在调用时报 AttributeError,不如构造期 fail-fast
            raise ValueError("Pipeline 至少需要 1 个阶段")
        self._stages: list[BaseStage] = list(stages)

    async def run_streaming(self, ctx: DialogContext) -> AsyncIterator[str]:
        """流式执行:逐 token yield 最终回复文本。

        流程:
        1. 顺序执行前 ``n-1`` 个阶段(准备阶段),每阶段后检查短路;
        2. 任一准备阶段短路 -> yield ``ctx.short_circuit_reply`` 并结束;
        3. 最后阶段若是流式生成器(具备 ``stream`` 方法),调用 ``stream(ctx)``
           逐段 yield token;否则退化调用 ``__call__`` 并一次性 yield 完整回复;
        4. 阶段异常统一转 ``PipelineError`` 后 yield 出错提示并结束,避免抛异常
           打断 SSE 流(客户端通过 error 事件感知)。

        Args:
            ctx: 当前对话上下文。

        Yields:
            字符串片段:流式模式下为逐 token/批次文本;短路或非流式末阶段为整段文本。
        """
        logger = _logger.bind(session_id=ctx.session_id, user_id=ctx.user_id)
        logger.info(
            "pipeline.run_streaming.start",
            stages=[s.name for s in self._stages],
        )

        # 前 n-1 个是"准备阶段":解析/安全/意图/实体/检索/策略,均非流式产出
        prepare_stages = self._stages[:-1]
        final_stage = self._stages[-1]

        for stage in prepare_stages:
            try:
                await stage(ctx)
            except Exception as exc:
                # BaseStage 已包装为 RuntimeError;这里再统一转业务异常并结束流
                logger.exception("pipeline.run_streaming.aborted", stage=stage.name)
                raise PipelineError(
                    f"阶段 {stage.name} 执行失败",
                    detail={"stage": stage.name, "reason": str(exc)},
                ) from exc

            # 短路检查放在每阶段之后:内容不安全等场景直接终止,省后续成本
            if ctx.short_circuit:
                logger.info(
                    "pipeline.run_streaming.short_circuit",
                    at_stage=stage.name,
                    reply_len=len(ctx.short_circuit_reply),
                )
                if ctx.short_circuit_reply:
                    yield ctx.short_circuit_reply
                return

        # 末阶段:优先走流式 stream(),否则退化为整段返回,保证调用方无需感知差异
        try:
            streamer = getattr(final_stage, "stream", None)
            if streamer is not None:
                # StreamGenerator:逐 token 下发,SSE 端点直接转发
                async for chunk in streamer(ctx):
                    yield chunk
            else:
                # 非流式末阶段(如批处理场景):执行后一次性产出完整文本
                await final_stage(ctx)
                # 约定非流式末阶段把完整回复写入 strategy.reply 或 short_circuit_reply
                reply = ctx.strategy.get("reply") or ctx.short_circuit_reply
                if reply:
                    yield reply
        except Exception as exc:
            logger.exception(
                "pipeline.run_streaming.final_failed", stage=final_stage.name
            )
            raise PipelineError(
                f"生成阶段 {final_stage.name} 执行失败",
                detail={"stage": final_stage.name, "reason": str(exc)},
            ) from exc

        logger.info("pipeline.run_streaming.done")

    async def run(self, ctx: DialogContext) -> DialogContext:
        """非流式执行:所有阶段顺序跑完,返回填充完整的 ctx。

        适用场景:离线批处理、语音 TTS 一次性合成、内部回放测试等无需实时下发
        token 的链路。短路时跳过剩余阶段,直接返回 ctx(其中 ``short_circuit_reply``
        已含最终回复),调用方据此取用。

        Args:
            ctx: 当前对话上下文。

        Returns:
            执行完毕的 ctx(就地修改后返回同一实例)。

        Raises:
            PipelineError: 任意阶段执行失败时抛出,携带阶段名与原因。
        """
        logger = _logger.bind(session_id=ctx.session_id, user_id=ctx.user_id)
        logger.info("pipeline.run.start", stages=[s.name for s in self._stages])

        for stage in self._stages:
            try:
                await stage(ctx)
            except Exception as exc:
                logger.exception("pipeline.run.aborted", stage=stage.name)
                raise PipelineError(
                    f"阶段 {stage.name} 执行失败",
                    detail={"stage": stage.name, "reason": str(exc)},
                ) from exc

            # 短路:剩余阶段不再执行,ctx 即为最终状态
            if ctx.short_circuit:
                logger.info(
                    "pipeline.run.short_circuit",
                    at_stage=stage.name,
                    reply_len=len(ctx.short_circuit_reply),
                )
                break

        logger.info("pipeline.run.done")
        return ctx
