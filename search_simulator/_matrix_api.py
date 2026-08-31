"""精确信念 Cheap-talk 决策矩阵的外部 Python 调用门面。"""

from __future__ import annotations

import math
from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ._decision_matrix import DecisionMatrixCalculator
from ._decision_matrix import DecisionMatrixRequest
from ._decision_state import CanonicalGameConfig
from ._decision_state import DecisionState
from ._decision_state import camp_for_role
from ._decision_state import is_wolf_role
from ._role_view import RoleView
from ._speech_action import ACTION_FAMILIES
from ._speech_action import enumerate_speech_actions

_DEFAULT_ROLES = ("狼人", "狼人", "村民", "村民", "预言家", "女巫", "守卫")
_DEFAULT_RULES_SPEC = "seven-player-microphase-rules"
_DEFAULT_VIEW_SPEC = "role-view-hard-knowledge"
_ALLOWED_TACTICS = frozenset({None, "villager_decoy", "wolf_jump"})
_CREDIBILITY_LEVELS = (0.0, 0.5, 0.8)

_TOP_LEVEL_FIELDS = frozenset(
    {
        "config",
        "decision_state",
        "role_view",
        "samples_per_cell",
        "base_seed",
    }
)
_CONFIG_FIELDS = frozenset({"roles", "max_days", "rules_spec"})
_STATE_FIELDS = frozenset(
    {
        "alive",
        "phase",
        "day_count",
        "night_count",
        "speech_order",
        "speech_index",
        "actor_id",
        "public_role_claims",
        "public_events",
        "last_guard_target",
        "witch_save_available",
        "witch_poison_available",
        "winner",
    }
)
_ROLE_VIEW_FIELDS = frozenset(
    {
        "actor_id",
        "actor_role",
        "known_roles",
        "known_camps",
        "seer_checks",
        "view_spec",
    }
)


class DecisionMatrixInputError(ValueError):
    """精确信念 Cheap-talk 决策矩阵调用参数不合法。"""


def _as_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DecisionMatrixInputError(f"{field} 必须是 object")
    if any(not isinstance(key, str) for key in value):
        raise DecisionMatrixInputError(f"{field} 的字段名必须是字符串")
    return value


def _as_array(value: Any, field: str) -> list[Any] | tuple[Any, ...]:
    if not isinstance(value, (list, tuple)):
        raise DecisionMatrixInputError(f"{field} 必须是 array")
    return value


def _as_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DecisionMatrixInputError(f"{field} 必须是 integer")
    return int(value)


def _as_boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise DecisionMatrixInputError(f"{field} 必须是 boolean")
    return bool(value)


def _as_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DecisionMatrixInputError(f"{field} 必须是 number")
    number = float(value)
    if not math.isfinite(number):
        raise DecisionMatrixInputError(f"{field} 必须是有限数值")
    return number


def _as_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise DecisionMatrixInputError(f"{field} 必须是非空字符串")
    return value


def _reject_unknown(data: Mapping[str, Any], allowed: frozenset[str], field: str) -> None:
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise DecisionMatrixInputError(f"{field} 包含未知字段: {', '.join(unknown)}")


def _seat(value: Any, field: str, number_of_players: int) -> int:
    seat = _as_integer(value, field)
    if not 0 <= seat < number_of_players:
        raise DecisionMatrixInputError(f"{field} 必须位于 0..{number_of_players - 1}")
    return seat


def _parse_config(value: Any) -> CanonicalGameConfig:
    data = _as_mapping({} if value is None else value, "config")
    _reject_unknown(data, _CONFIG_FIELDS, "config")
    role_values = _as_array(data.get("roles", list(_DEFAULT_ROLES)), "config.roles")
    roles = tuple(_as_string(role, f"config.roles[{index}]") for index, role in enumerate(role_values))
    if tuple(sorted(roles)) != tuple(sorted(_DEFAULT_ROLES)):
        raise DecisionMatrixInputError("config.roles 必须恰好是固定七人板子的角色多重集合")
    max_days = _as_integer(data.get("max_days", 8), "config.max_days")
    if max_days <= 0:
        raise DecisionMatrixInputError("config.max_days 必须大于零")
    rules_spec = _as_string(data.get("rules_spec", _DEFAULT_RULES_SPEC), "config.rules_spec")
    if rules_spec != _DEFAULT_RULES_SPEC:
        raise DecisionMatrixInputError(f"config.rules_spec 只支持 {_DEFAULT_RULES_SPEC}")
    return CanonicalGameConfig.from_roles(roles, max_days=max_days, rules_spec=rules_spec)


