"""前向终局 Monte Carlo 计算 worker。

该模块刻意只依赖标准库和纯规则/策略类型，禁止导入 SQLAlchemy、
Pygame、greenlet 或任何 LLM SDK。跨进程只返回充分统计量，不传输轨迹。
"""

from __future__ import annotations

import hashlib
import random
from collections import Counter
from typing import Any

from ._decision_state import CanonicalGameConfig
from ._decision_state import DecisionState
from ._decision_state import WorldState
from ._decision_state import is_wolf_role
from ._role_view import RoleView
from ._role_view import posterior_layouts
from ._role_view import sample_layout
from ._rollout_policy import UtilityRankedPolicy
from ._rule_kernel import RuleAction
from ._rule_kernel import RuleKernel
from ._speech_action import SpeechPlan

SCENARIOS = (
    "backlash",
    "target_transfer",
    "claim_accepted",
    "claim_contested",
    "ignored",
    "other",
)


def stable_seed(*parts: Any) -> int:
    """从规范字段派生与进程、线程和遍历顺序无关的 64 位种子。"""

    payload = "|".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big")


def _role_view_from_dict(data: dict[str, Any]) -> RoleView:
    return RoleView(
        actor_id=int(data["actor_id"]),
        actor_role=str(data["actor_role"]),
        known_roles=tuple((int(item[0]), str(item[1])) for item in data.get("known_roles", ())),
        known_camps=tuple((int(item[0]), str(item[1])) for item in data.get("known_camps", ())),
        seer_checks=tuple((int(item[0]), int(item[1]), bool(item[2])) for item in data.get("seer_checks", ())),
        view_spec=str(data.get("view_spec", "role-view-hard-knowledge")),
    )


def _speech_from_dict(data: dict[str, Any]) -> SpeechPlan:
    return SpeechPlan(
        family=str(data["family"]),
        claim_role=data.get("claim_role"),
        claim_target=(None if data.get("claim_target") is None else int(data["claim_target"])),
        claim_result=data.get("claim_result"),
        target_id=(None if data.get("target_id") is None else int(data["target_id"])),
        intensity=float(data.get("intensity", 0.5)),
        tactic=data.get("tactic"),
        structure_spec=str(data.get("structure_spec", "structured-cheap-talk-actions")),
    )


def _config_from_dict(data: dict[str, Any]) -> CanonicalGameConfig:
    return CanonicalGameConfig(
        number_of_players=int(data["number_of_players"]),
        number_of_wolves=int(data["number_of_wolves"]),
        roles=tuple(str(role) for role in data["roles"]),
        max_days=int(data.get("max_days", 8)),
        rules_spec=str(data.get("rules_spec", "seven-player-microphase-rules")),
    )


def _resolve_wolf_target(world: WorldState, rng: random.Random) -> None:
    """在女巫行动前确定狼队共同刀口，平票使用显式均匀随机。"""

    targets = {target for target in world.alive_indices if not is_wolf_role(world.roles[target])}
    counts = Counter(
        target
        for wolf, target in world.pending_wolf_votes.items()
        if wolf in world.alive_wolf_indices and target in targets
    )
    highest = max(counts.values(), default=0)
    top = [target for target in sorted(targets) if counts.get(target, 0) == highest and highest > 0]
    world.pending_wolf_target = rng.choice(top) if top else None


def _apply_future_speech(
    *,
    world: WorldState,
    policy: UtilityRankedPolicy,
    config: CanonicalGameConfig,
    rng: random.Random,
    kernel: RuleKernel,
    credibility: float,
) -> None:
    """顺序模拟当前日剩余玩家的结构化发言。"""

    while world.phase == "day_speech" and world.speech_index < len(world.speech_order):
        actor = world.speech_order[world.speech_index]
        distribution = policy.distribution(
            world=world,
            actor_id=actor,
            config=config,
            credibility=credibility,
        )
        action = policy.sample(distribution, rng)
        if action is None:
            # 没有可用结构动作时仍推进为显式沉默，避免卡在阶段。
            action = RuleAction(
                kind="speech",
                actor_id=actor,
                speech=SpeechPlan(family="silence", intensity=0.0),
            )
        kernel.apply_action(world=world, action=action)


