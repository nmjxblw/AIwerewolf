"""BFS/DFS 状态 DAG 构建与 wide/narrow interval 搜索后回传。"""

from __future__ import annotations

import gc
import logging
import multiprocessing
import multiprocessing.util as multiprocessing_util
import os
import pickle
import sys
import threading
import time
from array import array
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Iterable

from ._config import PERSISTENCE_BATCH_SIZE
from ._game_state import GameState
from ._game_state import decode_compact_state_blob
from ._game_state import encode_compact_state_blob
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

logger = logging.getLogger(__name__)

PREVIEW_NODE_BATCH_LIMIT = 48
PREVIEW_EDGE_BATCH_LIMIT = 72
PREVIEW_EMIT_INTERVAL_SECONDS = 0.5
MEMORY_CHECK_INTERVAL_SECONDS = 0.5
POSTPROCESS_PROGRESS_BATCH_SIZE = 1024
WORKER_PROGRESS_LOG_INTERVAL_SECONDS = 5.0
# Windows CPython 在该百万对象搜索热循环中会在单进程持续分配约 3 万次后
# 出现内置对象槽位错位。10k 预算让进程在危险窗口前主动退出；站位状态、
# frontier 与拓扑均由检查点承接，因此这只是执行隔离边界，不改变 DFS/BFS。
WORKER_NODE_BUDGET = 10_000
WORKER_MIN_NODE_BUDGET = 250
WORKER_CHUNK_RETRY_LIMIT = 6
SEARCH_CHECKPOINT_VERSION = 3
_NO_SITE_SPAWN_LOCK = threading.Lock()


@dataclass(slots=True)
class _CompactSearchNode:
    """worker 内部的紧凑节点；完整字典仅在预览/持久化时重建。"""

    state_key: bytes
    # 为兼容 v2 pickle 保留此槽；新节点不长期缓存哈希字符串，持久化时生成。
    state_signature: str
    round: int
    phase: str
    day_count: int
    night_count: int
    observation: tuple[Any, ...]
    is_terminal: bool = False
    result: str = "未结束"
    expanded: bool = False
    good_paths: int = 0
    wolf_paths: int = 0
    wide_lower: float = -1.0
    wide_upper: float = 1.0
    narrow_lower: float = -1.0
    narrow_upper: float = 1.0
    outgoing_start: int = -1
    outgoing_count: int = 0


@dataclass(slots=True)
class _CompactEdges:
    """连续数组拓扑；只有当前未落库分块暂存 Python 原因对象。"""

    parent_ids: array = field(default_factory=lambda: array("I"))
    child_ids: array = field(default_factory=lambda: array("I"))
    multiplicities: array = field(default_factory=lambda: array("Q"))
    pending_reasons: dict[
        int,
        tuple[tuple[tuple[Any, ...], int], ...],
    ] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.child_ids)

    def append(
        self,
        *,
        parent_id: int,
        child_id: int,
        multiplicity: int,
        reasons: tuple[tuple[tuple[Any, ...], int], ...],
    ) -> int:
        """追加一条边并返回稳定下标。"""

        edge_index = len(self.child_ids)
        self.parent_ids.append(parent_id)
        self.child_ids.append(child_id)
        self.multiplicities.append(multiplicity)
        if reasons:
            self.pending_reasons[edge_index] = reasons
        return edge_index


@dataclass(slots=True)
class _SearchCheckpoint:
    """站位内 worker 重启检查点；对象引用关系由 pickle memo 无损保留。"""

    version: int
    position_signature: str
    nodes: list[_CompactSearchNode]
    node_id_by_key: dict[bytes, int]
    frontier: deque[int]
    edges: _CompactEdges
    processed_states: int
    terminal_count: int
    focus_node_id: int
    accumulated_runtime_seconds: float


