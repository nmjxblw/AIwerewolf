from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from time import time
from typing import Any
from uuid import uuid4


@dataclass
class RoomCreateRequest:
    name: str = "Demo Room"
    seed: int = 7
    player_count: int = 7
    agent_type: str = "llm"
    human_seat: int | None = None
    rule_pack_id: str = "wolfcha-default"
    custom_roles: dict | None = None
    has_badge: bool = True
    share_persona: bool = True
    enable_strategy: bool = True
    persona_names: list[str] | None = None
    has_last_words: bool = True
    parallel_speech: bool = True


@dataclass
class RoomRecord:
    id: str
    name: str
    seed: int
    player_count: int
    agent_type: str
    human_seat: int | None = None
    rule_pack_id: str = "wolfcha-default"
    custom_roles: dict | None = None
    has_badge: bool = True
    share_persona: bool = True
    enable_strategy: bool = True
    persona_names: list[str] | None = None
    has_last_words: bool = True
    parallel_speech: bool = True
    status: str = "idle"
    created_at: float = field(default_factory=time)
    updated_at: float = field(default_factory=time)
    current_game_id: str | None = None
    game_history: list[str] = field(default_factory=list)
    latest_snapshot: dict[str, Any] | None = None

    @classmethod
    def create(
        cls,
        name: str,
        seed: int,
        player_count: int,
        agent_type: str,
        human_seat: int | None = None,
        rule_pack_id: str = "wolfcha-default",
        custom_roles: dict | None = None,
        has_badge: bool = True,
        share_persona: bool = True,
        enable_strategy: bool = True,
        persona_names: list[str] | None = None,
        has_last_words: bool = True,
        parallel_speech: bool = True,
    ) -> RoomRecord:
        return cls(
            id=str(uuid4()),
            name=name,
            seed=seed,
            player_count=player_count,
            agent_type=agent_type,
            human_seat=human_seat,
            rule_pack_id=rule_pack_id,
            custom_roles=custom_roles,
            has_badge=has_badge,
            share_persona=share_persona,
            enable_strategy=enable_strategy,
            persona_names=persona_names,
            has_last_words=has_last_words,
            parallel_speech=parallel_speech,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "seed": self.seed,
            "player_count": self.player_count,
            "agent_type": self.agent_type,
            "human_seat": self.human_seat,
            "rule_pack_id": self.rule_pack_id,
            "custom_roles": self.custom_roles,
            "has_badge": self.has_badge,
            "share_persona": self.share_persona,
            "enable_strategy": self.enable_strategy,
            "persona_names": self.persona_names,
            "has_last_words": self.has_last_words,
            "parallel_speech": self.parallel_speech,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "current_game_id": self.current_game_id,
            "game_history": list(self.game_history),
            "latest_snapshot": self.latest_snapshot,
        }