def _parse_known_pairs(
    value: Any,
    *,
    field: str,
    number_of_players: int,
    allowed_values: frozenset[str],
) -> dict[int, str]:
    result: dict[int, str] = {}
    for index, item_value in enumerate(_as_array(value, field)):
        item = _as_array(item_value, f"{field}[{index}]")
        if len(item) != 2:
            raise DecisionMatrixInputError(f"{field}[{index}] 必须包含 seat 和 value 两项")
        seat = _seat(item[0], f"{field}[{index}][0]", number_of_players)
        label = _as_string(item[1], f"{field}[{index}][1]")
        if label not in allowed_values:
            raise DecisionMatrixInputError(f"{field}[{index}][1] 包含不支持的值: {label}")
        if seat in result:
            raise DecisionMatrixInputError(f"{field} 不得重复声明席位 {seat}")
        result[seat] = label
    return result


def _parse_role_view(value: Any, config: CanonicalGameConfig) -> RoleView:
    data = _as_mapping(value, "role_view")
    _reject_unknown(data, _ROLE_VIEW_FIELDS, "role_view")
    if "actor_id" not in data or "actor_role" not in data:
        raise DecisionMatrixInputError("role_view.actor_id 和 role_view.actor_role 始终必填")
    actor_id = _seat(data["actor_id"], "role_view.actor_id", config.number_of_players)
    actor_role = _as_string(data["actor_role"], "role_view.actor_role")
    if actor_role not in config.roles:
        raise DecisionMatrixInputError("role_view.actor_role 不属于固定七人板子")

    known_roles = _parse_known_pairs(
        data.get("known_roles", [[actor_id, actor_role]]),
        field="role_view.known_roles",
        number_of_players=config.number_of_players,
        allowed_values=frozenset(config.roles),
    )
    if known_roles != {actor_id: actor_role}:
        raise DecisionMatrixInputError("role_view.known_roles 只能且必须包含行动者自己的确切角色")

    actor_camp = camp_for_role(actor_role)
    if "known_camps" not in data:
        if is_wolf_role(actor_role):
            raise DecisionMatrixInputError("狼人必须通过 role_view.known_camps 提供自己和狼队友的席位")
        known_camps = {actor_id: actor_camp}
    else:
        known_camps = _parse_known_pairs(
            data["known_camps"],
            field="role_view.known_camps",
            number_of_players=config.number_of_players,
            allowed_values=frozenset({"good", "wolf"}),
        )
    if is_wolf_role(actor_role):
        if len(known_camps) != config.number_of_wolves or known_camps.get(actor_id) != "wolf":
            raise DecisionMatrixInputError("狼人的 known_camps 必须包含自己和全部狼队席位")
        if any(camp != "wolf" for camp in known_camps.values()):
            raise DecisionMatrixInputError("狼人的 known_camps 只能包含 wolf")
    elif known_camps != {actor_id: "good"}:
        raise DecisionMatrixInputError("好人的 known_camps 只能且必须包含行动者自身的 good 阵营")

    checks_value = data.get("seer_checks", [])
    checks: list[tuple[int, int, bool]] = []
    seen_targets: set[int] = set()
    for index, item_value in enumerate(_as_array(checks_value, "role_view.seer_checks")):
        item = _as_array(item_value, f"role_view.seer_checks[{index}]")
        if len(item) != 3:
            raise DecisionMatrixInputError(f"role_view.seer_checks[{index}] 必须包含三项")
        observer = _seat(item[0], f"role_view.seer_checks[{index}][0]", config.number_of_players)
        target = _seat(item[1], f"role_view.seer_checks[{index}][1]", config.number_of_players)
        is_wolf = _as_boolean(item[2], f"role_view.seer_checks[{index}][2]")
        if observer != actor_id:
            raise DecisionMatrixInputError("seer_checks.observer 必须等于当前行动者")
        if target == actor_id:
            raise DecisionMatrixInputError("预言家不能查验自己")
        if target in seen_targets:
            raise DecisionMatrixInputError(f"role_view.seer_checks 不得重复查验席位 {target}")
        seen_targets.add(target)
        checks.append((observer, target, is_wolf))
    if actor_role == "预言家" and len(checks) != 1:
        raise DecisionMatrixInputError("固定首日的预言家必须提供恰好一条昨夜查验")
    if actor_role != "预言家" and checks:
        raise DecisionMatrixInputError("只有预言家行动者可以提供 seer_checks")

    view_spec = _as_string(data.get("view_spec", _DEFAULT_VIEW_SPEC), "role_view.view_spec")
    if view_spec != _DEFAULT_VIEW_SPEC:
        raise DecisionMatrixInputError(f"role_view.view_spec 只支持 {_DEFAULT_VIEW_SPEC}")
    return RoleView(
        actor_id=actor_id,
        actor_role=actor_role,
        known_roles=tuple(sorted(known_roles.items())),
        known_camps=tuple(sorted(known_camps.items())),
        seer_checks=tuple(sorted(checks)),
        view_spec=view_spec,
    )


