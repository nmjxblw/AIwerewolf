from __future__ import annotations

import logging
import time

from ._reporting import report_results

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def emit_simulation_artifacts(
    simulator,
    *,
    enable_plot: bool,
    enable_text_tree: bool,
    max_nodes_for_plot: int,
    plot_dpi: int,
    text_tree_output_path: str,
    max_text_tree_nodes: int,
) -> None:
    """输出模拟器运行产物，包括终局 JSON、统计日志和可视化结果。"""

    cache_stats = (
        simulator.signature_cache.stats_snapshot()
        if simulator.signature_cache is not None
        else None
    )
    report_results(
        endings=simulator.endings,
        wins=simulator.wins,
        search_mode=simulator.search_mode,
        stop_reason=simulator.stop_reason,
        processed_states=simulator.processed_states,
        queue_length=len(simulator.queue),
        pruned_by_limits=simulator.pruned_by_limits,
        runtime_seconds=time.monotonic() - simulator.start_time,
        build_state_path=simulator._build_state_path,
        build_labeled_state_path=simulator._build_labeled_state_path,
        cache_stats=cache_stats,
        signature_cache_db_path=simulator.signature_cache_db_path,
    )
    if enable_plot:
        from ._plotting import draw_state_tree

        draw_state_tree(
            state_parent_index=simulator.state_parent_index,
            state_action_index=simulator.state_action_index,
            state_players_snapshot=simulator.state_players_snapshot,
            endings=simulator.endings,
            build_state_path=simulator._build_state_path,
            max_nodes_for_plot=max_nodes_for_plot,
            plot_dpi=plot_dpi,
        )
    else:
        logger.info("已禁用绘图（enable_plot=False）")

    if enable_text_tree:
        from ._text_tree import export_text_state_tree

        export_text_state_tree(
            state_parent_index=simulator.state_parent_index,
            state_action_index=simulator.state_action_index,
            state_players_snapshot=simulator.state_players_snapshot,
            endings=simulator.endings,
            build_state_path=simulator._build_state_path,
            max_text_tree_nodes=max_text_tree_nodes,
            output_path=text_tree_output_path,
        )
