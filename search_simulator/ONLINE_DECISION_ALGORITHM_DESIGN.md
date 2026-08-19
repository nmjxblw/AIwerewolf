# 在线区间极大极小决策算法设计（双阵营零和博弈 + reward 区间）

> 本文档是「在线决策参考算法」的设计稿，作为实现前的单一事实来源。
> 目标：在 `search_simulator/` 内新增一套**在线参考决策算法**，与现有全树穷举搜索并存。

---

## 0. 命名对照

| 旧称 | 正式算法名 | 说明 |
|---|---|---|
| 方案 1 | **区间极大极小搜索（Interval Minimax Search）** | 零和完美信息博弈的经典 **极小极大（Minimax）** 算法，叶节点估值换成 [悲观下界, 乐观上界] 区间，区间估值思想可类比 **B* 搜索** |
| 方案 3 | **区间值矩阵博弈（Interval-valued Matrix Game）** | 零和**矩阵博弈**的**极小极大解**（零和情形下极小极大 = **纳什均衡**），矩阵元素为区间值 |
| 整体 | **在线区间极大极小决策（Online Interval-Minimax Decision）** | 主干 + 辅助的组合，在线产出参考行动与区间轨迹 |

---

## 1. 背景与目标

现有 `SearchSimulator` 做的是**全树穷举搜索**：

- `_resolve_night` 枚举「狼刀 × 守卫 × 女巫 × 预言家查验」所有组合；
- `_resolve_day_vote` 枚举所有放逐结果；
- `_check_game_over` 判定胜负，终局按 `wins` 计数。

关键结论：当前树是**完全信息**的（`GameState` 内 `Player.role` 已知），枚举的是「行动分支」而非「隐藏身份分支」；终局统计本质上是**随机走法的胜率**，**不是零和极小极大（minimax）的策略价值**。

本算法要补上「策略价值」这一层：把狼人杀抽象为**好人阵营 vs 狼人阵营的零和博弈**，通过 **reward 区间** 比较候选行动、选出一个参考行动。

> 与 `gametheory.md` 的「不完全信息动态博弈」笔记关系：本文档是**完全信息抽象**下的确定性零和决策基线（见 §10 假设 A1）；「不完全信息 / CheapTalk 信号 / 似然计算」属于后续扩展方向，暂不纳入 v1。

**成功标准**

1. `SearchSimulator(policy="online", ...)` 从根状态沿参考路径逐步决策到终局，输出结构化 trace（每步候选行动的 reward 区间 + 选中行动）。
2. 区间极大极小搜索的值区间随前瞻深度单调收紧，并**夹住**精确 minimax 值（在能穷举的小棋盘上验证）。
3. 区间值矩阵博弈能对夜晚「狼刀 × 守卫 × 女巫」子博弈给出区间博弈价值 `[V, V̄]` 与最优行/列。
4. 现有 `policy="exhaustive"` 默认路径行为零回归。
5. 测试覆盖区间算术、阵营抽象、minimax、矩阵博弈、在线驱动的合法性与收尾。

---

## 2. 核心抽象

- **两个敌对对象**：`Camp.GOOD`（好人阵营）与 `Camp.WOLF`（狼人阵营）。
  - 角色映射：狼人 / 白狼王 → WOLF；村民 / 预言家 / 女巫 / 守卫 / 猎人 → GOOD。
- **零和**：统一以「好人视角」计分，好人胜 = `+1.0`，狼人胜 = `-1.0`；狼人节点取负号最小化。
- **终局价值**：由 `_check_game_over` 结果字符串映射——含「好人」→ `+1.0`，否则（三套狼胜文案）→ `-1.0`。

---

## 3. reward 区间语义（区间极大极小搜索）

把狼人杀当成一棵**扩展式（时序）零和博弈树**——与现有 `SearchSimulator` 建的是同一棵树，区别在于：现有代码「穷举所有走法并均匀计数」，本算法「每个决策点用 minimax 只挑最优行动」。

**区间来源 = 前瞻深度截断。**

