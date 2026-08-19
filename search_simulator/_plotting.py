from __future__ import annotations

import logging
import os
import textwrap
from collections import defaultdict
from pathlib import Path
from typing import Callable

import matplotlib

matplotlib.use(os.environ.get("SEARCH_SIMULATOR_MPL_BACKEND", "Agg"), force=True)

import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.lines import Line2D

from ._game_state import GameState
from ._i18n import t, t_en

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

_LABEL_FONT_SIZE = 5.1
_LABEL_CHAR_WIDTH_INCHES = _LABEL_FONT_SIZE / 72.0 * 0.92
_LABEL_BBOX_PADDING_INCHES = 0.38
_HORIZONTAL_LABEL_PADDING_INCHES = 1.0


def draw_state_tree(
    *,
    state_parent_index: dict[int, int | None],
    state_action_index: dict[int, str],
    state_players_snapshot: dict[int, list[str]],
    endings: list[tuple[GameState, str]],
    build_state_path: Callable[[int], list[int]],
    max_nodes_for_plot: int,
    plot_dpi: int,
    output_path: Path | str = "search_tree.png",
) -> None:
    """根据搜索索引绘制状态树。"""

    if not state_parent_index:
        logger.info(t("log.plot_empty_index"))
        return
    plt.axis("off")
    terminal_state_ids = [state.state_id for state, _ in endings if state.state_id >= 0]
    if terminal_state_ids:
        plotted_nodes: set[int] = set()
        for state_id in terminal_state_ids:
            plotted_nodes.update(build_state_path(state_id))
    else:
        plotted_nodes = set(state_parent_index.keys())

    if not plotted_nodes:
        logger.info(t("log.plot_no_nodes"))
        return
    if len(plotted_nodes) > max_nodes_for_plot:
        logger.warning(
            t("log.plot_too_many", len(plotted_nodes), max_nodes_for_plot)
        )
        return

    children_map: dict[int, list[int]] = defaultdict(list)
    roots: list[int] = []
    for node_id in sorted(plotted_nodes):
        parent_id = state_parent_index.get(node_id)
        if parent_id is None or parent_id not in plotted_nodes:
            roots.append(node_id)
            continue
        children_map[parent_id].append(node_id)

    for node_id in children_map:
        children_map[node_id].sort()
    roots = sorted(set(roots))

    terminal_result_by_id = {state.state_id: result for state, result in endings}
    leaf_count = sum(1 for node_id in plotted_nodes if not children_map.get(node_id))
    label_by_node = _build_plot_labels(
        plotted_nodes=plotted_nodes,
        roots=set(roots),
        children_map=children_map,
        state_action_index=state_action_index,
        state_players_snapshot=state_players_snapshot,
        terminal_result_by_id=terminal_result_by_id,
    )
    max_label_line_count = max(
        (label.count("\n") + 1 for label in label_by_node.values()),
        default=1,
    )
    max_label_line_width = max(
        (len(line) for label in label_by_node.values() for line in label.splitlines()),
        default=12,
    )
    label_width_inches = _estimate_label_width_inches(max_label_line_width)
    leaf_gap = _compute_leaf_gap(label_width_inches)
    depth_gap = _compute_depth_gap(max_label_line_count)

    x_pos: dict[int, float] = {}
    y_pos: dict[int, float] = {}
    depth_by_node: dict[int, int] = {}
    next_leaf_x = 0.0
    max_depth = 0

    def assign_position(node_id: int, depth: int) -> float:
        nonlocal max_depth, next_leaf_x
        max_depth = max(max_depth, depth)
        depth_by_node[node_id] = depth
        y_pos[node_id] = float(depth) * depth_gap
        children = children_map.get(node_id, [])
        if not children:
            x_pos[node_id] = next_leaf_x
            next_leaf_x += leaf_gap
            return x_pos[node_id]

        child_xs = [assign_position(child_id, depth + 1) for child_id in children]
        x_pos[node_id] = sum(child_xs) / len(child_xs)
        return x_pos[node_id]

    for root_id in roots:
        assign_position(root_id, depth=0)
    _spread_nodes_by_depth(
        x_pos=x_pos,
        depth_by_node=depth_by_node,
        min_gap=leaf_gap,
    )
    render_order = _build_postorder_render_order(
        roots=roots,
        children_map=children_map,
    )

    node_color_by_id: dict[int, str] = {}
    for node_id in sorted(plotted_nodes):
        result = terminal_result_by_id.get(node_id)
        if result is None:
            node_color_by_id[node_id] = "#5B8FF9"
        elif "好人" in result:
            node_color_by_id[node_id] = "#52C41A"
        else:
            node_color_by_id[node_id] = "#F5222D"

    def resolve_plot_font() -> tuple[str, bool]:
        preferred_fonts = [
            "Microsoft YaHei",
            "SimHei",
            "Noto Sans CJK SC",
            "PingFang SC",
            "WenQuanYi Zen Hei",
            "Source Han Sans SC",
            "Arial Unicode MS",
        ]
        available_fonts = {font.name for font in font_manager.fontManager.ttflist}
        for font_name in preferred_fonts:
            if font_name in available_fonts:
                return font_name, True
        return "DejaVu Sans", False

    plot_font, has_cjk_font = resolve_plot_font()
    title_text = t("plot.title") if has_cjk_font else t_en("plot.title")
    intermediate_label = t("plot.intermediate") if has_cjk_font else t_en("plot.intermediate")
    village_win_label = t("plot.village_win") if has_cjk_font else t_en("plot.village_win")
    wolf_win_label = t("plot.wolf_win") if has_cjk_font else t_en("plot.wolf_win")

    max_x = max(x_pos.values()) if x_pos else 0.0
    min_x = min(x_pos.values()) if x_pos else 0.0
    max_y = max(y_pos.values()) if y_pos else 0.0
    fig_width, fig_height = _compute_figure_size(
        leaf_count=leaf_count,
        max_depth=max_depth,
        max_label_line_count=max_label_line_count,
        label_width_inches=label_width_inches,
        leaf_gap=leaf_gap,
        x_span=max_x - min_x,
    )
    dpi = min(max(plot_dpi, 72), 220)
    logger.info(
        t(
            "log.plot_size",
            fig_width,
            fig_height,
            leaf_count,
            leaf_gap,
            depth_gap,
        )
    )
    with plt.rc_context({"font.family": plot_font, "axes.unicode_minus": False}):
        fig, ax = plt.subplots(figsize=(fig_width, fig_height), constrained_layout=True)

        for node_id in render_order:
            parent_id = state_parent_index.get(node_id)
            if parent_id is None or parent_id not in plotted_nodes:
                continue
            ax.plot(
                [x_pos[parent_id], x_pos[node_id]],
                [y_pos[parent_id], y_pos[node_id]],
                color="#BFBFBF",
                linewidth=0.8,
                zorder=1,
            )

        ax.scatter(
            [x_pos[node_id] for node_id in render_order],
            [y_pos[node_id] for node_id in render_order],
            s=24,
            c=[node_color_by_id[node_id] for node_id in render_order],
            edgecolors="#333333",
            linewidths=0.3,
            zorder=2,
        )

        for node_id in render_order:
            node_text = label_by_node[node_id]
            ax.annotate(
                node_text,
                xy=(x_pos[node_id], y_pos[node_id]),
                xytext=(0, -18),
                textcoords="offset points",
                fontsize=_LABEL_FONT_SIZE,
                va="top",
                ha="center",
                color="#222222",
                linespacing=1.25,
                bbox={
                    "boxstyle": "round,pad=0.2",
                    "facecolor": "#FFFFFF",
                    "edgecolor": "#999999",
                    "linewidth": 0.5,
                    "alpha": 0.85,
                },
                zorder=3,
            )

        legend_handles = [
            Line2D(
                [0],
                [0],
                marker="o",
                color="w",
                markerfacecolor="#5B8FF9",
                markersize=6,
                label=intermediate_label,
            ),
            Line2D(
                [0],
                [0],
                marker="o",
                color="w",
                markerfacecolor="#52C41A",
                markersize=6,
                label=village_win_label,
            ),
            Line2D(
                [0],
                [0],
                marker="o",
                color="w",
                markerfacecolor="#F5222D",
                markersize=6,
                label=wolf_win_label,
            ),
        ]
        ax.legend(handles=legend_handles, loc="upper right", fontsize=8)

        ax.set_title(title_text, fontsize=12)
        axis_branch_text = t("plot.axis_branch") if has_cjk_font else t_en("plot.axis_branch")
        axis_depth_text = t("plot.axis_depth") if has_cjk_font else t_en("plot.axis_depth")
        ax.set_xlabel(axis_branch_text, fontsize=10)
        ax.set_ylabel(axis_depth_text, fontsize=10)
        ax.set_yticks([depth * depth_gap for depth in range(int(max_y / depth_gap) + 1)])
        ax.grid(axis="y", linestyle="--", linewidth=0.5, alpha=0.3)
        ax.invert_yaxis()
        x_axis_margin = max(leaf_gap * 0.5, label_width_inches * 0.75 + 0.5)
        ax.set_xlim(min_x - x_axis_margin, max_x + x_axis_margin)
        ax.set_ylim(max_y + depth_gap * 0.9, -depth_gap * 0.6)

        path = Path(output_path)
        fig.savefig(path, dpi=dpi)
        plt.close(fig)
    logger.info(t("log.plot_saved", output_path))


