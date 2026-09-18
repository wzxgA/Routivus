"""`no_proxy` 自检的回归测试（见 `routivus/http_env.py`）。

背景：`no_proxy` 里带 CIDR 前缀的 IPv6（`::1/128`）会让 httpx 拼出
`all://[::1/128]`，`URLPattern` 直接抛 `InvalidURL: Invalid port: ':1'` —— 失败点是
`AsyncClient.__init__`，于是**所有** HTTP 请求都发不出去，报错还指向一个用户从没配过的
端口。这类值由代理 / VPN 工具写进系统，用户无感，所以必须在进程里自愈。

注意 Windows 的 `os.environ` 会把键名归一成大写，`no_proxy` 与 `NO_PROXY` 是**同一个**
条目，所以这里的用例只用一种写法，不去假造"两个键同时存在"。
"""

from __future__ import annotations

import os

import httpx
import pytest

from routivus.http_env import normalize_no_proxy, sanitize_no_proxy_env

POISONED = "127.0.0.1,localhost,::1,127.0.0.0/8,::1/128"
HEALED = "127.0.0.1,localhost,::1,127.0.0.0/8,::1"


def test_normalize_repairs_ipv6_cidr_only() -> None:
    fixed, repaired = normalize_no_proxy(POISONED)

    assert repaired == ["::1/128"]
    assert fixed == HEALED
    # 语义不变：::1 仍然"不走代理"，只是换成了 httpx 认得出来的写法


@pytest.mark.parametrize(
    "value",
    [
        "127.0.0.1,localhost",  # 普通条目
        "127.0.0.0/8,10.0.0.0/8",  # IPv4 CIDR：httpx 解析得了，**不能动**
        "::1",  # 裸 IPv6：httpx 会自己补方括号
        "*.internal.example.com,.example.com",
        "  ",  # 空值
    ],
)
def test_normalize_leaves_healthy_values_untouched(value: str) -> None:
    fixed, repaired = normalize_no_proxy(value)
    assert repaired == []
    assert fixed == ",".join(part.strip() for part in value.split(",") if part.strip())


def test_normalize_drops_blank_entries() -> None:
    fixed, repaired = normalize_no_proxy("localhost,,  ,::1/128,")
    assert fixed == "localhost,::1"
    assert repaired == ["::1/128"]


def test_sanitize_writes_back_and_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("no_proxy", POISONED)

    assert sanitize_no_proxy_env() == ["::1/128"]
    assert os.environ["no_proxy"] == HEALED
    # 幂等：第二次调用既不改也不再告警
    assert sanitize_no_proxy_env() == []
    assert os.environ["no_proxy"] == HEALED


def test_sanitize_is_read_only_on_healthy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    assert sanitize_no_proxy_env() == []
    assert os.environ["no_proxy"] == "127.0.0.1,localhost"


async def test_httpx_client_constructs_after_sanitize(monkeypatch: pytest.MonkeyPatch) -> None:
    """用户可见的回归点：修好之后 `AsyncClient()` 能构造出来（否则所有 LLM 调用都挂）。"""
    monkeypatch.setenv("no_proxy", POISONED)

    sanitize_no_proxy_env()
    async with httpx.AsyncClient(timeout=1.0) as client:
        assert client is not None
