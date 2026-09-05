"""Cognitive pipeline — Agent Loop with tool-calling and self-termination.

Replaced the fixed 3-step Chain with an autonomous agent loop:
  Agent thinks → optionally calls tools → thinks more → self-terminates → Decision

Supports both:
  - AgentLoop (new default): LLM decides when to call tools, when to output
  - Legacy 3-step Chain: Observe → Think → Act (use_agent_loop=False)

Single Responsibility: orchestrate the LLM calls in the right order.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.messages import SystemMessage
from langchain_core.runnables import Runnable

from backend.agents.cognitive import trace_keys
from backend.agents.cognitive.action_catalog import ActionCatalog
from backend.agents.cognitive.agent_loop import AgentLoop
from backend.agents.cognitive.memory import Memory
from backend.agents.cognitive.observe import Observation
from backend.agents.cognitive.prompts import build_night_prompt
from backend.agents.cognitive.structured_decision import run_structured_decision
from backend.agents.cognitive.prompts import build_observe_prompt
from backend.agents.cognitive.prompts import build_speech_prompt
from backend.agents.cognitive.prompts import build_strategy_bias_block
from backend.agents.cognitive.prompts import build_think_prompt
from backend.agents.cognitive.prompts import build_vote_prompt
from backend.agents.cognitive.retrieval import format_strategies_for_prompt
from backend.agents.cognitive.retrieval import retrieve_strategies as retrieve_strategies_tfidf
from backend.agents.cognitive.retrieval_prod import retrieve_strategies_prod


class Pipeline:
    """Cognitive pipeline with autonomous agent loop + legacy fallback.

    Each invocation of run_speech / run_vote / run_night executes an
    autonomous agent loop where the LLM decides whether to call tools
    (search_strategies, recall_memory, check_rules, analyze_votes) and
    when it has enough information to produce a final decision.

    Between-turn analysis caching: when vote() follows talk() in the same
    turn, the analysis from talk() is reused to skip redundant thinking.
    """

    def __init__(
        self,
        llm: Runnable,
        system_prompt: str,
        strategy_bias: dict[str, list[str]] | None = None,
        persona_mbti: str = "",
        persona_style: str = "",
        use_agent_loop: bool = True,
        retrieval_policy: str = "",
        player_id: str = "",
        feature_flags: dict[str, bool] | None = None,
        game_id: str = "",
    ):
        self._llm = llm
        self._system_prompt = system_prompt
        self._strategy_bias = strategy_bias or {}
        self._persona_mbti = persona_mbti
        self._persona_style = persona_style
        self._use_agent_loop = use_agent_loop and os.getenv("COGNITIVE_USE_AGENT_LOOP", "true").lower() != "false"
        self._retrieval_policy = retrieval_policy
        self._player_id = player_id
        self._game_id = game_id
        self._feature_flags = dict(feature_flags or {})
        self._cached_analysis: str = ""
        self._tentative_vote: dict[str, str] = {}  # {target, reasoning} from speech for vote reuse

    # ================================================================
    # Public API (called by CognitiveAgent)
    # ================================================================

    def run_speech(
        self,
        obs: Observation,
        memory: Memory,
        is_first_speaker: bool = False,
        is_last_words: bool = False,
        rejection_note: str = "",
    ) -> dict[str, Any]:
        """Generate speech via agent loop (or legacy chain).

        rejection_note: set when the engine rejected the previous speech under
        the honesty rule (report §8.4) — injected so the retry knows why.
        """
        if self._use_agent_loop:
            return self._run_loop_speech(obs, memory, is_first_speaker, is_last_words, rejection_note)
        speech = self._run_legacy_speech(obs, memory, is_first_speaker, is_last_words, rejection_note)
        return {"speech": speech, "reasoning": ""}

    def run_vote(
        self,
        obs: Observation,
        memory: Memory,
        vote_temperature: float | None = None,
    ) -> dict[str, Any]:
        """Generate vote via agent loop (or legacy chain).

        vote_temperature: LLM sampling temperature for vote decisions.
            Derived from agent's MBTI-based courage level. Lower values
            produce more decisive votes, higher values produce more
            exploratory/uncertain votes.
        """
        if self._use_agent_loop:
            return self._run_loop_vote(obs, memory, vote_temperature=vote_temperature)
        return self._run_legacy_vote(obs, memory)

    def run_night(self, obs: Observation, memory: Memory, extra: str = "") -> dict[str, Any]:
        """Generate night action via agent loop (or legacy chain)."""
        if self._use_agent_loop:
            return self._run_loop_night(obs, memory, extra)
        return self._run_legacy_night(obs, memory, extra)

    def run_structured(
        self,
        obs: Observation,
        catalog: ActionCatalog,
        extra: str = "",
        bias_action: str = "talk",
        temperature: float | None = None,
        memory: Memory | None = None,
    ) -> dict[str, Any]:
        """One-shot catalog decision (native FC, then text JSON; one repair)."""
        # 对局日志审计 P0-2：小预算会截断长理由（理由与目标错位的诱因之一）。
        # 上限只是安全阀，模型看不到这个数值，正常回复远达不到 4096。
        max_tokens = 4096
        result = run_structured_decision(
            self._llm,
            self._system_prompt,
            obs,
            catalog,
            extra=extra,
            strategy_bias=self._strategy_bias,
            bias_action=bias_action,
            max_tokens=max_tokens,
            temperature=temperature,
            memory=memory,
        )
        if result.get("action") == "vote_intent" and result.get("target_seat"):
            self._tentative_vote = {"raw": f"{result['target_seat']}号"}
        elif catalog.require_speech:
            self._tentative_vote = {}
        if catalog.require_speech:
            self._cached_analysis = str(result.get("reasoning") or "")
        return result

    def run_vote_line(
        self,
        obs: Observation,
        memory: Memory | None = None,
        extra: str = "",
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """投票专用：严格单行文本「我投给X号，理由是……」。

        目标只从投票声明里的 X 解析；理由文本中提及的其他玩家一律不参与
        目标解析（对局日志审计 P0-3：structured JSON 的 reasoning 与
        target_seat 分离，曾出现"理由说投1号、票投4号"）。解析失败重试
        一次（附格式纠错提示），仍失败则抛错。
        """
        legal_entries = _normalise_legal_targets(getattr(obs, "legal_targets", None))
        labels = [
            f"{entry['seat']}号:{entry['name']}" for entry in legal_entries if entry["seat"] is not None
        ]
        legal_text = "；".join(labels) if labels else "当前合法目标"
        instruction = (
            "【投票输出格式（必须严格遵守）】\n"
            "不要输出 JSON，不要调用工具，不要输出多余内容。\n"
            "你的整个回复必须是单独一行，严格按此格式：\n"
            "我投给X号，理由是<不超过80字的简短理由>\n"
            f"座位号X只能从这些目标中选：{legal_text}。\n"
            "示例：我投给5号，理由是他在发言中前后矛盾且过度引导投票方向。"
        )

        from backend.agents.cognitive.observe import format_observation
        from backend.agents.cognitive.prompts import build_game_context

        parts = [build_game_context(obs), "", format_observation(obs)]
        if memory is not None:
            memory_text = memory.format_for_prompt()
            if memory_text:
                parts.extend(["", memory_text])
        bias = build_strategy_bias_block(self._strategy_bias or {}, "vote")
        if bias:
            parts.extend(["", bias])
        if cached := getattr(self, "_cached_analysis", ""):
            parts.extend(
                [
                    "",
                    f"【上一轮分析】\n{cached}\n"
                    "(这是你上一阶段的旧分析，仅供参考；如果此刻的判断已经变化，"
                    "以本次投票行为准，不要照抄旧结论。",
                ]
            )
        if extra.strip():
            parts.extend(["", extra.strip()])
        parts.extend(["", instruction])
        messages = [
            SystemMessage(content=self._system_prompt),
            HumanMessage(content="\n".join(parts)),
        ]

        last_text = ""
        for attempt in range(2):
            kwargs: dict[str, Any] = {"max_tokens": 4096}
            if temperature is not None:
                kwargs["temperature"] = temperature
            try:
                resp = self._llm.invoke(messages, **kwargs)
            except TypeError:
                resp = self._llm.invoke(messages)
            text = resp.content if hasattr(resp, "content") else str(resp)
            last_text = str(text)[:300]
            parsed = parse_vote_line(text, legal_entries)
            if parsed is None:
                # 兼容回退：老格式 JSON（{"target_seat": ...} / {"target": "X号"}），
                # 离线测试与未按新格式作答的模型仍可产出有效投票。
                parsed = _parse_vote_json_fallback(text, legal_entries)
            if parsed is not None:
                self._cached_analysis = ""
                return parsed
            if attempt == 0:
                messages.append(
                    HumanMessage(content=text if str(text).strip() else "（空回复）")
                )
                messages.append(
                    HumanMessage(
                        content=(
                            "上一次回复格式无效：没有解析出「我投给X号，理由是……」"
                            "且 X 是合法目标的投票行。\n" + instruction
                        )
                    )
                )
        raise RuntimeError(
            f"LLM vote line parse failed after 2 attempts for "
            f"{getattr(obs, 'player_id', '')}: last_response={last_text!r}"
        )

    def direct_call(self, user_prompt: str, max_tokens: int = 4096) -> str:
        """Single LLM call for special actions (shoot, boom, badge transfer)."""
        return self._call_legacy(self._system_prompt, user_prompt, max_tokens=max_tokens)

    def get_tentative_vote(self) -> dict[str, str]:
        """Return the tentative vote captured from the last speech (Plan A optimisation)."""
        return dict(self._tentative_vote)

    # ================================================================
    # Agent Loop (new)
    # ================================================================

    def _run_loop_speech(
        self,
        obs: Observation,
        memory: Memory,
        is_first: bool,
        is_last: bool,
        rejection_note: str = "",
    ) -> dict[str, Any]:
        extra_parts = []
        if is_first:
            extra_parts.append("你是本阶段第一个发言的人")
        if is_last:
            extra_parts.append("这是你的遗言")
        if rejection_note:
            extra_parts.append(
                "【系统驳回提示】你刚才的发言因违反本局诚实规则被系统驳回"
                f"（原因：{rejection_note}）。重新发言时严禁自称预言家/先知、"
                "严禁声称自己查验过任何玩家、严禁给任何人金水或查杀；"
                "请用不涉及预言家身份与查验信息的方式重新表达你的观点。"
            )
        extra = "; ".join(extra_parts) if extra_parts else ""

        loop = AgentLoop(
            self._llm,
            self._system_prompt,
            "speech",
            self._strategy_bias,
            mbti=self._persona_mbti,
            player_id=self._player_id,
            retrieval_policy=self._retrieval_policy,
            feature_flags=self._feature_flags,
            game_id=self._game_id,
        )
        result = loop.run(obs, memory, extra_context=extra)
        speech = result.get("speech", "")
        reasoning = result.get("reasoning", "")
        self._cached_analysis = reasoning
        # ── Optimisation: capture tentative vote from speech for vote-phase reuse ──
        tentative = result.get("tentative_vote", "")
        if tentative and isinstance(tentative, str):
            self._tentative_vote = {"raw": tentative}
        else:
            self._tentative_vote = {}
        return trace_keys.copy_loop_result_keys(result, {"speech": speech, "reasoning": reasoning})

    def _run_loop_vote(
        self,
        obs: Observation,
        memory: Memory,
        vote_temperature: float | None = None,
    ) -> dict[str, Any]:
        loop = AgentLoop(
            self._llm,
            self._system_prompt,
            "vote",
            self._strategy_bias,
            temperature=vote_temperature,
            mbti=self._persona_mbti,
            player_id=self._player_id,
            retrieval_policy=self._retrieval_policy,
            feature_flags=self._feature_flags,
            game_id=self._game_id,
        )
        result = loop.run(obs, memory, cached_analysis=self._cached_analysis)
        self._cached_analysis = ""
        return trace_keys.copy_loop_result_keys(
            result,
            {
                "target": result.get("target", ""),
                "reasoning": result.get("reasoning", ""),
            },
        )

    def _run_loop_night(self, obs: Observation, memory: Memory, extra: str) -> dict[str, Any]:
        loop = AgentLoop(
            self._llm,
            self._system_prompt,
            "night",
            self._strategy_bias,
            mbti=self._persona_mbti,
            player_id=self._player_id,
            retrieval_policy=self._retrieval_policy,
            feature_flags=self._feature_flags,
            game_id=self._game_id,
        )
        result = loop.run(obs, memory, extra_context=extra)
        return trace_keys.copy_loop_result_keys(
            result,
            {
                "target": result.get("target", ""),
                "reasoning": result.get("reasoning", ""),
            },
        )

    # ================================================================
    # Legacy 3-step Chain (fallback, use_agent_loop=False)
    # ================================================================

    def _run_legacy_speech(
        self,
        obs: Observation,
        memory: Memory,
        is_first: bool,
        is_last: bool,
        rejection_note: str = "",
    ) -> str:
        obs_result = self._legacy_observe(obs)
        think_result = self._legacy_think(obs, memory, obs_result)
        return self._legacy_act_speech(obs, think_result, memory, is_first, is_last, rejection_note)

    def _run_legacy_vote(self, obs: Observation, memory: Memory) -> dict[str, str]:
        obs_result = self._legacy_observe(obs)
        think_result = self._legacy_think(obs, memory, obs_result)
        return self._legacy_act_vote(obs, think_result)

    def _run_legacy_night(self, obs: Observation, memory: Memory, extra: str) -> dict[str, str]:
        obs_result = self._legacy_observe(obs)
        think_result = self._legacy_think(obs, memory, obs_result)
        return self._legacy_act_night(obs, think_result, extra)

    def _legacy_observe(self, obs: Observation) -> str:
        prompt = build_observe_prompt(obs)
        return self._call_legacy(
            "你是狼人杀观察者。提取关键信号和事实，不做最终判断。用中文。",
            prompt,
            max_tokens=4096,
        )

    def _legacy_think(self, obs: Observation, memory: Memory, obs_result: str) -> str:
        strategies = []
        if self._feature_flags.get("enable_strategy", True):
            strategies = retrieve_strategies_prod(obs.player_role, obs.phase, situation=obs_result, limit=3)
            if not strategies:
                strategies = retrieve_strategies_tfidf(
                    obs.player_role,
                    obs.phase,
                    situation=obs_result,
                    persona_mbti=self._persona_mbti,
                    persona_style=self._persona_style,
                )
        strategy_text = format_strategies_for_prompt(strategies)
        bias_text = build_strategy_bias_block(self._strategy_bias, "talk")
        prompt = build_think_prompt(obs, memory, strategy_text, bias_text)
        return self._call_legacy(self._system_prompt, prompt, max_tokens=4096)

    def _legacy_act_speech(
        self,
        obs: Observation,
        think_result: str,
        memory: Memory,
        is_first: bool,
        is_last: bool,
        rejection_note: str = "",
    ) -> str:
        prompt = build_speech_prompt(obs, think_result, memory, is_first, is_last)
        if rejection_note:
            prompt += (
                "\n\n【系统驳回提示】你刚才的发言因违反本局诚实规则被系统驳回"
                f"（原因：{rejection_note}）。重新发言时严禁自称预言家/先知、"
                "严禁声称自己查验过任何玩家、严禁给任何人金水或查杀。"
            )
        return self._call_legacy(self._system_prompt, prompt, max_tokens=4096)

    def _legacy_act_vote(self, obs: Observation, think_result: str) -> dict[str, str]:
        prompt = build_vote_prompt(obs, think_result)
        result = self._call_legacy(self._system_prompt, prompt, max_tokens=4096)
        return parse_json_target(result)

    def _legacy_act_night(self, obs: Observation, think_result: str, extra: str) -> dict[str, str]:
        prompt = build_night_prompt(obs, think_result, extra)
        result = self._call_legacy(self._system_prompt, prompt, max_tokens=4096)
        return parse_json_target(result)

    def _call_legacy(
        self,
        system: str,
        user: str,
        max_tokens: int = 4096,
        max_retries: int = 0,
        request: str = "",
        day: int = 0,
        phase: str = "",
    ) -> str:
        last_error: Exception | None = None
        for _attempt in range(max_retries + 1):
            try:
                messages = [
                    SystemMessage(content=system),
                    HumanMessage(content=user),
                ]
                try:
                    resp = self._llm.invoke(messages, max_tokens=max_tokens)
                except TypeError:
                    resp = self._llm.invoke(messages)
                content = resp.content.strip()
                # Save prompt snapshot synchronously
                try:
                    from backend.db.persist import save_prompt_snapshot

                    usage = getattr(resp, "usage_metadata", None) or {}
                    save_prompt_snapshot(
                        game_id=self._game_id,
                        player_id=self._player_id,
                        day=day,
                        phase=phase or request,
                        request=request or "pipeline",
                        system_prompt=system,
                        user_prompt=user,
                        response=content[:5000],
                        prompt_tokens=getattr(usage, "input_tokens", None),
                        completion_tokens=getattr(usage, "output_tokens", None),
                    )
                except Exception:
                    pass
                if content and len(content) > 10:
                    return content
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise RuntimeError("LLM legacy call failed") from last_error
        raise RuntimeError("LLM legacy call returned empty response")


# ============================================================
# Helpers
# ============================================================


def parse_json_target(text: str) -> dict[str, str]:
    try:
        m = re.search(r"\{[^}]+\}", text)
        if m:
            data = json.loads(m.group())
            return {
                "target": data.get("target", ""),
                "reasoning": data.get("reasoning", ""),
            }
    except (json.JSONDecodeError, KeyError):
        pass
    return {"target": "", "reasoning": text[:100]}


def parse_json_array(text: str) -> list[str]:
    try:
        m = re.search(r"\[.*?\]", text, re.DOTALL)
        if m:
            data = json.loads(m.group())
            if isinstance(data, list):
                return [str(item) for item in data if item]
        return [text.strip()]
    except (json.JSONDecodeError, KeyError):
        quoted = re.findall(r'"([^"]*)"', text)
        if quoted:
            return quoted
        return [text.strip()]


# ============================================================
# 严格单行投票解析（对局日志审计 P0-3）
# ============================================================


def _normalise_legal_targets(legal_targets: Any) -> list[dict[str, Any]]:
    """Observation.legal_targets 可能是 dict 或 PlayerInfo，统一为 dict。"""
    entries: list[dict[str, Any]] = []
    for target in list(legal_targets or []):
        if isinstance(target, dict):
            entries.append(
                {"seat": target.get("seat"), "name": str(target.get("name") or ""), "id": str(target.get("id") or "")}
            )
        else:
            entries.append(
                {
                    "seat": getattr(target, "seat", None),
                    "name": str(getattr(target, "name", "") or ""),
                    "id": str(getattr(target, "id", "") or ""),
                }
            )
    return entries


def _parse_vote_json_fallback(text: str, legal_entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """老格式 JSON 兜底：{"target_seat": N, ...} 或 {"target": "N号"/"名字"}。"""
    cleaned = str(text or "").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    reasoning = str(data.get("reasoning") or data.get("reason") or "").strip()
    seat = data.get("target_seat")
    if seat is None:
        raw_target = str(data.get("target") or "").strip()
        m = _VOTE_LINE_ANY.search(raw_target)
        if m:
            seat = m.group(1)
        else:
            for entry in legal_entries:
                if entry["name"] and entry["name"] in raw_target:
                    return {"target_seat": entry["seat"], "reasoning": reasoning[:300] or "legacy_json_vote"}
            return None
    seat_text = str(seat).translate(_FULLWIDTH_DIGITS)
    digits = re.sub(r"\D", "", seat_text)
    if not digits:
        return None
    seat_num = int(digits)
    for entry in legal_entries:
        if entry["seat"] is not None and int(entry["seat"]) == seat_num:
            return {"target_seat": seat_num, "reasoning": reasoning[:300] or "legacy_json_vote"}
    return None


# 投票严格行格式：模型必须输出一行「我投给X号，理由是……」，目标只从
# 该句的 X 解析。行首锚定优先，其次取全文最后一次「投给X号」声明；
# 理由文本中提及的其他玩家一律不参与目标解析。
_VOTE_LINE_ANCHOR = re.compile(r"^\s*我\s*投(?:票)?给?\s*([0-9０-９]{1,2})\s*号", re.MULTILINE)
_VOTE_LINE_ANY = re.compile(r"投(?:票)?给?\s*([0-9０-９]{1,2})\s*号")
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def parse_vote_line(text: str, legal_targets: list[dict[str, Any]]) -> dict[str, Any] | None:
    """从严格投票行解析 target_seat 与 reasoning；非法座位返回 None。

    只信任「投给X号」声明中的座位号 X；reasoning 里出现的其他玩家
    一律不参与目标解析。
    """
    cleaned = str(text or "").strip()
    # 去掉常见格式噪音（DECISION: 前缀 / 代码围栏）
    cleaned = re.sub(r"^\s*(?:DECISION|ANSWER|最终决策)\s*[:：]\s*", "", cleaned, flags=re.I)
    cleaned = cleaned.replace("```json", "").replace("```", "").strip()
    if not cleaned:
        return None

    by_seat = {
        int(str(t.get("seat")).translate(_FULLWIDTH_DIGITS)): t
        for t in legal_targets
        if t.get("seat") is not None and str(t.get("seat", "")).strip().isdigit()
    }

    matches: list[re.Match[str]] = []
    anchor = _VOTE_LINE_ANCHOR.search(cleaned)
    if anchor is not None:
        matches.append(anchor)
    any_matches = list(_VOTE_LINE_ANY.finditer(cleaned))
    if any_matches:
        matches.append(any_matches[-1])
    for match in matches:
        seat = int(match.group(1).translate(_FULLWIDTH_DIGITS))
        target = by_seat.get(seat)
        if target is None:
            continue
        reasoning = cleaned[match.end():].strip()
        reasoning = re.sub(r"^[\s，。,；;：:\-—·]+", "", reasoning)
        reasoning = re.sub(r"^(?:理由是|理由|因为)[\s：:]?", "", reasoning)
        if not reasoning:
            reasoning = cleaned[:300]
        return {"target_seat": seat, "reasoning": reasoning[:300], "target": target}
    return None
