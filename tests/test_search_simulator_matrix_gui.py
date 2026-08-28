from __future__ import annotations

import ast
import queue
import random
import threading
from pathlib import Path

import pytest

from search_simulator._decision_matrix import DecisionMatrixCalculator
from search_simulator._decision_matrix import MatrixInterrupted
from search_simulator._decision_matrix import build_default_decision_request
from search_simulator._decision_matrix import run_default_matrix
from search_simulator._decision_matrix_gui_runner import run_matrix_gui_process
from search_simulator._decision_matrix_store import DecisionMatrixStore
from search_simulator._gui import PygameSimulatorUI
from search_simulator._gui import _matrix_action_label
from search_simulator._gui import _matrix_row_sort_key
from search_simulator._gui import _matrix_terminal_popup_content
from search_simulator._i18n import EN_STRINGS
from search_simulator._i18n import STRINGS


def test_matrix_progress_follows_committed_batches_and_cache_hit(tmp_path) -> None:
    database_path = tmp_path / "matrix-progress.sqlite3"
    request = build_default_decision_request(samples_per_cell=2)
    progress: list[dict[str, object]] = []
    result = DecisionMatrixCalculator(
        database_path,
        workers=1,
        batch_size=1,
        memory_reserve_gib=0.0,
        memory_reserve_ratio=0.0,
        progress_callback=progress.append,
    ).calculate(request)

    assert result.status == "complete"
    assert progress[0]["status"] == "running"
    assert progress[0]["committed_batches"] == 0
    assert progress[-1]["status"] == "complete"
    assert progress[-1]["committed_batches"] == 6
    assert progress[-1]["total_batches"] == 6
    committed = [int(item["committed_batches"]) for item in progress]
    assert committed == sorted(committed)

    cached_progress: list[dict[str, object]] = []
    cached = DecisionMatrixCalculator(
        database_path,
        workers=1,
        batch_size=1,
        memory_reserve_gib=0.0,
        memory_reserve_ratio=0.0,
        progress_callback=cached_progress.append,
    ).calculate(request)
    assert cached.matrix_id == result.matrix_id
    assert cached_progress == [
        {
            "kind": "matrix_progress",
            "status": "complete",
            "matrix_id": result.matrix_id,
            "committed_batches": 6,
            "total_batches": 6,
            "cache_hit": True,
        }
    ]


def test_matrix_stop_event_creates_recoverable_interruption(tmp_path) -> None:
    database_path = tmp_path / "matrix-stop.sqlite3"
    request = build_default_decision_request(samples_per_cell=2)
    stop_event = threading.Event()
    stop_event.set()
    calculator = DecisionMatrixCalculator(
        database_path,
        workers=1,
        batch_size=1,
        memory_reserve_gib=0.0,
        memory_reserve_ratio=0.0,
        stop_event=stop_event,
    )

    with pytest.raises(MatrixInterrupted, match="用户请求中断"):
        calculator.calculate(request)

    store = DecisionMatrixStore(database_path)
    try:
        run = store.find_run(request.request_digest())
        assert run is not None
        assert run["status"] == "interrupted"
        assert store.committed_batches(matrix_id=str(run["matrix_id"])) == set()
    finally:
        store.close()


def test_matrix_gui_labels_and_terminal_copy_are_observer_safe() -> None:
    baseline = {"action_key": "baseline", "action": {"family": "baseline"}}
    claim = {
        "action_key": "claim",
        "action": {
            "family": "seer_claim",
            "claim_target": 4,
            "claim_result": "wolf",
        },
    }
    assert _matrix_action_label(baseline["action"]) == "不发言（基准）"
    assert _matrix_action_label(claim["action"]) == "声明预言家：查验 5号 为狼人"
    assert _matrix_row_sort_key(baseline) < _matrix_row_sort_key(claim)

    title, body = _matrix_terminal_popup_content(
        "interrupted",
        {
            "matrix_id": "matrix-123",
            "committed_batches": 4,
            "total_batches": 30,
            "runtime_log": "runtime.log",
            "crash_log": "crash.log",
        },
    )
    assert title == "分析已停止（可继续）"
    assert "4/30" in body
    assert "再次开始将继续分析" in body
    assert "隐藏站位" not in body


