"""鉴权路由:注册 / 登录 / 当前用户。

设计要点:
- 密码哈希用 passlib[bcrypt],绝不存明文;校验用常量时间比较防侧信道。
- 登录限流 5 次/分钟(按 IP),防爆破;若需按 username 维度限流,可扩展
  ``key_func``。slowapi 装饰器需在函数定义期绑定 limiter,故用共享的
  ``app.core.rate_limit.limiter``。
- 关键操作(注册/登录)写 audit_log,留痕便于安全审计与异常排查。
- token 用 JWT(stateless),claim 含 sub(user_id)/role/exp,
  避免每次请求查库验证 token 合法性(用户存在性仍查库,见 deps)。
- schemas 用运行时 import:模块加载期 schemas 可能尚未就绪(并行开发),
  函数内 import 保证路由文件可在任何阶段被 import 而不报错。
"""

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.logging import get_logger
from app.core.rate_limit import limiter
from app.models.user import User
from app.schemas.auth import TokenResponse, UserCreate, UserLogin, UserResponse

logger = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# 登录限流:5 次/分钟/IP。经验值--正常用户极少 1 分钟内登录 5 次,
# 但足以挡住自动化爆破。slowapi 默认内存后端,多实例部署需换 redis。
LOGIN_RATE_LIMIT = "5/minute"


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    summary="注册新用户",
)
async def register(
    payload: UserCreate,
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """注册新用户。

    步骤:唯一性校验 -> 哈希密码 -> 落库 -> 审计日志。
    唯一性校验在应用层做(而非仅依赖 DB 唯一约束),目的是返回
    友好的字段级错误,而非暴露 DB IntegrityError 给前端。
    """
    from app.core.exceptions import ValidationError
    from app.core.security import hash_password
    from app.repositories.audit_log_repository import AuditLogRepository
    from app.repositories.user_repository import UserRepository
    repo = UserRepository(db)
    # 唯一性预检:避免直接抛 IntegrityError,给前端可读的字段级提示
    if await repo.get_by_username(payload.username) is not None:
        raise ValidationError("用户名已被占用", detail={"field": "username"})
    if await repo.get_by_email(payload.email) is not None:
        raise ValidationError("邮箱已被注册", detail={"field": "email"})

    password_hash = hash_password(payload.password)
    user = await repo.create(
        username=payload.username,
        email=payload.email,
        password_hash=password_hash,
    )

    # 审计日志:记录注册事件,actor 为新用户自身。
    # 不记录密码等敏感字段;user_id 用于关联追溯。
    audit_repo = AuditLogRepository(db)
    await audit_repo.create(
        actor_id=user.id,
        action="user.register",
        detail={"username": user.username, "email": user.email},
    )
    await db.commit()

    logger.info("用户注册成功", user_id=str(user.id), username=user.username)
    return UserResponse.model_validate(user, from_attributes=True)


@router.post(
    "/login",
    status_code=status.HTTP_200_OK,
    summary="登录获取 token",
)
@limiter.limit(LOGIN_RATE_LIMIT)
async def login(
    request: Request,  # slowapi 要求第一个参数为 Request,用于取 key
    payload: UserLogin,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """登录并签发 JWT。

    限流:每 IP 5 次/分钟,防暴力破解。

    安全细节:
    - 用户不存在与密码错误返回相同提示,避免用户名枚举。
    - 密码校验用常量时间比较(passlib 内部实现)。
    """
    from app.core.exceptions import AuthenticationError
    from app.core.security import create_access_token, verify_password
    from app.repositories.audit_log_repository import AuditLogRepository
    from app.repositories.user_repository import UserRepository
    repo = UserRepository(db)
    user = await repo.get_by_username(payload.username)

    # 统一错误:用户不存在与密码错误都报"用户名或密码错误",
    # 防止攻击者通过响应差异探测有效用户名(用户名枚举攻击)。
    if user is None or not verify_password(payload.password, user.password_hash):
        logger.warning(
            "登录失败",
            username=payload.username,
            reason="user_not_found" if user is None else "wrong_password",
        )
        raise AuthenticationError("用户名或密码错误")

    if not user.is_active:
        raise AuthenticationError("账号已被禁用")

    # 签发 token:claim 含 sub/role/exp;exp 由 config 控制。
    # create_access_token 接收 data dict(含 sub/role),exp 由内部按 config 填充。
    token = create_access_token(
        {"sub": str(user.id), "role": user.role.value}
    )

    audit_repo = AuditLogRepository(db)
    await audit_repo.create(
        actor_id=user.id,
        action="user.login",
        detail={"ip": request.client.host if request.client else None},
    )
    await db.commit()

    logger.info("用户登录成功", user_id=str(user.id))
    return TokenResponse(access_token=token, token_type="bearer")


@router.get(
    "/me",
    summary="获取当前登录用户",
)
async def me(
    current_user: User = Depends(get_current_user),
) -> None:
    """返回当前 token 对应的用户信息。

    依赖 ``get_current_user`` 完成鉴权,这里只做序列化。
    用于前端刷新页面后恢复登录态。
    """
    return UserResponse.model_validate(current_user, from_attributes=True)
