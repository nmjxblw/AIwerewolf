from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any

from ._player import Player


def game_state_dict_from_compact(
    compact: tuple[Any, ...] | list[Any],
    *,
    roles: tuple[str, ...] | list[str],
    position_signature: str,
    is_game_over: bool,
    state_id: int,
    observation: tuple[Any, ...] | list[Any] | None = None,
) -> dict[str, Any]:
    """从无损紧凑状态与非转移观察字段按需重建完整 GameState 字典。"""

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
    ) = compact
    observed = observation or ((), "", None, int(day_count) + int(night_count), "")
    last_day_votes, last_day_strategy, parent_state_id, depth, action_label = observed
    players = [
        {
            "role": str(role),
            "is_alive": bool(player_state[0]),
            "skills": {
                str(name): int(count) for name, count in dict(player_state[1]).items()
            },
        }
        for role, player_state in zip(roles, player_states, strict=True)
    ]
    return {
        "is_game_over": bool(is_game_over),
        "phase": str(phase),
        "night_count": int(night_count),
        "day_count": int(day_count),
        "last_guard_target_index": (
            None if int(guard_target) < 0 else int(guard_target)
        ),
        "seer_check_results": (
            {str(index): bool(value) for index, value in seer_checks}
            if seer_checks
            else None
        ),
        "seer_revealed": bool(seer_revealed),
        "revealed_good_indices": [int(index) for index in revealed_good],
        "revealed_wolf_indices": [int(index) for index in revealed_wolf],
        "public_role_claims": {
            str(index): str(role) for index, role in public_role_claims
        },
        "idiot_revealed_indices": [int(index) for index in idiot_revealed],
        "wolf_priority_targets": [int(index) for index in wolf_priority],
        "last_day_votes": {
            str(voter): int(target) for voter, target in last_day_votes
        },
        "last_day_strategy": str(last_day_strategy),
        "position_signature": str(position_signature),
        "action_label": str(action_label),
        "players_snapshot": [
            f"{index + 1}:{player['role']}:{'alive' if player['is_alive'] else 'dead'}"
            for index, player in enumerate(players)
        ],
        "state_id": int(state_id),
        "parent_state_id": (
            None if parent_state_id is None else int(parent_state_id)
        ),
        "depth": int(depth),
        "players": players,
    }


