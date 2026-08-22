from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from typing import Any

from ._i18n import t

PERSISTENCE_BATCH_SIZE = 64


@dataclass(frozen=True)
class ArgumentSpec:
    flags: tuple[str, ...]
    kwargs: dict[str, Any]


DEFAULT_WORKERS = max(1, min(4, (os.cpu_count() or 2) - 1))

ARGUMENT_SPECS: tuple[ArgumentSpec, ...] = (
    ArgumentSpec(
        ("-p", "--number_of_players"),
        {"type": int, "default": 7, "help": t("help.number_of_players")},
    ),
    ArgumentSpec(
        ("-w", "--number_of_wolves"),
        {"type": int, "default": 2, "help": t("help.number_of_wolves")},
    ),
    ArgumentSpec(
        ("--include_seer",),
        {
            "action": argparse.BooleanOptionalAction,
            "default": True,
            "help": t("help.include_seer"),
        },
    ),
    ArgumentSpec(
        ("--include_witch",),
        {
            "action": argparse.BooleanOptionalAction,
            "default": True,
            "help": t("help.include_witch"),
        },
    ),
    ArgumentSpec(
        ("--include_guard",),
        {
            "action": argparse.BooleanOptionalAction,
            "default": True,
            "help": t("help.include_guard"),
        },
    ),
    ArgumentSpec(
        ("--include_hunter",),
        {"action": "store_true", "help": t("help.include_hunter")},
    ),
    ArgumentSpec(
        ("--include_idiot",),
        {
            "action": argparse.BooleanOptionalAction,
            "default": False,
            "help": t("help.include_idiot"),
        },
    ),
    ArgumentSpec(
        ("--include_white_werewolf_king",),
        {"action": "store_true", "help": t("help.include_white_werewolf_king")},
    ),
    ArgumentSpec(
        ("--search_mode",),
        {
            "type": str,
            "choices": ["bfs", "dfs"],
            "default": "dfs",
            "help": t("help.search_mode"),
        },
    ),
    ArgumentSpec(
        ("--parallel_workers",),
        {"type": int, "default": DEFAULT_WORKERS, "help": t("help.parallel_workers")},
    ),
    ArgumentSpec(
        ("--lambda_risk",),
        {"type": float, "default": 0.5, "help": t("help.lambda_risk")},
    ),
    ArgumentSpec(
        ("--smart_vote",),
        {
            "action": argparse.BooleanOptionalAction,
            "default": True,
            "help": t("help.smart_vote"),
        },
    ),
    ArgumentSpec(
        ("--all_positions",),
        {
            "action": argparse.BooleanOptionalAction,
            "default": True,
            "help": t("help.all_positions"),
        },
    ),
    ArgumentSpec(
        ("--tactics",),
        {
            "type": str,
            "default": "seer_hide,villager_decoy,wolf_bloc,wolf_self_kill,wolf_no_kill",
            "help": t("help.tactics"),
        },
    ),
    ArgumentSpec(
        ("--results_output_path",),
        {"type": str, "default": "tree_results.json", "help": t("help.results_output_path")},
    ),
    ArgumentSpec(
        ("--signature_cache_db_path",),
        {
            "type": str,
            "default": "search_simulator_cache.sqlite3",
            "help": t("help.signature_cache_db_path"),
        },
    ),
    ArgumentSpec(
        ("--signature_lru_capacity",),
        {"type": int, "default": 150_000, "help": t("help.signature_lru_capacity")},
    ),
    ArgumentSpec(
        ("--signature_commit_interval",),
        {"type": int, "default": 2_000, "help": t("help.signature_commit_interval")},
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
        ("--plot_position_index",),
        {"type": int, "default": 1, "help": t("help.plot_position_index")},
    ),
    ArgumentSpec(
        ("--max_nodes_for_plot",),
        {"type": int, "default": 2500, "help": t("help.max_nodes_for_plot")},
    ),
    ArgumentSpec(
        ("--plot_dpi",),
        {"type": int, "default": 140, "help": t("help.plot_dpi")},
    ),
    ArgumentSpec(
        ("--page_size",),
        {"type": int, "default": 20, "help": t("help.page_size")},
    ),
    ArgumentSpec(("--gui",), {"action": "store_true", "help": t("help.gui")}),
    ArgumentSpec(("--cli",), {"action": "store_true", "help": t("help.cli")}),
)

SIMULATOR_ARG_KEYS: tuple[str, ...] = (
    "number_of_players",
    "number_of_wolves",
    "include_seer",
    "include_witch",
    "include_guard",
    "include_hunter",
    "include_idiot",
    "include_white_werewolf_king",
    "search_mode",
    "parallel_workers",
    "lambda_risk",
    "smart_vote",
    "all_positions",
    "tactics",
    "results_output_path",
    "signature_cache_db_path",
    "signature_lru_capacity",
    "signature_commit_interval",
)

ARTIFACT_ARG_KEYS: tuple[str, ...] = (
    "plot_position_index",
    "max_nodes_for_plot",
    "plot_dpi",
)

GUI_WINDOW_SIZE = (1460, 860)
GUI_MIN_SIZE = (1180, 720)
GUI_BASIC_ENTRY_KEYS: tuple[str, ...] = (
    "number_of_players",
    "number_of_wolves",
    "parallel_workers",
    "lambda_risk",
)
GUI_ROLE_TOGGLE_KEYS: tuple[str, ...] = (
    "include_seer",
    "include_witch",
    "include_guard",
    "include_hunter",
    "include_idiot",
    "include_white_werewolf_king",
    "smart_vote",
    "all_positions",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=t("parser.description"))
    for spec in ARGUMENT_SPECS:
        parser.add_argument(*spec.flags, **spec.kwargs)
    return parser
