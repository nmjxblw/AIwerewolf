"""昼夜战术分支模型。"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

from ._game_state import GameState

TACTIC_SEER_HIDE = "seer_hide"
TACTIC_VILLAGER_DECOY = "villager_decoy"
TACTIC_WOLF_BLOC = "wolf_bloc"
TACTIC_WOLF_SELF_KILL = "wolf_self_kill"
TACTIC_WOLF_NO_KILL = "wolf_no_kill"

DAY_TACTICS = frozenset(
    {TACTIC_SEER_HIDE, TACTIC_VILLAGER_DECOY, TACTIC_WOLF_BLOC}
)
NIGHT_TACTICS = frozenset({TACTIC_WOLF_SELF_KILL, TACTIC_WOLF_NO_KILL})
DEFAULT_TACTICS = DAY_TACTICS | NIGHT_TACTICS


@dataclass(frozen=True)
class DayTacticProfile:
    """白天基线或一个显式战术组合。"""

    seer_action: str
    decoy_indices: tuple[int, ...]
    wolf_vote_mode: str
    wolf_vote_target: int | None
    next_night_target: int | None
    tactic_names: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        decoys = ",".join(str(index) for index in self.decoy_indices) or "-"
        wolf_target = "-" if self.wolf_vote_target is None else str(self.wolf_vote_target)
        night_target = "-" if self.next_night_target is None else str(self.next_night_target)
        names = ",".join(self.tactic_names) or "baseline"
        return (
            f"tactics={names};seer={self.seer_action};decoys={decoys};"
            f"wolf_vote={self.wolf_vote_mode}:{wolf_target};night={night_target}"
        )


@dataclass(frozen=True)
class NightTacticProfile:
    """狼人夜间基线、指定自刀或空刀分支。"""

    mode: str
    wolf_target: int | None = None
    tactic_names: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        target = "-" if self.wolf_target is None else str(self.wolf_target)
        names = ",".join(self.tactic_names) or "baseline"
        return f"tactics={names};mode={self.mode};target={target}"


def _alive_indices(state: GameState, role: str | None = None) -> list[int]:
    return [
        index
        for index, player in enumerate(state.players)
        if player.is_alive and (role is None or player.role == role)
    ]


def _is_wolf(role: str) -> bool:
    return role in {"狼人", "白狼王"}


def _decoy_options(
    state: GameState,
    tactics: frozenset[str],
) -> list[tuple[int, ...]]:
    options: list[tuple[int, ...]] = [()]
    if TACTIC_VILLAGER_DECOY not in tactics:
        return options
    villagers = _alive_indices(state, "村民")
    for count in range(1, len(villagers) + 1):
        options.extend(combinations(villagers, count))
    return options


def enumerate_day_tactic_profiles(
    state: GameState,
    *,
    tactics: frozenset[str] = DEFAULT_TACTICS,
    smart_vote: bool = True,
) -> list[DayTacticProfile]:
    """保留正常基线，并枚举用户启用的白天战术组合。"""

    effective = tactics & DAY_TACTICS if smart_vote else frozenset()
    alive = _alive_indices(state)
    alive_wolves = [index for index in alive if _is_wolf(state.players[index].role)]
    revealed_idiots = set(state.idiot_revealed_indices)
    non_wolves = [
        index
        for index in alive
        if index not in alive_wolves and index not in revealed_idiots
    ]
    alive_seers = _alive_indices(state, "预言家")
    seer_index = alive_seers[0] if alive_seers else None

    if seer_index is not None and not state.seer_revealed:
        seer_options = ["reveal"]
        if TACTIC_SEER_HIDE in effective:
            seer_options.append("hide")
    else:
        seer_options = ["none"]

    wolf_vote_options: list[tuple[str, int | None]] = [("normal", None)]
    if alive_wolves and TACTIC_WOLF_BLOC in effective:
        wolf_vote_options.extend(("bloc", target) for target in non_wolves)

    profiles: list[DayTacticProfile] = []
    for seer_action in seer_options:
        for decoys in _decoy_options(state, effective):
            priority_targets = list(decoys)
            if seer_action == "reveal" and seer_index is not None:
                priority_targets.insert(0, seer_index)
            night_targets: list[int | None] = (
                list(dict.fromkeys(priority_targets)) if priority_targets else [None]
            )
            for wolf_vote_mode, wolf_vote_target in wolf_vote_options:
                for next_night_target in night_targets:
                    names: list[str] = []
                    if seer_action == "hide":
                        names.append(TACTIC_SEER_HIDE)
                    if decoys:
                        names.append(TACTIC_VILLAGER_DECOY)
                    if wolf_vote_mode == "bloc":
                        names.append(TACTIC_WOLF_BLOC)
                    profiles.append(
                        DayTacticProfile(
                            seer_action=seer_action,
                            decoy_indices=tuple(decoys),
                            wolf_vote_mode=wolf_vote_mode,
                            wolf_vote_target=wolf_vote_target,
                            next_night_target=next_night_target,
                            tactic_names=tuple(names),
                        )
                    )
    return profiles


def enumerate_night_tactic_profiles(
    state: GameState,
    *,
    tactics: frozenset[str] = DEFAULT_TACTICS,
    smart_vote: bool = True,
) -> list[NightTacticProfile]:
    """保留正常刀口，并在当前状态合法时增加自刀和空刀。"""

    profiles = [NightTacticProfile(mode="normal")]
    if not smart_vote:
        return profiles
    has_protection_role = any(
        player.role in {"女巫", "守卫"} for player in state.players
    )
    if not has_protection_role:
        return profiles
    alive_wolves = [
        index
        for index, player in enumerate(state.players)
        if player.is_alive and _is_wolf(player.role)
    ]
    if TACTIC_WOLF_SELF_KILL in tactics and len(alive_wolves) >= 2:
        profiles.extend(
            NightTacticProfile(
                mode="self_kill",
                wolf_target=index,
                tactic_names=(TACTIC_WOLF_SELF_KILL,),
            )
            for index in alive_wolves
        )
    if TACTIC_WOLF_NO_KILL in tactics:
        profiles.append(
            NightTacticProfile(
                mode="no_kill",
                tactic_names=(TACTIC_WOLF_NO_KILL,),
            )
        )
    return profiles
