from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any

from ._i18n import t


@dataclass(frozen=True)
class ArgumentSpec:
    flags: tuple[str, ...]
    kwargs: dict[str, Any]


ARGUMENT_SPECS: tuple[ArgumentSpec, ...] = (
    ArgumentSpec(
        ("-p", "--number_of_players"),
        {"type": int, "default": 5, "help": t("help.number_of_players")},
    ),
    ArgumentSpec(
        ("-w", "--number_of_wolves"),
        {"type": int, "default": 1, "help": t("help.number_of_wolves")},
    ),
    ArgumentSpec(
        ("--include_seer",),
        {"action": "store_true", "help": t("help.include_seer")},
    ),
    ArgumentSpec(
        ("--include_witch",),
        {"action": "store_true", "help": t("help.include_witch")},
    ),
    ArgumentSpec(
        ("--include_guard",),
        {"action": "store_true", "help": t("help.include_guard")},
    ),
    ArgumentSpec(
        ("--include_hunter",),
        {"action": "store_true", "help": t("help.include_hunter")},
    ),
    ArgumentSpec(
        ("--include_white_werewolf_king",),
        {"action": "store_true", "help": t("help.include_white_werewolf_king")},
    ),
    ArgumentSpec(
        ("--include_sheriff",),
        {"action": "store_true", "help": t("help.include_sheriff")},
    ),
    ArgumentSpec(
        ("--smart_vote",),
        {"action": "store_true", "help": t("help.smart_vote")},
    ),
    ArgumentSpec(
        ("--search_mode",),
        {
            "type": str,
            "choices": ["dfs", "bfs"],
            "default": "dfs",
            "help": t("help.search_mode"),
        },
    ),
    ArgumentSpec(
        ("--max_processed_states",),
        {"type": int, "default": None, "help": t("help.max_processed_states")},
    ),
    ArgumentSpec(
        ("--max_queue_size",),
        {
            "type": int,
            "default": None,
            "help": t("help.max_queue_size"),
        },
    ),
    ArgumentSpec(
        ("--max_runtime_seconds",),
        {
            "type": float,
            "default": None,
            "help": t("help.max_runtime_seconds"),
        },
    ),
    ArgumentSpec(
        ("--max_night_branches_per_state",),
        {
            "type": int,
            "default": None,
            "help": t("help.max_night_branches_per_state"),
        },
    ),
    ArgumentSpec(
        ("--max_day_branches_per_state",),
        {
            "type": int,
            "default": None,
            "help": t("help.max_day_branches_per_state"),
        },
    ),
    ArgumentSpec(
        ("--gc_interval",),
        {
            "type": int,
            "default": 2000,
            "help": t("help.gc_interval"),
        },
    ),
    ArgumentSpec(
        ("--parallel_workers",),
        {"type": int, "default": 1, "help": t("help.parallel_workers")},
    ),
    ArgumentSpec(
        ("--policy",),
        {
            "type": str,
            "choices": ["exhaustive", "online"],
            "default": "exhaustive",
            "help": t("help.policy"),
        },
    ),
    ArgumentSpec(
        ("--lambda_risk",),
        {"type": float, "default": 1.0, "help": t("help.lambda_risk")},
    ),
    ArgumentSpec(
        ("--toggle",),
        {
            "type": str,
            "choices": ["optimistic", "conservative"],
            "default": "conservative",
            "help": t("help.toggle"),
        },
    ),
    ArgumentSpec(
        ("--lookahead_depth",),
        {"type": int, "default": None, "help": t("help.lookahead_depth")},
    ),
    ArgumentSpec(
        ("--tactics",),
        {"type": str, "default": None, "help": t("help.tactics")},
    ),
    ArgumentSpec(
        ("--online_trace_path",),
        {
            "type": str,
            "default": "online_trace.json",
            "help": t("help.online_trace_path"),
        },
    ),
    ArgumentSpec(
        ("--compare_with_exact",),
        {"action": "store_true", "help": t("help.compare_with_exact")},
    ),
    ArgumentSpec(
        ("--start_state_json",),
        {"type": str, "default": None, "help": t("help.start_state_json")},
    ),
    ArgumentSpec(
        ("--start_state_path",),
        {"type": str, "default": None, "help": t("help.start_state_path")},
    ),
    ArgumentSpec(
        ("--lang",),
        {
            "type": str,
            "choices": ["zh-CN", "en-US"],
            "default": "zh-CN",
            "help": t("help.lang"),
        },
    ),
    ArgumentSpec(
        ("--disable_plot",),
        {"action": "store_true", "help": t("help.disable_plot")},
    ),
    ArgumentSpec(
        ("--max_nodes_for_plot",),
        {
            "type": int,
            "default": 2500,
            "help": t("help.max_nodes_for_plot"),
        },
    ),
    ArgumentSpec(
        ("--plot_dpi",),
        {"type": int, "default": 140, "help": t("help.plot_dpi")},
    ),
    ArgumentSpec(
        ("--export_text_tree",),
        {"action": "store_true", "help": t("help.export_text_tree")},
    ),
    ArgumentSpec(
        ("--text_tree_output_path",),
        {
            "type": str,
            "default": "search_tree.txt",
            "help": t("help.text_tree_output_path"),
        },
    ),
    ArgumentSpec(
        ("--max_text_tree_nodes",),
        {
            "type": int,
            "default": 100000,
            "help": t("help.max_text_tree_nodes"),
        },
    ),
    ArgumentSpec(
        ("--gui",),
        {"action": "store_true", "help": t("help.gui")},
    ),
    ArgumentSpec(
        ("--cli",),
        {"action": "store_true", "help": t("help.cli")},
    ),
)

