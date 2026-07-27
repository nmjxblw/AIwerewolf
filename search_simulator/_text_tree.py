from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path
from typing import Callable

from ._game_state import GameState

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def export_text_state_tree(
    *,
    state_parent_index: dict[int, int | None],
    state_action_index: dict[int, str],
    state_players_snapshot: dict[int, list[str]],
    endings: list[tuple[GameState, str]],
    build_state_path: Callable[[int], list[int]],
    max_text_tree_nodes: int,
    output_path: Path | str = "search_tree.txt",
) -> None:
    """导出类似 tree 命令的文本状态树。"""

    if not state_parent_index:
        logger.info("状态索引为空，跳过文本树导出")
        return

    terminal_state_ids = [state.state_id for state, _ in endings if state.state_id >= 0]
    if terminal_state_ids:
        tree_nodes: set[int] = set()
        for state_id in terminal_state_ids:
            tree_nodes.update(build_state_path(state_id))
    else:
        tree_nodes = set(state_parent_index.keys())

    if not tree_nodes:
        logger.info("没有可导出的文本树节点，跳过文本树导出")
        return
    if len(tree_nodes) > max_text_tree_nodes:
        logger.warning(
            "文本树节点数为 %s，超过阈值 %s，跳过文本树导出",
            len(tree_nodes),
            max_text_tree_nodes,
        )
        return

    children_map: dict[int, list[int]] = defaultdict(list)
    roots: list[int] = []
    for node_id in sorted(tree_nodes):
        parent_id = state_parent_index.get(node_id)
        if parent_id is None or parent_id not in tree_nodes:
            roots.append(node_id)
            continue
        children_map[parent_id].append(node_id)

    for children in children_map.values():
        children.sort()
    roots = sorted(set(roots))
    terminal_result_by_id = {state.state_id: result for state, result in endings}

    lines: list[str] = [
        "search_tree",
        f"nodes: {len(tree_nodes)}",
        "",
    ]
    for root_index, root_id in enumerate(roots):
        if root_index > 0:
            lines.append("")
        _append_node_lines(
            lines=lines,
            node_id=root_id,
            prefix="",
            is_last=root_index == len(roots) - 1,
            is_root=True,
            children_map=children_map,
            state_action_index=state_action_index,
            state_players_snapshot=state_players_snapshot,
            terminal_result_by_id=terminal_result_by_id,
        )

    path = Path(output_path)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("文本状态树已保存到: %s", path)


def _append_node_lines(
    *,
    lines: list[str],
    node_id: int,
    prefix: str,
    is_last: bool,
    is_root: bool,
    children_map: dict[int, list[int]],
    state_action_index: dict[int, str],
    state_players_snapshot: dict[int, list[str]],
    terminal_result_by_id: dict[int, str],
) -> None:
    connector = "" if is_root else ("`-- " if is_last else "|-- ")
    lines.append(f"{prefix}{connector}{_format_node_label(node_id, state_action_index, state_players_snapshot, terminal_result_by_id)}")

    child_prefix = prefix if is_root else prefix + ("    " if is_last else "|   ")
    children = children_map.get(node_id, [])
    for child_index, child_id in enumerate(children):
        _append_node_lines(
            lines=lines,
            node_id=child_id,
            prefix=child_prefix,
            is_last=child_index == len(children) - 1,
            is_root=False,
            children_map=children_map,
            state_action_index=state_action_index,
            state_players_snapshot=state_players_snapshot,
            terminal_result_by_id=terminal_result_by_id,
        )


def _format_node_label(
    node_id: int,
    state_action_index: dict[int, str],
    state_players_snapshot: dict[int, list[str]],
    terminal_result_by_id: dict[int, str],
) -> str:
    action_label = state_action_index.get(node_id, "未知").replace("\n", " ").strip()
    player_status = ", ".join(state_players_snapshot.get(node_id, [])) or "无"
    result = terminal_result_by_id.get(node_id)
    parts = [
        f"#{node_id}",
        f"action={action_label}",
        f"alive={player_status}",
    ]
    if result is not None:
        parts.append(f"result={result}")
    return " | ".join(parts)
