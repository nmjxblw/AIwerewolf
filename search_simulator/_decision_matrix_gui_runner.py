"""精确信念 Cheap-talk 决策矩阵 GUI 的隔离协调进程入口。

本模块不导入 Pygame。Pygame 主进程只发送具名请求并消费有界消息；本进程
负责调用矩阵协调器、管理其前向终局模拟子进程、写入 SQLite 和归一化终态。
"""

from __future__ import annotations

import logging
import os
import traceback
from typing import Any

from ._i18n import t

logger = logging.getLogger(__name__)


def run_matrix_gui_process(
    *,
    output_queue: Any,
    stop_event: Any,
    database_path: str,
    actor_id: int,
    position_index: int,
    workers: int,
    batch_size: int,
    samples_per_cell: int,
    force_recompute: bool,
    memory_reserve_gib: float,
    memory_reserve_ratio: float,
) -> None:
    """在非 daemon 协调子进程中执行一次矩阵请求。

    参数全部是可序列化具名字段。``actor_id`` 使用内部零基席位；
    ``position_index`` 使用研究界面的一基站位编号。``output_queue`` 只传输
    进度、JSON-safe 最终矩阵和错误摘要，``stop_event`` 仅在批次边界触发
    可恢复中断。函数不返回跨进程对象，所有终态都通过队列发送。
    """

    try:
        from ._crash_handler import crash_log_path
        from ._crash_handler import install_crash_handlers
        from ._crash_handler import record_caught_failure
        from ._decision_matrix import MatrixInterrupted
        from ._decision_matrix import run_default_matrix
        from ._runtime_logging import configure_runtime_logging
        from ._runtime_logging import runtime_log_path
    except ImportError:
        from search_simulator._crash_handler import crash_log_path
        from search_simulator._crash_handler import install_crash_handlers
        from search_simulator._crash_handler import record_caught_failure
        from search_simulator._decision_matrix import MatrixInterrupted
        from search_simulator._decision_matrix import run_default_matrix
        from search_simulator._runtime_logging import configure_runtime_logging
        from search_simulator._runtime_logging import runtime_log_path

    configure_runtime_logging()
    install_crash_handlers()
    last_matrix_id = ""
    last_committed = 0
    last_total = 0
    cache_hit = False

    def publish(payload: dict[str, Any]) -> None:
        """把矩阵观察消息写入有界队列，并保存终态所需检查点。"""

        nonlocal cache_hit, last_committed, last_matrix_id, last_total
        last_matrix_id = str(payload.get("matrix_id") or last_matrix_id)
        last_committed = int(payload.get("committed_batches", last_committed))
        last_total = int(payload.get("total_batches", last_total))
        cache_hit = bool(payload.get("cache_hit", cache_hit))
        output_queue.put(dict(payload))

    output_queue.put(
        {
            "kind": "matrix_starting",
            "status": "running",
            "pid": os.getpid(),
            "runtime_log": str(runtime_log_path()),
            "crash_log": str(crash_log_path()),
        }
    )
    try:
        result = run_default_matrix(
            database_path,
            actor_id=int(actor_id),
            position_index=int(position_index),
            workers=int(workers),
            batch_size=int(batch_size),
            samples_per_cell=int(samples_per_cell),
            force_recompute=bool(force_recompute),
            memory_reserve_gib=float(memory_reserve_gib),
            memory_reserve_ratio=float(memory_reserve_ratio),
            progress_callback=publish,
            stop_event=stop_event,
        )
        payload = result.to_dict()
        output_queue.put(
            {
                "kind": "matrix_done",
                "status": "complete",
                "matrix_id": result.matrix_id,
                "committed_batches": last_total,
                "total_batches": last_total,
                "cache_hit": cache_hit,
                "result": payload,
                "runtime_log": str(runtime_log_path()),
                "crash_log": str(crash_log_path()),
            }
        )
        logger.info(
            t(
                "log.matrix.gui_complete",
                matrix_id=result.matrix_id,
                completed=last_total,
                total=last_total,
                cache_hit=cache_hit,
            )
        )
    except MatrixInterrupted as exc:
        output_queue.put(
            {
                "kind": "matrix_interrupted",
                "status": "interrupted",
                "matrix_id": last_matrix_id,
                "committed_batches": last_committed,
                "total_batches": last_total,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "runtime_log": str(runtime_log_path()),
                "crash_log": str(crash_log_path()),
            }
        )
        logger.warning(
            t(
                "log.matrix.gui_interrupted",
                matrix_id=last_matrix_id or t("common.unknown"),
                completed=last_committed,
                total=last_total,
                reason=exc,
            )
        )
    except BaseException as exc:
        record_caught_failure(
            exc,
            category="gui_matrix_coordinator",
            context={
                "matrix_id": last_matrix_id or "unknown",
                "batches": f"{last_committed}/{last_total}",
                "error_type": type(exc).__name__,
            },
        )
        output_queue.put(
            {
                "kind": "matrix_failed",
                "status": "failed",
                "matrix_id": last_matrix_id,
                "committed_batches": last_committed,
                "total_batches": last_total,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
                "runtime_log": str(runtime_log_path()),
                "crash_log": str(crash_log_path()),
            }
        )
        logger.exception(
            t(
                "log.matrix.gui_failed",
                matrix_id=last_matrix_id or t("common.unknown"),
                committed=last_committed,
                total=last_total,
            )
        )
