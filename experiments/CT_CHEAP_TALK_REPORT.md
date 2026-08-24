# 廉价磋商（Cheap Talk）研究报告 — 第一天战术对票型与胜率的影响

> 新研究方向首篇工作记录。项目对齐文档：`w.txt`（桌面）。旧研究（论文复现/制度实验）
> 的全部数据、报告与脚本已移出本仓库——完整备份在 `../AIwere_old/`。
> 本报告所有实验：2026-08-23，DeepSeek 官方渠道 `deepseek-v4-flash`。

---

## 1. 研究问题

在 7 人新板子（2 狼 + 2 平民 + 1 预言家 + 1 女巫 + 1 守卫）下，研究**第一天白天
cheap talk 战术**（狼人悍跳预言家 / 平民跳预言家挡刀 / 预言家不发言）对
**票型**与**阵营胜率**的影响。LLM 仅作为对局工具（理性功利、同质人格），
不研究模型能力本身。

## 2. 框架改动（AIwerewolf 大改清单）

| w.txt 需求 | 实现 | 位置 |
|---|---|---|
| 7人局 2狼2民1女巫1预言家1守卫 | 新角色组 `CT_ROLES_7P` | `backend/engine/rules.py` |
| 夜序：狼私聊→狼投票→预言家→女巫→守卫→结算 | CompositePhase 重排；守卫移出并行块 | `backend/engine/phases.py`、`game.py::_night_role_actions_parallel` |
| 狼人夜间私聊（归票） | 新增 `wolf_chat()` 单次调用私聊轮（≥2 狼才开），内容仅狼队可见并进入后续决策上下文 | `game.py::_wolf_phase`、`cognitive/agent.py::wolf_chat` |
| 空刀（平安夜假象） | 旗标 `wolf_empty_knife`：validator 放行空 target、AgentLoop 三处解析接受"空刀"token、全票空刀→平安夜 | `actions.py`、`visibility.py`、`agent_loop.py`、`game.py` |
| 自刀（骗解药） | 旗标 `wolf_self_knife`：狼自身进入夜刀合法目标 | `actions.py`、`visibility.py` |
| 同守同救冲突 | 既有"奶穿=死"规则，原样保留 | `game.py::_night_resolve` |
| 战术 system prompt 注入 | 环境变量 `AIWEREWOLF_TACTIC_<ROLE>` 按角色注入【战术指令】块，作用于该角色全部 prompt（发言/投票/夜行动） | `cognitive/prompts.py::build_game_context` |
| baseline（无战术+预言家必发言） | `AIWEREWOLF_RULE_ADDENDUM` 反战术条款 + `AIWEREWOLF_TACTIC_SEER` 强制如实报查验 + 关闭空刀/自刀 | `scripts/run_cheap_talk_experiments.py` 组 B |
| 性格理性功利、同质 | `persona_sampler` 全员同一 ISTJ 分析型人格（仅名字不同：陈衡/周慎/林笃/沈度/韩朴/苏正/魏一） | runner `_homogeneous_rational` |
| 完整对局文本 | 每局三件套：`*_transcript.md`（全量可读文本，含狼私聊/内心理由/票型）、`*_events.json`（主持人视角原始事件）、`*_result.json` | `backend/engine/transcript.py` |
| 胜负：狼≥半数/平民全灭/神职全灭=狼胜 | 既有 `kill_side_win=True` + `full_elimination=False` 精确匹配，直接复用 | `game.py::_check_win` |
| 平票随机决 / 无警徽 / 无遗言 / 票型公开 | 既有开关 `random_tiebreak` / `disable_badge` / `disable_last_words` | runner engine_kwargs |

**验证**：既有测试 133 项全过（引擎/制度/认知/提示词分层）；新机制离线冒烟
`scripts/smoke_cheap_talk.py` ALL OK（validator 三门、自刀合法目标、夜序
狼→预言家→女巫→守卫、狼私聊事件、transcript 渲染、两种旗标整局跑通）。

