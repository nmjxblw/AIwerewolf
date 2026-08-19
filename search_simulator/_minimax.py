"""区间极大极小搜索（主干）。"""

from __future__ import annotations

from ._interval import RewardInterval, UNRESOLVED, merge
from ._zero_sum import terminal_utility


def evaluate(
    state,
    *,
    depth,
    oracle,
    toggle,
    lambda_risk,
    seen=frozenset(),
) -> RewardInterval:
    """返回 state 的 reward 区间（价值回传阵营无关）。

    - 终局 -> [u, u]
    - 深度耗尽 / 退化环 -> [-1, +1]（未决）
    - 否则枚举子节点递归，按 toggle/λ 合并。
    """
    signature = oracle._state_signature(state)
    if signature in seen:
        return UNRESOLVED

    is_over, result = oracle._check_game_over(state)
    if is_over:
        u = terminal_utility(result)
        return RewardInterval(u, u)

    if depth is not None and depth <= 0:
        return UNRESOLVED

    next_seen = seen | {signature}
    next_depth = None if depth is None else depth - 1
    children = oracle.transition(state)
    child_intervals = [
        evaluate(
            child,
            depth=next_depth,
            oracle=oracle,
            toggle=toggle,
            lambda_risk=lambda_risk,
            seen=next_seen,
        )
        for child in children
    ]
    return merge(child_intervals, toggle=toggle, lambda_risk=lambda_risk)