```
value(好人视角): 好人胜 = +1, 狼人胜 = -1
- 好人控制节点(max): value = max 子节点
- 狼人控制节点(min): value = min 子节点
- 叶子(终局或到达深度 d):
   终局   -> 精确值 [v, v]        (退化区间)
   被截断 -> 启发式区间 [L, U]     (未来没看全 -> 只能给出范围)
```

区间按 **min/max 的区间扩展**逐层传播：

- max 节点：`[max L, max U]`
- min 节点：`[min L, min U]`

于是每个候选行动 `a` 得到区间 `V(a) = [L, U]`，含义是：

> 「从这里到前瞻尽头，对手最坏情况下我至少能保证 `L`；剩余不确定性最顺时我最多拿到 `U`。」

真实的全局 minimax 值一定落在 `[L, U]` 内（当叶子启发式能正确夹住真值时）。

**决策比较 = 区间排序**（可配置 criterion，见 §6.4）。

---

## 4. 区间值矩阵博弈（辅助）

区间极大极小搜索是主干；区间值矩阵博弈用于刻画**夜晚「双方同时行动」的子博弈**——这是狼人杀里唯一「两阵营同时出手」的环节，压成一次性矩阵博弈最自然。

```
       狼:刀X    狼:刀Y
好:守X  [.., ..]  [.., ..]
好:守Y  [.., ..]  [.., ..]
```

- 行 = 狼刀目标；列 = 好人联合行动（守卫 × 女巫）。
- 每个格子的 payoff = 该行动对到达「次日白天状态」后，区间极大极小搜索的递归区间（见 §6.3）。
- 用区间算术求解：下界矩阵的博弈值 = 悲观价值 `V`，上界矩阵的博弈值 = 乐观价值 `V̄`，得到区间价值 `[V, V̄]` 与双方最优行动。
- 零和矩阵博弈中，极小极大解 = 纳什均衡解；区间版本在上下界矩阵上分别求极小极大，得到价值区间。

---

## 5. 模块划分（新增文件，均在 `search_simulator/` 下）

沿用 `_` 前缀与中文 docstring 风格。

### 5.1 `_interval.py` — reward 区间类型与排序

```python
@dataclass(frozen=True)
class RewardInterval:
    lower: float
    upper: float
    # 构造时保证 lower <= upper（越界则交换）
    # width / midpoint 属性
```

- `max_over(intervals) -> RewardInterval`：`[max lower, max upper]`
- `min_over(intervals) -> RewardInterval`：`[min lower, min upper]`
- `select_best(intervals, criterion, alpha) -> int`：返回选中下标，稳定 tie-break 取首个最大。

### 5.2 `_zero_sum.py` — 双阵营零和抽象

- `class Camp(str, Enum): GOOD = "good"; WOLF = "wolf"`
- `camp_of_role(role: str) -> Camp`
- `terminal_utility(result: str) -> float`
- `leaf_heuristic(state, *, mode, oracle) -> RewardInterval`（`flat` / `count`）

### 5.3 `_minimax.py` — 区间极大极小搜索（主干）

```python
def minimax_interval(state, *, depth, camp, oracle,
                     criterion, alpha, leaf_mode,
                     enable_matrix) -> tuple[RewardInterval, DecisionSummary]:
    ...
```

- 先 `oracle._check_game_over` → 终局给精确区间；
- `depth == 0` → `leaf_heuristic`；
- 白天（GOOD 控制）→ `oracle._day_decision_candidates` + `max_over`；
- 夜晚（双方同时）→ 委托 `_matrix_game`。
- 返回值含选中行动、候选区间列表、所用 criterion，供 trace。

### 5.4 `_matrix_game.py` — 区间值矩阵博弈（辅助）

- `build_night_matrix(state, oracle, depth, ...)` → `(rows, cols, payoff 区间矩阵)`
- `solve_interval_game(matrix)` → `(value_interval [V,V̄], optimal_rows, optimal_cols)`
- 好人联合行动列 v1 只含「守卫 × 女巫」；预言家查验按 canonical 分支处理（假设 A4）。

### 5.5 `_online_policy.py` — 在线参考驱动 + 产物

