from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any

from ._player import Player

_COMPACT_STATE_BLOB_MAGIC = b"WWS3"


def encode_compact_state_blob(compact: tuple[Any, ...] | list[Any]) -> bytes:
    """把扁平 GameState 键编码为单个可逆字节对象。

    worker 会同时保留大量已发现状态。若每个状态长期保留几十个 Python
    标量组成的 tuple，会显著放大对象数量和分配器压力。该编码只接受当前
    规范键实际使用的整数和字符串，并使用长度前缀保证无歧义；它不改变
    状态合并语义，也不包含 reward、interval 或界面字段。
    """

    if not compact or compact[0] != "flat_v2":
        raise ValueError("只能编码当前扁平 GameState 紧凑键")
    encoded = bytearray(_COMPACT_STATE_BLOB_MAGIC)
    encoded.extend(len(compact).to_bytes(4, "big", signed=False))
    for value in compact:
        if isinstance(value, bool):
            value = int(value)
        if isinstance(value, int):
            encoded.extend(b"I")
            try:
                encoded.extend(value.to_bytes(8, "big", signed=True))
            except OverflowError as exc:
                raise ValueError(f"GameState 整数字段超出 64 位范围: {value}") from exc
            continue
        if isinstance(value, str):
            payload = value.encode("utf-8")
            encoded.extend(b"S")
            encoded.extend(len(payload).to_bytes(4, "big", signed=False))
            encoded.extend(payload)
            continue
        raise TypeError(f"GameState 紧凑键包含不支持的类型: {type(value).__name__}")
    return bytes(encoded)


def decode_compact_state_blob(blob: bytes) -> tuple[Any, ...]:
    """校验并解码 worker 的单块二进制状态键。"""

    if not isinstance(blob, bytes):
        raise TypeError(f"状态键必须是 bytes，实际为 {type(blob).__name__}")
    if len(blob) < 8 or not blob.startswith(_COMPACT_STATE_BLOB_MAGIC):
        raise ValueError("无效的 GameState 二进制紧凑键")
    value_count = int.from_bytes(blob[4:8], "big", signed=False)
    offset = 8
    values: list[Any] = []
    for _value_index in range(value_count):
        if offset >= len(blob):
            raise ValueError("GameState 二进制紧凑键被截断")
        tag = blob[offset]
        offset += 1
        if tag == ord("I"):
            stop = offset + 8
            if stop > len(blob):
                raise ValueError("GameState 二进制整数字段被截断")
            values.append(int.from_bytes(blob[offset:stop], "big", signed=True))
            offset = stop
            continue
        if tag == ord("S"):
            length_stop = offset + 4
            if length_stop > len(blob):
                raise ValueError("GameState 二进制字符串长度被截断")
            payload_length = int.from_bytes(
                blob[offset:length_stop],
                "big",
                signed=False,
            )
            offset = length_stop
            payload_stop = offset + payload_length
            if payload_stop > len(blob):
                raise ValueError("GameState 二进制字符串字段被截断")
            values.append(blob[offset:payload_stop].decode("utf-8"))
            offset = payload_stop
            continue
        raise ValueError(f"未知的 GameState 二进制字段标签: {tag}")
    if offset != len(blob):
        raise ValueError("GameState 二进制紧凑键存在尾随数据")
    return tuple(values)


@dataclass(slots=True)
class _CompactStateReader:
    """无闭包的扁平状态读取器，供百万级节点恢复热路径复用字节码。"""

    values: tuple[Any, ...] | list[Any]
    offset: int = 1

    def take(self) -> Any:
        value = self.values[self.offset]
        self.offset += 1
        return value

    def take_ints(self) -> tuple[int, ...]:
        count = int(self.take())
        values: list[int] = []
        for _index in range(count):
            values.append(int(self.take()))
        return tuple(values)

    def take_pairs(
        self,
        *,
        boolean_value: bool = False,
    ) -> tuple[tuple[Any, Any], ...]:
        count = int(self.take())
        values: list[tuple[Any, Any]] = []
        for _index in range(count):
            key = self.take()
            value = self.take()
            values.append((int(key), bool(value) if boolean_value else value))
        return tuple(values)


