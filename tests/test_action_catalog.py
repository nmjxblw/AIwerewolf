"""ActionCatalog legality and one-shot structured speech/night decisions."""

from __future__ import annotations

from langchain_core.messages import AIMessage

from backend.agents.cognitive.action_catalog import ActionCatalog
from backend.agents.cognitive.action_catalog import CLAIM_FAKE
from backend.agents.cognitive.action_catalog import CLAIM_IDENTITY
from backend.agents.cognitive.action_catalog import CLAIM_TRUE
from backend.agents.cognitive.action_catalog import coerce_legacy_payload
from backend.agents.cognitive.action_catalog import validate_payload
from backend.agents.cognitive.factory import create_cognitive_agent_with_character
from backend.agents.cognitive.observe import Observation
from backend.agents.cognitive.observe import PlayerInfo
from backend.engine.models import ActionType
from backend.engine.models import EventType
from backend.engine.visibility import PlayerView


def _players(*alive_seats: int, dead_seats: tuple[int, ...] = ()) -> list[PlayerInfo]:
    players = [
        PlayerInfo(id=f"P{seat}", name=f"P{seat}", seat=seat, alive=True) for seat in alive_seats
    ]
    players.extend(PlayerInfo(id=f"P{seat}", name=f"P{seat}", seat=seat, alive=False) for seat in dead_seats)
    return players


def _obs(
    role: str,
    phase: str = "DAY_SPEECH",
    *,
    seat: int = 1,
    alive: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7),
    dead: tuple[int, ...] = (),
    legal: tuple[int, ...] | None = None,
    seer_checks: list[dict] | None = None,
    day: int = 1,
) -> Observation:
    all_players = _players(*alive, dead_seats=dead)
    living = [p for p in all_players if p.alive]
    legal_targets = [p for p in living if p.seat in legal] if legal is not None else living
    private: dict = {}
    if seer_checks:
        private["seer_checks"] = seer_checks
        private["seer_check"] = seer_checks[-1]
    return Observation(
        player_id=f"P{seat}",
        player_name=f"P{seat}",
        player_seat=seat,
        player_role=role,
        day=day,
        phase=phase,
        alive=living,
        dead=[p for p in all_players if not p.alive],
        legal_targets=legal_targets,
        private=private,
    )


def test_speech_catalog_has_six_actions_for_villager() -> None:
    catalog = ActionCatalog.for_turn(_obs("Villager"))
    assert catalog.ids() == ("baseline", "silence", "accusation", "support", "vote_intent", "seer_claim")
    assert catalog.require_speech is True
    rendered = catalog.render()
    assert "必须且只能选一项" in rendered
    for action_id in catalog.ids():
        assert f"- {action_id}" in rendered


def test_speech_legal_targets_follow_alive_others() -> None:
    catalog = ActionCatalog.for_turn(_obs("Villager", alive=(1, 2, 5, 6, 7), dead=(3, 4)))
    for action_id in ("accusation", "support", "vote_intent"):
        spec = catalog.get(action_id)
        assert spec is not None
        assert spec.legal_targets == (2, 5, 6, 7)


def test_honesty_rule_drops_seer_claim_for_non_seer() -> None:
    villager = ActionCatalog.for_turn(_obs("Villager"), honesty_rule=True)
    wolf = ActionCatalog.for_turn(_obs("Werewolf"), honesty_rule=True)
    seer = ActionCatalog.for_turn(
        _obs(
            "Seer",
            seer_checks=[{"target_id": "P2", "target_name": "P2", "is_wolf": True, "day": 1}],
        ),
        honesty_rule=True,
    )
    assert "seer_claim" not in villager.ids()
    assert "seer_claim" not in wolf.ids()
    assert "seer_claim" in seer.ids()


