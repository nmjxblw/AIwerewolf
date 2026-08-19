from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from ._game_state import GameState
from ._i18n import t

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def save_endings_json(
    *,
    endings: list[tuple[GameState, str]],
    build_state_path: Callable[[int], list[int]],
    build_labeled_state_path: Callable[[int], list[dict[str, int | str | None]]],
    output_path: Path | str = "endings.json",
) -> None:
    """保存搜索得到的终局状态。"""

    path = Path(output_path)
    with path.open("w", encoding="utf-8") as f:
        json.dump(
            [
                {
                    "节点ID": state.state_id,
                    "父节点ID": state.parent_state_id,
                    "状态路径": build_state_path(state.state_id),
                    "带操作的状态路径": build_labeled_state_path(state.state_id),
                    "玩家状态": [
                        {
                            "角色": player.role,
                            "是否存活": player.is_alive,
                            "技能": player.skills,
                        }
                        for player in state.players
                    ],
                    "结果": result,
                    "深度": state.depth,
                    "reward_interval": state.reward_interval,
                }
                for state, result in endings
            ],
            f,
            ensure_ascii=False,
            indent=4,
        )


def build_results_report(
    *,
    endings: list[tuple[GameState, str]],
    wins: dict[str, int],
    search_mode: str,
    stop_reason: str,
    processed_states: int,
    queue_length: int,
    pruned_by_limits: int,
    runtime_seconds: float,
    cache_stats: dict[str, int] | None = None,
    signature_cache_db_path: Path | None = None,
) -> str:
    """构建搜索结果摘要文本。"""

    msg = t("report.total_endings", len(endings))
    for result, count in sorted(wins.items()):
        msg += t("report.result_count", result, count)
    msg += (
        t("report.search_mode", search_mode)
        + t("report.stop_reason", stop_reason)
        + t("report.processed", processed_states)
        + t("report.queue_length", queue_length)
        + t("report.pruned", pruned_by_limits)
        + t("report.runtime", runtime_seconds)
    )
    if cache_stats is not None:
        msg += (
            t("report.cache_stats_title")
            + t("report.cache_db", signature_cache_db_path)
            + t("report.cache_lru_capacity", cache_stats["lru_capacity"])
            + t("report.cache_lru_hits", cache_stats["lru_hits"])
            + t("report.cache_sqlite_hits", cache_stats["sqlite_hits"])
            + t("report.cache_inserted", cache_stats["inserted"])
            + t("report.cache_visited_size", cache_stats["visited_lru_size"])
            + t("report.cache_ending_size", cache_stats["ending_lru_size"])
        )
    return msg


def report_results(
    *,
    endings: list[tuple[GameState, str]],
    wins: dict[str, int],
    search_mode: str,
    stop_reason: str,
    processed_states: int,
    queue_length: int,
    pruned_by_limits: int,
    runtime_seconds: float,
    build_state_path: Callable[[int], list[int]],
    build_labeled_state_path: Callable[[int], list[dict[str, int | str | None]]],
    cache_stats: dict[str, int] | None = None,
    signature_cache_db_path: Path | None = None,
    endings_output_path: Path | str = "endings.json",
) -> None:
    """保存终局并输出搜索结果摘要。"""

    save_endings_json(
        endings=endings,
        build_state_path=build_state_path,
        build_labeled_state_path=build_labeled_state_path,
        output_path=endings_output_path,
    )
    report_text = build_results_report(
        endings=endings,
        wins=wins,
        search_mode=search_mode,
        stop_reason=stop_reason,
        processed_states=processed_states,
        queue_length=queue_length,
        pruned_by_limits=pruned_by_limits,
        runtime_seconds=runtime_seconds,
        cache_stats=cache_stats,
        signature_cache_db_path=signature_cache_db_path,
    )
    logger.info(t("log.game_end_stats", report_text))