@dataclass
class GameState:
    """可序列化的树节点状态，也是未来 API 续算的状态契约。"""

    players: list[Player]
    is_game_over: bool = False
    night_count: int = 0
    day_count: int = 0
    phase: str = "night"
    last_guard_target_index: int | None = None
    seer_check_results: dict[int, bool] | None = None
    seer_revealed: bool = False
    revealed_good_indices: tuple[int, ...] = ()
    revealed_wolf_indices: tuple[int, ...] = ()
    public_role_claims: dict[int, str] = field(default_factory=dict)
    idiot_revealed_indices: tuple[int, ...] = ()
    wolf_priority_targets: tuple[int, ...] = ()
    last_day_votes: dict[int, int] = field(default_factory=dict)
    last_day_strategy: str = ""
    position_signature: str = ""
    action_label: str = ""
    players_snapshot: list[str] = field(default_factory=list)
    state_id: int = -1
    parent_state_id: int | None = None
    depth: int = 0

    def to_dict(self) -> dict[str, Any]:
        """序列化全部可续算字段；输出可直接作为未来 API payload。"""

        return {
            "is_game_over": self.is_game_over,
            "phase": self.phase,
            "night_count": self.night_count,
            "day_count": self.day_count,
            "last_guard_target_index": self.last_guard_target_index,
            "seer_check_results": (
                {str(key): value for key, value in self.seer_check_results.items()}
                if self.seer_check_results is not None
                else None
            ),
            "seer_revealed": self.seer_revealed,
            "revealed_good_indices": list(self.revealed_good_indices),
            "revealed_wolf_indices": list(self.revealed_wolf_indices),
            "public_role_claims": {
                str(index): role for index, role in self.public_role_claims.items()
            },
            "idiot_revealed_indices": list(self.idiot_revealed_indices),
            "wolf_priority_targets": list(self.wolf_priority_targets),
            "last_day_votes": {str(voter): target for voter, target in self.last_day_votes.items()},
            "last_day_strategy": self.last_day_strategy,
            "position_signature": self.position_signature,
            "action_label": self.action_label,
            "players_snapshot": list(self.players_snapshot),
            "state_id": self.state_id,
            "parent_state_id": self.parent_state_id,
            "depth": self.depth,
            "players": [
                {
                    "role": player.role,
                    "is_alive": player.is_alive,
                    "skills": dict(player.skills),
                }
                for player in self.players
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GameState:
        """从 API/CLI 字典恢复完整状态，不丢失站位和战术上下文。"""

        players = [
            Player(
                role=str(player.get("role", "村民")),
                is_alive=bool(player.get("is_alive", True)),
                skills={str(name): int(count) for name, count in (player.get("skills") or {}).items()},
            )
            for player in (data.get("players") or [])
        ]
        seer_results = data.get("seer_check_results")
        return cls(
            players=players,
            is_game_over=bool(data.get("is_game_over", False)),
            phase=str(data.get("phase", "night")),
            night_count=int(data.get("night_count", 0)),
            day_count=int(data.get("day_count", 0)),
            last_guard_target_index=(
                int(data["last_guard_target_index"]) if data.get("last_guard_target_index") is not None else None
            ),
            seer_check_results=(
                {int(key): bool(value) for key, value in seer_results.items()} if seer_results else None
            ),
            seer_revealed=bool(data.get("seer_revealed", False)),
            revealed_good_indices=tuple(int(index) for index in data.get("revealed_good_indices", [])),
            revealed_wolf_indices=tuple(int(index) for index in data.get("revealed_wolf_indices", [])),
            public_role_claims={
                int(index): str(role)
                for index, role in (data.get("public_role_claims") or {}).items()
            },
            idiot_revealed_indices=tuple(
                int(index) for index in data.get("idiot_revealed_indices", [])
            ),
            wolf_priority_targets=tuple(int(index) for index in data.get("wolf_priority_targets", [])),
            last_day_votes={int(voter): int(target) for voter, target in (data.get("last_day_votes") or {}).items()},
            last_day_strategy=str(data.get("last_day_strategy", "")),
            position_signature=str(data.get("position_signature", "")),
            action_label=str(data.get("action_label", "")),
            players_snapshot=[str(value) for value in data.get("players_snapshot", [])],
            state_id=int(data.get("state_id", -1)),
            parent_state_id=(int(data["parent_state_id"]) if data.get("parent_state_id") is not None else None),
            depth=int(data.get("depth", 0)),
        )

    def clone(self) -> GameState:
        """显式复制可变字段，规避 CPython 3.14 ``deepcopy`` 崩溃。"""

        return GameState(
            players=[
                Player(role=player.role, is_alive=player.is_alive, skills=dict(player.skills))
                for player in self.players
            ],
            is_game_over=self.is_game_over,
            night_count=self.night_count,
            day_count=self.day_count,
            phase=self.phase,
            last_guard_target_index=self.last_guard_target_index,
            seer_check_results=(dict(self.seer_check_results) if self.seer_check_results is not None else None),
            seer_revealed=self.seer_revealed,
            revealed_good_indices=tuple(self.revealed_good_indices),
            revealed_wolf_indices=tuple(self.revealed_wolf_indices),
            public_role_claims=dict(self.public_role_claims),
            idiot_revealed_indices=tuple(self.idiot_revealed_indices),
            wolf_priority_targets=tuple(self.wolf_priority_targets),
            last_day_votes=dict(self.last_day_votes),
            last_day_strategy=self.last_day_strategy,
            position_signature=self.position_signature,
            action_label=self.action_label,
            players_snapshot=list(self.players_snapshot),
            state_id=self.state_id,
            parent_state_id=self.parent_state_id,
            depth=self.depth,
        )