def _parse_public_events(
    value: Any,
    *,
    speech_order: tuple[int, ...],
    speech_index: int,
    number_of_players: int,
) -> tuple[tuple[Any, ...], ...]:
    earlier_positions = {seat: index for index, seat in enumerate(speech_order[:speech_index])}
    seen_speakers: set[int] = set()
    last_position = -1
    events: list[tuple[Any, ...]] = []
    for event_index, event_value in enumerate(_as_array(value, "decision_state.public_events")):
        event = _as_array(event_value, f"decision_state.public_events[{event_index}]")
        if len(event) != 7:
            raise DecisionMatrixInputError(f"decision_state.public_events[{event_index}] 必须包含七项")
        if event[0] != "speech":
            raise DecisionMatrixInputError("public_events 当前只接受 speech 事件")
        speaker = _seat(event[1], f"decision_state.public_events[{event_index}][1]", number_of_players)
        if speaker not in earlier_positions:
            raise DecisionMatrixInputError("public_events 只能包含当前行动者之前已经发言的席位")
        if speaker in seen_speakers:
            raise DecisionMatrixInputError(f"public_events 不得重复记录席位 {speaker}")
        position = earlier_positions[speaker]
        if position <= last_position:
            raise DecisionMatrixInputError("public_events 必须遵循 speech_order 的先后顺序")
        seen_speakers.add(speaker)
        last_position = position
        family = _as_string(event[2], f"decision_state.public_events[{event_index}][2]")
        if family not in ACTION_FAMILIES:
            raise DecisionMatrixInputError(f"public_events 包含不支持的动作族: {family}")
        claim_role, claim_target, claim_result, tactic = event[3], event[4], event[5], event[6]
        if family == "seer_claim":
            if claim_role != "预言家":
                raise DecisionMatrixInputError("seer_claim 的 claim_role 必须为预言家")
            if (claim_target is None) != (claim_result is None):
                raise DecisionMatrixInputError("完整预言家声明必须同时提供 claim_target 和 claim_result")
            if claim_target is not None:
                claim_target = _seat(
                    claim_target,
                    f"decision_state.public_events[{event_index}][4]",
                    number_of_players,
                )
                if claim_target == speaker:
                    raise DecisionMatrixInputError("预言家声明不能查验自己")
                if claim_result not in {"good", "wolf"}:
                    raise DecisionMatrixInputError("预言家声明结果必须为 good 或 wolf")
            if tactic not in _ALLOWED_TACTICS:
                raise DecisionMatrixInputError("public_events 包含不支持的战术标签")
        elif any(item is not None for item in (claim_role, claim_target, claim_result, tactic)):
            raise DecisionMatrixInputError("非预言家声明事件不得携带 claim 或 tactic 字段")
        events.append(("speech", speaker, family, claim_role, claim_target, claim_result, tactic))
    return tuple(events)


def _parse_public_claims(
    value: Any,
    *,
    events: tuple[tuple[Any, ...], ...],
    number_of_players: int,
) -> tuple[tuple[int, str], ...]:
    expected = {int(event[1]): "预言家" for event in events if event[2] == "seer_claim"}
    if value is None:
        return tuple(sorted(expected.items()))
    claims = _parse_known_pairs(
        value,
        field="decision_state.public_role_claims",
        number_of_players=number_of_players,
        allowed_values=frozenset({"预言家"}),
    )
    if claims != expected:
        raise DecisionMatrixInputError("public_role_claims 必须与 public_events 中的身份声明一致")
    return tuple(sorted(claims.items()))