SIMULATOR_ARG_KEYS: tuple[str, ...] = (
    "number_of_players",
    "number_of_wolves",
    "include_seer",
    "include_witch",
    "include_guard",
    "include_hunter",
    "include_white_werewolf_king",
    "include_sheriff",
    "smart_vote",
    "search_mode",
    "max_processed_states",
    "max_queue_size",
    "max_runtime_seconds",
    "max_night_branches_per_state",
    "max_day_branches_per_state",
    "gc_interval",
    "parallel_workers",
    "policy",
    "lambda_risk",
    "toggle",
    "lookahead_depth",
    "tactics",
    "online_trace_path",
    "compare_with_exact",
)

ARTIFACT_ARG_KEYS: tuple[str, ...] = (
    "max_nodes_for_plot",
    "plot_dpi",
    "text_tree_output_path",
    "max_text_tree_nodes",
)

GUI_GEOMETRY = "1560x760"
GUI_MIN_SIZE = (1100, 640)

GUI_BASIC_ENTRY_KEYS: tuple[str, ...] = ("number_of_players", "number_of_wolves")
GUI_ROLE_TOGGLE_KEYS: tuple[str, ...] = (
    "include_seer",
    "include_witch",
    "include_guard",
    "include_hunter",
    "include_white_werewolf_king",
    "include_sheriff",
    "export_text_tree",
)
GUI_LIMIT_ENTRY_LAYOUT: tuple[tuple[str, int, int], ...] = (
    ("max_processed_states", 0, 0),
    ("max_queue_size", 1, 0),
    ("max_runtime_seconds", 2, 0),
    ("max_night_branches_per_state", 3, 0),
    ("max_day_branches_per_state", 4, 0),
    ("gc_interval", 5, 0),
    ("parallel_workers", 0, 1),
    ("max_nodes_for_plot", 1, 1),
    ("plot_dpi", 2, 1),
    ("text_tree_output_path", 3, 1),
    ("max_text_tree_nodes", 4, 1),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=t("parser.description"))
    for spec in ARGUMENT_SPECS:
        parser.add_argument(*spec.flags, **spec.kwargs)
    return parser
