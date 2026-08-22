from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys

"""基于 BFS/DFS 的狼人杀全树搜索模拟入口（支持 GUI 参数配置）。"""
logging.basicConfig(
    level=logging.INFO,
    format=r"[%(asctime)s.%(msecs)03d][%(pathname)s:%(lineno)d][%(levelname)s]"
    + os.linesep
    + r"%(message)s"
    + os.linesep,
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def _ensure_windows_allocator_stability() -> None:
    """Windows 3.12 使用系统分配器，避免大图 worker 的 pymalloc/debug 峰值。"""

    if (
        os.name != "nt"
        or sys.version_info < (3, 12)
        or os.environ.get("PYTHONMALLOC") == "malloc"
        or os.environ.get("SEARCH_SIMULATOR_ALLOCATOR_RESTARTED") == "1"
    ):
        return
    environment = os.environ.copy()
    environment["PYTHONMALLOC"] = "malloc"
    environment["PYTHONFAULTHANDLER"] = "1"
    environment["SEARCH_SIMULATOR_ALLOCATOR_RESTARTED"] = "1"
    completed = subprocess.run(
        [sys.executable, "-m", "search_simulator", *sys.argv[1:]],
        env=environment,
        check=False,
    )
    raise SystemExit(completed.returncode)


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
    except ImportError:
        from ._i18n import set_language

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
        iteration_callback=callback if callable(callback) else None,
        progress_queue=getattr(args, "progress_queue", None),
        result_queue=getattr(args, "result_queue", None),
        resume_event=getattr(args, "resume_event", None),
    )
    result = simulator.run(start_state=_load_start_state(args))

    emit_simulation_artifacts(
        simulator,
        result=result,
        enable_plot=not args.disable_plot,
        phase_callback=phase_callback,
        plot_position_index=args.plot_position_index,
        max_nodes_for_plot=args.max_nodes_for_plot,
        plot_dpi=args.plot_dpi,
    )
    return simulator


def _install_crash_handlers() -> None:
    """安装全局崩溃处理器；安装失败不应阻断主流程。"""
    try:
        from ._crash_handler import install_crash_handlers
    except ImportError:
        from search_simulator._crash_handler import install_crash_handlers
    try:
        install_crash_handlers()
    except Exception:
        pass


def main() -> None:
    _ensure_windows_allocator_stability()
    _install_crash_handlers()
    _, build_parser, _ = _import_runtime_modules()
    parser = build_parser()
    args: argparse.Namespace = parser.parse_args()
    no_extra_args = len(sys.argv) <= 1
    if args.gui or (no_extra_args and not args.cli):
        _import_gui_launcher()(parser, _run_simulation)
        return
    _run_simulation(args)


if __name__ == "__main__":
    main()
