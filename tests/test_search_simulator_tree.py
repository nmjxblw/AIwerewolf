from __future__ import annotations

import ast
import ctypes
import json
import logging
import multiprocessing
import os
import queue
import re
import subprocess
import sys
import textwrap
import threading
from concurrent.futures.process import BrokenProcessPool
from itertools import product
from pathlib import Path

import pytest

from search_simulator import GameState
from search_simulator import SearchSimulator
from search_simulator._config import build_parser
from search_simulator._crash_handler import mark_crash_log_reported
from search_simulator._crash_handler import prepare_crash_log_path
from search_simulator._crash_handler import previous_unreported_crash_log
from search_simulator._crash_handler import record_caught_failure
from search_simulator._game_state import GameState as GameStateContract
from search_simulator._gui import UI_DATA_REFRESH_SECONDS
from search_simulator._gui import PygameSimulatorUI
from search_simulator._gui import _terminal_popup_content
from search_simulator._interval import RewardInterval
from search_simulator._interval import RobustIntervals
from search_simulator._interval import interval_branch_color
from search_simulator._interval import interval_camp
from search_simulator._interval import propagate_interval_values
from search_simulator._interval import propagate_intervals
from search_simulator._memory_guard import _GLOBAL_MEMORY_STATUS_EX
from search_simulator._memory_guard import MemorySnapshot
from search_simulator._memory_guard import _WindowsMemoryStatusEx
from search_simulator._memory_guard import memory_pressure_snapshot
from search_simulator._positions import build_role_roster
from search_simulator._positions import enumerate_position_layouts
from search_simulator._positions import players_for_layout
from search_simulator._reporting import report_tree_summary
from search_simulator._reporting import save_tree_results
from search_simulator._sqlite_lru_signature_store import _SQLiteLRUSignatureStore
from search_simulator._strategy import enumerate_day_tactic_profiles
from search_simulator._strategy import enumerate_night_tactic_profiles
from search_simulator._tree_search import PREVIEW_EDGE_BATCH_LIMIT
from search_simulator._tree_search import PREVIEW_EMIT_INTERVAL_SECONDS
from search_simulator._tree_search import PREVIEW_NODE_BATCH_LIMIT
from search_simulator._tree_search import _isolated_compute_worker_spawn
from search_simulator._tree_search import _remove_search_checkpoint
from search_simulator._tree_search import recompute_graph_intervals


@pytest.fixture
def standard_roster() -> tuple[str, ...]:
    return build_role_roster(
        number_of_players=7,
        number_of_wolves=2,
        include_seer=True,
        include_witch=True,
        include_guard=True,
        include_hunter=False,
        include_idiot=False,
        include_white_werewolf_king=False,
    )


def test_standard_board_has_1260_unique_positions(
    standard_roster: tuple[str, ...],
) -> None:
    assert standard_roster.count("狼人") == 2
    assert standard_roster.count("村民") == 2
    assert {"预言家", "女巫", "守卫"} <= set(standard_roster)
    layouts = enumerate_position_layouts(standard_roster)
    assert len(layouts) == 1260
    assert len({layout.roles for layout in layouts}) == 1260
    assert len({layout.signature for layout in layouts}) == 1260


def test_game_state_round_trip_preserves_continuation_contract(
    standard_roster: tuple[str, ...],
) -> None:
    layout = enumerate_position_layouts(standard_roster)[17]
    state = GameState(
        players=players_for_layout(layout),
        phase="day",
        night_count=2,
        day_count=1,
        last_guard_target_index=3,
        seer_check_results={1: True, 5: False},
        seer_revealed=True,
        revealed_good_indices=(0, 5),
        revealed_wolf_indices=(1,),
        public_role_claims={0: "预言家"},
        idiot_revealed_indices=(),
        wolf_priority_targets=(5,),
        last_day_votes={0: 1},
        last_day_strategy="seer=reveal",
        position_signature=layout.signature,
    )
    restored = GameState.from_dict(json.loads(json.dumps(state.to_dict())))
    assert restored.to_dict() == state.to_dict()


def test_state_signature_streams_compact_key_deterministically() -> None:
    simulator = SearchSimulator(
        number_of_players=3,
        number_of_wolves=1,
        include_seer=False,
        include_witch=False,
        include_guard=False,
        tactics="",
        persistence_enabled=False,
    )
    state = simulator.initial_state
    key = simulator._state_key(state)
    signature = simulator._state_signature_from_key(
        state.position_signature,
        key,
    )
    assert len(signature) == 32
    assert int(signature, 16) >= 0
    assert signature == simulator._state_signature(state.clone())

    changed = state.clone()
    changed.day_count += 1
    assert simulator._state_signature(changed) != signature


def test_state_signature_survives_one_million_hot_calls() -> None:
    """回归 Windows CPython 3.12 递归闭包热路径的对象错位与访问冲突。"""

    code = textwrap.dedent(
        """
        from search_simulator._simulator import SearchSimulator

        simulator = SearchSimulator(
            number_of_players=3,
            number_of_wolves=1,
            include_seer=False,
            include_witch=False,
            include_guard=False,
            tactics="",
            persistence_enabled=False,
        )
        state = simulator.initial_state
        key = simulator._state_key(state)
        expected = simulator._state_signature_from_key(state.position_signature, key)
        actual = expected
        for _iteration in range(1_000_000):
            actual = simulator._state_signature_from_key(state.position_signature, key)
        assert actual == expected
        print("SIGNATURE_STRESS_OK")
        """
    )
    environment = os.environ.copy()
    environment["PYTHONMALLOC"] = "malloc"
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "SIGNATURE_STRESS_OK" in completed.stdout


def test_all_generated_transitions_preserve_player_contract() -> None:
    """昼夜战术转移不得把视图对象或字典迭代器写入玩家列表。"""

    code = textwrap.dedent(
        """
        from search_simulator._positions import enumerate_position_layouts
        from search_simulator._simulator import SearchSimulator

        simulator = SearchSimulator(
            number_of_players=7,
            number_of_wolves=2,
            include_seer=True,
            include_witch=True,
            include_guard=True,
            smart_vote=True,
            persistence_enabled=False,
        )
        layout = enumerate_position_layouts(simulator.roster)[0]
        first_day_state = None
        night_count = 0
        for transition in simulator.expand_state(
            simulator.initial_state_for_layout(layout)
        ):
            simulator._state_key(transition.state)
            night_count += 1
            if first_day_state is None:
                first_day_state = transition.state
        assert first_day_state is not None
        day_count = 0
        for transition in simulator.expand_state(first_day_state):
            simulator._state_key(transition.state)
            day_count += 1
        assert night_count > 0 and day_count > 0
        print("TRANSITION_CONTRACT_OK")
        """
    )
    environment = os.environ.copy()
    environment["PYTHONMALLOC"] = "malloc"
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "TRANSITION_CONTRACT_OK" in completed.stdout