def unpack_compact_state(
    compact: tuple[Any, ...] | list[Any] | bytes,
) -> tuple[Any, ...]:
    """把当前扁平状态或历史嵌套状态统一解码为结构化字段。"""

    if isinstance(compact, bytes):
        compact = decode_compact_state_blob(compact)
    if not compact or compact[0] != "flat_v2":
        return tuple(compact)
    reader = _CompactStateReader(compact)
    phase = str(reader.take())
    night_count = int(reader.take())
    day_count = int(reader.take())
    guard_target = int(reader.take())
    seer_revealed = bool(reader.take())
    seer_checks = reader.take_pairs(boolean_value=True)
    revealed_good = reader.take_ints()
    revealed_wolf = reader.take_ints()
    public_role_claims = reader.take_pairs()
    idiot_revealed = reader.take_ints()
    wolf_priority = reader.take_ints()
    player_count = int(reader.take())
    player_states: list[tuple[bool, tuple[tuple[str, int], ...]]] = []
    for _player_index in range(player_count):
        is_alive = bool(reader.take())
        skill_count = int(reader.take())
        skills: list[tuple[str, int]] = []
        for _skill_index in range(skill_count):
            skills.append((str(reader.take()), int(reader.take())))
        player_states.append((is_alive, tuple(skills)))
    if reader.offset != len(compact):
        raise ValueError("扁平 GameState 紧凑键存在未消费字段")
    return (
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
        tuple(player_states),
    )


def game_state_dict_from_compact(
    compact: tuple[Any, ...] | list[Any] | bytes,
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
    ) = unpack_compact_state(compact)
    observed = observation or ((), "", None, int(day_count) + int(night_count), "")
    last_day_votes, last_day_strategy, parent_state_id, depth, action_label = observed
    players = [
        {
            "role": str(role),
            "is_alive": bool(player_state[0]),
            "skills": {str(name): int(count) for name, count in dict(player_state[1]).items()},
        }
        for role, player_state in zip(roles, player_states, strict=True)
    ]
    return {
        "is_game_over": bool(is_game_over),
        "phase": str(phase),
        "night_count": int(night_count),
        "day_count": int(day_count),
        "last_guard_target_index": (None if int(guard_target) < 0 else int(guard_target)),
        "seer_check_results": ({str(index): bool(value) for index, value in seer_checks} if seer_checks else None),
        "seer_revealed": bool(seer_revealed),
        "revealed_good_indices": [int(index) for index in revealed_good],
        "revealed_wolf_indices": [int(index) for index in revealed_wolf],
        "public_role_claims": {str(index): str(role) for index, role in public_role_claims},
        "idiot_revealed_indices": [int(index) for index in idiot_revealed],
        "wolf_priority_targets": [int(index) for index in wolf_priority],
        "last_day_votes": {str(voter): int(target) for voter, target in last_day_votes},
        "last_day_strategy": str(last_day_strategy),
        "position_signature": str(position_signature),
        "action_label": str(action_label),
        "players_snapshot": [
            f"{index + 1}:{player['role']}:{'alive' if player['is_alive'] else 'dead'}"
            for index, player in enumerate(players)
        ],
        "state_id": int(state_id),
        "parent_state_id": (None if parent_state_id is None else int(parent_state_id)),
        "depth": int(depth),
        "players": players,
    }


@dataclass(slots=True)
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
            "public_role_claims": {str(index): role for index, role in self.public_role_claims.items()},
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
        revealed_good_indices: list[int] = []
        for index in data.get("revealed_good_indices", []):
            revealed_good_indices.append(int(index))
        revealed_wolf_indices: list[int] = []
        for index in data.get("revealed_wolf_indices", []):
            revealed_wolf_indices.append(int(index))
        idiot_revealed_indices: list[int] = []
        for index in data.get("idiot_revealed_indices", []):
            idiot_revealed_indices.append(int(index))
        wolf_priority_targets: list[int] = []
        for index in data.get("wolf_priority_targets", []):
            wolf_priority_targets.append(int(index))
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
            revealed_good_indices=tuple(revealed_good_indices),
            revealed_wolf_indices=tuple(revealed_wolf_indices),
            public_role_claims={
                int(index): str(role) for index, role in (data.get("public_role_claims") or {}).items()
            },
            idiot_revealed_indices=tuple(idiot_revealed_indices),
            wolf_priority_targets=tuple(wolf_priority_targets),
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
        """显式复制可变字段，避免搜索热路径进入递归 ``deepcopy``。"""

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
