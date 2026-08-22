"""BFS/DFS 状态 DAG 构建与 wide/narrow interval 搜索后回传。"""

from __future__ import annotations

import multiprocessing
import sys
import threading
import time
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from typing import Any
from typing import Iterable

from ._config import PERSISTENCE_BATCH_SIZE
from ._game_state import GameState
from ._game_state import game_state_dict_from_compact
from ._interval import UNRESOLVED
from ._interval import RewardInterval
from ._interval import RobustIntervals
from ._interval import _IntervalValueAccumulator
from ._interval import interval_camp
from ._interval import propagate_intervals
from ._memory_guard import memory_pressure_snapshot
from ._positions import PositionLayout
from ._positions import enumerate_position_layouts
from ._positions import position_signature

PREVIEW_NODE_BATCH_LIMIT = 48
PREVIEW_EDGE_BATCH_LIMIT = 72
PREVIEW_EMIT_INTERVAL_SECONDS = 0.5
MEMORY_CHECK_INTERVAL_SECONDS = 0.5


class MemoryPressureInterrupt(RuntimeError):
    """系统内存进入安全保留区，要求以可恢复检查点停止。"""


def _publish_search_progress(simulator: Any, event: dict[str, Any]) -> None:
    """将节点级进度发送给 GUI；无共享队列时退化为本地回调。"""

    progress_queue = getattr(simulator, "progress_queue", None)
    if progress_queue is not None:
        progress_queue.put(event)
        return
    callback = getattr(simulator, "iteration_callback", None)
    if callback is not None:
        callback(event)


def _wait_until_resumed(simulator: Any) -> None:
    """暂停时阻止展开下一节点，同时完整保留 frontier 和 DAG。"""

    resume_event = getattr(simulator, "resume_event", None)
    if resume_event is not None:
        resume_event.wait()


def _state_observation(
    state: GameState,
    *,
    parent_node_id: int | None,
    depth: int,
    action_label: str,
) -> tuple[Any, ...]:
    """保留不参与转移签名、但完整 GameState 检视需要的紧凑字段。"""

    return (
        tuple(sorted(state.last_day_votes.items())),
        state.last_day_strategy,
        parent_node_id,
        depth,
        action_label,
    )


def _node_intervals(node: dict[str, Any]) -> RobustIntervals:
    return RobustIntervals(
        wide=RewardInterval(*node["wide_interval"]),
        narrow=RewardInterval(*node["narrow_interval"]),
    )


def recompute_graph_intervals(
    graph: dict[str, list[dict[str, Any]]],
    *,
    lambda_risk: float,
    outgoing_edge_indices: dict[int, list[int]] | None = None,
    reverse_node_ids: Iterable[int] | None = None,
) -> RobustIntervals:
    """以流式常数额外空间在固定 DAG 上回传 interval。"""

    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    if not nodes:
        return UNRESOLVED
    sequential_node_ids = True
    for index, node in enumerate(nodes):
        if int(node["node_id"]) != index:
            sequential_node_ids = False
            break
    node_by_id = (
        None
        if sequential_node_ids
        else {int(node["node_id"]): node for node in nodes}
    )

    def node_for(node_id: int) -> dict[str, Any]:
        if node_by_id is None:
            return nodes[node_id]
        return node_by_id[node_id]
    children_by_parent: dict[int, dict[int, None]] | None = None
    if outgoing_edge_indices is None:
        children_by_parent = {}
        for edge in edges:
            parent_id = int(edge["parent_id"])
            child_id = int(edge["child_id"])
            children_by_parent.setdefault(parent_id, {}).setdefault(child_id, None)
    if reverse_node_ids is None:
        ordered_node_ids: Iterable[int] = [int(node["node_id"]) for node in nodes]
        ordered_node_ids.sort(
            key=lambda item: (
                int(node_for(item)["day_count"])
                + int(node_for(item)["night_count"]),
                item,
            ),
            reverse=True,
        )
    else:
        ordered_node_ids = reverse_node_ids
    for node_id in ordered_node_ids:
        node = node_for(node_id)
        if node["is_terminal"]:
            if "好人" in str(node["result"]):
                values = (1.0, 1.0, 1.0, 1.0)
            elif "狼人" in str(node["result"]):
                values = (-1.0, -1.0, -1.0, -1.0)
            else:
                values = (-1.0, 1.0, -1.0, 1.0)
        else:
            accumulator = _IntervalValueAccumulator()
            if outgoing_edge_indices is not None:
                for edge_index in outgoing_edge_indices.get(node_id, []):
                    child_id = int(edges[edge_index]["child_id"])
                    child = node_for(child_id)
                    wide_interval = child["wide_interval"]
                    narrow_interval = child["narrow_interval"]
                    accumulator.add(
                        wide_interval[0],
                        wide_interval[1],
                        narrow_interval[0],
                        narrow_interval[1],
                    )
            else:
                child_ids = (children_by_parent or {}).get(node_id, {})
                for child_id in child_ids:
                    child = node_for(child_id)
                    wide_interval = child["wide_interval"]
                    narrow_interval = child["narrow_interval"]
                    accumulator.add(
                        wide_interval[0],
                        wide_interval[1],
                        narrow_interval[0],
                        narrow_interval[1],
                    )
            values = accumulator.resolve(lambda_risk=lambda_risk)
            if values is None:
                values = (-1.0, 1.0, -1.0, 1.0)
        node["wide_interval"] = [values[0], values[1]]
        node["narrow_interval"] = [values[2], values[3]]
    for edge in edges:
        child = node_for(int(edge["child_id"]))
        edge["wide_interval"] = list(child["wide_interval"])
        edge["narrow_interval"] = list(child["narrow_interval"])
    root_id = int(nodes[0]["node_id"])
    for node in nodes[1:]:
        candidate_id = int(node["node_id"])
        if candidate_id < root_id:
            root_id = candidate_id
    return _node_intervals(node_for(root_id))


