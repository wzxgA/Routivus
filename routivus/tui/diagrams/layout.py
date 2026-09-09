"""Stable, bounded layout for the supported Mermaid flowchart model."""

from __future__ import annotations

from dataclasses import dataclass

from routivus.tui.diagrams.model import FlowchartModel


@dataclass(frozen=True)
class LayoutNode:
    """A node position in a rank/row layout."""

    node_id: str
    rank: int
    order: int


@dataclass(frozen=True)
class FlowchartLayout:
    """Deterministic placement metadata used by the text renderer."""

    nodes: tuple[LayoutNode, ...]
    ranks: tuple[tuple[str, ...], ...]
    cyclic: bool = False

    def position(self, node_id: str) -> LayoutNode:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise KeyError(node_id)


def layout_flowchart(
    model: FlowchartModel,
    *,
    max_iterations: int | None = None,
    rank_by_node: dict[str, int] | None = None,
) -> FlowchartLayout:
    """Assign nodes to stable layers without an unbounded graph traversal.

    DAGs use the longest known predecessor rank.  For cyclic graphs the same
    relaxation is capped at ``node_count`` iterations and the remaining nodes
    keep their stable source order.  This is deliberately small and
    predictable rather than a full Mermaid-compatible layout engine.
    """
    node_ids = [node.id for node in model.nodes]
    index = {node_id: i for i, node_id in enumerate(node_ids)}
    incoming: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for edge in model.edges:
        if edge.source in incoming and edge.target in incoming:
            incoming[edge.target].append(edge.source)

    limit = max_iterations if max_iterations is not None else max(1, len(node_ids))
    ranks = {node_id: 0 for node_id in node_ids}
    if rank_by_node is not None:
        # Callers such as Plan already have a business-level batch partition.
        # Honor it while retaining a safe default for omitted nodes.
        ranks.update({node_id: rank for node_id, rank in rank_by_node.items() if node_id in ranks and rank >= 0})
        grouped: dict[int, list[str]] = {}
        for node_id in node_ids:
            grouped.setdefault(ranks[node_id], []).append(node_id)
        ordered_ranks = sorted(grouped)
        if model.direction in ("BT", "RL"):
            ordered_ranks.reverse()
        rank_lists = [tuple(grouped[rank]) for rank in ordered_ranks]
        positions = tuple(
            LayoutNode(node_id=node_id, rank=rank_no, order=order)
            for rank_no, rank_nodes in enumerate(rank_lists)
            for order, node_id in enumerate(rank_nodes)
        )
        return FlowchartLayout(nodes=positions, ranks=tuple(rank_lists), cyclic=False)

    changed = False
    for _ in range(limit):
        changed = False
        for node_id in node_ids:
            predecessors = incoming[node_id]
            if not predecessors:
                continue
            candidate = max(ranks[pred] + 1 for pred in predecessors)
            if candidate > ranks[node_id]:
                ranks[node_id] = candidate
                changed = True
        if not changed:
            break

    cyclic = changed
    # A cycle can make the relaxation increase every rank on every pass. Keep
    # it bounded and collapse the inflated ranks to a useful stable layout.
    if cyclic:
        ranks = {node_id: 0 for node_id in node_ids}
        for node_id in node_ids:
            predecessors = [p for p in incoming[node_id] if index[p] < index[node_id]]
            ranks[node_id] = min(1, max((ranks[p] + 1 for p in predecessors), default=0))

    grouped: dict[int, list[str]] = {}
    for node_id in node_ids:
        grouped.setdefault(ranks[node_id], []).append(node_id)
    ordered_ranks = sorted(grouped)
    if model.direction in ("BT", "RL"):
        ordered_ranks.reverse()

    rank_lists = [tuple(grouped[rank]) for rank in ordered_ranks]
    positions = tuple(
        LayoutNode(node_id=node_id, rank=rank_no, order=order)
        for rank_no, rank_nodes in enumerate(rank_lists)
        for order, node_id in enumerate(rank_nodes)
    )
    return FlowchartLayout(nodes=positions, ranks=tuple(rank_lists), cyclic=cyclic)
