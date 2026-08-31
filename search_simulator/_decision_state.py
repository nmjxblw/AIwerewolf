"""精确信念 Cheap-talk 决策矩阵的决策状态与隐藏世界模型。

本模块只保存能够影响未来转移的规范状态，以及 worker 内部使用的完整
隐藏世界。完整角色站位永远不放入 ``DecisionState``，必须通过
``RoleView`` 投影后才可交给行动策略。
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any

WOLF_ROLES = frozenset({"狼人", "白狼王"})
GOOD_ROLES = frozenset({"村民", "预言家", "女巫", "守卫", "猎人", "愚者"})


def is_wolf_role(role: str) -> bool:
    """返回角色是否属于狼人阵营。"""

    return str(role) in WOLF_ROLES


def camp_for_role(role: str) -> str:
    """把角色映射为 ``good`` 或 ``wolf`` 阵营。"""

    return "wolf" if is_wolf_role(role) else "good"


@dataclass(frozen=True, slots=True)
class CanonicalGameConfig:
    """影响未来转移的规范板子配置。

    ``roles`` 是角色多重集合的稳定排序，不包含某一次具体隐藏站位。
    具体站位属于 posterior 的样本空间，而不是配置本身。
    """

    number_of_players: int
    number_of_wolves: int
    roles: tuple[str, ...]
    max_days: int = 8
    rules_spec: str = "seven-player-microphase-rules"

    @classmethod
    def from_roles(
        cls,
        roles: tuple[str, ...] | list[str],
        *,
        max_days: int = 8,
        rules_spec: str = "seven-player-microphase-rules",
    ) -> "CanonicalGameConfig":
        ordered = tuple(sorted(str(role) for role in roles))
        return cls(
            number_of_players=len(ordered),
            number_of_wolves=sum(is_wolf_role(role) for role in ordered),
            roles=ordered,
            max_days=max(1, int(max_days)),
            rules_spec=str(rules_spec),
        )

    def key(self) -> tuple[Any, ...]:
        """返回用于请求摘要的稳定元组。"""

        return (
            int(self.number_of_players),
            int(self.number_of_wolves),
            tuple(self.roles),
            int(self.max_days),
            str(self.rules_spec),
        )


@dataclass(frozen=True, slots=True)
class DecisionState:
    """行动者发言前可见的顺序化决策状态。

    该类型只含公开状态和动作轮次，不含完整真实角色。私有查验、狼队
    队友等信息由 ``RoleView`` 单独携带，避免策略层误读上帝视角。
    """

    alive: tuple[bool, ...]
    phase: str = "day_speech"
    day_count: int = 0
    night_count: int = 1
    speech_order: tuple[int, ...] = ()
    speech_index: int = 0
    actor_id: int = 0
    public_role_claims: tuple[tuple[int, str], ...] = ()
    public_events: tuple[tuple[Any, ...], ...] = ()
    last_guard_target: int | None = None
    witch_save_available: bool = True
    witch_poison_available: bool = True
    winner: str | None = None

    @classmethod
    def first_day_speech(
        cls,
        number_of_players: int,
        *,
        actor_id: int = 0,
        alive: tuple[bool, ...] | None = None,
        public_events: tuple[tuple[Any, ...], ...] = (),
    ) -> "DecisionState":
        """构造第一天逐席位发言状态。

        参数：
            number_of_players: 板子人数，必须为正数。
            actor_id: 当前实际发言席位。
            alive: 可选存活向量；省略时默认全员存活。
            public_events: 已公开且会影响未来转移的结构化事件。

        返回：
            不含隐藏角色的发言前决策状态。
        """

        count = int(number_of_players)
        if count <= 0:
            raise ValueError("number_of_players 必须为正数")
        flags = tuple(True for _ in range(count)) if alive is None else tuple(alive)
        if len(flags) != count:
            raise ValueError("alive 长度必须等于 number_of_players")
        if not (0 <= int(actor_id) < count) or not flags[int(actor_id)]:
            raise ValueError("actor_id 必须指向存活席位")
        order = tuple(index for index, is_alive in enumerate(flags) if is_alive)
        try:
            index = order.index(int(actor_id))
        except ValueError as exc:
            raise ValueError("actor_id 不在发言顺序中") from exc
        return cls(
            alive=flags,
            speech_order=order,
            speech_index=index,
            actor_id=int(actor_id),
            public_events=tuple(tuple(event) for event in public_events),
        )

    @property
    def public_claim_map(self) -> dict[int, str]:
        """返回公开声明的可变副本。"""

        return {int(index): str(role) for index, role in self.public_role_claims}

    def key(self) -> tuple[Any, ...]:
        """返回不含界面字段、可用于缓存的规范状态键。"""

        return (
            tuple(bool(value) for value in self.alive),
            str(self.phase),
            int(self.day_count),
            int(self.night_count),
            tuple(int(index) for index in self.speech_order),
            int(self.speech_index),
            int(self.actor_id),
            tuple((int(index), str(role)) for index, role in self.public_role_claims),
            tuple(tuple(event) for event in self.public_events),
            None if self.last_guard_target is None else int(self.last_guard_target),
            bool(self.witch_save_available),
            bool(self.witch_poison_available),
            self.winner,
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为 worker 和 API 可传递的普通字典。"""

        return {
            "alive": list(self.alive),
            "phase": self.phase,
            "day_count": self.day_count,
            "night_count": self.night_count,
            "speech_order": list(self.speech_order),
            "speech_index": self.speech_index,
            "actor_id": self.actor_id,
            "public_role_claims": [list(item) for item in self.public_role_claims],
            "public_events": [list(item) for item in self.public_events],
            "last_guard_target": self.last_guard_target,
            "witch_save_available": self.witch_save_available,
            "witch_poison_available": self.witch_poison_available,
            "winner": self.winner,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DecisionState":
        """从规范字典恢复决策状态并校验字段长度。"""

        alive = tuple(bool(value) for value in data.get("alive", ()))
        if not alive:
            raise ValueError("DecisionState.alive 不能为空")
        return cls(
            alive=alive,
            phase=str(data.get("phase", "day_speech")),
            day_count=int(data.get("day_count", 0)),
            night_count=int(data.get("night_count", 1)),
            speech_order=tuple(int(value) for value in data.get("speech_order", ())),
            speech_index=int(data.get("speech_index", 0)),
            actor_id=int(data.get("actor_id", 0)),
            public_role_claims=tuple((int(item[0]), str(item[1])) for item in data.get("public_role_claims", ())),
            public_events=tuple(tuple(item) for item in data.get("public_events", ())),
            last_guard_target=(None if data.get("last_guard_target") is None else int(data["last_guard_target"])),
            witch_save_available=bool(data.get("witch_save_available", True)),
            witch_poison_available=bool(data.get("witch_poison_available", True)),
            winner=(None if data.get("winner") is None else str(data["winner"])),
        )


@dataclass(slots=True)
class WorldState:
    """worker 内部的完整隐藏世界。

    该对象只存在于单条 Monte Carlo 轨迹中，不跨进程传输、不写入矩阵，
    并通过 ``clone`` 为每个候选动作建立独立分支。
    """

    roles: tuple[str, ...]
    alive: list[bool]
    phase: str = "day_speech"
    day_count: int = 0
    night_count: int = 1
    speech_order: tuple[int, ...] = ()
    speech_index: int = 0
    actor_id: int = 0
    public_role_claims: dict[int, str] = field(default_factory=dict)
    public_events: list[tuple[Any, ...]] = field(default_factory=list)
    private_seer_checks: dict[int, dict[int, bool]] = field(default_factory=dict)
    last_guard_target: int | None = None
    witch_save_available: bool = True
    witch_poison_available: bool = True
    first_night: bool = False
    pending_votes: dict[int, int] = field(default_factory=dict)
    pending_wolf_votes: dict[int, int] = field(default_factory=dict)
    pending_wolf_target: int | None = None
    pending_guard_target: int | None = None
    pending_witch_action: str = "none"
    pending_witch_target: int | None = None
    pending_seer_target: int | None = None
    winner: str | None = None
    terminal_reason: str = ""

    @classmethod
    def from_state(
        cls,
        roles: tuple[str, ...],
        state: DecisionState,
        *,
        private_seer_checks: dict[int, dict[int, bool]] | None = None,
    ) -> "WorldState":
        """把抽样站位和公开决策状态合成为单条轨迹世界。"""

        if len(roles) != len(state.alive):
            raise ValueError("roles 与 state.alive 长度不一致")
        return cls(
            roles=tuple(str(role) for role in roles),
            alive=[bool(value) for value in state.alive],
            phase=str(state.phase),
            day_count=int(state.day_count),
            night_count=int(state.night_count),
            speech_order=tuple(state.speech_order),
            speech_index=int(state.speech_index),
            actor_id=int(state.actor_id),
            public_role_claims=state.public_claim_map,
            public_events=[tuple(event) for event in state.public_events],
            private_seer_checks={
                int(actor): {int(target): bool(value) for target, value in checks.items()}
                for actor, checks in (private_seer_checks or {}).items()
            },
            last_guard_target=state.last_guard_target,
            witch_save_available=state.witch_save_available,
            witch_poison_available=state.witch_poison_available,
        )

    def clone(self) -> "WorldState":
        """复制一条轨迹，确保候选动作之间不共享可变状态。"""

        return WorldState(
            roles=self.roles,
            alive=list(self.alive),
            phase=self.phase,
            day_count=self.day_count,
            night_count=self.night_count,
            speech_order=self.speech_order,
            speech_index=self.speech_index,
            actor_id=self.actor_id,
            public_role_claims=dict(self.public_role_claims),
            public_events=[tuple(event) for event in self.public_events],
            private_seer_checks={actor: dict(checks) for actor, checks in self.private_seer_checks.items()},
            last_guard_target=self.last_guard_target,
            witch_save_available=self.witch_save_available,
            witch_poison_available=self.witch_poison_available,
            first_night=self.first_night,
            pending_votes=dict(self.pending_votes),
            pending_wolf_votes=dict(self.pending_wolf_votes),
            pending_wolf_target=self.pending_wolf_target,
            pending_guard_target=self.pending_guard_target,
            pending_witch_action=self.pending_witch_action,
            pending_witch_target=self.pending_witch_target,
            pending_seer_target=self.pending_seer_target,
            winner=self.winner,
            terminal_reason=self.terminal_reason,
        )

    @property
    def alive_indices(self) -> tuple[int, ...]:
        """返回当前存活席位。"""

        return tuple(index for index, value in enumerate(self.alive) if value)

    @property
    def alive_wolf_indices(self) -> tuple[int, ...]:
        """返回当前存活狼人席位。"""

        return tuple(index for index in self.alive_indices if is_wolf_role(self.roles[index]))

    def to_public_state(self) -> DecisionState:
        """投影为不含完整真实角色的公开决策状态。"""

        order = self.speech_order or tuple(self.alive_indices)
        return DecisionState(
            alive=tuple(self.alive),
            phase=self.phase,
            day_count=self.day_count,
            night_count=self.night_count,
            speech_order=order,
            speech_index=self.speech_index,
            actor_id=self.actor_id,
            public_role_claims=tuple(sorted(self.public_role_claims.items())),
            public_events=tuple(tuple(event) for event in self.public_events),
            last_guard_target=self.last_guard_target,
            witch_save_available=self.witch_save_available,
            witch_poison_available=self.witch_poison_available,
            winner=self.winner,
        )


def terminal_result(world: WorldState) -> tuple[str, str] | None:
    """按七人微阶段规则的固定优先级判断终局。"""

    alive_wolves = sum(is_wolf_role(world.roles[index]) for index in world.alive_indices)
    alive_good = len(world.alive_indices) - alive_wolves
    if alive_wolves == 0:
        return "good", "所有狼人死亡"
    if alive_wolves >= alive_good:
        return "wolf", "狼人达到人数条件"
    clergy = {"预言家", "女巫", "守卫", "猎人", "愚者"}
    if any(role in clergy for role in world.roles) and not any(
        world.alive[index] and world.roles[index] in clergy for index in range(len(world.roles))
    ):
        return "wolf", "神职全部死亡"
    if not any(world.alive[index] and world.roles[index] == "村民" for index in range(len(world.roles))):
        return "wolf", "村民全部死亡"
    return None
