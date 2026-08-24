"""Unit tests for the report §8.4 institution switches.

Covers:
  * honesty.detect_fake_seer_claim — first-person seer-claim regex
  * WerewolfGame(random_vote_device=True) — day exile designated uniformly,
    agent voting bypassed, votes recorded for the designated target
  * WerewolfGame(honesty_rule=True) — fake claims by non-seers rejected and
    regenerated; the real seer's claim passes untouched; persistent
    violators are dropped (message cannot be conveyed)
"""

from __future__ import annotations

from backend.engine.game import WerewolfGame
from backend.engine.honesty import detect_fake_seer_claim
from backend.engine.models import ActionType
from backend.engine.models import Alignment
from backend.engine.models import Decision
from backend.engine.models import Player
from backend.engine.models import Role


def _make_game(seed: int = 13, **engine_kwargs) -> WerewolfGame:
    players = [
        Player(id="P1", seat=1, name="VilA", role=Role.VILLAGER, alignment=Alignment.VILLAGE),
        Player(id="P2", seat=2, name="WolfA", role=Role.WEREWOLF, alignment=Alignment.WOLF),
        Player(id="P3", seat=3, name="Seer", role=Role.SEER, alignment=Alignment.VILLAGE),
        Player(id="P4", seat=4, name="VilB", role=Role.VILLAGER, alignment=Alignment.VILLAGE),
    ]
    return WerewolfGame(players=players, agents={p.id: object() for p in players}, seed=seed, **engine_kwargs)


# ------------------------------------------------------------------
# Detector
# ------------------------------------------------------------------

def test_detect_fake_seer_claim_positives() -> None:
    claims = [
        "我是预言家，昨晚我查验了1号，1号是狼人。",
        "我就是预言家，你们要相信我。",
        "我才是真预言家，对跳的那个是悍跳狼。",
        "我起跳预言家，昨晚验了3号，金水。",
        "我昨晚验的4号是好人。",
        "我查了2号，狼人无误。",
        "我给5号金水。",
        "3号是我的金水。",
        "我查杀6号。",
        "我的查验结果是7号好人。",
    ]
    for text in claims:
        assert detect_fake_seer_claim(text), f"should flag: {text}"


def test_detect_fake_seer_claim_negatives() -> None:
    legal = [
        "预言家说查了3号，我不太信他的逻辑。",  # third-person discussion
        "他给5号金水，但我觉得不可靠。",
        "如果我是预言家，我就不会这么急。",
        "假如我就是预言家，这局早赢了。",
        "我不是预言家，我只是个普通村民。",
        "我怀疑2号，他的发言前后矛盾。",
        "我想听3号再解释一下投票的理由。",
        "如果我昨晚能验人就好了，可惜我不能。",
    ]
    for text in legal:
        assert detect_fake_seer_claim(text) is None, f"should NOT flag: {text}"


def test_detect_fake_seer_claim_hypothetical_plus_real_claim() -> None:
    text = "如果我是预言家我肯定跳了，但Anyway我昨晚查验了2号，2号是狼。"
    assert detect_fake_seer_claim(text) is not None


# ------------------------------------------------------------------
# random_vote_device
# ------------------------------------------------------------------

def test_random_vote_device_designates_target_and_bypasses_agent_votes() -> None:
    game = _make_game(random_vote_device=True, disable_badge=True)
    game.state.day = 1

    asked: list[str] = []
    game._batch_ask = lambda players, request, call_fn: asked.append(request) or []  # type: ignore[assignment]

    game._vote_phase()

    # The device replaces agent voting entirely.
    assert asked == []

    votes = dict(game.state.votes)
    assert votes, "device should record votes for every eligible voter"
    assert set(votes.values()).__len__() == 1, "all votes go to the designated target"
    designated = next(iter(set(votes.values())))
    assert designated in {p.id for p in game.state.players if p.alive}

    messages = [
        str(e.to_dict().get("payload", {}).get("message", ""))
        for e in game.state.events
        if e.to_dict().get("type") == "SYSTEM_MESSAGE"
    ]
    assert any("随机投票装置" in m and "为今日放逐目标" in m for m in messages)
    vote_events = [e.to_dict() for e in game.state.events if e.to_dict().get("type") == "VOTE_CAST"]
    assert vote_events and all(v["payload"].get("agent_source") == "random_device" for v in vote_events)


