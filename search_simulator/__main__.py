from __future__ import annotations

import argparse
import json
import logging
import os
import sys

"""基于 BFS/DFS 的狼人杀全树搜索模拟入口（支持 GUI 参数配置）。"""
logging.basicConfig(
    format=r"[%(asctime)s.%(msecs)03d][%(pathname)s:%(lineno)d][%(levelname)s]"
    + os.linesep
    + r"%(message)s"
    + os.linesep,
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def _import_runtime_modules():
    try:
        from search_simulator._artifacts import emit_simulation_artifacts
        from search_simulator._config import ARTIFACT_ARG_KEYS
        from search_simulator._config import SIMULATOR_ARG_KEYS
        from search_simulator._config import build_parser
        from search_simulator._gui import launch_gui
        from search_simulator._simulator import SearchSimulator
    except ImportError:
        from ._artifacts import emit_simulation_artifacts
        from ._config import ARTIFACT_ARG_KEYS
        from ._config import SIMULATOR_ARG_KEYS
        from ._config import build_parser
        from ._gui import launch_gui
        from ._simulator import SearchSimulator
    return (
        ARTIFACT_ARG_KEYS,
        SIMULATOR_ARG_KEYS,
        SearchSimulator,
        build_parser,
        emit_simulation_artifacts,
        launch_gui,
    )


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

        return GameState.from_dict(
            json.loads(Path(args.start_state_path).read_text(encoding="utf-8-sig"))
        )
    return None


def _run_simulation(args: argparse.Namespace, phase_callback=None):
    (
        artifact_arg_keys,
        simulator_arg_keys,
        SearchSimulator,
        _build_parser,
        emit_simulation_artifacts,
        _launch_gui,
    ) = _import_runtime_modules()
    _ = (_build_parser, _launch_gui)

    simulator_kwargs = {
        key: getattr(args, key)
        for key in simulator_arg_keys
        if hasattr(args, key)
    }
    callback = getattr(args, "iteration_callback", None)
    if callable(callback):
        simulator_kwargs["iteration_callback"] = callback

    simulator = SearchSimulator(**simulator_kwargs)
    if simulator.policy == "online":
        simulator.run_online(start_state=_load_start_state(args))
        return simulator

    simulator.run()

    artifact_kwargs = {
        key: getattr(args, key)
        for key in artifact_arg_keys
        if hasattr(args, key)
    }
    emit_simulation_artifacts(
        simulator,
        enable_plot=not args.disable_plot,
        enable_text_tree=args.export_text_tree,
        phase_callback=phase_callback,
        **artifact_kwargs,
    )
    return simulator


def main() -> None:
    _, _, _, build_parser, _, launch_gui = _import_runtime_modules()
    parser = build_parser()
    args: argparse.Namespace = parser.parse_args()
    no_extra_args = len(sys.argv) <= 1
    if args.gui or (no_extra_args and not args.cli):
        launch_gui(parser, _run_simulation)
        return
    _run_simulation(args)


if __name__ == "__main__":
    main()
