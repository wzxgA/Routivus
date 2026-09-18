"""Routivus: smart-routing multi-agent backend (port of Routivus core)."""

from routivus.http_env import sanitize_no_proxy_env

__version__ = "0.1.0"

# 导入期自检一次：系统代理设置里的畸形 no_proxy 会让所有 httpx 客户端构造失败
# （详见 routivus/http_env.py 的模块注释）。放在这里是为了覆盖所有调用点。
sanitize_no_proxy_env()
