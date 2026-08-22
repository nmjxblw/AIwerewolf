"""统一输出树搜索 JSON、摘要日志与选定站位图。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from typing import Callable

from ._i18n import t
from ._plotting import draw_position_graph
from ._reporting import report_tree_summary
from ._reporting import save_tree_results

logger = logging.getLogger(__name__)


def _selected_position(result: dict[str, Any], position_index: int) -> dict[str, Any] | None:
    for item in result.get("positions", []):
        if int(item["position_index"]) == position_index:
            return item
    if int(result.get("position_index", -1)) == position_index:
        return result
    return None


def emit_simulation_artifacts(
    simulator: Any,
    *,
    result: dict[str, Any] | None = None,
    enable_plot: bool,
    plot_position_index: int,
    max_nodes_for_plot: int,
    plot_dpi: int,
    phase_callback: Callable[[str], None] | None = None,
) -> None:
    result = result or simulator.last_result
    if result is None:
        raise RuntimeError("模拟器尚未产生结果")

    if phase_callback is not None:
        phase_callback("report")
    save_tree_results(result, output_path=simulator.results_output_path)
    report_tree_summary(result)

    if not enable_plot:
        logger.info(t("log.plot_disabled"))
        return
    if phase_callback is not None:
        phase_callback("plot")

    selected = _selected_position(result, plot_position_index)
    if selected is None:
        logger.warning(t("log.position_missing", plot_position_index))
        return
    if "nodes" in result and "edges" in result:
        graph = {"nodes": result["nodes"], "edges": result["edges"]}
    elif simulator.signature_cache is not None:
        graph = simulator.signature_cache.get_position_graph(
            simulator.run_id,
            selected["position_signature"],
        )
    else:
        logger.warning(t("log.position_missing", plot_position_index))
        return
    output_path = Path(simulator.results_output_path).with_name(f"position_{plot_position_index}_tree.png")
    draw_position_graph(
        graph=graph,
        position_index=plot_position_index,
        output_path=output_path,
        max_nodes_for_plot=max_nodes_for_plot,
        plot_dpi=plot_dpi,
    )
