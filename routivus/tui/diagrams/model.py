"""Pure data structures for the supported Mermaid flowchart subset."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


Direction = Literal["TD", "TB", "BT", "LR", "RL"]
NodeShape = Literal["rect", "round", "diamond", "circle"]
EdgeStyle = Literal["solid", "dotted", "thick"]


@dataclass
class FlowchartNode:
    id: str
    label: str
    shape: NodeShape = "rect"


@dataclass
class FlowchartEdge:
    source: str
    target: str
    label: str = ""
    style: EdgeStyle = "solid"
    arrow: bool = True


@dataclass
class FlowchartModel:
    direction: Direction = "TD"
    nodes: list[FlowchartNode] = field(default_factory=list)
    edges: list[FlowchartEdge] = field(default_factory=list)
    source: str = ""
    warnings: list[str] = field(default_factory=list)
