# AI Werewolf — 廉价磋商（Cheap Talk）研究框架

7 人局狼人杀多智能体实验平台，当前研究方向：**第一天白天 cheap talk 战术
（狼人悍跳 / 平民挡刀 / 预言家隐藏）对票型与阵营胜率的影响**。
LLM 仅作为对局工具（理性功利、同质人格），所有对局导出完整文本供复盘分析。

> **旧研究已归档**：论文复现/制度实验（A/B/C/P/P2/C2/Z 系列）的全部数据、
> 报告与脚本已移出本仓库，完整备份在 `../AIwere_old/`。
> 当前研究方向与规则的权威描述：`w.txt`（项目对齐文档，桌面）。

---

## 1. 当前研究的板子与规则（w.txt 对齐）

- **板子**：7 人 = 2 狼人 + 2 平民 + 1 预言家 + 1 女巫 + 1 守卫（无猎人）
- **夜序**：狼队私聊（归票）→ 狼队投票 → 预言家查验 → 女巫用药 →
  守卫守护 → 结算（**同守同救 = 死亡**）
- **白天**：公布死者（不公开身份）→ 顺序发言每人一次 → 同时投票、
  票型公开 → 平票随机决；无警徽、无遗言
- **胜负**：狼全灭 = 好人胜；狼数 ≥ 存活半数 / 平民全灭 / 神职全灭 = 狼胜
- **性格**：全员理性功利、同质（同一 ISTJ 分析型人格，仅名字不同）
- **战术注入**：按角色写入 system prompt（环境变量），baseline 组用
  反战术条款压制一切跳/悍跳/挡刀/空刀/自刀

## 2. 快速开始

### 环境要求

- Windows（Git Bash）/ Linux，Python 3.12+（仓库自带 `.venv`，Windows 下用
  `./.venv/Scripts/python.exe`）
- DeepSeek API key（当前唯一在用渠道：`deepseek-v4-flash`，约 **¥0.11/局**）

### 配置密钥

```bash
cp .env.example .env   # 首次使用
# 编辑 .env，填入：
#   DEEPSEEK_API_KEY=sk-xxx
#   LLM_PROVIDER=deepseek
#   DEEPSEEK_BASE_URL=https://api.deepseek.com
#   DEEPSEEK_MODEL=deepseek-v4-flash
```

注意：`.env` 含密钥，已在 `.gitignore` 中，**不要提交、不要外传**。

### 三个脚本跑通全部实验

```bash
# 0) 无 API 验证框架（必跑，几秒钟）
./.venv/Scripts/python.exe scripts/smoke_cheap_talk.py

# 1) 跑实验组（B/WJ/VJ/SQ 四个条件）
./.venv/Scripts/python.exe scripts/run_cheap_talk_experiments.py --group B  --start-seed 101 --games 10
./.venv/Scripts/python.exe scripts/run_cheap_talk_experiments.py --group WJ --start-seed 201 --games 2
./.venv/Scripts/python.exe scripts/run_cheap_talk_experiments.py --group VJ --start-seed 301 --games 2
./.venv/Scripts/python.exe scripts/run_cheap_talk_experiments.py --group SQ --start-seed 401 --games 2

# 2) 汇总分析（票型/假跳/预言家存活/成本）
./.venv/Scripts/python.exe scripts/analyze_cheap_talk.py
```

runner 自动清理早期环境开关、关闭局后反思（省 7 次调用/局）、
按组设置战术注入；带余额熔断（疑似 402 停跑，余额以**前后差额**核算，
接口单次读数会抖动）。同一 seed 重跑会覆盖旧文件，扩样直接接着现有种子往后编。

### 实验组定义（战术条件）

| 组 | 条件 | 战术注入要点 |
|---|---|---|
| `B` | baseline | 全员禁战术；预言家必须如实报查验 |
| `WJ` | 狼人悍跳 | 前置位（1–4 号）率先起跳；后置位（5–7 号）对跳/起身悍跳；开放空刀/自刀 |
| `VJ` | 平民挡刀 | 前置位假跳预言家吸引狼刀；后置位视情况配合 |
| `SQ` | 预言家隐藏 | 不公布身份与查验，扮平民发言 |

战术文本全部在 `scripts/run_cheap_talk_experiments.py` 的 `GROUPS` 字典里，
改措辞/加条件组直接编辑该文件（战术 = `AIWEREWOLF_TACTIC_<ROLE>` 环境变量，
夜刀合法性 = `engine_kwargs`）。