def _apply_votes_and_settle(
    *,
    world: WorldState,
    policy: UtilityRankedPolicy,
    config: CanonicalGameConfig,
    rng: random.Random,
    kernel: RuleKernel,
    credibility: float,
) -> None:
    """顺序完成白天投票并按均匀平票规则结算。"""

    for actor in world.alive_indices:
        distribution = policy.distribution(
            world=world,
            actor_id=actor,
            config=config,
            credibility=credibility,
        )
        action = policy.sample(distribution, rng)
        if action is not None:
            kernel.apply_action(world=world, action=action)
    kernel.settle_phase(world=world, random_source=rng)


def _apply_night_and_settle(
    *,
    world: WorldState,
    policy: UtilityRankedPolicy,
    config: CanonicalGameConfig,
    rng: random.Random,
    kernel: RuleKernel,
    credibility: float,
) -> None:
    """按狼、守卫、女巫、预言家顺序模拟夜晚。"""

    for actor in world.alive_wolf_indices:
        distribution = policy.distribution(
            world=world,
            actor_id=actor,
            config=config,
            credibility=credibility,
        )
        action = policy.sample(distribution, rng)
        if action is not None:
            kernel.apply_action(world=world, action=action)
    _resolve_wolf_target(world, rng)
    for actor in world.alive_indices:
        if world.roles[actor] not in {"守卫", "女巫", "预言家"}:
            continue
        distribution = policy.distribution(
            world=world,
            actor_id=actor,
            config=config,
            credibility=credibility,
        )
        action = policy.sample(distribution, rng)
        if action is not None:
            kernel.apply_action(world=world, action=action)
    kernel.settle_phase(world=world, random_source=rng)


def _simulate_terminal(
    *,
    world: WorldState,
    action: SpeechPlan,
    actor_id: int,
    config: CanonicalGameConfig,
    credibility: float,
    seed: int,
) -> tuple[str, str]:
    """模拟一个候选动作到终局并返回阵营与解释情景。"""

    kernel = RuleKernel()
    policy = UtilityRankedPolicy()
    action_rng = random.Random(seed)
    kernel.apply_action(
        world=world,
        action=RuleAction(kind="speech", actor_id=int(actor_id), speech=action),
    )
    while world.winner is None:
        if world.phase == "day_speech":
            _apply_future_speech(
                world=world,
                policy=policy,
                config=config,
                rng=action_rng,
                kernel=kernel,
                credibility=credibility,
            )
        if world.winner is not None:
            break
        if world.phase == "day_vote":
            _apply_votes_and_settle(
                world=world,
                policy=policy,
                config=config,
                rng=action_rng,
                kernel=kernel,
                credibility=credibility,
            )
        if world.winner is not None:
            break
        if world.phase == "night":
            _apply_night_and_settle(
                world=world,
                policy=policy,
                config=config,
                rng=action_rng,
                kernel=kernel,
                credibility=credibility,
            )
        if world.day_count >= config.max_days and world.winner is None:
            world.winner = "wolf"
            world.terminal_reason = "max_days_reached"
    winner = str(world.winner or "wolf")
    actor_is_wolf = is_wolf_role(world.roles[int(actor_id)])
    backlash = not world.alive[int(actor_id)]
    target = action.target_id if action.target_id is not None else action.claim_target
    if backlash:
        scenario = "backlash"
    elif target is not None and 0 <= int(target) < len(world.alive) and not world.alive[int(target)]:
        scenario = "target_transfer"
    elif action.family == "seer_claim":
        truthful = (
            world.roles[int(actor_id)] == "预言家"
            and action.claim_target is not None
            and action.claim_result == ("wolf" if is_wolf_role(world.roles[int(action.claim_target)]) else "good")
        )
        scenario = "claim_accepted" if truthful else "claim_contested"
    elif action.family in {"baseline", "silence"}:
        scenario = "ignored"
    else:
        scenario = "other"
    _ = actor_is_wolf, credibility
    return winner, scenario