def _parse_state(value: Any, config: CanonicalGameConfig, role_view: RoleView) -> DecisionState:
    data = _as_mapping({} if value is None else value, "decision_state")
    _reject_unknown(data, _STATE_FIELDS, "decision_state")
    actor_id = _seat(data.get("actor_id", role_view.actor_id), "decision_state.actor_id", config.number_of_players)
    if actor_id != role_view.actor_id:
        raise DecisionMatrixInputError("decision_state.actor_id 必须与 role_view.actor_id 一致")
    alive_values = _as_array(
        data.get("alive", [True] * config.number_of_players),
        "decision_state.alive",
    )
    if len(alive_values) != config.number_of_players:
        raise DecisionMatrixInputError("decision_state.alive 长度必须等于七人板子人数")
    alive = tuple(
        _as_boolean(value, f"decision_state.alive[{index}]") for index, value in enumerate(alive_values)
    )
    if not alive[actor_id]:
        raise DecisionMatrixInputError("当前行动者必须存活")

    default_order = [index for index, is_alive in enumerate(alive) if is_alive]
    order_values = _as_array(data.get("speech_order", default_order), "decision_state.speech_order")
    speech_order = tuple(
        _seat(value, f"decision_state.speech_order[{index}]", config.number_of_players)
        for index, value in enumerate(order_values)
    )
    if len(set(speech_order)) != len(speech_order) or set(speech_order) != set(default_order):
        raise DecisionMatrixInputError("speech_order 必须无重复并恰好覆盖全部存活席位")
    inferred_index = speech_order.index(actor_id)
    speech_index = _as_integer(data.get("speech_index", inferred_index), "decision_state.speech_index")
    if not 0 <= speech_index < len(speech_order) or speech_order[speech_index] != actor_id:
        raise DecisionMatrixInputError("speech_index 必须指向 speech_order 中的当前行动者")

    phase = _as_string(data.get("phase", "day_speech"), "decision_state.phase")
    if phase != "day_speech":
        raise DecisionMatrixInputError("decision_state.phase 当前只支持 day_speech")
    day_count = _as_integer(data.get("day_count", 0), "decision_state.day_count")
    night_count = _as_integer(data.get("night_count", 1), "decision_state.night_count")
    if day_count != 0 or night_count != 1:
        raise DecisionMatrixInputError("当前接口只支持第一天白天发言前状态")
    winner = data.get("winner")
    if winner is not None:
        raise DecisionMatrixInputError("decision_state.winner 必须为 null")

    events = _parse_public_events(
        data.get("public_events", []),
        speech_order=speech_order,
        speech_index=speech_index,
        number_of_players=config.number_of_players,
    )
    claims = _parse_public_claims(
        data.get("public_role_claims"),
        events=events,
        number_of_players=config.number_of_players,
    )
    guard_value = data.get("last_guard_target")
    last_guard_target = (
        None
        if guard_value is None
        else _seat(guard_value, "decision_state.last_guard_target", config.number_of_players)
    )
    return DecisionState(
        alive=alive,
        phase=phase,
        day_count=day_count,
        night_count=night_count,
        speech_order=speech_order,
        speech_index=speech_index,
        actor_id=actor_id,
        public_role_claims=claims,
        public_events=events,
        last_guard_target=last_guard_target,
        witch_save_available=_as_boolean(
            data.get("witch_save_available", True),
            "decision_state.witch_save_available",
        ),
        witch_poison_available=_as_boolean(
            data.get("witch_poison_available", True),
            "decision_state.witch_poison_available",
        ),
        winner=None,
    )