def _save_search_checkpoint(path: Path, checkpoint: _SearchCheckpoint) -> None:
    """原子写入站位内检查点，崩溃时旧版本仍保持可恢复。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    try:
        with temporary_path.open("wb") as handle:
            pickle.dump(checkpoint, handle, protocol=pickle.HIGHEST_PROTOCOL)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _load_search_checkpoint(
    path: Path,
    *,
    position_signature_value: str,
) -> _SearchCheckpoint | None:
    """读取并校验站位内检查点；不存在时返回 ``None``。"""

    if not path.exists():
        return None
    with path.open("rb") as handle:
        checkpoint = pickle.load(handle)
    if not isinstance(checkpoint, _SearchCheckpoint):
        raise TypeError(f"无效搜索检查点类型: {type(checkpoint).__name__}")
    if checkpoint.version not in {2, SEARCH_CHECKPOINT_VERSION}:
        raise ValueError(f"不支持的搜索检查点版本: {checkpoint.version}")
    if checkpoint.position_signature != position_signature_value:
        raise ValueError("搜索检查点站位签名不匹配")
    if checkpoint.version == 2:
        # v2 为每个节点保留多元素 tuple 和 32 字符哈希。迁移时先清空旧
        # 去重字典，再逐节点替换，避免新旧两份大型索引同时驻留造成峰值。
        checkpoint.node_id_by_key.clear()
        migrated_index: dict[bytes, int] = {}
        for node_id, node in enumerate(checkpoint.nodes):
            if isinstance(node.state_key, bytes):
                state_blob = node.state_key
            else:
                state_blob = encode_compact_state_blob(node.state_key)
            node.state_key = state_blob
            node.state_signature = ""
            migrated_index[state_blob] = node_id
        checkpoint.node_id_by_key = migrated_index
        checkpoint.version = SEARCH_CHECKPOINT_VERSION
        logger.info(
            "WORKER_CHECKPOINT_MIGRATED position_signature=%s nodes=%s v2->v%s",
            position_signature_value,
            len(checkpoint.nodes),
            SEARCH_CHECKPOINT_VERSION,
        )
    return checkpoint


def _remove_search_checkpoint(path: Path) -> None:
    """完整站位持久化前移除已消费的临时检查点。"""

    path.unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass


@contextmanager
def _isolated_compute_worker_spawn() -> Iterable[None]:
    """仅为 Windows 计算 worker 的 spawn 命令追加 ``-S``。

    父进程仍从共享 ``.venv`` 加载 GUI、SQLAlchemy 等依赖；纯计算子进程
    禁止加载 site 初始化。Windows 事件日志和 12 万节点对照表明，当前
    CPython 安装在启用 site 后会发生内置对象槽位错位与 0xc0000005。
    修改只在进程池存活期间生效，并用全局锁禁止并发改写 spawn 参数。
    """

    if os.name != "nt":
        yield
        return
    with _NO_SITE_SPAWN_LOCK:
        original = multiprocessing_util._args_from_interpreter_flags

        def no_site_interpreter_flags() -> list[str]:
            flags = list(original())
            if "-S" not in flags:
                flags.append("-S")
            return flags

        multiprocessing_util._args_from_interpreter_flags = no_site_interpreter_flags
        try:
            yield
        finally:
            multiprocessing_util._args_from_interpreter_flags = original


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


def _compact_node_to_dict(
    simulator: Any,
    node: _CompactSearchNode,
    *,
    node_id: int,
    position_signature_value: str,
) -> dict[str, Any]:
    """把一个紧凑节点转换为 SQLite/API 的稳定字典契约。"""

    state_signature = node.state_signature
    if not state_signature:
        state_signature = simulator._state_signature_from_key(
            position_signature_value,
            node.state_key,
        )
    return {
        "node_id": node_id,
        "state_signature": state_signature,
        "phase": node.phase,
        "day_count": node.day_count,
        "night_count": node.night_count,
        "is_terminal": node.is_terminal,
        "result": node.result,
        "good_paths": node.good_paths,
        "wolf_paths": node.wolf_paths,
        "wide_interval": [node.wide_lower, node.wide_upper],
        "narrow_interval": [node.narrow_lower, node.narrow_upper],
        "state_observation": node.observation,
        # SQLite/API 仍保存可读、可迁移的扁平数组；worker 常驻态才使用 bytes。
        "state_compact": decode_compact_state_blob(node.state_key),
    }


def _compact_reason_to_dict(
    simulator: Any,
    *,
    action_key: tuple[Any, ...],
    multiplicity: int,
) -> dict[str, Any]:
    """仅在输出边时把原始动作键展开为完整派生原因。"""

    if action_key[0] == "night":
        tactic_names = list(action_key[2])
        target = action_key[3]
    else:
        tactic_names = []
        for name, active in (
            ("seer_hide", action_key[1] == "hide"),
            ("villager_decoy", bool(action_key[2])),
            ("wolf_bloc", action_key[3] == "bloc"),
        ):
            if active:
                tactic_names.append(name)
        target = action_key[6]
    return {
        "action_key": simulator._action_key_text(action_key),
        "action_label": simulator._action_label(action_key),
        "tactic": tactic_names,
        "target": target,
        "multiplicity": multiplicity,
    }


def _compact_edge_to_dict(
    simulator: Any,
    *,
    edges: _CompactEdges,
    edge_index: int,
    nodes: list[_CompactSearchNode],
) -> dict[str, Any]:
    """把一个紧凑边按需转换为持久化/可视化字典。"""

    reasons: list[dict[str, Any]] = []
    reason_labels: list[str] = []
    for action_key, multiplicity in edges.pending_reasons.get(edge_index, ()):
        reason = _compact_reason_to_dict(
            simulator,
            action_key=action_key,
            multiplicity=multiplicity,
        )
        reasons.append(reason)
        reason_labels.append(str(reason["action_label"]))
    parent_id = int(edges.parent_ids[edge_index])
    child_id = int(edges.child_ids[edge_index])
    child = nodes[child_id]
    return {
        "parent_id": parent_id,
        "child_id": child_id,
        "action_key": f"{parent_id}->{child_id}",
        "action_label": " / ".join(reason_labels),
        "action_variant_count": len(reasons),
        "multiplicity": int(edges.multiplicities[edge_index]),
        "reasons": reasons,
        "wide_interval": [child.wide_lower, child.wide_upper],
        "narrow_interval": [child.narrow_lower, child.narrow_upper],
    }


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
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> RobustIntervals:
    """以流式常数额外空间在固定 DAG 上回传 interval。

    参数：
        graph: 包含节点和边的固定 DAG。
        lambda_risk: wide/narrow 区间的风险放缩参数。
        outgoing_edge_indices: 搜索阶段已构建的父节点到边下标映射；提供时
            避免再次构造邻接表。
        reverse_node_ids: 已按逆拓扑排序的节点编号；提供时避免重复排序。
        progress_callback: 可选阶段进度回调，依次接收阶段名、已处理量和
            总量。阶段包括 ``prepare_edges``、``node_intervals`` 和
            ``edge_intervals``。

    返回：
        根节点的 wide/narrow 区间。
    """

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
        edge_total = len(edges)
        if progress_callback is not None:
            progress_callback("prepare_edges", 0, edge_total)
        for edge_number, edge in enumerate(edges, start=1):
            parent_id = int(edge["parent_id"])
            child_id = int(edge["child_id"])
            children_by_parent.setdefault(parent_id, {}).setdefault(child_id, None)
            if (
                progress_callback is not None
                and edge_number % POSTPROCESS_PROGRESS_BATCH_SIZE == 0
            ):
                progress_callback("prepare_edges", edge_number, edge_total)
        if progress_callback is not None:
            progress_callback("prepare_edges", edge_total, edge_total)
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
    child_reference_count = 0
    if outgoing_edge_indices is not None:
        for edge_indices in outgoing_edge_indices.values():
            child_reference_count += len(edge_indices)
    else:
        for child_ids in (children_by_parent or {}).values():
            child_reference_count += len(child_ids)
    node_work_total = len(nodes) + child_reference_count
    node_work_completed = 0
    if progress_callback is not None:
        progress_callback("node_intervals", 0, node_work_total)
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
                    node_work_completed += 1
                    if (
                        progress_callback is not None
                        and node_work_completed % POSTPROCESS_PROGRESS_BATCH_SIZE == 0
                    ):
                        progress_callback(
                            "node_intervals",
                            node_work_completed,
                            node_work_total,
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
                    node_work_completed += 1
                    if (
                        progress_callback is not None
                        and node_work_completed % POSTPROCESS_PROGRESS_BATCH_SIZE == 0
                    ):
                        progress_callback(
                            "node_intervals",
                            node_work_completed,
                            node_work_total,
                        )
            values = accumulator.resolve(lambda_risk=lambda_risk)
            if values is None:
                values = (-1.0, 1.0, -1.0, 1.0)
        node["wide_interval"] = [values[0], values[1]]
        node["narrow_interval"] = [values[2], values[3]]
        node_work_completed += 1
        if (
            progress_callback is not None
            and node_work_completed % POSTPROCESS_PROGRESS_BATCH_SIZE == 0
        ):
            progress_callback(
                "node_intervals",
                node_work_completed,
                node_work_total,
            )
    if progress_callback is not None:
        progress_callback("node_intervals", node_work_total, node_work_total)

    edge_total = len(edges)
    if progress_callback is not None:
        progress_callback("edge_intervals", 0, edge_total)
    for edge_number, edge in enumerate(edges, start=1):
        child = node_for(int(edge["child_id"]))
        edge["wide_interval"] = list(child["wide_interval"])
        edge["narrow_interval"] = list(child["narrow_interval"])
        if (
            progress_callback is not None
            and edge_number % POSTPROCESS_PROGRESS_BATCH_SIZE == 0
        ):
            progress_callback("edge_intervals", edge_number, edge_total)
    if progress_callback is not None:
        progress_callback("edge_intervals", edge_total, edge_total)
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
    materialize_graph: bool = True,
    checkpoint_path: str | None = None,
    node_budget: int | None = None,
    check_system_memory: bool = True,
) -> dict[str, Any]:
    """搜索站位并回传区间，必要时在节点预算处保存可恢复检查点。

    ``check_system_memory`` 只控制当前进程是否调用系统物理内存 API。隔离
    worker 传入 ``False``，由父协调器在每个短分块边界检查；本地/API 直接
    搜索保留 ``True``。该开关不改变搜索、检查点或 interval 语义。
    """

    started_at = time.monotonic()
    if not root.position_signature:
        root_roles: list[str] = []
        for player in root.players:
            root_roles.append(player.role)
        root.position_signature = position_signature(tuple(root_roles))
    role_values: list[str] = []
    for player in root.players:
        role_values.append(player.role)
    roles = tuple(role_values)
    position_signature_value = root.position_signature
    position_labels: list[str] = []
    for index, role in enumerate(roles):
        position_labels.append(f"{index + 1}:{role}")
    position_display_value = " | ".join(position_labels)
    checkpoint_file = Path(checkpoint_path).resolve() if checkpoint_path else None
    checkpoint = (
        _load_search_checkpoint(
            checkpoint_file,
            position_signature_value=position_signature_value,
        )
        if checkpoint_file is not None
        else None
    )
    if checkpoint is None:
        root_key = simulator._state_key(root)
        nodes: list[_CompactSearchNode] = [
            _CompactSearchNode(
                state_key=root_key,
                state_signature="",
                round=root.day_count + root.night_count,
                phase=root.phase,
                day_count=root.day_count,
                night_count=root.night_count,
                observation=_state_observation(
                    root,
                    parent_node_id=None,
                    depth=0,
                    action_label=root.action_label or "根状态",
                ),
            )
        ]
        node_id_by_key: dict[bytes, int] = {root_key: 0}
        frontier: deque[int] = deque([0])
        edges = _CompactEdges()
        processed_states = 0
        terminal_count_live = 0
        focus_node_id = 0
        accumulated_runtime_seconds = 0.0
    else:
        nodes = checkpoint.nodes
        node_id_by_key = checkpoint.node_id_by_key
        frontier = checkpoint.frontier
        edges = checkpoint.edges
        processed_states = checkpoint.processed_states
        terminal_count_live = checkpoint.terminal_count
        focus_node_id = checkpoint.focus_node_id
        accumulated_runtime_seconds = checkpoint.accumulated_runtime_seconds
    chunk_start_processed_states = processed_states
    last_progress_at = started_at
    last_worker_log_at = started_at
    last_memory_check_at = 0.0
    has_progress_consumer = (
        getattr(simulator, "progress_queue", None) is not None
        or getattr(simulator, "iteration_callback", None) is not None
    )
    pending_preview_node_ids: deque[int] = deque(
        maxlen=PREVIEW_NODE_BATCH_LIMIT * 2
    )
    pending_preview_edge_indices: deque[int] = deque(
        maxlen=PREVIEW_EDGE_BATCH_LIMIT * 2
    )
    stream_staged_edges = (
        getattr(simulator, "result_queue", None) is not None
        and checkpoint_file is not None
    )
    staged_edge_cursor = len(edges)

    def flush_staged_edges() -> None:
        """只在 checkpoint 边界批量落新边，保证 SQLite 与快照一致。"""

        nonlocal staged_edge_cursor
        if not stream_staged_edges or staged_edge_cursor >= len(edges):
            return
        while staged_edge_cursor < len(edges):
            stop = min(
                staged_edge_cursor + PERSISTENCE_BATCH_SIZE,
                len(edges),
            )
            items: list[dict[str, Any]] = []
            for edge_index in range(staged_edge_cursor, stop):
                items.append(
                    _compact_edge_to_dict(
                        simulator,
                        edges=edges,
                        edge_index=edge_index,
                        nodes=nodes,
                    )
                )
                edges.pending_reasons.pop(edge_index, None)
            simulator.result_queue.put(
                {
                    "kind": "position_stage_edges",
                    "position_signature": position_signature_value,
                    "items": items,
                }
            )
            staged_edge_cursor = stop

    def ensure_search_memory_available(*, force: bool = False) -> float:
        """在节点展开过程中定期检查内存，并返回当前单调时钟。"""

        nonlocal last_memory_check_at
        now = time.monotonic()
        if not check_system_memory:
            return now
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

    def node_preview(node_id: int) -> dict[str, Any]:
        node = nodes[node_id]
        return {
            "node_id": node_id,
            "phase": node.phase,
            "day_count": node.day_count,
            "night_count": node.night_count,
            "is_terminal": node.is_terminal,
            "result": node.result,
            "expanded": node.expanded,
            "wide_interval": [-1.0, 1.0],
            "narrow_interval": [-1.0, 1.0],
            "state": game_state_dict_from_compact(
                node.state_key,
                roles=roles,
                position_signature=position_signature_value,
                is_game_over=node.is_terminal,
                state_id=node_id,
                observation=node.observation,
            ),
            "live_preview": True,
        }

    def latest_node_previews() -> list[dict[str, Any]]:
        """只在 0.5 秒发布边界重建最近节点的完整预览。"""

        unique_node_ids = list(dict.fromkeys(pending_preview_node_ids))[
            -PREVIEW_NODE_BATCH_LIMIT:
        ]
        return [node_preview(node_id) for node_id in unique_node_ids]

    if has_progress_consumer:
        pending_preview_node_ids.append(focus_node_id)

    def publish_progress(*, kind: str = "node_progress", force: bool = False) -> None:
        """按 0.5 秒发布预览，并以同一周期执行内存安全检查。"""

        nonlocal last_progress_at, last_worker_log_at
        now = ensure_search_memory_available(force=force)
        if not force and now - last_progress_at < PREVIEW_EMIT_INTERVAL_SECONDS:
            return
        next_node_id = None
        if frontier:
            next_node_id = (
                frontier[0] if simulator.search_mode == "bfs" else frontier[-1]
            )
        if has_progress_consumer:
            preview_nodes = latest_node_previews()
            preview_edges: list[dict[str, Any]] = []
            recent_edge_indices = list(pending_preview_edge_indices)[
                -PREVIEW_EDGE_BATCH_LIMIT:
            ]
            for edge_index in recent_edge_indices:
                preview_edge = _compact_edge_to_dict(
                    simulator,
                    edges=edges,
                    edge_index=edge_index,
                    nodes=nodes,
                )
                # 实时预览仍受边批次上限约束，但不能截断或丢弃派生原因；
                # 边 hover 必须与持久化 DAG 使用同一完整原因集合。
                preview_edge["action_label"] = str(preview_edge["action_label"])
                preview_edge["live_preview"] = True
                preview_edges.append(preview_edge)
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
                "phase": nodes[next_node_id].phase
                if next_node_id is not None
                else "complete",
                "focus_node_id": focus_node_id,
                "preview_nodes": preview_nodes,
                "preview_edges": preview_edges,
                "runtime_seconds": accumulated_runtime_seconds + now - started_at,
                },
            )
        pending_preview_node_ids.clear()
        pending_preview_edge_indices.clear()
        if force or now - last_worker_log_at >= WORKER_PROGRESS_LOG_INTERVAL_SECONDS:
            logger.info(
                "WORKER_PROGRESS pid=%s position=%s processed=%s discovered=%s "
                "frontier=%s edges=%s terminals=%s runtime_seconds=%.3f",
                os.getpid(),
                position_index,
                processed_states,
                len(nodes),
                len(frontier),
                len(edges),
                terminal_count_live,
                accumulated_runtime_seconds + now - started_at,
            )
            last_worker_log_at = now
        last_progress_at = now

    publish_progress(kind="position_started", force=True)

    while frontier:
        _wait_until_resumed(simulator)
        if (
            checkpoint_file is not None
            and node_budget is not None
            and processed_states - chunk_start_processed_states
            >= max(1, int(node_budget))
        ):
            flush_staged_edges()
            checkpoint_runtime = (
                accumulated_runtime_seconds + time.monotonic() - started_at
            )
            _save_search_checkpoint(
                checkpoint_file,
                _SearchCheckpoint(
                    version=SEARCH_CHECKPOINT_VERSION,
                    position_signature=position_signature_value,
                    nodes=nodes,
                    node_id_by_key=node_id_by_key,
                    frontier=frontier,
                    edges=edges,
                    processed_states=processed_states,
                    terminal_count=terminal_count_live,
                    focus_node_id=focus_node_id,
                    accumulated_runtime_seconds=checkpoint_runtime,
                ),
            )
            publish_progress(kind="worker_checkpoint", force=True)
            logger.info(
                "WORKER_CHECKPOINT pid=%s position=%s processed=%s "
                "discovered=%s frontier=%s edges=%s path=%s",
                os.getpid(),
                position_index,
                processed_states,
                len(nodes),
                len(frontier),
                len(edges),
                checkpoint_file,
            )
            return {
                "position_index": position_index,
                "position_signature": position_signature_value,
                "chunk_incomplete": True,
                "checkpoint_path": str(checkpoint_file),
                "processed_states": processed_states,
                "state_count": len(nodes),
                "edge_count": len(edges),
                "frontier_size": len(frontier),
                "runtime_seconds": checkpoint_runtime,
            }
        node_id = (
            frontier.popleft() if simulator.search_mode == "bfs" else frontier.pop()
        )
        node = nodes[node_id]
        state = simulator._state_from_key(
            node.state_key,
            roles=roles,
            position_signature_value=position_signature_value,
        )
        state.state_id = node_id
        state.depth = node.round
        is_terminal, result = simulator._check_game_over(state)
        node.is_terminal = is_terminal
        node.result = result
        node.expanded = True
        focus_node_id = node_id
        if has_progress_consumer:
            pending_preview_node_ids.append(node_id)
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
            if child_id is None:
                child_id = len(nodes)
                node_id_by_key[child_key] = child_id
                action_label = simulator._action_label(transition.action_key)
                nodes.append(
                    _CompactSearchNode(
                        state_key=child_key,
                        state_signature="",
                        round=child.day_count + child.night_count,
                        phase=child.phase,
                        day_count=child.day_count,
                        night_count=child.night_count,
                        observation=_state_observation(
                            child,
                            parent_node_id=node_id,
                            depth=node.round + 1,
                            action_label=action_label,
                        ),
                    )
                )
                if has_progress_consumer:
                    pending_preview_node_ids.append(child_id)
                frontier.append(child_id)

            edge_data = local_edges.setdefault(
                child_id,
                {"multiplicity": 0, "reasons": {}},
            )
            edge_data["multiplicity"] += int(transition.multiplicity)
            reason_multiplicity = edge_data["reasons"].get(
                transition.action_key,
                0,
            )
            edge_data["reasons"][transition.action_key] = (
                reason_multiplicity + int(transition.multiplicity)
            )

        if transition_count == 0:
            raise RuntimeError(
                "非终局状态没有合法分支: "
                f"position={position_signature_value}, node={node_id}, phase={state.phase}"
            )

        node.outgoing_start = len(edges)
        for child_id, edge_data in local_edges.items():
            edge_index = edges.append(
                parent_id=node_id,
                child_id=child_id,
                multiplicity=int(edge_data["multiplicity"]),
                reasons=tuple(edge_data["reasons"].items()),
            )
            if has_progress_consumer:
                pending_preview_edge_indices.append(edge_index)
        node.outgoing_count = len(edges) - node.outgoing_start
        publish_progress()

    flush_staged_edges()
    publish_progress(force=True)

    # 搜索已经完成，状态去重索引与实时预览缓冲不会再被读取。先释放这些
    # 大对象，给路径/interval 回传留下稳定的内存余量。
    del node_id_by_key
    pending_preview_node_ids.clear()
    pending_preview_edge_indices.clear()

    reverse_topological_ids = list(range(len(nodes)))
    reverse_topological_ids.sort(
        key=lambda item: (nodes[item].round, item), reverse=True
    )
    ensure_search_memory_available(force=True)
    last_postprocess_progress_at = 0.0

    def publish_postprocess_progress(
        *,
        kind: str,
        stage: str,
        completed: int,
        total: int,
        force: bool = False,
    ) -> None:
        """把固定 DAG 后处理阶段按 0.5 秒节流发送给 GUI。"""

        nonlocal last_postprocess_progress_at
        _wait_until_resumed(simulator)
        now = ensure_search_memory_available(force=force)
        if (
            not force
            and now - last_postprocess_progress_at
            < PREVIEW_EMIT_INTERVAL_SECONDS
        ):
            return
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
                "frontier_size": 0,
                "edge_count": len(edges),
                "terminal_count": terminal_count_live,
                "postprocess_stage": stage,
                "postprocess_completed": int(completed),
                "postprocess_total": int(total),
                "runtime_seconds": accumulated_runtime_seconds + now - started_at,
            },
        )
        last_postprocess_progress_at = now

    path_total = len(reverse_topological_ids)
    publish_postprocess_progress(
        kind="path_progress",
        stage="path_counts",
        completed=0,
        total=path_total,
        force=True,
    )
    for path_number, node_id in enumerate(reverse_topological_ids, start=1):
        node = nodes[node_id]
        if node.is_terminal:
            node.good_paths = int("好人" in node.result)
            node.wolf_paths = int("狼人" in node.result)
        else:
            good_paths = 0
            wolf_paths = 0
            edge_stop = node.outgoing_start + node.outgoing_count
            for edge_index in range(node.outgoing_start, edge_stop):
                child = nodes[int(edges.child_ids[edge_index])]
                multiplicity = int(edges.multiplicities[edge_index])
                good_paths += multiplicity * child.good_paths
                wolf_paths += multiplicity * child.wolf_paths
            node.good_paths = good_paths
            node.wolf_paths = wolf_paths

        if path_number % POSTPROCESS_PROGRESS_BATCH_SIZE == 0:
            ensure_search_memory_available()
            publish_postprocess_progress(
                kind="path_progress",
                stage="path_counts",
                completed=path_number,
                total=path_total,
            )

    publish_postprocess_progress(
        kind="path_progress",
        stage="path_counts",
        completed=path_total,
        total=path_total,
        force=True,
    )

    ensure_search_memory_available(force=True)

    def publish_interval_progress(stage: str, completed: int, total: int) -> None:
        force = completed == 0 or completed >= total
        publish_postprocess_progress(
            kind="interval_progress",
            stage=stage,
            completed=completed,
            total=total,
            force=force,
        )

    interval_work_total = len(nodes) + len(edges)
    interval_work_completed = 0
    publish_interval_progress("node_intervals", 0, interval_work_total)
    for node_id in reverse_topological_ids:
        node = nodes[node_id]
        if node.is_terminal:
            if "好人" in node.result:
                values = (1.0, 1.0, 1.0, 1.0)
            elif "狼人" in node.result:
                values = (-1.0, -1.0, -1.0, -1.0)
            else:
                values = (-1.0, 1.0, -1.0, 1.0)
        else:
            accumulator = _IntervalValueAccumulator()
            edge_stop = node.outgoing_start + node.outgoing_count
            for edge_index in range(node.outgoing_start, edge_stop):
                child = nodes[int(edges.child_ids[edge_index])]
                accumulator.add(
                    child.wide_lower,
                    child.wide_upper,
                    child.narrow_lower,
                    child.narrow_upper,
                )
                interval_work_completed += 1
                if interval_work_completed % POSTPROCESS_PROGRESS_BATCH_SIZE == 0:
                    publish_interval_progress(
                        "node_intervals",
                        interval_work_completed,
                        interval_work_total,
                    )
            values = accumulator.resolve(lambda_risk=simulator.lambda_risk)
            if values is None:
                values = (-1.0, 1.0, -1.0, 1.0)
        (
            node.wide_lower,
            node.wide_upper,
            node.narrow_lower,
            node.narrow_upper,
        ) = values
        interval_work_completed += 1
        if interval_work_completed % POSTPROCESS_PROGRESS_BATCH_SIZE == 0:
            publish_interval_progress(
                "node_intervals",
                interval_work_completed,
                interval_work_total,
            )
    publish_interval_progress(
        "node_intervals",
        interval_work_total,
        interval_work_total,
    )
    publish_interval_progress("edge_intervals", 0, len(edges))
    for edge_number in range(1, len(edges) + 1):
        if edge_number % POSTPROCESS_PROGRESS_BATCH_SIZE == 0:
            publish_interval_progress("edge_intervals", edge_number, len(edges))
    publish_interval_progress("edge_intervals", len(edges), len(edges))
    ensure_search_memory_available(force=True)

    root_node = nodes[0]
    root_robust = RobustIntervals(
        wide=RewardInterval(root_node.wide_lower, root_node.wide_upper),
        narrow=RewardInterval(root_node.narrow_lower, root_node.narrow_upper),
    )
    del reverse_topological_ids
    result: dict[str, Any] = {
        "position_index": position_index,
        "position_signature": position_signature_value,
        "roles": list(roles),
        "position_display": position_display_value,
        "search_mode": simulator.search_mode,
        "state_count": len(nodes),
        "edge_count": len(edges),
        "terminal_count": terminal_count_live,
        "processed_states": processed_states,
        "good_paths": root_node.good_paths,
        "wolf_paths": root_node.wolf_paths,
        "wide_interval": root_robust.wide.to_list(),
        "narrow_interval": root_robust.narrow.to_list(),
        "camp": interval_camp(root_robust.wide),
        "runtime_seconds": (
            accumulated_runtime_seconds + time.monotonic() - started_at
        ),
    }
    if materialize_graph:
        result["nodes"] = [
            _compact_node_to_dict(
                simulator,
                node,
                node_id=node_id,
                position_signature_value=position_signature_value,
            )
            for node_id, node in enumerate(nodes)
        ]
        result["edges"] = [
            _compact_edge_to_dict(
                simulator,
                edges=edges,
                edge_index=edge_index,
                nodes=nodes,
            )
            for edge_index in range(len(edges))
        ]
    else:
        result["_compact_nodes"] = nodes
        result["_compact_edges"] = edges
        result["_edges_staged"] = stream_staged_edges
    return result


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

    # 检查点 DAG 没有引用环，且 worker 每个分块后必然退出。关闭自动循环
    # GC 可避免其反复扫描数十万个 slotted 节点和邻接容器；普通引用计数仍
    # 正常工作，进程退出则回收全部剩余页。Windows 压力栈显示故障阈值会
    # 随已加载容器数增加而下降，GC 扫描是需要隔离的解释器热路径。
    gc.disable()

    # worker 的 Python 异常由父进程记录；若发生 access violation 等 C 级
    # 崩溃，则由本进程 faulthandler 直接写入独立 crash 日志。
    worker_crash_path: Any = "unavailable"
    try:
        from ._crash_handler import install_crash_handlers
        from ._runtime_logging import configure_runtime_logging

        configure_runtime_logging()
        worker_crash_path = install_crash_handlers()
    except Exception:
        logger.exception("WORKER_CRASH_HANDLER_INSTALL_FAILED pid=%s", os.getpid())

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
    if os.name == "nt" and not sys.flags.no_site:
        raise RuntimeError("Windows 计算 worker 必须使用 -S 隔离启动")
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
    logger.info(
        "WORKER_STARTED pid=%s position=%s/%s signature=%s mode=%s "
        "python=%s executable=%s no_site=%s allocator=%s crash_log=%s",
        os.getpid(),
        layout.index,
        payload.get("total_positions", 1),
        layout.signature,
        simulator.search_mode,
        sys.version.split()[0],
        sys.executable,
        sys.flags.no_site,
        os.environ.get("PYTHONMALLOC", "default"),
        worker_crash_path,
    )
    logger.info(
        "WORKER_RUNTIME_GUARD pid=%s cyclic_gc_enabled=%s",
        os.getpid(),
        gc.isenabled(),
    )
    result_queue = payload.get("result_queue")
    result = _search_root(
        simulator,
        simulator.initial_state_for_layout(layout),
        position_index=layout.index,
        total_positions=int(payload.get("total_positions", 1)),
        materialize_graph=result_queue is None,
        checkpoint_path=str(payload["checkpoint_path"]),
        node_budget=int(payload["node_budget"]),
        check_system_memory=bool(payload["check_system_memory"]),
    )
    if result_queue is None:
        return result
    if result.get("chunk_incomplete"):
        return result

    summary = {
        key: value
        for key, value in result.items()
        if key not in {"_compact_nodes", "_compact_edges", "_edges_staged"}
    }
    position_signature_value = str(summary["position_signature"])
    result_queue.put({"kind": "position_begin", "summary": summary})
    compact_nodes = result["_compact_nodes"]
    compact_edges = result["_compact_edges"]
    for start in range(0, len(compact_nodes), PERSISTENCE_BATCH_SIZE):
        node_items: list[dict[str, Any]] = []
        stop = min(start + PERSISTENCE_BATCH_SIZE, len(compact_nodes))
        for node_id in range(start, stop):
            node_items.append(
                _compact_node_to_dict(
                    simulator,
                    compact_nodes[node_id],
                    node_id=node_id,
                    position_signature_value=position_signature_value,
                )
            )
        result_queue.put(
            {
                "kind": "position_nodes",
                "position_signature": position_signature_value,
                "items": node_items,
            }
        )
    if not result["_edges_staged"]:
        for start in range(0, len(compact_edges), PERSISTENCE_BATCH_SIZE):
            edge_items: list[dict[str, Any]] = []
            stop = min(start + PERSISTENCE_BATCH_SIZE, len(compact_edges))
            for edge_index in range(start, stop):
                edge_items.append(
                    _compact_edge_to_dict(
                        simulator,
                        edges=compact_edges,
                        edge_index=edge_index,
                        nodes=compact_nodes,
                    )
                )
            result_queue.put(
                {
                    "kind": "position_edges",
                    "position_signature": position_signature_value,
                    "items": edge_items,
                }
            )
    else:
        result_queue.put(
            {
                "kind": "position_sync_edge_intervals",
                "position_signature": position_signature_value,
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


def _loaded_solution_summary(
    simulator: Any,
    *,
    config: dict[str, Any],
    loaded: dict[str, Any],
    layouts: list[PositionLayout],
    started_at: float,
) -> dict[str, Any]:
    """将持久化 solution 摘要恢复成与正常运行一致的结果对象。"""

    ordered = sorted(
        loaded["positions"],
        key=lambda item: int(item["position_index"]),
    )
    position_intervals: list[RobustIntervals] = []
    for item in ordered:
        position_intervals.append(
            RobustIntervals(
                wide=RewardInterval(*item["wide_interval"]),
                narrow=RewardInterval(*item["narrow_interval"]),
            )
        )
    aggregate = (
        propagate_intervals(
            position_intervals,
            lambda_risk=(
                float(loaded["source_lambda"])
                if loaded.get("source_lambda") is not None
                else simulator.lambda_risk
            ),
        )
        if ordered
        else UNRESOLVED
    )
    return {
        "run_id": str(loaded["run_id"]),
        "status": "complete",
        "loaded_solution": True,
        "solution_source_run_id": str(loaded["run_id"]),
        "config": config,
        "position_count": len(ordered),
        "total_position_count": len(layouts),
        "next_position_index": None,
        "good_paths": sum([int(item["good_paths"]) for item in ordered]),
        "wolf_paths": sum([int(item["wolf_paths"]) for item in ordered]),
        "wide_interval": aggregate.wide.to_list(),
        "narrow_interval": aggregate.narrow.to_list(),
        "camp": interval_camp(aggregate.wide),
        "processed_states": sum([int(item["processed_states"]) for item in ordered]),
        "runtime_seconds": time.monotonic() - started_at,
        "position_runtime_seconds": sum(
            [float(item["runtime_seconds"]) for item in ordered]
        ),
        "resumed_run": False,
        "discarded_incomplete_positions": 0,
        "positions": ordered,
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
    expected_signatures = {layout.signature for layout in layouts}
    if not simulator.force_recompute:
        loaded_solution = store.load_solution(
            config,
            expected_position_signatures=expected_signatures,
        )
        if loaded_solution is not None:
            summary = _loaded_solution_summary(
                simulator,
                config=config,
                loaded=loaded_solution,
                layouts=layouts,
                started_at=started_at,
            )
            simulator.run_id = str(loaded_solution["run_id"])
            simulator.position_results = list(summary["positions"])
            simulator.processed_positions = int(summary["position_count"])
            simulator.processed_states = int(summary["processed_states"])
            logger.info(
                "SOLUTION_LOADED pid=%s run_id=%s positions=%s/%s",
                os.getpid(),
                simulator.run_id,
                summary["position_count"],
                summary["total_position_count"],
            )
            return summary
    simulator.run_id, resumed_run = store.start_or_resume_run(
        config,
        force_new=simulator.force_recompute,
    )
    resumed_node_budgets: dict[int, int] = {}
    if resumed_run:
        memory_record = store.get_memory_run(simulator.run_id)
        memory_summary = (
            memory_record.get("summary")
            if memory_record is not None
            else None
        )
        in_position = (
            memory_summary.get("in_position_checkpoint")
            if isinstance(memory_summary, dict)
            else None
        )
        if isinstance(in_position, dict):
            saved_budget = in_position.get("worker_node_budget")
            saved_position = in_position.get("position_index")
            if saved_budget is not None and saved_position is not None:
                resumed_node_budgets[int(saved_position)] = max(
                    WORKER_MIN_NODE_BUDGET,
                    min(WORKER_NODE_BUDGET, int(saved_budget)),
                )
    checkpoint_directory = (
        Path(simulator.signature_cache_db_path).resolve().parent
        / ".search_simulator_checkpoints"
    )
    checkpoint_signatures = {
        layout.signature
        for layout in layouts
        if (
            checkpoint_directory
            / f"{simulator.run_id}_position_{layout.index}.pickle"
        ).exists()
    }
    discarded_incomplete_positions = store.discard_incomplete_position_results(
        simulator.run_id,
        preserve_signatures=checkpoint_signatures,
    )
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
        [int(item["processed_states"]) for item in summaries]
    )
    pending_layouts = [
        layout
        for layout in layouts
        if layout.signature not in completed_signatures
    ]
    logger.info(
        "RUN_STARTED pid=%s status=running run_id=%s resumed=%s checkpoints=%s/%s "
        "next_position=%s mode=%s",
        os.getpid(),
        simulator.run_id,
        resumed_run,
        len(summaries),
        len(layouts),
        pending_layouts[0].index if pending_layouts else "none",
        simulator.search_mode,
    )
    owned_result_manager = None
    if simulator.result_queue is None:
        owned_result_manager = multiprocessing.Manager()
        simulator.result_queue = owned_result_manager.Queue(maxsize=8)
    worker_config = _worker_config(simulator)
    def payload_for(layout: PositionLayout) -> dict[str, Any]:
        """只为即将执行的一个站位构造任务，禁止批量预取。"""

        checkpoint_path = checkpoint_directory / (
            f"{simulator.run_id}_position_{layout.index}.pickle"
        )
        return {
            "simulator_config": worker_config,
            "progress_queue": simulator.progress_queue,
            "result_queue": simulator.result_queue,
            "resume_event": simulator.resume_event,
            "total_positions": len(layouts),
            "checkpoint_path": str(checkpoint_path),
            "node_budget": resumed_node_budgets.get(
                layout.index,
                WORKER_NODE_BUDGET,
            ),
            # 父协调器在每个短分块前后检查系统内存；计算 worker 不再跨
            # ctypes ABI，避免原生守卫与大型 Python 堆同时处于热路径。
            "check_system_memory": False,
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
        total_good = sum([int(item["good_paths"]) for item in ordered])
        total_wolf = sum([int(item["wolf_paths"]) for item in ordered])
        position_intervals: list[RobustIntervals] = []
        for item in ordered:
            position_intervals.append(
                RobustIntervals(
                    wide=RewardInterval(*item["wide_interval"]),
                    narrow=RewardInterval(*item["narrow_interval"]),
                )
            )
        aggregate = (
            propagate_intervals(
                position_intervals,
                lambda_risk=simulator.lambda_risk,
            )
            if ordered
            else UNRESOLVED
        )
        done_signatures = {
            str(item["position_signature"])
            for item in ordered
        }
        next_position_index = None
        for layout in layouts:
            if layout.signature not in done_signatures:
                next_position_index = layout.index
                break
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
                [int(item["processed_states"]) for item in ordered]
            ),
            "runtime_seconds": time.monotonic() - started_at,
            "position_runtime_seconds": sum(
                [float(item["runtime_seconds"]) for item in ordered]
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
        logger.info(
            "POSITION_CHECKPOINT pid=%s run_id=%s position=%s checkpoints=%s/%s "
            "next_position=%s states=%s edges=%s",
            os.getpid(),
            simulator.run_id,
            summary["position_index"],
            checkpoint["position_count"],
            checkpoint["total_position_count"],
            checkpoint["next_position_index"] or "none",
            summary["state_count"],
            summary["edge_count"],
        )
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
    staged_streams: set[str] = set(checkpoint_signatures)

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
                if kind == "position_stage_begin":
                    summary = message["summary"]
                    position_signature_value = str(summary["position_signature"])
                    store.begin_position_staging(simulator.run_id, summary)
                    staged_streams.add(position_signature_value)
                elif kind == "position_stage_edges":
                    store.append_position_edges(
                        simulator.run_id,
                        position_signature_value,
                        message["items"],
                    )
                elif kind == "position_begin":
                    summary = message["summary"]
                    position_signature_value = str(summary["position_signature"])
                    if position_signature_value in staged_streams:
                        store.update_position_result_summary(
                            simulator.run_id,
                            summary,
                        )
                    else:
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
                elif kind == "position_sync_edge_intervals":
                    store.sync_position_edge_intervals(
                        simulator.run_id,
                        position_signature_value,
                    )
                elif kind == "position_end":
                    store.finish_position_result()
                    active_streams.discard(position_signature_value)
                    staged_streams.discard(position_signature_value)
                    record_summary(stream_summaries.pop(position_signature_value))
            except BaseException as exc:
                writer_errors.append(exc)
                failed = True
                with persisted_condition:
                    persisted_condition.notify_all()
        # 中断时保留暂存图；无对应 checkpoint 的残片会在下次启动时由
        # discard_incomplete_position_results 统一清理。

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
        with _isolated_compute_worker_spawn():
            for layout in pending_layouts:
                # 只有前一站位已经通过 writer 的数量完整性事务并唤醒父进程，
                # 循环才会走到下一次 submit；因此不存在并发展开的站位。
                ensure_memory_available()
                payload = payload_for(layout)
                checkpoint_path = Path(payload["checkpoint_path"])
                if (
                    simulator.result_queue is not None
                    and not checkpoint_path.exists()
                ):
                    simulator.result_queue.put(
                        {
                            "kind": "position_stage_begin",
                            "summary": {
                                "position_index": layout.index,
                                "position_signature": layout.signature,
                                "roles": list(layout.roles),
                            },
                        }
                    )
                chunk_budget = int(payload["node_budget"])
                consecutive_worker_failures = 0
                while True:
                    payload["node_budget"] = chunk_budget
                    try:
                        # 每个分块拥有独立进程池。原生崩溃会破坏当前 pool，
                        # 但不会污染父协调器或上一个原子检查点。
                        with ProcessPoolExecutor(
                            max_workers=1,
                            max_tasks_per_child=1,
                        ) as executor:
                            result = executor.submit(
                                _position_task,
                                payload,
                            ).result()
                    except MemoryPressureInterrupt:
                        raise
                    except Exception as exc:
                        consecutive_worker_failures += 1
                        if consecutive_worker_failures > WORKER_CHUNK_RETRY_LIMIT:
                            logger.critical(
                                "WORKER_CHUNK_RETRY_EXHAUSTED pid=%s run_id=%s "
                                "position=%s attempts=%s budget=%s checkpoint=%s",
                                os.getpid(),
                                simulator.run_id,
                                layout.index,
                                consecutive_worker_failures,
                                chunk_budget,
                                checkpoint_path,
                                exc_info=True,
                            )
                            raise
                        next_budget = max(
                            WORKER_MIN_NODE_BUDGET,
                            chunk_budget // 2,
                        )
                        logger.critical(
                            "WORKER_CHUNK_RETRY pid=%s run_id=%s position=%s "
                            "attempt=%s/%s error_type=%s budget=%s->%s "
                            "checkpoint=%s",
                            os.getpid(),
                            simulator.run_id,
                            layout.index,
                            consecutive_worker_failures,
                            WORKER_CHUNK_RETRY_LIMIT,
                            type(exc).__name__,
                            chunk_budget,
                            next_budget,
                            checkpoint_path,
                            exc_info=True,
                        )
                        chunk_budget = next_budget
                        ensure_memory_available()
                        continue
                    consecutive_worker_failures = 0
                    if not result.get("chunk_incomplete"):
                        break
                    checkpoint_summary = build_run_summary(status="running")
                    checkpoint_summary["next_position_index"] = layout.index
                    checkpoint_summary["in_position_checkpoint"] = {
                        "position_index": layout.index,
                        "position_signature": layout.signature,
                        "checkpoint_path": result["checkpoint_path"],
                        "processed_states": result["processed_states"],
                        "state_count": result["state_count"],
                        "edge_count": result["edge_count"],
                        "frontier_size": result["frontier_size"],
                        "runtime_seconds": result["runtime_seconds"],
                        "worker_node_budget": chunk_budget,
                    }
                    store.checkpoint_run(
                        simulator.run_id,
                        checkpoint_summary,
                        status="running",
                    )
                    logger.info(
                        "RUN_CHUNK_CHECKPOINT pid=%s run_id=%s position=%s "
                        "processed=%s states=%s edges=%s frontier=%s budget=%s path=%s",
                        os.getpid(),
                        simulator.run_id,
                        layout.index,
                        result["processed_states"],
                        result["state_count"],
                        result["edge_count"],
                        result["frontier_size"],
                        chunk_budget,
                        result["checkpoint_path"],
                    )
                    ensure_memory_available()
                if simulator.result_queue is None:
                    persist(result)
                else:
                    signature = str(result["position_signature"])
                    with persisted_condition:
                        while signature not in persisted_signatures:
                            if writer_errors:
                                raise writer_errors[0]
                            persisted_condition.wait(timeout=0.1)
                _remove_search_checkpoint(Path(payload["checkpoint_path"]))
                ensure_memory_available()

        stop_writer()
        if writer_errors:
            raise writer_errors[0]
        summary = build_run_summary(status="complete")
        if summary["position_count"] != summary["total_position_count"]:
            raise RuntimeError(
                "完整运行终态校验失败："
                f"checkpoints={summary['position_count']}/"
                f"{summary['total_position_count']}"
            )
        simulator.position_results = list(summary["positions"])
        store.finish_run(simulator.run_id, summary, status="complete")
        logger.info(
            "RUN_TERMINAL pid=%s status=complete run_id=%s checkpoints=%s/%s "
            "next_position=none states=%s runtime_seconds=%.3f",
            os.getpid(),
            simulator.run_id,
            summary["position_count"],
            summary["total_position_count"],
            summary["processed_states"],
            summary["runtime_seconds"],
        )
        return summary
    except MemoryPressureInterrupt as exc:
        stop_writer()
        summary = build_run_summary(
            status="interrupted",
            interruption_reason=str(exc),
        )
        simulator.position_results = list(summary["positions"])
        simulator.stop_reason = str(exc)
        logger.warning(
            "RUN_TERMINAL pid=%s status=interrupted reason=memory_guard run_id=%s "
            "checkpoints=%s/%s next_position=%s detail=%s",
            os.getpid(),
            simulator.run_id,
            summary["position_count"],
            summary["total_position_count"],
            summary["next_position_index"] or "none",
            exc,
        )
        try:
            store.finish_run(simulator.run_id, summary, status="interrupted")
        except Exception:
            logger.critical(
                "RUN_STATUS_PERSIST_FAILED pid=%s intended_status=interrupted "
                "run_id=%s",
                os.getpid(),
                simulator.run_id,
                exc_info=True,
            )
            raise
        return summary
    except KeyboardInterrupt as exc:
        stop_writer()
        summary = build_run_summary(
            status="interrupted",
            interruption_reason="用户中断运行",
        )
        simulator.position_results = list(summary["positions"])
        logger.warning(
            "RUN_TERMINAL pid=%s status=interrupted reason=user_interrupt run_id=%s "
            "checkpoints=%s/%s next_position=%s",
            os.getpid(),
            simulator.run_id,
            summary["position_count"],
            summary["total_position_count"],
            summary["next_position_index"] or "none",
        )
        try:
            store.finish_run(simulator.run_id, summary, status="interrupted")
        except Exception:
            logger.critical(
                "RUN_STATUS_PERSIST_FAILED pid=%s intended_status=interrupted "
                "run_id=%s",
                os.getpid(),
                simulator.run_id,
                exc_info=True,
            )
            raise
        raise exc
    except Exception as exc:
        stop_writer()
        failed_summary = build_run_summary(status="failed")
        failed_summary.update(
            {"error_type": type(exc).__name__, "error": str(exc)}
        )
        # 把运行上下文附加到原异常，GUI 后台线程无需访问已失败的模拟器
        # 也能显示 run_id、检查点进度和下一恢复站位。
        for attribute, value in (
            ("run_id", simulator.run_id),
            ("completed_positions", failed_summary["position_count"]),
            ("total_positions", failed_summary["total_position_count"]),
            ("next_position_index", failed_summary["next_position_index"]),
        ):
            try:
                setattr(exc, attribute, value)
            except (AttributeError, TypeError):
                pass
        worker_crash = isinstance(exc, BrokenProcessPool)
        failure_category = "worker_crash" if worker_crash else "python_exception"
        from ._crash_handler import record_caught_failure

        record_caught_failure(
            exc,
            category=failure_category,
            context={
                "run_id": simulator.run_id,
                "checkpoints": (
                    f"{failed_summary['position_count']}/"
                    f"{failed_summary['total_position_count']}"
                ),
                "next_position": failed_summary["next_position_index"] or "none",
                "error_type": type(exc).__name__,
            },
        )
        logger.log(
            logging.CRITICAL if worker_crash else logging.ERROR,
            "RUN_TERMINAL pid=%s status=failed category=%s run_id=%s checkpoints=%s/%s "
            "next_position=%s error_type=%s error=%s",
            os.getpid(),
            failure_category,
            simulator.run_id,
            failed_summary["position_count"],
            failed_summary["total_position_count"],
            failed_summary["next_position_index"] or "none",
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        try:
            store.finish_run(
                simulator.run_id,
                failed_summary,
                status="failed",
            )
        except Exception:
            logger.critical(
                "RUN_STATUS_PERSIST_FAILED pid=%s intended_status=failed run_id=%s",
                os.getpid(),
                simulator.run_id,
                exc_info=True,
            )
        raise
    finally:
        if owned_result_manager is not None:
            owned_result_manager.shutdown()
            simulator.result_queue = None
