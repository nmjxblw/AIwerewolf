"""Structured observation of the game state.

Single Responsibility: extract what the agent can legitimately see
from the raw PlayerView. No judgments, no analysis — pure fact extraction.

Now integrates BeliefTracker: stateful tracking of role claims, contradictions,
and voting patterns across rounds. Mirrors BeliefState from llm_agent.py
but optimized for the cognitive pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from backend.engine.visibility import PlayerView

# ============================================================
# Structured observation types
# ============================================================


@dataclass
class PlayerInfo:
    """玩家公开信息。"""

    id: str
    """玩家唯一 ID"""
    name: str
    """玩家名称"""
    seat: int
    """座位号（1-based）"""
    alive: bool
    """是否存活"""
    role: str = "unknown"
    """公开可见的角色（通常为 unknown，死亡翻牌后可见）"""


@dataclass
class SpeechInfo:
    """一条玩家发言记录。"""

    player_id: str
    """发言者 ID"""
    player_name: str
    """发言者名称"""
    seat: int
    """发言者座位号"""
    content: str
    """发言内容原文"""


@dataclass
class VoteInfo:
    """一条投票记录。"""

    voter_id: str
    """投票者 ID"""
    voter_name: str
    """投票者名称"""
    target_id: str
    """被投票玩家 ID"""
    target_name: str
    """被投票玩家名称"""
    day: int = 0
    """投票所在天数"""


@dataclass
class DeathInfo:
    """一条玩家死亡记录。"""

    player_id: str
    """死亡玩家 ID"""
    player_name: str
    """死亡玩家名称"""
    seat: int
    """死亡玩家座位号"""
    cause: str
    """死因：werewolf_killed(狼杀) / voted_out(投票放逐) / witch_killed(女巫毒杀) / hunter_killed(猎人开枪) / white_wolf_king_boom(白狼王自爆) / guard_witch_conflict(奶穿)"""
    revealed_role: str = ""
    """死亡翻牌时揭示的角色（空字符串表示未翻牌）"""


@dataclass
class RoleClaim:
    """从发言或系统事件中提取的角色声称。"""

    player_name: str
    """声称者名称"""
    player_id: str
    """声称者 ID"""
    seat: int
    """声称者座位号"""
    claimed_role: str
    """声称的角色名（预言家/女巫/猎人/守卫）"""
    day: int
    """声称所在天数"""
    context: str
    """声称语境：day_speech(白天发言) / revealed_on_death(死亡翻牌)"""


@dataclass
class Contradiction:
    """检测到的矛盾（例如多人声称同一唯一角色）。"""

    role: str
    """被声称的角色"""
    claimants: List[str]
    """声称该角色的玩家名称列表"""
    description: str
    """矛盾的人类可读描述"""


# ============================================================
# BeliefTracker — stateful game-state tracker
# ============================================================


class BeliefTracker:
    """Stateful tracker of game knowledge across rounds.

    Extracts and maintains:
    - Role claims (who claimed what, when)
    - Contradictions (multiple claims of unique roles)
    - Voting patterns (who voted for whom)
    - Death history with roles

    Designed to be lightweight and embeddable in the Observation pipeline.
    """

    def __init__(self):
        self.claims: List[RoleClaim] = []
        self.votes: List[VoteInfo] = []
        self.deaths: List[DeathInfo] = []
        self.contradictions: List[Contradiction] = []
        self._unique_roles = {
            "预言家",
            "女巫",
            "猎人",
            "守卫",
            "Seer",
            "Witch",
            "Hunter",
            "Guard",
        }
        self._processed_speech_ids: set = set()
        self._seen_vote_keys: set = set()
        self._seen_death_keys: set = set()

    def update(self, view: Any) -> None:
        """Update tracker from current PlayerView."""
        self._extract_claims(view)
        self._extract_votes(view)
        self._extract_deaths(view)
        self._detect_contradictions()

    # ---- Extraction ----

    def _extract_claims(self, view: Any) -> None:
        """Extract role claims from speeches and system events."""
        for e in view.public_events:
            etype = e.get("type", "")
            payload = e.get("payload", {}) or {}
            day = e.get("day", 0)
            actor_id = e.get("actor_id", "")
            actor = _find_player(view, actor_id)

            if etype == "CHAT_MESSAGE":
                speech = payload.get("speech", "")
                claimed = _detect_role_claim(speech)
                if claimed and claimed in self._unique_roles:
                    # Avoid duplicates from same speech
                    speech_key = f"{actor_id}:{speech[:50]}"
                    if speech_key in self._processed_speech_ids:
                        continue
                    self._processed_speech_ids.add(speech_key)
                    self.claims.append(
                        RoleClaim(
                            player_name=actor.get("name", actor_id),
                            player_id=actor_id,
                            seat=actor.get("seat", 0),
                            claimed_role=claimed,
                            day=day,
                            context="day_speech",
                        )
                    )

            elif etype == "PLAYER_DIED":
                revealed = payload.get("role", "")
                if revealed:
                    pid = payload.get("player_id", "")
                    dead = _find_player(view, pid)
                    self.claims.append(
                        RoleClaim(
                            player_name=dead.get("name", pid),
                            player_id=pid,
                            seat=dead.get("seat", 0),
                            claimed_role=revealed,
                            day=day,
                            context="revealed_on_death",
                        )
                    )

    def _extract_votes(self, view: Any) -> None:
        """Extract votes from public events.

        Deduplicates: a (voter_id, target_id, day) key prevents the same
        vote from being recorded twice when update() runs on every _observe() call.
        """
        for e in view.public_events:
            if e.get("type") == "VOTE_CAST" and e.get("day") == view.day:
                payload = e.get("payload", {}) or {}
                voter_id = e.get("actor_id", "")
                target_id = payload.get("target_id", "")
                day = e.get("day", view.day)
                vote_key = (voter_id, target_id, day)
                if vote_key in self._seen_vote_keys:
                    continue
                self._seen_vote_keys.add(vote_key)
                voter = _find_player(view, voter_id)
                target = _find_player(view, target_id)
                self.votes.append(
                    VoteInfo(
                        voter_id=voter_id,
                        voter_name=voter.get("name", ""),
                        target_id=target_id,
                        target_name=target.get("name", ""),
                        day=day,
                    )
                )

    def _extract_deaths(self, view: Any) -> None:
        """Extract deaths from public events. Deduplicates by (player_id, cause)."""
        for e in view.public_events:
            if e.get("type") == "PLAYER_DIED":
                payload = e.get("payload", {}) or {}
                pid = payload.get("player_id", "")
                cause = payload.get("cause", payload.get("reason", "unknown"))
                death_key = (pid, cause)
                if death_key in self._seen_death_keys:
                    continue
                self._seen_death_keys.add(death_key)
                dead = _find_player(view, pid)
                self.deaths.append(
                    DeathInfo(
                        player_id=pid,
                        player_name=dead.get("name", pid),
                        seat=dead.get("seat", 0),
                        cause=cause,
                        revealed_role=payload.get("role", ""),
                    )
                )

    # ---- Contradiction detection ----

    def _detect_contradictions(self) -> None:
        """Detect multiple claims of the same unique role."""
        self.contradictions = []
        claims_by_role: Dict[str, List[RoleClaim]] = {}
        for c in self.claims:
            role = c.claimed_role
            if role not in claims_by_role:
                claims_by_role[role] = []
            claims_by_role[role].append(c)

        for role, role_claims in claims_by_role.items():
            if len(role_claims) >= 2:
                names = list({c.player_name for c in role_claims})
                if len(names) >= 2:
                    self.contradictions.append(
                        Contradiction(
                            role=role,
                            claimants=names,
                            description=f"多人声称是{role}: {', '.join(names)}",
                        )
                    )

    # ---- Formatting ----

    def format_for_prompt(self) -> str:
        """Format tracker state as prompt text."""
        parts = []

        if self.claims:
            lines = ["=== 角色声称 ==="]
            for c in self.claims[-8:]:
                lines.append(
                    f"  {c.seat}号:{c.player_name} 声称是 {c.claimed_role} (D{c.day}, {c.context})"
                )
            parts.append("\n".join(lines))

        if self.contradictions:
            lines = ["=== 矛盾 ==="]
            for c in self.contradictions:
                lines.append(f"  {c.description}")
            parts.append("\n".join(lines))

        if self.votes:
            latest_day = max(v.day for v in self.votes) if self.votes else 0
            day_votes = [v for v in self.votes if v.day == latest_day]
            if day_votes:
                lines = [f"=== D{latest_day} 投票 ==="]
                for v in day_votes:
                    lines.append(f"  {v.voter_name} -> {v.target_name}")
                parts.append("\n".join(lines))

        return "\n\n".join(parts)

    def clear_round(self) -> None:
        """Clear per-round data (claims/votes persist across rounds)."""
        pass  # Claims/votes accumulate usefully


# ============================================================
# Observation — what the agent sees right now
# ============================================================


@dataclass
class Observation:
    """Agent 当前可见的游戏状态结构化提取。

    这是观察层的唯一输出。下游所有模块（推理、决策）都消费这个结构，
    不直接消费原始 PlayerView。

    构建入口：observe(view, role, tracker) → Observation
    """

    # ── 身份 ──
    player_id: str
    """玩家唯一 ID"""
    player_name: str
    """玩家名称"""
    player_seat: int
    """玩家座位号（1-based）"""
    player_role: str
    """玩家身份（Werewolf / Seer / Witch / Villager 等）"""

    # ── 游戏状态 ──
    day: int
    """当前天数（0 = 第0夜准备阶段）"""
    phase: str
    """当前阶段名（NIGHT_WOLF_ACTION / DAY_SPEECH / DAY_VOTE 等）"""

    # ── 玩家列表 ──
    alive: List[PlayerInfo] = field(default_factory=list)
    """存活玩家列表"""
    dead: List[PlayerInfo] = field(default_factory=list)
    """死亡玩家列表（含死亡翻牌角色信息）"""
    legal_targets: List[PlayerInfo] = field(default_factory=list)
    """当前行动合法目标列表（由引擎根据阶段和角色计算）"""

    # ── 本轮事件 ──
    speeches: List[SpeechInfo] = field(default_factory=list)
    """本轮（当天+当前阶段）所有玩家发言记录"""
    votes: List[VoteInfo] = field(default_factory=list)
    """本轮投票记录"""

    # ── 历史 ──
    deaths: List[DeathInfo] = field(default_factory=list)
    """全局死亡记录（含死因和翻牌角色）"""

    # ── 私有信息（角色专属） ──
    private: Dict[str, Any] = field(default_factory=dict)
    """角色私有信息。狼人含 known_wolves（狼队友列表），预言家含 seer_check（查验结果），女巫含 witch_victim（被刀玩家）"""

    # ── 社交信号 ──
    mentioned_by: List[str] = field(default_factory=list)
    """本轮发言中提到当前玩家的其他玩家名称列表"""
    adjacent_dead: List[str] = field(default_factory=list)
    """座位相邻的死亡玩家名称列表（用于生成\"邻座死亡\"视角提示）"""

    # ── 游戏配置 ──
    role_roster: List[str] = field(default_factory=list)
    """本局实际角色清单（来自游戏引擎配置，非玩家声称）。
    用于 prompt 构建中的规则摘要，例如判断本局是否有预言家/女巫/守卫/猎人。"""
    has_badge: bool = True
    """本局是否有警长/警徽机制（来自前端 toggle）。False 时 prompt 不显示警长相关内容。"""

    # ── BeliefTracker 输出 ──
    role_claims: List[RoleClaim] = field(default_factory=list)
    """全局角色声称记录（来自 BeliefTracker 的状态追踪）"""
    contradictions: List[Contradiction] = field(default_factory=list)
    """检测到的角色声称矛盾（如多人声称预言家）"""
    belief_summary: str = ""
    """BeliefTracker 生成的局势摘要文本（直接注入 prompt）"""