def run_matrix_batch(payload: dict[str, Any]) -> dict[str, Any]:
    """计算一个可信度和样本区间的全部候选动作。"""

    config = _config_from_dict(payload["config"])
    state = DecisionState.from_dict(payload["decision_state"])
    role_view = _role_view_from_dict(payload["role_view"])
    actions = tuple(_speech_from_dict(item) for item in payload["candidate_actions"])
    credibility = float(payload["credibility"])
    request_digest = str(payload["request_digest"])
    sample_start = int(payload["sample_start"])
    sample_end = int(payload["sample_end"])
    posterior = posterior_layouts(config, role_view, state, credibility=credibility)
    rows: dict[str, dict[str, Any]] = {
        action.key(): {
            "action_key": action.key(),
            "sample_count": 0,
            "reward_sum": 0.0,
            "reward_sum_sq": 0.0,
            "delta_sum": 0.0,
            "delta_sum_sq": 0.0,
            "scenario_counts": dict.fromkeys(SCENARIOS, 0),
        }
        for action in actions
    }
    if not actions or actions[0].family != "baseline":
        raise ValueError("候选动作必须以 baseline 作为配对基准")
    baseline = actions[0]
    for sample_index in range(sample_start, sample_end):
        sample_seed = stable_seed(request_digest, credibility, sample_index, "world")
        world_rng = random.Random(sample_seed)
        roles = sample_layout(posterior, world_rng)
        private_checks = {
            role_view.actor_id: {
                target: is_wolf for observer, target, is_wolf in role_view.seer_checks if observer == role_view.actor_id
            }
        }
        base_world = WorldState.from_state(roles, state, private_seer_checks=private_checks)
        baseline_winner, _baseline_scenario = _simulate_terminal(
            world=base_world.clone(),
            action=baseline,
            actor_id=role_view.actor_id,
            config=config,
            credibility=credibility,
            seed=stable_seed(request_digest, credibility, sample_index, "trajectory"),
        )
        baseline_reward = (
            1.0 if baseline_winner == ("wolf" if is_wolf_role(roles[role_view.actor_id]) else "good") else -1.0
        )
        for action in actions:
            winner, scenario = _simulate_terminal(
                world=base_world.clone(),
                action=action,
                actor_id=role_view.actor_id,
                config=config,
                credibility=credibility,
                seed=stable_seed(request_digest, credibility, sample_index, "trajectory"),
            )
            actor_camp = "wolf" if is_wolf_role(roles[role_view.actor_id]) else "good"
            reward = 1.0 if winner == actor_camp else -1.0
            delta = reward - baseline_reward
            row = rows[action.key()]
            row["sample_count"] += 1
            row["reward_sum"] += reward
            row["reward_sum_sq"] += reward * reward
            row["delta_sum"] += delta
            row["delta_sum_sq"] += delta * delta
            row["scenario_counts"][scenario] += 1
    return {
        "batch_id": f"{credibility:.6f}:{sample_start}:{sample_end}",
        "credibility": credibility,
        "sample_start": sample_start,
        "sample_end": sample_end,
        "rows": list(rows.values()),
    }


def worker_loop(task_queue: Any, result_queue: Any) -> None:
    """multiprocessing worker 主循环；异常以结构化消息返回父进程。"""

    while True:
        payload = task_queue.get()
        if payload is None:
            return
        try:
            result_queue.put({"kind": "batch", "payload": run_matrix_batch(payload)})
        except BaseException as exc:  # noqa: BLE001 - 跨进程必须回传完整错误
            result_queue.put(
                {
                    "kind": "error",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
