"""验证在线决策算法：7 人局输出 + 自定义输入逻辑正确性。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# 确保仓库根目录在 sys.path（本脚本位于 scripts/ 下）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 输出与缓存全部落到临时目录，避免污染仓库
os.chdir(tempfile.mkdtemp())

from search_simulator import SearchSimulator
from search_simulator._game_state import GameState
from search_simulator._player import Player

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


TERMINALS = {
    "好人阵营胜利",
    "狼人阵营胜利（人数过半）",
    "狼人阵营胜利（神职角色已被消灭）",
    "狼人阵营胜利（村民已被消灭）",
}


def is_terminal(r):
    return r in TERMINALS


print("== 1. 7 人局（2狼1预言家1女巫）穷举 ==")
sim = SearchSimulator(
    number_of_players=7, number_of_wolves=2, include_seer=True, include_witch=True
)
sim.run()
total = sum(sim.wins.values())
check("wins 之和 == 终局数", total == len(sim.endings), f"{total} vs {len(sim.endings)}")
check("终局数 > 0", len(sim.endings) > 0)
check("所有终局结果合法", all(is_terminal(r) for _, r in sim.endings))
check("好人胜 > 0", sim.wins.get("好人阵营胜利", 0) > 0)
wolf_total = sum(v for k, v in sim.wins.items() if k.startswith("狼人"))
check("狼人胜 > 0", wolf_total > 0)
print(f"    wins={sim.wins}")
sim.signature_cache.close()  # 释放 SQLite 连接，避免与后续实例锁冲突

print("== 2. 7 人局在线（depth=2 conservative） ==")
sim2 = SearchSimulator(
    number_of_players=7,
    number_of_wolves=2,
    include_seer=True,
    include_witch=True,
    policy="online",
    lookahead_depth=2,
    toggle="conservative",
    online_trace_path="trace7.json",
)
trace = sim2.run_online()
check("结果合法(终局或未决)", is_terminal(trace["outcome"]) or trace["outcome"] == "未决", trace["outcome"])
steps = trace["steps"]
check("有决策步", len(steps) >= 1)
for s in steps:
    lo, hi = s["chosen_interval"]
    check(f"step{s['step']} 区间∈[-1,1]且lo<=hi", -1.001 <= lo <= hi <= 1.001, str(s["chosen_interval"]))
    check(f"step{s['step']} 双区间齐全", "optimistic_interval" in s and "conservative_interval" in s)
    check(f"step{s['step']} chosen 在候选内", any(c.get("chosen") for c in s["candidates"]))
phases = [s["phase"] for s in steps]
check("phase 从 night 开始", phases[0] == "night")
check("phase 日夜交替", all(phases[i] != phases[i + 1] for i in range(len(phases) - 1)))
print(f"    outcome={trace['outcome']} root_iv={trace['reward_interval']} steps={len(steps)}")
sim2.signature_cache.close()

print("== 3. 自定义输入逻辑正确性 ==")


def run_custom(name, state, *, n_wolves=1, include_witch=False, phase_check=None):
    sim = SearchSimulator(
        number_of_players=len(state.players),
        number_of_wolves=n_wolves,
        include_witch=include_witch,
        policy="online",
        lookahead_depth=2,
        online_trace_path=f"t_{name}.json",
    )
    return sim.run_online(start_state=state)


# 3a 狼人已过半 -> 立即狼胜（0 步）
s = GameState(
    players=[
        Player("狼人", True, {"攻击": -1}),
        Player("狼人", True, {"攻击": -1}),
        Player("村民", True, {}),
    ],
    phase="night",
)
t = run_custom("wolf_majority", s, n_wolves=2)
check("狼人过半->狼胜", t["outcome"].startswith("狼人"), t["outcome"])
check("狼人过半->0 步", len(t["steps"]) == 0, f"{len(t['steps'])} 步")

# 3b 狼全死 -> 好人胜（0 步）
s = GameState(
    players=[
        Player("狼人", False, {"攻击": -1}),
        Player("村民", True, {}),
        Player("村民", True, {}),
    ],
    phase="day",
)
t = run_custom("wolves_dead", s, n_wolves=1)
check("狼全死->好人胜", t["outcome"] == "好人阵营胜利", t["outcome"])
check("狼全死->0 步", len(t["steps"]) == 0)

# 3c 神职全灭（有神职但已死）-> 狼胜
s = GameState(
    players=[
        Player("狼人", True, {"攻击": -1}),
        Player("女巫", False, {"解药": 1, "毒药": 1}),
        Player("村民", True, {}),
        Player("村民", True, {}),
    ],
    phase="night",
)
t = run_custom("clergy_dead", s, n_wolves=1, include_witch=True)
check("神职全灭->狼胜", t["outcome"] == "狼人阵营胜利（神职角色已被消灭）", t["outcome"])

# 3d 从 day 起迭代
s = GameState(
    players=[
        Player("狼人", True, {"攻击": -1}),
        Player("村民", True, {}),
        Player("村民", True, {}),
    ],
    phase="day",
)
t = run_custom("day_start", s, n_wolves=1)
check("day 起首步 phase=day", t["steps"] and t["steps"][0]["phase"] == "day")
check("day 起首步 camp=good", t["steps"] and t["steps"][0]["camp"] == "good")

# 3e 预言家身份探知 roundtrip
s = GameState(
    players=[
        Player("预言家", True, {"查验": -1}),
        Player("狼人", True, {"攻击": -1}),
        Player("村民", True, {}),
    ],
    phase="day",
    seer_check_results={1: True},
)
check("seer_check_results roundtrip", GameState.from_dict(s.to_dict()).seer_check_results == {1: True})

# 3f 全狼 -> 狼胜
s = GameState(
    players=[
        Player("狼人", True, {"攻击": -1}),
        Player("狼人", True, {"攻击": -1}),
    ],
    phase="night",
)
t = run_custom("all_wolves", s, n_wolves=2)
check("全狼->狼胜", t["outcome"].startswith("狼人"), t["outcome"])

# 3g transition 从自定义状态产生合法子节点
sim9 = SearchSimulator(number_of_players=3, number_of_wolves=1)
s = GameState(
    players=[
        Player("狼人", True, {"攻击": -1}),
        Player("村民", True, {}),
        Player("村民", True, {}),
    ],
    phase="night",
)
children = sim9.transition(s)
check("transition(night)->day 子节点", children and all(c.phase == "day" for c in children))
check("子节点 action_label 非空", all(c.action_label for c in children))

print(f"\n== 汇总: {PASS} PASS, {FAIL} FAIL ==")
raise SystemExit(1 if FAIL else 0)
