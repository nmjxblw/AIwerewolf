from __future__ import annotations

import ast
import json
import multiprocessing
import queue
import re
import threading
from dataclasses import fields
from itertools import product
from pathlib import Path

import pytest

from search_simulator import GameState
from search_simulator import SearchSimulator
from search_simulator._config import build_parser
from search_simulator._game_state import GameState as GameStateContract
from search_simulator._gui import UI_DATA_REFRESH_SECONDS
from search_simulator._gui import PygameSimulatorUI
from search_simulator._interval import RewardInterval
from search_simulator._interval import RobustIntervals
from search_simulator._interval import interval_branch_color
from search_simulator._interval import interval_camp
from search_simulator._interval import propagate_interval_values
from search_simulator._interval import propagate_intervals
from search_simulator._positions import build_role_roster
from search_simulator._positions import enumerate_position_layouts
from search_simulator._positions import players_for_layout
from search_simulator._reporting import save_tree_results
from search_simulator._sqlite_lru_signature_store import _SQLiteLRUSignatureStore
from search_simulator._strategy import enumerate_day_tactic_profiles
from search_simulator._strategy import enumerate_night_tactic_profiles
from search_simulator._tree_search import PREVIEW_EDGE_BATCH_LIMIT
from search_simulator._tree_search import PREVIEW_EMIT_INTERVAL_SECONDS
from search_simulator._tree_search import PREVIEW_NODE_BATCH_LIMIT
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
    final_progress = remaining_events[-1]
    result = result_holder["result"]
    assert final_progress["kind"] == "node_progress"
    assert final_progress["processed_states"] == result["processed_states"]
    assert final_progress["discovered_states"] == result["state_count"]
    assert final_progress["edge_count"] == result["edge_count"]
    assert final_progress["frontier_size"] == 0
    assert final_progress["preview_nodes"]
    assert final_progress["preview_edges"]
    for event in [started, *remaining_events]:
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
    } <= destinations
    assert "policy" not in destinations
    assert "lookahead_depth" not in destinations
    assert "confidence_level" not in destinations
    assert parser.parse_args([]).search_mode == "dfs"


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
    for field in fields(GameStateContract):
        assert f'"{field.name}"' in hover_text
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
