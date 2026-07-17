"""Pipeline 阶段实现包。

本包只导出抽象基类 ``BaseStage``。具体阶段类(InputParser / ContentGuard /
IntentClassifier / EntityTracker / RagRetriever / StrategyInjector /
StreamGenerator)由其他模块逐步补充,各自实现 ``BaseStage.run`` 契约。

为什么不在这里聚合导出全部阶段:
- 阶段实现依赖 RAG / LLM / 安全 等下游服务,集中 import 会引入重依赖环,
  也让"加一个阶段"变成修改本文件的耦合操作。
- 调用方(如 ``services/chat.py``)按需从各自子模块 import 具体类即可,
  组装成 ``Pipeline(stages=[...])`` 传入,编排与实现彻底解耦。
"""

from app.pipeline.stages.base import BaseStage

__all__ = ["BaseStage"]
