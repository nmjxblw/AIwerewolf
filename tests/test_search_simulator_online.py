"""search_simulator 在线区间极大极小决策算法测试。"""

from __future__ import annotations

import pytest

from search_simulator import RewardInterval, SearchSimulator
from search_simulator._game_state import GameState
from search_simulator._interval import UNRESOLVED, compare, merge
from search_simulator._minimax import evaluate
from search_simulator._player import Player
from search_simulator._zero_sum import Camp, camp_of_role, terminal_utility


# ---------- _interval ----------


def test_interval_clamp_and_order():
    iv = RewardInterval(1.5, -2.0)
    assert iv.lower == -1.0
    assert iv.upper == 1.0


def test_merge_optimistic_is_union():
    v1 = RewardInterval(-0.2, 0.8)
    v2 = RewardInterval(-0.5, 0.5)
    merged = merge([v1, v2], toggle="optimistic", lambda_risk=1.0)
    assert (merged.lower, merged.upper) == (-0.5, 0.8)


def test_merge_conservative_is_intersection():
    v1 = RewardInterval(-0.2, 0.8)
    v2 = RewardInterval(-0.5, 0.5)
    merged = merge([v1, v2], toggle="conservative", lambda_risk=1.0)
    assert (merged.lower, merged.upper) == (-0.2, 0.5)


def test_merge_lambda_zero_collapses_to_mean():
    v1 = RewardInterval(-0.2, 0.8)
    v2 = RewardInterval(-0.5, 0.5)
    merged = merge([v1, v2], toggle="optimistic", lambda_risk=0.0)
    assert merged.lower == pytest.approx(-0.35)
    assert merged.upper == pytest.approx(0.65)


def test_compare_toggle():
    a = RewardInterval(-0.5, 0.8)
    b = RewardInterval(-0.2, 0.5)
    assert compare(a, b, "optimistic") == 1  # 0.8 > 0.5
    assert compare(a, b, "conservative") == -1  # -0.5 < -0.2


# ---------- _zero_sum ----------


def test_camp_of_role():
    assert camp_of_role("狼人") is Camp.WOLF
    assert camp_of_role("白狼王") is Camp.WOLF
    assert camp_of_role("村民") is Camp.GOOD
    assert camp_of_role("预言家") is Camp.GOOD


def test_terminal_utility_mapping():
    assert terminal_utility("好人阵营胜利") == 1.0
    assert terminal_utility("狼人阵营胜利（人数过半）") == -1.0
    assert terminal_utility("狼人阵营胜利（神职角色已被消灭）") == -1.0
    assert terminal_utility("狼人阵营胜利（村民已被消灭）") == -1.0


# ---------- _game_state ----------


def test_game_state_roundtrip():
    state = GameState(
        players=[
            Player(role="狼人", is_alive=True, skills={"攻击": -1}),
            Player(role="预言家", is_alive=False, skills={"查验": -1}),
        ],
        phase="day",
        night_count=1,
        day_count=2,
        last_guard_target_index=0,
        seer_check_results={0: True, 1: False},
    )
    restored = GameState.from_dict(state.to_dict())
    assert restored.phase == "day"
    assert restored.night_count == 1
    assert restored.day_count == 2
    assert restored.last_guard_target_index == 0
    assert restored.seer_check_results == {0: True, 1: False}
    assert [p.role for p in restored.players] == ["狼人", "预言家"]
    assert restored.players[0].is_alive is True
    assert restored.players[1].is_alive is False
    assert restored.parent_state_id is None
    assert restored.depth == 0


def test_transition_flips_phase_and_counts_day():
    sim = SearchSimulator(number_of_players=5, number_of_wolves=1)
    root = sim.initial_state
    assert root.phase == "night"
    day_children = sim.transition(root)
    assert day_children
    assert all(c.phase == "day" for c in day_children)
    night_children = sim.transition(day_children[0])
    assert all(c.phase == "night" for c in night_children)
    assert all(c.day_count == day_children[0].day_count + 1 for c in night_children)


# ---------- tactics ----------


def test_tactic_self_kill_expands_targets():
    sim = SearchSimulator(
        number_of_players=5,
        number_of_wolves=2,
        include_witch=True,
        tactics="self_kill",
    )
    root = sim.initial_state
    targets = sim._wolf_targets_for_night(root)
    assert set(targets) == set(sim._alive_indices(root))


def test_no_self_kill_targets_exclude_wolves():
    sim = SearchSimulator(number_of_players=5, number_of_wolves=2, include_witch=True)
    root = sim.initial_state
    wolves = {i for i, p in enumerate(root.players) if sim._is_wolf_role(p.role)}
    targets = sim._wolf_targets_for_night(root)
    assert wolves.isdisjoint(targets)


def test_no_kill_branch():
    sim = SearchSimulator(number_of_players=5, number_of_wolves=1, tactics="no_kill")
    children = sim._resolve_night(sim.initial_state)
    assert any("空刀" in c.action_label for c in children)


# ---------- _minimax ----------


def test_evaluate_frontier_is_unresolved():
    sim = SearchSimulator(number_of_players=5, number_of_wolves=1)
    iv = evaluate(
        sim.initial_state,
        depth=0,
        oracle=sim,
        toggle="conservative",
        lambda_risk=1.0,
    )
    assert iv == UNRESOLVED


def test_evaluate_full_depth_terminates():
    sim = SearchSimulator(number_of_players=4, number_of_wolves=1)
    iv = evaluate(
        sim.initial_state,
        depth=None,
        oracle=sim,
        toggle="conservative",
        lambda_risk=1.0,
    )
    assert -1.0 <= iv.lower <= iv.upper <= 1.0


# ---------- online policy ----------


def test_run_online_reference(tmp_path):
    sim = SearchSimulator(
        number_of_players=5,
        number_of_wolves=1,
        policy="online",
        lookahead_depth=2,
        online_trace_path=str(tmp_path / "trace.json"),
    )
    trace = sim.run_online()
    assert trace["outcome"] in {
        "好人阵营胜利",
        "狼人阵营胜利（人数过半）",
        "狼人阵营胜利（村民已被消灭）",
        "未决",
    }
    assert len(trace["steps"]) >= 1
    for step in trace["steps"]:
        assert "optimistic_interval" in step
        assert "conservative_interval" in step
        lo, hi = step["chosen_interval"]
        assert -1.0 <= lo <= hi <= 1.0


def test_run_online_from_custom_state(tmp_path):
    state = GameState(
        players=[
            Player(role="狼人", is_alive=True, skills={"攻击": -1}),
            Player(role="村民", is_alive=True, skills={}),
            Player(role="村民", is_alive=True, skills={}),
        ],
        phase="day",
    )
    sim = SearchSimulator(
        number_of_players=3,
        number_of_wolves=1,
        policy="online",
        lookahead_depth=2,
        online_trace_path=str(tmp_path / "t.json"),
    )
    trace = sim.run_online(start_state=state)
    assert trace["steps"]
    assert trace["steps"][0]["phase"] == "day"
