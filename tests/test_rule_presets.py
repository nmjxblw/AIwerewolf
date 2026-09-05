"""Named baseline / cheap_talk ability packs."""

from __future__ import annotations

import pytest

from backend.agents.cognitive.action_catalog import ActionCatalog
from backend.agents.cognitive.action_catalog import CLAIM_IDENTITY
from backend.agents.cognitive.observe import Observation
from backend.agents.cognitive.observe import PlayerInfo
from backend.engine.game import WerewolfGame
from backend.engine.rule_presets import BASELINE
from backend.engine.rule_presets import CHEAP_TALK
from backend.engine.rule_presets import normalize_preset
from backend.engine.rule_presets import preset_from_rule_pack
from backend.engine.rule_presets import resolve_ability_flags
from backend.engine.rule_presets import wolf_night_option_tokens


def _obs(role: str, phase: str) -> Observation:
    alive = [PlayerInfo(id=f"P{seat}", name=f"P{seat}", seat=seat, alive=True) for seat in range(1, 8)]
    return Observation(
        player_id="P1",
        player_name="P1",
        player_seat=1,
        player_role=role,
        day=1,
        phase=phase,
        alive=alive,
        legal_targets=alive,
    )


def test_normalize_preset_aliases() -> None:
    assert normalize_preset("baseline") == BASELINE
    assert normalize_preset("cheap-talk") == CHEAP_TALK
    assert normalize_preset("cheap_talk") == CHEAP_TALK
    assert normalize_preset(None) is None
    with pytest.raises(ValueError, match="unknown rule_preset"):
        normalize_preset("wolfcha-default")


def test_preset_from_rule_pack() -> None:
    assert preset_from_rule_pack("baseline") == BASELINE
    assert preset_from_rule_pack("cheap-talk") == CHEAP_TALK
    assert preset_from_rule_pack("wolfcha-default") is None


def test_baseline_flags_block_fake_claim_and_special_knives() -> None:
    flags = resolve_ability_flags("baseline")
    assert flags["honesty_rule"] is True
    assert flags["wolf_self_knife"] is False
    assert flags["wolf_empty_knife"] is False
    assert wolf_night_option_tokens(flags) == set()

    speech = ActionCatalog.for_turn(_obs("Villager", "DAY_SPEECH"), honesty_rule=flags["honesty_rule"])
    assert "seer_claim" not in speech.ids()
    seer_speech = ActionCatalog.for_turn(
        _obs("Seer", "DAY_SPEECH"),
        honesty_rule=flags["honesty_rule"],
    )
    spec = seer_speech.get("seer_claim")
    assert spec is not None
    assert spec.claim_mode == CLAIM_IDENTITY
    night = ActionCatalog.for_turn(
        _obs("Werewolf", "NIGHT_WOLF_ACTION"),
        wolf_night_options=wolf_night_option_tokens(flags),
    )
    assert night.ids() == ("attack",)


def test_cheap_talk_flags_unlock_all_listed_abilities() -> None:
    flags = resolve_ability_flags("cheap_talk")
    assert flags["honesty_rule"] is False
    assert flags["wolf_self_knife"] is True
    assert flags["wolf_empty_knife"] is True
    assert wolf_night_option_tokens(flags) == {"self", "empty"}

    villager = ActionCatalog.for_turn(_obs("Villager", "DAY_SPEECH"), honesty_rule=False)
    wolf = ActionCatalog.for_turn(_obs("Werewolf", "DAY_SPEECH"), honesty_rule=False)
    assert "seer_claim" in villager.ids()
    assert "seer_claim" in wolf.ids()
    night = ActionCatalog.for_turn(
        _obs("Werewolf", "NIGHT_WOLF_ACTION"),
        wolf_night_options=wolf_night_option_tokens(flags),
    )
    assert night.ids() == ("attack", "self_attack", "skip")


def test_explicit_override_wins_over_preset() -> None:
    flags = resolve_ability_flags("cheap_talk", wolf_self_knife=False)
    assert flags["wolf_empty_knife"] is True
    assert flags["wolf_self_knife"] is False


def test_werewolf_game_applies_baseline_preset() -> None:
    game = WerewolfGame(rule_preset="baseline", seed=1)
    assert game.rule_preset == BASELINE
    assert game.honesty_rule is True
    assert game.wolf_self_knife is False
    assert game.wolf_empty_knife is False
    assert game.state.board_options["rule_preset"] == BASELINE
    assert game.state.board_options["honesty_rule"] is True


def test_werewolf_game_applies_cheap_talk_preset() -> None:
    game = WerewolfGame(rule_preset="cheap-talk", seed=1)
    assert game.rule_preset == CHEAP_TALK
    assert game.honesty_rule is False
    assert game.wolf_self_knife is True
    assert game.wolf_empty_knife is True


def test_web_preset_reads_env_when_api_omits_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.engine.rule_presets import resolve_web_rule_preset

    monkeypatch.delenv("AIWEREWOLF_RULE_PRESET", raising=False)
    assert resolve_web_rule_preset(None, "wolfcha-default") is None
    monkeypatch.setenv("AIWEREWOLF_RULE_PRESET", "baseline")
    assert resolve_web_rule_preset(None, "wolfcha-default") == BASELINE
    assert resolve_web_rule_preset("cheap_talk", "wolfcha-default") == CHEAP_TALK


def test_build_game_uses_env_rule_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.engine.rule_presets import env_ability_overrides
    from backend.engine.rule_presets import resolve_web_rule_preset

    monkeypatch.setenv("AIWEREWOLF_RULE_PRESET", "baseline")
    monkeypatch.delenv("AIWEREWOLF_HONESTY_RULE", raising=False)
    monkeypatch.delenv("AIWEREWOLF_WOLF_SELF_KNIFE", raising=False)
    monkeypatch.delenv("AIWEREWOLF_WOLF_EMPTY_KNIFE", raising=False)
    monkeypatch.delenv("AIWEREWOLF_WOLF_NIGHT_CHAT", raising=False)
    game = WerewolfGame(
        seed=1,
        rule_preset=resolve_web_rule_preset(),
        **env_ability_overrides(),
    )
    assert game.rule_preset == BASELINE
    assert game.honesty_rule is True
    assert game.wolf_empty_knife is False
    assert game.wolf_self_knife is False