- `run_online_reference(simulator) -> dict`：从根状态循环决策到终局，产出 trace。
- `emit_online_artifacts(simulator, trace)`：写 `online_trace.json` + 结果摘要。
- `evaluate_against_exact(simulator, trace)`：`--compare_with_exact` 时算 regret / 行动吻合率，写 `online_eval.json`。

---

## 6. 算法细节

### 6.1 统一价值尺度

- 一律好人视角：好人胜 `+1.0`，狼人胜 `-1.0`。
- 狼人控制节点对价值取负号（最小化好人的价值）。

### 6.2 区间传播规则

- 好人控制（max）：`V = [max L, max U]`
- 狼人控制（min）：`V = [min L, min U]`
- 叶子（终局）：`[v, v]`；叶子（截断）：启发式区间。

### 6.3 白天 vs 夜晚的分工

**白天** = 单方决策（抽象为「好人阵营选放逐目标」，狼票折叠进区间）→ 区间极大极小搜索递归：

```
对每个放逐目标 e:
    child = 放逐 e 后的状态
    V(e) = minimax_interval(child, depth-1, WOLF, ...)
V = max_over([V(e)])
```

**夜晚** = 双方同时行动 → 区间值矩阵博弈：

```
rows   = 狼刀目标
cols   = 好人联合行动（守卫 × 女巫）
payoff[i][j] = minimax_interval(次日白天状态, depth-1, GOOD, ...)
solve -> [V, V̄], 狼最优行, 好人最优列
```

同一 cell 因**死亡连锁**（猎人 / 白狼王）产生的多条分支按控制方合并：

- 猎人开枪 → 好人控制 → `max_over`
- 白狼王带走 → 狼人控制 → `min_over`

### 6.4 决策比较（criterion，可配置）

| criterion | 规则 |
|---|---|
| `optimistic` | 按 `upper` 最大，其次 `lower` |
| `pessimistic` | 按 `lower` 最大，其次 `upper` |
| `hurwicz` | `alpha*upper + (1-alpha)*lower` 最大 |
| `dominance` | `a` 占优 `b` 当 `a.lower>=b.lower 且 a.upper>=b.upper`（至少一者严格）；无非被占优行动时回退 `pessimistic` |

### 6.5 在线参考驱动

```
state = 根状态
while not 终局:
    if 夜晚: (chosen, interval, candidates) = 区间值矩阵博弈决策(state)
    else:    (chosen, interval, candidates) = 区间极大极小搜索决策(state)
    trace.append({step, phase, camp, candidates, chosen_action, chosen_interval})
    state = canonical 物化(chosen)   # 见假设 A5
```

- 步数上限兜底（复用 `max_processed_states` 或新增 `--max_online_steps`），超限按现有引擎 `max_days` 语义判狼胜。

**trace step 结构**：

```json
{
  "step": 1,
  "phase": "night",
  "camp": "both",
  "candidates": [
    {"action": "狼刀→3", "interval": [-1.0, 1.0], "score": 0.0, "chosen": true}
  ],
  "chosen_action": "狼刀→3",
  "chosen_interval": [-1.0, 1.0]
}
```

### 6.6 精确对照（regret）

`--compare_with_exact` 时，以全深度 `depth=None` 重算各决策点的精确价值与最优行动集：

- `regret = exact_value - online_value`（根节点 + 逐决策点）
- `action_agreement_rate`：在线选中行动落在精确最优集的比例

---

## 7. 现有文件改动点

### `_config.py`

新增 CLI 参数，并同步 `UI_LABELS` / `GUI_TOOLTIPS` / `SIMULATOR_ARG_KEYS` / `ARTIFACT_ARG_KEYS` / `CONFIG_SUMMARY_KEYS`：

- `--policy`（`exhaustive|online`，默认 `exhaustive`）
- `--lookahead_depth`（int，默认 `2`；负数/None = 全深度）
- `--interval_criterion`（`optimistic|pessimistic|hurwicz|dominance`，默认 `pessimistic`）
- `--hurwicz_alpha`（float，默认 `0.5`）
- `--leaf_heuristic`（`flat|count`，默认 `flat`）
- `--enable_matrix_subgame`（store_true，默认 True）
- `--online_trace_path`（str，默认 `online_trace.json`）
- `--compare_with_exact`（store_true）