def test_day_tactics_enumerate_requested_combinations(
    standard_roster: tuple[str, ...],
) -> None:
    layout = enumerate_position_layouts(standard_roster)[0]
    state = GameState(
        players=players_for_layout(layout),
        phase="day",
        position_signature=layout.signature,
        seer_check_results={4: True},
    )
    profiles = enumerate_day_tactic_profiles(state)
    villagers = {index for index, player in enumerate(state.players) if player.role == "村民"}
    seer = next(index for index, player in enumerate(state.players) if player.role == "预言家")
    non_wolves = {index for index, player in enumerate(state.players) if player.role not in {"狼人", "白狼王"}}

    assert {profile.seer_action for profile in profiles} == {"hide", "reveal"}
    assert any(set(profile.decoy_indices) == villagers for profile in profiles)
    assert {profile.wolf_vote_target for profile in profiles if profile.wolf_vote_mode == "bloc"} == non_wolves
    assert any(profile.seer_action == "reveal" and profile.next_night_target == seer for profile in profiles)
    for decoy in villagers:
        assert any(decoy in profile.decoy_indices and profile.next_night_target == decoy for profile in profiles)
    assert all(profile.seer_action != "wolf_fake_seer" for profile in profiles)


def test_idiot_roster_and_day_reveal_rule() -> None:
    roster = build_role_roster(
        number_of_players=7,
        number_of_wolves=2,
        include_seer=True,
        include_witch=True,
        include_guard=True,
        include_hunter=False,
        include_idiot=True,
        include_white_werewolf_king=False,
    )
    assert roster.count("愚者") == 1
    assert roster.count("村民") == 1
    assert len(enumerate_position_layouts(roster)) == 2520

    simulator = SearchSimulator(
        number_of_players=3,
        number_of_wolves=1,
        include_seer=False,
        include_witch=False,
        include_guard=False,
        include_hunter=False,
        include_idiot=True,
        include_white_werewolf_king=False,
        smart_vote=True,
        tactics="",
        persistence_enabled=False,
    )
    roles = ("狼人", "愚者", "村民")
    state = GameState(players=players_for_layout(roles), phase="day")
    reveal = next(
        transition
        for transition in simulator.expand_state(state)
        if transition.action_key[-1] == "idiot_reveal"
    )
    assert reveal.state.players[1].is_alive
    assert reveal.state.idiot_revealed_indices == (1,)
    assert reveal.state.players[1].skills["身份揭示"] == 0
    profile = enumerate_day_tactic_profiles(reveal.state, tactics=frozenset())[0]
    targets = simulator._allowed_vote_targets(reveal.state, 0, profile, [0, 1, 2])
    assert 1 not in targets


def test_night_tactics_require_protection_role_and_two_wolves_for_self_kill() -> None:
    protected = GameState(
        players=players_for_layout(("狼人", "狼人", "女巫", "村民")),
        phase="night",
    )
    modes = {profile.mode for profile in enumerate_night_tactic_profiles(protected)}
    assert modes == {"normal", "self_kill", "no_kill"}
    assert len([profile for profile in enumerate_night_tactic_profiles(protected) if profile.mode == "self_kill"]) == 2

    unprotected = GameState(
        players=players_for_layout(("狼人", "狼人", "村民", "村民")),
        phase="night",
    )
    assert {profile.mode for profile in enumerate_night_tactic_profiles(unprotected)} == {"normal"}


def test_vote_dynamic_program_preserves_assignment_multiplicity() -> None:
    alive = [0, 1, 2]
    allowed = {0: [1, 2], 1: [0, 2], 2: [0, 1]}
    actual = SearchSimulator._vote_outcome_multiplicities(alive, alive, allowed)

    expected = dict.fromkeys(alive, 0)
    for assignment in product(*(allowed[voter] for voter in alive)):
        counts = [assignment.count(target) for target in alive]
        highest = max(counts)
        for target, count in enumerate(counts):
            if count == highest:
                expected[target] += 1
    assert actual == expected


def _small_result(mode: str) -> dict:
    simulator = SearchSimulator(
        number_of_players=4,
        number_of_wolves=1,
        include_seer=False,
        include_witch=False,
        include_guard=False,
        tactics="",
        search_mode=mode,
        persistence_enabled=False,
    )
    return simulator.run(start_state=simulator.initial_state)


def test_bfs_and_dfs_build_equivalent_complete_graphs() -> None:
    bfs = _small_result("bfs")
    dfs = _small_result("dfs")
    comparable = (
        "state_count",
        "edge_count",
        "terminal_count",
        "good_paths",
        "wolf_paths",
        "wide_interval",
        "narrow_interval",
        "camp",
    )
    assert {key: bfs[key] for key in comparable} == {key: dfs[key] for key in comparable}
    assert bfs["good_paths"] + bfs["wolf_paths"] > bfs["terminal_count"]
    assert all(edge["multiplicity"] >= 1 for edge in bfs["edges"])


def test_continue_from_game_state_dict_uses_same_tree_contract() -> None:
    simulator = SearchSimulator(
        number_of_players=3,
        number_of_wolves=1,
        include_seer=False,
        include_witch=False,
        include_guard=False,
        tactics="",
        persistence_enabled=False,
    )
    direct = simulator.run(start_state=simulator.initial_state)
    continued = simulator.continue_from_game_state(simulator.initial_state.to_dict())
    for key in ("state_count", "edge_count", "good_paths", "wolf_paths"):
        assert continued[key] == direct[key]