def test_seer_claim_true_check_only_enumerates_real_alive_results() -> None:
    catalog = ActionCatalog.for_turn(
        _obs(
            "Seer",
            seer_checks=[
                {"target_id": "P3", "target_name": "P3", "is_wolf": True, "day": 1},
                {"target_id": "P4", "target_name": "P4", "is_wolf": False, "day": 2},
            ],
        )
    )
    spec = catalog.get("seer_claim")
    assert spec is not None
    assert spec.claim_mode == CLAIM_TRUE
    assert spec.legal_targets == (3, 4)
    ok, err = validate_payload(
        {"action": "seer_claim", "claim_seat": 3, "claim_result": "wolf", "speech": "我是预言家，3号查杀。", "reasoning": "报真验"},
        catalog,
    )
    assert err == ""
    assert ok["claim_mode"] == CLAIM_TRUE
    _, bad = validate_payload(
        {"action": "seer_claim", "claim_seat": 3, "claim_result": "good", "speech": "我是预言家，3号金水。", "reasoning": "报反"},
        catalog,
    )
    assert "私有查验" in bad
    _, unseen = validate_payload(
        {"action": "seer_claim", "claim_seat": 5, "claim_result": "wolf", "speech": "我是预言家，5号查杀。", "reasoning": "编造"},
        catalog,
    )
    assert "不在合法目标" in unseen


def test_seer_claim_identity_when_seer_has_no_alive_checks() -> None:
    catalog = ActionCatalog.for_turn(_obs("Seer", alive=(1, 2, 3), dead=(4, 5, 6, 7)))
    spec = catalog.get("seer_claim")
    assert spec is not None
    assert spec.claim_mode == CLAIM_IDENTITY
    ok, err = validate_payload(
        {"action": "seer_claim", "speech": "我是预言家，验人已出局，先听票型。", "reasoning": "只跳"},
        catalog,
    )
    assert err == ""
    assert ok["claim_seat"] is None


def test_wolf_seer_claim_allows_any_alive_and_either_result() -> None:
    catalog = ActionCatalog.for_turn(_obs("Werewolf"))
    spec = catalog.get("seer_claim")
    assert spec is not None
    assert spec.claim_mode == CLAIM_FAKE
    assert spec.legal_targets == (2, 3, 4, 5, 6, 7)
    assert spec.claim_results == ("good", "wolf")
    ok, err = validate_payload(
        {
            "action": "seer_claim",
            "claim_seat": 2,
            "claim_result": "good",
            "speech": "我是预言家，2号金水。",
            "reasoning": "给队友金水",
        },
        catalog,
    )
    assert err == ""
    assert ok["claim_mode"] == CLAIM_FAKE


def test_identity_only_seer_claim_strips_check_fields() -> None:
    for role in ("Villager", "Witch", "Guard"):
        catalog = ActionCatalog.for_turn(_obs(role))
        spec = catalog.get("seer_claim")
        assert spec is not None
        assert spec.claim_mode == CLAIM_IDENTITY
        assert spec.params == ()
        ok, err = validate_payload(
            {
                "action": "seer_claim",
                "claim_seat": 3,
                "claim_result": "wolf",
                "speech": "我是预言家，先听发言。",
                "reasoning": "空跳却带验",
            },
            catalog,
        )
        assert err == ""
        assert ok["claim_seat"] is None
        assert ok["claim_result"] is None
        ok, ok_err = validate_payload(
            {"action": "seer_claim", "speech": "我是预言家，先听发言。", "reasoning": "只跳"},
            catalog,
        )
        assert ok_err == ""
        assert ok["claim_seat"] is None


def test_validate_rejects_multiple_actions_missing_speech_and_illegal_seat() -> None:
    catalog = ActionCatalog.for_turn(_obs("Villager"))
    _, missing = validate_payload({"reasoning": "x", "speech": "hello"}, catalog)
    assert "缺少 action" in missing
    _, illegal = validate_payload(
        {"action": "support", "target_seat": 9, "speech": "我挺9号。", "reasoning": "非法座位"},
        catalog,
    )
    assert "不在合法目标" in illegal
    _, no_speech = validate_payload({"action": "support", "target_seat": 2, "reasoning": "缺发言"}, catalog)
    assert "speech" in no_speech
    _, extra_target = validate_payload(
        {"action": "baseline", "target_seat": 2, "speech": "平铺发言即可。", "reasoning": "不该带目标"},
        catalog,
    )
    assert extra_target == ""


