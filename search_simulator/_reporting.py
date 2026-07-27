from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from ._game_state import GameState

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
                    "state_id": state.state_id,
                    "parent_state_id": state.parent_state_id,
                    "state_path": build_state_path(state.state_id),
                    "state_path_with_actions": build_labeled_state_path(
                        state.state_id
                    ),
                    "player_state": [
                        {
                            "role": player.role,
                            "is_alive": player.is_alive,
                            "skills": player.skills,
                        }
                        for player in state.players
                    ],
                    "result": result,
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

    msg = f"总共模拟了 {len(endings)} 个终局\n"
    for result, count in sorted(wins.items()):
        msg += f"{result:<50} \t次数: {count:>5}\n"
    msg += (
        f"搜索模式: {search_mode}\n"
        f"停止原因: {stop_reason}\n"
        f"已处理状态数: {processed_states}\n"
        f"当前待处理容器长度: {queue_length}\n"
        f"因阈值裁剪分支数: {pruned_by_limits}\n"
        f"运行耗时(秒): {runtime_seconds:.2f}\n"
    )
    if cache_stats is not None:
        msg += (
            "签名缓存统计:\n"
            f"  sqlite文件: {signature_cache_db_path}\n"
            f"  LRU容量: {cache_stats['lru_capacity']}\n"
            f"  LRU命中: {cache_stats['lru_hits']}\n"
            f"  SQLite命中: {cache_stats['sqlite_hits']}\n"
            f"  新增签名: {cache_stats['inserted']}\n"
            f"  visited LRU大小: {cache_stats['visited_lru_size']}\n"
            f"  ending LRU大小: {cache_stats['ending_lru_size']}\n"
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
    logger.info("游戏结束统计:\n%s", report_text)