## 3. 实验设置

- 模型：`deepseek-v4-flash`（官方 API），原生 function calling
- 每局约 25–48 次 LLM 调用、30–55 秒；局后反思已关闭（不影响对局，省 7 调用/局）
- 组与局数（战术条件小样本仅验证框架可用性与战术显形，不做胜率推断）：

| 组 | 条件 | 局数 | seeds |
|---|---|---|---|
| B | baseline：全员禁战术，预言家必如实报查验 | 10（+冒烟1） | 101–110（+100） |
| WJ | 狼人悍跳预言家（前置位率先起跳/后置位对跳）+ 开放空刀/自刀 | 2 | 201–202 |
| VJ | 平民跳预言家挡刀（前置位起跳吸刀/后置位视情况） | 2 | 301–302 |
| SQ | 预言家不发言（隐藏查验，扮平民） | 2 | 401–402 |

## 4. 结果

```
组   说明              局数  好人胜  胜率%  均天数  D1放逐狼%  假跳局  空刀夜 自刀夜 预言家存活%
B    baseline 无战术    10    4     40.0   2.3    50.0      0      0     0     40.0
WJ   狼悍跳+空/自刀      2    1     50.0   2.5    50.0      2      0     0     50.0
VJ   平民跳预言家挡刀    2    0     0.0    2.0    0.0       2      0     0     0.0
SQ   预言家不发言        2    0     0.0    2.0    0.0       0      0     0     50.0
```

（含冒烟局 seed100 则 B 组 4/11 = 36.4%。）

### 4.1 baseline（B，10 局）：框架纪律性成立

- **零假跳**：10 局第一天非预言家声称预言家次数 = 0，反战术条款生效；
  真预言家全部按指令首日跳身份报查验。
- 好人胜率 40%（4/10），第一天放逐命中狼 50%（放逐角色分布：狼5/预言家3/女巫1/守卫1）。
- 女巫 10 局用解药 8 次、**毒药 0 次**（理性功利人格下极端保守，值得后续关注）。
- 狼队私聊平均 3 条/局，归票协商真实发生（transcript 可查）。

### 4.2 狼人悍跳（WJ，2 局）：战术完整显形，形成经典对跳

seed201：真预言家 1号 报金水；狼 2号（前置位）悍跳并给狼队友 7号 发**金水**；
狼 7号（后置位）对跳并给真预言家 1号 发**查杀**。三跳对垒、狼队互保，
好人最终被屠神输掉（all_gods_dead）。seed202 好人翻盘（两狼都被放逐）。
两局悍跳发言、配合逻辑、投票理由全部可在 transcript 中逐条复核。

### 4.3 平民挡刀（VJ，2 局）：**挡刀反噬——假跳把真预言家投出局**

两局第一天放逐的都是**真预言家**（D1 放逐角色 = Seer×2），好人 0/2：

- seed301：平民 3号、7号 双双假跳（各编金水），真预言家 4号 报出真查杀；
  多重对跳下好人判断"激进报查杀者更像悍跳狼"，把真预言家 4号 放逐。
- seed302：平民 1号 前置假跳，真预言家 7号 后置真跳；同样的对跳混乱，
  首日放逐 7号。

这是本次最有价值的初步发现：**挡刀战术在 LLM 对局中没有吸到狼刀，
反而制造了信息污染，让好人亲手放逐了自己的信息源**——cheap talk 的
负外部性直接体现在票型上。（2 局样本，仅作显形验证与机制记录。）

### 4.4 预言家不发言（SQ，2 局）：隐藏成功但好人失去方向盘

两局全场**无任何**预言家声称（含真身），预言家成功扮平民；但第一天好人
只能盲投（放逐对象均为平民），狼队两局均速胜（wolves_reached_parity）。
预言家存活 50%——藏住了人，但信息价值同时归零。

