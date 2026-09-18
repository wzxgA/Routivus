"""进程级 HTTP 环境自检：别让系统代理配置把整个网络栈打瘫。

**问题**：httpx（0.28 及以前）会把 `no_proxy` 的每一项拼成 URL pattern
`all://{item}` 当"不走代理"的白名单。裸 IPv6（`::1`）它能正确补上方括号，但
**带 CIDR 前缀的 IPv6**（`::1/128`）会拼成 `all://[::1/128]`，`URLPattern` 解析时抛
`httpx.InvalidURL: Invalid port: ':1'`。

要命的是失败点在 `AsyncClient.__init__` —— 也就是**任何** HTTP 请求都发不出去，
而且报错文案指向一个用户从没配过的端口，完全看不出跟自己机器的代理设置有关。

`no_proxy=127.0.0.1,localhost,::1,127.0.0.0/8,::1/128` 这种值是代理 / VPN 工具写进
系统的（Windows 上落在环境变量里），用户无感。这里在**包导入期**做一次自检，把这类
条目退化成裸地址形式（`::1/128` → `::1`）：语义仍是"该地址不走代理"，但能被 httpx
解析。**只修解析不了的形状，其他条目一律不动。**

放在导入期而不是各个调用点：httpx 客户端散落在 LLM / 网页抓取 / 网页搜索 / MCP HTTP
多处，逐个传 `mounts` / `trust_env` 既容易漏，也会随新增调用点慢慢漂移。
"""

from __future__ import annotations

import ipaddress
import logging
import os

logger = logging.getLogger("routivus.http_env")

#: 大小写两种写法都可能存在（urllib 扫描时会统一小写匹配，但环境里常同时存在）。
_NO_PROXY_KEYS = ("no_proxy", "NO_PROXY")


def _is_ipv6_cidr(host: str) -> bool:
    """`::1/128` 这类"IPv6 + 前缀长度"。

    IPv4 的 CIDR（`127.0.0.0/8`）httpx 拼出来是 `all://127.0.0.0/8`，能正常解析，
    所以**不动它**——只处理确定会炸的 IPv6 形态。
    """
    if "/" not in host or ":" not in host:
        return False
    try:
        network = ipaddress.ip_network(host, strict=False)
    except ValueError:
        return False
    return isinstance(network.network_address, ipaddress.IPv6Address)


def normalize_no_proxy(value: str) -> tuple[str, list[str]]:
    """→ (修好的值, 被修过的原条目)。

    纯函数，便于单测；空条目会被丢掉（httpx 自己也跳过它们）。
    """
    kept: list[str] = []
    repaired: list[str] = []
    for raw in value.split(","):
        host = raw.strip()
        if not host:
            continue
        if _is_ipv6_cidr(host):
            repaired.append(host)
            kept.append(host.split("/", 1)[0])
        else:
            kept.append(host)
    return ",".join(kept), repaired


def sanitize_no_proxy_env() -> list[str]:
    """就地修好 `no_proxy` / `NO_PROXY`；返回被修过的条目（没改就是空表）。

    幂等：修完再调用不会重复告警。只在确实有坏条目时才写回环境变量并告警——
    正常环境下面这是一次只读检查。
    """
    repaired: list[str] = []
    for key in _NO_PROXY_KEYS:
        value = os.environ.get(key)
        if not value:
            continue
        fixed, changed = normalize_no_proxy(value)
        if not changed:
            continue
        os.environ[key] = fixed
        repaired.extend(changed)
    if repaired:
        logger.warning(
            "no_proxy 含 httpx 无法解析的 IPv6 CIDR 条目（%s），已退化为裸地址；"
            "否则所有 HTTP 请求都会以 Invalid port 失败",
            ", ".join(repaired),
        )
    return repaired