### `_simulator.py`

- `load_config` 读取新 kwargs 到实例属性。
- 新增 `run_online()`：委托 `_online_policy`，以 `self` 作为 transition oracle。
- **结构化决策元数据（关键重构，向后兼容）**：抽取 `_resolve_night` / `_resolve_day_vote` 的核心枚举，新增两个仅增不改签名的方法：
  - `_night_decision_cells(state) -> list[tuple[NightCellMeta, GameState]]`
  - `_day_decision_candidates(state) -> list[tuple[int, GameState]]`
  - 现有方法继续返回 `list[GameState]`，行为不变。

### `__main__.py` / `_artifacts.py`

- `__main__.py` 已按 `SIMULATOR_ARG_KEYS` 自动透传；`_run_simulation` 在 `policy == "online"` 时调 `run_online()`。
- `_artifacts.py`：online 模式跳过状态树绘图/文本树，改调用 `emit_online_artifacts`。

### `__init__.py`

- 可选 re-export `RewardInterval`、`SearchSimulator`。

---

## 8. 边界情况与失败模式

1. **空候选**：白天存活 ≤1 或夜晚无刀口 → 直接返回终局/精确区间。
2. **区间不变量**：构造与合并强制 `lower <= upper`；NaN/inf 防护。
3. **零和一致性**：价值全程好人视角，狼人节点负号化；终局映射用「好人」子串。
4. **死亡连锁**：同一 cell 多分支按控制方合并；参考路径取 canonical 分支（A5）。
5. **不终止**：在线驱动步数上限兜底，超限判狼胜。
6. **性能**：bounded depth ≤3 在 5–8 人板可接受；全深度对照仅限极小板（约 3–4 人、1 狼）。
7. **可复现**：v1 全程确定性（稳定排序 + 首个最大 tie-break），无随机 rollout。
8. **回归**：`policy` 默认 `exhaustive`，`run()` 分支未动；新增方法仅增不改。

---

## 9. 测试与验收

新增 `tests/test_search_simulator_online.py`：

- `_interval`：不变量、`max_over`/`min_over`、四种 criterion、dominance 回退。
- `_zero_sum`：`camp_of_role`、`terminal_utility` 三套狼胜文案。
- `_minimax`：极小板全深度价值与穷举方向一致；深度增大区间单调收紧且夹住精确值。
- `_matrix_game`：手写 2×2 区间矩阵验证 `[V, V̄]` 与最优行/列。
- `_online_policy`：路径合法、trace 结构完整、chosen 在 candidates 内。
- 集成：`policy="online"` 跑通并产出 `online_trace.json`；默认 `exhaustive` 跑一次断言 `wins` 与旧行为一致。

**验收命令**（父项目 venv 下，遵循 `search_simulator/AGENTS.md`）：

```powershell
chcp 65001; ..\.venv\Scripts\Activate.ps1; $env:PYTHONUTF8="1"; $env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH=".."
python -m search_simulator --cli --number_of_players 5 --number_of_wolves 1
python -m search_simulator --cli --policy online --number_of_players 5 --number_of_wolves 1 --lookahead_depth 2
python -m search_simulator --cli --policy online --number_of_players 4 --number_of_wolves 1 --compare_with_exact --lookahead_depth -1
pytest tests/test_search_simulator_online.py -q
```

---

## 10. 显式假设

- **A1 完全信息**：与现有模拟器一致，`GameState` 内角色已知；v1 不建模隐藏身份，区间只反映「行动/对抗」不确定性。
- **A2 白天抽象**：白天视为「好人阵营选放逐」，狼票影响折入区间。
- **A3 夜晚抽象**：双方同时行动 → 区间值矩阵博弈；好人列 v1 = 守卫 × 女巫。
- **A4 预言家查验**：不进矩阵列，按 oracle canonical 分支处理（后续可扩展为信息价值列）。
- **A5 canonical 物化**：选中行动的多条死亡连锁/查验分支，取确定性首个分支生成参考路径；minimax 区间仍含全部分支。
- **A6 「作为参考」口径**：默认「可读 trace + 可选精确对照（regret / 行动吻合率）」。
