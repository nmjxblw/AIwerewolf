from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Iterator

from ._game_state import GameState
from ._i18n import t
from ._player import Player
from ._positions import PositionLayout
from ._positions import build_role_roster
from ._positions import players_for_layout
from ._positions import position_signature
from ._strategy import DEFAULT_TACTICS
from ._strategy import DayTacticProfile
from ._strategy import enumerate_day_tactic_profiles
from ._strategy import enumerate_night_tactic_profiles

logger = logging.getLogger(__name__)


@dataclass
class StateTransition:
    """一条完整保留的决策分支边。"""

    action_key: tuple[Any, ...]
    state: GameState
    multiplicity: int = 1


class SearchSimulator:
    """站位感知、BFS/DFS 全分支迭代的狼人杀模拟器。"""

    def __init__(self, **kwargs: Any) -> None:
        self._state_index_lock = threading.Lock()
        self._next_state_id = 0
        self.state_parent_index: dict[int, int | None] = {}
        self.state_action_index: dict[int, str] = {}
        self.state_players_snapshot: dict[int, list[str]] = {}
        self.state_depth_index: dict[int, int] = {}
        self._vote_outcome_cache: dict[tuple[Any, ...], dict[int, int]] = {}
        self.position_results: list[dict[str, Any]] = []
        self.processed_states = 0
        self.processed_positions = 0
        self.start_time = 0.0
        self.stop_reason = t("stop.sim_done")
        self.run_id = ""
        self.last_result: dict[str, Any] | None = None
        self.signature_cache = None
        self.load_config(**kwargs)

    def load_config(self, **kwargs: Any) -> None:
        """加载树遍历、站位、多进程和持久化配置。"""

        self.number_of_players = int(kwargs.get("number_of_players", 7))
        self.number_of_wolves = int(kwargs.get("number_of_wolves", 2))
        self.include_seer = bool(kwargs.get("include_seer", True))
        self.include_witch = bool(kwargs.get("include_witch", True))
        self.include_guard = bool(kwargs.get("include_guard", True))
        self.include_hunter = bool(kwargs.get("include_hunter", False))
        self.include_idiot = bool(kwargs.get("include_idiot", False))
        self.include_white_werewolf_king = bool(
            kwargs.get("include_white_werewolf_king", False)
        )
        self.roster = build_role_roster(
            number_of_players=self.number_of_players,
            number_of_wolves=self.number_of_wolves,
            include_seer=self.include_seer,
            include_witch=self.include_witch,
            include_guard=self.include_guard,
            include_hunter=self.include_hunter,
            include_idiot=self.include_idiot,
            include_white_werewolf_king=self.include_white_werewolf_king,
        )
        self.smart_vote = bool(kwargs.get("smart_vote", True))
        tactics_value = kwargs.get("tactics")
        if tactics_value is None:
            self.tactics = DEFAULT_TACTICS if self.smart_vote else frozenset()
        elif isinstance(tactics_value, str):
            self.tactics = frozenset(
                token.strip() for token in tactics_value.split(",") if token.strip()
            )
        else:
            self.tactics = frozenset(str(token) for token in tactics_value)
        if not self.smart_vote:
            self.tactics = frozenset()
        self.search_mode = str(kwargs.get("search_mode", "dfs")).lower()
        if self.search_mode not in {"bfs", "dfs"}:
            raise ValueError("search_mode 必须是 bfs 或 dfs")
        self.lambda_risk = max(0.0, min(1.0, float(kwargs.get("lambda_risk", 0.5))))
        default_workers = max(1, min(4, (os.cpu_count() or 2) - 1))
        self.parallel_workers = max(
            1, int(kwargs.get("parallel_workers", default_workers))
        )
        self.all_positions = bool(kwargs.get("all_positions", True))
        self.results_output_path = Path(
            kwargs.get("results_output_path", "tree_results.json")
        )
        self.signature_cache_db_path = Path(
            kwargs.get("signature_cache_db_path", "search_simulator_cache.sqlite3")
        )
        self.signature_lru_capacity = max(
            1, int(kwargs.get("signature_lru_capacity", 150_000))
        )
        self.signature_commit_interval = max(
            1, int(kwargs.get("signature_commit_interval", 2_000))
        )
        self.persistence_enabled = bool(kwargs.get("persistence_enabled", True))
        callback = kwargs.get("iteration_callback")
        self.iteration_callback: Callable[[dict[str, Any]], None] | None = (
            callback if callable(callback) else None
        )
        self.progress_queue = kwargs.get("progress_queue")
        self.result_queue = kwargs.get("result_queue")
        self.resume_event = kwargs.get("resume_event")

        self.initial_state = GameState(
            players=players_for_layout(self.roster),
            phase="night",
            position_signature=position_signature(self.roster),
        )
        self._assign_state_identity(
            self.initial_state,
            parent_state_id=None,
            action_label=t("action.root"),
        )

    def _assign_state_identity(
        self,
        state: GameState,
        *,
        parent_state_id: int | None,
        action_label: str,
    ) -> None:
        with self._state_index_lock:
            state.state_id = self._next_state_id
            state.parent_state_id = parent_state_id
            state.depth = (
                0
                if parent_state_id is None
                else self.state_depth_index.get(parent_state_id, -1) + 1
            )
            state.action_label = action_label
            state.players_snapshot = [
                f"{index + 1}:{player.role}:{'alive' if player.is_alive else 'dead'}"
                for index, player in enumerate(state.players)
            ]
            self.state_parent_index[state.state_id] = parent_state_id
            self.state_action_index[state.state_id] = action_label
            self.state_players_snapshot[state.state_id] = list(state.players_snapshot)
            self.state_depth_index[state.state_id] = state.depth
            self._next_state_id += 1

    @staticmethod
    def _is_wolf_role(role: str) -> bool:
        return role in {"狼人", "白狼王"}

    def _alive_indices(
        self,
        state: GameState,
        *,
        exclude: set[int] | None = None,
        role: str | None = None,
    ) -> list[int]:
        excluded = exclude or set()
        return [
            index
            for index, player in enumerate(state.players)
            if player.is_alive
            and index not in excluded
            and (role is None or player.role == role)
        ]

    @staticmethod
    def _consume_skill(player: Player, skill_name: str) -> None:
        remaining = player.skills.get(skill_name)
        if remaining is not None and remaining > 0:
            player.skills[skill_name] = remaining - 1

    def _state_signature(self, state: GameState) -> str:
        """签名显式纳入站位、昼夜、技能与后续战术目标。"""

        return self._state_signature_from_key(
            state.position_signature,
            self._state_key(state),
        )

    @staticmethod
    def _state_signature_from_key(
        position_signature_value: str,
        key: tuple[Any, ...],
    ) -> str:
        """不物化 repr/UTF-8 缓冲，对规范键执行稳定的双 64 位流式哈希。"""

        mask = (1 << 64) - 1
        hash_a = 14695981039346656037
        hash_b = 7809847782465536322

        def mix(code: int) -> None:
            nonlocal hash_a, hash_b
            normalized = int(code) & mask
            hash_a = ((hash_a ^ normalized) * 1099511628211) & mask
            hash_b = (
                (hash_b ^ ((normalized + 0x9E3779B97F4A7C15) & mask))
                * 14029467366897019727
            ) & mask

        def visit(value: Any) -> None:
            if value is None:
                mix(1)
            elif isinstance(value, bool):
                mix(2)
                mix(int(value))
            elif isinstance(value, int):
                mix(3)
                mix(int(value < 0))
                magnitude = abs(value)
                if magnitude == 0:
                    mix(0)
                while magnitude:
                    mix(magnitude & 0xFF)
                    magnitude >>= 8
                mix(0x1FF)
            elif isinstance(value, str):
                mix(4)
                mix(len(value))
                for character in value:
                    mix(ord(character))
            elif isinstance(value, tuple):
                mix(5)
                mix(len(value))
                for item in value:
                    visit(item)
            else:
                raise TypeError(f"状态签名包含不支持的类型: {type(value).__name__}")

        visit((position_signature_value, key))
        return f"{hash_a:016x}{hash_b:016x}"

    @staticmethod
    def _state_key(state: GameState) -> tuple[Any, ...]:
        """用于状态合并的紧凑、无损键；站位在单次搜索中保持不变。"""

        return (
            state.phase,
            state.night_count,
            state.day_count,
            (
                -1
                if state.last_guard_target_index is None
                else state.last_guard_target_index
            ),
            tuple(sorted((state.seer_check_results or {}).items())),
            state.seer_revealed,
            tuple(state.revealed_good_indices),
            tuple(state.revealed_wolf_indices),
            tuple(sorted(state.public_role_claims.items())),
            tuple(state.idiot_revealed_indices),
            tuple(state.wolf_priority_targets),
            tuple(
                (
                    player.is_alive,
                    tuple(sorted(player.skills.items())),
                )
                for player in state.players
            ),
        )

    @staticmethod
    def _state_from_key(
        key: tuple[Any, ...],
        *,
        roles: tuple[str, ...],
        position_signature_value: str,
    ) -> GameState:
        """从紧凑键恢复可继续展开的 GameState。"""

        (
            phase,
            night_count,
            day_count,
            guard_target,
            seer_checks,
            seer_revealed,
            revealed_good,
            revealed_wolf,
            public_role_claims,
            idiot_revealed,
            wolf_priority,
            player_states,
        ) = key
        return GameState(
            players=[
                Player(
                    role=role,
                    is_alive=bool(player_state[0]),
                    skills=dict(player_state[1]),
                )
                for role, player_state in zip(roles, player_states, strict=True)
            ],
            phase=str(phase),
            night_count=int(night_count),
            day_count=int(day_count),
            last_guard_target_index=(
                None if int(guard_target) < 0 else int(guard_target)
            ),
            seer_check_results=(
                {int(index): bool(value) for index, value in seer_checks}
                if seer_checks
                else None
            ),
            seer_revealed=bool(seer_revealed),
            revealed_good_indices=tuple(int(index) for index in revealed_good),
            revealed_wolf_indices=tuple(int(index) for index in revealed_wolf),
            public_role_claims={
                int(index): str(role) for index, role in public_role_claims
            },
            idiot_revealed_indices=tuple(int(index) for index in idiot_revealed),
            wolf_priority_targets=tuple(int(index) for index in wolf_priority),
            position_signature=position_signature_value,
        )

    @staticmethod
    def _kill_player(state: GameState, player_index: int) -> None:
        if 0 <= player_index < len(state.players):
            state.players[player_index].is_alive = False

    def _resolve_one_death(self, state: GameState, dead_index: int) -> list[GameState]:
        if not (0 <= dead_index < len(state.players)):
            return [state]
        player = state.players[dead_index]
        if not player.is_alive:
            return [state]
        self._kill_player(state, dead_index)
        branches = [state]
        if player.role == "猎人" and player.skills.get("开枪", 0) > 0:
            next_branches: list[GameState] = []
            for branch in branches:
                targets = self._alive_indices(branch, exclude={dead_index})
                if not targets:
                    next_branches.append(branch)
                for target in targets:
                    child = branch.clone()
                    self._consume_skill(child.players[dead_index], "开枪")
                    next_branches.extend(self._resolve_one_death(child, target))
            branches = next_branches
        if player.role == "白狼王" and player.skills.get("带走击杀", 0) > 0:
            next_branches = []
            for branch in branches:
                targets = [
                    index
                    for index in self._alive_indices(branch, exclude={dead_index})
                    if not self._is_wolf_role(branch.players[index].role)
                ]
                if not targets:
                    next_branches.append(branch)
                for target in targets:
                    child = branch.clone()
                    self._consume_skill(child.players[dead_index], "带走击杀")
                    next_branches.extend(self._resolve_one_death(child, target))
            branches = next_branches
        return branches

    def _expand_death_chain(
        self, state: GameState, death_indices: list[int]
    ) -> list[GameState]:
        branches = [state]
        for dead_index in dict.fromkeys(death_indices):
            next_branches: list[GameState] = []
            for branch in branches:
                next_branches.extend(self._resolve_one_death(branch, dead_index))
            branches = next_branches
        return branches

    def _smart_wolf_targets(
        self,
        state: GameState,
        ordinary_targets: list[int],
    ) -> list[int]:
        """只使用狼队知识和公开信息，保留同级全部目标。"""

        if not self.smart_vote:
            return ordinary_targets
        forced = [
            index for index in state.wolf_priority_targets if index in ordinary_targets
        ]
        if forced:
            return forced

        public_claims = state.public_role_claims
        revealed_idiots = set(state.idiot_revealed_indices)
        has_living_witch_or_guard = any(
            player.is_alive and player.role in {"女巫", "守卫"}
            for player in state.players
        )
        priorities: list[tuple[int, list[int]]] = []
        if not has_living_witch_or_guard:
            priorities.append(
                (0, [index for index in ordinary_targets if index in revealed_idiots])
            )
        for rank, role in enumerate(("女巫", "守卫", "预言家"), start=1):
            priorities.append(
                (
                    rank,
                    [
                        index
                        for index in ordinary_targets
                        if public_claims.get(index) == role
                    ],
                )
            )
        priorities.append(
            (4, [index for index in ordinary_targets if index in revealed_idiots])
        )
        priorities.append(
            (
                5,
                [
                    index
                    for index in ordinary_targets
                    if index in state.revealed_good_indices
                ],
            )
        )
        for _rank, candidates in priorities:
            if candidates:
                return list(dict.fromkeys(candidates))
        return ordinary_targets

    def _expand_night(self, state: GameState) -> Iterator[StateTransition]:
        alive = self._alive_indices(state)
        ordinary_targets = [
            index
            for index in alive
            if not self._is_wolf_role(state.players[index].role)
        ]
        normal_targets = self._smart_wolf_targets(state, ordinary_targets)

        guard_indices = self._alive_indices(state, role="守卫")
        guard_index = guard_indices[0] if guard_indices else None
        guard_options: list[int | None] = [None]
        if guard_index is not None:
            guard_options.extend(
                index for index in alive if index != state.last_guard_target_index
            )
        witch_indices = self._alive_indices(state, role="女巫")
        witch_index = witch_indices[0] if witch_indices else None
        seer_indices = self._alive_indices(state, role="预言家")
        seer_index = seer_indices[0] if seer_indices else None
        known = state.seer_check_results or {}
        seer_options: list[int | None] = [None]
        if seer_index is not None:
            unchecked = [
                index for index in alive if index != seer_index and index not in known
            ]
            if unchecked:
                seer_options = unchecked

        profiles = enumerate_night_tactic_profiles(
            state,
            tactics=self.tactics,
            smart_vote=self.smart_vote,
        )
        for profile in profiles:
            if profile.mode == "normal":
                wolf_targets: list[int | None] = list(normal_targets)
            elif profile.mode == "self_kill":
                wolf_targets = [profile.wolf_target]
            else:
                wolf_targets = [None]
            for wolf_target in wolf_targets:
                for guard_target in guard_options:
                    base = state.clone()
                    base.last_guard_target_index = guard_target
                    if guard_index is not None:
                        self._consume_skill(base.players[guard_index], "保护")
                    witch_options: list[tuple[str, int | None]] = [("none", None)]
                    if witch_index is not None:
                        witch = base.players[witch_index]
                        can_self_save = (
                            wolf_target == witch_index and state.night_count == 0
                        )
                        if (
                            wolf_target is not None
                            and witch.skills.get("解药", 0) > 0
                            and (wolf_target != witch_index or can_self_save)
                        ):
                            witch_options.append(("save", None))
                        if witch.skills.get("毒药", 0) > 0:
                            witch_options.extend(
                                ("poison", target)
                                for target in alive
                                if target != witch_index
                            )
                    for witch_action, poison_target in witch_options:
                        for seer_target in seer_options:
                            child = base.clone()
                            witch_saved = witch_action == "save"
                            if witch_index is not None and witch_saved:
                                self._consume_skill(child.players[witch_index], "解药")
                            if witch_index is not None and witch_action == "poison":
                                self._consume_skill(child.players[witch_index], "毒药")
                            if seer_target is not None:
                                checks = dict(child.seer_check_results or {})
                                checks[seer_target] = self._is_wolf_role(
                                    child.players[seer_target].role
                                )
                                child.seer_check_results = checks
                                if seer_index is not None:
                                    self._consume_skill(child.players[seer_index], "查验")
                            guard_saved = (
                                wolf_target is not None and guard_target == wolf_target
                            )
                            deaths: list[int] = []
                            if wolf_target is not None and not (guard_saved or witch_saved):
                                deaths.append(wolf_target)
                            if poison_target is not None:
                                deaths.append(poison_target)
                            for resolved in self._expand_death_chain(child, deaths):
                                resolved.phase = "day"
                                resolved.night_count += 1
                                resolved.wolf_priority_targets = ()
                                action_key = (
                                    "night",
                                    profile.mode,
                                    profile.tactic_names,
                                    wolf_target,
                                    guard_target,
                                    witch_action,
                                    poison_target,
                                    seer_target,
                                    tuple(deaths),
                                )
                                yield StateTransition(action_key, resolved)

    def _apply_profile_information(
        self, state: GameState, profile: DayTacticProfile
    ) -> None:
        claims = dict(state.public_role_claims)
        for decoy_index in profile.decoy_indices:
            claims[decoy_index] = "预言家"
        if profile.seer_action == "reveal":
            seers = self._alive_indices(state, role="预言家")
            if seers:
                claims[seers[0]] = "预言家"
                good = set(state.revealed_good_indices)
                wolves = set(state.revealed_wolf_indices)
                for index, is_wolf in (state.seer_check_results or {}).items():
                    (wolves if is_wolf else good).add(index)
                state.seer_revealed = True
                state.revealed_good_indices = tuple(sorted(good))
                state.revealed_wolf_indices = tuple(sorted(wolves))
        state.public_role_claims = claims
        state.wolf_priority_targets = (
            (profile.next_night_target,)
            if profile.next_night_target is not None
            else ()
        )

    def _allowed_vote_targets(
        self,
        state: GameState,
        voter: int,
        profile: DayTacticProfile,
        alive: list[int],
    ) -> list[int]:
        targets = [index for index in alive if index != voter]
        if not self.smart_vote:
            return targets
        revealed_idiots = set(state.idiot_revealed_indices)
        targets = [index for index in targets if index not in revealed_idiots]
        if not targets:
            return []
        if self._is_wolf_role(state.players[voter].role):
            non_wolf_targets = [
                index
                for index in targets
                if not self._is_wolf_role(state.players[index].role)
            ]
            if (
                profile.wolf_vote_mode == "bloc"
                and profile.wolf_vote_target in non_wolf_targets
            ):
                return [int(profile.wolf_vote_target)]
            return non_wolf_targets or targets
        confirmed_wolves = set(state.revealed_wolf_indices)
        confirmed_good = set(state.revealed_good_indices)
        if state.players[voter].role == "预言家":
            for index, is_wolf in (state.seer_check_results or {}).items():
                (confirmed_wolves if is_wolf else confirmed_good).add(index)
        known_wolves = [index for index in targets if index in confirmed_wolves]
        if known_wolves:
            return known_wolves
        filtered = [index for index in targets if index not in confirmed_good]
        return filtered or targets

    @staticmethod
    def _vote_outcome_multiplicities(
        voters: list[int],
        eligible_targets: list[int],
        allowed_targets: dict[int, list[int]],
    ) -> dict[int, int]:
        """精确统计所有票型对应的可放逐目标；平票中的每个目标都是一条分支。"""

        target_offset = {
            target: offset for offset, target in enumerate(eligible_targets)
        }
        vote_distributions: dict[tuple[int, ...], int] = {
            (0,) * len(eligible_targets): 1
        }
        for voter in voters:
            next_distributions: dict[tuple[int, ...], int] = {}
            for distribution, ways in vote_distributions.items():
                for target in allowed_targets.get(voter, []):
                    counts = list(distribution)
                    counts[target_offset[target]] += 1
                    key = tuple(counts)
                    next_distributions[key] = next_distributions.get(key, 0) + ways
            vote_distributions = next_distributions
        outcome_counts = dict.fromkeys(eligible_targets, 0)
        for distribution, ways in vote_distributions.items():
            highest = max(distribution, default=0)
            for offset, votes in enumerate(distribution):
                if votes == highest:
                    outcome_counts[eligible_targets[offset]] += ways
        return {target: count for target, count in outcome_counts.items() if count > 0}

    def _expand_day(self, state: GameState) -> Iterator[StateTransition]:
        for profile in enumerate_day_tactic_profiles(
            state,
            tactics=self.tactics,
            smart_vote=self.smart_vote,
        ):
            prepared = state.clone()
            self._apply_profile_information(prepared, profile)
            alive = self._alive_indices(prepared)
            voters = [
                index
                for index in alive
                if index not in set(prepared.idiot_revealed_indices)
            ]
            allowed_targets = {
                voter: self._allowed_vote_targets(prepared, voter, profile, alive)
                for voter in voters
            }
            vote_shape = (
                tuple(alive),
                tuple((voter, tuple(allowed_targets[voter])) for voter in voters),
            )
            outcome_multiplicities = self._vote_outcome_cache.get(vote_shape)
            if outcome_multiplicities is None:
                outcome_multiplicities = self._vote_outcome_multiplicities(
                    voters,
                    alive,
                    allowed_targets,
                )
                self._vote_outcome_cache[vote_shape] = outcome_multiplicities
            for expelled, multiplicity in outcome_multiplicities.items():
                candidate = prepared.clone()
                expelled_player = candidate.players[expelled]
                idiot_reveal = (
                    expelled_player.role == "愚者"
                    and expelled not in candidate.idiot_revealed_indices
                    and expelled_player.skills.get("身份揭示", 0) > 0
                )
                if idiot_reveal:
                    self._consume_skill(expelled_player, "身份揭示")
                    candidate.idiot_revealed_indices = tuple(
                        sorted({*candidate.idiot_revealed_indices, expelled})
                    )
                    candidate.revealed_good_indices = tuple(
                        sorted({*candidate.revealed_good_indices, expelled})
                    )
                    candidate.public_role_claims[expelled] = "愚者"
                    resolved_branches = [candidate]
                else:
                    resolved_branches = self._expand_death_chain(candidate, [expelled])
                for resolved in resolved_branches:
                    resolved.phase = "night"
                    resolved.day_count += 1
                    resolved.last_day_strategy = ""
                    resolved.last_day_votes = {}
                    resolved.wolf_priority_targets = tuple(
                        index
                        for index in resolved.wolf_priority_targets
                        if resolved.players[index].is_alive
                    )
                    action_key = (
                        "day",
                        profile.seer_action,
                        profile.decoy_indices,
                        profile.wolf_vote_mode,
                        profile.wolf_vote_target,
                        profile.next_night_target,
                        expelled,
                        "idiot_reveal" if idiot_reveal else "expelled",
                    )
                    yield StateTransition(
                        action_key,
                        resolved,
                        multiplicity=multiplicity,
                    )

    @staticmethod
    def _action_key_text(action_key: tuple[Any, ...]) -> str:
        return json.dumps(
            action_key,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _action_label(action_key: tuple[Any, ...]) -> str:
        if action_key[0] == "night":
            (
                _phase,
                night_mode,
                tactic_names,
                wolf_target,
                guard_target,
                witch_action,
                poison_target,
                seer_target,
                deaths,
            ) = action_key
            label = t(
                "action.tree_night",
                wolf=(int(wolf_target) + 1 if wolf_target is not None else "-"),
                guard=(int(guard_target) + 1 if guard_target is not None else "-"),
                witch=witch_action,
                poison=(int(poison_target) + 1 if poison_target is not None else "-"),
                seer=(int(seer_target) + 1 if seer_target is not None else "-"),
                deaths=",".join(str(int(index) + 1) for index in deaths) or "-",
            )
            reason = ",".join(tactic_names) or str(night_mode)
            return f"{label} [{reason}]"
        (
            _phase,
            seer_action,
            decoy_indices,
            wolf_vote_mode,
            wolf_vote_target,
            next_night_target,
            expelled,
            day_outcome,
        ) = action_key
        decoys = ",".join(str(int(index) + 1) for index in decoy_indices) or "-"
        strategy = (
            f"seer={seer_action};decoys={decoys};"
            f"wolf_vote={wolf_vote_mode}:"
            f"{'-' if wolf_vote_target is None else int(wolf_vote_target) + 1};"
            f"night={'-' if next_night_target is None else int(next_night_target) + 1}"
        )
        label = t(
            "action.tree_day",
            strategy=strategy,
            expelled=int(expelled) + 1,
        )
        return f"{label} [{day_outcome}]"

    def expand_state(self, state: GameState) -> Iterator[StateTransition]:
        """展开当前节点全部合法分支；不选择最优分支，也不做数量裁剪。"""

        return (
            self._expand_night(state)
            if state.phase == "night"
            else self._expand_day(state)
        )

    def _check_game_over(self, state: GameState) -> tuple[bool, str]:
        alive = [player for player in state.players if player.is_alive]
        wolves = [player for player in alive if self._is_wolf_role(player.role)]
        if not wolves:
            return True, "好人阵营胜利"
        if len(wolves) >= len(alive) - len(wolves):
            return True, "狼人阵营胜利（人数过半）"
        has_clergies = any(
            player.role in {"预言家", "女巫", "守卫", "猎人", "愚者"}
            for player in state.players
        )
        alive_clergies = [
            player
            for player in alive
            if player.role in {"预言家", "女巫", "守卫", "猎人", "愚者"}
        ]
        if has_clergies and not alive_clergies:
            return True, "狼人阵营胜利（神职角色已被消灭）"
        if not any(player.role == "村民" for player in alive):
            return True, "狼人阵营胜利（村民已被消灭）"
        return False, "未结束"

    def initial_state_for_layout(self, layout: PositionLayout) -> GameState:
        return GameState(
            players=players_for_layout(layout),
            phase="night",
            position_signature=layout.signature,
        )

    def run(self, start_state: GameState | None = None) -> dict[str, Any]:
        """运行全站位 BFS/DFS，或从传入 GameState 继续构建分支树。"""

        from ._tree_search import run_position_batch
        from ._tree_search import search_from_state

        self.start_time = time.monotonic()
        logger.info(t("log.run_start", self.search_mode.upper()))
        if start_state is not None:
            self.last_result = search_from_state(self, start_state)
        else:
            self.last_result = run_position_batch(self)
        return self.last_result

    def continue_from_game_state(
        self, state: GameState | dict[str, Any]
    ) -> dict[str, Any]:
        """未来 API 续算入口：传入完整 GameState 后继续 BFS/DFS。"""

        from ._tree_search import search_from_state

        restored = GameState.from_dict(state) if isinstance(state, dict) else state
        self.last_result = search_from_state(self, restored)
        return self.last_result
