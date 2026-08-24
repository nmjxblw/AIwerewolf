"""廉价磋商实验结果汇总分析。

读取 experiments/ct_*/game_seed*_result.json + events.json，输出：
  - 各组胜率 / 平均天数 / 调用与 token 成本
  - 第 1 天票型聚合（放逐目标角色分布、放逐狼比例）
  - 战术显形率（假跳次数、空刀/自刀夜数、预言家隐藏是否生效）
  - 预言家存活率
用法: python scripts/analyze_cheap_talk.py [--include-seed100]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXP = ROOT / "experiments"

GROUPS = [
    ("B", "ct_B_baseline", "baseline 无战术"),
    ("WJ", "ct_WJ_wolf_jump", "狼人悍跳+空刀/自刀"),
    ("VJ", "ct_VJ_villager_jump", "平民跳预言家挡刀"),
    ("SQ", "ct_SQ_seer_quiet", "预言家不发言"),
]


def load_group(dir_name: str, include_seed100: bool) -> list[dict]:
    rows = []
    for f in sorted(EXP.glob(f"{dir_name}/game_seed*_result.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        if not include_seed100 and data.get("seed") == 100:
            continue
        events_path = f.parent / f"game_seed{data['seed']}_events.json"
        data["_events"] = json.loads(events_path.read_text(encoding="utf-8")) if events_path.exists() else {}
        rows.append(data)
    return rows


def day1_exile_role(game: dict) -> str:
    """第 1 天放逐目标的角色（从 events 的 day_history 或死亡事件推断）。"""
    events = game["_events"].get("events", [])
    for e in events:
        if e.get("day") == 1 and e.get("type") == "PLAYER_DIED" and e.get("payload", {}).get("reason") == "vote":
            pid = e["payload"]["player_id"]
            for p in game["_events"].get("players", []):
                if p.get("id") == pid:
                    return p.get("role", "?")
    return "无放逐"


def analyze_group(label: str, dir_name: str, desc: str, include_seed100: bool) -> dict:
    games = [g for g in load_group(dir_name, include_seed100) if g.get("winner") != "error"]
    n = len(games)
    if not n:
        return {"label": label, "n": 0}
    village = sum(1 for g in games if g["winner"] == "village")
    exile_roles = Counter(day1_exile_role(g) for g in games)
    return {
        "label": label,
        "desc": desc,
        "n": n,
        "village_wins": village,
        "village_rate_pct": round(village / n * 100, 1),
        "avg_days": round(sum(g["days"] for g in games) / n, 1),
        "day1_exile_roles": dict(exile_roles),
        "day1_exiled_wolf_pct": round(
            sum(1 for g in games if day1_exile_role(g) == "Werewolf") / n * 100, 1
        ),
        "day1_nonseer_claims_total": sum(g.get("day1_nonseer_claims", 0) for g in games),
        "day1_claim_games": sum(1 for g in games if g.get("day1_nonseer_claims", 0) > 0),
        "empty_knife_nights": sum(g.get("empty_knife_nights", 0) for g in games),
        "self_knife_nights": sum(g.get("self_knife_nights", 0) for g in games),
        "seer_alive_pct": round(sum(1 for g in games if g.get("seer_alive")) / n * 100, 1),
        "witch_save_total": sum(g.get("witch_save_used", 0) for g in games),
        "witch_poison_total": sum(g.get("witch_poison_used", 0) for g in games),
        "wolf_chat_avg": round(sum(g.get("wolf_chat_messages", 0) for g in games) / n, 1),
        "avg_llm_calls": round(sum(g.get("llm_calls", 0) for g in games) / n, 1),
        "total_tokens": sum(g.get("prompt_tokens", 0) + g.get("completion_tokens", 0) for g in games),
        "avg_elapsed_s": round(sum(g.get("elapsed", 0) for g in games) / n, 1),
        "seeds": [g["seed"] for g in games],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--include-seed100", action="store_true", help="把冒烟局 seed100 计入 baseline")
    args = parser.parse_args()

    out = []
    for label, dir_name, desc in GROUPS:
        stats = analyze_group(label, dir_name, desc, args.include_seed100)
        out.append(stats)

    lines = ["# 廉价磋商实验汇总", ""]
    header = f"{'组':<4}{'说明':<18}{'局数':<5}{'好人胜':<7}{'胜率%':<7}{'均天数':<7}{'D1放逐狼%':<10}{'假跳局':<7}{'空刀夜':<7}{'自刀夜':<7}{'预言家存活%':<10}"
    lines.append("```")
    lines.append(header)
    for s in out:
        if s.get("n", 0) == 0:
            lines.append(f"{s['label']:<5}(无数据)")
            continue
        lines.append(
            f"{s['label']:<5}{s['desc']:<17}{s['n']:<6}{s['village_wins']:<8}{s['village_rate_pct']:<8}"
            f"{s['avg_days']:<7}{s['day1_exiled_wolf_pct']:<11}{s['day1_claim_games']:<8}"
            f"{s['empty_knife_nights']:<8}{s['self_knife_nights']:<8}{s['seer_alive_pct']:<11}"
        )
    lines.append("```")
    lines.append("")
    for s in out:
        if s.get("n", 0) == 0:
            continue
        lines.append(f"## {s['label']} — {s['desc']}")
        lines.append(f"- 局数 {s['n']}（seeds {s['seeds']}），好人胜 {s['village_wins']}（{s['village_rate_pct']}%），均 {s['avg_days']} 天")
        lines.append(f"- D1 放逐角色分布: {s['day1_exile_roles']}｜D1 放逐到狼比例 {s['day1_exiled_wolf_pct']}%")
        lines.append(
            f"- 假跳（非预言家声称）总次数 {s['day1_nonseer_claims_total']}，出现假跳的局数 {s['day1_claim_games']}；"
            f"空刀夜 {s['empty_knife_nights']}，自刀夜 {s['self_knife_nights']}"
        )
        lines.append(
            f"- 女巫用解药 {s['witch_save_total']} 次/毒药 {s['witch_poison_total']} 次；"
            f"狼私聊均 {s['wolf_chat_avg']} 条；预言家存活到终局 {s['seer_alive_pct']}%"
        )
        lines.append(
            f"- 成本：均 {s['avg_llm_calls']} 次调用/局，总 tokens {s['total_tokens']:,}，均 {s['avg_elapsed_s']}s/局"
        )
        lines.append("")

    report_path = EXP / "ct_analysis.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    (EXP / "ct_analysis.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n→ {report_path}")


if __name__ == "__main__":
    main()