def build_custom_decision_matrix_request(payload: Mapping[str, Any]) -> DecisionMatrixRequest:
    """校验外部输入并构造精确信念 Cheap-talk 决策矩阵请求。

    本函数只构造规范请求，不访问数据库，也不启动 Monte Carlo。固定七人
    板子、首日发言状态、每格 100 个样本和基准种子 7 均有默认值；完整
    隐藏站位不是合法参数。

    参数：
        payload: ``Mapping[str, Any]`` 类型的观察者安全映射。唯一始终
            必填的子对象是
            ``role_view``，其中 ``actor_id`` 表示当前行动者的零基席位，
            ``actor_role`` 表示其自知角色。狼人还必须在 ``known_camps``
            中提供自己和狼队友的席位；固定首日的预言家必须在
            ``seer_checks`` 中提供一条昨夜查验。可选顶层字段包括：

            - ``config``：固定角色多重集合、最大天数和规则规范；
            - ``decision_state``：存活状态、发言轮次、公开事件及规则资源；
            - ``samples_per_cell``：每个动作与可信度档位的样本数，默认 100；
            - ``base_seed``：可复现随机流的非负基准种子，默认 7。

            规范化后的上述全部请求字段都会进入请求身份。

    返回：
        ``DecisionMatrixRequest``：已经补齐默认值、生成全部二级候选动作并
        验证 posterior 非空的不可变请求。可使用 ``request_digest()`` 取得
        稳定请求摘要，使用 ``candidate_actions`` 查看可查询动作。

    异常：
        DecisionMatrixInputError: payload 类型或字段不合法、角色视角越权、
            必要的狼队/查验信息缺失，或公开状态与当前行动者不一致。

    示例：
        普通好人角色只需提供行动者席位和角色：

        >>> from search_simulator import build_custom_decision_matrix_request
        >>> payload = {"role_view": {"actor_id": 3, "actor_role": "女巫"}}
        >>> request = build_custom_decision_matrix_request(payload)
        >>> request.actor_id
        3
        >>> request.samples_per_cell
        100
        >>> len(request.credibility_levels)
        3
    """

    try:
        data = _as_mapping(payload, "payload")
        _reject_unknown(data, _TOP_LEVEL_FIELDS, "payload")
        if "role_view" not in data:
            raise DecisionMatrixInputError("payload.role_view 必填")
        config = _parse_config(data.get("config"))
        role_view = _parse_role_view(data["role_view"], config)
        state = _parse_state(data.get("decision_state"), config, role_view)
        samples_per_cell = _as_integer(data.get("samples_per_cell", 100), "samples_per_cell")
        if samples_per_cell <= 0:
            raise DecisionMatrixInputError("samples_per_cell 必须大于零")
        base_seed = _as_integer(data.get("base_seed", 7), "base_seed")
        if base_seed < 0:
            raise DecisionMatrixInputError("base_seed 必须不小于零")
        request = DecisionMatrixRequest(
            config=config,
            decision_state=state,
            actor_id=role_view.actor_id,
            actor_role=role_view.actor_role,
            role_view=role_view,
            candidate_actions=enumerate_speech_actions(state, role_view),
            samples_per_cell=samples_per_cell,
            base_seed=base_seed,
        )
        request.request_digest()
        return request
    except DecisionMatrixInputError:
        raise
    except (TypeError, ValueError) as exc:
        raise DecisionMatrixInputError(str(exc)) from exc


def _validate_database_path(database_path: str | Path) -> Path:
    if not isinstance(database_path, (str, Path)):
        raise DecisionMatrixInputError("database_path 必须是 str 或 Path")
    if isinstance(database_path, str) and not database_path.strip():
        raise DecisionMatrixInputError("database_path 不能为空")
    return Path(database_path)


def _validate_execution(
    *,
    workers: Any,
    batch_size: Any,
    force_recompute: Any,
    memory_reserve_gib: Any,
    memory_reserve_ratio: Any,
    progress_callback: Any,
    stop_event: Any,
) -> tuple[int, int, bool, float, float, Callable[[dict[str, Any]], None] | None, Any | None]:
    worker_count = _as_integer(workers, "workers")
    batch_count = _as_integer(batch_size, "batch_size")
    if worker_count <= 0 or batch_count <= 0:
        raise DecisionMatrixInputError("workers 和 batch_size 必须大于零")
    if not isinstance(force_recompute, bool):
        raise DecisionMatrixInputError("force_recompute 必须是 boolean")
    reserve_gib = _as_number(memory_reserve_gib, "memory_reserve_gib")
    reserve_ratio = _as_number(memory_reserve_ratio, "memory_reserve_ratio")
    if reserve_gib < 0.0:
        raise DecisionMatrixInputError("memory_reserve_gib 必须不小于零")
    if not 0.0 <= reserve_ratio <= 1.0:
        raise DecisionMatrixInputError("memory_reserve_ratio 必须位于 [0,1]")
    if progress_callback is not None and not callable(progress_callback):
        raise DecisionMatrixInputError("progress_callback 必须可调用或为 None")
    if stop_event is not None and not callable(getattr(stop_event, "is_set", None)):
        raise DecisionMatrixInputError("stop_event 必须提供 is_set()")
    return (
        worker_count,
        batch_count,
        force_recompute,
        reserve_gib,
        reserve_ratio,
        progress_callback,
        stop_event,
    )


