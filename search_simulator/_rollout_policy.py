"""不依赖 LLM 的功利等级策略。"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from ._decision_state import CanonicalGameConfig
from ._decision_state import WorldState
from ._role_view import build_role_view
from ._role_view import policy_wolf_probabilities
from ._rule_kernel import RuleAction
from ._speech_action import SpeechPlan
from ._speech_action import enumerate_speech_actions

POLICY_SCORES = (1.0, 0.75, 0.5, 0.25, 0.0)


def _quantize(value: float) -> float:
    """把连续启发式映射到五档规范分数。"""

    number = max(0.0, min(1.0, float(value)))
    if number >= 0.875:
        return 1.0
    if number >= 0.625:
        return 0.75
    if number >= 0.375:
        return 0.5
    if number >= 0.125:
        return 0.25
    return 0.0


@dataclass(frozen=True, slots=True)
class UtilityRankedPolicy:
    """具有稳定规范标识、可复现且不调用语言模型的行动策略。"""

    temperature: float = 0.25
    policy_spec: str = "utility-ranked-rollout-policy"

    def __post_init__(self) -> None:
        if float(self.temperature) <= 0.0:
            raise ValueError("policy temperature 必须大于 0")

    def distribution(
        self,
        *,
        world: WorldState,
        actor_id: int,
        config: CanonicalGameConfig,
        credibility: float = 0.5,
    ) -> tuple[tuple[RuleAction, float], ...]:
        """返回硬规则过滤后的动作与五档分数。"""

        actor = int(actor_id)
        if actor not in world.alive_indices:
            return ()
        role_view = build_role_view(world.roles, world, actor_id=actor)
        state = world.to_public_state()
        probabilities = policy_wolf_probabilities(
            config,
            role_view,
            state,
            credibility=float(credibility),
        )
        if world.phase == "day_speech":
            return tuple(
                (
                    RuleAction(kind="speech", actor_id=actor, speech=plan),
                    self._speech_score(plan=plan, probabilities=probabilities, actor_id=actor),
                )
                for plan in enumerate_speech_actions(state, role_view)
            )
        if world.phase == "day_vote":
            actions: list[tuple[RuleAction, float]] = []
            target_ids = [target for target in world.alive_indices if target != actor]
            for target in target_ids:
                probability = probabilities.get(int(target), 0.0)
                if world.roles[actor] in {"狼人", "白狼王"}:
                    score = _quantize(1.0 - probability)
                else:
                    score = _quantize(probability)
                actions.append((RuleAction(kind="vote", actor_id=actor, target_id=target), score))
            return tuple(actions)
        if world.phase == "night":
            return self._night_distribution(
                world=world,
                actor_id=actor,
                config=config,
                probabilities=probabilities,
            )
        return ()

    def sample(
        self,
        distribution: tuple[tuple[RuleAction, float], ...],
        rng: random.Random,
    ) -> RuleAction | None:
        """按固定 softmax 温度抽取一个动作。"""

        if not distribution:
            return None
        maximum = max(float(score) for _action, score in distribution)
        weights = [math.exp((float(score) - maximum) / float(self.temperature)) for _action, score in distribution]
        total = sum(weights)
        threshold = rng.random() * total
        cumulative = 0.0
        for (action, _score), weight in zip(distribution, weights, strict=True):
            cumulative += weight
            if threshold <= cumulative:
                return action
        return distribution[-1][0]

    @staticmethod
    def _speech_score(
        *,
        plan: SpeechPlan,
        probabilities: dict[int, float],
        actor_id: int,
    ) -> float:
        if plan.family == "silence":
            return 0.25
        if plan.family == "baseline":
            return 0.5
        if plan.family in {"accusation", "vote_intent"} and plan.target_id is not None:
            suspicion = probabilities.get(int(plan.target_id), 0.0)
            return _quantize(suspicion)
        if plan.family == "support" and plan.target_id is not None:
            return _quantize(1.0 - probabilities.get(int(plan.target_id), 0.0))
        if plan.family == "seer_claim":
            if plan.claim_target is None:
                return 0.5
            suspicion = probabilities.get(int(plan.claim_target), 0.0)
            if plan.claim_result == "wolf":
                return _quantize(suspicion)
            if plan.claim_result == "good":
                return _quantize(1.0 - suspicion)
            return 0.5
        return _quantize(0.5)

    def _night_distribution(
        self,
        *,
        world: WorldState,
        actor_id: int,
        config: CanonicalGameConfig,
        probabilities: dict[int, float],
    ) -> tuple[tuple[RuleAction, float], ...]:
        actor = int(actor_id)
        role = world.roles[actor]
        if role in {"狼人", "白狼王"}:
            actions: list[tuple[RuleAction, float]] = []
            for target in world.alive_indices:
                if is_wolf(world.roles[target]):
                    continue
                suspicion = probabilities.get(int(target), 0.0)
                actions.append(
                    (
                        RuleAction(kind="wolf_vote", actor_id=actor, target_id=target),
                        _quantize(1.0 - suspicion),
                    )
                )
            return tuple(actions)
        if role == "守卫":
            return tuple(
                (
                    RuleAction(kind="guard", actor_id=actor, target_id=target),
                    _quantize(1.0 - probabilities.get(int(target), 0.0)),
                )
                for target in world.alive_indices
                if target != actor and target != world.last_guard_target
            )
        if role == "女巫":
            actions: list[tuple[RuleAction, float]] = [
                (RuleAction(kind="witch", actor_id=actor, witch_action="none"), 0.5)
            ]
            if world.witch_save_available and world.pending_wolf_target is not None:
                if world.pending_wolf_target != actor or world.first_night:
                    actions.append((RuleAction(kind="witch", actor_id=actor, witch_action="save"), 0.75))
            if world.witch_poison_available:
                actions.extend(
                    (
                        RuleAction(
                            kind="witch",
                            actor_id=actor,
                            target_id=target,
                            witch_action="poison",
                        ),
                        _quantize(probabilities.get(int(target), 0.0)),
                    )
                    for target in world.alive_indices
                    if target != actor
                )
            return tuple(actions)
        if role == "预言家":
            candidates = [
                target
                for target in world.alive_indices
                if target != actor and target not in world.private_seer_checks.get(actor, {})
            ]
            return tuple(
                (
                    RuleAction(kind="seer", actor_id=actor, target_id=target),
                    _quantize(1.0 - abs(0.5 - probabilities.get(int(target), 0.0)) * 2.0),
                )
                for target in candidates
            )
        return ()


def is_wolf(role: str) -> bool:
    """避免功利等级策略依赖规则层的策略实现。"""

    return str(role) in {"狼人", "白狼王"}
