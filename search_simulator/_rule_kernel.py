"""无策略规则内核。

内核只负责合法性、确定性动作转移、阶段结算和终局判断。行为策略与
随机抽样由调用方注入，便于与完整分支树 ``SearchSimulator`` 做差分验证。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from ._decision_state import WorldState
from ._decision_state import is_wolf_role
from ._decision_state import terminal_result
from ._speech_action import SpeechPlan


@dataclass(frozen=True, slots=True)
class RuleAction:
    """规则层接受的单个确定动作。"""

    kind: str
    actor_id: int
    target_id: int | None = None
    speech: SpeechPlan | None = None
    witch_action: str = "none"


class RuleKernel:
    """执行硬规则，不包含策略或行为概率。

    规则强制的平票随机性只消费调用方传入的显式 ``random.Random``。
    """

    def legal_actions(
        self,
        *,
        world: WorldState,
        actor_id: int,
    ) -> tuple[RuleAction, ...]:
        """返回当前阶段对指定席位的硬规则合法动作。"""

        actor = int(actor_id)
        if actor not in world.alive_indices:
            return ()
        if world.phase == "day_vote":
            return tuple(
                RuleAction(kind="vote", actor_id=actor, target_id=target)
                for target in world.alive_indices
                if target != actor
            )
        if world.phase == "night":
            role = world.roles[actor]
            if is_wolf_role(role):
                return tuple(
                    RuleAction(kind="wolf_vote", actor_id=actor, target_id=target)
                    for target in world.alive_indices
                    if not is_wolf_role(world.roles[target])
                )
            if role == "守卫":
                return tuple(
                    RuleAction(kind="guard", actor_id=actor, target_id=target)
                    for target in world.alive_indices
                    if target != actor and target != world.last_guard_target
                )
            if role == "女巫":
                actions = [RuleAction(kind="witch", actor_id=actor, witch_action="none")]
                if world.witch_save_available and world.pending_wolf_target is not None:
                    if world.pending_wolf_target != actor or world.first_night:
                        actions.append(RuleAction(kind="witch", actor_id=actor, witch_action="save"))
                if world.witch_poison_available:
                    actions.extend(
                        RuleAction(
                            kind="witch",
                            actor_id=actor,
                            target_id=target,
                            witch_action="poison",
                        )
                        for target in world.alive_indices
                        if target != actor
                    )
                return tuple(actions)
            if role == "预言家":
                return tuple(
                    RuleAction(kind="seer", actor_id=actor, target_id=target)
                    for target in world.alive_indices
                    if target != actor and target not in world.private_seer_checks.get(actor, {})
                )
        return ()

    def apply_action(self, *, world: WorldState, action: RuleAction) -> WorldState:
        """应用一个确定动作并返回同一世界对象。

        不合法动作抛出 ``ValueError``；调用方应先通过 ``legal_actions`` 或
        专用校验构造动作。
        """

        actor = int(action.actor_id)
        if actor not in world.alive_indices:
            raise ValueError("死亡席位不能执行动作")
        if action.kind == "speech":
            if action.speech is None:
                raise ValueError("speech 动作缺少 SpeechPlan")
            self.apply_speech(world=world, actor_id=actor, speech=action.speech)
            return world
        if action.kind == "vote":
            target = self._require_target(world, action.target_id, actor)
            if target == actor:
                raise ValueError("投票不能投自己")
            world.pending_votes[actor] = target
            return world
        if action.kind == "wolf_vote":
            target = self._require_target(world, action.target_id, actor)
            if is_wolf_role(world.roles[target]):
                raise ValueError("狼人不能把狼队友作为刀口")
            world.pending_wolf_votes[actor] = target
            return world
        if action.kind == "guard":
            target = self._require_target(world, action.target_id, actor)
            if target == actor or target == world.last_guard_target:
                raise ValueError("守卫不能守护自己或连续守同一目标")
            world.pending_guard_target = target
            return world
        if action.kind == "witch":
            self._apply_witch_action(world=world, action=action)
            return world
        if action.kind == "seer":
            target = self._require_target(world, action.target_id, actor)
            if world.roles[actor] != "预言家":
                raise ValueError("只有预言家可以查验")
            if target in world.private_seer_checks.get(actor, {}):
                raise ValueError("同一目标不能重复查验")
            world.pending_seer_target = target
            return world
        raise ValueError(f"未知规则动作类型: {action.kind}")

    def apply_speech(
        self,
        *,
        world: WorldState,
        actor_id: int,
        speech: SpeechPlan,
    ) -> None:
        """记录结构化发言并推进逐席位发言指针。"""

        actor = int(actor_id)
        if world.phase != "day_speech" or actor not in world.alive_indices:
            raise ValueError("当前不是该席位的白天发言阶段")
        for target in (speech.target_id, speech.claim_target):
            if target is not None and self._require_target(world, target, actor) == actor:
                raise ValueError("发言目标必须是其他存活席位")
        if (
            speech.family == "seer_claim"
            and speech.claim_target is not None
            and speech.claim_result in {"good", "wolf"}
            and world.roles[actor] == "预言家"
        ):
            known = world.private_seer_checks.get(actor, {})
            if speech.claim_target not in known:
                raise ValueError("真预言家不能伪造未查验目标")
            expected = "wolf" if known[speech.claim_target] else "good"
            if speech.claim_result != expected:
                raise ValueError("真预言家公开查验结果必须与私有查验一致")
        if speech.family == "seer_claim":
            world.public_role_claims[actor] = "预言家"
        event = (
            "speech",
            actor,
            speech.family,
            speech.claim_role,
            speech.claim_target,
            speech.claim_result,
            speech.tactic,
        )
        world.public_events.append(event)
        world.speech_index += 1
        if world.speech_index >= len(world.speech_order):
            world.phase = "day_vote"
            world.speech_index = 0
        else:
            world.actor_id = world.speech_order[world.speech_index]

    def settle_phase(self, *, world: WorldState, random_source: random.Random) -> WorldState:
        """结算完整白天或夜晚阶段，并推进到下一微阶段。"""

        if world.phase == "day_vote":
            self._settle_day(world=world, random_source=random_source)
        elif world.phase == "night":
            self._settle_night(world=world, random_source=random_source)
        else:
            raise ValueError(f"不能结算阶段: {world.phase}")
        return world

    def terminal_result(self, *, world: WorldState) -> tuple[str, str] | None:
        """返回固定终局结果，未结束时返回 ``None``。"""

        return terminal_result(world)

    @staticmethod
    def _require_target(world: WorldState, target_id: int | None, actor_id: int) -> int:
        if target_id is None:
            raise ValueError("动作缺少 target_id")
        target = int(target_id)
        if target not in world.alive_indices:
            raise ValueError("目标必须是存活席位")
        return target

    def _apply_witch_action(self, *, world: WorldState, action: RuleAction) -> None:
        actor = int(action.actor_id)
        if world.roles[actor] != "女巫":
            raise ValueError("只有女巫可以使用药剂")
        if action.witch_action == "none":
            world.pending_witch_action = "none"
            world.pending_witch_target = None
            return
        if action.witch_action == "save":
            if not world.witch_save_available or world.pending_wolf_target is None:
                raise ValueError("解药不可用或当前没有狼刀")
            if world.pending_wolf_target == actor and not world.first_night:
                raise ValueError("女巫只有第一夜可以自救")
            world.pending_witch_action = "save"
            world.pending_witch_target = world.pending_wolf_target
            return
        if action.witch_action == "poison":
            target = self._require_target(world, action.target_id, actor)
            if not world.witch_poison_available or target == actor:
                raise ValueError("毒药不可用或不能自毒")
            world.pending_witch_action = "poison"
            world.pending_witch_target = target
            return
        raise ValueError(f"未知女巫动作: {action.witch_action}")

    @staticmethod
    def _kill(world: WorldState, target: int | None) -> None:
        if target is not None and 0 <= int(target) < len(world.alive):
            world.alive[int(target)] = False

    def _settle_day(self, *, world: WorldState, random_source: random.Random) -> None:
        eligible = tuple(world.alive_indices)
        if not eligible:
            world.winner = "good"
            world.terminal_reason = "无存活玩家"
            return
        counts = dict.fromkeys(eligible, 0)
        for voter, target in world.pending_votes.items():
            if voter in eligible and target in counts and voter != target:
                counts[target] += 1
        highest = max(counts.values(), default=0)
        top = [target for target, votes in counts.items() if votes == highest]
        expelled = random_source.choice(top) if top else None
        self._kill(world, expelled)
        world.pending_votes.clear()
        result = terminal_result(world)
        if result is not None:
            world.winner, world.terminal_reason = result
            return
        world.phase = "night"
        world.night_count += 1
        world.first_night = False
        world.pending_wolf_votes.clear()
        world.pending_guard_target = None
        world.pending_witch_action = "none"
        world.pending_witch_target = None
        world.pending_seer_target = None

    def _settle_night(self, *, world: WorldState, random_source: random.Random) -> None:
        eligible_targets = tuple(index for index in world.alive_indices if not is_wolf_role(world.roles[index]))
        counts = dict.fromkeys(eligible_targets, 0)
        for wolf, target in world.pending_wolf_votes.items():
            if wolf in world.alive_wolf_indices and target in counts:
                counts[target] += 1
        highest = max(counts.values(), default=0)
        top = [target for target, votes in counts.items() if votes == highest and votes > 0]
        wolf_target = world.pending_wolf_target
        if wolf_target not in counts:
            wolf_target = random_source.choice(top) if top else None
        world.pending_wolf_target = wolf_target
        guard_target = world.pending_guard_target
        world.last_guard_target = guard_target
        saved = world.pending_witch_action == "save" and world.pending_witch_target == wolf_target
        if world.pending_witch_action == "save":
            world.witch_save_available = False
        if world.pending_witch_action == "poison":
            world.witch_poison_available = False
        if wolf_target is not None and not saved and guard_target != wolf_target:
            self._kill(world, wolf_target)
        if world.pending_witch_action == "poison":
            self._kill(world, world.pending_witch_target)
        if world.pending_seer_target is not None:
            seers = [index for index in world.alive_indices if world.roles[index] == "预言家"]
            if seers:
                seer = seers[0]
                world.private_seer_checks.setdefault(seer, {})[world.pending_seer_target] = is_wolf_role(
                    world.roles[world.pending_seer_target]
                )
        result = terminal_result(world)
        if result is not None:
            world.winner, world.terminal_reason = result
            return
        world.phase = "day_speech"
        world.day_count += 1
        world.speech_order = tuple(world.alive_indices)
        world.speech_index = 0
        world.actor_id = world.speech_order[0] if world.speech_order else 0
        world.pending_wolf_votes.clear()
        world.pending_wolf_target = None
        world.pending_guard_target = None
        world.pending_witch_action = "none"
        world.pending_witch_target = None
        world.pending_seer_target = None


class TreeSearchCompatibilityAdapter:
    """为完整分支树迭代保留完整分支入口的兼容适配器。

    整阶段展开仍由 ``SearchSimulator`` 负责；决策矩阵 worker 不实例化本类，
    从而不会把旧搜索器、GUI 或数据库导入计算进程。重构阶段可用该适配器
    对比旧 ``StateTransition`` 与共享规则内核的结果。
    """

    def __init__(self, simulator: Any) -> None:
        self.simulator = simulator

    def expand(self, state: Any) -> list[Any]:
        """调用完整分支树展开，保留原动作键与 multiplicity。"""

        expand_state = getattr(self.simulator, "expand_state", None)
        if not callable(expand_state):
            raise TypeError("传入对象不是兼容的完整分支树 SearchSimulator")
        return list(expand_state(state))