## 3. 每局产出（研究核心资产）

对局写入 `experiments/ct_<组名>/`，每局三件套：

| 文件 | 内容 |
|---|---|
| `game_seed<N>_transcript.md` | **全量对局文本**：座位角色表、狼队私聊原文、每人发言全文 + 内心理由、每张票的目标与理由、夜行动、死亡与胜负 |
| `game_seed<N>_events.json` | 主持人视角原始事件（含全部私密信息与 `visible_to` 可见性），任何统计可从它重算 |
| `game_seed<N>_result.json` | 单局摘要：胜者、票型、假跳数、token 成本等 |

另有 `experiments/summary_ct_<组>.json`（组汇总）与 `ct_analysis.md`（跨组对比）。
**当前成果与结论见 `experiments/CT_CHEAP_TALK_REPORT.md`**（首篇工作记录，
含 B×10 + 战术组×2 的全部数据、"平民挡刀反噬真预言家"的票型证据、成本实况）。

## 4. 代码结构（改动后）

```
backend/
  engine/
    rules.py          # CT_ROLES_7P 新板子角色组（2狼2民1预言家1女巫1守卫）
    phases.py         # 新夜序：狼→预言家→女巫→守卫→结算
    game.py           # 狼队夜间私聊轮、空刀/自刀旗标、同守同救奶穿、屠边胜负
    actions.py        # validator：空刀/自刀合法性门（刀队友永远非法）
    visibility.py     # 自刀开启时狼自身进入夜刀合法目标
    transcript.py     # 全量对局文本导出（新）
    honesty.py        # 诚实规则开关（诚实 vs 非诚实对照实验用，新）
  agents/cognitive/
    agent.py          # wolf_chat() 夜间私聊；attack() 空刀透传
    agent_loop.py     # 空刀 token 解析（submit_decision/JSON/freeform 三处）
    prompts.py        # AIWEREWOLF_TACTIC_<ROLE> 按角色战术注入
scripts/
  smoke_cheap_talk.py            # 离线冒烟（无 API）
  run_cheap_talk_experiments.py  # 实验组 runner（B/WJ/VJ/SQ + 熔断）
  analyze_cheap_talk.py          # 汇总分析
  e2e_smoke.py / run_backend_full_strict.py / verify_visibility_strict.py  # 平台校验（make 用）
tests/   # 9 个核心测试文件：引擎/制度开关/认知离线/提示词分层/角色注册等
configs/ # strategy_library 等（cognitive 检索链路引用，保留）
```

## 5. 测试与验证

```bash
# 全部测试（195 项，离线，无 API 消耗）
./.venv/Scripts/python.exe -m pytest tests/ -q

# 新机制冒烟：validator 空刀/自刀门、自刀合法目标、夜序、狼私聊、transcript
./.venv/Scripts/python.exe scripts/smoke_cheap_talk.py
```

引擎旧能力（警徽/PK/猎人/白狼王/12人局等）仍在，当前实验通过
`disable_badge=True / disable_last_words=True / random_tiebreak=True`
关闭它们对齐 w.txt 规则；新板子不带猎人是角色组决定的。

## 6. 下一步实验计划（详见 CT_CHEAP_TALK_REPORT.md §7）

1. B 与 VJ 各扩到 30 局，验证"挡刀反噬"的稳定性（¥0.11/局，预算约 ¥6.6）
2. 诚实规则对照：复用 `honesty_rule` 开关（挡刀/悍跳发言会被系统驳回）
3. 收益矩阵：以 B 为基线量化各战术胜率增量与第一天票型流向
4. 夜战术显形：空刀/自刀需更强措辞或固定脚本夜

## 7. 遗留系统（与当前研究并行可用）

- FastAPI 后端（`backend/app.py`）+ Next.js 前端（`frontend/`）：可玩对局、
  观战、真人混战，`make deploy` 一键起
- Docker 部署：`docker-compose.yml`（PostgreSQL 可选，当前实验链路不依赖 DB）
- 多渠道 LLM 支持：deepseek / doubao / anthropic / weapi（`backend/llm/`），
  MODEL_POOL 可混编多模型
- Makefile：`make test / make lint / make deploy`（详见 Makefile）

## License

MIT