def _build_plot_labels(
    *,
    plotted_nodes: set[int],
    roots: set[int],
    children_map: dict[int, list[int]],
    state_action_index: dict[int, str],
    state_players_snapshot: dict[int, list[str]],
    terminal_result_by_id: dict[int, str],
) -> dict[int, str]:
    labels: dict[int, str] = {}
    for node_id in sorted(plotted_nodes):
        action_label = state_action_index.get(node_id, "")
        if node_id in roots:
            action_text = t("plot.root_action")
        else:
            action_text = _wrap_label_text(action_label, width=22, max_lines=4)
        statuses = state_players_snapshot.get(node_id, [])
        player_text = (
            _wrap_label_text(", ".join(statuses), width=28, max_lines=5)
            if statuses
            else t("plot.none")
        )
        if not children_map.get(node_id):
            result_text = terminal_result_by_id.get(node_id, t("plot.unfinished"))
            labels[node_id] = (
                f"#{node_id}\n{t('plot.action_label')}:\n{action_text}\n"
                f"{t('plot.alive_status')}:\n{player_text}\n"
                f"{t('plot.result_label')}:\n"
                f"{_wrap_label_text(result_text, width=24, max_lines=3)}"
            )
        else:
            labels[node_id] = (
                f"#{node_id}\n{t('plot.action_label')}:\n{action_text}\n"
                f"{t('plot.alive_status')}:\n{player_text}"
            )
    return labels