def test_random_vote_device_covers_all_alive_players_over_many_days() -> None:
    """Uniform designation: over many seeds/days every seat is reachable."""
    from collections import Counter

    hits: Counter[str] = Counter()
    for seed in range(40):
        game = _make_game(random_vote_device=True, disable_badge=True, seed=seed)
        game.state.day = 1
        game._batch_ask = lambda players, request, call_fn: []  # type: ignore[assignment]
        game._vote_phase()
        designated = next(iter(set(game.state.votes.values())))
        hits[designated] += 1
    assert set(hits) == {"P1", "P2", "P3", "P4"}, "every alive seat must be designatable"


# ------------------------------------------------------------------
# honesty_rule
# ------------------------------------------------------------------

def _speech_decision(player_id: str, speech: str) -> Decision:
    return Decision(player_id, ActionType.TALK, speech=speech, reasoning="test", metadata={"source": "llm"})


def test_honesty_rule_rejects_fake_claim_and_accepts_retry() -> None:
    game = _make_game(honesty_rule=True)
    game.state.day = 1

    def fake_batch(players, request, call_fn):
        assert request == "TALK"
        out = []
        for p in players:
            if p.role == Role.WEREWOLF:
                out.append(_speech_decision(p.id, "我是预言家，昨晚查验了1号，1号是狼人。"))
            elif p.role == Role.SEER:
                out.append(_speech_decision(p.id, "我是预言家，昨晚查验了2号，2号是狼人。"))
            else:
                out.append(_speech_decision(p.id, "我觉得2号的发言有点奇怪，先观察一天。"))
        return out

    retries: dict[str, int] = {}

    def fake_ask(player, request, call):
        assert request == "TALK"
        retries[player.id] = retries.get(player.id, 0) + 1
        return _speech_decision(player.id, "好，不谈查验了。2号的发言确实可疑，我保留怀疑。")

    game._batch_ask = fake_batch  # type: ignore[assignment]
    game._ask = fake_ask  # type: ignore[assignment]

    game._speech_phase()

    speeches = {
        e.to_dict()["payload"]["actor_id"]: e.to_dict()["payload"]["speech"]
        for e in game.state.events
        if e.to_dict().get("type") == "CHAT_MESSAGE"
    }
    # Fake claim never broadcast; the retried compliant speech is.
    assert "我是预言家" not in speeches.get("P2", "")
    assert "2号的发言确实可疑" in speeches["P2"]
    # The real seer's claim passes untouched (exempt from the filter).
    assert "我是预言家" in speeches["P3"]
    assert "先观察一天" in speeches["P1"]
    # Only the wolf needed a retry.
    assert retries == {"P2": 1}

    messages = [
        str(e.to_dict().get("payload", {}).get("message", ""))
        for e in game.state.events
        if e.to_dict().get("type") == "SYSTEM_MESSAGE"
    ]
    assert any("诚实规则" in m and "驳回" in m for m in messages)


def test_honesty_rule_drops_speech_after_repeated_violations() -> None:
    game = _make_game(honesty_rule=True)
    game.state.day = 1

    def always_claim(players, request, call_fn):
        return [_speech_decision(p.id, "我是预言家，我查了1号。") for p in players]

    game._batch_ask = always_claim  # type: ignore[assignment]
    game._ask = lambda player, request, call: _speech_decision(  # type: ignore[assignment]
        player.id, "我就是预言家，昨晚查验了2号。"
    )

    game._speech_phase()

    chats = [
        e.to_dict()["payload"]["actor_id"]
        for e in game.state.events
        if e.to_dict().get("type") == "CHAT_MESSAGE"
    ]
    # Everyone violated; only the real seer (exempt) gets broadcast.
    assert chats == ["P3"]

    messages = [
        str(e.to_dict().get("payload", {}).get("message", ""))
        for e in game.state.events
        if e.to_dict().get("type") == "SYSTEM_MESSAGE"
    ]
    assert any("多次违反诚实规则" in m for m in messages)


def test_honesty_rule_off_by_default() -> None:
    game = _make_game()
    game.state.day = 1
    game._batch_ask = lambda players, request, call_fn: [  # type: ignore[assignment]
        _speech_decision(p.id, "我是预言家，昨晚查验了3号。") for p in players
    ]
    game._speech_phase()
    chats = [
        e.to_dict()["payload"]["actor_id"]
        for e in game.state.events
        if e.to_dict().get("type") == "CHAT_MESSAGE"
    ]
    # Without the switch every claim is broadcast (cheap-talk baseline).
    assert len(chats) == 4
