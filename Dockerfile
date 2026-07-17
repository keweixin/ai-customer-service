# ============================================================
# AI 客服系统 Dockerfile(多阶段构建)
# ============================================================
# 设计要点:
# 1. 多阶段构建:builder 阶段编译 wheel,运行阶段只装 wheel,
#    最终镜像不含构建工具与源码缓存,体积小、攻击面小。
# 2. 运行阶段用非 root 用户(appuser),最小权限原则。
# 3. 生产用 gunicorn + uvicorn worker,多进程稳定且支持优雅停机;
#    开发可通过环境变量切回纯 uvicorn --reload。
# 4. HEALTHCHECK 用 curl 探 /health,Docker/K8s 据此判断存活。
# 5. 启动时先跑 alembic upgrade head 再起服务,保证 schema 就绪。
# ============================================================

# ---------- 构建阶段:编译 wheel ----------
FROM python:3.11-slim AS builder

# 装 build 依赖:build 用于打 wheel,无需留在运行镜像
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先拷构建元信息,利用 Docker 层缓存:依赖不变则跳过重新构建 wheel
COPY pyproject.toml README.md ./
COPY src/ ./src/

# 构建 wheel 到 dist/
RUN pip install --no-cache-dir build \
    && python -m build --wheel \
    && pip install --no-cache-dir wheel

# ---------- 运行阶段:精简镜像 ----------
FROM python:3.11-slim

# 装 curl(HEALTHCHECK 用)+ libpq(asyncpg 运行时需要)
# 清理 apt 缓存减小体积
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 从 builder 拷 wheel 并安装,装完即删 wheel 文件
COPY --from=builder /app/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl \
    && rm /tmp/*.whl

# 拷应用代码与迁移配置(运行时需要 alembic 迁移与源码)
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY alembic.ini ./
COPY scripts/ ./scripts/

# 创建非 root 用户并移交目录权限
RUN useradd -m -r appuser \
    && chown -R appuser:appuser /app

USER appuser

# 健康检查:每 30s 探 /health,超时 5s,连续 3 次失败标记 unhealthy
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

EXPOSE 8000

# 默认生产启动:先迁移后启动。
# 用 sh -c 串联命令,alembic 失败则不启动服务(避免带病运行)。
# gunicorn 管 uvicorn worker:--workers 按 CPU 调,--timeout 兼顾长对话,
# --graceful-timeout 给 SSE 流收尾时间。
CMD ["sh", "-c", "alembic upgrade head && gunicorn src.app.main:app -k uvicorn.workers.UvicornWorker --workers 4 --bind 0.0.0.0:8000 --timeout 120 --graceful-timeout 30 --access-logfile - --error-logfile -"]
