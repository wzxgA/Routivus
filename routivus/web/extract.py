"""Small dependency-free HTML article extractor.

It deliberately handles static documents only. Scripts, forms, navigation and
other page chrome are ignored; JavaScript is never executed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser


@dataclass
class HtmlNode:
    tag: str
    attrs: dict[str, str]
    children: list[object]


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = HtmlNode("root", {}, [])
        self.stack = [self.root]
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        node = HtmlNode(tag.lower(), {str(k).lower(): str(v or "") for k, v in attrs}, [])
        self.stack[-1].children.append(node)
        if tag.lower() not in {"meta", "link", "img", "br", "hr", "input", "source", "area", "base", "embed", "param", "track", "wbr"}:
            self.stack.append(node)
        if tag.lower() == "title":
            self._in_title = True

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in {"meta", "link", "img", "br", "hr", "input", "source", "area", "base", "embed", "param", "track", "wbr"} and len(self.stack) > 1:
            self.stack.pop()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        self.stack[-1].children.append(data)


DROP_TAGS = {"script", "style", "noscript", "template", "svg", "canvas", "nav", "header", "footer", "aside", "form", "button", "iframe", "広告"}
BLOCK_TAGS = {"article", "main", "section", "div", "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre", "tr"}


def _text(node: HtmlNode) -> str:
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        elif child.tag not in DROP_TAGS:
            parts.append(_text(child))
    return " ".join("".join(parts).split())


def _score(node: HtmlNode) -> int:
    if node.tag in DROP_TAGS:
        return -10_000
    own = len(_text(node))
    bonus = 1000 if node.tag in {"article", "main"} else 0
    return own + bonus


def parse_html(html: str) -> tuple[HtmlNode, str]:
    parser = _TreeParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # HTMLParser is forgiving, but malformed input should still produce a
        # safe partial result rather than escape into the caller.
        pass
    return parser.root, " ".join(parser.title.split())[:500]


def select_content(root: HtmlNode) -> HtmlNode:
    candidates: list[HtmlNode] = []
    def walk(node: HtmlNode):
        if node.tag not in DROP_TAGS:
            candidates.append(node)
            for child in node.children:
                if isinstance(child, HtmlNode):
                    walk(child)
    walk(root)
    preferred = [node for node in candidates if node.tag in {"article", "main"} and _text(node)]
    if preferred:
        return max(preferred, key=lambda node: len(_text(node)))
    return max(candidates, key=_score) if candidates else root


@dataclass(frozen=True)
class ExtractedContent:
    title: str
    node: HtmlNode
    text: str


def extract_html(html: str, *, max_chars: int = 32_000) -> ExtractedContent:
    root, title = parse_html(html)
    node = select_content(root)
    text = _text(node)
    return ExtractedContent(title, node, text[:max_chars])


def extract_main_content(html: str, max_chars: int = 32_000) -> ExtractedContent:
    return extract_html(html, max_chars=max_chars)


def extract_text(html: str, max_chars: int = 32_000) -> str:
    """Compatibility helper for callers that only need plain extracted text."""
    return extract_html(html, max_chars=max_chars).text
