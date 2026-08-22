"""BFS/DFS 状态 DAG 构建与 wide/narrow interval 搜索后回传。"""

from __future__ import annotations

import multiprocessing
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import wait
from typing import Any
from typing import Iterable

from ._config import PERSISTENCE_BATCH_SIZE
from ._game_state import GameState
from ._game_state import game_state_dict_from_compact
from ._interval import UNRESOLVED
from ._interval import RewardInterval
from ._interval import RobustIntervals
from ._interval import interval_camp
from ._interval import propagate_interval_values
from ._interval import propagate_intervals
from ._positions import PositionLayout
from ._positions import enumerate_position_layouts
from ._positions import position_signature

PREVIEW_NODE_BATCH_LIMIT = 48
PREVIEW_EDGE_BATCH_LIMIT = 72
PREVIEW_EMIT_INTERVAL_SECONDS = 0.5


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
    sequential_node_ids = all(
        int(node["node_id"]) == index for index, node in enumerate(nodes)
    )
    node_by_id = (
        None
        if sequential_node_ids
        else {int(node["node_id"]): node for node in nodes}
    )

    def node_for(node_id: int) -> dict[str, Any]:
        if node_by_id is None:
            return nodes[node_id]
        return node_by_id[node_id]
    children_by_parent: dict[int, set[int]] | None = None
    if outgoing_edge_indices is None:
        children_by_parent = {}
        for edge in edges:
            children_by_parent.setdefault(int(edge["parent_id"]), set()).add(
                int(edge["child_id"])
            )
    ordered_node_ids: Iterable[int] = (
        reverse_node_ids
        if reverse_node_ids is not None
        else (
            int(node["node_id"])
            for node in sorted(
                nodes,
                key=lambda item: (
                    int(item["day_count"]) + int(item["night_count"]),
                    int(item["node_id"]),
                ),
                reverse=True,
            )
        )
    )
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
            child_ids = (
                (
                    int(edges[edge_index]["child_id"])
                    for edge_index in outgoing_edge_indices.get(node_id, [])
                )
                if outgoing_edge_indices is not None
                else iter(sorted((children_by_parent or {}).get(node_id, set())))
            )
            values = propagate_interval_values(
                (
                    (
                        float(node_for(child_id)["wide_interval"][0]),
                        float(node_for(child_id)["wide_interval"][1]),
                        float(node_for(child_id)["narrow_interval"][0]),
                        float(node_for(child_id)["narrow_interval"][1]),
                    )
                    for child_id in child_ids
                ),
                lambda_risk=lambda_risk,
            )
            if values is None:
                values = (-1.0, 1.0, -1.0, 1.0)
        node["wide_interval"] = [values[0], values[1]]
        node["narrow_interval"] = [values[2], values[3]]
    for edge in edges:
        child = node_for(int(edge["child_id"]))
        edge["wide_interval"] = list(child["wide_interval"])
        edge["narrow_interval"] = list(child["narrow_interval"])
    root_id = min(int(node["node_id"]) for node in nodes)
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
    focus_node_id = 0
    pending_preview_nodes: deque[dict[str, Any]] = deque(
        maxlen=PREVIEW_NODE_BATCH_LIMIT * 2
    )
    pending_preview_edges: deque[dict[str, Any]] = deque(
        maxlen=PREVIEW_EDGE_BATCH_LIMIT * 2
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
        nonlocal last_progress_at
        now = time.monotonic()
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

    reverse_topological_ids = sorted(
        range(len(nodes)),
        key=lambda item: (int(nodes[item]["round"]), item),
        reverse=True,
    )
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

    recompute_graph_intervals(
        {"nodes": nodes, "edges": edges},
        lambda_risk=simulator.lambda_risk,
        outgoing_edge_indices=outgoing_by_node,
        reverse_node_ids=reverse_topological_ids,
    )

    root_node = nodes[0]
    root_robust = _node_intervals(root_node)
    terminal_count = sum(1 for node in nodes if node["is_terminal"])
    return {
        "position_index": position_index,
        "position_signature": position_signature_value,
        "roles": list(roles),
        "position_display": position_display_value,
        "search_mode": simulator.search_mode,
        "state_count": len(nodes),
        "edge_count": len(edges),
        "terminal_count": terminal_count,
        "processed_states": processed_states,
        "good_paths": int(root_node["good_paths"]),
        "wolf_paths": int(root_node["wolf_paths"]),
        "wide_interval": root_robust.wide.to_list(),
        "narrow_interval": root_robust.narrow.to_list(),
        "camp": interval_camp(root_robust.wide),
        "runtime_seconds": time.monotonic() - started_at,
        "nodes": [
            {
                "node_id": node["node_id"],
                "state_signature": node["state_signature"],
                "phase": node["phase"],
                "day_count": node["day_count"],
                "night_count": node["night_count"],
                "is_terminal": node["is_terminal"],
                "result": node["result"],
                "good_paths": node["good_paths"],
                "wolf_paths": node["wolf_paths"],
                "wide_interval": node["wide_interval"],
                "narrow_interval": node["narrow_interval"],
                "state_compact": node["state_key"],
                "state_observation": node["state_observation"],
            }
            for node in nodes
        ],
        "edges": edges,
    }


def search_from_state(simulator: Any, state: GameState) -> dict[str, Any]:
    """从 API 传入 GameState 继续构建完整 BFS/DFS 分支 DAG。"""

    return _search_root(simulator, state.clone(), position_index=1, total_positions=1)


def _position_task(payload: dict[str, Any]) -> dict[str, Any]:
    from ._simulator import SearchSimulator

    simulator = SearchSimulator(
        **payload["simulator_config"],
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
    }


def run_position_batch(simulator: Any) -> dict[str, Any]:
    """枚举站位、多进程搜索，并由父进程顺序持久化完整 DAG。"""

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
        "parallel_workers": simulator.parallel_workers,
        "tactics": sorted(simulator.tactics),
    }
    simulator.run_id = store.start_run(config)
    owned_result_manager = None
    if simulator.result_queue is None:
        owned_result_manager = multiprocessing.Manager()
        simulator.result_queue = owned_result_manager.Queue(maxsize=8)
    worker_config = _worker_config(simulator)
    payloads = [
        {
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
        for layout in layouts
    ]
    summaries: list[dict[str, Any]] = []

    def record_summary(summary: dict[str, Any]) -> None:
        summaries.append(summary)
        simulator.processed_positions += 1
        simulator.processed_states += int(summary["processed_states"])
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

    try:
        payload_iterator = iter(payloads)
        pending = set()
        max_workers = min(len(payloads), simulator.parallel_workers)
        with ProcessPoolExecutor(
            max_workers=max_workers,
            max_tasks_per_child=1,
        ) as executor:
            for _ in range(max_workers):
                pending.add(executor.submit(_position_task, next(payload_iterator)))
            while pending:
                finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in finished:
                    result = future.result()
                    if simulator.result_queue is None:
                        persist(result)
                    try:
                        payload = next(payload_iterator)
                    except StopIteration:
                        continue
                    pending.add(executor.submit(_position_task, payload))

        if simulator.result_queue is not None:
            simulator.result_queue.put({"kind": "shutdown"})
            writer_thread.join()
            if writer_errors:
                raise writer_errors[0]

        summaries.sort(key=lambda item: int(item["position_index"]))
        simulator.position_results = summaries
        total_good = sum(int(item["good_paths"]) for item in summaries)
        total_wolf = sum(int(item["wolf_paths"]) for item in summaries)
        aggregate = propagate_intervals(
            (
                RobustIntervals(
                    wide=RewardInterval(*item["wide_interval"]),
                    narrow=RewardInterval(*item["narrow_interval"]),
                )
                for item in summaries
            ),
            lambda_risk=simulator.lambda_risk,
        ) if summaries else UNRESOLVED
        summary = {
            "run_id": simulator.run_id,
            "config": config,
            "position_count": len(summaries),
            "good_paths": total_good,
            "wolf_paths": total_wolf,
            "wide_interval": aggregate.wide.to_list(),
            "narrow_interval": aggregate.narrow.to_list(),
            "camp": interval_camp(aggregate.wide),
            "processed_states": simulator.processed_states,
            "runtime_seconds": time.monotonic() - started_at,
            "positions": summaries,
        }
        store.finish_run(simulator.run_id, summary, status="complete")
        return summary
    except Exception as exc:
        if simulator.result_queue is not None and writer_thread is not None:
            if writer_thread.is_alive():
                simulator.result_queue.put({"kind": "shutdown"})
                writer_thread.join()
        store.finish_run(
            simulator.run_id,
            {"error_type": type(exc).__name__, "error": str(exc)},
            status="failed",
        )
        raise
    finally:
        if owned_result_manager is not None:
            owned_result_manager.shutdown()
            simulator.result_queue = None
