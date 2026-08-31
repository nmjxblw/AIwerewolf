"""精确信念 Cheap-talk 决策收益矩阵协调器。

本模块负责请求规范化、受限多进程 Monte Carlo、单写线程聚合和结果输出；
所有轨迹细节留在 ``_decision_matrix_worker``，LLM 不在调用链中出现。
"""

from __future__ import annotations

import gc
import hashlib
import json
import logging
import multiprocessing
import queue
import threading
import time
from dataclasses import dataclass
from dataclasses import field
from math import sqrt
from pathlib import Path
from typing import Any
from typing import Callable

from ._decision_matrix_store import DecisionMatrixStore
from ._decision_matrix_worker import run_matrix_batch
from ._decision_matrix_worker import worker_loop
from ._decision_state import CanonicalGameConfig
from ._decision_state import DecisionState
from ._decision_state import WorldState
from ._decision_state import is_wolf_role
from ._i18n import t
from ._memory_guard import memory_pressure_snapshot
from ._positions import PositionLayout
from ._positions import build_role_roster
from ._positions import enumerate_position_layouts
from ._role_view import RoleView
from ._role_view import build_role_view
from ._role_view import posterior_digest
from ._role_view import posterior_layouts
from ._speech_action import SpeechPlan
from ._speech_action import enumerate_speech_actions

logger = logging.getLogger(__name__)


def _digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=16).hexdigest()


@dataclass(frozen=True, slots=True)
class DecisionMatrixRequest:
    """一次矩阵计算的完整规范身份。"""

    config: CanonicalGameConfig
    decision_state: DecisionState
    actor_id: int
    actor_role: str
    role_view: RoleView
    candidate_actions: tuple[SpeechPlan, ...]
    credibility_levels: tuple[float, ...] = (0.0, 0.5, 0.8)
    policy_temperature: float = 0.25
    samples_per_cell: int = 100
    seed_scheme: str = "indexed-common-random-numbers"
    base_seed: int = 7
    policy_spec: str = "utility-ranked-rollout-policy"
    candidate_spec: str = "structured-cheap-talk-actions"

    def __post_init__(self) -> None:
        if int(self.actor_id) != int(self.role_view.actor_id):
            raise ValueError("actor_id 与 role_view.actor_id 不一致")
        if str(self.actor_role) != str(self.role_view.actor_role):
            raise ValueError("actor_role 与 role_view.actor_role 不一致")
        if not self.candidate_actions or self.candidate_actions[0].family != "baseline":
            raise ValueError("candidate_actions 必须以 baseline 开始")
        if abs(float(self.policy_temperature) - 0.25) > 1e-12:
            raise ValueError("policy_temperature 必须固定为 0.25")
        if int(self.samples_per_cell) <= 0:
            raise ValueError("samples_per_cell 必须为正数")
        levels = tuple(float(value) for value in self.credibility_levels)
        if levels != (0.0, 0.5, 0.8):
            raise ValueError("credibility_levels 必须固定为 (0.0, 0.5, 0.8)")
        if any(not 0.0 <= value <= 1.0 for value in levels):
            raise ValueError("credibility_levels 必须位于 [0,1]")

    @property
    def posterior_digest(self) -> str:
        """摘要默认中档证据下的完整 posterior。"""

        posterior = posterior_layouts(
            self.config,
            self.role_view,
            self.decision_state,
            credibility=0.5,
        )
        return posterior_digest(posterior)

    def identity_payload(self) -> dict[str, Any]:
        """生成不含运行 UUID 的规范请求字段。"""

        return {
            "config": {
                "number_of_players": self.config.number_of_players,
                "number_of_wolves": self.config.number_of_wolves,
                "roles": list(self.config.roles),
                "max_days": self.config.max_days,
                "rules_spec": self.config.rules_spec,
            },
            "decision_state": self.decision_state.to_dict(),
            "actor_id": self.actor_id,
            "actor_role": self.actor_role,
            "role_view": self.role_view.to_dict(),
            "posterior_digest": self.posterior_digest,
            "candidate_actions": [action.to_dict() for action in self.candidate_actions],
            "credibility_levels": list(self.credibility_levels),
            "policy_temperature": self.policy_temperature,
            "samples_per_cell": self.samples_per_cell,
            "seed_scheme": self.seed_scheme,
            "base_seed": self.base_seed,
            "policy_spec": self.policy_spec,
            "candidate_spec": self.candidate_spec,
        }

    def request_digest(self) -> str:
        """返回矩阵请求稳定摘要。"""

        return _digest(self.identity_payload())

    def worker_payload(self, *, credibility: float, sample_start: int, sample_end: int) -> dict[str, Any]:
        """构造单批 worker 的具名参数。"""

        payload = self.identity_payload()
        payload.update(
            {
                "request_digest": self.request_digest(),
                "credibility": float(credibility),
                "sample_start": int(sample_start),
                "sample_end": int(sample_end),
            }
        )
        return payload


