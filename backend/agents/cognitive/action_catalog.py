"""Per-turn mutually exclusive action catalog for one-shot structured decisions.

Aligns with docs/对齐文档.md action_list. Observation / game context stay
elsewhere; this module only enumerates legal acts, params, and targets.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any
from typing import Iterable

from backend.agents.cognitive.observe import Observation
from backend.agents.cognitive.observe import PlayerInfo

SPEECH_NO_TARGET = frozenset({"baseline", "silence"})
SPEECH_WITH_TARGET = frozenset({"accusation", "support", "vote_intent"})
SPEECH_ACTIONS = frozenset({*SPEECH_NO_TARGET, *SPEECH_WITH_TARGET, "seer_claim"})
_ACTION_ALIASES = {
    "talk": "baseline",
    "plain": "baseline",
    "skip": "silence",
    "pass": "silence",
    "accuse": "accusation",
    "support_player": "support",
    "vote": "vote_intent",
    "claim": "seer_claim",
    "jump": "seer_claim",
}

WOLF_ROLES = frozenset({"Werewolf", "WhiteWolfKing", "BigBadWolf", "WolfCub", "WolfKing"})
SEER_ROLES = frozenset({"Seer"})
IDENTITY_CLAIM_ROLES = frozenset({"Villager", "Witch", "Guard", "Hunter", "Idiot"})

CLAIM_TRUE = "true_check"
CLAIM_FAKE = "fake_check"
CLAIM_IDENTITY = "identity_only"
CLAIM_RESULTS = frozenset({"good", "wolf"})

_SPEECH_PHASE_TOKENS = ("SPEECH", "LAST_WORDS", "SHERIFF_CLOSING")


@dataclass(frozen=True)
class ActionSpec:
    """One mutually exclusive action available this turn."""

    id: str
    description: str
    effect: str
    params: tuple[str, ...] = ()
    legal_targets: tuple[int, ...] = ()
    claim_results: tuple[str, ...] = ()
    claim_result_by_seat: tuple[tuple[int, str], ...] = ()
    claim_mode: str = ""
    mapped_action: str = ""


@dataclass(frozen=True)
class ActionCatalog:
    """The exclusive action_list for the current role / phase / board state."""

    actions: tuple[ActionSpec, ...]
    phase: str
    role: str
    require_speech: bool = False

    def ids(self) -> tuple[str, ...]:
        return tuple(spec.id for spec in self.actions)

    def get(self, action_id: str) -> ActionSpec | None:
        for spec in self.actions:
            if spec.id == action_id:
                return spec
        return None

    def render(self) -> str:
        """Prompt block listing exclusive options and legal parameters."""
        lines = ["【本回合可选操作，必须且只能选一项】"]
        for spec in self.actions:
            lines.append(f"- {spec.id}：{spec.description}")
            if spec.effect:
                lines.append(f"  效果：{spec.effect}")
            if spec.params:
                lines.append(f"  参数：{', '.join(spec.params)}")
            if "target_seat" in spec.params:
                seats = _format_seats(spec.legal_targets)
                lines.append(f"  target_seat 合法：{seats or '无'}")
            if "claim_seat" in spec.params:
                seats = _format_seats(spec.legal_targets)
                lines.append(f"  claim_seat 合法：{seats or '无'}")
            if "claim_result" in spec.params:
                if spec.claim_results:
                    if len(spec.legal_targets) == 1 and len(spec.claim_results) == 1:
                        lines.append(
                            f"  claim_result 只能是 {spec.claim_results[0]}（与私有结果一致）"
                        )
                    else:
                        lines.append(f"  claim_result：{' 或 '.join(spec.claim_results)}")
                else:
                    lines.append("  不得填写 claim_result")
            if spec.id in SPEECH_NO_TARGET or spec.id == "seer_claim" and not spec.params:
                lines.append("  不得带 target_seat / claim_seat / claim_result")
        lines.append("请只返回一个 JSON 对象，action 必须是上列恰好一项。")
        return "\n".join(lines)

    @classmethod
    def for_turn(
        cls,
        obs: Observation,
        *,
        honesty_rule: bool = False,
        wolf_night_options: Iterable[str] | None = None,
        witch_save_used: bool = False,
        witch_poison_used: bool = False,
        witch_victim_id: str | None = None,
        last_guard_target_id: str | None = None,
    ) -> "ActionCatalog":
        phase = str(obs.phase or "")
        role = str(obs.player_role or "")
        options = set(wolf_night_options) if wolf_night_options is not None else _wolf_night_options()
        if _is_speech_phase(phase):
            actions = _speech_actions(obs, honesty_rule=honesty_rule)
            return cls(tuple(actions), phase, role, require_speech=True)
        if "VOTE" in phase.upper() and "BADGE" not in phase.upper():
            return cls(tuple(_vote_actions(obs)), phase, role, require_speech=False)
        if "WOLF" in phase.upper():
            return cls(tuple(_wolf_night_actions(obs, options)), phase, role)
        if "SEER" in phase.upper():
            return cls(tuple(_seer_night_actions(obs)), phase, role)
        if "WITCH" in phase.upper():
            return cls(
                tuple(
                    _witch_night_actions(
                        obs,
                        save_used=witch_save_used,
                        poison_used=witch_poison_used,
                        victim_id=witch_victim_id,
                    )
                ),
                phase,
                role,
            )
        if "GUARD" in phase.upper():
            return cls(tuple(_guard_night_actions(obs, last_guard_target_id)), phase, role)
        return cls((), phase, role)


def honesty_rule_from_obs(obs: Observation, explicit: bool | None = None) -> bool:
    """Resolve honesty-rule flag from explicit value, env, or public announcement."""
    if explicit is not None:
        return bool(explicit)
    env = os.getenv("AIWEREWOLF_HONESTY_RULE", "").strip().lower()
    if env in {"1", "true", "yes", "on"}:
        return True
    if env in {"0", "false", "no", "off"}:
        return False
    return False


def seer_alive_checks(obs: Observation) -> list[dict[str, Any]]:
    """True seer checks whose targets are still alive."""
    checks = list(obs.private.get("seer_checks") or [])
    if not checks and obs.private.get("seer_check"):
        checks = [obs.private["seer_check"]]
    alive_by_id = {p.id: p for p in obs.alive}
    alive_by_name = {p.name: p for p in obs.alive if p.name}
    results: list[dict[str, Any]] = []
    seen: set[int] = set()
    for check in checks:
        player = _resolve_check_player(check, alive_by_id, alive_by_name)
        if player is None or player.seat in seen:
            continue
        seen.add(player.seat)
        results.append(
            {
                "seat": player.seat,
                "player_id": player.id,
                "name": player.name,
                "result": "wolf" if check.get("is_wolf") else "good",
            }
        )
    return results


def other_alive_seats(obs: Observation) -> tuple[int, ...]:
    return tuple(sorted(p.seat for p in obs.alive if p.id != obs.player_id and p.seat))


def validate_payload(payload: dict[str, Any], catalog: ActionCatalog) -> tuple[dict[str, Any], str]:
    """Validate a parsed object against this turn's exclusive catalog.

    Returns (normalized, error). error is empty when valid.
    """
    if not isinstance(payload, dict):
        return {}, "输出必须是一个 JSON 对象"
    action = _normalize_action_id(payload.get("action"), catalog)
    if not action:
        return {}, "缺少 action"
    if action not in catalog.ids():
        return {}, f"action={action!r} 不在本回合可选操作中：{', '.join(catalog.ids())}"
    spec = catalog.get(action)
    assert spec is not None

    reasoning = str(payload.get("reasoning") or "").strip()
    if not reasoning:
        return {}, "缺少 reasoning"

    target_seat = _coerce_seat(payload.get("target_seat"))
    claim_seat = _coerce_seat(payload.get("claim_seat"))
    claim_result = str(payload.get("claim_result") or "").strip().lower() or None
    if claim_result in {"none", "null"}:
        claim_result = None
    speech = str(payload.get("speech") or "").strip()

    # Unified FC schema lists target/claim fields for every speech action.
    # Models often fill unused fields; drop them instead of failing the turn.
    if "target_seat" in spec.params:
        if target_seat is None:
            return {}, f"{action} 必须提供 target_seat"
        if target_seat not in spec.legal_targets:
            return {}, f"target_seat={target_seat} 不在合法目标中：{_format_seats(spec.legal_targets)}"
    else:
        target_seat = None

    if action == "seer_claim":
        if spec.claim_mode == CLAIM_IDENTITY:
            claim_seat = None
            claim_result = None
        error = _validate_seer_claim(spec, claim_seat, claim_result)
        if error:
            return {}, error
    else:
        claim_seat = None
        claim_result = None

    if catalog.require_speech:
        if action == "silence":
            if not speech:
                return {}, "silence 也需要极短公开发言（例如「过。」）"
        elif len(speech) < 3:
            return {}, "缺少 speech 或发言过短"
    elif speech:
        # Night / vote: ignore stray speech rather than fail.
        speech = ""

    normalized = {
        "action": action,
        "reasoning": reasoning,
        "target_seat": target_seat,
        "claim_seat": claim_seat,
        "claim_result": claim_result,
        "speech": speech,
        "claim_mode": spec.claim_mode,
        "mapped_action": spec.mapped_action or action,
    }
    return normalized, ""


def coerce_legacy_payload(payload: dict[str, Any], catalog: ActionCatalog, obs: Observation) -> dict[str, Any]:
    """Map older speech/target/witch JSON onto the exclusive action schema."""
    if not isinstance(payload, dict):
        return {}
    if str(payload.get("action") or "").strip():
        return dict(payload)

    adapted = dict(payload)
    if catalog.require_speech:
        if not str(adapted.get("action") or "").strip():
            adapted["action"] = "baseline"
        return adapted

    ids = set(catalog.ids())
    if "save" in payload or "poison_target" in payload:
        return _coerce_legacy_witch(payload, ids)

    raw_target = payload.get("target")
    target_text = "" if raw_target is None else str(raw_target).strip()
    seat = _seat_from_text(target_text, obs)
    explicit_skip = {"跳过", "空刀", "空", "无", "none", "null", "skip", "pass", "不行动"}
    if target_text.lower() in explicit_skip and "skip" in ids:
        adapted["action"] = "skip"
        adapted.pop("target_seat", None)
        return adapted
    if seat is not None and seat == obs.player_seat and "self_attack" in ids:
        adapted["action"] = "self_attack"
        adapted.pop("target_seat", None)
        return adapted
    for action_id in ("day_vote", "attack", "divine", "guard", "poison"):
        spec = catalog.get(action_id)
        if spec and seat is not None and seat in spec.legal_targets:
            adapted["action"] = action_id
            adapted["target_seat"] = seat
            return adapted
    return adapted


def extract_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    cleaned = str(text).strip()
    if cleaned.startswith("DECISION:"):
        cleaned = cleaned[len("DECISION:") :].strip()
    start = cleaned.find("{")
    if start < 0:
        return None
    end = cleaned.rfind("}")
    fragment = cleaned[start : end + 1] if end > start else cleaned[start:]
    import json

    try:
        data = json.loads(fragment)
    except json.JSONDecodeError:
        data = _loads_truncated(cleaned[start:])
    return data if isinstance(data, dict) else None


def salvage_seat_from_text(text: str, catalog: ActionCatalog, obs: Observation) -> dict[str, Any] | None:
    """If unstructured text names exactly one legal target, map it."""
    seats: list[int] = []
    for spec in catalog.actions:
        if "target_seat" not in spec.params:
            continue
        for seat in spec.legal_targets:
            player = _player_by_seat(obs, seat)
            if player is None:
                continue
            if _text_mentions_player(text, player):
                seats.append(seat)
    unique = list(dict.fromkeys(seats))
    if len(unique) != 1:
        return None
    seat = unique[0]
    snippet = str(text or "").strip()[:500]
    for spec in catalog.actions:
        if "target_seat" in spec.params and seat in spec.legal_targets:
            payload = {"action": spec.id, "target_seat": seat, "reasoning": snippet[:300] or spec.id}
            if catalog.require_speech:
                payload["speech"] = snippet or "过。"
            return payload
    return None


# ---------------------------------------------------------------------------
# Catalog builders
# ---------------------------------------------------------------------------


def _speech_actions(obs: Observation, *, honesty_rule: bool) -> list[ActionSpec]:
    others = other_alive_seats(obs)
    actions = [
        ActionSpec(
            id="baseline",
            description="平铺陈述当前判断，不点名、不跳身份。",
            effect="公开信息增量低，不站边、不点人。",
        ),
        ActionSpec(
            id="silence",
            description="少说或过牌，只给极短公开发言。",
            effect="几乎不提供新信息。",
        ),
        ActionSpec(
            id="accusation",
            description="点名怀疑一名存活他人。",
            effect="公开站边反对该玩家。",
            params=("target_seat",),
            legal_targets=others,
        ),
        ActionSpec(
            id="support",
            description="支持一名存活他人，公开站边。",
            effect="公开站边支持该玩家。",
            params=("target_seat",),
            legal_targets=others,
        ),
        ActionSpec(
            id="vote_intent",
            description="表态本轮要投某名存活他人。",
            effect="公开预告投票目标。",
            params=("target_seat",),
            legal_targets=others,
        ),
    ]
    seer_claim = _seer_claim_spec(obs, honesty_rule=honesty_rule)
    if seer_claim is not None:
        actions.append(seer_claim)
    return actions


def _seer_claim_spec(obs: Observation, *, honesty_rule: bool) -> ActionSpec | None:
    role = str(obs.player_role or "")
    if honesty_rule and role not in SEER_ROLES:
        return None
    if role in SEER_ROLES:
        checks = seer_alive_checks(obs)
        if not checks:
            return ActionSpec(
                id="seer_claim",
                description="公开声明自己是预言家。当前没有仍存活的查验对象，不得编造查验。",
                effect="可自称预言家，但不得报已出局或未查过的对象。",
                claim_mode=CLAIM_IDENTITY,
            )
        seats = tuple(int(item["seat"]) for item in checks)
        results = tuple(str(item["result"]) for item in checks)
        # One result per seat; if mixed seats, list unique allowed results.
        unique_results = tuple(dict.fromkeys(results))
        seat_result = tuple((int(item["seat"]), str(item["result"])) for item in checks)
        result_hint = "、".join(f"{seat}={result}" for seat, result in seat_result)
        return ActionSpec(
            id="seer_claim",
            description=f"公开跳预言家并公布真实查验。已查存活目标：{result_hint}。",
            effect="必须跳预言家，且报出的对象/阵营与参数一致，不得编造未查或相反结果。",
            params=("claim_seat", "claim_result"),
            legal_targets=seats,
            claim_results=unique_results,
            claim_result_by_seat=seat_result,
            claim_mode=CLAIM_TRUE,
        )
    if role in WOLF_ROLES:
        others = other_alive_seats(obs)
        return ActionSpec(
            id="seer_claim",
            description="悍跳预言家并编造一条查验（可对队友报杀，也可给好人金水）。",
            effect="必须跳预言家，并带上该假查验；不得自相矛盾。",
            params=("claim_seat", "claim_result"),
            legal_targets=others,
            claim_results=("good", "wolf"),
            claim_mode=CLAIM_FAKE,
        )
    if role == "Villager":
        # 廉价磋商 VJ 战术：平民假跳预言家需要能编造查验（与"我们的版本"自由发言行为对齐）。
        # 诚实规则下非预言家在函数开头已返回 None，不会走到这里。
        others = other_alive_seats(obs)
        return ActionSpec(
            id="seer_claim",
            description="假跳预言家挡刀并编造一条查验（如给某人金水）。",
            effect="必须跳预言家，并带上该假查验；不得自相矛盾。",
            params=("claim_seat", "claim_result"),
            legal_targets=others,
            claim_results=("good", "wolf"),
            claim_mode=CLAIM_FAKE,
        )
    if role in IDENTITY_CLAIM_ROLES or role:
        return ActionSpec(
            id="seer_claim",
            description="仅声明自己是预言家，禁止编造查验对象或结果。",
            effect="可自称预言家，但不得报「查了谁、金水/查杀」。",
            claim_mode=CLAIM_IDENTITY,
        )
    return None


def _vote_actions(obs: Observation) -> list[ActionSpec]:
    seats = other_alive_seats(obs)
    legal = tuple(p.seat for p in (obs.legal_targets or []) if p.seat and p.id != obs.player_id)
    return [
        ActionSpec(
            id="day_vote",
            description="白天放逐投票，必须投一名存活他人，不能弃票。",
            effect="该票公开计入放逐。",
            params=("target_seat",),
            legal_targets=legal or seats,
            mapped_action="vote",
        )
    ]


def _wolf_night_actions(obs: Observation, options: set[str]) -> list[ActionSpec]:
    legal = [p for p in (obs.legal_targets or obs.alive) if p.alive]
    attack_seats = tuple(sorted(p.seat for p in legal if p.id != obs.player_id and p.seat))
    actions = [
        ActionSpec(
            id="attack",
            description="刀一名合法目标（非自己）。",
            effect="计入狼队夜刀归票。",
            params=("target_seat",),
            legal_targets=attack_seats,
            mapped_action="attack",
        )
    ]
    if "self" in options:
        actions.append(
            ActionSpec(
                id="self_attack",
                description="自刀（袭击自己，骗女巫解药）。",
                effect="刀口为自己。",
                mapped_action="attack",
            )
        )
    if "empty" in options:
        actions.append(
            ActionSpec(
                id="skip",
                description="空刀 / 跳过，本夜不袭击。",
                effect="制造平安夜假象。",
                mapped_action="attack_empty",
            )
        )
    return actions


def _seer_night_actions(obs: Observation) -> list[ActionSpec]:
    seats = tuple(
        sorted(
            p.seat
            for p in (obs.legal_targets or obs.alive)
            if p.id != obs.player_id and p.seat
        )
    )
    return [
        ActionSpec(
            id="divine",
            description="查验一名尚未查过的存活他人。",
            effect="获得该玩家好人/狼人结果。",
            params=("target_seat",),
            legal_targets=seats,
            mapped_action="divine",
        ),
        ActionSpec(
            id="skip",
            description="本夜不查验。",
            effect="不获得新信息。",
            mapped_action="skip",
        ),
    ]


def _witch_night_actions(
    obs: Observation,
    *,
    save_used: bool,
    poison_used: bool,
    victim_id: str | None,
) -> list[ActionSpec]:
    actions: list[ActionSpec] = []
    victim = _player_by_id(obs, victim_id) if victim_id else None
    can_save = bool(victim_id) and not save_used
    if can_save and victim is not None and victim.id == obs.player_id and obs.day != 1:
        can_save = False
    if can_save:
        label = f"{victim.seat}号:{victim.name}" if victim else victim_id
        actions.append(
            ActionSpec(
                id="save",
                description=f"对今晚刀口 {label} 使用解药。",
                effect="救下该刀口；本夜不能再用药。",
                mapped_action="witch_save",
            )
        )
    if not poison_used:
        poison_seats = tuple(
            sorted(p.seat for p in obs.alive if p.id != obs.player_id and p.seat)
        )
        actions.append(
            ActionSpec(
                id="poison",
                description="对一名除自己以外的存活玩家使用毒药。",
                effect="天亮无条件死亡；本夜不能再用药。",
                params=("target_seat",),
                legal_targets=poison_seats,
                mapped_action="witch_poison",
            )
        )
    actions.append(
        ActionSpec(
            id="skip",
            description="本夜不用药。",
            effect="保留未使用的药。",
            mapped_action="skip",
        )
    )
    return actions


def _guard_night_actions(obs: Observation, last_guard_target_id: str | None) -> list[ActionSpec]:
    seats = tuple(
        sorted(
            p.seat
            for p in (obs.legal_targets or obs.alive)
            if p.id != obs.player_id and p.id != (last_guard_target_id or "") and p.seat
        )
    )
    return [
        ActionSpec(
            id="guard",
            description="守护一名其他存活玩家（不能自守、不能连续两夜守同一人）。",
            effect="若与狼刀重合且女巫未救则挡刀；奶穿则死亡。挡不住毒。",
            params=("target_seat",),
            legal_targets=seats,
            mapped_action="guard",
        ),
        ActionSpec(
            id="skip",
            description="本夜空守。",
            effect="无人被守护。",
            mapped_action="skip",
        ),
    ]


def _validate_seer_claim(spec: ActionSpec, claim_seat: int | None, claim_result: str | None) -> str:
    if spec.claim_mode == CLAIM_IDENTITY:
        if claim_seat is not None or claim_result:
            return "该身份的 seer_claim 只跳身份，不得带 claim_seat / claim_result"
        return ""
    if claim_seat is None:
        return "seer_claim 必须提供 claim_seat"
    if claim_seat not in spec.legal_targets:
        return f"claim_seat={claim_seat} 不在合法目标中：{_format_seats(spec.legal_targets)}"
    if not claim_result:
        return "seer_claim 必须提供 claim_result（good 或 wolf）"
    if claim_result not in CLAIM_RESULTS:
        return f"claim_result={claim_result!r} 非法，只能是 good 或 wolf"
    if spec.claim_mode == CLAIM_TRUE:
        expected = dict(spec.claim_result_by_seat).get(claim_seat)
        if expected and claim_result != expected:
            return f"claim_result 必须与私有查验一致（{expected}）"
    elif spec.claim_mode == CLAIM_FAKE:
        if claim_result not in spec.claim_results:
            return "claim_result 只能是 good 或 wolf"
    return ""


def _coerce_legacy_witch(payload: dict[str, Any], ids: set[str]) -> dict[str, Any]:
    adapted = dict(payload)
    save = bool(payload.get("save"))
    poison = payload.get("poison_target")
    poison_text = "" if poison is None else str(poison).strip()
    no_poison = poison_text.lower() in {"", "none", "null", "无", "不用", "不毒", "跳过"}
    if save and "save" in ids:
        adapted["action"] = "save"
        return adapted
    if save:
        adapted["action"] = "save"
        return adapted
    if poison_text and not no_poison:
        adapted["action"] = "poison"
        if "target_seat" not in adapted:
            seat = _coerce_seat(poison_text)
            if seat is not None:
                adapted["target_seat"] = seat
        return adapted
    if "skip" in ids:
        adapted["action"] = "skip"
    return adapted


def _wolf_night_options() -> set[str]:
    raw = os.getenv("AIWEREWOLF_WOLF_NIGHT_OPTIONS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def _is_speech_phase(phase: str) -> bool:
    upper = str(phase or "").upper()
    return any(token in upper for token in _SPEECH_PHASE_TOKENS)


def _format_seats(seats: Iterable[int]) -> str:
    return ",".join(str(seat) for seat in seats)


def _normalize_action_id(value: Any, catalog: ActionCatalog) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw in catalog.ids():
        return raw
    lowered = raw.lower().replace("-", "_").replace(" ", "_")
    if lowered in catalog.ids():
        return lowered
    aliased = _ACTION_ALIASES.get(lowered, "")
    if aliased and aliased in catalog.ids():
        return aliased
    return raw


def _loads_truncated(fragment: str) -> dict[str, Any] | None:
    """Best-effort parse when the model truncated the closing braces/quotes."""
    import json

    text = str(fragment or "").strip()
    if not text.startswith("{"):
        return None
    buf = text.rstrip().rstrip(",")
    if buf.count('"') % 2 == 1:
        buf += '"'
    for _ in range(6):
        try:
            data = json.loads(buf)
        except json.JSONDecodeError:
            buf += "}"
            continue
        return data if isinstance(data, dict) else None
    return None


def _coerce_seat(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"(\d+)", text)
    if not match:
        return None
    seat = int(match.group(1))
    return seat if seat > 0 else None


def _seat_from_text(text: str, obs: Observation) -> int | None:
    seat = _coerce_seat(text)
    if seat is not None and _player_by_seat(obs, seat):
        return seat
    candidate = str(text or "").strip().lower()
    if not candidate:
        return None
    for player in list(obs.alive) + list(obs.legal_targets):
        if player.name and player.name.lower() == candidate:
            return player.seat
        if player.id and player.id.lower() == candidate:
            return player.seat
    return None


def _player_by_seat(obs: Observation, seat: int) -> PlayerInfo | None:
    for player in list(obs.alive) + list(obs.dead) + list(obs.legal_targets):
        if player.seat == seat:
            return player
    return None


def _player_by_id(obs: Observation, player_id: str | None) -> PlayerInfo | None:
    if not player_id:
        return None
    for player in list(obs.alive) + list(obs.dead) + list(obs.legal_targets):
        if player.id == player_id:
            return player
    return None


def _resolve_check_player(
    check: dict[str, Any],
    alive_by_id: dict[str, PlayerInfo],
    alive_by_name: dict[str, PlayerInfo],
) -> PlayerInfo | None:
    target_id = str(check.get("target_id") or "")
    if target_id and target_id in alive_by_id:
        return alive_by_id[target_id]
    name = str(check.get("target_name") or "")
    if name and name in alive_by_name:
        return alive_by_name[name]
    return None


def _text_mentions_player(text: str, player: PlayerInfo) -> bool:
    blob = str(text or "")
    if player.name and player.name in blob:
        return True
    if player.seat and re.search(rf"(?<!\d){player.seat}\s*号", blob):
        return True
    if player.id and player.id in blob:
        return True
    return False
