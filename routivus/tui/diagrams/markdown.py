"""Recognize complete Mermaid fenced blocks without parsing Markdown globally."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class MermaidBlock:
    source: str
    start: int
    end: int


_FENCE_RE = re.compile(
    r"```[ \t]*mermaid[ \t]*\r?\n(?P<body>.*?)(?P<fence>```)[ \t]*(?:\r?\n|$)",
    re.IGNORECASE | re.DOTALL,
)


def split_mermaid_blocks(text: str) -> list[tuple[str, MermaidBlock | None]]:
    """Split text into ``("text", None)`` and complete Mermaid blocks.

    An unclosed fence is deliberately returned as ordinary text.  This keeps
    streaming assistant output visible as source until the fence is complete.
    """
    parts: list[tuple[str, MermaidBlock | None]] = []
    cursor = 0
    for match in _FENCE_RE.finditer(text):
        if match.start() > cursor:
            parts.append((text[cursor : match.start()], None))
        parts.append((match.group("body"), MermaidBlock(
            source=match.group("body"), start=match.start(), end=match.end()
        )))
        cursor = match.end()
    if cursor < len(text):
        parts.append((text[cursor:], None))
    return parts or [(text, None)]
