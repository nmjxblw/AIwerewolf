"""树分支模拟结果的 JSON 持久化与摘要日志。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ._i18n import t

logger = logging.getLogger(__name__)
_MAX_SAFE_JSON_INTEGER = (1 << 53) - 1


def _json_safe(value: Any) -> Any:
    """避免超大路径计数在 JavaScript/GUI 消费端发生精度损失。"""

    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return str(value) if abs(value) > _MAX_SAFE_JSON_INTEGER else value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def save_tree_results(
    result: dict[str, Any],
    *,
    output_path: Path | str,
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_json_safe(result), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(t("log.result_saved", path))
    return path


def report_tree_summary(result: dict[str, Any]) -> str:
    """按互斥终态记录摘要，禁止把中断误报为完成。"""

    status = str(result.get("status", "complete"))
    positions = int(result.get("position_count", 1))
    total_positions = int(result.get("total_position_count", positions))
    states = int(result.get("processed_states", result.get("state_count", 0)))
    good = int(result.get("good_paths", 0))
    wolf = int(result.get("wolf_paths", 0))
    wide = result.get("wide_interval", [-1.0, 1.0])
    narrow = result.get("narrow_interval", [-1.0, 1.0])
    values = {
        "positions": positions,
        "total": total_positions,
        "next": result.get("next_position_index") or "none",
        "states": states,
        "good": good,
        "wolf": wolf,
        "wide": f"[{float(wide[0]):.6f}, {float(wide[1]):.6f}]",
        "narrow": f"[{float(narrow[0]):.6f}, {float(narrow[1]):.6f}]",
    }
    if status == "interrupted":
        text = t("log.summary_interrupted", **values)
        logger.warning(text)
    elif status == "failed":
        text = t("log.summary_failed", **values)
        logger.error(text)
    else:
        text = t("log.summary", **values)
        logger.info(text)
    return text
