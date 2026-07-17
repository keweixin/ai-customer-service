"""Repository 基类:泛型 CRUD 封装。

设计要点(与 API/DI 层契约对齐):
- 构造期注入 ``db: AsyncSession``,方法签名不再每处传 db--契合
  ``UserRepository(db).get_by_id(id)`` 的调用风格(deps.py 与各路由均如此),
  调用点更简洁,事务边界仍由 service/路由层通过同一 session 控制;
- 泛型 T 绑定到 Base 子类,子 repository 指定实体类型即可复用全部方法;
- 用 select() 2.0 风格查询,返回 Mapped 对象;
- create/update 接收 dict 而非实体,避免调用方先构造再传入的冗余;
- 不在此 commit/rollback:repository 只 flush 让对象拿到 DB 默认值,
  事务提交/回滚交给上层(service / 路由),避免框架层隐式提交掩盖业务错误。

为什么 db 走构造期注入而非方法参数:
- 现有 api/v1 与 deps.py 全部按 ``Repo(db).method(args)`` 调用,统一注入更自然;
- 一个 repository 实例对应一次请求(请求级 session),生命周期短,不存在跨请求串状态;
- 单测时构造 ``Repo(fake_session)`` 即可替换持久层,无需在每个方法上传 mock。
"""

from __future__ import annotations

from typing import Any, Generic, Optional, TypeVar
from uuid import UUID

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import Base

# 泛型类型变量:约束为 Base 子类,保证有 __table__ 等属性。
T = TypeVar("T", bound=Base)


class BaseRepository(Generic[T]):
    """通用异步 Repository 基类(构造期注入 db)。

    子类用法:
        class UserRepository(BaseRepository[User]):
            def __init__(self, db: AsyncSession) -> None:
                super().__init__(User, db)

            async def get_by_username(self, username: str) -> Optional[User]: ...
    """

    def __init__(self, model: type[T], db: AsyncSession) -> None:
        self.model = model
        self.db = db

    # ---- 读 ----
    async def get_by_id(self, id_: UUID | str) -> Optional[T]:
        """按主键查询单条。

        接受 ``UUID | str``:鉴权层从 JWT claim 取出的 ``sub`` 是字符串,
        直接传入避免每处都做 UUID 转换;SQLAlchemy 会按列类型归一化。
        """
        # db.get 直接按主键取,比 select 更简洁且走主键索引。
        return await self.db.get(self.model, id_)

    # 别名:get 与 get_by_id 等价,保留两者以适配不同团队命名习惯。
    get = get_by_id

    async def list_all(
        self,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> list[T]:
        """分页列表查询(按主键自然序)。

        offset/limit 分页:简单通用;超大表深分页性能差时,子类可改用
        keyset pagination(按游标),此处不预设以保持基类精简。
        """
        stmt = select(self.model).offset(offset).limit(limit)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def count(self) -> int:
        """返回该表总行数,用于管理看板统计。"""
        stmt = select(func.count()).select_from(self.model)
        result = await self.db.execute(stmt)
        return int(result.scalar_one())

    # ---- 写 ----
    async def create(self, data: dict[str, Any]) -> T:
        """创建一条记录。

        data 为字段->值字典,避免调用方先 new 实体再传入的冗余。
        不在此 commit,由上层控制事务提交时机;flush 让 obj 拿到 DB 生成的
        默认值(如 server_default 的 id/created_at)。
        """
        obj = self.model(**data)
        self.db.add(obj)
        await self.db.flush()
        await self.db.refresh(obj)
        return obj

    async def update(self, id_: UUID, data: dict[str, Any]) -> Optional[T]:
        """按主键更新:仅更新 data 中给出的字段(partial update)。"""
        obj = await self.get_by_id(id_)
        if obj is None:
            return None
        for key, value in data.items():
            setattr(obj, key, value)
        await self.db.flush()
        await self.db.refresh(obj)
        return obj

    async def delete_by_id(self, id_: UUID) -> bool:
        """按主键删除,返回是否实际删除了记录。"""
        obj = await self.get_by_id(id_)
        if obj is None:
            return False
        await self.db.delete(obj)
        await self.db.flush()
        return True

    async def delete_all_by(self, **filters: Any) -> int:
        """按等值条件批量删除,返回删除行数。

        用于"摘要后删除已压缩的早期消息"等场景;条件以关键字传入,
        自动拼成 WHERE,避免手写 SQL 拼字符串。
        """
        if not filters:
            # 无条件全删太危险,显式拒绝,防止误调用清空整表。
            raise ValueError("delete_all_by 至少需要一个过滤条件")
        stmt = sa_delete(self.model).where(
            *[getattr(self.model, k) == v for k, v in filters.items()]
        )
        result = await self.db.execute(stmt)
        await self.db.flush()
        return int(result.rowcount or 0)