@dataclass(frozen=True, slots=True)
class DecisionMatrixCell:
    """单个具体动作和可信度档位的充分统计量。"""

    action_key: str
    action: dict[str, Any]
    credibility: float
    sample_count: int
    reward_sum: float
    reward_sum_sq: float
    delta_sum: float
    delta_sum_sq: float
    scenario_counts: dict[str, int] = field(default_factory=dict)

    @property
    def mean(self) -> float:
        return self.reward_sum / self.sample_count if self.sample_count else 0.0

    @property
    def delta_mean(self) -> float:
        return self.delta_sum / self.sample_count if self.sample_count else 0.0

    @property
    def standard_error(self) -> float:
        if self.sample_count < 2:
            return 0.0
        variance = (self.reward_sum_sq - self.reward_sum * self.reward_sum / self.sample_count) / (
            self.sample_count - 1
        )
        return sqrt(max(0.0, variance) / self.sample_count)

    @property
    def delta_standard_error(self) -> float:
        """返回相对 baseline 配对差的 Monte Carlo 标准误。"""

        if self.sample_count < 2:
            return 0.0
        variance = (self.delta_sum_sq - self.delta_sum * self.delta_sum / self.sample_count) / (self.sample_count - 1)
        return sqrt(max(0.0, variance) / self.sample_count)

    def to_dict(self) -> dict[str, Any]:
        """输出矩阵单元，不包含隐藏站位和轨迹。"""

        return {
            "action_key": self.action_key,
            "action": self.action,
            "credibility": self.credibility,
            "mean": self.mean,
            "standard_error": self.standard_error,
            "baseline_delta": self.delta_mean,
            "baseline_delta_standard_error": self.delta_standard_error,
            "sample_count": self.sample_count,
            "scenario_counts": dict(self.scenario_counts),
        }


@dataclass(frozen=True, slots=True)
class DecisionMatrixResult:
    """完整矩阵输出。"""

    matrix_id: str
    request_digest: str
    status: str
    request: dict[str, Any]
    cells: tuple[DecisionMatrixCell, ...]

    def to_dict(self) -> dict[str, Any]:
        """转换为 JSON 友好的矩阵输出。"""

        grouped: dict[str, dict[str, Any]] = {}
        for cell in self.cells:
            row = grouped.setdefault(
                cell.action_key,
                {"action_key": cell.action_key, "action": cell.action, "by_credibility": {}},
            )
            row["by_credibility"][str(cell.credibility)] = {
                "mean": cell.mean,
                "standard_error": cell.standard_error,
                "baseline_delta": cell.delta_mean,
                "baseline_delta_standard_error": cell.delta_standard_error,
                "sample_count": cell.sample_count,
                "scenario_counts": dict(cell.scenario_counts),
            }
        return {
            "matrix_id": self.matrix_id,
            "request_digest": self.request_digest,
            "status": self.status,
            "request": self.request,
            "action_rows": list(grouped.values()),
            "notice": t("matrix.notice.model_scope"),
        }


class MatrixInterrupted(RuntimeError):
    """父进程内存安全区或人工停止导致的可恢复中断。"""


