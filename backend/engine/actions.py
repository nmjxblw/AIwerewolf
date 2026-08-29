from __future__ import annotations

from dataclasses import dataclass

from backend.engine.models import ActionType
from backend.engine.models import Alignment
from backend.engine.models import Decision
from backend.engine.models import GameState
from backend.engine.models import Phase
from backend.engine.models import Role


@dataclass(frozen=True)
class ActionRule:
    action_type: ActionType
    actor_roles: tuple[Role, ...]
    requires_target: bool = True
    alive_actor_required: bool = True


ACTION_RULES: dict[ActionType, ActionRule] = {
    ActionType.TALK: ActionRule(ActionType.TALK, tuple(Role), requires_target=False),
    ActionType.VOTE: ActionRule(ActionType.VOTE, tuple(Role)),
    ActionType.ATTACK: ActionRule(ActionType.ATTACK, (Role.WEREWOLF, Role.WHITE_WOLF_KING)),
    ActionType.BOOM: ActionRule(ActionType.BOOM, (Role.WHITE_WOLF_KING,), alive_actor_required=True),
    ActionType.DIVINE: ActionRule(ActionType.DIVINE, (Role.SEER,)),
    ActionType.GUARD: ActionRule(ActionType.GUARD, (Role.GUARD,)),
    ActionType.WITCH_SAVE: ActionRule(ActionType.WITCH_SAVE, (Role.WITCH,)),
    ActionType.WITCH_POISON: ActionRule(ActionType.WITCH_POISON, (Role.WITCH,)),
    ActionType.SHOOT: ActionRule(ActionType.SHOOT, (Role.HUNTER,), alive_actor_required=False),
    ActionType.SKIP: ActionRule(ActionType.SKIP, tuple(Role), requires_target=False, alive_actor_required=False),
}


class ActionValidator:
    def validate(self, state: GameState, decision: Decision) -> bool:
        rule = ACTION_RULES[decision.action_type]
        actor = state.player(decision.actor_id)
        if rule.alive_actor_required and not actor.alive:
            return False
        if actor.role not in rule.actor_roles:
            return False
        if decision.action_type == ActionType.VOTE and actor.role == Role.IDIOT and state.abilities.idiot_revealed:
            return False
        if (
            decision.action_type == ActionType.VOTE
            and state.phase == Phase.DAY_VOTE
            and actor.role == Role.SEER
            and any(
                event.payload.get("kind") == "seer_result"
                and actor.id in event.visible_to
                and event.payload.get("target_id") == decision.target_id
                and not bool(event.payload.get("is_wolf"))
                for event in state.events
            )
        ):
            return False
        if (
            decision.action_type == ActionType.DIVINE
            and actor.role == Role.SEER
            and decision.target_id
            and any(
                event.payload.get("kind") == "seer_result"
                and actor.id in event.visible_to
                and event.payload.get("target_id") == decision.target_id
                for event in state.events
            )
        ):
            return False
        # 廉价磋商板子：空刀（狼人集体放弃袭击 → 平安夜）为合法选择
        empty_knife_ok = bool(state.board_options.get("wolf_empty_knife")) and decision.action_type == ActionType.ATTACK
        if rule.requires_target:
            if decision.target_id is None:
                if not empty_knife_ok:
                    return False
                return True
            try:
                target = state.player(decision.target_id)
            except KeyError:
                return False
            if not target.alive:
                return False
            # 廉价磋商板子：允许狼人自刀（骗女巫解药）；仍禁止刀狼队友
            self_knife_ok = (
                bool(state.board_options.get("wolf_self_knife")) and decision.action_type == ActionType.ATTACK
            )
            if target.id == actor.id and decision.action_type in {
                ActionType.VOTE,
                ActionType.DIVINE,
                ActionType.GUARD,
                ActionType.WITCH_POISON,
                ActionType.SHOOT,
            }:
                return False
            if (
                target.id == actor.id
                and decision.action_type == ActionType.WITCH_SAVE
                and state.day != 1
            ):
                return False
            if (
                decision.action_type == ActionType.GUARD
                and target.id == state.night_actions.last_guard_target_id
            ):
                return False
            if target.id == actor.id and decision.action_type == ActionType.ATTACK and not self_knife_ok:
                return False
            # 狼人不能攻击狼队友（CLAUDE.md 关键规则 #1）
            if decision.action_type == ActionType.ATTACK:
                if target.alignment == Alignment.WOLF and target.id != actor.id:
                    return False
        return True
