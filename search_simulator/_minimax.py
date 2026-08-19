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
    cache=None,
) -> RewardInterval:
    """返回 state 的 reward 区间（价值回传阵营无关）。

    - 终局 -> [u, u]
    - 深度耗尽 / 退化环 -> [-1, +1]（未决）
    - 否则枚举子节点递归，按 toggle/λ 合并。
    - cache：换位表（transposition table），以 (签名, depth) 缓存子图价值，
      使全深度（depth=None）评估退化为对状态 DAG 的一趟遍历，避免同一状态被
      不同路径反复重算导致的指数级重复工作。
    - 缓存值采用「打包元组 (lower, upper)」紧凑存储（AoS 打包），命中时再还原为
      RewardInterval，减少每个缓存条目持有的 dataclass 对象数量与对象抖动。
    """
    if cache is None:
        cache = {}

    signature = oracle._state_signature(state)
    # 路径环检测优先于缓存：当前路径上重复出现 -> 未决，防止无界递归。
    if signature in seen:
        return UNRESOLVED

    key = (signature, depth)
    cached = cache.get(key)
    if cached is not None:
        return RewardInterval(cached[0], cached[1])

    is_over, result = oracle._check_game_over(state)
    if is_over:
        u = terminal_utility(result)
        value = RewardInterval(u, u)
    elif depth is not None and depth <= 0:
        value = UNRESOLVED
    else:
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
                cache=cache,
            )
            for child in children
        ]
        value = merge(child_intervals, toggle=toggle, lambda_risk=lambda_risk)

    cache[key] = (value.lower, value.upper)
    return value
