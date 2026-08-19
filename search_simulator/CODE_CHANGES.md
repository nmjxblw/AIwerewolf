# 在线决策算法 — 代码修改清单

> 本文档是「在线决策参考算法」的**代码修改清单**（新增文件、现有文件改动、测试与验收）。
> 算法原理（reward 区间、λ、toggle 放缩、夜间战术、深度驱动等）见 **`ALGORITHM_DESIGN.md`**。

## 0. 实现状态清单（✅ 已完成 / ☐ 未完成）

> 勾选 = 已实现并通过验证。清单之外的后续修改见文末「§4 复盘整理」。

### 0.1 新增文件
- [x] `_interval.py` — `RewardInterval`（夹取/不变量）、`merge`（乐观=并集/保守=交集 + λ）、`compare`、`UNRESOLVED`
- [x] `_zero_sum.py` — `Camp`、`is_wolf_role`、`camp_of_role`、`terminal_utility`
- [x] `_minimax.py` — `evaluate(state, *, depth, oracle, toggle, lambda_risk, seen=frozenset())`（区间回传 + 路径环检测）
- [x] `_online_policy.py` — `run_online_reference` / `emit_online_artifacts` / `evaluate_against_exact`（trace 双区间 + regret/吻合率）
- [x] `_i18n.py` — `t(key, *args, **kwargs)` / `set_language()` / `t_en()` + `STRINGS`/`EN_STRINGS` 单表（复盘新增，见 §4.1）

### 0.2 现有文件改动
- [x] `_game_state.py` — `phase` / `reward_interval` / `action_label` / `players_snapshot` + `to_dict`/`from_dict`；签名含 phase/day_count
- [x] `_simulator.py` — `transition`、`run_online`、战术分支（骗刀/空刀）、`_flow_*` 模块级重构（3.14 闭包修复）、线程安全队列
- [x] `_config.py` — 在线参数、help 走 `t()`、`CONFIG_SUMMARY_KEYS` 移入 `_i18n.py`
- [x] `_gui.py` — λ slider、自定义状态编辑器、战术勾选树、迭代树预览、三栏弹性布局、`t()` 化
- [x] `__main__.py` / `_artifacts.py` — online 分支透传与产物
- [x] `_reporting.py` — 终局条目写入 `reward_interval`
- [x] `_plotting.py` / `_text_tree.py` — 参考路径图 + 节点标签区间 + `t()`/`t_en()` 化
- [x] `__init__.py` — re-export

### 0.3 测试与验收
- [x] `tests/test_search_simulator_online.py` — 16 passed
- [x] `scripts/verify_online_algorithm.py` — 29 PASS 0 FAIL
- [x] 7 人（2 狼 1 预言家 1 女巫）穷举 + 自定义输入正确性验证通过
- [ ] GUI 自定义编辑器「合法性检测 / 技能 Spinbox / 预言家身份探知勾选行 / 红字提示」——实际简化为「玩家 TreeView + 编辑对话框 + 文本字段」，未落地（见 §4.8）
- [ ] `_matrix_game.py` 区间值矩阵博弈——按设计 §5 标 optional，v1 不实现（有意为之）

## 1. 新增文件（模块划分，均在 `search_simulator/` 下）

沿用 `_` 前缀与中文 docstring 风格。

### 1.1 `_interval.py` — reward 区间类型与聚合

```python
@dataclass(frozen=True)
class RewardInterval:
    lower: float   # >= -1
    upper: float   # <= +1
    # 构造时保证 lower <= upper（越界则交换），并夹取到 [-1, +1]
    # width / midpoint 属性
```

- `merge(intervals, *, toggle, lambda_risk) -> RewardInterval`：按算法文档 §4.3 做并集（乐观）/ 交集（保守）+ λ 混合。
- `compare(a, b, toggle) -> int`：乐观比 `upper`、保守比 `lower`。
- `UNRESOLVED = RewardInterval(-1.0, 1.0)`：未决（前沿/退化环）区间。

### 1.2 `_zero_sum.py` — 双阵营零和抽象

