"""URL and DNS policy used by every web fetch redirect hop."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from routivus.web.errors import WebSecurityError, WebInputError


BLOCKED_HOSTS = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


def _blocked_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


@dataclass(frozen=True)
class ValidatedURL:
    url: str
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...] = ()


class URLPolicy:
    def __init__(self, *, allowed_ports: tuple[int, ...] = (80, 443),
                 resolver=None, resolve_dns: bool = True) -> None:
        self.allowed_ports = tuple(allowed_ports)
        self.resolver = resolver or socket.getaddrinfo
        self.resolve_dns = resolve_dns

    def validate(self, url: str) -> ValidatedURL:
        if not isinstance(url, str) or len(url) > 4096:
            raise WebInputError("URL 为空或过长")
        try:
            parts = urlsplit(url.strip())
        except ValueError as exc:
            raise WebInputError("URL 格式不正确") from exc
        if parts.scheme.lower() not in {"http", "https"}:
            raise WebSecurityError("只允许 http 和 https scheme")
        if not parts.hostname or parts.username is not None or parts.password is not None:
            raise WebSecurityError("URL 必须是公开 HTTP 地址，不能包含用户信息")
        host = parts.hostname.rstrip(".").lower()
        if host in BLOCKED_HOSTS or host.endswith(".localhost"):
            raise WebSecurityError("禁止访问 localhost")
        try:
            port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
        except ValueError as exc:
            raise WebInputError("URL 端口不合法") from exc
        if port not in self.allowed_ports:
            raise WebSecurityError(f"不允许访问端口 {port}")
        addresses: list[str] = []
        if _blocked_ip(host):
            raise WebSecurityError("禁止访问本地、内网或保留 IP 地址")
        try:
            ipaddress.ip_address(host)
            addresses = [host]
        except ValueError:
            if self.resolve_dns:
                try:
                    records = self.resolver(host, port, type=socket.SOCK_STREAM)
                except OSError as exc:
                    raise WebSecurityError("域名 DNS 解析失败") from exc
                for record in records:
                    address = record[4][0]
                    addresses.append(address)
                    if _blocked_ip(address):
                        raise WebSecurityError("域名解析到了本地、内网或保留 IP 地址")
                if not addresses:
                    raise WebSecurityError("域名没有可用地址")
        # SplitResult._replace cannot replace hostname (derived field), so use a
        # safe normalized authority without userinfo.
        authority = host if (":" not in host or host.startswith("[")) else f"[{host}]"
        if port != (443 if parts.scheme.lower() == "https" else 80):
            authority += f":{port}"
        normalized_url = f"{parts.scheme.lower()}://{authority}{parts.path or '/'}"
        if parts.query:
            normalized_url += f"?{parts.query}"
        return ValidatedURL(normalized_url, parts.scheme.lower(), host, port, tuple(addresses))

    def validate_redirect(self, current: str, location: str) -> ValidatedURL:
        target = urljoin(current, location)
        return self.validate(target)

    async def avalidate(self, url: str) -> ValidatedURL:
        return await asyncio.to_thread(self.validate, url)

    async def avalidate_redirect(self, current: str, location: str) -> ValidatedURL:
        return await asyncio.to_thread(self.validate_redirect, current, location)


def validate_url(url: str, **kwargs) -> ValidatedURL:
    return URLPolicy(**kwargs).validate(url)


def is_safe_url(url: str, **kwargs) -> bool:
    try:
        validate_url(url, **kwargs)
        return True
    except (WebInputError, WebSecurityError, ValueError):
        return False
