"""MCP resource decoding and explicit @server:uri reference parsing."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


_REFERENCE_RE = re.compile(
    r"@(?P<server>[A-Za-z0-9_-]{1,32}):(?P<uri>[^\s，。；！？,;!?\)\]\}]+)"
)
_TRAILING = ".,;!?，。；！？)]}"


@dataclass(frozen=True)
class ResourceReference:
    server: str
    uri: str
    token: str


def find_resource_references(text: str, limit: int = 8) -> list[ResourceReference]:
    found: list[ResourceReference] = []
    for match in _REFERENCE_RE.finditer(text):
        uri = match.group("uri").rstrip(_TRAILING)
        if not uri or ":" not in uri:
            continue
        token = f"@{match.group('server')}:{uri}"
        found.append(ResourceReference(match.group("server"), uri, token))
        if len(found) >= limit:
            break
    return found


def decode_resource_contents(result: dict, max_chars: int) -> tuple[str, bool]:
    contents = result.get("contents", [])
    if not isinstance(contents, list):
        return "", False
    parts: list[str] = []
    binary_seen = False
    for item in contents:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif isinstance(item.get("blob"), str):
            binary_seen = True
            try:
                size = len(base64.b64decode(item["blob"], validate=False))
            except Exception:
                size = len(item["blob"])
            parts.append(f"[二进制 MCP resource，约 {size} bytes，未内联]")
    text = "\n\n".join(parts)
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars] + f"\n... (资源已截断，原始长度 {len(text)} 字符)"
    return text, truncated or binary_seen


def redact_uri(uri: str) -> str:
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return uri.split("?", 1)[0]
    if not parsed.scheme:
        return uri.split("?", 1)[0]
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