class DecisionMatrixCalculator:
    """使用隔离 worker 计算矩阵，并由单写线程提交 SQLite。"""

    def __init__(
        self,
        database_path: str | Path,
        *,
        workers: int = 2,
        batch_size: int = 10,
        memory_reserve_gib: float = 8.0,
        memory_reserve_ratio: float = 0.15,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        stop_event: Any | None = None,
    ) -> None:
        """配置矩阵协调器。

        参数：
            database_path: 与完整分支树迭代共用或显式指定的 SQLite 文件。
            workers: 隔离 rollout 子进程数，最小为 1。
            batch_size: 单任务覆盖的连续样本索引数量，最小为 1。
            memory_reserve_gib: 触发可恢复中断的绝对内存保留量。
            memory_reserve_ratio: 触发可恢复中断的内存保留比例。
            progress_callback: 批次事务提交后的只读进度回调；不参与请求身份。
            stop_event: 可选跨进程事件；置位后停止派发并写为可恢复中断。
        """

        self.database_path = Path(database_path)
        self.workers = max(1, int(workers))
        self.batch_size = max(1, int(batch_size))
        self.memory_reserve_gib = max(0.0, float(memory_reserve_gib))
        self.memory_reserve_ratio = max(0.0, min(1.0, float(memory_reserve_ratio)))
        self.progress_callback = progress_callback
        self.stop_event = stop_event

    def _stop_requested(self) -> bool:
        """返回调用方是否请求在批次边界可恢复中断。"""

        return bool(self.stop_event is not None and self.stop_event.is_set())

    def _emit_progress(self, payload: dict[str, Any]) -> None:
        """发送不影响算法身份的只读进度；界面异常不会污染计算结果。"""

        if self.progress_callback is None:
            return
        try:
            self.progress_callback(dict(payload))
        except Exception:
            logger.exception(t("log.matrix.progress_callback_failed"))

    def _memory_pressure(self) -> MatrixInterrupted | None:
        """检查父进程是否进入安全保留区，返回可恢复中断原因。"""

        pressure = memory_pressure_snapshot(
            reserve_ratio=self.memory_reserve_ratio,
            reserve_gib=self.memory_reserve_gib,
        )
        if pressure is None:
            return None
        snapshot, threshold = pressure
        return MatrixInterrupted(
            t(
                "error.matrix.memory_guard",
                available=snapshot.available_bytes,
                threshold=threshold,
            )
        )

    def calculate(
        self,
        request: DecisionMatrixRequest,
        *,
        force_recompute: bool = False,
    ) -> DecisionMatrixResult:
        """从零或最近批次检查点计算矩阵。

        进度只在 SQLite 批次事务完成后发布，因此 GUI 展示的已提交批次数
        始终可以由数据库恢复；停止事件只在批次边界检查，不删除样本或换种子。
        """

        store = DecisionMatrixStore(self.database_path)
        request_digest = request.request_digest()
        total_batches = len(request.credibility_levels) * (
            (request.samples_per_cell + self.batch_size - 1) // self.batch_size
        )
        existing = store.load_complete(request_digest=request_digest)
        if existing is not None and not force_recompute:
            result = self._result_from_storage(existing)
            self._emit_progress(
                {
                    "kind": "matrix_progress",
                    "status": "complete",
                    "matrix_id": result.matrix_id,
                    "committed_batches": total_batches,
                    "total_batches": total_batches,
                    "cache_hit": True,
                }
            )
            store.close()
            return result
        matrix_id, _resumed, already_complete = store.start_run(
            request_digest=request_digest,
            request_json=request.identity_payload(),
            target_samples=request.samples_per_cell,
            expected_cell_count=len(request.candidate_actions) * len(request.credibility_levels),
            force_new=force_recompute,
        )
        if already_complete and not force_recompute:
            loaded = store.load_complete(request_digest=request_digest)
            if loaded is None:
                raise RuntimeError("矩阵运行头标记 complete 但无法读取结果")
            result = self._result_from_storage(loaded)
            self._emit_progress(
                {
                    "kind": "matrix_progress",
                    "status": "complete",
                    "matrix_id": result.matrix_id,
                    "committed_batches": total_batches,
                    "total_batches": total_batches,
                    "cache_hit": True,
                }
            )
            store.close()
            return result
        actions = tuple(
            {
                "action_key": action.key(),
                "action_family": action.family,
                "action_json": action.to_dict(),
            }
            for action in request.candidate_actions
        )
        store.initialize_rows(
            matrix_id=matrix_id,
            actions=actions,
            credibility_levels=tuple(request.credibility_levels),
        )
        committed = store.committed_batches(matrix_id=matrix_id)
        committed_count = len(committed)
        self._emit_progress(
            {
                "kind": "matrix_progress",
                "status": "running",
                "matrix_id": matrix_id,
                "committed_batches": committed_count,
                "total_batches": total_batches,
                "cache_hit": False,
                "resumed": bool(_resumed),
            }
        )
        tasks: list[dict[str, Any]] = []
        for credibility in request.credibility_levels:
            for start in range(0, request.samples_per_cell, self.batch_size):
                end = min(request.samples_per_cell, start + self.batch_size)
                batch_id = f"{float(credibility):.6f}:{start}:{end}"
                if batch_id not in committed:
                    tasks.append(
                        request.worker_payload(
                            credibility=float(credibility),
                            sample_start=start,
                            sample_end=end,
                        )
                    )
        try:
            if tasks:
                if self.workers == 1:
                    for task in tasks:
                        if self._stop_requested():
                            raise MatrixInterrupted("用户请求中断矩阵计算")
                        pressure = self._memory_pressure()
                        if pressure is not None:
                            raise pressure
                        batch = run_matrix_batch(task)
                        if store.commit_batch(matrix_id=matrix_id, batch=batch):
                            committed_count += 1
                            self._emit_progress(
                                {
                                    "kind": "matrix_progress",
                                    "status": "running",
                                    "matrix_id": matrix_id,
                                    "committed_batches": committed_count,
                                    "total_batches": total_batches,
                                    "credibility": float(batch["credibility"]),
                                    "sample_start": int(batch["sample_start"]),
                                    "sample_end": int(batch["sample_end"]),
                                    "cache_hit": False,
                                }
                            )
                        gc.collect()
                else:
                    self._run_process_batches(
                        store=store,
                        matrix_id=matrix_id,
                        tasks=tasks,
                        committed_count=committed_count,
                        total_batches=total_batches,
                    )
            valid = store.validate_complete(
                matrix_id=matrix_id,
                target_samples=request.samples_per_cell,
                expected_cells=len(request.candidate_actions) * len(request.credibility_levels),
            )
            if not valid:
                store.mark_status(matrix_id=matrix_id, status="interrupted", error_summary="矩阵行计数未完整")
                raise RuntimeError("矩阵批次未形成完整结果")
            # 强制重算会派生带 UUID 的 request_digest，因此完成后按 matrix_id
            # 回读，避免把旧运行误当作本次结果。
            loaded = self._load_by_matrix_id(store, matrix_id)
            if loaded is None:
                raise RuntimeError("矩阵已完成但读取结果失败")
            result = self._result_from_storage(loaded)
            self._emit_progress(
                {
                    "kind": "matrix_progress",
                    "status": "complete",
                    "matrix_id": matrix_id,
                    "committed_batches": total_batches,
                    "total_batches": total_batches,
                    "cache_hit": False,
                }
            )
            return result
        except KeyboardInterrupt:
            store.mark_status(matrix_id=matrix_id, status="interrupted", error_summary="用户中断")
            raise
        except MatrixInterrupted as exc:
            store.mark_status(matrix_id=matrix_id, status="interrupted", error_summary=str(exc))
            raise
        except BaseException as exc:
            store.mark_status(matrix_id=matrix_id, status="failed", error_summary=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            store.close()

    def load_cell(
        self,
        request: DecisionMatrixRequest,
        *,
        action: SpeechPlan,
        credibility: float,
    ) -> DecisionMatrixCell | None:
        """按完整请求身份、具体动作和可信度读取一个已完成单元格。

        该只读路径不启动计算；请求必须已经以 `complete` 状态持久化，
        未命中或单元格尚未完成时返回 ``None``。
        """

        store = DecisionMatrixStore(self.database_path)
        try:
            run = store.find_complete_run(request.request_digest())
            if run is None:
                return None
            row = store.load_row(
                matrix_id=str(run["matrix_id"]),
                action_key=action.key(),
                credibility=float(credibility),
            )
            if row is None:
                return None
            return DecisionMatrixCell(
                action_key=str(row["action_key"]),
                action=dict(row["action_json"]),
                credibility=float(row["credibility"]),
                sample_count=int(row["sample_count"]),
                reward_sum=float(row["reward_sum"]),
                reward_sum_sq=float(row["reward_sum_sq"]),
                delta_sum=float(row["delta_sum"]),
                delta_sum_sq=float(row["delta_sum_sq"]),
                scenario_counts={str(key): int(value) for key, value in row.get("scenario_counts", {}).items()},
            )
        finally:
            store.close()

    def _run_process_batches(
        self,
        *,
        store: DecisionMatrixStore,
        matrix_id: str,
        tasks: list[dict[str, Any]],
        committed_count: int,
        total_batches: int,
    ) -> None:
        """启动有界任务/结果队列、聚合线程和 SQLite 单写线程。

        ``committed_count`` 是恢复前已经幂等提交的批次数。只有单一写线程
        完成事务后才递增并发布，任务完成顺序不会进入请求身份或随机种子。
        """

        context = multiprocessing.get_context("spawn")
        queue_size = max(2, self.workers * 2)
        task_queue = context.Queue(maxsize=queue_size)
        result_queue = context.Queue(maxsize=queue_size)
        persist_queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=queue_size)
        processes = [
            context.Process(target=worker_loop, args=(task_queue, result_queue), daemon=True)
            for _index in range(self.workers)
        ]
        errors: list[BaseException] = []
        dispatch_done = threading.Event()
        dispatched_count = 0

        def dispatch() -> None:
            nonlocal dispatched_count
            try:
                for task in tasks:
                    if self._stop_requested():
                        errors.append(MatrixInterrupted("用户请求中断矩阵计算"))
                        break
                    pressure = self._memory_pressure()
                    if pressure is not None:
                        errors.append(pressure)
                        break
                    task_queue.put(task)
                    dispatched_count += 1
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                for _index in processes:
                    task_queue.put(None)
                dispatch_done.set()

        def aggregate() -> None:
            try:
                received_count = 0
                idle_since = time.monotonic()
                while True:
                    try:
                        message = result_queue.get(timeout=0.5)
                    except queue.Empty as exc:
                        if dispatch_done.is_set() and received_count >= dispatched_count:
                            break
                        if time.monotonic() - idle_since >= 120:
                            dead = [process.exitcode for process in processes if process.exitcode not in {None, 0}]
                            detail = f" worker_exitcodes={dead}" if dead else ""
                            raise RuntimeError(t("error.matrix.worker_timeout", detail=detail)) from exc
                        continue
                    idle_since = time.monotonic()
                    if message.get("kind") == "error":
                        errors.append(
                            RuntimeError(
                                t(
                                    "error.matrix.worker_failed",
                                    error_type=message.get("error_type"),
                                    error=message.get("error"),
                                )
                            )
                        )
                    else:
                        persist_queue.put(message["payload"])
                    received_count += 1
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                persist_queue.put(None)

        def persist() -> None:
            committed_total = int(committed_count)
            try:
                while True:
                    batch = persist_queue.get()
                    if batch is None:
                        return
                    if store.commit_batch(matrix_id=matrix_id, batch=batch):
                        committed_total += 1
                        self._emit_progress(
                            {
                                "kind": "matrix_progress",
                                "status": "running",
                                "matrix_id": matrix_id,
                                "committed_batches": committed_total,
                                "total_batches": total_batches,
                                "credibility": float(batch["credibility"]),
                                "sample_start": int(batch["sample_start"]),
                                "sample_end": int(batch["sample_end"]),
                                "cache_hit": False,
                            }
                        )
                    gc.collect()
            except BaseException as exc:  # noqa: BLE001 - 主线程统一记录失败
                errors.append(exc)
                while True:
                    discarded = persist_queue.get()
                    if discarded is None:
                        return

        for process in processes:
            process.start()
        dispatcher = threading.Thread(target=dispatch, name="matrix-dispatcher", daemon=True)
        aggregator = threading.Thread(target=aggregate, name="matrix-aggregator", daemon=True)
        writer = threading.Thread(target=persist, name="matrix-sqlite-writer", daemon=True)
        dispatcher.start()
        aggregator.start()
        writer.start()
        dispatcher.join()
        aggregator.join()
        writer.join()
        for process in processes:
            process.join(timeout=30)
            if process.is_alive():
                process.terminate()
                process.join()
            if process.exitcode not in {0, None}:
                errors.append(
                    RuntimeError(
                        t(
                            "error.matrix.worker_exit",
                            exitcode=process.exitcode,
                        )
                    )
                )
            process.close()
        for process_queue in (task_queue, result_queue):
            process_queue.close()
            process_queue.join_thread()
        if errors:
            raise errors[0]

    @staticmethod
    def _load_by_matrix_id(store: DecisionMatrixStore, matrix_id: str) -> dict[str, Any] | None:
        statement = store.runs.select().where(store.runs.c.matrix_id == str(matrix_id))
        with store.engine.connect() as connection:
            run = connection.execute(statement).mappings().first()
        if run is None:
            return None
        rows_statement = (
            store.rows.select()
            .where(store.rows.c.matrix_id == str(matrix_id))
            .order_by(
                store.rows.c.action_key,
                store.rows.c.credibility,
            )
        )
        with store.engine.connect() as connection:
            rows = [dict(row) for row in connection.execute(rows_statement).mappings()]
        for row in rows:
            row["action_json"] = json.loads(row["action_json"])
            row["scenario_counts"] = json.loads(row.pop("scenario_counts_json") or "{}")
        return {"run": dict(run), "rows": rows}

    @staticmethod
    def _result_from_storage(payload: dict[str, Any]) -> DecisionMatrixResult:
        run = payload["run"]
        request = json.loads(run["request_json"])
        cells = tuple(
            DecisionMatrixCell(
                action_key=str(row["action_key"]),
                action=dict(row["action_json"]),
                credibility=float(row["credibility"]),
                sample_count=int(row["sample_count"]),
                reward_sum=float(row["reward_sum"]),
                reward_sum_sq=float(row["reward_sum_sq"]),
                delta_sum=float(row["delta_sum"]),
                delta_sum_sq=float(row["delta_sum_sq"]),
                scenario_counts={str(key): int(value) for key, value in row.get("scenario_counts", {}).items()},
            )
            for row in payload["rows"]
        )
        return DecisionMatrixResult(
            matrix_id=str(run["matrix_id"]),
            request_digest=str(run["request_digest"]),
            status=str(run["status"]),
            request=request,
            cells=cells,
        )