def test_speech_schema_strips_unused_claim_fields_on_baseline() -> None:
    catalog = ActionCatalog.for_turn(_obs("Werewolf", alive=(1, 2, 3), dead=(4, 5, 6, 7)))
    ok, err = validate_payload(
        {
            "action": "baseline",
            "target_seat": 2,
            "claim_seat": 3,
            "claim_result": "good",
            "speech": "场上三人，我先听票型。",
            "reasoning": "schema 把可选字段都填了",
        },
        catalog,
    )
    assert err == ""
    assert ok["action"] == "baseline"
    assert ok["target_seat"] is None
    assert ok["claim_seat"] is None


def test_truncated_json_and_action_alias_still_parse() -> None:
    from backend.agents.cognitive.action_catalog import extract_json_object

    catalog = ActionCatalog.for_turn(_obs("Villager", alive=(1, 2, 3)))
    fragment = '{"action": "accuse", "target_seat": 2, "speech": "2号不像好人。", "reasoning": "残局点人"'
    payload = extract_json_object(fragment)
    assert payload is not None
    ok, err = validate_payload(payload, catalog)
    assert err == ""
    assert ok["action"] == "accusation"
    assert ok["target_seat"] == 2


class BrokenThenEmptyLLM:
    def invoke(self, messages, **kwargs):
        return AIMessage(content="不是合法 JSON，也没有座位。")

    def bind_tools(self, tool_schemas: list[dict]) -> "BrokenThenEmptyLLM":
        return self


class SupportSpeechLLM:
    def invoke(self, messages, **kwargs):
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "call_support",
                    "name": "choose_action",
                    "args": {
                        "action": "support",
                        "target_seat": 3,
                        "speech": "我同意3号的判断，今天先跟3号。",
                        "reasoning": "一轮同时给出 support 和原文",
                    },
                }
            ],
        )


class InvalidThenSupportLLM:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(content='{"action": "support", "target_seat": 9, "speech": "挺9。", "reasoning": "非法"}')
        return AIMessage(
            content='{"action": "support", "target_seat": 3, "speech": "我同意3号的判断，今天先跟3号。", "reasoning": "repair support"}'
        )


def _talk_view() -> PlayerView:
    return PlayerView(
        player_id="P1",
        day=1,
        phase="DAY_SPEECH",
        self_player={"id": "P1", "name": "Alice", "seat": 1, "role": "Villager", "alive": True},
        players=[
            {"id": "P1", "name": "Alice", "seat": 1, "role": "Villager", "alive": True},
            {"id": "P2", "name": "Bob", "seat": 2, "alive": True},
            {"id": "P3", "name": "Carol", "seat": 3, "alive": True},
        ],
        public_events=[],
        private_events=[],
        known_wolves=[],
        observations=[],
    )


def test_fake_llm_one_shot_support_includes_speech() -> None:
    view = _talk_view()
    agent = create_cognitive_agent_with_character(
        player_id="P1",
        role="Villager",
        llm=SupportSpeechLLM(),
        player_name="Alice",
        player_seat=1,
        character=None,
    )
    agent.initialize(view, {})
    agent.update(view, "TALK")
    decision = agent.talk()
    assert decision.action_type == ActionType.TALK
    assert decision.speech == "我同意3号的判断，今天先跟3号。"
    assert decision.metadata["speech_action"] == "support"
    assert decision.metadata["target_seat"] == 3
    assert decision.reasoning == "一轮同时给出 support 和原文"