def _search_root(
    simulator: Any,
    root: GameState,
    *,
    position_index: int,
    total_positions: int = 1,
) -> dict[str, Any]:
    """搜索一个站位，并在图完成后按逆拓扑顺序计算观测区间。"""

    started_at = time.monotonic()
    if not root.position_signature:
        root.position_signature = position_signature(
            tuple(player.role for player in root.players)
        )
    roles = tuple(player.role for player in root.players)
    position_signature_value = root.position_signature
    position_display_value = " | ".join(
        f"{index + 1}:{role}" for index, role in enumerate(roles)
    )
    root_key = simulator._state_key(root)
    root_signature = simulator._state_signature_from_key(
        position_signature_value,
        root_key,
    )
    nodes: list[dict[str, Any]] = [
        {
            "node_id": 0,
            "state_key": root_key,
            "state_signature": root_signature,
            "round": root.day_count + root.night_count,
            "phase": root.phase,
            "day_count": root.day_count,
            "night_count": root.night_count,
            "is_terminal": False,
            "result": "未结束",
            "expanded": False,
            "good_paths": 0,
            "wolf_paths": 0,
            "wide_interval": [-1.0, 1.0],
            "narrow_interval": [-1.0, 1.0],
            "state_observation": _state_observation(
                root,
                parent_node_id=None,
                depth=0,
                action_label=root.action_label or "根状态",
            ),
        }
    ]
    node_id_by_key: dict[tuple[Any, ...], int] = {root_key: 0}
    frontier: deque[int] = deque([0])
    edges: list[dict[str, Any]] = []
    outgoing_by_node: dict[int, list[int]] = {}
    processed_states = 0
    terminal_count_live = 0
    last_progress_at = started_at
    last_memory_check_at = 0.0
    focus_node_id = 0
    pending_preview_nodes: deque[dict[str, Any]] = deque(
        maxlen=PREVIEW_NODE_BATCH_LIMIT * 2
    )
    pending_preview_edges: deque[dict[str, Any]] = deque(
        maxlen=PREVIEW_EDGE_BATCH_LIMIT * 2
    )

    def ensure_search_memory_available(*, force: bool = False) -> float:
        """在节点展开过程中定期检查内存，并返回当前单调时钟。"""

        nonlocal last_memory_check_at
        now = time.monotonic()
        if not force and now - last_memory_check_at < MEMORY_CHECK_INTERVAL_SECONDS:
            return now
        pressure = memory_pressure_snapshot(
            reserve_ratio=simulator.memory_reserve_ratio,
            reserve_gib=simulator.memory_reserve_gib,
        )
        last_memory_check_at = now
        if pressure is None:
            return now
        snapshot, threshold = pressure
        raise MemoryPressureInterrupt(
            "系统可用物理内存进入安全保留区："
            f"available={snapshot.available_bytes / 1024**3:.2f} GiB, "
            f"threshold={threshold / 1024**3:.2f} GiB"
        )

    def node_preview(node: dict[str, Any]) -> dict[str, Any]:
        return {
            "node_id": int(node["node_id"]),
            "phase": str(node["phase"]),
            "day_count": int(node["day_count"]),
            "night_count": int(node["night_count"]),
            "is_terminal": bool(node["is_terminal"]),
            "result": str(node["result"]),
            "expanded": bool(node.get("expanded", False)),
            "wide_interval": [-1.0, 1.0],
            "narrow_interval": [-1.0, 1.0],
            "state": game_state_dict_from_compact(
                node["state_key"],
                roles=roles,
                position_signature=position_signature_value,
                is_game_over=bool(node["is_terminal"]),
                state_id=int(node["node_id"]),
                observation=node["state_observation"],
            ),
            "live_preview": True,
        }

    def latest_node_previews() -> list[dict[str, Any]]:
        by_node_id: dict[int, dict[str, Any]] = {}
        for item in pending_preview_nodes:
            node_id = int(item["node_id"])
            by_node_id.pop(node_id, None)
            by_node_id[node_id] = item
        return list(by_node_id.values())[-PREVIEW_NODE_BATCH_LIMIT:]

    pending_preview_nodes.append(node_preview(nodes[0]))

    def publish_progress(*, kind: str = "node_progress", force: bool = False) -> None:
        """按 0.5 秒发布预览，并以同一周期执行内存安全检查。"""

        nonlocal last_progress_at
        now = ensure_search_memory_available(force=force)
        if not force and now - last_progress_at < PREVIEW_EMIT_INTERVAL_SECONDS:
            return
        next_node_id = None
        if frontier:
            next_node_id = (
                frontier[0] if simulator.search_mode == "bfs" else frontier[-1]
            )
        preview_nodes = latest_node_previews()
        preview_edges = list(pending_preview_edges)[-PREVIEW_EDGE_BATCH_LIMIT:]
        _publish_search_progress(
            simulator,
            {
                "kind": kind,
                "position_index": position_index,
                "position_signature": position_signature_value,
                "roles": list(roles),
                "position_display": position_display_value,
                "total_positions": total_positions,
                "processed_states": processed_states,
                "discovered_states": len(nodes),
                "frontier_size": len(frontier),
                "edge_count": len(edges),
                "terminal_count": terminal_count_live,
                "phase": nodes[next_node_id]["phase"]
                if next_node_id is not None
                else "complete",
                "focus_node_id": focus_node_id,
                "preview_nodes": preview_nodes,
                "preview_edges": preview_edges,
                "runtime_seconds": now - started_at,
            },
        )
        pending_preview_nodes.clear()
        pending_preview_edges.clear()
        last_progress_at = now

    publish_progress(kind="position_started", force=True)

    while frontier:
        _wait_until_resumed(simulator)
        node_id = (
            frontier.popleft() if simulator.search_mode == "bfs" else frontier.pop()
        )
        node = nodes[node_id]
        state = simulator._state_from_key(
            node["state_key"],
            roles=roles,
            position_signature_value=position_signature_value,
        )
        state.state_id = node_id
        state.depth = int(node["round"])
        is_terminal, result = simulator._check_game_over(state)
        node["is_terminal"] = is_terminal
        node["result"] = result
        node["expanded"] = True
        focus_node_id = node_id
        pending_preview_nodes.append(node_preview(node))
        processed_states += 1
        if is_terminal:
            terminal_count_live += 1
            publish_progress()
            continue

        local_edges: dict[int, dict[str, Any]] = {}
        transition_count = 0
        for transition in simulator.expand_state(state):
            transition_count += 1
            if transition_count % 256 == 0:
                # 单个高扇出节点可能长时间不触发节点级进度发布，因此在
                # 转移流内部也检查安全线，防止一个局部组合直接耗尽内存。
                ensure_search_memory_available()
            child = transition.state
            child_key = simulator._state_key(child)
            child_id = node_id_by_key.get(child_key)
            action_text = simulator._action_key_text(transition.action_key)
            action_label = simulator._action_label(transition.action_key)
            if child_id is None:
                child_id = len(nodes)
                node_id_by_key[child_key] = child_id
                nodes.append(
                    {
                        "node_id": child_id,
                        "state_key": child_key,
                        "state_signature": simulator._state_signature_from_key(
                            position_signature_value,
                            child_key,
                        ),
                        "round": child.day_count + child.night_count,
                        "phase": child.phase,
                        "day_count": child.day_count,
                        "night_count": child.night_count,
                        "is_terminal": False,
                        "result": "未结束",
                        "expanded": False,
                        "good_paths": 0,
                        "wolf_paths": 0,
                        "wide_interval": [-1.0, 1.0],
                        "narrow_interval": [-1.0, 1.0],
                        "state_observation": _state_observation(
                            child,
                            parent_node_id=node_id,
                            depth=int(node["round"]) + 1,
                            action_label=action_label,
                        ),
                    }
                )
                pending_preview_nodes.append(node_preview(nodes[child_id]))
                frontier.append(child_id)

            edge_data = local_edges.setdefault(
                child_id,
                {"multiplicity": 0, "reasons": {}},
            )
            edge_data["multiplicity"] += int(transition.multiplicity)
            reason = edge_data["reasons"].setdefault(
                action_text,
                {
                    "action_key": action_text,
                    "action_label": action_label,
                    "tactic": list(transition.action_key[2])
                    if transition.action_key[0] == "night"
                    else [
                        name
                        for name, active in (
                            ("seer_hide", transition.action_key[1] == "hide"),
                            ("villager_decoy", bool(transition.action_key[2])),
                            ("wolf_bloc", transition.action_key[3] == "bloc"),
                        )
                        if active
                    ],
                    "target": transition.action_key[3]
                    if transition.action_key[0] == "night"
                    else transition.action_key[6],
                    "multiplicity": 0,
                },
            )
            reason["multiplicity"] += int(transition.multiplicity)

        if transition_count == 0:
            raise RuntimeError(
                "非终局状态没有合法分支: "
                f"position={position_signature_value}, node={node_id}, phase={state.phase}"
            )

        outgoing_indices: list[int] = []
        for child_id, edge_data in local_edges.items():
            reasons = list(edge_data["reasons"].values())
            edge_index = len(edges)
            outgoing_indices.append(edge_index)
            edge = {
                "parent_id": node_id,
                "child_id": child_id,
                "action_key": f"{node_id}->{child_id}",
                "action_label": " / ".join(
                    str(reason["action_label"]) for reason in reasons
                ),
                "action_variant_count": len(reasons),
                "multiplicity": int(edge_data["multiplicity"]),
                "reasons": reasons,
                "wide_interval": [-1.0, 1.0],
                "narrow_interval": [-1.0, 1.0],
            }
            edges.append(edge)
            pending_preview_edges.append(
                {
                    "parent_id": node_id,
                    "child_id": child_id,
                    "action_key": edge["action_key"],
                    "action_label": str(edge["action_label"])[:160],
                    "action_variant_count": len(reasons),
                    "multiplicity": int(edge_data["multiplicity"]),
                    "reasons": [
                        {"action_label": str(reason["action_label"])[:160]}
                        for reason in reasons[:4]
                    ],
                    "wide_interval": [-1.0, 1.0],
                    "narrow_interval": [-1.0, 1.0],
                    "live_preview": True,
                }
            )
        outgoing_by_node[node_id] = outgoing_indices
        publish_progress()

    publish_progress(force=True)

    # 搜索已经完成，状态去重索引与实时预览缓冲不会再被读取。先释放这些
    # 大对象，给路径/interval 回传留下稳定的内存余量。
    del node_id_by_key
    pending_preview_nodes.clear()
    pending_preview_edges.clear()

    reverse_topological_ids = [int(node["node_id"]) for node in nodes]
    reverse_topological_ids.sort(
        key=lambda item: (int(nodes[item]["round"]), item), reverse=True
    )
    ensure_search_memory_available(force=True)
    for node_id in reverse_topological_ids:
        node = nodes[node_id]
        if node["is_terminal"]:
            node["good_paths"] = int("好人" in str(node["result"]))
            node["wolf_paths"] = int("狼人" in str(node["result"]))
        else:
            good_paths = 0
            wolf_paths = 0
            for edge_index in outgoing_by_node.get(node_id, []):
                edge = edges[edge_index]
                child = nodes[int(edge["child_id"])]
                multiplicity = int(edge["multiplicity"])
                good_paths += multiplicity * int(child["good_paths"])
                wolf_paths += multiplicity * int(child["wolf_paths"])
            node["good_paths"] = good_paths
            node["wolf_paths"] = wolf_paths

        if node_id % 4096 == 0:
            ensure_search_memory_available()

    ensure_search_memory_available(force=True)
    recompute_graph_intervals(
        {"nodes": nodes, "edges": edges},
        lambda_risk=simulator.lambda_risk,
        outgoing_edge_indices=outgoing_by_node,
        reverse_node_ids=reverse_topological_ids,
    )
    ensure_search_memory_available(force=True)

    root_node = nodes[0]
    root_robust = _node_intervals(root_node)
    del reverse_topological_ids
    del outgoing_by_node
    for node in nodes:
        node["state_compact"] = node.pop("state_key")
        node.pop("round", None)
        node.pop("expanded", None)
    return {
        "position_index": position_index,
        "position_signature": position_signature_value,
        "roles": list(roles),
        "position_display": position_display_value,
        "search_mode": simulator.search_mode,
        "state_count": len(nodes),
        "edge_count": len(edges),
        "terminal_count": terminal_count_live,
        "processed_states": processed_states,
        "good_paths": int(root_node["good_paths"]),
        "wolf_paths": int(root_node["wolf_paths"]),
        "wide_interval": root_robust.wide.to_list(),
        "narrow_interval": root_robust.narrow.to_list(),
        "camp": interval_camp(root_robust.wide),
        "runtime_seconds": time.monotonic() - started_at,
        "nodes": nodes,
        "edges": edges,
    }


