from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from search_simulator import DecisionMatrixInputError
from search_simulator import MatrixInterrupted
from search_simulator import build_custom_decision_matrix_request
from search_simulator import calculate_custom_decision_matrix
from search_simulator import load_custom_decision_matrix_cell


def _villager_payload(*, samples_per_cell: int = 1) -> dict[str, object]:
    return {
        "role_view": {
            "actor_id": 0,
            "actor_role": "村民",
        },
        "samples_per_cell": samples_per_cell,
    }


def test_custom_request_fills_fixed_board_defaults() -> None:
    request = build_custom_decision_matrix_request(
        {
            "role_view": {
                "actor_id": 3,
                "actor_role": "女巫",
            }
        }
    )

    assert request.config.number_of_players == 7
    assert request.config.number_of_wolves == 2
    assert request.config.max_days == 8
    assert request.decision_state.alive == (True,) * 7
    assert request.decision_state.speech_order == tuple(range(7))
    assert request.decision_state.speech_index == 3
    assert request.decision_state.actor_id == 3
    assert request.role_view.known_roles == ((3, "女巫"),)
    assert request.role_view.known_camps == ((3, "good"),)
    assert request.samples_per_cell == 100
    assert request.base_seed == 7

    expanded = build_custom_decision_matrix_request(
        {
            "config": {
                "roles": ["狼人", "狼人", "村民", "村民", "预言家", "女巫", "守卫"],
                "max_days": 8,
                "rules_spec": "seven-player-microphase-rules",
            },
            "decision_state": {
                "alive": [True, True, True, True, True, True, True],
                "phase": "day_speech",
                "day_count": 0,
                "night_count": 1,
                "speech_order": [0, 1, 2, 3, 4, 5, 6],
                "speech_index": 3,
                "actor_id": 3,
                "public_role_claims": [],
                "public_events": [],
                "last_guard_target": None,
                "witch_save_available": True,
                "witch_poison_available": True,
                "winner": None,
            },
            "role_view": {
                "actor_id": 3,
                "actor_role": "女巫",
                "known_roles": [[3, "女巫"]],
                "known_camps": [[3, "good"]],
                "seer_checks": [],
                "view_spec": "role-view-hard-knowledge",
            },
            "samples_per_cell": 100,
            "base_seed": 7,
        }
    )
    assert expanded.request_digest() == request.request_digest()


def test_custom_request_requires_private_role_positions_conditionally() -> None:
    with pytest.raises(DecisionMatrixInputError, match="狼队友"):
        build_custom_decision_matrix_request(
            {
                "role_view": {
                    "actor_id": 1,
                    "actor_role": "狼人",
                }
            }
        )
    wolf_request = build_custom_decision_matrix_request(
        {
            "role_view": {
                "actor_id": 1,
                "actor_role": "狼人",
                "known_camps": [[1, "wolf"], [5, "wolf"]],
            }
        }
    )
    assert wolf_request.role_view.known_camps == ((1, "wolf"), (5, "wolf"))

    with pytest.raises(DecisionMatrixInputError, match="昨夜查验"):
        build_custom_decision_matrix_request(
            {
                "role_view": {
                    "actor_id": 2,
                    "actor_role": "预言家",
                }
            }
        )
    seer_request = build_custom_decision_matrix_request(
        {
            "role_view": {
                "actor_id": 2,
                "actor_role": "预言家",
                "seer_checks": [[2, 5, True]],
            }
        }
    )
    assert seer_request.role_view.seer_checks == ((2, 5, True),)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                "role_view": {"actor_id": 0, "actor_role": "村民"},
                "seat_roles": ["村民", "狼人", "守卫", "狼人", "预言家", "女巫", "村民"],
            },
            "未知字段",
        ),
        (
            {
                "role_view": {"actor_id": 0, "actor_role": "村民"},
                "decision_state": {"actor_id": 1},
            },
            "必须与 role_view.actor_id 一致",
        ),
        (
            {
                "role_view": {
                    "actor_id": 0,
                    "actor_role": "村民",
                    "known_camps": [[0, "good"], [1, "wolf"]],
                }
            },
            "只能且必须包含行动者自身",
        ),
    ],
)
def test_custom_request_rejects_observer_unsafe_or_inconsistent_input(
    payload: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(DecisionMatrixInputError, match=message):
        build_custom_decision_matrix_request(payload)


def test_custom_request_accepts_sparse_prior_public_events() -> None:
    request = build_custom_decision_matrix_request(
        {
            "role_view": {"actor_id": 3, "actor_role": "守卫"},
            "decision_state": {
                "public_events": [
                    ["speech", 0, "seer_claim", "预言家", 5, "wolf", None],
                    ["speech", 2, "silence", None, None, None, None],
                ]
            },
        }
    )

    assert request.decision_state.public_role_claims == ((0, "预言家"),)
    assert [event[1] for event in request.decision_state.public_events] == [0, 2]


def test_custom_api_calculates_reuses_and_loads_cell(tmp_path: Path) -> None:
    database_path = tmp_path / "custom-matrix.sqlite3"
    payload = _villager_payload()
    progress: list[dict[str, object]] = []

    result = calculate_custom_decision_matrix(
        payload,
        database_path,
        workers=1,
        batch_size=1,
        memory_reserve_gib=0.0,
        memory_reserve_ratio=0.0,
        progress_callback=progress.append,
    )

    assert result["status"] == "complete"
    assert result["action_rows"]
    assert result["request"]["actor_id"] == 0
    assert result["request"]["role_view"]["known_roles"] == [[0, "村民"]]
    assert "seat_roles" not in result["request"]
    assert progress[-1]["status"] == "complete"
    assert progress[-1]["cache_hit"] is False

    cached = calculate_custom_decision_matrix(
        payload,
        database_path,
        workers=1,
        batch_size=1,
        memory_reserve_gib=0.0,
        memory_reserve_ratio=0.0,
    )
    assert cached["matrix_id"] == result["matrix_id"]

    first_row = result["action_rows"][0]
    cell = load_custom_decision_matrix_cell(
        payload,
        database_path,
        action_key=first_row["action_key"],
        credibility=0.5,
    )
    assert cell is not None
    assert cell["action_key"] == first_row["action_key"]
    assert cell["credibility"] == 0.5
    assert cell["sample_count"] == 1


def test_custom_api_public_exceptions_are_exported() -> None:
    assert issubclass(DecisionMatrixInputError, ValueError)
    assert issubclass(MatrixInterrupted, RuntimeError)


def test_custom_api_docstrings_describe_library_contract() -> None:
    functions = (
        build_custom_decision_matrix_request,
        calculate_custom_decision_matrix,
        load_custom_decision_matrix_cell,
    )
    for function in functions:
        docstring = inspect.getdoc(function)
        assert docstring is not None
        for section in ("参数：", "返回：", "异常：", "示例："):
            assert section in docstring

    calculate_docstring = inspect.getdoc(calculate_custom_decision_matrix) or ""
    for parameter in (
        "payload",
        "database_path",
        "workers",
        "batch_size",
        "force_recompute",
        "memory_reserve_gib",
        "memory_reserve_ratio",
        "progress_callback",
        "stop_event",
    ):
        assert parameter in calculate_docstring
    for result_field in ("matrix_id", "request_digest", "action_rows", "notice"):
        assert result_field in calculate_docstring