def test_node_progress_pause_and_resume_preserve_search_frontier() -> None:
    progress_events: queue.Queue[dict] = queue.Queue()
    resume_event = threading.Event()
    simulator = SearchSimulator(
        number_of_players=3,
        number_of_wolves=1,
        include_seer=False,
        include_witch=False,
        include_guard=False,
        tactics="",
        persistence_enabled=False,
        progress_queue=progress_events,
        resume_event=resume_event,
    )
    result_holder: dict[str, dict] = {}

    worker = threading.Thread(
        target=lambda: result_holder.setdefault(
            "result", simulator.run(start_state=simulator.initial_state)
        )
    )
    worker.start()
    started = progress_events.get(timeout=1.0)
    assert started["kind"] == "position_started"
    assert started["processed_states"] == 0
    assert started["discovered_states"] == 1
    assert started["roles"] == ["狼人", "村民", "村民"]
    assert [node["node_id"] for node in started["preview_nodes"]] == [0]
    preview_players = started["preview_nodes"][0]["state"]["players"]
    assert [player["role"] for player in preview_players] == ["狼人", "村民", "村民"]
    assert all(player["is_alive"] for player in preview_players)
    assert all("skills" in player for player in preview_players)
    assert started["preview_edges"] == []
    assert worker.is_alive()

    resume_event.set()
    worker.join(timeout=3.0)
    assert not worker.is_alive()
    remaining_events = []
    while not progress_events.empty():
        remaining_events.append(progress_events.get_nowait())
    search_events = [
        event
        for event in [started, *remaining_events]
        if event["kind"] in {"position_started", "node_progress"}
    ]
    final_search_progress = search_events[-1]
    final_progress = remaining_events[-1]
    result = result_holder["result"]
    assert final_search_progress["kind"] == "node_progress"
    assert final_search_progress["processed_states"] == result["processed_states"]
    assert final_search_progress["discovered_states"] == result["state_count"]
    assert final_search_progress["edge_count"] == result["edge_count"]
    assert final_search_progress["frontier_size"] == 0
    assert final_search_progress["preview_nodes"]
    assert final_search_progress["preview_edges"]
    assert any(event["kind"] == "path_progress" for event in remaining_events)
    assert any(event["kind"] == "interval_progress" for event in remaining_events)
    assert final_progress["kind"] == "interval_progress"
    assert final_progress["postprocess_stage"] == "edge_intervals"
    assert final_progress["postprocess_completed"] == final_progress["postprocess_total"]
    for event in search_events:
        assert len(event["preview_nodes"]) <= PREVIEW_NODE_BATCH_LIMIT
        assert len(event["preview_edges"]) <= PREVIEW_EDGE_BATCH_LIMIT


def test_progress_and_ui_data_refresh_are_batched_every_half_second() -> None:
    assert PREVIEW_EMIT_INTERVAL_SECONDS == pytest.approx(0.5)
    assert UI_DATA_REFRESH_SECONDS == pytest.approx(0.5)


def test_visible_dag_expands_one_parent_at_a_time_and_preserves_shared_paths() -> None:
    graph = {
        "nodes": [
            {"node_id": node_id}
            for node_id in range(4)
        ],
        "edges": [
            {"parent_id": 0, "child_id": 1},
            {"parent_id": 0, "child_id": 2},
            {"parent_id": 1, "child_id": 3},
            {"parent_id": 2, "child_id": 3},
        ],
        "_expanded_node_ids": set(),
    }
    assert [node["node_id"] for node in PygameSimulatorUI._visible_graph(graph)["nodes"]] == [0]
    graph["_expanded_node_ids"].add(0)
    assert {node["node_id"] for node in PygameSimulatorUI._visible_graph(graph)["nodes"]} == {0, 1, 2}
    graph["_expanded_node_ids"].update({1, 2})
    assert {node["node_id"] for node in PygameSimulatorUI._visible_graph(graph)["nodes"]} == {0, 1, 2, 3}
    graph["_expanded_node_ids"].remove(1)
    assert 3 in {node["node_id"] for node in PygameSimulatorUI._visible_graph(graph)["nodes"]}

    ui = PygameSimulatorUI.__new__(PygameSimulatorUI)
    ui.running = False
    ui.graph = graph
    ui.live_graphs = {}
    ui.preview_position = 0
    ui.selected_node = None
    graph["_expanded_node_ids"].clear()
    ui._set_all_nodes_expanded(expanded=True)
    assert {node["node_id"] for node in ui._visible_graph(graph)["nodes"]} == {0, 1, 2, 3}
    assert all("_visible_since" in node for node in graph["nodes"][1:])
    ui._set_all_nodes_expanded(expanded=False)
    assert [node["node_id"] for node in ui._visible_graph(graph)["nodes"]] == [0]


def test_locate_root_resets_local_pan_without_changing_expansion() -> None:
    graph = {
        "nodes": [
            {"node_id": 0, "day_count": 0, "night_count": 0},
            {"node_id": 1, "day_count": 1, "night_count": 0},
            {"node_id": 2, "day_count": 0, "night_count": 0},
        ],
        "edges": [{"parent_id": 0, "child_id": 1}],
        "_expanded_node_ids": {0},
    }
    ui = PygameSimulatorUI.__new__(PygameSimulatorUI)
    ui.running = False
    ui.graph = graph
    ui.live_graphs = {}
    ui.preview_position = 0
    ui.graph_zoom = 0.78
    ui.graph_pan = [420.0, -180.0]
    ui.selected_node = 1
    ui.status = ""

    ui._locate_root()

    assert ui.selected_node == 0
    assert ui.graph_pan[0] == 0.0
    assert ui.graph_pan[1] == pytest.approx(56.55)
    assert graph["_expanded_node_ids"] == {0}


def test_reward_interval_and_plot_color_follow_sign_then_absolute_bound_rule() -> None:
    assert interval_camp(RewardInterval(0.2, 0.2)) == "good"
    assert interval_branch_color(RewardInterval(0.2, 0.2)) == "#2563EB"
    assert interval_camp(RewardInterval(-0.3, -0.3)) == "wolf"
    assert interval_branch_color(RewardInterval(-0.3, -0.3)) == "#DC2626"
    assert interval_camp(RewardInterval(0.0, 0.0)) == "balanced"
    assert interval_camp(RewardInterval(-0.2, 0.8)) == "good"
    assert interval_branch_color(RewardInterval(-0.2, 0.8)) == "#2563EB"
    assert interval_camp(RewardInterval(-0.8, 0.2)) == "wolf"
    assert interval_branch_color(RewardInterval(-0.8, 0.2)) == "#DC2626"
    assert interval_camp(RewardInterval(-0.4004, 0.4)) == "balanced"
    assert interval_branch_color(RewardInterval(-0.4004, 0.4)) == "#111111"
    propagated = propagate_intervals(
        [
            RobustIntervals(RewardInterval(-1.0, 0.4), RewardInterval(-0.2, 0.2)),
            RobustIntervals(RewardInterval(0.2, 1.0), RewardInterval(0.1, 0.5)),
        ],
        lambda_risk=0.5,
    )
    assert propagated.wide.to_list() == pytest.approx([-0.7, 0.85])
    assert propagated.narrow.to_list() == pytest.approx([0.025, 0.275])