def calculate_custom_decision_matrix(
    payload: Mapping[str, Any],
    database_path: str | Path = "search_simulator_cache.sqlite3",
    *,
    workers: int = 2,
    batch_size: int = 10,
    force_recompute: bool = False,
    memory_reserve_gib: float = 8.0,
    memory_reserve_ratio: float = 0.15,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    stop_event: Any | None = None,
) -> dict[str, Any]:
    """计算或复用自定义观察状态的精确信念 Cheap-talk 决策矩阵。

    本函数是同步 CPU API。它先调用
    ``build_custom_decision_matrix_request()`` 规范化输入，再查询同一 SQLite
    中的完整矩阵；缓存未命中时使用隔离计算子进程执行前向终局模拟。

    参数：
        payload: ``Mapping[str, Any]`` 类型的观察者安全矩阵输入。必须包含
            ``role_view.actor_id`` 和
            ``role_view.actor_role``；狼人还需提供两名狼的阵营席位，固定
            首日的预言家还需提供昨夜一条查验。其余请求字段和默认值与
            ``build_custom_decision_matrix_request()`` 相同。规范化 payload
            进入请求身份。
        database_path: ``str | Path`` 类型的矩阵结果和检查点文件。默认使用
            当前工作目录下的 ``search_simulator_cache.sqlite3``；不进入
            请求身份。
        workers: ``int`` 类型的隔离计算子进程数，默认 2，必须大于零；
            只影响执行方式，不进入请求身份。
        batch_size: ``int`` 类型的连续样本批次大小，默认 10，必须大于零；
            只影响调度和检查点粒度，不进入请求身份。
        force_recompute: ``bool`` 类型，默认 ``False``，优先复用相同请求的
            完整矩阵；为 ``True`` 时创建新运行并保留历史结果。该选择不
            进入请求身份。
        memory_reserve_gib: ``float`` 类型的物理内存绝对保留量，默认
            8.0 GiB，必须不小于零；只控制可恢复中断，不进入请求身份。
        memory_reserve_ratio: ``float`` 类型的可用内存比例保留线，默认
            0.15，合法范围为 ``[0, 1]``；不进入请求身份。
        progress_callback: ``Callable[[dict[str, Any]], None] | None`` 类型的
            可选回调，默认 ``None``。每次批次成功提交后接收一个新字典，
            字段包括 ``status``、``matrix_id``、``committed_batches``、
            ``total_batches`` 和 ``cache_hit``。回调不参与请求身份。
        stop_event: 可选事件对象，默认 ``None``，非空时必须提供
            ``is_set()``。置位后在批次边界形成可恢复中断，不删除已经
            提交的批次，也不进入请求身份。

    返回：
        JSON-safe 字典，主要字段为：

        - ``matrix_id``：持久化运行标识；
        - ``request_digest``：规范请求摘要；
        - ``status``：完整返回时为 ``complete``；
        - ``request``：不含完整隐藏站位的规范请求；
        - ``action_rows``：每个具体动作一行，分别包含可信度
          ``0.0``、``0.5``、``0.8`` 下的收益均值、标准误、相对基线差、
          样本数和情景计数；
        - ``notice``：结果适用范围说明。

    异常：
        DecisionMatrixInputError: payload、数据库路径或执行参数不合法。
        MatrixInterrupted: 内存安全保护或 ``stop_event`` 导致可恢复中断。
        RuntimeError: 计算进程、持久化或完整性校验失败。函数不会用空矩阵
            代替失败结果。
        KeyboardInterrupt: 调用方人工中断；已提交检查点仍保留。

    示例：
        Windows 多进程脚本需要主模块入口保护：

        .. code-block:: python

            from __future__ import annotations

            import multiprocessing
            from pathlib import Path

            from search_simulator import calculate_custom_decision_matrix


            def main() -> None:
                payload = {
                    "role_view": {
                        "actor_id": 3,
                        "actor_role": "女巫",
                    },
                    "samples_per_cell": 100,
                }
                result = calculate_custom_decision_matrix(
                    payload,
                    Path("search_simulator_cache.sqlite3"),
                    workers=2,
                    batch_size=10,
                )
                print(result["matrix_id"], len(result["action_rows"]))


            if __name__ == "__main__":
                multiprocessing.freeze_support()
                main()
    """

    request = build_custom_decision_matrix_request(payload)
    database = _validate_database_path(database_path)
    (
        worker_count,
        batch_count,
        force,
        reserve_gib,
        reserve_ratio,
        callback,
        event,
    ) = _validate_execution(
        workers=workers,
        batch_size=batch_size,
        force_recompute=force_recompute,
        memory_reserve_gib=memory_reserve_gib,
        memory_reserve_ratio=memory_reserve_ratio,
        progress_callback=progress_callback,
        stop_event=stop_event,
    )
    result = DecisionMatrixCalculator(
        database,
        workers=worker_count,
        batch_size=batch_count,
        memory_reserve_gib=reserve_gib,
        memory_reserve_ratio=reserve_ratio,
        progress_callback=callback,
        stop_event=event,
    ).calculate(request, force_recompute=force)
    return result.to_dict()