def search_from_state(simulator: Any, state: GameState) -> dict[str, Any]:
    """从 API 传入 GameState 继续构建完整 BFS/DFS 分支 DAG。"""

    return _search_root(simulator, state.clone(), position_index=1, total_positions=1)


def _position_task(payload: dict[str, Any]) -> dict[str, Any]:
    """在隔离进程中计算一个站位。

    参数：
        payload: 父进程显式构造的模拟器配置、站位、队列和总站位数。

    返回：
        使用结果队列时返回已流式发送的站位标识；否则返回完整站位 DAG。
    """

    forbidden_modules = (
        "pygame",
        "sqlalchemy",
        "greenlet",
    )
    loaded_forbidden = [
        module_name
        for module_name in forbidden_modules
        if module_name in sys.modules
    ]
    if loaded_forbidden:
        raise RuntimeError(
            "计算 worker 加载了禁止的原生/持久化模块: "
            + ", ".join(loaded_forbidden)
        )
    from ._simulator import SearchSimulator

    config = payload["simulator_config"]
    simulator = SearchSimulator(
        number_of_players=config["number_of_players"],
        number_of_wolves=config["number_of_wolves"],
        include_seer=config["include_seer"],
        include_witch=config["include_witch"],
        include_guard=config["include_guard"],
        include_hunter=config["include_hunter"],
        include_idiot=config["include_idiot"],
        include_white_werewolf_king=config["include_white_werewolf_king"],
        smart_vote=config["smart_vote"],
        tactics=config["tactics"],
        search_mode=config["search_mode"],
        lambda_risk=config["lambda_risk"],
        parallel_workers=config["parallel_workers"],
        memory_reserve_gib=config["memory_reserve_gib"],
        memory_reserve_ratio=config["memory_reserve_ratio"],
        all_positions=False,
        persistence_enabled=False,
        iteration_callback=None,
        progress_queue=payload.get("progress_queue"),
        resume_event=payload.get("resume_event"),
    )
    layout = PositionLayout(
        index=int(payload["layout"]["index"]),
        roles=tuple(payload["layout"]["roles"]),
        signature=str(payload["layout"]["signature"]),
    )
    result = _search_root(
        simulator,
        simulator.initial_state_for_layout(layout),
        position_index=layout.index,
        total_positions=int(payload.get("total_positions", 1)),
    )
    result_queue = payload.get("result_queue")
    if result_queue is None:
        return result

    summary = {
        key: value
        for key, value in result.items()
        if key not in {"nodes", "edges"}
    }
    position_signature_value = str(summary["position_signature"])
    result_queue.put({"kind": "position_begin", "summary": summary})
    for start in range(0, len(result["nodes"]), PERSISTENCE_BATCH_SIZE):
        result_queue.put(
            {
                "kind": "position_nodes",
                "position_signature": position_signature_value,
                "items": result["nodes"][start : start + PERSISTENCE_BATCH_SIZE],
            }
        )
    for start in range(0, len(result["edges"]), PERSISTENCE_BATCH_SIZE):
        result_queue.put(
            {
                "kind": "position_edges",
                "position_signature": position_signature_value,
                "items": result["edges"][start : start + PERSISTENCE_BATCH_SIZE],
            }
        )
    result_queue.put(
        {
            "kind": "position_end",
            "position_signature": position_signature_value,
        }
    )
    return {
        "position_index": layout.index,
        "position_signature": position_signature_value,
        "streamed": True,
    }


