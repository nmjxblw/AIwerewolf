"""精确信念 Cheap-talk 决策矩阵的结构化发言动作。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ._decision_state import DecisionState
from ._decision_state import WorldState
from ._role_view import RoleView

ACTION_FAMILIES = (
    "baseline",
    "accusation",
    "support",
    "vote_intent",
    "seer_claim",
    "silence",
)


@dataclass(frozen=True, slots=True)
class SpeechPlan:
    """与自然语言无关的二级结构化发言动作。"""

    family: str
    claim_role: str | None = None
    claim_target: int | None = None
    claim_result: str | None = None
    target_id: int | None = None
    intensity: float = 0.5
    tactic: str | None = None
    structure_spec: str = "structured-cheap-talk-actions"

    def __post_init__(self) -> None:
        if self.family not in ACTION_FAMILIES:
            raise ValueError(f"未知 cheap-talk 动作族: {self.family}")
        if not 0.0 <= float(self.intensity) <= 1.0:
            raise ValueError("intensity 必须在 [0,1] 范围内")
        if self.claim_result is not None and self.claim_result not in {"good", "wolf"}:
            raise ValueError("claim_result 必须为 good、wolf 或 None")

    def canonical(self) -> tuple[Any, ...]:
        """返回与展示文案无关的规范动作键。"""

        return (
            str(self.family),
            self.claim_role,
            None if self.claim_target is None else int(self.claim_target),
            self.claim_result,
            None if self.target_id is None else int(self.target_id),
            round(float(self.intensity), 6),
            self.tactic,
            str(self.structure_spec),
        )

    def key(self) -> str:
        """返回稳定规范动作键。"""

        # 该键位于 rollout 热路径。Windows CPython 3.13 的 tier-2 优化在
        # 大量重复调用 json.dumps 时偶发错误，因此用只含标量的 repr 作为
        # 等价规范键；字段顺序由 canonical() 固定，中文标签仍不参与。
        return repr(self.canonical())

    def to_dict(self) -> dict[str, Any]:
        """序列化动作摘要，不包含自然语言文本。"""

        return {
            "family": self.family,
            "claim_role": self.claim_role,
            "claim_target": self.claim_target,
            "claim_result": self.claim_result,
            "target_id": self.target_id,
            "intensity": self.intensity,
            "tactic": self.tactic,
            "structure_spec": self.structure_spec,
        }


def _alive_targets(state: DecisionState | WorldState, actor_id: int) -> tuple[int, ...]:
    """返回当前行动者以外的存活席位。"""

    alive = (
        state.alive_indices
        if isinstance(state, WorldState)
        else tuple(index for index, value in enumerate(state.alive) if value)
    )
    return tuple(index for index in alive if index != int(actor_id))


def enumerate_speech_actions(
    state: DecisionState,
    role_view: RoleView,
    *,
    include_baseline: bool = True,
) -> tuple[SpeechPlan, ...]:
    """生成当前席位全部二级 cheap-talk 候选。

    目标不按座位号裁剪；同族动作只在输出层汇总，Monte Carlo 始终逐行动
    计算。完整跳预言家必须带目标与结果，弱声明使用空目标的独立动作表示。
    """

    actor = int(role_view.actor_id)
    targets = _alive_targets(state, actor)
    actions: list[SpeechPlan] = []
    if include_baseline:
        actions.append(SpeechPlan(family="baseline", intensity=0.0))
    actions.append(SpeechPlan(family="silence", intensity=0.0))
    actions.extend(SpeechPlan(family="accusation", target_id=target, intensity=0.7) for target in targets)
    actions.extend(SpeechPlan(family="support", target_id=target, intensity=0.5) for target in targets)
    actions.extend(SpeechPlan(family="vote_intent", target_id=target, intensity=0.7) for target in targets)
    # 弱身份声明不带查验，和完整跳预言家严格分离。
    actions.append(
        SpeechPlan(
            family="seer_claim",
            claim_role="预言家",
            intensity=0.6,
            tactic=("villager_decoy" if role_view.actor_role == "村民" else None),
        )
    )
    known_checks = {
        int(target): bool(is_wolf) for observer, target, is_wolf in role_view.seer_checks if int(observer) == actor
    }
    if role_view.actor_role == "预言家" and known_checks:
        complete_results = [
            (target, "wolf" if is_wolf else "good")
            for target, is_wolf in sorted(known_checks.items())
            if target in targets
        ]
    elif role_view.actor_role == "预言家":
        # 真预言家没有私有查验时不能伪造完整查验结果；只有弱声明仍可选。
        complete_results = []
    else:
        complete_results = [(target, result) for target in targets for result in ("good", "wolf")]
    for target, result in complete_results:
        actions.append(
            SpeechPlan(
                family="seer_claim",
                claim_role="预言家",
                claim_target=target,
                claim_result=result,
                intensity=0.8,
                tactic=(
                    None
                    if role_view.actor_role == "预言家"
                    else ("villager_decoy" if role_view.actor_role == "村民" else "wolf_jump")
                ),
            )
        )
    # 依据规范键去重；排序保持生成顺序稳定。
    unique: dict[str, SpeechPlan] = {}
    for action in actions:
        unique.setdefault(action.key(), action)
    return tuple(unique.values())