- `class Camp(str, Enum): GOOD = "good"; WOLF = "wolf"`
- `camp_of_role(role: str) -> Camp`
- `terminal_utility(result: str) -> float`（+1 / -1）

### 1.3 `_minimax.py` — 区间极大极小搜索（主干）

```python
def evaluate(state, *, depth, camp, oracle,
             toggle, lambda_risk) -> RewardInterval:
    ...
```

- 终局 → `[u,u]`；`depth==0` 或退化环 → `[-1,+1]`；
- 白天（GOOD 控制）→ `oracle._day_decision_candidates` + `_interval.merge`；
- 夜晚（双方同时）→ `oracle._resolve_night` 全枚举 + `_interval.merge`（无矩阵）。
- 返回区间 + 选中行动 + 候选区间列表，供 trace。

### 1.4 `_online_policy.py` — 在线参考驱动 + 产物

- `run_online_reference(simulator, start_state=None) -> dict`：从根/自定义状态循环决策到真终局，产出 trace（含双区间）。
- `emit_online_artifacts(simulator, trace)`：写 `online_trace.json` + 结果摘要。
- `evaluate_against_exact(simulator, trace)`：`--compare_with_exact` 时算 regret / 行动吻合率，写 `online_eval.json`。
- 路径环检测：维护当前路径签名集合，重复即按 `[-1,+1]` 停止。

> `_matrix_game.py` **不实现**（区间值矩阵博弈标 optional，见算法文档 §5）。

## 2. 现有文件改动点

### 2.1 `_game_state.py`

- `GameState` 增加 `phase: str = "night"`（`"day" | "night"`）。
- 增加 `reward_interval: tuple[float, float] | None = None`：存计算后的区间 `(lower, upper)`，在线算法写回，穷举不写。
- 增加 `action_label: str = ""`（从 `state_action_index` 迁移）、`players_snapshot: list[str] = field(default_factory=list)`（从 `state_players_snapshot` 迁移）。
- 新增 `to_dict()` / `from_dict(d)`：自定义起始状态 JSON 序列化/反序列化。
- `phase`、`day_count` 纳入 `_state_signature`（避免昼夜状态错误去重）；`reward_interval` 不进签名。

### 2.2 `_simulator.py`

- 新增 `transition(current_game_state: GameState) -> list[GameState]`：按 `phase` 分发夜/昼结算；白天结算后 `phase="night"` 且 **`day_count += 1`**，夜晚结算后 `phase="day"`。
- `load_config` 读取新 kwargs：`policy`、`lambda_risk`、`toggle`、`lookahead_depth`、`tactics`、`online_trace_path`、`compare_with_exact`、`start_state`。
- 新增 `run_online(start_state: GameState | None = None)`：委托 `_online_policy`；GUI 输入的 `start_state` 强制 `parent_state_id=None`、`depth=0`。
- 战术分支：`_resolve_night` 在「骗刀」开启且存活女巫解药>0 时，狼人可选目标=全部存活玩家（含狼）；「空刀」开启时增加 `wolf_target=None` 分支。
- 临时数据迁移：`state_action_index`/`state_players_snapshot` → `GameState.action_label`/`players_snapshot`；`parent/depth` 直接读 `GameState` 字段。
- 结构化决策元数据：`_night_decision_cells(state)`、`_day_decision_candidates(state)`（仅增不改现有方法）。

### 2.3 `_config.py`

- `--policy`（`exhaustive|online`，默认 `exhaustive`）
- `--lambda_risk`（float，默认 `1.0`，`[0,1]`）
- `--toggle`（`optimistic|conservative`，默认 `conservative`）
- `--lookahead_depth`（int，默认 `2`，决策点计；负数/None = 全深度）
- `--tactics`（str，默认 None，逗号分隔：`self_kill`(骗刀) / `no_kill`(空刀)）
- `--online_trace_path`（str，默认 `online_trace.json`）
- `--compare_with_exact`（store_true）
- `--start_state_json` / `--start_state_path`（自定义起始状态）
- `--lang`（`zh-CN|en-US`，默认 `zh-CN`）
- 同步 `UI_LABELS` / `GUI_TOOLTIPS` / `SIMULATOR_ARG_KEYS` / `ARTIFACT_ARG_KEYS` / `CONFIG_SUMMARY_KEYS`。