### 4.5 夜战术（空刀/自刀）：合法但未被选择

WJ 两局开放空刀/自刀选项（合法性已写入 prompt），狼队 4 个夜晚全部选择
实刀具体目标，空刀 0 次、自刀 0 次。理性功利人格 + 期望收益措辞下，
模型判断实刀优势更大。若要研究夜战术需更强的指令注入或专门条件组。

## 5. 成本与余额事件（含一次误判，如实记录）

- 开跑前余额 **¥19.07**；17 局（10+1 baseline、6 战术局）跑完后实际余额
  **¥17.26**，共花费 **¥1.81 ≈ ¥0.11/局**（每局 10–14 万 prompt tokens +
  约 8 千 completion tokens，折合约 ¥1/百万 tokens 混合单价）。
- **余额误判经过**：实验链结束后立即查询 `/user/balance` 一次返回
  `¥0.00`（与官网不符），当时误判为余额耗尽并停写报告；次日复核为
  ¥17.26。该接口在连续计费结算时存在瞬时抖动（冒烟后查询显示未扣费的
  ¥19.07 也是同类延迟），**单次读数不可信，成本核算应以差额为准**。
- 好在全部计划内对局（B×10 + 三战术组×2 + 冒烟 1）在该读数出现前已
  完整跑完（exit 0、零崩溃），无数据损失；熔断未触发也无需触发。
- 结论：实际预算基准 **¥0.11/局**，比最初预估（¥1.1/局）低一个数量级。

## 6. 数据资产

- `experiments/ct_B_baseline/`、`ct_WJ_wolf_jump/`、`ct_VJ_villager_jump/`、
  `ct_SQ_seer_quiet/`：每局 `transcript.md`（全量对局文本）+ `events.json`
  （主持人视角原始事件，含全部私密信息与决策理由）+ `result.json`
- `experiments/summary_ct_*.json`：各组带明细汇总
- `experiments/ct_analysis.md` / `ct_analysis.json`：跨组对比（`scripts/analyze_cheap_talk.py` 生成）

## 7. 下一步（余额 ¥17.26 足够，无需充值）

1. **扩样**：B 与 VJ 各 30 局优先——VJ 的"挡刀反噬"若在 30 局中稳定出现
   （D1 放逐真预言家率显著高于 B），即是便宜磋商负外部性的直接证据。
2. **诚实规则对照**：w.txt 计划中的"非诚实 vs 诚实规则"对比可直接复用
   既有 `honesty_rule` 开关 + 本框架（挡刀/悍跳在诚实规则下会被系统驳回，
   预期反噬消失）。
3. **收益矩阵**：以 B 为基线，量化各战术的胜率增量与"第一天票型流向"
   （跟跳率、对跳胜率、预言家首日存活率）。
4. **夜战术显形**：空刀/自刀需更强注入措辞或固定脚本夜（不完全交给模型选择）。
5. 预算参考：**¥0.11/局** → 30 局/组 × 2 组 ≈ **¥6.6**，当前余额足够。

## 8. 复现命令

```bash
# 离线验证（无需 API）
.venv/Scripts/python.exe scripts/smoke_cheap_talk.py
.venv/Scripts/python.exe -m pytest tests/test_engine.py tests/test_institution_switches.py -q

# 各实验组
.venv/Scripts/python.exe scripts/run_cheap_talk_experiments.py --group B  --start-seed 101 --games 10
.venv/Scripts/python.exe scripts/run_cheap_talk_experiments.py --group WJ --start-seed 201 --games 2
.venv/Scripts/python.exe scripts/run_cheap_talk_experiments.py --group VJ --start-seed 301 --games 2
.venv/Scripts/python.exe scripts/run_cheap_talk_experiments.py --group SQ --start-seed 401 --games 2

# 汇总分析
.venv/Scripts/python.exe scripts/analyze_cheap_talk.py
```
