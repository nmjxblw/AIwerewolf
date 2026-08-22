"""搜索后 wide/narrow reward interval 回传与阵营着色。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


def _clamp(value: float) -> float:
    if value != value:
        return 0.0
    if value < -1.0:
        return -1.0
    if value > 1.0:
        return 1.0
    return value


@dataclass(frozen=True)
class RewardInterval:
    """好人视角区间，恒满足 ``-1 <= lower <= upper <= 1``。"""

    lower: float
    upper: float

    def __post_init__(self) -> None:
        lower = _clamp(float(self.lower))
        upper = _clamp(float(self.upper))
        if lower > upper:
            lower, upper = upper, lower
        object.__setattr__(self, "lower", lower)
        object.__setattr__(self, "upper", upper)

    @property
    def midpoint(self) -> float:
        return (self.lower + self.upper) / 2.0

    @property
    def width(self) -> float:
        return self.upper - self.lower

    def to_list(self) -> list[float]:
        return [self.lower, self.upper]


@dataclass(frozen=True)
class RobustIntervals:
    """同一状态的 wide 与 narrow 两个纯观测区间。"""

    wide: RewardInterval
    narrow: RewardInterval

    def to_dict(self) -> dict[str, list[float]]:
        return {
            "wide_interval": self.wide.to_list(),
            "narrow_interval": self.narrow.to_list(),
        }


UNRESOLVED_INTERVAL = RewardInterval(-1.0, 1.0)
UNRESOLVED = RobustIntervals(UNRESOLVED_INTERVAL, UNRESOLVED_INTERVAL)
GOOD_TERMINAL = RobustIntervals(RewardInterval(1.0, 1.0), RewardInterval(1.0, 1.0))
WOLF_TERMINAL = RobustIntervals(RewardInterval(-1.0, -1.0), RewardInterval(-1.0, -1.0))


def terminal_intervals(result: str) -> RobustIntervals:
    if "好人" in str(result):
        return GOOD_TERMINAL
    if "狼人" in str(result):
        return WOLF_TERMINAL
    return UNRESOLVED


def propagate_intervals(
    children: Iterable[RobustIntervals],
    *,
    lambda_risk: float = 0.5,
) -> RobustIntervals:
    """只按唯一直接子节点、无权重回传 wide/narrow。"""

    values = propagate_interval_values(
        (
            (
                item.wide.lower,
                item.wide.upper,
                item.narrow.lower,
                item.narrow.upper,
            )
            for item in children
        ),
        lambda_risk=lambda_risk,
    )
    if values is None:
        return UNRESOLVED
    wide_lower, wide_upper, narrow_lower, narrow_upper = values
    return RobustIntervals(
        wide=RewardInterval(wide_lower, wide_upper),
        narrow=RewardInterval(narrow_lower, narrow_upper),
    )


def propagate_interval_values(
    children: Iterable[tuple[float, float, float, float]],
    *,
    lambda_risk: float = 0.5,
) -> tuple[float, float, float, float] | None:
    """以常数额外空间流式聚合子节点的四个 interval 边界。"""

    count = 0
    wide_lower_sum = 0.0
    wide_upper_sum = 0.0
    narrow_lower_sum = 0.0
    narrow_upper_sum = 0.0
    wide_lower_min = 1.0
    wide_upper_max = -1.0
    narrow_lower_max = -1.0
    narrow_upper_min = 1.0
    for raw_wide_lower, raw_wide_upper, raw_narrow_lower, raw_narrow_upper in children:
        wide_lower = _clamp(float(raw_wide_lower))
        wide_upper = _clamp(float(raw_wide_upper))
        narrow_lower = _clamp(float(raw_narrow_lower))
        narrow_upper = _clamp(float(raw_narrow_upper))
        count += 1
        wide_lower_sum += wide_lower
        wide_upper_sum += wide_upper
        narrow_lower_sum += narrow_lower
        narrow_upper_sum += narrow_upper
        wide_lower_min = min(wide_lower_min, wide_lower)
        wide_upper_max = max(wide_upper_max, wide_upper)
        narrow_lower_max = max(narrow_lower_max, narrow_lower)
        narrow_upper_min = min(narrow_upper_min, narrow_upper)
    if count == 0:
        return None
    risk = _clamp(float(lambda_risk))
    risk = max(0.0, risk)
    mean_weight = 1.0 - risk
    wide_lower = _clamp(
        risk * wide_lower_min + mean_weight * (wide_lower_sum / count)
    )
    wide_upper = _clamp(
        risk * wide_upper_max + mean_weight * (wide_upper_sum / count)
    )
    narrow_endpoint_a = _clamp(
        risk * narrow_lower_max + mean_weight * (narrow_lower_sum / count)
    )
    narrow_endpoint_b = _clamp(
        risk * narrow_upper_min + mean_weight * (narrow_upper_sum / count)
    )
    return (
        min(wide_lower, wide_upper),
        max(wide_lower, wide_upper),
        min(narrow_endpoint_a, narrow_endpoint_b),
        max(narrow_endpoint_a, narrow_endpoint_b),
    )


def interval_camp(interval: RewardInterval, *, tolerance: float = 0.001) -> str:
    """同号区间先按符号归属；跨零区间再比较端点绝对值。"""

    if interval.lower >= 0.0 and interval.upper > 0.0:
        return "good"
    if interval.lower < 0.0 and interval.upper <= 0.0:
        return "wolf"
    absolute_gap = abs(abs(interval.upper) - abs(interval.lower))
    if absolute_gap < tolerance:
        return "balanced"
    if abs(interval.upper) > abs(interval.lower):
        return "good"
    return "wolf"


def interval_branch_color(interval: RewardInterval) -> str:
    """分支颜色只应传入子节点的 wide interval。"""

    camp = interval_camp(interval)
    if camp == "good":
        return "#2563EB"
    if camp == "wolf":
        return "#DC2626"
    return "#111111"
