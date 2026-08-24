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


@dataclass(slots=True)
class _IntervalValueAccumulator:
    """以固定大小字段累计直接子节点边界，不创建逐子节点临时对象。"""

    count: int = 0
    wide_lower_sum: float = 0.0
    wide_upper_sum: float = 0.0
    narrow_lower_sum: float = 0.0
    narrow_upper_sum: float = 0.0
    wide_lower_min: float = 1.0
    wide_upper_max: float = -1.0
    narrow_lower_max: float = -1.0
    narrow_upper_min: float = 1.0

    def add(
        self,
        raw_wide_lower: float,
        raw_wide_upper: float,
        raw_narrow_lower: float,
        raw_narrow_upper: float,
    ) -> None:
        wide_lower = _clamp(float(raw_wide_lower))
        wide_upper = _clamp(float(raw_wide_upper))
        narrow_lower = _clamp(float(raw_narrow_lower))
        narrow_upper = _clamp(float(raw_narrow_upper))
        self.count += 1
        self.wide_lower_sum += wide_lower
        self.wide_upper_sum += wide_upper
        self.narrow_lower_sum += narrow_lower
        self.narrow_upper_sum += narrow_upper
        if wide_lower < self.wide_lower_min:
            self.wide_lower_min = wide_lower
        if wide_upper > self.wide_upper_max:
            self.wide_upper_max = wide_upper
        if narrow_lower > self.narrow_lower_max:
            self.narrow_lower_max = narrow_lower
        if narrow_upper < self.narrow_upper_min:
            self.narrow_upper_min = narrow_upper

    def resolve(
        self,
        *,
        lambda_risk: float,
    ) -> tuple[float, float, float, float] | None:
        if self.count == 0:
            return None
        risk = _clamp(float(lambda_risk))
        if risk < 0.0:
            risk = 0.0
        mean_weight = 1.0 - risk
        wide_lower = _clamp(
            risk * self.wide_lower_min
            + mean_weight * (self.wide_lower_sum / self.count)
        )
        wide_upper = _clamp(
            risk * self.wide_upper_max
            + mean_weight * (self.wide_upper_sum / self.count)
        )
        narrow_endpoint_a = _clamp(
            risk * self.narrow_lower_max
            + mean_weight * (self.narrow_lower_sum / self.count)
        )
        narrow_endpoint_b = _clamp(
            risk * self.narrow_upper_min
            + mean_weight * (self.narrow_upper_sum / self.count)
        )
        if wide_lower > wide_upper:
            wide_lower, wide_upper = wide_upper, wide_lower
        if narrow_endpoint_a > narrow_endpoint_b:
            narrow_endpoint_a, narrow_endpoint_b = (
                narrow_endpoint_b,
                narrow_endpoint_a,
            )
        return (
            wide_lower,
            wide_upper,
            narrow_endpoint_a,
            narrow_endpoint_b,
        )


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

    accumulator = _IntervalValueAccumulator()
    for item in children:
        accumulator.add(
            item.wide.lower,
            item.wide.upper,
            item.narrow.lower,
            item.narrow.upper,
        )
    values = accumulator.resolve(lambda_risk=lambda_risk)
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

    accumulator = _IntervalValueAccumulator()
    for child_values in children:
        accumulator.add(
            child_values[0],
            child_values[1],
            child_values[2],
            child_values[3],
        )
    return accumulator.resolve(lambda_risk=lambda_risk)


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
