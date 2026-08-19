"""reward 区间类型与聚合。"""

from __future__ import annotations

from dataclasses import dataclass


def _clamp(value: float) -> float:
    """夹取到 [-1, 1]，NaN 归 0，inf 归边界。"""
    if value != value:  # NaN
        return 0.0
    if value > 1.0:
        return 1.0
    if value < -1.0:
        return -1.0
    return value


@dataclass(frozen=True)
class RewardInterval:
    """reward 区间 [lower, upper]，恒满足 -1 <= lower <= upper <= +1。"""

    lower: float
    upper: float

    def __post_init__(self) -> None:
        low = _clamp(self.lower)
        high = _clamp(self.upper)
        if low > high:
            low, high = high, low
        object.__setattr__(self, "lower", low)
        object.__setattr__(self, "upper", high)

    @property
    def width(self) -> float:
        return self.upper - self.lower

    @property
    def midpoint(self) -> float:
        return (self.lower + self.upper) / 2.0


UNRESOLVED = RewardInterval(-1.0, 1.0)
"""未决（前沿/深度耗尽/退化环）区间。"""


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def merge(intervals, *, toggle: str, lambda_risk: float) -> RewardInterval:
    """按算法文档 §4.3 合并：乐观=并集、保守=交集，并用 λ 向均值折中。"""
    intervals = list(intervals)
    if not intervals:
        return UNRESOLVED
    lowers = [iv.lower for iv in intervals]
    uppers = [iv.upper for iv in intervals]
    mean_lower = _mean(lowers)
    mean_upper = _mean(uppers)
    lam = _clamp(lambda_risk)
    if toggle == "optimistic":
        lower = lam * min(lowers) + (1.0 - lam) * mean_lower
        upper = lam * max(uppers) + (1.0 - lam) * mean_upper
    else:  # conservative
        lower = lam * max(lowers) + (1.0 - lam) * mean_lower
        upper = lam * min(uppers) + (1.0 - lam) * mean_upper
    return RewardInterval(lower, upper)


def compare(a: RewardInterval, b: RewardInterval, toggle: str) -> int:
    """按 toggle 比较两个区间：乐观比 upper、保守比 lower。返回 -1/0/1。"""
    key_a = a.upper if toggle == "optimistic" else a.lower
    key_b = b.upper if toggle == "optimistic" else b.lower
    if key_a > key_b:
        return 1
    if key_a < key_b:
        return -1
    return 0