def _wrap_label_text(text: str, *, width: int, max_lines: int) -> str:
    normalized = text.strip() or "未知"
    wrapped_lines = textwrap.wrap(
        normalized,
        width=width,
        break_long_words=False,
        break_on_hyphens=False,
    )
    if not wrapped_lines:
        wrapped_lines = [normalized]
    if len(wrapped_lines) > max_lines:
        wrapped_lines = wrapped_lines[:max_lines]
        last_line = wrapped_lines[-1]
        wrapped_lines[-1] = (
            f"{last_line[: max(1, width - 3)]}..."
            if len(last_line) >= width
            else f"{last_line}..."
        )
    return "\n".join(wrapped_lines)


def _spread_nodes_by_depth(
    *,
    x_pos: dict[int, float],
    depth_by_node: dict[int, int],
    min_gap: float,
) -> None:
    nodes_by_depth: dict[int, list[int]] = defaultdict(list)
    for node_id, depth in depth_by_node.items():
        nodes_by_depth[depth].append(node_id)

    for node_ids in nodes_by_depth.values():
        ordered_node_ids = sorted(node_ids, key=lambda node_id: (x_pos[node_id], node_id))
        next_allowed_x: float | None = None
        for node_id in ordered_node_ids:
            current_x = x_pos[node_id]
            if next_allowed_x is not None and current_x < next_allowed_x:
                current_x = next_allowed_x
                x_pos[node_id] = current_x
            next_allowed_x = current_x + min_gap