### 2.4 `_gui.py`

> ⚠️ 本节为早期设计描述；**最终实现以 §0 状态清单与 §4 复盘为准**——编辑器简化为「玩家 TreeView + 编辑对话框 + 文本字段」，「合法性检测/Spinbox/身份探知勾选行/红字提示」未落地；战术由 TreeView 改为父级勾选树；语言下拉已移除（默认 zh-CN）；文案统一走 `t()`。

- 参数面板：λ slider（`0~1`）、toggle 开关。
- **可视化自定义起始状态编辑器**（替代 JSON 文本区）：
  - 玩家人数增删；职业下拉（随人数动态计算 + 合法性检测：狼≥1、狼<总数、至少 1 非狼好人、神职各≤1、技能与职业一致）；
  - 每位玩家：存活/死亡勾选、技能 Spinbox；
  - 预言家身份探知勾选行（目标 → 狼/好人）；
  - `phase` 单选（day/night）、`night_count`/`day_count` Spinbox、`last_guard_target` 下拉；
  - 非法组合红字提示、禁止运行；填了即覆盖参数面板（置灰人数参数）；`parent=None/depth=0`。
- **战术 TreeView**：`--smart_vote` 勾选时显示，树形勾选「骗刀 / 空刀」。
- **i18n + hover**：`zh-CN`/`en-US` 双语文案映射 + 语言下拉即时切换 + 每控件 hover tooltip；缺 key 回退中文。

### 2.5 `__main__.py` / `_artifacts.py`

- `__main__.py` 按 `SIMULATOR_ARG_KEYS` 透传；`policy == "online"` 时调 `run_online(start_state=...)`。
- `_artifacts.py`：online 模式跳过状态树绘图/文本树，改调 `emit_online_artifacts`。

### 2.6 `_reporting.py`

- `save_endings_json`：终局条目加 `"reward_interval": [lower, upper]`。
- `build_results_report`：在线模式加区间摘要（根节点区间 + 各步区间轨迹）。
- trace 每步存 `optimistic_interval` + `conservative_interval` + `chosen_interval`（4 位小数、JSON 留 float）。

### 2.7 `_plotting.py` / `_text_tree.py`

- `_build_plot_labels` / `_format_node_label`：节点标签追加 `区间=[lower,upper]`（`None` 不显示）。
- 在线模式：绘制**参考路径链**（不画完整前瞻子树），节点带区间；穷举树不受影响。

### 2.8 `__init__.py`

- re-export `RewardInterval`、`SearchSimulator`。

## 3. 测试与验收

新增 `tests/test_search_simulator_online.py`：

- `_game_state`：`phase` 默认值、`transition` 白天自增 `day_count` 且翻转 phase、签名含 phase/day_count；`reward_interval` 默认 None；`to_dict`/`from_dict` 往返一致。
- `_interval`：不变量与 `[-1,+1]` 夹取；`merge` 乐观=并集/保守=交集（`v1=(-0.2,0.8)`、`v2=(-0.5,0.5)`，λ=1 得 `(-0.5,0.8)` / `(-0.2,0.5)`）；λ=0 坍缩为均值；`UNRESOLVED=[-1,+1]`。
- `_zero_sum`：`camp_of_role`、`terminal_utility` 三套狼胜文案。
- `_minimax`：λ=1 全深度价值与穷举方向一致；深度增大区间单调收紧；λ=0 坍缩为均值；toggle 乐观→并集、保守→交集；前沿返回 `[-1,+1]`。
- 战术：开启骗刀（女巫解药>0）时狼可选目标含狼、否则不含；开启空刀时存在 `wolf_target=None` 分支且本夜无狼刀死亡。
- 环检测：空刀 + 无有效投票的极端状态不无限循环，按 `[-1,+1]` 停止。
- `_online_policy`：路径到真终局、trace 双区间齐全、chosen 在 candidates 内、phase 逐步翻转。
- 自定义状态编辑器合法性：狼≥总数等非法组合被拒绝；职业分配随人数动态计算。
- i18n：`zh-CN`/`en-US` 文案齐全，缺 key 回退中文。
- 集成：`policy="online"` 跑通产出 `online_trace.json`，参考路径各状态 `reward_interval` 非空、落在 `[-1,+1]`；自定义 `start_state` 起迭代 trace 首步 phase 与输入一致、`parent=None/depth=0`；默认 `exhaustive` 跑一次断言 `wins` 与旧行为一致。