def observe(
    view: PlayerView, role: str, tracker: Optional[BeliefTracker] = None
) -> Observation:
    """Build an Observation from a PlayerView.

    Args:
        view: PlayerView from game engine
        role: Agent's role string
        tracker: Optional BeliefTracker for stateful claim/contradiction tracking.
                 If provided, it is updated and its output merged into the Observation.

    This is the ONLY public function in this module.
    Pure extraction — no logic, no judgments.
    """
    obs = Observation(
        player_id=view.self_player["id"],
        player_name=view.self_player.get("name", ""),
        player_seat=view.self_player.get("seat", 0),
        player_role=role,
        day=view.day,
        phase=view.phase,
    )

    # Players
    for p in view.players:
        info = PlayerInfo(
            id=p["id"],
            name=p.get("name", ""),
            seat=p.get("seat", 0),
            alive=p["alive"],
            role=p.get("role", "unknown"),
        )
        (obs.alive if p["alive"] else obs.dead).append(info)

    for p in getattr(view, "legal_targets", []):
        obs.legal_targets.append(
            PlayerInfo(
                id=p["id"],
                name=p.get("name", ""),
                seat=p.get("seat", 0),
                alive=p.get("alive", True),
            )
        )

    # Today's speeches
    for e in view.public_events:
        if e.get("type") == "CHAT_MESSAGE" and e.get("day") == view.day:
            payload = e.get("payload", {}) or {}
            actor = _find_player(view, e.get("actor_id", ""))
            obs.speeches.append(
                SpeechInfo(
                    player_id=e.get("actor_id", ""),
                    player_name=actor.get("name", ""),
                    seat=actor.get("seat", 0),
                    content=payload.get("speech", ""),
                )
            )

        elif e.get("type") == "VOTE_CAST" and e.get("day") == view.day:
            payload = e.get("payload", {}) or {}
            voter = _find_player(view, e.get("actor_id", ""))
            target = _find_player(view, payload.get("target_id", ""))
            obs.votes.append(
                VoteInfo(
                    voter_id=e.get("actor_id", ""),
                    voter_name=voter.get("name", ""),
                    target_id=payload.get("target_id", ""),
                    target_name=target.get("name", ""),
                    day=e.get("day", view.day),
                )
            )

        elif e.get("type") == "PLAYER_DIED":
            payload = e.get("payload", {}) or {}
            dead = _find_player(view, payload.get("player_id", ""))
            obs.deaths.append(
                DeathInfo(
                    player_id=payload.get("player_id", ""),
                    player_name=dead.get("name", ""),
                    seat=dead.get("seat", 0),
                    cause=payload.get("cause", payload.get("reason", "unknown")),
                    revealed_role=payload.get("role", ""),
                )
            )

    # Private info
    known_wolves = list(getattr(view, "known_wolves", []) or [])
    if known_wolves:
        obs.private["known_wolves"] = [
            f"{wolf.get('seat', '?')}号:{wolf.get('name', wolf.get('id', '?'))}"
            for wolf in known_wolves
        ]
    for e in view.private_events:
        payload = e.get("payload", {}) or {}
        if payload.get("kind") == "seer_result":
            obs.private["seer_check"] = payload
        if "check_result" in payload:
            obs.private["seer_check"] = payload
        if "victim_id" in payload:
            obs.private["witch_victim"] = payload

    # Social signals
    my_seat = f"@{obs.player_seat}号"
    for s in obs.speeches:
        if my_seat in s.content:
            obs.mentioned_by.append(s.player_name)

    total_seats = len(view.players)
    for d in obs.deaths:
        diff = abs(d.seat - obs.player_seat)
        if diff == 1 or diff == total_seats - 1:
            obs.adjacent_dead.append(d.player_name)

    # Role roster (actual game config, not claimed roles)
    obs.role_roster = list(getattr(view, "role_roster", []) or [])
    obs.has_badge = bool(getattr(view, "has_badge", True))

    # Belief tracker integration
    if tracker is not None:
        tracker.update(view)
        obs.role_claims = tracker.claims[:]
        obs.contradictions = tracker.contradictions[:]
        obs.belief_summary = tracker.format_for_prompt()

    return obs


