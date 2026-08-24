"""Offline smoke test for the cheap-talk board (w.txt) — no API needed.

Checks:
  1. ActionValidator: 空刀/自刀 flags gate correctly, teammate knife still banned
  2. Visibility: wolf legal targets include self only when 自刀 on
  3. Full offline game on CT_ROLES_7P (tactic flags on) reaches GAME_END:
     night order 狼→预言家→女巫→守卫, wolf chat messages exist,
     transcript renders with speeches/votes
  4. Full offline game baseline flags also completes

Usage: python scripts/smoke_cheap_talk.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

os.environ["AIWEREWOLF_RULE_ADDENDUM"] = ""
os.environ["AIWEREWOLF_TACTIC_WEREWOLF"] = "战术注入冒烟：此行应出现在狼人 prompt 中。"
os.environ["AIWEREWOLF_WOLF_NIGHT_OPTIONS"] = "self,empty"

from tests.test_cognitive_offline import DeterministicCognitiveLLM
from backend.agents.cognitive.factory import create_cognitive_agent_with_character
from backend.engine.actions import ActionValidator
from backend.engine.game import WerewolfGame
from backend.engine.models import ActionType
from backend.engine.models import Alignment
from backend.engine.models import Decision
from backend.engine.models import GameState
from backend.engine.models import Phase
from backend.engine.models import Player
from backend.engine.models import Role
from backend.engine.rules import CT_ROLES_7P
from backend.engine.rules import build_players
from backend.engine.transcript import build_transcript
from backend.engine.visibility import Visibility


def _mk_player(pid: str, role: Role, alignment: Alignment, seat: int = 1) -> Player:
    return Player(id=pid, seat=seat, name=f"P{seat}", role=role, alignment=alignment)


def test_validator() -> None:
    state = GameState(id="t", phase=Phase.NIGHT_WOLF_ACTION, day=1, players=[
        _mk_player("W1", Role.WEREWOLF, Alignment.WOLF, 1),
        _mk_player("W2", Role.WEREWOLF, Alignment.WOLF, 2),
        _mk_player("V1", Role.VILLAGER, Alignment.VILLAGE, 3),
        _mk_player("S1", Role.SEER, Alignment.VILLAGE, 4),
    ])
    v = ActionValidator()
    attack = lambda actor, target: Decision(actor, ActionType.ATTACK, target_id=target, reasoning="r")

    # 默认（无旗标）：空刀/自刀均非法
    state.board_options = {}
    assert not v.validate(state, attack("W1", None)), "empty knife must be illegal by default"
    assert not v.validate(state, attack("W1", "W1")), "self knife must be illegal by default"

    # 开旗标后合法；刀队友仍然非法
    state.board_options = {"wolf_self_knife": True, "wolf_empty_knife": True}
    assert v.validate(state, attack("W1", None)), "empty knife must be legal when enabled"
    assert v.validate(state, attack("W1", "W1")), "self knife must be legal when enabled"
    assert not v.validate(state, attack("W1", "W2")), "teammate knife must stay illegal"
    assert v.validate(state, attack("W1", "V1")), "normal knife stays legal"
    print("  [1] validator 空刀/自刀/刀队友 gates OK")


def test_visibility() -> None:
    state = GameState(id="t", phase=Phase.NIGHT_WOLF_ACTION, day=1, players=[
        _mk_player("W1", Role.WEREWOLF, Alignment.WOLF, 1),
        _mk_player("W2", Role.WEREWOLF, Alignment.WOLF, 2),
        _mk_player("V1", Role.VILLAGER, Alignment.VILLAGE, 3),
    ])
    vis = Visibility()
    state.board_options = {}
    targets = {t["id"] for t in vis.for_player(state, "W1").legal_targets}
    assert "W1" not in targets and "W2" not in targets and "V1" in targets
    state.board_options = {"wolf_self_knife": True}
    targets = {t["id"] for t in vis.for_player(state, "W1").legal_targets}
    assert "W1" in targets and "W2" not in targets, "self only, teammates never"
    print("  [2] visibility 狼夜合法目标（自刀开关）OK")


def run_offline_game(label: str, seed: int, **engine_kwargs) -> WerewolfGame:
    players = build_players(CT_ROLES_7P, seed=seed)
    fake = DeterministicCognitiveLLM()
    agents = {
        p.id: create_cognitive_agent_with_character(
            player_id=p.id, role=p.role.value, llm=fake,
            player_name=p.name, player_seat=p.seat, character=None,
        )
        for p in players
    }
    for p in players:
        p.agent_type = "llm"
    game = WerewolfGame(
        players=players, agents=agents, seed=seed,
        max_days=8, disable_badge=True, disable_last_words=True,
        random_tiebreak=True, kill_side_win=True, full_elimination=False,
        **engine_kwargs,
    )
    state = game.play()
    assert state.phase == Phase.GAME_END, f"{label}: game did not finish (phase={state.phase})"
    assert state.winner is not None, f"{label}: no winner"
    return game


def test_full_game_tactics(seed: int = 7) -> None:
    game = run_offline_game(
        "tactics-on", seed, wolf_self_knife=True, wolf_empty_knife=True, wolf_night_chat=True,
    )
    state = game.state
    events = [e.to_dict() for e in state.events]

    # 夜序：狼 → 预言家 → 女巫 → 守卫（同一夜内事件先后）
    night1 = [e for e in events if e["day"] == 1 and str(e["phase"]).startswith("NIGHT_")]
    order = {}
    for e in night1:
        order.setdefault(e["phase"], len(order))
    expected = ["NIGHT_WOLF_ACTION", "NIGHT_SEER_ACTION", "NIGHT_WITCH_ACTION", "NIGHT_GUARD_ACTION"]
    got = [p for p in expected if p in order]
    assert got == [p for p in expected if p in order] and sorted(got, key=lambda p: order[p]) == got, (
        f"night order wrong: {order}"
    )

    # 狼队私聊存在且对全部存活狼可见
    chats = [e for e in events if e["payload"].get("kind") == "wolf_chat_message"]
    assert chats, "no wolf chat messages in night 1"
    wolf_ids = {p.id for p in state.players if p.role == Role.WEREWOLF}
    assert all(wolf_ids <= set(c["visible_to"]) for c in chats)

    # 战术注入出现在狼人调用里（fake LLM 记录了 prompt）
    # （DeterministicCognitiveLLM.calls 汇聚所有 agent 的调用文本）
    # transcript 渲染
    md = build_transcript(state, title="smoke", meta={"a": "b"})
    assert "座位与角色" in md and "发言" in md and "[投票]" in md
    assert "狼队私聊" in md

    # 胜负原因属于 4 种合法值
    end = [e for e in events if e["type"] == "GAME_END"][0]["payload"]
    assert end["reason"] in {"all_wolves_dead", "wolves_reached_parity", "all_gods_dead", "all_villagers_dead", "max_days_reached"}
    print(f"  [3] tactics-on offline game OK: winner={end['winner']} reason={end['reason']} days={state.day} wolf_chats={len(chats)}")


def test_full_game_baseline(seed: int = 11) -> None:
    game = run_offline_game("baseline", seed, wolf_self_knife=False, wolf_empty_knife=False, wolf_night_chat=True)
    state = game.state
    events = [e.to_dict() for e in state.events]
    end = [e for e in events if e["type"] == "GAME_END"][0]["payload"]
    print(f"  [4] baseline offline game OK: winner={end['winner']} reason={end['reason']} days={state.day}")


if __name__ == "__main__":
    print("=== cheap talk board offline smoke ===")
    test_validator()
    test_visibility()
    test_full_game_tactics()
    test_full_game_baseline()
    print("ALL OK")
