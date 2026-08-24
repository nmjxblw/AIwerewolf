"""全量对局文本导出（廉价磋商研究用）。

研究重点是廉价磋商（cheap talk），因此每局导出完整可读的主持人视角
对局文本：夜间行动（含狼队私聊/刀票理由）、白天发言全文、投票票型
与理由、死亡与胜负。事件顺序即真实发生顺序，不做二次加工。

两个入口：
  build_transcript(state)          — 对局刚结束时，从 GameState 直接渲染
  build_transcript_from_log(log)   — 从已保存的 events.json（moderator_dict
                                     内容）重建，用于批量刷新历史对局文本
"""

from __future__ import annotations

from typing import Any
from typing import Iterable

from backend.engine.models import EventType
from backend.engine.models import GameState

_ROLE_CN = {
    "Werewolf": "狼人",
    "Villager": "平民",
    "Seer": "预言家",
    "Witch": "女巫",
    "Guard": "守卫",
    "Hunter": "猎人",
    "WhiteWolfKing": "白狼王",
    "Idiot": "白痴",
}


def _player_row(player: Any) -> dict[str, Any]:
    """统一 Player 对象与 private_dict 两种来源的玩家表示。"""
    if isinstance(player, dict):
        return {
            "seat": int(player.get("seat") or 0),
            "name": str(player.get("name") or "?"),
            "role": str(player.get("role") or "?"),
            "alive": bool(player.get("alive")),
            "death_day": player.get("death_day"),
            "death_reason": player.get("death_reason"),
        }
    return {
        "seat": player.seat,
        "name": player.name,
        "role": player.role.value,
        "alive": player.alive,
        "death_day": player.death_day,
        "death_reason": player.death_reason,
    }


