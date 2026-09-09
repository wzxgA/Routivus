"""Small, safe parser for the common Mermaid flowchart syntax."""

from __future__ import annotations

import html
import re

from routivus.tui.diagrams.model import FlowchartEdge, FlowchartModel, FlowchartNode


class FlowchartParseError(ValueError):
    """The source is not a supported flowchart subset."""


_HEADER_RE = re.compile(r"^(?:flowchart|graph)(?:\s+(TD|TB|BT|LR|RL))?\s*$", re.IGNORECASE)
_NODE_RE = re.compile(
    r"\s*(?P<id>[A-Za-z_][\w-]*)"
    r"(?P<body>\(\([^\n()]*\)\)|\[[^\n\]]*\]|\([^\n()]*\)|\{[^\n}]*\}|>[^\n\]]*\])?"
)
_PIPE_LABEL_RE = re.compile(r"\|(?P<label>[^|]*)\|")
_EDGE_OPS = ("-.->", "==>", "-->", "---")


def _clean_label(value: str) -> str:
    value = html.unescape(value.replace("<br/>", "\n").replace("<br>", "\n"))
    value = re.sub(r"<[^>]+>", "", value)
    return value.strip()


def _node_parts(node_id: str, body: str | None) -> tuple[str, str]:
    if not body:
        return node_id, "rect"
    if body.startswith("(("):
        return _clean_label(body[2:-2]) or node_id, "circle"
    if body.startswith("["):
        return _clean_label(body[1:-1]) or node_id, "rect"
    if body.startswith("("):
        return _clean_label(body[1:-1]) or node_id, "round"
    if body.startswith("{"):
        return _clean_label(body[1:-1]) or node_id, "diamond"
    if body.startswith(">"):  # Mermaid flag shape: >label]
        return _clean_label(body[1:-1]) or node_id, "rect"
    return node_id, "rect"


def parse_flowchart(source: str, *, max_nodes: int = 80, max_edges: int = 160, max_label_chars: int = 120) -> FlowchartModel:
    """Parse a bounded subset of ``flowchart`` / ``graph`` Mermaid syntax."""
    lines = [line.strip() for line in source.splitlines() if line.strip() and not line.lstrip().startswith("%%")]
    if not lines:
        raise FlowchartParseError("flowchart 内容为空")
    header = _HEADER_RE.fullmatch(lines[0])
    if not header:
        raise FlowchartParseError("只支持 flowchart/graph 语法及 TD/TB/BT/LR/RL 方向")
    direction = (header.group(1) or "TD").upper()
    model = FlowchartModel(direction=direction, source=source)
    nodes: dict[str, FlowchartNode] = {}

    def ensure_node(node_id: str, body: str | None) -> None:
        label, shape = _node_parts(node_id, body)
        if len(label) > max_label_chars:
            label = label[:max_label_chars] + "…"
            model.warnings.append(f"节点 {node_id} 标签已截断")
        existing = nodes.get(node_id)
        if existing is None:
            if len(nodes) >= max_nodes:
                raise FlowchartParseError(f"节点数超过上限 {max_nodes}")
            nodes[node_id] = FlowchartNode(id=node_id, label=label, shape=shape)  # type: ignore[arg-type]
        elif body:
            existing.label = label
            existing.shape = shape  # type: ignore[assignment]

    def parse_node(text: str, pos: int) -> tuple[str, str | None, int]:
        match = _NODE_RE.match(text, pos)
        if not match:
            raise FlowchartParseError(f"无法解析节点：{text[pos:].strip()[:80]}")
        return match.group("id"), match.group("body"), match.end()

    for line_no, line in enumerate(lines[1:], 2):
        if line.lower().startswith("subgraph") or line.lower().startswith("end"):
            raise FlowchartParseError(f"第 {line_no} 行不支持 subgraph")
        for statement in (part.strip() for part in line.split(";") if part.strip()):
            pos = 0
            source_id, source_body, pos = parse_node(statement, pos)
            ensure_node(source_id, source_body)
            while pos < len(statement):
                rest = statement[pos:].lstrip()
                pos = len(statement) - len(rest)
                label = ""
                style = "solid"
                op = next((candidate for candidate in _EDGE_OPS if rest.startswith(candidate)), None)
                if op is not None:
                    pos += len(op)
                    style = "dotted" if op == "-.->" else "thick" if op == "==>" else "solid"
                    label_match = _PIPE_LABEL_RE.match(statement, pos)
                    if label_match:
                        label = _clean_label(label_match.group("label"))
                        pos = label_match.end()
                elif rest.startswith("--"):
                    # Support the readable form: A -- label --> B.
                    end = rest.find("-->", 2)
                    if end < 0:
                        raise FlowchartParseError(f"第 {line_no} 行的连线不完整")
                    label = _clean_label(rest[2:end])
                    pos += end + 3
                else:
                    raise FlowchartParseError(f"第 {line_no} 行存在不支持的语法：{rest[:80]}")
                target_id, target_body, pos = parse_node(statement, pos)
                ensure_node(target_id, target_body)
                if len(model.edges) >= max_edges:
                    raise FlowchartParseError(f"连线数超过上限 {max_edges}")
                model.edges.append(FlowchartEdge(
                    source=source_id, target=target_id, label=label,
                    style=style, arrow=True,  # type: ignore[arg-type]
                ))
                source_id = target_id
    model.nodes = list(nodes.values())
    if not model.nodes:
        raise FlowchartParseError("没有找到节点")
    return model