def test_lambda_recomputes_fixed_graph_without_using_edge_multiplicity() -> None:
    graph = {
        "nodes": [
            {"node_id": 0, "day_count": 0, "night_count": 0, "is_terminal": False, "result": "未结束", "wide_interval": [-1, 1], "narrow_interval": [-1, 1]},
            {"node_id": 1, "day_count": 1, "night_count": 0, "is_terminal": True, "result": "好人阵营胜利", "wide_interval": [-1, 1], "narrow_interval": [-1, 1]},
            {"node_id": 2, "day_count": 1, "night_count": 0, "is_terminal": True, "result": "狼人阵营胜利", "wide_interval": [-1, 1], "narrow_interval": [-1, 1]},
        ],
        "edges": [
            {"parent_id": 0, "child_id": 1, "multiplicity": 999},
            {"parent_id": 0, "child_id": 2, "multiplicity": 1},
        ],
    }
    mean_observation = recompute_graph_intervals(graph, lambda_risk=0.0)
    assert mean_observation.wide.to_list() == pytest.approx([0.0, 0.0])
    risk_observation = recompute_graph_intervals(graph, lambda_risk=1.0)
    assert risk_observation.wide.to_list() == pytest.approx([-1.0, 1.0])


def test_interval_recompute_reports_prepare_node_and_edge_progress() -> None:
    graph = {
        "nodes": [
            {"node_id": 0, "day_count": 0, "night_count": 0, "is_terminal": False, "result": "未结束", "wide_interval": [-1, 1], "narrow_interval": [-1, 1]},
            {"node_id": 1, "day_count": 1, "night_count": 0, "is_terminal": True, "result": "好人阵营胜利", "wide_interval": [-1, 1], "narrow_interval": [-1, 1]},
            {"node_id": 2, "day_count": 1, "night_count": 0, "is_terminal": True, "result": "狼人阵营胜利", "wide_interval": [-1, 1], "narrow_interval": [-1, 1]},
        ],
        "edges": [
            {"parent_id": 0, "child_id": 1},
            {"parent_id": 0, "child_id": 2},
        ],
    }
    progress: list[tuple[str, int, int]] = []
    recompute_graph_intervals(
        graph,
        lambda_risk=0.5,
        progress_callback=lambda stage, completed, total: progress.append(
            (stage, completed, total)
        ),
    )
    assert {stage for stage, _completed, _total in progress} == {
        "prepare_edges",
        "node_intervals",
        "edge_intervals",
    }
    for stage in {item[0] for item in progress}:
        stage_events = [item for item in progress if item[0] == stage]
        assert stage_events[0][1] == 0
        assert stage_events[-1][1] == stage_events[-1][2]


def test_dynamic_lambda_interval_recompute_runs_in_background() -> None:
    class Slider:
        @staticmethod
        def get_current_value() -> float:
            return 0.75

    ui = PygameSimulatorUI.__new__(PygameSimulatorUI)
    ui.graph = {
        "nodes": [
            {"node_id": 0, "day_count": 0, "night_count": 0, "is_terminal": False, "result": "未结束", "wide_interval": [-1, 1], "narrow_interval": [-1, 1]},
            {"node_id": 1, "day_count": 1, "night_count": 0, "is_terminal": True, "result": "好人阵营胜利", "wide_interval": [-1, 1], "narrow_interval": [-1, 1]},
        ],
        "edges": [{"parent_id": 0, "child_id": 1}],
    }
    ui.rows = [{"position_index": 1}]
    ui.selected_row = 0
    ui.lambda_slider = Slider()
    ui.events = queue.Queue()
    ui.interval_recompute_running = False
    ui.interval_recompute_requested_lambda = None

    ui._recompute_loaded_graph()
    assert ui.interval_recompute_running is True
    event_kinds: list[str] = []
    while "local_interval_done" not in event_kinds:
        kind, _payload = ui.events.get(timeout=2.0)
        event_kinds.append(kind)
    assert "local_interval_progress" in event_kinds
    assert event_kinds[-1] == "local_interval_done"


def test_ui_displays_path_and_interval_postprocess_status() -> None:
    ui = PygameSimulatorUI.__new__(PygameSimulatorUI)
    ui.rows = []
    ui.position_progress = {}
    ui.live_graphs = {}
    ui.preview_position = 0
    ui.selected_node = None
    ui.active_position = 0
    ui.paused = False
    ui.status = ""
    ui.live_stats = {
        "terminal_count": 0,
        "good_paths": 0,
        "wolf_paths": 0,
        "expanded_nodes": 0,
        "discovered_nodes": 0,
        "frontier_size": 0,
        "edge_count": 0,
        "completed_positions": 0,
        "total_positions": 1,
    }
    common = {
        "position_index": 1,
        "position_signature": "position-1",
        "roles": ["狼人", "村民", "村民"],
        "position_display": "1:狼人 | 2:村民 | 3:村民",
        "total_positions": 1,
        "processed_states": 20,
        "discovered_states": 20,
        "frontier_size": 0,
        "edge_count": 25,
        "terminal_count": 10,
        "runtime_seconds": 1.0,
    }
    ui._apply_iteration_event(
        {
            **common,
            "kind": "path_progress",
            "postprocess_stage": "path_counts",
            "postprocess_completed": 10,
            "postprocess_total": 20,
        }
    )
    assert "10/20" in ui.status
    assert ui.rows[0]["processing_phase"] == "path_progress"

    ui._apply_iteration_event(
        {
            **common,
            "kind": "interval_progress",
            "postprocess_stage": "node_intervals",
            "postprocess_completed": 30,
            "postprocess_total": 45,
        }
    )
    assert "30/45" in ui.status
    assert ui.rows[0]["processing_phase"] == "interval_progress"
    assert ui.rows[0]["postprocess_stage"] == "node_intervals"