def load_custom_decision_matrix_cell(
    payload: Mapping[str, Any],
    database_path: str | Path = "search_simulator_cache.sqlite3",
    *,
    action_key: str,
    credibility: float,
) -> dict[str, Any] | None:
    """读取一个已完成的精确信念 Cheap-talk 决策矩阵单元。

    本函数只查询完整矩阵，不启动计算，也不返回未完成批次。调用时必须
    使用与计算阶段相同的 payload；样本数、基准种子或观察状态不同都会
    形成不同请求身份。

    参数：
        payload: ``Mapping[str, Any]`` 类型、与已完成矩阵相同的观察者安全
            输入。默认补齐规则与 ``build_custom_decision_matrix_request()``
            完全一致，并进入矩阵请求身份。
        database_path: ``str | Path`` 类型的已完成矩阵文件，默认
            ``search_simulator_cache.sqlite3``；只定位存储，不进入请求身份。
        action_key: ``str`` 类型、由完整矩阵某一行返回的稳定动作键。它
            必须属于当前 payload 的候选集合，与可信度共同定位单元格，
            但不改变矩阵请求身份。
        credibility: ``float`` 类型的发言证据强度，只允许 ``0``、``0.5``
            或 ``0.8``；与动作键共同定位单元格，不改变矩阵请求身份。

    返回：
        命中时返回 JSON-safe 字典，包含 ``action_key``、``action``、
        ``credibility``、``mean``、``standard_error``、``baseline_delta``、
        ``baseline_delta_standard_error``、``sample_count`` 和
        ``scenario_counts``。相同请求尚无完整矩阵或目标单元不存在时返回
        ``None``。

    异常：
        DecisionMatrixInputError: payload 或数据库路径不合法、可信度不在
            固定三档中，或 ``action_key`` 不属于该请求的候选动作。
        RuntimeError: 数据库读取失败。函数不会隐式启动补算。

    示例：
        从相同请求重建稳定动作键，再读取已经完成的中等可信度单元；如果
        完整矩阵尚未计算，``cell`` 为 ``None``：

        .. code-block:: python

            from search_simulator import build_custom_decision_matrix_request
            from search_simulator import load_custom_decision_matrix_cell

            payload = {
                "role_view": {
                    "actor_id": 0,
                    "actor_role": "村民",
                },
                "samples_per_cell": 100,
            }
            request = build_custom_decision_matrix_request(payload)
            action_key = request.candidate_actions[0].key()
            cell = load_custom_decision_matrix_cell(
                payload,
                action_key=action_key,
                credibility=0.5,
            )
            if cell is not None:
                print(cell["mean"], cell["standard_error"])
    """

    request = build_custom_decision_matrix_request(payload)
    database = _validate_database_path(database_path)
    key = _as_string(action_key, "action_key")
    level = _as_number(credibility, "credibility")
    if level not in _CREDIBILITY_LEVELS:
        raise DecisionMatrixInputError("credibility 必须为 0、0.5 或 0.8")
    action = next((candidate for candidate in request.candidate_actions if candidate.key() == key), None)
    if action is None:
        raise DecisionMatrixInputError("action_key 不属于该请求的候选动作")
    cell = DecisionMatrixCalculator(database, workers=1).load_cell(
        request,
        action=action,
        credibility=level,
    )
    return None if cell is None else cell.to_dict()