def format_observation(obs: Observation) -> str:
    """Format Observation into text for LLM consumption."""
    lines = [
        "=== 当前状态 ===",
        f"你是 {obs.player_seat}号:{obs.player_name}，身份={obs.player_role}",
        f"第{obs.day}天 / {obs.phase}阶段",
        "",
        f"存活：{'，'.join(f'{p.seat}号:{p.name}' for p in obs.alive)}",
        f"死亡：{'，'.join(f'{p.seat}号:{p.name}' for p in obs.dead) or '无'}",
    ]

    if obs.legal_targets:
        lines.append(
            f"合法目标：{'，'.join(f'{p.seat}号:{p.name}' for p in obs.legal_targets)}"
        )

    if obs.speeches:
        lines.append("\n=== 今日发言 ===")
        for s in obs.speeches[-8:]:
            lines.append(f"  {s.seat}号:{s.player_name}：{s.content[:200]}")

    if obs.votes:
        lines.append("\n=== 今日投票 ===")
        for v in obs.votes:
            lines.append(f"  {v.voter_name} -> {v.target_name}")

    if obs.deaths:
        lines.append("\n=== 淘汰记录 ===")
        for d in obs.deaths:
            role_str = f"({d.revealed_role})" if d.revealed_role else ""
            lines.append(f"  第{d.seat}号:{d.player_name} {role_str} | 原因：{d.cause}")

    if obs.role_claims:
        lines.append("\n=== 角色声称 ===")
        for c in obs.role_claims[-6:]:
            lines.append(f"  {c.seat}号:{c.player_name} -> {c.claimed_role} (D{c.day})")

    if obs.contradictions:
        lines.append("\n=== 矛盾 ===")
        for c in obs.contradictions:
            lines.append(f"  {c.description}")

    if obs.mentioned_by:
        lines.append(f"\n你被 {', '.join(obs.mentioned_by)} 点名提到")

    if obs.adjacent_dead:
        lines.append(f"你和 {', '.join(obs.adjacent_dead)} 座位相邻")

    if obs.private:
        lines.append("\n=== 私有信息 ===")
        for k, v in obs.private.items():
            lines.append(f"  {k}: {v}")

    return "\n".join(lines)


# ============================================================
# Internal helpers
# ============================================================


def _find_player(view: Any, player_id: str) -> dict:
    """Find player dict by id."""
    for p in view.players:
        if p["id"] == player_id:
            return p
    return {"id": player_id, "name": player_id, "seat": 0, "alive": False}


def _detect_role_claim(speech: str) -> Optional[str]:
    """Detect if a speech contains a role claim. Returns role name or None."""
    patterns = [
        (
            r"(?:我是|我就是|我是真的)\s*(?:一个\s*)?(预言家|女巫|猎人|守卫|村民|白狼王|狼人)",
            1,
        ),
        (r"(?:跳|报)\s*(?:一个\s*)?(预言家|女巫|猎人|守卫)", 1),
        (r"(?:身份.*?是|底牌.*?是)\s*(预言家|女巫|猎人|守卫|村民|白狼王|狼人)", 1),
    ]
    for pattern, group in patterns:
        m = re.search(pattern, speech)
        if m:
            return m.group(group)
    return None
