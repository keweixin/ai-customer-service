"""对话处理流水线核心框架。

本包定义了贯穿一次对话请求的 7 阶段 Pipeline 骨架:
- ``DialogContext``:贯穿全流程的可变载体,各阶段读取并写回其中字段。
- ``Pipeline``:顺序编排各阶段的执行器,支持流式与非流式两种返回模式。
- ``BaseStage``(在 ``app.pipeline.stages`` 下):所有阶段的抽象基类,
  约定 ``(ctx) -> ctx`` 的统一契约与统一的日志/指标/异常处理。

具体阶段类(InputParser / ContentGuard / ... / StreamGenerator)由独立
模块补充,本文件仅导出核心框架符号,避免在框架层硬编码阶段实现,
保持"编排"与"实现"解耦。
"""

from app.pipeline.context import DialogContext
from app.pipeline.runner import Pipeline

__all__ = ["DialogContext", "Pipeline"]
