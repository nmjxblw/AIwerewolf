"""在线参考驱动 + 产物。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ._interval import RewardInterval, merge
from ._i18n import t
from ._minimax import evaluate
from ._zero_sum import Camp

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def _to_list(iv: RewardInterval) -> list[float]:
    return [iv.lower, iv.upper]


def _scalar(iv: RewardInterval, toggle: str) -> float:
    return iv.upper if toggle == "optimistic" else iv.lower


def _scalar_list(iv_list: list[float], toggle: str) -> float:
    return iv_list[1] if toggle == "optimistic" else iv_list[0]


def run_online_reference(simulator, root, *, depth) -> dict:
    """深度驱动参考路径：从 root 决策到真终局/未决，产出 trace。"""
    toggle = simulator.toggle
    lambda_risk = simulator.lambda_risk

    def eval_interval(state, *, seen=frozenset()):
        return evaluate(
            state,
            depth=depth,
            oracle=simulator,
            toggle=toggle,
            lambda_risk=lambda_risk,
            seen=seen,
        )

    root_iv = eval_interval(root)
    state = root
    path_signatures = {simulator._state_signature(state)}
    steps: list[dict] = []
    outcome: str | None = None

    while outcome is None:
        is_over, result = simulator._check_game_over(state)
        if is_over:
            outcome = result
            break

        children = simulator.transition(state)
        if not children:
            outcome = "未决"
            break

        camp = Camp.WOLF if state.phase == "night" else Camp.GOOD
        child_infos = [
            (child, eval_interval(child, seen=frozenset(path_signatures)))
            for child in children
        ]
        all_ivs = [iv for _, iv in child_infos]
        opt_iv = merge(all_ivs, toggle="optimistic", lambda_risk=lambda_risk)
        con_iv = merge(all_ivs, toggle="conservative", lambda_risk=lambda_risk)
        chosen_iv = opt_iv if toggle == "optimistic" else con_iv

        if camp is Camp.GOOD:
            chosen_child, _ = max(child_infos, key=lambda t: _scalar(t[1], toggle))
        else:
            chosen_child, _ = min(child_infos, key=lambda t: _scalar(t[1], toggle))

        steps.append(
            {
                "step": len(steps) + 1,
                "phase": state.phase,
                "camp": camp.value,
                "toggle": toggle,
                "candidates": [
                    {
                        "action": child.action_label,
                        "interval": _to_list(iv),
                        "chosen": child is chosen_child,
                    }
                    for child, iv in child_infos
                ],
                "chosen_action": chosen_child.action_label,
                "optimistic_interval": _to_list(opt_iv),
                "conservative_interval": _to_list(con_iv),
                "chosen_interval": _to_list(chosen_iv),
            }
        )

        sig = simulator._state_signature(chosen_child)
        if sig in path_signatures:
            outcome = "未决"
            break
        path_signatures.add(sig)
        state = chosen_child

    return {
        "config": {
            "toggle": toggle,
            "lambda_risk": lambda_risk,
            "lookahead_depth": depth,
        },
        "steps": steps,
        "outcome": outcome,
        "reward_interval": _to_list(root_iv),
    }


def emit_online_artifacts(simulator, trace: dict) -> None:
    """写 online_trace.json + 结果摘要。"""
    path = Path(simulator.online_trace_path)
    with path.open("w", encoding="utf-8") as f:
        json.dump(trace, f, ensure_ascii=False, indent=2)
    logger.info(t("log.online_trace_saved", path))

    lines = [
        t("log.online_result", trace["outcome"]),
        t("log.online_root_interval", trace["reward_interval"]),
        t("log.online_step_count", len(trace["steps"])),
    ]
    for s in trace["steps"]:
        lines.append(
            t(
                "log.online_step",
                s["step"],
                s["phase"],
                s["camp"],
                s["chosen_action"],
                s["chosen_interval"],
            )
        )
    logger.info(t("log.online_summary", "\n".join(lines)))

    try:
        from ._plotting import draw_reference_path

        draw_reference_path(trace)
    except Exception as exc:  # 绘图失败不影响主流程
        logger.debug(t("log.ref_path_plot_failed", exc))


def evaluate_against_exact(simulator, root, online_trace: dict) -> dict:
    """以全深度（depth=None）重算，输出 regret / 行动吻合率。"""
    exact_trace = run_online_reference(simulator, root, depth=None)
    toggle = simulator.toggle
    online_root = _scalar_list(online_trace["reward_interval"], toggle)
    exact_root = _scalar_list(exact_trace["reward_interval"], toggle)
    regret = exact_root - online_root

    n = min(len(online_trace["steps"]), len(exact_trace["steps"]))
    agree = sum(
        1
        for i in range(n)
        if online_trace["steps"][i]["chosen_action"]
        == exact_trace["steps"][i]["chosen_action"]
    )
    agreement = agree / n if n else 1.0

    result = {
        "online_root_value": online_root,
        "exact_root_value": exact_root,
        "regret": regret,
        "action_agreement_rate": agreement,
    }
    path = Path("online_eval.json")
    with path.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info(t("log.exact_saved", path, result))
    return result