def _index_players(players: Iterable[Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for player in players:
        row = _player_row(player)
        if isinstance(player, dict):
            pid = str(player.get("id") or "")
        else:
            pid = str(player.id)
        if pid:
            index[pid] = row
    return index


def _label(index: dict[str, dict[str, Any]], player_id: Any) -> str:
    """带角色标签（主持人视角）。"""
    row = index.get(str(player_id or ""))
    if row is None:
        return str(player_id or "?")
    return f"{row['seat']}号:{row['name']}({row['role']})"


def _public_label(index: dict[str, dict[str, Any]], player_id: Any) -> str:
    """不带角色（公开视角标签，如投票人）。"""
    row = index.get(str(player_id or ""))
    if row is None:
        return str(player_id or "?")
    return f"{row['seat']}号:{row['name']}"


def render_transcript(
    events: list[dict[str, Any]],
    players_index: dict[str, dict[str, Any]],
    *,
    title: str = "",
    meta: dict[str, Any] | None = None,
) -> str:
    """把事件字典列表渲染成 markdown 全量对局文本（主持人视角）。"""
    lines: list[str] = [f"# {title or '狼人杀对局全量文本'}", ""]

    if meta:
        for key, value in meta.items():
            lines.append(f"- {key}: {value}")
        lines.append("")

    # 上帝视角座位表
    lines.append("## 座位与角色（主持人视角）")
    lines.append("")
    ordered = sorted(players_index.values(), key=lambda r: r["seat"])
    for row in ordered:
        fate = "存活" if row["alive"] else f"第{row['death_day']}夜/日死亡({row['death_reason']})"
        lines.append(f"- {row['seat']}号 {row['name']} — {_ROLE_CN.get(row['role'], row['role'])} — {fate}")
    lines.append("")

    lines.append("## 对局过程")
    lines.append("")

    current_day = None
    for event in events:
        etype = event.get("type", "")
        payload = event.get("payload", {}) or {}
        day = event.get("day", 0)
        if day != current_day:
            current_day = day
            lines.append(f"### 第 {day} 天/夜")
            lines.append("")

        if etype == EventType.SYSTEM_MESSAGE.value:
            lines.append(f"[系统] {payload.get('message', '')}")
        elif etype == EventType.CHAT_MESSAGE.value:
            speaker = _public_label(players_index, payload.get("actor_id"))
            text = str(payload.get("speech", payload.get("message", ""))).strip()
            tag = "（遗言）" if payload.get("last_words") else ""
            tag += "（PK发言）" if payload.get("pk_speech") else ""
            reason = str(payload.get("reasoning", "")).strip()
            reason_part = f"｜（内心理由：{reason}）" if reason else ""
            lines.append(f"**{speaker} 发言{tag}**：{text}{reason_part}")
        elif etype == EventType.VOTE_CAST.value:
            voter = _public_label(players_index, payload.get("voter_id"))
            target = _public_label(players_index, payload.get("target_id"))
            reason = str(payload.get("reasoning", "")).strip()
            lines.append(f"[投票] {voter} → {target}｜理由：{reason}")
        elif etype == EventType.PLAYER_DIED.value:
            who = _label(players_index, payload.get("player_id"))
            lines.append(f"[死亡] {who}｜原因：{payload.get('reason', '?')}")
        elif etype == EventType.NIGHT_ACTION.value:
            # "行动完毕" 阶段占位事件（无 actor_id）不是真实行动，跳过
            if not payload.get("actor_id"):
                continue
            actor = _public_label(players_index, payload.get("actor_id"))
            action = str(payload.get("action_type", ""))
            target = _label(players_index, payload.get("target_id"))
            reason = str(payload.get("reasoning", "")).strip()
            ignored = "（无效/被忽略）" if payload.get("ignored") else ""
            extra = ""
            if payload.get("kind") == "wolf_attack_vote":
                extra = " ｜当前狼队票: " + ", ".join(
                    f"{_public_label(players_index, v)}→{_public_label(players_index, t) or '空刀'}"
                    for v, t in (payload.get("current_votes") or {}).items()
                )
            lines.append(
                f"[夜行动] {actor} {action} → {target or '（无目标/空刀）'}{ignored}｜理由：{reason}{extra}"
            )
        elif etype == EventType.PRIVATE_INFO.value:
            kind = str(payload.get("kind", ""))
            if kind == "wolf_chat_message":
                lines.append(f"[狼队私聊] {payload.get('message', '')}")
            elif kind == "wolf_chat_start":
                lines.append("[狼队私聊] —— 狼队夜间讨论开始 ——")
            elif kind == "wolf_attack_tally":
                votes = payload.get("votes") or {}
                vote_str = ", ".join(
                    f"{_public_label(players_index, v)}→{_public_label(players_index, t) or '空刀'}"
                    for v, t in votes.items()
                )
                lines.append(f"[狼队计票] {payload.get('message', '')}｜明细: {vote_str}")
            elif kind == "seer_result":
                lines.append(f"[预言家查验] {payload.get('message', '')}（仅预言家可见）")
            else:
                lines.append(f"[私密信息] {payload.get('message', '')}")
        elif etype == EventType.GAME_END.value:
            lines.append(f"[游戏结束] 胜者: {payload.get('winner')}｜原因: {payload.get('reason')}")
        elif etype in {EventType.HUNTER_SHOT.value, EventType.WHITE_WOLF_KING_BOOM.value}:
            lines.append(f"[{etype}] {payload}")
        lines.append("")

    return "\n".join(lines)


def build_transcript(state: GameState, *, title: str = "", meta: dict[str, Any] | None = None) -> str:
    """对局刚结束时，从 GameState 渲染全量对局文本。"""
    events = [event.to_dict() for event in state.events]
    players_index = _index_players(state.players)
    return render_transcript(events, players_index, title=title, meta=meta)


def build_transcript_from_log(
    log: dict[str, Any],
    *,
    title: str = "",
    meta: dict[str, Any] | None = None,
) -> str:
    """从已保存的 events.json（moderator_dict 内容）重建对局文本。"""
    events = list(log.get("events") or [])
    players_index = _index_players(log.get("players") or [])
    return render_transcript(events, players_index, title=title, meta=meta)