**验收命令**（父项目 venv 下，遵循 `search_simulator/AGENTS.md`）：

```powershell
chcp 65001; ..\.venv\Scripts\Activate.ps1; $env:PYTHONUTF8="1"; $env:PYTHONIOENCODING="utf-8"; $env:PYTHONPATH=".."
python -m search_simulator --cli --number_of_players 5 --number_of_wolves 1
python -m search_simulator --cli --policy online --number_of_players 5 --number_of_wolves 1 --lookahead_depth 2 --lambda_risk 0.8 --toggle conservative
python -m search_simulator --cli --policy online --smart_vote --tactics self_kill,no_kill --number_of_players 6 --number_of_wolves 2 --lookahead_depth 2
python -m search_simulator --cli --policy online --number_of_players 4 --number_of_wolves 1 --compare_with_exact --lookahead_depth -1 --lambda_risk 1.0
pytest tests/test_search_simulator_online.py -q
```

## 4. 复盘整理：清单之外的后续修改

> 以下修改发生在上文清单定稿之后，补记于此，作为与最终实现一致的事实来源（对应 §0 中标注「复盘新增」的项）。

### 4.1 i18n 重构为 `t()` 函数（消除散落硬编码）
- 新增 `_i18n.py` 的 `t(key, *args, **kwargs)`：按当前语言取文本并 `.format(*args, **kwargs)`（等价 f-string）；`set_language(lang)` 切换语言；`t_en(key, ...)` 供无 CJK 字体的绘图回退。
- 所有面向用户文案收敛到 `STRINGS` / `EN_STRINGS` 单表；`_gui.py`、`_config.py`、`_reporting.py`、`_plotting.py`、`_online_policy.py`、`_text_tree.py`、`_artifacts.py`、`_simulator.py` 全部迁移。
- 语言切换入口隐藏（默认 `zh-CN`）；领域数据（角色名/技能名/胜负结果串/phase 机器值）**不翻译**（参与逻辑判断与 JSON 序列化）。

### 4.2 `_config.py` / `_i18n.py` 职责拆分
- `_config.py` 只留数据（`ArgumentSpec`、`ARGUMENT_SPECS`、`SIMULATOR_ARG_KEYS`、`ARTIFACT_ARG_KEYS`、GUI 布局常量等）；文本全部进 `_i18n.py`。
- `CONFIG_SUMMARY_KEYS` 从 `_config.py` 移入 `_i18n.py`，`_config.py` 改用 `from ._i18n import t`，打破 `_i18n -> _config` 循环导入。
- `build_parser()` 的 `--help` 文案经 `t("help.*")` 取值，`parser.description` 走 `t("parser.description")`。

### 4.3 GUI 布局与交互（三栏弹性 + 紧凑）
- 三栏 grid 布局（左=基础/角色，中=性能/在线/战术，右=自定义状态/控制/状态），窗口 `1560x760`、最小 `1100x640`，保证所有内容可见。
- λ slider（`0~1`）带实时数值标签；toggle/搜索模式/运行模式下拉显示值统一走 `t("opt.*")`。

### 4.4 迭代树预览：弃用 Treeview → 自定义分层视图
- `Canvas + 垂直 Scrollbar + 嵌套 Frame` 取代 `ttk.Treeview`；每行 = 缩进 + 独立 `▸/▾` 展开器 + 文本标签。
- **只有点 `▸/▾` 才展开/收缩，点节点文字不再切换**（消除 Treeview 整行点击即 toggle 的误触）。
- 根节点默认展开、非根默认折叠；`nodes_open` 保留展开状态与滚动位置；`_MAX_RENDERED_NODES = 1200` 截断保护（避免大穷举树卡死 GUI）。

