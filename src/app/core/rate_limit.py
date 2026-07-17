"""全局限流器实例(基于 slowapi)。

为什么单独成模块:
- slowapi 的 ``@limiter.limit(...)`` 装饰器需要路由模块在定义期就拿到
  ``Limiter`` 实例,而 ``main.py`` 又要把它挂到 ``app.state`` 并注册中间件。
- 若把 ``Limiter`` 定义在 ``main.py``,路由文件 import 它会造成循环依赖
  (main -> router -> main)。
- 因此放在独立的轻量模块里,路由与 main 各自单向 import,打破环。

key_func 取客户端 IP:在反向代理(Nginx/CDN)后部署时,需确保代理已设置
``X-Forwarded-For`` 且配置 ``X-Forwarded-For`` 为可信头,否则 IP 会被伪造。
"""

from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

# 进程级单例 limiter,所有路由共用同一计数后端(默认内存)。
# 生产可替换为 redis 后端以支持多实例:limiter = Limiter(..., storage_uri="redis://...")
limiter: Limiter = Limiter(key_func=get_remote_address)
