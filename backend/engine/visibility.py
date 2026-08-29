from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any

from backend.engine.models import Alignment
from backend.engine.models import GameState
from backend.engine.models import Phase
from backend.engine.models import Player
from backend.engine.models import Role


@dataclass(frozen=True)
class PlayerView:
    player_id: str
    day: int
    phase: str
    self_player: dict[str, Any]
    players: list[dict[str, Any]]
    public_events: list[dict[str, Any]]
    private_events: list[dict[str, Any]]
    known_wolves: list[dict[str, Any]]
    observations: list[str]
    legal_targets: list[dict[str, Any]] = field(default_factory=list)
    game_id: str = ""
    role_roster: list[str] = field(default_factory=list)
    has_badge: bool = False
    """本局实际角色清单（如 ["Werewolf","Seer","Witch","Villager"]），用于 prompt 规则摘要"""


class Visibility:
    """Builds per-agent views and keeps private role information isolated."""

    def __init__(self, share_persona: bool = True, has_badge: bool = False):
        self.share_persona = share_persona
        self.has_badge = has_badge

    def for_player(self, state: GameState, player_id: str) -> PlayerView:
        player = state.player(player_id)
        public_events = [event.to_dict() for event in state.events if event.visibility == "public"]
        private_events = [
            event.to_dict() for event in state.events if event.visibility == "private" and player_id in event.visible_to
        ]
        known_wolves = []
        if player.alignment == Alignment.WOLF:
            known_wolves = [p.private_dict() for p in state.players if p.alignment == Alignment.WOLF]

        # Keep duplicate role entries so prompts can state exact counts
        # (e.g. 2 Werewolf). A set would collapse that to a single "Werewolf"
        # and models then invent 3-wolf boards from training priors.
        role_roster = sorted(p.role.value for p in state.players)
        return PlayerView(
            player_id=player_id,
            day=state.day,
            phase=state.phase.value,
            self_player=player.private_dict(),
            players=[self._visible_player(player, target) for target in state.players],
            public_events=public_events,
            private_events=private_events,
            known_wolves=known_wolves,
            observations=self._observations(private_events),
            legal_targets=self._legal_targets(state, player),
            game_id=state.id,
            role_roster=role_roster,
            has_badge=self.has_badge,
        )

    def _visible_player(self, viewer: Player, target: Player) -> dict[str, Any]:
        if viewer.id == target.id:
            return target.private_dict()
        if viewer.alignment == Alignment.WOLF and target.alignment == Alignment.WOLF:
            return target.private_dict()
        return target.public_dict(share_persona=self.share_persona)

    def _observations(self, events: list[dict[str, Any]]) -> list[str]:
        observations: list[str] = []
        for event in events:
            payload = event["payload"]
            if event["type"] == "PRIVATE_INFO":
                observations.append(str(payload.get("message", "")))
        return [item for item in observations if item]

    def _legal_targets(self, state: GameState, player: Player) -> list[dict[str, Any]]:
        target_ids: set[str] | None = None
        include_self = False

        if state.phase == Phase.DAY_BADGE_ELECTION and state.badge.candidates:
            target_ids = set(state.badge.candidates)
            include_self = True
        elif state.phase == Phase.DAY_VOTE and state.pk_targets:
            target_ids = set(state.pk_targets)
        elif state.phase == Phase.NIGHT_WOLF_ACTION:
            # 廉价磋商板子：自刀选项开启时，狼人自己也进入合法目标
            target_ids = {target.id for target in state.alive_players if target.alignment != Alignment.WOLF}
            if state.board_options.get("wolf_self_knife"):
                target_ids.add(player.id)
                include_self = True
        elif state.phase in {
            Phase.DAY_VOTE,
            Phase.NIGHT_SEER_ACTION,
            Phase.HUNTER_SHOOT,
            Phase.WHITE_WOLF_KING_BOOM,
            Phase.BADGE_TRANSFER,
        }:
            target_ids = {target.id for target in state.alive_players}
        elif state.phase == Phase.NIGHT_GUARD_ACTION:
            target_ids = {
                target.id
                for target in state.alive_players
                if target.id != state.night_actions.last_guard_target_id
            }

        if target_ids is None:
            return []
        if state.phase == Phase.NIGHT_SEER_ACTION and player.role == Role.SEER:
            # 已有查验结果的目标不再进入合法列表；只剩已查验对象时允许跳过。
            checked_ids = {
                str(event.payload.get("target_id", ""))
                for event in state.events
                if event.payload.get("kind") == "seer_result"
                and player.id in event.visible_to
                and event.payload.get("target_id")
            }
            target_ids.difference_update(checked_ids)
        if state.phase == Phase.DAY_VOTE and player.role == Role.SEER:
            confirmed_good_ids = {
                str(event.payload.get("target_id", ""))
                for event in state.events
                if event.payload.get("kind") == "seer_result"
                and player.id in event.visible_to
                and not bool(event.payload.get("is_wolf"))
            }
            target_ids.difference_update(confirmed_good_ids)

        targets = []
        for target in state.alive_players:
            if target.id not in target_ids:
                continue
            if target.id == player.id and not include_self:
                continue
            targets.append(
                {
                    "id": target.id,
                    "seat": target.seat,
                    "name": target.name,
                    "alive": target.alive,
                }
            )
        return targets
