"""玩家站位排列枚举。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from hashlib import blake2b
from typing import Iterator

from ._player import Player

ROLE_SKILLS: dict[str, dict[str, int]] = {
    "村民": {},
    "狼人": {"攻击": -1},
    "白狼王": {"带走击杀": 1},
    "预言家": {"查验": -1},
    "女巫": {"解药": 1, "毒药": 1},
    "守卫": {"保护": -1},
    "猎人": {"开枪": 1},
    "愚者": {"身份揭示": 1},
}


@dataclass(frozen=True)
class PositionLayout:
    """一个去重后的角色站位。"""

    index: int
    roles: tuple[str, ...]
    signature: str

    @property
    def display(self) -> str:
        return " | ".join(f"{seat + 1}:{role}" for seat, role in enumerate(self.roles))


def build_role_roster(
    *,
    number_of_players: int,
    number_of_wolves: int,
    include_seer: bool,
    include_witch: bool,
    include_guard: bool,
    include_hunter: bool,
    include_idiot: bool,
    include_white_werewolf_king: bool,
) -> tuple[str, ...]:
    """根据板子配置构建角色多重集合。"""

    roles: list[str] = []
    if include_seer:
        roles.append("预言家")
    if include_witch:
        roles.append("女巫")
    if include_guard:
        roles.append("守卫")
    if include_hunter:
        roles.append("猎人")
    if include_idiot:
        roles.append("愚者")
    if include_white_werewolf_king:
        roles.append("白狼王")
    roles.extend("狼人" for _ in range(max(0, number_of_wolves)))
    villagers = number_of_players - len(roles)
    if villagers < 0:
        raise ValueError("角色数量超过玩家人数")
    roles.extend("村民" for _ in range(villagers))
    if not any(role in {"狼人", "白狼王"} for role in roles):
        raise ValueError("至少需要一名狼人阵营玩家")
    if not any(role not in {"狼人", "白狼王"} for role in roles):
        raise ValueError("至少需要一名好人阵营玩家")
    return tuple(roles)


def position_signature(roles: tuple[str, ...] | list[str]) -> str:
    """构造同时可读且稳定的站位签名。"""

    canonical = "|".join(str(role) for role in roles)
    digest = blake2b(canonical.encode("utf-8"), digest_size=8).hexdigest()
    return f"{digest}:{canonical}"


def iter_unique_role_orders(roles: tuple[str, ...]) -> Iterator[tuple[str, ...]]:
    """用多重集合回溯枚举唯一排列，不先生成重复的 ``n!`` 个元组。"""

    counts = Counter(roles)
    role_order = tuple(dict.fromkeys(roles))
    current: list[str] = []

    def visit() -> Iterator[tuple[str, ...]]:
        if len(current) == len(roles):
            yield tuple(current)
            return
        for role in role_order:
            if counts[role] <= 0:
                continue
            counts[role] -= 1
            current.append(role)
            yield from visit()
            current.pop()
            counts[role] += 1

    yield from visit()


def enumerate_position_layouts(roles: tuple[str, ...]) -> list[PositionLayout]:
    """返回稳定编号的全部唯一站位。"""

    return [
        PositionLayout(index=index, roles=order, signature=position_signature(order))
        for index, order in enumerate(iter_unique_role_orders(roles), start=1)
    ]


def players_for_layout(layout: PositionLayout | tuple[str, ...]) -> list[Player]:
    """把站位转换成新的 Player 列表。"""

    roles = layout.roles if isinstance(layout, PositionLayout) else layout
    return [Player(role=role, is_alive=True, skills=dict(ROLE_SKILLS.get(role, {}))) for role in roles]
