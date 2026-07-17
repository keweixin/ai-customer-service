"""v1 路由聚合:把各业务子路由挂到统一的 ``/api/v1`` 前缀下。

设计要点:
- 子路由各自已带子前缀(如 ``/auth``),这里只做 include_router 拼装,
  最终路径形如 ``/api/v1/auth/login``。
- 每个子路由的 import 放在函数内会丢失 OpenAPI tags 分组能力,
  故在模块级 import,配合各 router 已声明的 tags 生成分组文档。
- ``api_router`` 作为对外唯一出口,``main.py`` 只需 include 一次。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import admin, auth, chat, knowledge

api_router = APIRouter(prefix="/api/v1")

# 各业务域子路由:每个 router 内部已声明 prefix 与 tags,
# include 时无需重复指定,保持单一事实来源(各文件)。
api_router.include_router(auth.router)
api_router.include_router(chat.router)
api_router.include_router(knowledge.router)
api_router.include_router(admin.router)

__all__ = ["api_router"]