def test_matrix_gui_uses_fixed_database_without_path_entry(tmp_path) -> None:
    class _Entry:
        def __init__(self, value: str) -> None:
            self.value = value

        def get_text(self) -> str:
            return self.value

    ui = object.__new__(PygameSimulatorUI)
    ui.matrix_entries = {
        "position_index": _Entry("1"),
        "actor_seat": _Entry("1"),
        "samples": _Entry("100"),
    }
    ui.matrix_database_path = str((tmp_path / "fixed.sqlite3").resolve())
    ui.matrix_force_recompute = False
    ui.defaults = type(
        "Defaults",
        (),
        {
            "memory_reserve_gib": 0.0,
            "memory_reserve_ratio": 0.0,
            "matrix_workers": 2,
            "matrix_batch_size": 10,
        },
    )()

    kwargs = ui._build_matrix_process_kwargs()

    assert "database_path" not in ui.matrix_entries
    assert "workers" not in ui.matrix_entries
    assert "batch_size" not in ui.matrix_entries
    assert kwargs["database_path"] == ui.matrix_database_path
    assert kwargs["workers"] == 2
    assert kwargs["batch_size"] == 10


def test_matrix_gui_process_emits_complete_terminal_event(tmp_path) -> None:
    output_queue: queue.Queue[dict[str, object]] = queue.Queue()
    stop_event = threading.Event()

    run_matrix_gui_process(
        output_queue=output_queue,
        stop_event=stop_event,
        database_path=str(tmp_path / "matrix-gui-process.sqlite3"),
        actor_id=0,
        position_index=1,
        workers=1,
        batch_size=1,
        samples_per_cell=1,
        force_recompute=False,
        memory_reserve_gib=0.0,
        memory_reserve_ratio=0.0,
    )

    messages: list[dict[str, object]] = []
    while not output_queue.empty():
        messages.append(output_queue.get_nowait())
    kinds = [str(message.get("kind")) for message in messages]
    assert kinds[0] == "matrix_starting"
    assert kinds[-1] == "matrix_done"
    assert "matrix_failed" not in kinds


def test_gui_i18n_catalogs_match_and_source_hides_internal_terms() -> None:
    assert set(STRINGS) == set(EN_STRINGS)
    source = (Path(__file__).parents[1] / "search_simulator" / "_gui.py").read_text(encoding="utf-8")
    for forbidden in ("DFS", "BFS", "V1", "V2", "V3", "v1", "v2", "v3"):
        assert forbidden not in source


def test_runtime_log_messages_use_i18n_catalog() -> None:
    module_root = Path(__file__).parents[1] / "search_simulator"
    violations: list[str] = []
    for path in module_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"debug", "info", "warning", "error", "critical", "exception"} or not node.args:
                continue
            owner = node.func.value
            is_module_logger = isinstance(owner, ast.Name) and owner.id == "logger"
            is_inline_logger = (
                isinstance(owner, ast.Call)
                and isinstance(owner.func, ast.Attribute)
                and isinstance(owner.func.value, ast.Name)
                and owner.func.value.id == "logging"
                and owner.func.attr == "getLogger"
            )
            if not (is_module_logger or is_inline_logger):
                continue
            message = node.args[0]
            is_translated_call = (
                isinstance(message, ast.Call) and isinstance(message.func, ast.Name) and message.func.id == "t"
            )
            is_translated_variable = isinstance(message, ast.Name) and message.id == "text"
            if not (is_translated_call or is_translated_variable):
                violations.append(f"{path.name}:{node.lineno}")
    assert violations == []


def test_ten_random_positions_produce_complete_matrices(tmp_path) -> None:
    positions = random.Random(20260829).sample(range(1, 1261), 10)
    database_path = tmp_path / "ten-random-positions.sqlite3"
    digests: set[str] = set()
    completed_positions: list[int] = []

    for position_index in positions:
        result = run_default_matrix(
            database_path,
            actor_id=0,
            position_index=position_index,
            workers=2,
            batch_size=1,
            samples_per_cell=1,
            memory_reserve_gib=0.0,
            memory_reserve_ratio=0.0,
        )
        assert result.status == "complete"
        assert result.cells
        assert {cell.credibility for cell in result.cells} == {0.0, 0.5, 0.8}
        assert all(cell.sample_count == 1 for cell in result.cells)
        assert all(sum(cell.scenario_counts.values()) == 1 for cell in result.cells)
        digests.add(result.request_digest)
        completed_positions.append(position_index)

    assert completed_positions == positions
    assert len(set(completed_positions)) == 10
    assert 1 < len(digests) <= 10
