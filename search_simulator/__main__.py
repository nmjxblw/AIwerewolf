"""狼人杀完整分支树迭代与决策矩阵入口。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

logger = logging.getLogger(__name__)


def _import_runtime_modules():
    try:
        from search_simulator._artifacts import emit_simulation_artifacts
        from search_simulator._config import build_parser
        from search_simulator._simulator import SearchSimulator
    except ImportError:
        from ._artifacts import emit_simulation_artifacts
        from ._config import build_parser
        from ._simulator import SearchSimulator
    return (
        SearchSimulator,
        build_parser,
        emit_simulation_artifacts,
    )


def _import_gui_launcher():
    try:
        from search_simulator._gui import launch_gui
    except ImportError:
        from ._gui import launch_gui
    return launch_gui


def _load_start_state(args):
    """从 CLI 参数解析自定义起始状态（JSON 字符串或文件路径）。"""
    try:
        from search_simulator._game_state import GameState
    except ImportError:
        from ._game_state import GameState

    if getattr(args, "start_state_json", None):
        return GameState.from_dict(json.loads(args.start_state_json))
    if getattr(args, "start_state_path", None):
        from pathlib import Path

        return GameState.from_dict(json.loads(Path(args.start_state_path).read_text(encoding="utf-8-sig")))
    return None


def _run_simulation(args: argparse.Namespace, phase_callback=None):
    """使用显式 CLI/GUI 参数构造并运行模拟器。

    参数：
        args: 参数解析器或 GUI 生成的完整具名参数空间。
        phase_callback: 可选的产物生成阶段回调，仅用于界面状态提示。

    返回：
        已保存最终或可恢复中断结果的模拟器实例。
    """

    (
        SearchSimulator,
        _build_parser,
        emit_simulation_artifacts,
    ) = _import_runtime_modules()
    _ = _build_parser

    callback = getattr(args, "iteration_callback", None)

    try:
        from search_simulator._i18n import set_language
        from search_simulator._i18n import t
    except ImportError:
        from ._i18n import set_language
        from ._i18n import t

    set_language(getattr(args, "lang", "zh-CN"))
    # CLI/GUI 到模拟器的运行参数逐项显式传递。新增参数若未在此列出，
    # 会在回归中直接暴露，而不会被动态字典悄悄吞掉。
    simulator = SearchSimulator(
        number_of_players=args.number_of_players,
        number_of_wolves=args.number_of_wolves,
        include_seer=args.include_seer,
        include_witch=args.include_witch,
        include_guard=args.include_guard,
        include_hunter=args.include_hunter,
        include_idiot=args.include_idiot,
        include_white_werewolf_king=args.include_white_werewolf_king,
        search_mode=args.search_mode,
        parallel_workers=args.parallel_workers,
        memory_reserve_gib=args.memory_reserve_gib,
        memory_reserve_ratio=args.memory_reserve_ratio,
        lambda_risk=args.lambda_risk,
        smart_vote=args.smart_vote,
        all_positions=args.all_positions,
        tactics=args.tactics,
        results_output_path=args.results_output_path,
        signature_cache_db_path=args.signature_cache_db_path,
        signature_lru_capacity=args.signature_lru_capacity,
        signature_commit_interval=args.signature_commit_interval,
        force_recompute=getattr(args, "force_recompute", False),
        iteration_callback=callback if callable(callback) else None,
        progress_queue=getattr(args, "progress_queue", None),
        result_queue=getattr(args, "result_queue", None),
        resume_event=getattr(args, "resume_event", None),
        live_preview_enabled=getattr(args, "live_preview_enabled", True),
    )
    result = simulator.run(start_state=_load_start_state(args))

    try:
        emit_simulation_artifacts(
            simulator,
            result=result,
            enable_plot=not args.disable_plot,
            phase_callback=phase_callback,
            plot_position_index=args.plot_position_index,
            max_nodes_for_plot=args.max_nodes_for_plot,
            plot_dpi=args.plot_dpi,
        )
    except Exception as exc:
        # 搜索终态已经形成时，后处理失败不能反向伪装成“搜索未执行”。
        # 把迭代上下文附到异常上供 GUI 弹窗区分。
        for attribute, value in (
            ("run_id", result.get("run_id", simulator.run_id)),
            ("iteration_status", result.get("status", "complete")),
            ("completed_positions", result.get("position_count", 0)),
            ("total_positions", result.get("total_position_count", 0)),
            ("next_position_index", result.get("next_position_index")),
        ):
            try:
                setattr(exc, attribute, value)
            except (AttributeError, TypeError):
                pass
        logger.exception(
            t(
                "log.artifact_pipeline_failed",
                status=result.get("status", "complete"),
                run_id=result.get("run_id", simulator.run_id),
            )
        )
        try:
            from ._crash_handler import record_caught_failure
        except ImportError:
            from search_simulator._crash_handler import record_caught_failure
        record_caught_failure(
            exc,
            category="artifact_pipeline",
            context={
                "run_id": result.get("run_id", simulator.run_id),
                "iteration_status": result.get("status", "complete"),
                "checkpoints": (f"{result.get('position_count', 0)}/{result.get('total_position_count', 0)}"),
                "next_position": result.get("next_position_index") or "none",
                "error_type": type(exc).__name__,
            },
        )
        raise
    return simulator


def _run_decision_matrix(args: argparse.Namespace) -> None:
    """运行精确信念 Cheap-talk 决策矩阵并输出结构化结果。"""

    try:
        from search_simulator._decision_matrix import run_default_matrix
    except ImportError:
        from ._decision_matrix import run_default_matrix

    try:
        from search_simulator._i18n import set_language
    except ImportError:
        from ._i18n import set_language
    set_language(getattr(args, "lang", "zh-CN"))

    result = run_default_matrix(
        args.matrix_db_path,
        actor_id=args.matrix_actor_id,
        position_index=args.matrix_position_index,
        workers=args.matrix_workers,
        batch_size=args.matrix_batch_size,
        samples_per_cell=args.matrix_samples,
        force_recompute=args.matrix_force_recompute,
        memory_reserve_gib=getattr(args, "memory_reserve_gib", 8.0),
        memory_reserve_ratio=getattr(args, "memory_reserve_ratio", 0.15),
    )
    # 矩阵是机器可消费的 JSON；Windows 默认代码页可能不是 UTF-8，
    # 这里显式设置 stdout，避免重定向后的中文 action_json 无法解析。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


def _install_crash_handlers():
    """安装全局崩溃处理器并返回 crash 日志路径。"""
    try:
        from ._crash_handler import install_crash_handlers
    except ImportError:
        from search_simulator._crash_handler import install_crash_handlers
    try:
        return install_crash_handlers()
    except Exception:
        try:
            from ._i18n import t
        except ImportError:
            from search_simulator._i18n import t
        logger.exception(t("log.crash_handler_install_failed"))
        return None


def main() -> None:
    try:
        from ._runtime_logging import configure_runtime_logging
    except ImportError:
        from search_simulator._runtime_logging import configure_runtime_logging
    try:
        from ._i18n import t
    except ImportError:
        from search_simulator._i18n import t

    runtime_path = configure_runtime_logging()
    crash_path = _install_crash_handlers()
    logger.info(
        t(
            "log.logging_ready",
            pid=os.getpid(),
            runtime_log=runtime_path,
            crash_log=crash_path or t("common.unavailable"),
        )
    )
    _, build_parser, _ = _import_runtime_modules()
    parser = build_parser()
    args: argparse.Namespace = parser.parse_args()
    if getattr(args, "decision_matrix", False):
        _run_decision_matrix(args)
        return
    no_extra_args = len(sys.argv) <= 1
    if args.gui or (no_extra_args and not args.cli):
        _import_gui_launcher()(parser, _run_simulation)
        return
    _run_simulation(args)


if __name__ == "__main__":
    main()