def test_interval_aggregation_consumes_high_fanout_as_a_stream() -> None:
    class StreamingChildren:
        def __init__(self, count: int) -> None:
            self.remaining = count

        def __iter__(self):
            return self

        def __next__(self):
            if self.remaining <= 0:
                raise StopIteration
            self.remaining -= 1
            return (-0.5, 0.75, -0.1, 0.2)

        def __length_hint__(self) -> int:
            raise AssertionError("interval 回传不得物化子节点序列")

    values = propagate_interval_values(
        StreamingChildren(128),
        lambda_risk=0.5,
    )
    assert values == pytest.approx((-0.5, 0.75, -0.1, 0.2))


def test_graph_interval_rollup_uses_explicit_loops_without_generator_frames() -> None:
    source_path = (
        Path(__file__).parents[1]
        / "search_simulator"
        / "_tree_search.py"
    )
    syntax = ast.parse(source_path.read_text(encoding="utf-8"))
    function = next(
        node
        for node in syntax.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "recompute_graph_intervals"
    )
    assert not any(isinstance(node, ast.GeneratorExp) for node in ast.walk(function))

    child_count = 2_000
    nodes = [
        {
            "node_id": 0,
            "day_count": 0,
            "night_count": 0,
            "is_terminal": False,
            "result": "未结束",
            "wide_interval": [-1.0, 1.0],
            "narrow_interval": [-1.0, 1.0],
        }
    ]
    edges = []
    outgoing = {0: []}
    for child_id in range(1, child_count + 1):
        good_terminal = child_id % 2 == 0
        nodes.append(
            {
                "node_id": child_id,
                "day_count": 1,
                "night_count": 0,
                "is_terminal": True,
                "result": "好人阵营胜利" if good_terminal else "狼人阵营胜利",
                "wide_interval": [-1.0, 1.0],
                "narrow_interval": [-1.0, 1.0],
            }
        )
        outgoing[0].append(len(edges))
        edges.append({"parent_id": 0, "child_id": child_id})
    reverse_ids = list(range(child_count, -1, -1))
    result = recompute_graph_intervals(
        {"nodes": nodes, "edges": edges},
        lambda_risk=0.5,
        outgoing_edge_indices=outgoing,
        reverse_node_ids=reverse_ids,
    )
    assert result.wide.to_list() == pytest.approx([-0.5, 0.5])
    assert result.narrow.to_list() == pytest.approx([-0.5, 0.5])


def test_parser_has_tree_options_and_no_online_policy() -> None:
    parser = build_parser()
    destinations = {action.dest for action in parser._actions}
    assert {
        "search_mode",
        "all_positions",
        "tactics",
        "start_state_json",
        "lambda_risk",
        "smart_vote",
        "include_idiot",
        "memory_reserve_gib",
        "memory_reserve_ratio",
    } <= destinations
    assert "policy" not in destinations
    assert "lookahead_depth" not in destinations
    assert "confidence_level" not in destinations
    assert parser.parse_args([]).search_mode == "dfs"


def test_memory_guard_uses_higher_capacity_or_ratio_reserve(monkeypatch) -> None:
    snapshot = MemorySnapshot(
        total_bytes=64 * 1024**3,
        available_bytes=7 * 1024**3,
    )
    monkeypatch.setattr(
        "search_simulator._memory_guard.system_memory_snapshot",
        lambda: snapshot,
    )
    pressure = memory_pressure_snapshot(reserve_ratio=0.15, reserve_gib=8.0)
    assert pressure is not None
    actual_snapshot, threshold = pressure
    assert actual_snapshot == snapshot
    assert threshold == int(64 * 1024**3 * 0.15)


@pytest.mark.skipif(os.name != "nt", reason="仅验证 Windows Win32 ABI")
def test_windows_memory_guard_uses_one_static_type_and_explicit_abi() -> None:
    """回归动态 ctypes 类型和隐式 ABI 长时间调用后的访问冲突。"""

    assert _GLOBAL_MEMORY_STATUS_EX is not None
    assert _GLOBAL_MEMORY_STATUS_EX.restype is ctypes.c_int
    assert _GLOBAL_MEMORY_STATUS_EX.argtypes == (
        ctypes.POINTER(_WindowsMemoryStatusEx),
    )
    assert ctypes.sizeof(_WindowsMemoryStatusEx) == 64

    # 多次调用必须复用模块级结构体类型和函数代理；旧实现没有这两个
    # 静态边界，因此本回归会在进入原生高频路径前直接失败。
    from search_simulator._memory_guard import system_memory_snapshot

    for _iteration in range(10_000):
        snapshot = system_memory_snapshot()
        assert snapshot is not None
        assert snapshot.total_bytes >= snapshot.available_bytes >= 0


def test_search_does_not_materialize_preview_without_consumer(monkeypatch) -> None:
    """CLI 无进度消费者时不得为每个节点重建完整 GameState。"""

    import search_simulator._tree_search as tree_search

    simulator = SearchSimulator(
        number_of_players=3,
        number_of_wolves=1,
        include_seer=False,
        include_witch=False,
        include_guard=False,
        smart_vote=True,
        tactics="",
        all_positions=False,
        persistence_enabled=False,
        progress_queue=None,
        iteration_callback=None,
    )
    monkeypatch.setattr(
        tree_search,
        "game_state_dict_from_compact",
        lambda *args, **kwargs: pytest.fail("不应构造无人消费的节点预览"),
    )
    result = tree_search._search_root(
        simulator,
        simulator.initial_state,
        position_index=1,
        total_positions=1,
    )
    assert len(result["nodes"]) > 0


@pytest.mark.skipif(os.name != "nt", reason="仅验证 Windows spawn 隔离参数")
def test_windows_compute_worker_spawn_adds_no_site_and_restores() -> None:
    """计算 worker 必须带 -S，退出局部上下文后不得污染其他子进程。"""

    import multiprocessing.util as multiprocessing_util

    original = multiprocessing_util._args_from_interpreter_flags
    with _isolated_compute_worker_spawn():
        assert "-S" in multiprocessing_util._args_from_interpreter_flags()
    assert multiprocessing_util._args_from_interpreter_flags is original