def test_structured_speech_repairs_illegal_seat_once() -> None:
    view = _talk_view()
    llm = InvalidThenSupportLLM()
    agent = create_cognitive_agent_with_character(
        player_id="P1",
        role="Villager",
        llm=llm,
        player_name="Alice",
        player_seat=1,
        character=None,
    )
    agent.initialize(view, {})
    agent.update(view, "TALK")
    decision = agent.talk()
    assert decision.metadata["speech_action"] == "support"
    assert decision.metadata["target_seat"] == 3
    assert llm.calls == 2


def test_unparseable_speech_falls_back_to_silence() -> None:
    view = _talk_view()
    agent = create_cognitive_agent_with_character(
        player_id="P1",
        role="Villager",
        llm=BrokenThenEmptyLLM(),
        player_name="Alice",
        player_seat=1,
        character=None,
    )
    agent.initialize(view, {})
    agent.update(view, "TALK")
    decision = agent.talk()
    assert decision.action_type == ActionType.TALK
    assert decision.speech
    assert decision.metadata["speech_action"] in {"silence", "baseline"}


def test_wolf_night_catalog_respects_board_options() -> None:
    obs = _obs("Werewolf", "NIGHT_WOLF_ACTION", legal=(2, 3, 4))
    closed = ActionCatalog.for_turn(obs, wolf_night_options=set())
    assert closed.ids() == ("attack",)
    opened = ActionCatalog.for_turn(obs, wolf_night_options={"self", "empty"})
    assert opened.ids() == ("attack", "self_attack", "skip")


def test_witch_catalog_filters_potions() -> None:
    obs = _obs("Witch", "NIGHT_WITCH_ACTION", seat=1)
    both = ActionCatalog.for_turn(obs, witch_victim_id="P2")
    assert both.ids() == ("save", "poison", "skip")
    used_save = ActionCatalog.for_turn(obs, witch_victim_id="P2", witch_save_used=True)
    assert used_save.ids() == ("poison", "skip")
    no_victim = ActionCatalog.for_turn(obs)
    assert no_victim.ids() == ("poison", "skip")
    night2_self = ActionCatalog.for_turn(
        _obs("Witch", "NIGHT_WITCH_ACTION", day=2),
        witch_victim_id="P1",
    )
    assert "save" not in night2_self.ids()


def test_day_vote_is_exclusive_and_cannot_abstain() -> None:
    catalog = ActionCatalog.for_turn(_obs("Villager", "DAY_VOTE"))
    assert catalog.ids() == ("day_vote",)
    _, err = validate_payload({"action": "day_vote", "reasoning": "弃票"}, catalog)
    assert "target_seat" in err


def test_legacy_speech_without_action_maps_to_baseline() -> None:
    catalog = ActionCatalog.for_turn(_obs("Villager"))
    adapted = coerce_legacy_payload({"speech": "今天先听发言。", "reasoning": "旧格式"}, catalog, _obs("Villager"))
    normalized, error = validate_payload(adapted, catalog)
    assert error == ""
    assert normalized["action"] == "baseline"


def test_emit_speech_copies_speech_action_onto_chat_event() -> None:
    from backend.engine.game import WerewolfGame
    from backend.engine.models import Decision
    from backend.engine.models import Player
    from backend.engine.models import Role
    from backend.engine.models import Alignment

    player = Player(id="P1", seat=1, name="Alice", role=Role.VILLAGER, alignment=Alignment.VILLAGE)
    game = WerewolfGame(players=[player], agents={"P1": object()}, seed=1)
    game._emit_speech(
        player,
        Decision(
            actor_id="P1",
            action_type=ActionType.TALK,
            speech="我同意3号。",
            reasoning="support",
            metadata={"speech_action": "support", "target_seat": 3, "segments": ["我同意3号。"]},
        ),
        {},
    )
    chats = [event for event in game.state.events if event.type == EventType.CHAT_MESSAGE]
    assert chats
    assert chats[0].payload["speech_action"] == "support"
    assert chats[0].payload["target_seat"] == 3
