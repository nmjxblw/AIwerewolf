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
    """在易触发 adaptive-cache 损坏的 Windows 新版 CPython 下自重启。"""

    if (
        os.name != "nt"
        or sys.version_info < (3, 12)
        or os.environ.get("PYTHONMALLOC") == "debug"
        or os.environ.get("SEARCH_SIMULATOR_ALLOCATOR_RESTARTED") == "1"
    ):
        return
    environment = os.environ.copy()
    environment["PYTHONMALLOC"] = "debug"
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
        from search_simulator._config import ARTIFACT_ARG_KEYS
        from search_simulator._config import SIMULATOR_ARG_KEYS
        from search_simulator._config import build_parser
        from search_simulator._simulator import SearchSimulator
    except ImportError:
        from ._artifacts import emit_simulation_artifacts
        from ._config import ARTIFACT_ARG_KEYS
        from ._config import SIMULATOR_ARG_KEYS
        from ._config import build_parser
        from ._simulator import SearchSimulator
    return (
        ARTIFACT_ARG_KEYS,
        SIMULATOR_ARG_KEYS,
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
    (
        artifact_arg_keys,
        simulator_arg_keys,
        SearchSimulator,
        _build_parser,
        emit_simulation_artifacts,
    ) = _import_runtime_modules()
    _ = _build_parser

    simulator_kwargs = {key: getattr(args, key) for key in simulator_arg_keys if hasattr(args, key)}
    callback = getattr(args, "iteration_callback", None)
    if callable(callback):
        simulator_kwargs["iteration_callback"] = callback
    for runtime_key in ("progress_queue", "result_queue", "resume_event"):
        if hasattr(args, runtime_key):
            simulator_kwargs[runtime_key] = getattr(args, runtime_key)

    try:
        from search_simulator._i18n import set_language
    except ImportError:
        from ._i18n import set_language

    set_language(getattr(args, "lang", "zh-CN"))
    simulator = SearchSimulator(**simulator_kwargs)
    result = simulator.run(start_state=_load_start_state(args))

    artifact_kwargs = {key: getattr(args, key) for key in artifact_arg_keys if hasattr(args, key)}
    emit_simulation_artifacts(
        simulator,
        result=result,
        enable_plot=not args.disable_plot,
        phase_callback=phase_callback,
        **artifact_kwargs,
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
    _, _, _, build_parser, _ = _import_runtime_modules()
    parser = build_parser()
    args: argparse.Namespace = parser.parse_args()
    no_extra_args = len(sys.argv) <= 1
    if args.gui or (no_extra_args and not args.cli):
        _import_gui_launcher()(parser, _run_simulation)
        return
    _run_simulation(args)


if __name__ == "__main__":
    main()
