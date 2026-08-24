"""廉价磋商（cheap talk）实验 runner — 项目对齐文档 w.txt 新板子。

板子: 7人 = 2狼 + 2平民 + 1预言家 + 1女巫 + 1守卫
夜序: 狼队私聊(归票) → 狼队投票 → 预言家查验 → 女巫用药 → 守卫守护 → 结算(同守同救=死)
白天: 公布死者(不公开身份) → 顺序发言每人一次 → 同时投票(票型公开) → 平票随机决
胜负: 狼全灭=好人胜 | 狼≥存活半数 / 平民全灭 / 神职全灭 = 狼胜
性格: 理性功利、同质（全员同一 persona，仅名字不同）
输出: 每局 transcript.md（全量对局文本）+ events.json + result.json

组（实验条件，战术经 system prompt 按角色注入）:
  B   baseline：无战术（预言家必须如实报查验；全员禁止跳/悍跳/挡刀/空刀/自刀）
  WJ  狼人悍跳预言家（前置位1-4率先起跳 / 后置位5-7对跳）+ 夜间空刀/自刀选项
  VJ  平民跳预言家挡刀（前置位起跳吸引狼刀 / 后置位保持平民视角）
  SQ  预言家不发言（隐藏查验，像普通平民发言）

用法:
    python scripts/run_cheap_talk_experiments.py --group B --start-seed 101 --games 10
    python scripts/run_cheap_talk_experiments.py --group WJ --start-seed 201 --games 2
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.llm.env import load_env_file

load_env_file()

# ── 通道卫生：回到 DeepSeek 官方渠道，清掉 zen/公益站实验遗留的环境开关 ──
for _var in (
    "AGENT_USE_NATIVE_FC",          # zen 需要=0（文本决策）；DeepSeek 官方支持原生 FC
    "AIWEREWOLF_MAX_TOKENS_FLOOR",  # zen 混元推理模型的补全预算下限
    "LLM_MIN_REQUEST_INTERVAL",     # 公益站限速垫片
    "LLM_RETRY_400",                # 公益站 400 重试开关
    "MODEL_POOL",
    "EXP_LABEL_PREFIX",
    "AIWEREWOLF_RULE_ADDENDUM",
):
    os.environ.pop(_var, None)
for _role in ("WEREWOLF", "VILLAGER", "SEER", "WITCH", "GUARD"):
    os.environ.pop(f"AIWEREWOLF_TACTIC_{_role}", None)
# 局后反思不改变对局结果（纯局后动作），关掉省 7 次调用/局
os.environ["COGNITIVE_ENABLE_REFLECTION"] = "false"

from backend.agents.factory import create_agents
from backend.engine.game import WerewolfGame
from backend.engine.models import Role
from backend.engine.rules import CT_ROLES_7P
from backend.engine.rules import build_players
from backend.engine.transcript import build_transcript

EXP_DIR = ROOT / "experiments"

# ── 理性功利、同质人格（w.txt：性格配置 理性功利、同质）────────────────
_RATIONAL_PERSONA = {
    "mbti": "ISTJ",
    "gender": "neutral",
    "age": 30,
    "basic_info": "理性功利的策略玩家：只按胜率与期望收益做判断，不受情绪、人情或表演影响。",
    "style_label": "analytical",
    "vocabulary_style": "平实精确，像写分析报告",
    "speech_length_habit": "简洁，只说与判断相关的信息",
    "reasoning_style": "期望收益与信息推理",
    "social_habit": "独立分析，按证据投票",
    "humor_style": "none",
    "pressure_style": "用证据回应质疑",
    "uncertainty_style": "承认不确定，按概率行动",
    "voice_rules": ["concise", "structured"],
    "logic_style": "事实+概率推断",
    "trigger_topics": [],
    "werewolf_experience": "熟练",
}
_CT_NAMES = ["陈衡", "周慎", "林笃", "沈度", "韩朴", "苏正", "魏一"]


def _homogeneous_rational(count: int, seed: int | None) -> list[dict]:
    return [dict(_RATIONAL_PERSONA, name=_CT_NAMES[i % len(_CT_NAMES)]) for i in range(count)]


# ── 实验条件（战术文本按角色注入；engine_kwargs 控制夜刀合法性）─────────
_BASE_SEER_HONEST = (
    "你是预言家：白天发言时必须如实公布你的身份与查验结果（查验对象+好人/狼人），不得隐藏、不得谎报。"
)

GROUPS: dict[str, dict] = {
    "B": {
        "dir": "ct_B_baseline",
        "engine_kwargs": dict(wolf_self_knife=False, wolf_empty_knife=False),
        "rule_addendum": (
            "本局为基线对局：所有玩家按标准方式打牌。"
            "|禁止使用任何特殊战术：不得跳预言家、不得悍跳、不得挡刀、不得空刀、不得自刀。"
            "|发言只基于自己真实可见的信息表态；非预言家玩家不得声称自己是预言家或公布查验结果。"
        ),
        "tactics": {"SEER": _BASE_SEER_HONEST},
    },
    "WJ": {
        "dir": "ct_WJ_wolf_jump",
        "engine_kwargs": dict(wolf_self_knife=True, wolf_empty_knife=True),
        "rule_addendum": (
            "本局为战术实验局（狼人悍跳条件）：其他玩家按标准方式打牌。"
        ),
        "tactics": {
            "WEREWOLF": (
                "本局启用狼人悍跳预言家战术。"
                "|白天发言时你要悍跳预言家：声称自己是真预言家，编造并公布查验结果（给好人发查杀、给队友发金水），搅乱好人信息。"
                "|若你是前置位（你的座位号≤4，发言在前）：率先起跳，直接报编造的查验。"
                "|若你是后置位（座位号≥5，发言在后）：若真预言家或其他跳者已发言，与之对跳并坚决否定其结果；若无人跳，起身悍跳。"
                "|夜间战术（可选）：可空刀制造平安夜假象，或自刀骗女巫解药，按期望收益决定。"
            ),
            "SEER": _BASE_SEER_HONEST,
        },
    },
    "VJ": {
        "dir": "ct_VJ_villager_jump",
        "engine_kwargs": dict(wolf_self_knife=False, wolf_empty_knife=False),
        "rule_addendum": (
            "本局为战术实验局（平民挡刀条件）：其他玩家按标准方式打牌。"
        ),
        "tactics": {
            "VILLAGER": (
                "本局启用平民跳预言家挡刀战术。"
                "|你的目标是保护真预言家：通过假跳预言家吸引狼人夜晚刀你。"
                "|若你是前置位（座位号≤4，发言在前）：起身声称自己是预言家，编造一份查验结果（如给某人金水）。"
                "|若你是后置位（座位号≥5，发言在后）：若已有跳者可配合其发言保持平民视角；若无跳者且局势需要，可起身假跳。"
            ),
            "SEER": _BASE_SEER_HONEST,
        },
    },
    "SQ": {
        "dir": "ct_SQ_seer_quiet",
        "engine_kwargs": dict(wolf_self_knife=False, wolf_empty_knife=False),
        "rule_addendum": (
            "本局为战术实验局（预言家隐藏条件）：其他玩家按标准方式打牌。"
        ),
        "tactics": {
            "SEER": (
                "本局启用预言家不发言战术。"
                "|白天发言时不得暴露自己是预言家：不公布身份、不公布任何查验结果、不做任何暗示。"
                "|像普通平民一样基于公开信息分析发言，把查验信息只用于自己的投票判断。"
            ),
        },
    },
}

_CLAIM_KEYWORDS = ("我是预言家", "预言家是我", "我查验", "我的查验", "金水", "查杀", "我验了", "我查了")


def _seat_label(state, player_id: str) -> str:
    p = state.player(player_id)
    return f"{p.seat}号:{p.name}"


def _analyse_game(state, game) -> dict:
    """从事件流提取票型/战术显形/关键统计（上帝视角）。"""
    events = [e.to_dict() for e in state.events]

    day1_votes = state.vote_history.get(1, {})
    tally: dict[str, list[str]] = {}
    for voter, target in day1_votes.items():
        try:
            tally.setdefault(_seat_label(state, target), []).append(_seat_label(state, voter))
        except KeyError:
            continue
    day1_pattern = {t: len(vs) for t, vs in sorted(tally.items())}

    # 第 1 天发言中的身份声称（谁在跳预言家；是否非预言家）
    seer = next((p for p in state.players if p.role == Role.SEER), None)
    day1_claims: list[str] = []
    nonseer_claim_count = 0
    for e in events:
        if e.get("day") != 1 or e.get("type") != "CHAT_MESSAGE":
            continue
        speech = str(e.get("payload", {}).get("speech", ""))
        if any(k in speech for k in _CLAIM_KEYWORDS):
            actor_id = e.get("payload", {}).get("actor_id", "")
            label = _seat_label(state, actor_id)
            role_cn = {"Seer": "真预言家", "Werewolf": "狼人", "Villager": "平民", "Witch": "女巫", "Guard": "守卫"}
            role = role_cn.get(state.player(actor_id).role.value, "?")
            day1_claims.append(f"{label}({role})")
            if seer is None or actor_id != seer.id:
                nonseer_claim_count += 1

    # 夜间战术显形
    empty_knife_nights = sum(
        1
        for e in events
        if e.get("type") == "PRIVATE_INFO"
        and e.get("payload", {}).get("kind") == "wolf_attack_tally"
        and not e.get("payload", {}).get("target_id")
    )
    self_knife_nights = 0
    for e in events:
        if e.get("type") != "NIGHT_ACTION" or e.get("payload", {}).get("action_type") != "attack":
            continue
        if e.get("payload", {}).get("actor_id") and e.get("payload", {}).get("actor_id") == e.get("payload", {}).get("target_id"):
            self_knife_nights += 1

    witch_save = sum(1 for e in events if e.get("payload", {}).get("action_type") == "witch_save")
    witch_poison = sum(1 for e in events if e.get("payload", {}).get("action_type") == "witch_poison")
    wolf_chat_msgs = sum(
        1
        for e in events
        if e.get("type") == "PRIVATE_INFO" and e.get("payload", {}).get("kind") == "wolf_chat_message"
    )

    llm_calls = len(state.decision_records)
    prompt_tokens = sum(r.prompt_tokens or 0 for r in state.decision_records)
    completion_tokens = sum(r.completion_tokens or 0 for r in state.decision_records)

    return {
        "day1_vote_pattern": day1_pattern,
        "day1_pattern_detail": tally,
        "day1_seer_claims": day1_claims,
        "day1_nonseer_claims": nonseer_claim_count,
        "empty_knife_nights": empty_knife_nights,
        "self_knife_nights": self_knife_nights,
        "witch_save_used": witch_save,
        "witch_poison_used": witch_poison,
        "wolf_chat_messages": wolf_chat_msgs,
        "llm_calls": llm_calls,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "seer_alive": bool(seer and seer.alive),
    }


def run_one_game(seed: int, group_cfg: dict, label: str) -> dict:
    players = build_players(CT_ROLES_7P, seed=seed)
    agents = create_agents(players, {"type": "llm", "seed": seed})

    t0 = time.time()
    game = WerewolfGame(
        players=players,
        agents=agents,
        seed=seed,
        max_days=8,
        # 白天规则（w.txt）：无警徽、无遗言、平票随机决、票型公开
        disable_badge=True,
        disable_last_words=True,
        random_tiebreak=True,
        # 胜负规则（w.txt）：狼≥半数 / 平民全灭 / 神职全灭 = 狼胜（kill_side_win 默认 True）
        full_elimination=False,
        kill_side_win=True,
        persona_sampler=_homogeneous_rational,
        **group_cfg["engine_kwargs"],
    )
    state = game.play()
    elapsed = time.time() - t0

    winner = state.winner.value if state.winner else "none"
    end_reason = ""
    for e in state.events:
        ed = e.to_dict()
        if ed.get("type") == "GAME_END":
            end_reason = ed.get("payload", {}).get("reason", "")

    analysis = _analyse_game(state, game)

    result = {
        "label": label,
        "seed": seed,
        "winner": winner,
        "end_reason": end_reason,
        "days": state.day,
        "elapsed": round(elapsed, 1),
        **analysis,
    }

    out_dir = EXP_DIR / group_cfg["dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"game_seed{seed}_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 全量对局文本（研究核心资产）
    (out_dir / f"game_seed{seed}_transcript.md").write_text(
        build_transcript(
            state,
            title=f"廉价磋商对局 {label} seed={seed}",
            meta={
                "实验组": label,
                "胜者": winner,
                "结束原因": end_reason,
            },
        ),
        encoding="utf-8",
    )
    # 主持人视角原始事件（含全部私密信息，完整可复核）
    (out_dir / f"game_seed{seed}_events.json").write_text(
        json.dumps(state.moderator_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def apply_group_env(group_cfg: dict) -> None:
    if group_cfg.get("rule_addendum"):
        os.environ["AIWEREWOLF_RULE_ADDENDUM"] = group_cfg["rule_addendum"]
    else:
        os.environ.pop("AIWEREWOLF_RULE_ADDENDUM", None)
    for role in ("WEREWOLF", "VILLAGER", "SEER", "WITCH", "GUARD"):
        key = f"AIWEREWOLF_TACTIC_{role}"
        if role in group_cfg.get("tactics", {}):
            os.environ[key] = group_cfg["tactics"][role]
        else:
            os.environ.pop(key, None)


_BALANCE_ERROR_MARKERS = ("402", "Insufficient Balance", "balance", "余额")


def looks_like_quota_error(end_reason: str) -> bool:
    return any(m in end_reason for m in _BALANCE_ERROR_MARKERS)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", type=str, required=True, choices=list(GROUPS.keys()))
    parser.add_argument("--start-seed", type=int, default=101)
    parser.add_argument("--games", type=int, default=10)
    args = parser.parse_args()

    group_cfg = GROUPS[args.group]
    label = args.group
    seeds = list(range(args.start_seed, args.start_seed + args.games))
    apply_group_env(group_cfg)

    print(f"=== CT group {label} → experiments/{group_cfg['dir']} | seeds {seeds[0]}..{seeds[-1]} ===", flush=True)
    results: list[dict] = []
    quota_hit = False
    for seed in seeds:
        try:
            r = run_one_game(seed, group_cfg, label)
        except Exception as e:  # noqa: BLE001 — 单局崩溃不拖垮整组
            r = {
                "label": label, "seed": seed, "winner": "error",
                "end_reason": f"crash: {str(e)[:160]}", "days": 0, "elapsed": 0,
                "llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
            }
            (EXP_DIR / group_cfg["dir"]).mkdir(parents=True, exist_ok=True)
            (EXP_DIR / group_cfg["dir"] / f"game_seed{seed}_result.json").write_text(
                json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        results.append(r)
        print(
            f"  seed={r['seed']} winner={r['winner']:<8} reason={r['end_reason']:<24} "
            f"days={r['days']} calls={r.get('llm_calls', 0)} "
            f"d1claims={r.get('day1_nonseer_claims', '-')} {r['elapsed']:.0f}s",
            flush=True,
        )
        if r.get("winner") == "error" and looks_like_quota_error(r.get("end_reason", "")):
            quota_hit = True
            print("  !! 疑似 DeepSeek 余额耗尽，熔断停止后续对局", flush=True)
            break

    n = len(results)
    village_wins = sum(1 for r in results if r["winner"] == "village")
    wolf_wins = sum(1 for r in results if r["winner"] == "wolf")
    errors = sum(1 for r in results if r["winner"] == "error")
    summary = {
        "label": label,
        "n_games": n,
        "village_wins": village_wins,
        "wolf_wins": wolf_wins,
        "errors": errors,
        "village_win_rate": f"{village_wins}/{n}",
        "avg_days": round(sum(r.get("days", 0) for r in results) / max(n, 1), 1),
        "total_llm_calls": sum(r.get("llm_calls", 0) for r in results),
        "total_tokens": sum(r.get("prompt_tokens", 0) + r.get("completion_tokens", 0) for r in results),
        "quota_aborted": quota_hit,
        "games": results,
    }
    summary_path = EXP_DIR / f"summary_ct_{label}.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  village {village_wins}/{n} | wolf {wolf_wins}/{n} | errors {errors} | quota_aborted={quota_hit}")
    print(f"  summary → {summary_path}", flush=True)


if __name__ == "__main__":
    main()
