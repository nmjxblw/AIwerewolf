"""双阵营零和抽象。"""

from __future__ import annotations

from enum import Enum


class Camp(str, Enum):
    GOOD = "good"
    WOLF = "wolf"


_WOLF_ROLES = {"狼人", "白狼王"}


def is_wolf_role(role: str) -> bool:
    """角色是否属于狼人阵营。"""
    return role in _WOLF_ROLES


def camp_of_role(role: str) -> Camp:
    """角色 → 阵营：狼人/白狼王 → WOLF，其余 → GOOD。"""
    return Camp.WOLF if is_wolf_role(role) else Camp.GOOD


def terminal_utility(result: str) -> float:
    """终局结果字符串 → 好人视角价值：好人胜 +1，否则 -1。"""
    return 1.0 if "好人" in result else -1.0
