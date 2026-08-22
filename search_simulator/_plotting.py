"""持久化状态图绘制。"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

from ._i18n import t
from ._interval import RewardInterval
from ._interval import interval_branch_color

logger = logging.getLogger(__name__)


def _positions_by_round(
    nodes: list[dict[str, Any]],
) -> dict[int, tuple[float, float]]:
    by_round: dict[int, list[int]] = defaultdict(list)
    for node in nodes:
        round_index = int(node["day_count"]) + int(node["night_count"])
        by_round[round_index].append(int(node["node_id"]))
    positions: dict[int, tuple[float, float]] = {}
    for round_index, node_ids in sorted(by_round.items()):
        count = len(node_ids)
        for offset, node_id in enumerate(sorted(node_ids)):
            centered = offset - (count - 1) / 2
            positions[node_id] = (centered, -float(round_index))
    return positions


def draw_position_graph(
    *,
    graph: dict[str, list[dict[str, Any]]],
    position_index: int,
    output_path: Path | str = "position_tree.png",
    max_nodes_for_plot: int = 2500,
    plot_dpi: int = 140,
) -> Path | None:
    """绘制一个站位的 DAG；节点上限只影响画图，不影响迭代。"""

    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    if not nodes:
        logger.info(t("log.position_missing", position_index))
        return None
    if len(nodes) > max_nodes_for_plot:
        logger.warning(t("log.plot_too_many", position_index, len(nodes), max_nodes_for_plot))
        return None

    import matplotlib

    matplotlib.rcParams["font.sans-serif"] = [
        "Microsoft YaHei",
        "SimHei",
        "Arial Unicode MS",
        "DejaVu Sans",
    ]
    matplotlib.rcParams["axes.unicode_minus"] = False
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    positions = _positions_by_round(nodes)
    rounds = max(int(node["day_count"]) + int(node["night_count"]) for node in nodes)
    width = min(32.0, max(10.0, len(nodes) ** 0.5 * 0.7))
    height = min(28.0, max(7.0, (rounds + 1) * 1.8))
    figure, axis = plt.subplots(figsize=(width, height))

    for edge in edges:
        parent = positions.get(int(edge["parent_id"]))
        child = positions.get(int(edge["child_id"]))
        if parent is None or child is None:
            continue
        lower, upper = edge["wide_interval"]
        color = interval_branch_color(RewardInterval(float(lower), float(upper)))
        multiplicity = max(1, int(edge.get("multiplicity", 1)))
        line_width = min(2.4, 0.45 + multiplicity.bit_length() * 0.08)
        axis.plot(
            [parent[0], child[0]],
            [parent[1], child[1]],
            color=color,
            linewidth=line_width,
            alpha=0.65,
            zorder=1,
        )
        reasons = edge.get("reasons") or []
        reason_text = "\n".join(
            str(reason.get("action_label", reason.get("action_key", "")))
            for reason in reasons
        ) or str(edge.get("action_label", ""))
        if reason_text:
            axis.text(
                (parent[0] + child[0]) / 2.0,
                (parent[1] + child[1]) / 2.0,
                reason_text,
                color=color,
                fontsize=5,
                alpha=0.85,
                ha="center",
                va="center",
                zorder=3,
            )

    x_values: list[float] = []
    y_values: list[float] = []
    node_colors: list[str] = []
    for node in nodes:
        x, y = positions[int(node["node_id"])]
        x_values.append(x)
        y_values.append(y)
        if node["is_terminal"]:
            node_colors.append("#60A5FA" if "好人" in str(node["result"]) else "#F87171")
        else:
            node_colors.append("#E5E7EB")
    axis.scatter(
        x_values,
        y_values,
        s=20,
        c=node_colors,
        edgecolors="#374151",
        linewidths=0.35,
        zorder=2,
    )
    axis.set_title(t("plot.title", index=position_index))
    axis.set_xlabel(t("plot.axis_order"))
    axis.set_ylabel(t("plot.axis_depth"))
    axis.grid(axis="y", alpha=0.18)
    axis.legend(
        handles=[
            Line2D([0], [0], color="#2563EB", label=t("plot.good")),
            Line2D([0], [0], color="#DC2626", label=t("plot.wolf")),
            Line2D([0], [0], color="#111111", label=t("plot.balanced")),
        ],
        loc="best",
        fontsize=8,
    )
    figure.tight_layout()
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path, dpi=plot_dpi, bbox_inches="tight")
    plt.close(figure)
    logger.info(t("log.plot_saved", path))
    return path