def _build_postorder_render_order(
    *,
    roots: list[int],
    children_map: dict[int, list[int]],
) -> list[int]:
    render_order: list[int] = []

    def visit(node_id: int) -> None:
        for child_id in children_map.get(node_id, []):
            visit(child_id)
        render_order.append(node_id)

    for root_id in roots:
        visit(root_id)
    return render_order


def _estimate_label_width_inches(max_label_line_width: int) -> float:
    content_width = max_label_line_width * _LABEL_CHAR_WIDTH_INCHES
    return max(2.8, min(8.0, content_width + _LABEL_BBOX_PADDING_INCHES))


def _compute_leaf_gap(label_width_inches: float) -> float:
    return label_width_inches + _HORIZONTAL_LABEL_PADDING_INCHES


def _compute_depth_gap(max_label_line_count: int) -> float:
    return max(4.2, max_label_line_count * 0.45 + 2.2)


def _compute_figure_size(
    *,
    leaf_count: int,
    max_depth: int,
    max_label_line_count: int,
    label_width_inches: float,
    leaf_gap: float,
    x_span: float,
) -> tuple[float, float]:
    label_height_inches = max(1.5, min(4.8, max_label_line_count * 0.23 + 0.8))
    horizontal_margin_inches = max(2.5, label_width_inches * 0.75 + 1.0)
    layout_width = max(leaf_count * leaf_gap, x_span)
    width = max(14.0, layout_width + horizontal_margin_inches * 2)
    height = max(10.0, (max_depth + 1) * (label_height_inches + 1.5) + 4.0)
    return width, height


def draw_reference_path(trace, *, output_path: Path | str = "online_path.png", plot_dpi: int = 140) -> None:
    """绘制在线参考路径：每步的 reward 区间（竖直误差条）。"""
    steps = trace.get("steps") or []
    if not steps:
        logger.info(t("log.ref_path_empty"))
        return

    x = list(range(1, len(steps) + 1))
    lows = [float(s["chosen_interval"][0]) for s in steps]
    highs = [float(s["chosen_interval"][1]) for s in steps]
    mids = [(lo + hi) / 2.0 for lo, hi in zip(lows, highs)]

    preferred_fonts = [
        "Microsoft YaHei",
        "SimHei",
        "Noto Sans CJK SC",
        "Arial Unicode MS",
    ]
    available_fonts = {font.name for font in font_manager.fontManager.ttflist}
    plot_font = next((f for f in preferred_fonts if f in available_fonts), "DejaVu Sans")
    has_cjk = plot_font in preferred_fonts
    title = t("plot.ref_title") if has_cjk else t_en("plot.ref_title")
    ylabel = t("plot.ref_ylabel") if has_cjk else t_en("plot.ref_ylabel")

    with plt.rc_context({"font.family": plot_font, "axes.unicode_minus": False}):
        fig, ax = plt.subplots(figsize=(max(8.0, len(steps) * 1.4), 5.5))
        ax.vlines(x, lows, highs, color="#5B8FF9", linewidth=2.5, zorder=2)
        ax.scatter(x, mids, color="#5B8FF9", s=32, zorder=3)
        ax.axhline(0.0, color="#cccccc", linewidth=0.8)
        ax.set_xticks(x)
        ax.set_xticklabels(
            [f"#{s['step']}\n{s['chosen_action'][:14]}" for s in steps], fontsize=6.5
        )
        ax.set_ylim(-1.08, 1.08)
        ax.set_ylabel(ylabel)
        ax.set_title(f"{title}  outcome={trace.get('outcome')}")
        ax.grid(axis="y", linestyle="--", alpha=0.3)
        fig.tight_layout()
        fig.savefig(Path(output_path), dpi=min(max(plot_dpi, 72), 220))
        plt.close(fig)
    logger.info(t("log.ref_path_saved", output_path))