def test_search_resumes_inside_position_from_atomic_checkpoint(tmp_path) -> None:
    """单站位超过 worker 预算后必须从原 frontier 继续而非从头搜索。"""

    import search_simulator._tree_search as tree_search

    simulator = SearchSimulator(
        number_of_players=3,
        number_of_wolves=1,
        include_seer=False,
        include_witch=False,
        include_guard=False,
        smart_vote=True,
        tactics="",
        all_positions=False,
        persistence_enabled=False,
    )
    checkpoint_path = tmp_path / "position.pickle"
    chunk_processed_values: list[int] = []
    while True:
        result = tree_search._search_root(
            simulator,
            simulator.initial_state,
            position_index=1,
            total_positions=1,
            materialize_graph=True,
            checkpoint_path=str(checkpoint_path),
            node_budget=1,
        )
        if not result.get("chunk_incomplete"):
            break
        chunk_processed_values.append(int(result["processed_states"]))
    assert chunk_processed_values == sorted(set(chunk_processed_values))
    assert len(chunk_processed_values) >= 1
    assert len(result["nodes"]) == 3
    _remove_search_checkpoint(checkpoint_path)
    assert not checkpoint_path.exists()


def test_failed_memory_run_can_resume_same_run_id(tmp_path) -> None:
    """上次启动失败后仍应复用已存在的站位内检查点批次。"""

    store = _SQLiteLRUSignatureStore(
        tmp_path / "resume.sqlite3",
        lru_capacity=128,
        commit_interval=16,
    )
    config = {"roster": ["狼人", "村民", "村民"], "search_mode": "dfs"}
    run_id = store.start_run(config)
    store.finish_run(
        run_id,
        {
            "next_position_index": 1,
            "positions": [],
            "in_position_checkpoint": {"checkpoint_path": "position.pickle"},
        },
        status="failed",
    )
    resumed_run_id, resumed = store.start_or_resume_run(config)
    assert resumed is True
    assert resumed_run_id == run_id
    store.close()


def test_staged_zero_node_position_is_never_a_completed_checkpoint(tmp_path) -> None:
    """站位内分块暂存行不得被 0=0 的计数偶然判定为完整解。"""

    store = _SQLiteLRUSignatureStore(
        tmp_path / "staging.sqlite3",
        lru_capacity=128,
        commit_interval=16,
    )
    config = {"roster": ["狼人", "村民", "村民"], "search_mode": "dfs"}
    run_id = store.start_run(config)
    store.begin_position_staging(
        run_id,
        {
            "position_index": 1,
            "position_signature": "position-1",
            "roles": ["狼人", "村民", "村民"],
        },
    )
    assert store.list_completed_position_results(run_id) == []
    store.close()


def test_complete_run_rejects_missing_position_graphs(tmp_path) -> None:
    """solution 终态必须覆盖全部站位且每个站位至少存在根节点。"""

    store = _SQLiteLRUSignatureStore(
        tmp_path / "invalid-solution.sqlite3",
        lru_capacity=128,
        commit_interval=16,
    )
    run_id = store.start_run({"roster": ["狼人", "村民", "村民"]})
    with pytest.raises(ValueError, match="拒绝登记不完整 solution"):
        store.finish_run(
            run_id,
            {
                "position_count": 1,
                "total_position_count": 1,
                "next_position_index": None,
                "positions": [{"state_count": 0}],
            },
            status="complete",
        )
    store.close()