def _worker_config(simulator: Any) -> dict[str, Any]:
    """显式导出单站位 worker 所需的全部可序列化参数。"""

    return {
        "number_of_players": simulator.number_of_players,
        "number_of_wolves": simulator.number_of_wolves,
        "include_seer": simulator.include_seer,
        "include_witch": simulator.include_witch,
        "include_guard": simulator.include_guard,
        "include_hunter": simulator.include_hunter,
        "include_idiot": simulator.include_idiot,
        "include_white_werewolf_king": simulator.include_white_werewolf_king,
        "smart_vote": simulator.smart_vote,
        "tactics": sorted(simulator.tactics),
        "search_mode": simulator.search_mode,
        "lambda_risk": simulator.lambda_risk,
        "parallel_workers": 1,
        "memory_reserve_gib": simulator.memory_reserve_gib,
        "memory_reserve_ratio": simulator.memory_reserve_ratio,
    }


def run_position_batch(simulator: Any) -> dict[str, Any]:
    """串行完成每个站位，并在站位边界自动持久化与恢复。

    参数：
        simulator: 已完成显式配置加载的搜索模拟器实例。

    返回：
        完整运行摘要，或包含下一站位编号的可恢复中断摘要。
    """

    from ._sqlite_lru_signature_store import _SQLiteLRUSignatureStore

    started_at = time.monotonic()
    layouts = enumerate_position_layouts(simulator.roster)
    if not simulator.all_positions:
        layouts = layouts[:1]
    store = _SQLiteLRUSignatureStore(
        simulator.signature_cache_db_path,
        lru_capacity=simulator.signature_lru_capacity,
        commit_interval=simulator.signature_commit_interval,
    )
    simulator.signature_cache = store
    config = {
        "roster": list(simulator.roster),
        "position_count": len(layouts),
        "search_mode": simulator.search_mode,
        "lambda_risk": simulator.lambda_risk,
        "smart_vote": simulator.smart_vote,
        "position_workers": 1,
        "tactics": sorted(simulator.tactics),
    }
    simulator.run_id, resumed_run = store.start_or_resume_run(config)
    discarded_incomplete_positions = store.discard_incomplete_position_results(
        simulator.run_id
    )
    expected_signatures = {layout.signature for layout in layouts}
    summaries = [
        item
        for item in store.list_completed_position_results(simulator.run_id)
        if item["position_signature"] in expected_signatures
    ]
    summaries.sort(key=lambda item: int(item["position_index"]))
    completed_signatures = {
        str(item["position_signature"])
        for item in summaries
    }
    simulator.processed_positions = len(summaries)
    simulator.processed_states = sum(
        int(item["processed_states"])
        for item in summaries
    )
    pending_layouts = [
        layout
        for layout in layouts
        if layout.signature not in completed_signatures
    ]
    owned_result_manager = None
    if simulator.result_queue is None:
        owned_result_manager = multiprocessing.Manager()
        simulator.result_queue = owned_result_manager.Queue(maxsize=8)
    worker_config = _worker_config(simulator)

    def payload_for(layout: PositionLayout) -> dict[str, Any]:
        """只为即将执行的一个站位构造任务，禁止批量预取。"""

        return {
            "simulator_config": worker_config,
            "progress_queue": simulator.progress_queue,
            "result_queue": simulator.result_queue,
            "resume_event": simulator.resume_event,
            "total_positions": len(layouts),
            "layout": {
                "index": layout.index,
                "roles": list(layout.roles),
                "signature": layout.signature,
            },
        }

    def build_run_summary(
        *,
        status: str,
        interruption_reason: str | None = None,
    ) -> dict[str, Any]:
        """从已完成站位构造可持久化的批次检查点摘要。"""

        ordered = sorted(summaries, key=lambda item: int(item["position_index"]))
        total_good = sum(int(item["good_paths"]) for item in ordered)
        total_wolf = sum(int(item["wolf_paths"]) for item in ordered)
        aggregate = (
            propagate_intervals(
                (
                    RobustIntervals(
                        wide=RewardInterval(*item["wide_interval"]),
                        narrow=RewardInterval(*item["narrow_interval"]),
                    )
                    for item in ordered
                ),
                lambda_risk=simulator.lambda_risk,
            )
            if ordered
            else UNRESOLVED
        )
        done_signatures = {
            str(item["position_signature"])
            for item in ordered
        }
        next_position_index = next(
            (
                layout.index
                for layout in layouts
                if layout.signature not in done_signatures
            ),
            None,
        )
        summary = {
            "run_id": simulator.run_id,
            "status": status,
            "config": config,
            "position_count": len(ordered),
            "total_position_count": len(layouts),
            "next_position_index": next_position_index,
            "good_paths": total_good,
            "wolf_paths": total_wolf,
            "wide_interval": aggregate.wide.to_list(),
            "narrow_interval": aggregate.narrow.to_list(),
            "camp": interval_camp(aggregate.wide),
            "processed_states": sum(
                int(item["processed_states"])
                for item in ordered
            ),
            "runtime_seconds": time.monotonic() - started_at,
            "position_runtime_seconds": sum(
                float(item["runtime_seconds"])
                for item in ordered
            ),
            "resumed_run": resumed_run,
            "discarded_incomplete_positions": discarded_incomplete_positions,
            "positions": ordered,
        }
        if interruption_reason is not None:
            summary["interruption_reason"] = interruption_reason
        return summary

    persisted_condition = threading.Condition()
    persisted_signatures = set(completed_signatures)

    def record_summary(summary: dict[str, Any]) -> None:
        """在完整 DAG 落库后提交检查点，再向 UI 宣告站位完成。"""

        summaries.append(summary)
        simulator.processed_positions += 1
        simulator.processed_states += int(summary["processed_states"])
        checkpoint = build_run_summary(status="running")
        store.checkpoint_run(simulator.run_id, checkpoint)
        with persisted_condition:
            persisted_signatures.add(str(summary["position_signature"]))
            persisted_condition.notify_all()
        if simulator.iteration_callback is not None:
            simulator.iteration_callback(
                {
                    "kind": "position_result",
                    "run_id": simulator.run_id,
                    "completed_positions": simulator.processed_positions,
                    "total_positions": len(layouts),
                    **summary,
                }
            )

    def persist(result: dict[str, Any]) -> None:
        """无跨进程结果队列时的兼容持久化路径。"""

        store.save_position_result(simulator.run_id, result)
        record_summary(
            {
                key: value
                for key, value in result.items()
                if key not in {"nodes", "edges"}
            }
        )

    writer_errors: list[BaseException] = []
    active_streams: set[str] = set()

    # position_end 不重复携带摘要；writer 以签名暂存 begin 中的小型摘要。
    stream_summaries: dict[str, dict[str, Any]] = {}

    def consume_result_batches() -> None:
        """单写线程按摘要、节点、边、完成标记顺序消费有界队列。"""

        result_queue = simulator.result_queue
        if result_queue is None:
            return
        failed = False
        while True:
            message = result_queue.get()
            kind = message.get("kind")
            if kind == "shutdown":
                break
            position_signature_value = str(message.get("position_signature", ""))
            try:
                if failed:
                    continue
                if kind == "position_begin":
                    summary = message["summary"]
                    position_signature_value = str(summary["position_signature"])
                    store.begin_position_result(simulator.run_id, summary)
                    stream_summaries[position_signature_value] = summary
                    active_streams.add(position_signature_value)
                elif kind == "position_nodes":
                    store.append_position_nodes(
                        simulator.run_id,
                        position_signature_value,
                        message["items"],
                    )
                elif kind == "position_edges":
                    store.append_position_edges(
                        simulator.run_id,
                        position_signature_value,
                        message["items"],
                    )
                elif kind == "position_end":
                    store.finish_position_result()
                    active_streams.discard(position_signature_value)
                    record_summary(stream_summaries.pop(position_signature_value))
            except BaseException as exc:
                writer_errors.append(exc)
                failed = True
                with persisted_condition:
                    persisted_condition.notify_all()
        for position_signature_value in active_streams:
            store.abort_position_result(simulator.run_id, position_signature_value)

    writer_thread = None
    if simulator.result_queue is not None:
        writer_thread = threading.Thread(
            target=consume_result_batches,
            name="sqlite-result-writer",
            daemon=True,
        )
        writer_thread.start()

    if simulator.iteration_callback is not None:
        for completed_number, summary in enumerate(summaries, start=1):
            simulator.iteration_callback(
                {
                    "kind": "position_result",
                    "run_id": simulator.run_id,
                    "completed_positions": completed_number,
                    "total_positions": len(layouts),
                    **summary,
                }
            )

    def stop_writer() -> None:
        """排空并停止 SQLite 单写线程，确保状态更新发生在其后。"""

        if (
            simulator.result_queue is not None
            and writer_thread is not None
            and writer_thread.is_alive()
        ):
            simulator.result_queue.put({"kind": "shutdown"})
            writer_thread.join()

    def ensure_memory_available() -> None:
        """站位边界执行内存检查，低于安全线时停止继续派发。"""

        pressure = memory_pressure_snapshot(
            reserve_ratio=simulator.memory_reserve_ratio,
            reserve_gib=simulator.memory_reserve_gib,
        )
        if pressure is None:
            return
        snapshot, threshold = pressure
        raise MemoryPressureInterrupt(
            "系统可用物理内存进入安全保留区："
            f"available={snapshot.available_bytes / 1024**3:.2f} GiB, "
            f"threshold={threshold / 1024**3:.2f} GiB"
        )

    try:
        with ProcessPoolExecutor(
            max_workers=1,
            max_tasks_per_child=1,
        ) as executor:
            for layout in pending_layouts:
                # 只有前一站位已经通过 writer 的数量完整性事务并唤醒父进程，
                # 循环才会走到下一次 submit；因此不存在并发展开的站位。
                ensure_memory_available()
                result = executor.submit(_position_task, payload_for(layout)).result()
                if simulator.result_queue is None:
                    persist(result)
                else:
                    signature = str(result["position_signature"])
                    with persisted_condition:
                        while signature not in persisted_signatures:
                            if writer_errors:
                                raise writer_errors[0]
                            persisted_condition.wait(timeout=0.1)
                ensure_memory_available()

        stop_writer()
        if writer_errors:
            raise writer_errors[0]
        summary = build_run_summary(status="complete")
        simulator.position_results = list(summary["positions"])
        store.finish_run(simulator.run_id, summary, status="complete")
        return summary
    except MemoryPressureInterrupt as exc:
        stop_writer()
        summary = build_run_summary(
            status="interrupted",
            interruption_reason=str(exc),
        )
        simulator.position_results = list(summary["positions"])
        simulator.stop_reason = str(exc)
        store.finish_run(simulator.run_id, summary, status="interrupted")
        return summary
    except KeyboardInterrupt as exc:
        stop_writer()
        summary = build_run_summary(
            status="interrupted",
            interruption_reason="用户中断运行",
        )
        simulator.position_results = list(summary["positions"])
        store.finish_run(simulator.run_id, summary, status="interrupted")
        raise exc
    except Exception as exc:
        stop_writer()
        failed_summary = build_run_summary(status="failed")
        failed_summary.update(
            {"error_type": type(exc).__name__, "error": str(exc)}
        )
        store.finish_run(
            simulator.run_id,
            failed_summary,
            status="failed",
        )
        raise
    finally:
        if owned_result_manager is not None:
            owned_result_manager.shutdown()
            simulator.result_queue = None