### 4.5 智能投票 + 战术：合并为父级勾选树
- 弃用 Treeview，改为原生 `ttk.Checkbutton` 嵌套：父「智能投票剪枝」勾选后显示缩进的「骗刀 / 空刀」子勾选；父未勾选时子项隐藏且所有战术失效。
- 解决「点勾选也触发 +- 展开/收缩」的 Treeview 冲突（对应 `ALGORITHM_DESIGN.md` §6.7 与 A9 的「TreeView」描述已过时）。

### 4.6 `_minimax.evaluate` 签名修订
- 实际签名**无 `camp` 参数**：`evaluate(state, *, depth, oracle, toggle, lambda_risk, seen=frozenset())`；环检测用路径签名 `seen`（原清单 §1.3 的 `camp` 为过时描述，以本节为准）。

### 4.7 Python 3.14 兼容性修复
- **闭包/cell 损坏**：`_bounded_vote_flow_feasible` 的嵌套函数抽为模块级 `_flow_add_capacity_edge` / `_flow_add_bounded_edge` / `_flow_bfs_level` / `_flow_dfs`（显式参数，不再闭包捕获可变状态），修复非确定性的 `'cell' object is not an iterator` / `'int' object is not callable`。
- **线程安全**：工作线程改用 `queue.Queue` + 主线程 `_poll_main_queue` 轮询，不再从非主线程 `root.after`（修复 `RuntimeError: main thread is not in main loop`）。
- **递归特化闪退（补充）**：`_flow_dfs` 的递归实现触发 CPython 3.14.0 特化解释器 `_PyEval_EvalFrameDefault: Executing a cache` 致命错误（7人2狼 smart_vote 全战术 深度5 在线决策闪退）；改为**显式栈迭代实现**（去掉递归与内联 `min` 调用），7人2狼 smart_vote 穷举结果不变（1214=656/442/90/26）。

### 4.8 自定义状态编辑器：实际实现比清单更简洁
- 实际为「玩家 TreeView（职业/存活/技能三列）+ 添加/删除/编辑对话框 + phase/night/day/守卫守护索引/预言家查验文本字段」。
- 清单 §2.4 描述的「合法性检测（狼≥1、狼<总数、神职各≤1、技能与职业一致）、技能 Spinbox、预言家身份探知勾选行、非法组合红字提示」**未落地**，属待办；`_build_custom_state()` 目前不做合法性校验，`phase`/`day_count`/`seer_check_results` 直接由输入构建。

> 后续若要补上编辑器合法性校验，建议在 `_build_custom_state()` 内复用 `_simulator` 的胜负/角色约束做前置检查，并回写红字提示与「禁止运行」门控。

### 4.9 前瞻深度默认全深度 + 换位表提速
- `--lookahead_depth` 默认由 `2` 改为 `None`（全深度）：深度过浅会在到达终局前返回未决区间，导致 reward 计算无意义；全深度让 `evaluate` 递归到真终局（退化环由 `seen` 路径签名兜底）。
- `_minimax.evaluate` 增加换位表（transposition table）`cache`，以 `(签名, depth)` 缓存子图价值；`run_online_reference` 跨决策步共享同一 `cache`，使全深度评估退化为对状态 DAG 的一趟遍历（等价于穷举搜索的复杂度），避免同一状态被不同路径反复重算的指数级重复。
- 未引入 numpy/GPU：本算法瓶颈是「游戏树的组合爆炸 + 复杂 Python 状态转移（deepcopy/角色技能判定/Dinic 流）」，属不规则树遍历，无法向量化；区间 `merge` 本身是 O(n) 常数级。换位表才是对症的提速手段。

### 4.10 全局崩溃处理器 `_crash_handler.py`
- 新增 `install_crash_handlers()`：`sys.excepthook` / `threading.excepthook` / `sys.unraisablehook` 落盘，`faulthandler.enable(file)` 转储 C 级致命错误（`Py_FatalError` / 段错误）线程栈。
- 日志默认 `search_simulator_crash.log`（`*.log` 已 gitignore），可用环境变量 `SEARCH_SIMULATOR_CRASH_LOG` 覆盖；`__main__.py` 在 `main()` 入口安装（失败不阻断主流程）。
