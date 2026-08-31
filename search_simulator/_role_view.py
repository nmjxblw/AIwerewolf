"""角色视角精确条件化、posterior 枚举和离散抽样。"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from ._decision_state import CanonicalGameConfig
from ._decision_state import DecisionState
from ._decision_state import WorldState
from ._decision_state import camp_for_role
from ._decision_state import is_wolf_role
from ._positions import iter_unique_role_orders


@lru_cache(maxsize=8)
def _cached_layouts(roles: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    """缓存固定板子的唯一站位，避免每个 rollout 递归生成 1260 次排列。"""

    return tuple(iter_unique_role_orders(roles))


@dataclass(frozen=True, slots=True)
class RoleView:
    """行动者依法可见的硬知识。

    ``known_roles`` 只放确切角色（自身和规则明确揭示的角色）；狼人队友
    使用 ``known_camps`` 表示阵营知识，避免把扩展狼角色误写成精确子角色。
    """

    actor_id: int
    actor_role: str
    known_roles: tuple[tuple[int, str], ...] = ()
    known_camps: tuple[tuple[int, str], ...] = ()
    seer_checks: tuple[tuple[int, int, bool], ...] = ()
    view_spec: str = "role-view-hard-knowledge"

    def key(self) -> tuple[Any, ...]:
        """返回稳定的角色视角键。"""

        return (
            int(self.actor_id),
            str(self.actor_role),
            tuple((int(index), str(role)) for index, role in self.known_roles),
            tuple((int(index), str(camp)) for index, camp in self.known_camps),
            tuple((int(observer), int(target), bool(is_wolf)) for observer, target, is_wolf in self.seer_checks),
            str(self.view_spec),
        )

    def digest(self) -> str:
        """返回角色视角摘要，不暴露完整隐藏站位。"""

        payload = json.dumps(self.key(), ensure_ascii=False, separators=(",", ":"))
        return hashlib.blake2b(payload.encode("utf-8"), digest_size=16).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """序列化角色视角以便审计和查询。"""

        return {
            "actor_id": self.actor_id,
            "actor_role": self.actor_role,
            "known_roles": [list(item) for item in self.known_roles],
            "known_camps": [list(item) for item in self.known_camps],
            "seer_checks": [list(item) for item in self.seer_checks],
            "view_spec": self.view_spec,
        }


def build_role_view(
    roles: tuple[str, ...] | list[str],
    state: DecisionState | WorldState,
    *,
    actor_id: int,
) -> RoleView:
    """从真实世界构造一个行动者合法视角。

    该函数只用于 worker 内部或受控请求构建；返回值不包含其他玩家的完整
    角色。公开声明和死亡本身不会自动升级为 ``known_roles``。
    """

    role_tuple = tuple(str(role) for role in roles)
    index = int(actor_id)
    if not (0 <= index < len(role_tuple)):
        raise ValueError("actor_id 超出角色站位范围")
    actor_role = role_tuple[index]
    known_roles: dict[int, str] = {index: actor_role}
    known_camps: dict[int, str] = {index: camp_for_role(actor_role)}
    if is_wolf_role(actor_role):
        for seat, role in enumerate(role_tuple):
            if is_wolf_role(role):
                known_camps[seat] = "wolf"

    seer_checks: dict[tuple[int, int], bool] = {}
    if isinstance(state, WorldState):
        own_checks = state.private_seer_checks.get(index, {})
        for target, is_wolf in own_checks.items():
            seer_checks[(index, int(target))] = bool(is_wolf)
        for seat, role in state.public_role_claims.items():
            # 公开身份揭示即使发生在死亡后仍然是硬知识；这里不以存活状态
            # 过滤，避免把愚者等规则明确揭示的身份错误降级为未知。
            if role == "愚者":
                known_roles[int(seat)] = "愚者"
                known_camps[int(seat)] = "good"

    return RoleView(
        actor_id=index,
        actor_role=actor_role,
        known_roles=tuple(sorted(known_roles.items())),
        known_camps=tuple(sorted(known_camps.items())),
        seer_checks=tuple((observer, target, value) for (observer, target), value in sorted(seer_checks.items())),
    )


def _speech_likelihood(
    roles: tuple[str, ...],
    event: tuple[Any, ...],
) -> float:
    """计算一条结构化公开发言在隐藏站位下的基础似然。"""

    if not event or event[0] != "speech":
        return 1.0
    _tag, speaker, family, claim_role, target, result, _tactic = (tuple(event) + (None,) * 7)[:7]
    speaker_index = int(speaker)
    if not (0 <= speaker_index < len(roles)):
        return 0.1
    if family != "seer_claim":
        return 1.0
    if claim_role != "预言家":
        return 1.0
    is_real_seer = roles[speaker_index] == "预言家"
    if target is None or result not in {"good", "wolf"}:
        return 1.0 if is_real_seer else 0.2
    target_index = int(target)
    if not (0 <= target_index < len(roles)) or target_index == speaker_index:
        return 0.1
    truthful = camp_for_role(roles[target_index]) == str(result)
    if is_real_seer:
        return 1.0 if truthful else 0.05
    # 假跳仍可能命中真实阵营，因此命中时只是较强证据而非硬事实。
    return 0.35 if truthful else 0.15


def posterior_layouts(
    config: CanonicalGameConfig,
    role_view: RoleView,
    state: DecisionState,
    *,
    credibility: float = 0.0,
) -> tuple[tuple[tuple[str, ...], float], ...]:
    """精确枚举与角色视角相容的隐藏站位及其归一化权重。"""

    strength = float(credibility)
    if not 0.0 <= strength <= 1.0:
        raise ValueError("credibility 必须在 [0,1] 范围内")
    known_roles = dict(role_view.known_roles)
    known_camps = dict(role_view.known_camps)
    checks = {(int(observer), int(target)): bool(value) for observer, target, value in role_view.seer_checks}
    candidates: list[tuple[tuple[str, ...], float]] = []
    for roles in _cached_layouts(tuple(config.roles)):
        if any(roles[index] != role for index, role in known_roles.items()):
            continue
        if any(camp_for_role(roles[index]) != camp for index, camp in known_camps.items()):
            continue
        if any(
            camp_for_role(roles[target]) != ("wolf" if value else "good")
            for (_observer, target), value in checks.items()
        ):
            continue
        weight = 1.0
        for event in state.public_events:
            likelihood = _speech_likelihood(roles, tuple(event))
            weight *= (1.0 - strength) + strength * likelihood
        if weight > 0.0:
            candidates.append((roles, weight))
    total = sum(weight for _roles, weight in candidates)
    if total <= 0.0:
        raise ValueError("角色视角与公开证据不一致，posterior 权重全为零")
    return tuple((roles, weight / total) for roles, weight in candidates)


def sample_layout(
    posterior: tuple[tuple[tuple[str, ...], float], ...],
    rng: random.Random,
) -> tuple[str, ...]:
    """从完整离散 posterior 直接抽样，不使用 MCMC。"""

    threshold = rng.random()
    cumulative = 0.0
    for roles, weight in posterior:
        cumulative += float(weight)
        if threshold <= cumulative:
            return roles
    return posterior[-1][0]


def posterior_wolf_probability(
    posterior: tuple[tuple[tuple[str, ...], float], ...],
    target_id: int,
) -> float:
    """计算 posterior 下目标属于狼人的概率。"""

    target = int(target_id)
    return sum(float(weight) for roles, weight in posterior if 0 <= target < len(roles) and is_wolf_role(roles[target]))


def policy_wolf_probabilities(
    config: CanonicalGameConfig,
    role_view: RoleView,
    state: DecisionState,
    *,
    credibility: float,
) -> dict[int, float]:
    """为 rollout policy 提供快速的边缘狼人概率。

    矩阵世界抽样仍使用 ``posterior_layouts`` 的完整精确枚举；策略热路径只
    需要逐目标边缘概率，因此使用硬知识后的剩余狼人比例并顺序吸收结构化
    公开查验证据，避免每个未来动作重复生成 1260 个排列。
    """

    strength = max(0.0, min(1.0, float(credibility)))
    known_roles = dict(role_view.known_roles)
    known_camps = dict(role_view.known_camps)
    known_wolves = {index for index, camp in known_camps.items() if camp == "wolf"}
    unknown = [
        index for index in range(config.number_of_players) if index not in known_roles and index not in known_camps
    ]
    remaining_wolves = max(0, int(config.number_of_wolves) - len(known_wolves))
    prior = remaining_wolves / len(unknown) if unknown else 0.0
    probabilities = {
        index: (1.0 if index in known_wolves else 0.0 if index in known_roles or index in known_camps else prior)
        for index in range(config.number_of_players)
    }
    checked_targets = set()
    for observer, target, is_wolf in role_view.seer_checks:
        if int(observer) != int(role_view.actor_id) or int(target) not in probabilities:
            continue
        target_index = int(target)
        checked_targets.add(target_index)
        probabilities[target_index] = 1.0 if bool(is_wolf) else 0.0
    for event in state.public_events:
        if not event or event[0] != "speech" or len(event) < 6 or event[2] != "seer_claim":
            continue
        target = event[4]
        result = event[5]
        if target is None or result not in {"good", "wolf"}:
            continue
        target_index = int(target)
        if target_index not in probabilities:
            continue
        # 私有查验是硬知识，公开声明只能影响未知目标，不能覆盖行动者
        # 已经知道的结果。
        if target_index in checked_targets:
            continue
        p = probabilities[target_index]
        if result == "wolf":
            like_wolf, like_good = 0.70, 0.20
        else:
            like_wolf, like_good = 0.20, 0.70
        like_wolf = (1.0 - strength) + strength * like_wolf
        like_good = (1.0 - strength) + strength * like_good
        denominator = p * like_wolf + (1.0 - p) * like_good
        if denominator > 0.0:
            probabilities[target_index] = p * like_wolf / denominator
    return probabilities


def posterior_digest(
    posterior: tuple[tuple[tuple[str, ...], float], ...],
) -> str:
    """摘要完整 posterior 的规范内容，供矩阵请求身份使用。"""

    payload = json.dumps(
        [[list(roles), round(float(weight), 16)] for roles, weight in posterior],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=16).hexdigest()