def build_default_decision_request(
    *,
    actor_id: int = 0,
    position_index: int = 1,
    samples_per_cell: int = 100,
    base_seed: int = 7,
) -> DecisionMatrixRequest:
    """按当前默认七人板子构造一个第一天发言前矩阵请求。"""

    roles = build_role_roster(
        number_of_players=7,
        number_of_wolves=2,
        include_seer=True,
        include_witch=True,
        include_guard=True,
        include_hunter=False,
        include_idiot=False,
        include_white_werewolf_king=False,
    )
    layouts = enumerate_position_layouts(roles)
    if not (1 <= int(position_index) <= len(layouts)):
        raise ValueError(f"position_index 必须在 1..{len(layouts)} 范围内")
    layout: PositionLayout = layouts[int(position_index) - 1]
    state = DecisionState.first_day_speech(7, actor_id=int(actor_id))
    if layout.roles[int(actor_id)] == "预言家":
        # 默认第一天状态模拟上一夜已有一条私有查验，且不把查验结果写入
        # 公开决策状态；它只进入该行动者的 RoleView。
        target = next(index for index in range(7) if index != int(actor_id))
        world = WorldState.from_state(
            layout.roles,
            state,
            private_seer_checks={
                int(actor_id): {
                    target: is_wolf_role(layout.roles[target]),
                }
            },
        )
        role_view = build_role_view(layout.roles, world, actor_id=int(actor_id))
    else:
        role_view = build_role_view(layout.roles, state, actor_id=int(actor_id))
    config = CanonicalGameConfig.from_roles(roles)
    actions = enumerate_speech_actions(state, role_view)
    return DecisionMatrixRequest(
        config=config,
        decision_state=state,
        actor_id=int(actor_id),
        actor_role=role_view.actor_role,
        role_view=role_view,
        candidate_actions=actions,
        samples_per_cell=int(samples_per_cell),
        base_seed=int(base_seed),
    )


def run_default_matrix(
    database_path: str | Path = "search_simulator_cache.sqlite3",
    *,
    actor_id: int = 0,
    position_index: int = 1,
    workers: int = 2,
    batch_size: int = 10,
    samples_per_cell: int = 100,
    force_recompute: bool = False,
    memory_reserve_gib: float = 8.0,
    memory_reserve_ratio: float = 0.15,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    stop_event: Any | None = None,
) -> DecisionMatrixResult:
    """计算默认板子的矩阵，供 CLI、GUI 和验证脚本复用。

    ``progress_callback`` 只接收批次提交后的观察消息；``stop_event`` 置位
    后在批次边界形成可恢复中断。两者都不进入规范矩阵请求身份。
    """

    request = build_default_decision_request(
        actor_id=actor_id,
        position_index=position_index,
        samples_per_cell=samples_per_cell,
    )
    return DecisionMatrixCalculator(
        database_path,
        workers=workers,
        batch_size=batch_size,
        memory_reserve_gib=memory_reserve_gib,
        memory_reserve_ratio=memory_reserve_ratio,
        progress_callback=progress_callback,
        stop_event=stop_event,
    ).calculate(request, force_recompute=force_recompute)
