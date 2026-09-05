"""One-shot structured action selection against an ActionCatalog.

Observation is pre-injected; the model must pick exactly one catalog action.
Illegal output gets a single same-schema repair round.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.messages import SystemMessage

from backend.agents.cognitive.action_catalog import ActionCatalog
from backend.agents.cognitive.action_catalog import coerce_legacy_payload
from backend.agents.cognitive.action_catalog import extract_json_object
from backend.agents.cognitive.action_catalog import salvage_seat_from_text
from backend.agents.cognitive.action_catalog import validate_payload
from backend.agents.cognitive.observe import Observation
from backend.agents.cognitive.observe import format_observation
from backend.agents.cognitive.prompts import build_game_context
from backend.agents.cognitive.prompts import build_strategy_bias_block

CHOOSE_ACTION = "choose_action"


def build_structured_user_prompt(
    obs: Observation,
    catalog: ActionCatalog,
    extra: str = "",
    strategy_bias: dict[str, list[str]] | None = None,
    bias_action: str = "talk",
    memory: Any = None,
) -> str:
    parts = [build_game_context(obs), "", format_observation(obs)]
    # 对局日志审计 P0-1：不注入 memory 会导致玩家对自己的行动失忆
    # （女巫救人后白天声称"未用药"）。角色状态/判断/行动记录必须进入决策上下文。
    if memory is not None:
        memory_text = memory.format_for_prompt()
        if memory_text:
            parts.extend(["", memory_text])
    bias = build_strategy_bias_block(strategy_bias or {}, bias_action)
    if bias:
        parts.extend(["", bias])
    if extra.strip():
        parts.extend(["", extra.strip()])
    parts.extend(["", catalog.render(), "", _output_contract(catalog)])
    return "\n".join(parts)


def build_repair_prompt(
    obs: Observation,
    catalog: ActionCatalog,
    previous: dict[str, Any] | str,
    error: str,
    extra: str = "",
    strategy_bias: dict[str, list[str]] | None = None,
    bias_action: str = "talk",
    memory: Any = None,
) -> str:
    previous_text = previous if isinstance(previous, str) else json.dumps(previous, ensure_ascii=False)
    extra_block = extra.strip() + "\n\n" if extra.strip() else ""
    return "\n".join(
        [
            build_structured_user_prompt(
                obs, catalog, extra="", strategy_bias=strategy_bias, bias_action=bias_action, memory=memory
            ),
            "",
            extra_block.rstrip(),
            f"上一次输出无法执行，原因: {error}",
            f"上一次输出: {previous_text}",
            "请按同一 schema 重新选择恰好一个合法 action，不要解释。",
            _output_contract(catalog),
        ]
    )


def choose_action_schema(catalog: ActionCatalog) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "reasoning": {"type": "string", "description": "简短说明为何选择该操作。"},
        "action": {
            "type": "string",
            "enum": list(catalog.ids()),
            "description": "本回合必须且只能选的一项操作。",
        },
    }
    required = ["reasoning", "action"]
    needs_target = any("target_seat" in spec.params for spec in catalog.actions)
    needs_claim = any("claim_seat" in spec.params for spec in catalog.actions)
    if needs_target:
        properties["target_seat"] = {
            "type": ["integer", "null"],
            "description": "需要座位参数的 action 填写合法座位号；其他 action 填 null。",
        }
    if needs_claim:
        properties["claim_seat"] = {
            "type": ["integer", "null"],
            "description": "seer_claim（真报/造假）的查验座位；只跳身份时必须为 null。",
        }
        properties["claim_result"] = {
            "type": ["string", "null"],
            "description": "seer_claim 的查验结果，只能是 good 或 wolf；其他 action 必须为 null。",
        }
    if catalog.require_speech:
        properties["speech"] = {
            "type": "string",
            "description": "按所选 action 组织的公开发言。silence 允许极短。",
        }
        required.append("speech")
    return {
        "type": "function",
        "function": {
            "name": CHOOSE_ACTION,
            "description": "从本回合 action_list 中选择恰好一项并给出参数。",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def parse_structured_response(
    response: Any,
    catalog: ActionCatalog,
    obs: Observation,
) -> tuple[dict[str, Any] | None, str, str]:
    """Parse native FC or text JSON. Returns (normalized, error, raw_dump)."""
    payload, raw = _payload_from_response(response)
    if payload is None:
        text = raw if isinstance(raw, str) else str(raw or "")
        salvaged = salvage_seat_from_text(text, catalog, obs)
        if salvaged is not None:
            payload = salvaged
        else:
            return None, "未得到可解析的结构化决策", text
    adapted = coerce_legacy_payload(payload, catalog, obs)
    normalized, error = validate_payload(adapted, catalog)
    raw_dump = json.dumps(adapted, ensure_ascii=False)
    if error:
        return None, error, raw_dump
    return normalized, "", raw_dump


def supports_bind_tools(llm: Any) -> bool:
    return hasattr(llm, "bind_tools") and callable(getattr(llm, "bind_tools"))


def invoke_structured(
    llm: Any,
    system_prompt: str,
    user_prompt: str,
    catalog: ActionCatalog,
    *,
    max_tokens: int = 4096,
    temperature: float | None = None,
) -> Any:
    messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
    schema = choose_action_schema(catalog)
    bound = llm
    kwargs: dict[str, Any] = {"max_tokens": max_tokens}
    if temperature is not None:
        kwargs["temperature"] = temperature
    if supports_bind_tools(llm):
        bound = llm.bind_tools([schema])
        kwargs["force_tool_name"] = CHOOSE_ACTION
        try:
            return bound.invoke(messages, **kwargs)
        except TypeError:
            try:
                return bound.invoke(messages, max_tokens=max_tokens)
            except TypeError:
                return bound.invoke(messages)
        except Exception:
            # Provider rejected the tool schema (e.g. union types) — retry as text JSON.
            kwargs.pop("force_tool_name", None)
    try:
        return llm.invoke(messages, **kwargs)
    except TypeError:
        try:
            return llm.invoke(messages, max_tokens=max_tokens)
        except TypeError:
            return llm.invoke(messages)


def run_structured_decision(
    llm: Any,
    system_prompt: str,
    obs: Observation,
    catalog: ActionCatalog,
    *,
    extra: str = "",
    strategy_bias: dict[str, list[str]] | None = None,
    bias_action: str = "talk",
    max_tokens: int = 4096,
    temperature: float | None = None,
    memory: Any = None,
) -> dict[str, Any]:
    """One native/text call plus at most one repair. Raises on persistent failure."""
    if not catalog.actions:
        raise RuntimeError(f"no catalog actions for phase={obs.phase} role={obs.player_role}")
    user = build_structured_user_prompt(
        obs, catalog, extra=extra, strategy_bias=strategy_bias, bias_action=bias_action, memory=memory
    )
    response = invoke_structured(
        llm, system_prompt, user, catalog, max_tokens=max_tokens, temperature=temperature
    )
    normalized, error, raw = parse_structured_response(response, catalog, obs)
    calls = 1
    if error:
        repair_user = build_repair_prompt(
            obs,
            catalog,
            raw,
            error,
            extra=extra,
            strategy_bias=strategy_bias,
            bias_action=bias_action,
            memory=memory,
        )
        response = invoke_structured(
            llm, system_prompt, repair_user, catalog, max_tokens=max_tokens, temperature=temperature
        )
        normalized, error, raw = parse_structured_response(response, catalog, obs)
        calls = 2
    if error or normalized is None:
        empty_speech = _is_empty_speech_error(error)
        if catalog.require_speech and not empty_speech:
            fallback = _fallback_structured(catalog, raw, error or "empty")
            if fallback is not None:
                fallback["_structured_calls"] = calls
                fallback["_structured_fallback"] = True
                return fallback
        raise RuntimeError(f"structured decision failed after repair: {error or 'empty'}")
    normalized["_structured_calls"] = calls
    return normalized


def _output_contract(catalog: ActionCatalog) -> str:
    example: dict[str, Any] = {"reasoning": "一句话理由", "action": catalog.ids()[0]}
    spec = catalog.actions[0]
    if "target_seat" in spec.params and spec.legal_targets:
        example["target_seat"] = spec.legal_targets[0]
    if catalog.require_speech:
        example["speech"] = "按所选操作组织的公开发言。"
    return (
        "约束：action 必须是本回合 list 中的恰好一个；需要座位时 target_seat / "
        "claim_seat 必须落在该 action 的合法集合内；不需要的字段填 null；"
        "seer_claim 的参数随职业变化。"
        + (" speech 必填。" if catalog.require_speech else " 不要输出 speech。")
        + f" 例: {json.dumps(example, ensure_ascii=False)}"
    )


def _is_empty_speech_error(error: str | None) -> bool:
    text = error or ""
    return any(token in text for token in ("缺少 speech", "发言过短", "silence 也需要"))


def _fallback_structured(catalog: ActionCatalog, raw: str, error: str) -> dict[str, Any] | None:
    """Keep the turn legal when exclusive-schema output cannot be repaired."""
    payload = extract_json_object(raw) if isinstance(raw, str) else None
    speech = ""
    reasoning = error
    if isinstance(payload, dict):
        speech = str(payload.get("speech") or "").strip()
        reasoning = str(payload.get("reasoning") or reasoning).strip() or error
    if catalog.require_speech and not speech:
        speech = "过。"
    if not catalog.require_speech or not catalog.ids():
        return None
    action = "silence" if "silence" in catalog.ids() else catalog.ids()[0]
    if not speech:
        speech = "过。"
    elif action != "silence" and len(speech) < 3:
        speech = "过。"
    candidate = {"action": action, "reasoning": reasoning or "structured-fallback", "speech": speech}
    spec = catalog.get(action)
    if spec and "target_seat" in spec.params and spec.legal_targets:
        candidate["target_seat"] = spec.legal_targets[0]
    normalized, err = validate_payload(candidate, catalog)
    return None if err else normalized


def _payload_from_response(response: Any) -> tuple[dict[str, Any] | None, str]:
    tool_calls = getattr(response, "tool_calls", None) or []
    for call in tool_calls:
        name = call.get("name") if isinstance(call, dict) else getattr(call, "name", "")
        if name not in {CHOOSE_ACTION, "submit_decision"}:
            continue
        args = call.get("args") if isinstance(call, dict) else getattr(call, "args", {})
        if isinstance(args, str):
            parsed = extract_json_object(args)
            return parsed, args
        if isinstance(args, dict):
            if args:
                return args, json.dumps(args, ensure_ascii=False)
            continue
    content = getattr(response, "content", None)
    text = content if isinstance(content, str) else str(content or response or "")
    return extract_json_object(text), text