def test_crash_log_uses_one_timestamp_without_pid(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("SEARCH_SIMULATOR_CRASH_LOG", raising=False)
    monkeypatch.delenv("SEARCH_SIMULATOR_CRASH_SESSION", raising=False)
    monkeypatch.setattr(
        "search_simulator._crash_handler.crash_log_directory",
        lambda: tmp_path,
    )
    first = prepare_crash_log_path()
    second = prepare_crash_log_path()
    assert first == second
    assert first.parent == tmp_path
    assert re.fullmatch(r"crash_\d{8}_\d{6}_\d{6}\.log", first.name)


def test_previous_native_crash_is_reported_only_once(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(
        "SEARCH_SIMULATOR_CRASH_LOG",
        str(tmp_path / "crash_20260823_080000_000000.log"),
    )
    monkeypatch.setenv("SEARCH_SIMULATOR_CRASH_SESSION", "20260823_080000_000000")
    monkeypatch.setattr(
        "search_simulator._crash_handler.crash_log_directory",
        lambda: tmp_path,
    )
    previous = tmp_path / "crash_20260823_075900_000000.log"
    previous.write_text("Windows fatal exception: access violation", encoding="utf-8")
    assert previous_unreported_crash_log() == previous.resolve()
    mark_crash_log_reported(previous)
    assert previous_unreported_crash_log() is None


def test_caught_python_failure_populates_timestamp_crash_log(
    monkeypatch,
    tmp_path,
) -> None:
    crash_path = tmp_path / "crash_20260823_080000_000000.log"
    monkeypatch.setenv("SEARCH_SIMULATOR_CRASH_LOG", str(crash_path))
    monkeypatch.setenv("SEARCH_SIMULATOR_CRASH_SESSION", "20260823_080000_000000")
    try:
        raise AttributeError("captured worker failure")
    except AttributeError as exc:
        record_caught_failure(
            exc,
            category="python_exception",
            context={
                "run_id": "run-caught",
                "checkpoints": "0/1",
                "next_position": 1,
            },
        )
        first_size = crash_path.stat().st_size
        record_caught_failure(
            exc,
            category="gui_worker",
            context={"run_id": "run-caught"},
        )

    content = crash_path.read_text(encoding="utf-8")
    assert crash_path.stat().st_size == first_size
    assert "pid=" in content
    assert "run_id=run-caught" in content
    assert "AttributeError: captured worker failure" in content
    assert "Traceback" in content


def test_terminal_popup_distinguishes_complete_interrupted_and_failed(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("SEARCH_SIMULATOR_LOG", str(tmp_path / "runtime.log"))
    monkeypatch.setenv(
        "SEARCH_SIMULATOR_CRASH_LOG",
        str(tmp_path / "crash_20260823_080000_000000.log"),
    )
    monkeypatch.setenv("SEARCH_SIMULATOR_CRASH_SESSION", "20260823_080000_000000")
    base = {
        "run_id": "run-123",
        "position_count": 3,
        "total_position_count": 5,
        "next_position_index": 4,
    }
    complete_title, complete_body = _terminal_popup_content(
        "complete",
        {**base, "position_count": 5, "next_position_index": None},
    )
    interrupted_title, interrupted_body = _terminal_popup_content(
        "interrupted",
        base,
    )
    failed_title, failed_body = _terminal_popup_content(
        "failed",
        base,
        error={"error_type": "BrokenProcessPool", "error": "worker exited"},
    )
    assert complete_title == "迭代完成"
    assert "全部目标站位均已完成" in complete_body
    assert "并非完成" in interrupted_body
    assert "#4" in interrupted_body
    assert "未完成" in failed_title
    assert "BrokenProcessPool" in failed_body
    assert "runtime.log" in failed_body
    assert "crash_20260823_080000_000000.log" in failed_body


def test_interrupted_summary_is_never_logged_as_complete(caplog) -> None:
    result = {
        "status": "interrupted",
        "position_count": 2,
        "total_position_count": 5,
        "next_position_index": 3,
        "processed_states": 20,
        "good_paths": 2,
        "wolf_paths": 4,
        "wide_interval": [-0.5, 0.2],
        "narrow_interval": [-0.2, 0.1],
    }
    with caplog.at_level(logging.INFO):
        text = report_tree_summary(result)
    assert "未完成（可恢复中断）" in text
    assert "树搜索完成：" not in caplog.text


def test_position_scheduler_is_fixed_to_one_isolated_worker() -> None:
    source_path = (
        Path(__file__).parents[1]
        / "search_simulator"
        / "_tree_search.py"
    )
    syntax = ast.parse(source_path.read_text(encoding="utf-8"))
    function = next(
        node
        for node in syntax.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "run_position_batch"
    )
    pool_calls = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ProcessPoolExecutor"
    ]
    assert len(pool_calls) == 1
    max_workers = next(
        keyword.value
        for keyword in pool_calls[0].keywords
        if keyword.arg == "max_workers"
    )
    assert isinstance(max_workers, ast.Constant)
    assert max_workers.value == 1


def test_broken_worker_pool_logs_critical_failed_terminal(
    monkeypatch,
    tmp_path,
    caplog,
) -> None:
    crash_path = tmp_path / "crash_20260823_080100_000000.log"
    monkeypatch.setenv("SEARCH_SIMULATOR_CRASH_LOG", str(crash_path))
    monkeypatch.setenv("SEARCH_SIMULATOR_CRASH_SESSION", "20260823_080100_000000")

    class BrokenFuture:
        def result(self):
            raise BrokenProcessPool("simulated native worker crash")

    class BrokenExecutor:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _traceback):
            return False

        def submit(self, _function, _payload):
            return BrokenFuture()

    monkeypatch.setattr(
        "search_simulator._tree_search.ProcessPoolExecutor",
        BrokenExecutor,
    )
    simulator = SearchSimulator(
        number_of_players=4,
        number_of_wolves=1,
        include_seer=False,
        include_witch=False,
        include_guard=False,
        tactics="",
        all_positions=False,
        signature_cache_db_path=tmp_path / "worker-crash.sqlite3",
        memory_reserve_gib=0.0,
        memory_reserve_ratio=0.0,
    )
    with caplog.at_level(logging.INFO), pytest.raises(BrokenProcessPool) as raised:
        simulator.run()
    assert "status=failed" in caplog.text
    assert "category=worker_crash" in caplog.text
    assert "simulated native worker crash" in caplog.text
    assert raised.value.run_id == simulator.run_id
    assert raised.value.next_position_index == 1
    crash_content = crash_path.read_text(encoding="utf-8")
    assert "category=worker_crash" in crash_content
    assert f"run_id={simulator.run_id}" in crash_content
    assert "BrokenProcessPool: simulated native worker crash" in crash_content
    simulator.signature_cache.close()


def test_cli_and_worker_boundaries_pass_simulator_parameters_by_name() -> None:
    project_root = Path(__file__).parents[1] / "search_simulator"
    boundaries = (
        (project_root / "__main__.py", "_run_simulation"),
        (project_root / "_tree_search.py", "_position_task"),
    )
    required = {
        "number_of_players",
        "number_of_wolves",
        "search_mode",
        "lambda_risk",
        "memory_reserve_gib",
        "memory_reserve_ratio",
    }
    for source_path, function_name in boundaries:
        syntax = ast.parse(source_path.read_text(encoding="utf-8"))
        function = next(
            node
            for node in syntax.body
            if isinstance(node, ast.FunctionDef)
            and node.name == function_name
        )
        constructor = next(
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "SearchSimulator"
        )
        keyword_names = {keyword.arg for keyword in constructor.keywords}
        assert None not in keyword_names
        assert required <= keyword_names


def test_memory_interrupt_resumes_same_run_from_position_checkpoint(tmp_path) -> None:
    db_path = tmp_path / "resume.sqlite3"
    common = {
        "number_of_players": 4,
        "number_of_wolves": 1,
        "include_seer": False,
        "include_witch": False,
        "include_guard": False,
        "tactics": "",
        "search_mode": "dfs",
        "parallel_workers": 4,
        "all_positions": False,
        "signature_cache_db_path": db_path,
    }
    interrupted_simulator = SearchSimulator(
        **common,
        memory_reserve_gib=10**9,
        memory_reserve_ratio=1.0,
    )
    interrupted = interrupted_simulator.run()
    interrupted_run_id = interrupted["run_id"]
    assert interrupted["status"] == "interrupted"
    assert interrupted["position_count"] == 0
    assert interrupted["next_position_index"] == 1
    interrupted_simulator.signature_cache.close()

    resumed_simulator = SearchSimulator(
        **common,
        memory_reserve_gib=0.0,
        memory_reserve_ratio=0.0,
    )
    resumed = resumed_simulator.run()
    assert resumed["status"] == "complete"
    assert resumed["run_id"] == interrupted_run_id
    assert resumed["resumed_run"] is True
    assert resumed["position_count"] == 1
    assert resumed["total_position_count"] == 1
    resumed_simulator.signature_cache.close()


def test_resume_skips_complete_position_and_discards_partial_rows(tmp_path) -> None:
    db_path = tmp_path / "complete-checkpoint.sqlite3"
    simulator = SearchSimulator(
        number_of_players=4,
        number_of_wolves=1,
        include_seer=False,
        include_witch=False,
        include_guard=False,
        tactics="",
        all_positions=False,
        signature_cache_db_path=db_path,
        memory_reserve_gib=0.0,
        memory_reserve_ratio=0.0,
    )
    first = simulator.run()
    run_id = first["run_id"]
    simulator.signature_cache.finish_run(run_id, first, status="interrupted")
    simulator.signature_cache.close()

    resumed_simulator = SearchSimulator(
        number_of_players=4,
        number_of_wolves=1,
        include_seer=False,
        include_witch=False,
        include_guard=False,
        tactics="",
        all_positions=False,
        signature_cache_db_path=db_path,
        memory_reserve_gib=0.0,
        memory_reserve_ratio=0.0,
    )
    resumed = resumed_simulator.run()
    assert resumed["run_id"] == run_id
    assert resumed["position_count"] == 1
    assert resumed["processed_states"] == first["processed_states"]
    assert resumed["positions"][0]["restored_from_checkpoint"] is True
    resumed_simulator.signature_cache.close()


def test_sqlite_persists_position_aware_graph_and_pages(tmp_path) -> None:
    db_path = tmp_path / "tree.sqlite3"
    simulator = SearchSimulator(
        number_of_players=4,
        number_of_wolves=1,
        include_seer=False,
        include_witch=False,
        include_guard=False,
        tactics="",
        search_mode="bfs",
        parallel_workers=1,
        all_positions=False,
        signature_cache_db_path=db_path,
        signature_lru_capacity=64,
        signature_commit_interval=8,
    )
    result = simulator.run()
    store = simulator.signature_cache
    assert isinstance(store, _SQLiteLRUSignatureStore)
    page = store.list_position_results(result["run_id"], limit=20, offset=0)
    assert len(page) == 1
    assert page[0]["position_signature"] == result["positions"][0]["position_signature"]
    graph = store.get_position_graph(result["run_id"], page[0]["position_signature"])
    assert len(graph["nodes"]) == page[0]["state_count"]
    assert len(graph["edges"]) == page[0]["edge_count"]
    assert "state_compact" in graph["nodes"][0]
    assert "state_observation" in graph["nodes"][0]
    assert "state" not in graph["nodes"][0]
    graph["roles"] = page[0]["roles"]
    graph["position_signature"] = page[0]["position_signature"]
    ui = PygameSimulatorUI.__new__(PygameSimulatorUI)
    ui.running = False
    ui.graph = graph
    ui.live_graphs = {}
    ui.preview_position = 0
    ui.selected_row = None
    hover_text = "\n".join(ui._node_game_state_details(0))
    expected_labels = {
        "players": "玩家详情",
        "is_game_over": "是否终局",
        "night_count": "黑夜轮次",
        "day_count": "白天轮次",
        "phase": "当前阶段",
        "last_guard_target_index": "上夜守护目标",
        "seer_check_results": "预言家查验",
        "seer_revealed": "预言家已公开",
        "revealed_good_indices": "公开确认好人",
        "revealed_wolf_indices": "公开确认狼人",
        "public_role_claims": "公开身份声明",
        "idiot_revealed_indices": "已揭示愚者",
        "wolf_priority_targets": "狼人优先目标",
        "last_day_votes": "上轮白天票型",
        "last_day_strategy": "上轮白天战术",
        "position_signature": "站位签名",
        "action_label": "派生动作",
        "players_snapshot": "玩家快照",
        "state_id": "状态编号",
        "parent_state_id": "父节点",
        "depth": "搜索深度",
    }
    assert set(GameStateContract.__dataclass_fields__) == set(expected_labels)
    assert all(label in hover_text for label in expected_labels.values())
    assert "【节点概览】" in hover_text
    assert "【玩家详情】" in hover_text
    assert "号玩家｜角色：" in hover_text
    assert "｜状态：存活｜技能：" in hover_text
    assert '"' not in hover_text
    assert "{" not in hover_text
    assert "}" not in hover_text
    assert "wide_interval" in graph["nodes"][0]
    assert "narrow_interval" in graph["nodes"][0]
    assert graph["edges"][0]["reasons"]
    for table_name in ("position_results", "graph_nodes", "graph_edges"):
        columns = store.schema_columns(table_name)
        assert "reward_lower" not in columns
        assert "reward_upper" not in columns
    stats = store.stats_snapshot()
    assert stats["position_results"] == 1
    assert stats["graph_nodes"] == page[0]["state_count"]
    store.close()


def test_bounded_result_queue_streams_batches_into_sqlite(tmp_path) -> None:
    with multiprocessing.Manager() as manager:
        result_queue = manager.Queue(maxsize=2)
        simulator = SearchSimulator(
            number_of_players=4,
            number_of_wolves=1,
            include_seer=False,
            include_witch=False,
            include_guard=False,
            tactics="",
            search_mode="dfs",
            parallel_workers=1,
            all_positions=False,
            result_queue=result_queue,
            signature_cache_db_path=tmp_path / "queued.sqlite3",
        )
        result = simulator.run()

    assert result["position_count"] == 1
    assert result["processed_states"] > 0
    page = simulator.signature_cache.list_position_results(
        result["run_id"],
        limit=1,
        offset=0,
    )
    assert page[0]["state_count"] == result["positions"][0]["state_count"]
    assert simulator.signature_cache.stats_snapshot()["position_results"] == 1
    simulator.signature_cache.close()


def test_sqlite_store_contains_no_plaintext_statements() -> None:
    source_path = (
        Path(__file__).parents[1]
        / "search_simulator"
        / "_sqlite_lru_signature_store.py"
    )
    syntax = ast.parse(source_path.read_text(encoding="utf-8"))
    forbidden = re.compile(
        r"\b(?:PRAGMA|CREATE\s+TABLE|CREATE\s+INDEX|ALTER\s+TABLE|"
        r"DROP\s+TABLE|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|"
        r"SELECT\s+.+\s+FROM)\b",
        re.IGNORECASE | re.DOTALL,
    )
    string_literals = (
        node.value
        for node in ast.walk(syntax)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )
    assert not any(forbidden.search(value) for value in string_literals)


def test_large_path_counts_are_stringified_for_json(tmp_path) -> None:
    output = tmp_path / "result.json"
    save_tree_results(
        {
            "good_paths": 10**30,
            "wolf_paths": 2,
            "wide_interval": [-0.1, 0.2],
            "narrow_interval": [-0.05, 0.1],
        },
        output_path=output,
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["good_paths"] == str(10**30)
    assert payload["wolf_paths"] == 2
