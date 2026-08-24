# Search Simulator 代码更新记录

本文档只记录 `search_simulator/` 的工程实现事实；当前算法模型、状态转移语义和数学公式见 `ALGORITHM_DESIGN_V2.md`，V1 只作历史归档。

## 持久化分层

- SQLite 通过 SQLAlchemy Core 元数据和表达式 API 创建、查询、写入、更新、删除及校验，不向搜索层暴露 cursor，也不在源码中使用明文 SQL。
- `lru` 表保存站位感知的状态签名热缓存。内存 `OrderedDict` 负责最近命中，SQLite `lru` 负责容量受限的磁盘回落；interval、节点边关系和 UI 字段不进入该表的签名键。
- `memory` 表保存运行批次和断点信息，包括规范配置、终态、摘要、已完成站位数、下一恢复站位和更新时间。站位节点/边数量校验通过后才写入可恢复检查点。
- `solution` 表保存完整运行的站位结果索引，包含规范解配置、来源运行 ID、站位摘要和节点/边计数。只有 `complete` 且完整性校验通过的站位才能登记；中断和失败运行不会进入该表。
- 既有 `simulation_runs`、`position_results`、`graph_nodes`、`graph_edges` 表继续承担图数据存储；`memory`/`solution` 是运行状态和解索引层，不复制整棵 DAG。
- 打开旧缓存时通过 Core 批量迁移 `state_signatures` 到 `lru`，并为旧 `simulation_runs` 补建 `memory` 行；不会执行明文迁移 SQL，也不会删除旧图数据。

## solution 优先加载

- 搜索协调器启动时先通过持久化 API 查找同一规范配置的完整 `solution`。
- 命中时返回 `loaded_solution=true` 的结果对象，载入站位摘要；图节点和边仍按 UI 选择通过持久化 API 分页/按站位读取，不在加载路径复制完整 DAG。
- GUI 首次点击运行时显示“已载入已有解”，保留结果行和 DAG 查看能力；下一次点击将显式传入 `force_recompute=true`，创建新的运行批次并重新展开。
- CLI 提供同名的显式强制重算参数，默认保持 solution 优先加载。没有已有 solution 时，默认请求直接进入正常 DFS/BFS 搜索和 `memory` 断点流程。
- 重新计算完成后，新的完整站位摘要写入 `solution`，并保留旧运行记录以支持历史审计。

## 参数与边界

- `force_recompute` 从 CLI/GUI 入口、模拟器配置到站位批处理均使用具名字段显式传递。
- `lambda_risk` 仍只用于搜索后的 interval 观测；solution 配置键在比较时排除该字段，使同一 DAG 可以动态观察不同 lambda。
- 读取已有 solution 不改变状态签名、DAG 合并、路径计数和 interval 传播；重新计算才会创建新的搜索任务和新的检查点。

## Windows 原生崩溃隔离与站位内恢复

- Windows 物理内存读取改为模块级静态 `MEMORYSTATUSEX` ABI 与显式 `argtypes`/`restype` 绑定，避免在内存检查热路径反复创建 ctypes 类型和使用隐式调用签名。
- 计算 worker 以 `-S` 隔离启动，启动后校验不得加载 Pygame、SQLAlchemy 或 greenlet；父进程仍使用项目共享的 Python 3.12 `.venv` 和完整 site 环境。
- worker 常驻状态键升级为可逆的单块二进制编码，持久化/API 边界再恢复为扁平数组；状态签名延迟到批量落库时生成。旧版 v2 站位内检查点在恢复时原地迁移为 v3，不同时保留新旧两份去重索引。
- 邻接拓扑改用连续整数数组保存父节点、子节点和 multiplicity；派生原因只在当前未落库分块暂存，分块边通过有界结果队列交给唯一 SQLite 写入者。
- 每个 worker 只处理一个站位分块，随后退出。其检查点对象没有引用环，因此 worker 内关闭循环 GC，普通引用计数仍正常工作；进程退出回收全部剩余对象。此隔离仅作用于计算子进程，不修改 GUI/父进程 GC。
- 系统可用内存由父协调器在每个短分块派发前后检查；worker 通过显式任务参数禁止调用系统物理内存 ctypes API，避免大型 Python 堆展开与 Win32 ABI 同时处于热路径。内存安全保留线仍生效，最坏延迟为一个有界分块。
- 每个分块使用独立进程池。若子进程发生 access violation、`BrokenProcessPool` 或解释器对象槽位错位，协调器保留上一个原子检查点、缩小节点预算并自动重试；连续失败超过上限才把运行写为 `failed`。成功检查点会把实际预算写入 `memory`，应用重启后沿用该预算，避免重复触发已知危险窗口。
- 站位内 checkpoint 和完整站位 checkpoint 使用不同完成语义。暂存摘要的节点/边计数使用 `-1` 哨兵；只有实际节点数大于零，且节点/边实际计数分别等于最终摘要时，站位才能被跳过或登记到 `solution`。
- `complete` 终态增加二次防线：摘要必须覆盖全部预期站位，每个站位至少存在根节点，否则持久化 API 拒绝登记 solution。历史上由 `0 == 0` 暂存计数造成的空图不会再被识别为已有解。

## 本次压力验证结论

- 原始 access violation 的栈帧曾落在状态克隆、区间回传、昼夜分支和状态编码等不同纯 Python 行；它不是可复现于某一条游戏规则的确定性异常。
- 单独对状态克隆、签名和 Windows 内存 ABI 进行百万级压力均可通过；默认 7 人单站位搜索则显示，风险随恢复检查点中的 Python 容器数量增长而上升。
- 同一 `run_id` 从 150,000 节点的旧检查点恢复，v3 迁移后先越过此前崩溃点并写到 225,000；协调器自动重试、二进制状态键、GC 隔离和 worker ABI 隔离共同把可恢复检查点推进到已处理 593,750、已发现 595,813、边 3,019,460、frontier 2,063。
- 该压力运行已显式写为 `interrupted` 并保留站位内检查点，没有登记为 solution。完整 pickle 图在规模继续增长时仍会迫使 worker 缩小分块预算，因此当前实现提供可靠的崩溃恢复保底，但尚不能把“整图反序列化”的规模风险表述为已经消除。

## 验证记录

2026-08-23 实际验证：

- 使用项目共享 Python 3.12 `.venv` 执行 `pytest tests/test_search_simulator_tree.py -q`：45 项通过；包含站位内恢复、暂存空图拒绝、完整 solution 防线、Core/Inspector schema、结果队列背压和无明文 SQL 回归。
- 修改模块的 `py_compile` 通过。
- 3 人 1 狼最小局 DFS CLI 完成 1/1 站位；同一 SQLite 第二次运行输出 `SOLUTION_LOADED` 且没有创建 worker。
- 3 人 1 狼显式 BFS CLI 完成 1/1 站位，结果与 DFS 的终局 interval 一致。
- Pygame 使用 dummy 视频/音频驱动启动后主循环保持存活 4 秒，由验证脚本主动停止。
- 本轮验证生成的 `crash_log/` 按约束保留。递归删除隔离验证目录与 pytest 临时目录的命令已被当前执行环境策略拒绝，未伪报清理成功。

2026-08-23 GUI 默认模式压力观察：

- 通过 Pygame GUI 保持默认 7 人板子、智能投票和战术勾选，仅取消“所有站位”，固定 DFS 单站位运行。
- 运行约 142 秒后由 UI 暂停，现场计数为已展开 327,909、已发现 330,249、边 1,636,573、终局节点 186,687、frontier 2,340；暂停状态保留可恢复断点，未误报为完成。
- 视觉检查发现旧 `_layout_graph` 将搜索深度映射到纵轴、同层节点映射到横轴，与研究 UI 约定相反；已改为深度 X 轴递增、同层节点按稳定 `node_id` 从上到下排列，并补充中文不变量注释。
- 重启修正版 GUI 后再次取消“所有站位”运行 DFS，约 246 秒时暂停，已展开 379,985、已发现 382,121、边 1,856,622、终局节点 218,542；实时观测窗口出现多层节点列，视觉上确认深度向右、同层向下，未出现新的原生崩溃。
- 使用项目 `.venv` 对 `_layout_graph` 做静态坐标回归：深度 0/1/2 分别得到 X=0/105/210，同层两个节点得到 Y=-72.5/72.5；`py_compile _gui.py` 通过。
- DAG 预览横向基准改为画布左内边距（不再以画布中心为根层基准），因此根节点左对齐、后续深度向右展开，同时保留平移和缩放；新增中文注释说明该坐标不变量。
- DAG 边的派生原因标签改为仅在悬停折线时显示，常态只保留分支线；悬停提示增加完整原因标题与全部原因行，取消实时预览对原因数量和文本长度的截断。
- 本次验证：45 项模块测试通过，`ruff`、`py_compile` 和 Pygame dummy 3 帧 GUI 启动通过；边 hover 文本统一走换行/多列路径，不再受短提示的前 7 行限制。
- DAG 控件区新增“定位根节点”按钮；点击只重置本地画布平移并选中稳定的最小入口节点，不改变展开集合、frontier 或持久化图。按钮、说明文字和状态提示均纳入中英文 i18n。
- 根节点定位回归测试加入后，模块测试为 45 项通过；ruff、py_compile、Pygame dummy 3 帧启动和 `git diff --check` 均通过。

## 2026-08-23 文档整理与 GUI 视觉验收

- `ALGORITHM_DESIGN.md` 重新收敛为纯算法文档：保留零和建模、信息隔离、状态等价、战术分支、终局效用和 wide/narrow 公式；移除工程文件、SQLite、Pygame、进程和测试清单，避免与代码文档混写。
- `strategy_implemetation.md` 补充同级目标不得用票数向量裁决、隔离 worker/有界队列、运行终态/crash 证据、实时观测窗口和清理纪律；边原因明确为“折线 hover 显示、常态隐藏”。
- `CODE_DESIGN.md` 增加历史文档声明：当前工程事实、测试数量和最新验证以本文件为准，旧日期记录不覆盖增量记录。
- 使用默认 7 人板子、智能投票和战术勾选，取消“所有站位”后启动固定 DFS 单站位视觉检测；运行期间实时显示展开/发现/frontier/边/终局计数，随后主动暂停，现场未将未完成站位误报为 complete。
- 视觉上确认 DAG 深度从左到右、同层节点从上到下；“定位根节点”能把根节点放到缩略图左侧并选中；“全部展开/全部收起”只改变当前观测窗口；节点 hover 使用中文分区文本显示完整 GameState。
- 视觉检测发现实时 DAG 边密集时单条边 hover 不易稳定命中；代码路径已保留完整原因多行渲染，但该 hit-test 仍列为下一轮 GUI 回归的显式检查项，不能仅凭节点详情栏替代边 hover 通过证据。
- 本轮只产生文档改动；临时 GUI 进程已关闭，未新增或提交测试数据库、JSON、截图和日志。

## 2026-08-23 DAG 网格深度坐标布局

- `_gui.py` 的 DAG 坐标改为固定网格：节点显式迭代深度优先映射到 X 轴列，旧持久化节点回退读取紧凑观测中的深度，再退化为昼夜轮次之和；同一深度按稳定 `node_id` 占用固定 Y 轴网格行。
- DAG 视口新增横向“迭代深度 X”坐标轴、`D0`/`D1` 深度刻度、竖向虚线深度分隔和有限范围的横向网格线；网格绘制受当前视口和缩放限制，不改变状态、边或 interval。
- 轴线和刻度为节点绘制预留底部空间，根节点定位、平移、缩放、点击展开/收起和 hover 命中继续使用同一画布坐标变换。
- 新增固定列/行坐标回归测试；共享 Python 3.12 `.venv` 下模块测试为 46 项通过，`ruff`、`py_compile` 和 Pygame dummy 图形绘制 smoke 均通过。

## 2026-08-23 Computer Use GUI 视觉验收规范

- 将 GUI 视觉验收要求固化到 `AGENTS.md`：任何 GUI 代码、布局、样式、交互、hover、动画或可视化改动，都必须用 Computer Use（电脑工具）启动真实 Windows 窗口检查；离屏 smoke、截图或坐标回归只能作为补充，不能替代真实窗口验收。
- 本轮使用电脑工具启动真实 `狼人杀树分支迭代模拟器`，确认中文固定 DFS 界面、lambda 滑块与数值显示、参数 hover 文本、角色勾选与战术禁用提示、单站位模式，以及开始迭代后的运行状态和完成弹窗均可见且未发现明显重叠。
- 以 3 人 1 狼、无神职、关闭“全部站位”的最小配置完成 1/1 站位 DFS；完成弹窗显示运行 ID、检查点、日志路径和 crash 日志路径，结果表显示节点/边/终局计数。该次真实窗口检查未提交截图、临时数据库或日志文件。

## 2026-08-23 access violation 检查点与绘制路径修复

- 检查点格式升级为 v4：节点快照、frontier、父子节点数组、multiplicity 数组和派生原因分块写入独立 pickle 记录；节点块为 2048 条、原因块为 1024 条。写入临时文件后执行 flush/fsync，再原子替换正式检查点，避免一次性 `pickle.dump` 整棵大图造成瞬时序列化峰值。
- v4 不持久化可由节点快照重建的 `node_id_by_key` 重复索引；加载时校验 magic、版本、规范配置、计数和重复状态键。既有 v2/v3 检查点仍可读取，恢复后由下一次保存迁移为 v4。
- GUI 图形坐标统一经过有限值检查和边界裁剪。水平/垂直线使用安全整数填充，虚线先在小型透明离屏 surface 上合成后单次 blit 到真实窗口；DAG 边也使用安全绘制路径，避免把浮点或极端坐标直接交给 Windows Pygame-ce 原生绘制函数。
- 节点详情读取紧凑状态快照时改为统一的状态恢复函数，并对损坏或旧格式快照做受控异常处理，避免详情面板因紧凑元组下标不匹配引发绘制线程异常。

### 实际验证

- 使用 Computer Use 启动真实 Windows 窗口 `狼人杀树分支迭代模拟器`：启动画面稳定，中文固定 DFS 界面、深度轴、竖向深度分隔线和横向虚线网格可见，未出现标签重叠。
- 通过真实窗口设置 3 人、1 狼、关闭预言家/女巫/守卫和“全部站位”，保持智能投票；点击开始后立即显示 worker 启动中的运行状态和可用暂停按钮，随后完成 1/1 站位（3 个节点、2 条边、100%）。
- 通过真实窗口点击结果行加载持久化 DAG，节点详情栏显示中文玩家信息及完整边原因；本次验收期间未再次出现 access violation。验收结束后已关闭 GUI，并清理本轮遗留的孤立 Python worker helper 进程。
- 共享 Python 3.12 `.venv` 下 `pytest tests/test_search_simulator_tree.py -q` 为 46 项通过；`py_compile`、`ruff check` 和 `git diff --check` 通过。
- 独立 v4 检查点往返验证通过（分块读取、10 个节点、12 条边）；最小配置 DFS CLI 和显式 BFS CLI 均完成 1/1 站位；dummy Pygame 安全绘制 smoke 覆盖有限值、极端坐标和非有限坐标，真实 display 的裁剪/填充 smoke 通过。

## 2026-08-23 暂停迭代树 GUI 渲染

- `_gui.py` 增加临时开关 `ENABLE_ITERATION_TREE_RENDERING = False`。GUI 不再进入实时预览节点合并、SQLite DAG 加载、布局、节点/边绘制、hover、平移缩放和 lambda interval 重算路径；“全部展开/收起/定位根节点”控件同步隐藏。
- 后台进度队列仍由 GUI 持续消费，站位进度、结果表、统计卡片、进度条、暂停/恢复、检查点续算和终态弹窗保持可用。树区域改为明确提示面板，不保留旧的空白或假进度展示。
- 使用 Computer Use 启动真实 Windows GUI，设置 3 人、1 狼，关闭预言家/女巫/守卫和“全部站位”，先验证已有 solution 加载，再点击“重新迭代 · DFS”强制启动 worker。worker 运行中立即显示“迭代中 · DFS”和可用暂停按钮，最终完成 1/1 站位；结果表为 3 个状态、2 条边、2 个终局，树统计不触发 DAG 绘制。
- 完成弹窗显示 `complete`、运行 ID、检查点 `1/1`、无下一恢复站位和日志路径；本次运行对应 crash 日志大小为 0，验收期间未再次出现 access violation。窗口已通过真实 GUI 操作关闭。
- 本次代码验证：`py_compile _gui.py`、`ruff check _gui.py`、`pytest tests/test_search_simulator_tree.py -q`（46 项通过）和 `git diff --check` 均通过。该开关为临时 GUI 降级措施，后续恢复树 UI 时需重新执行真实窗口视觉验收。

## 2026-08-24 worker 检查点边原因流式修复

- 根据两条 worker 都落在 `_save_search_checkpoint` 边原因 pickle 行的崩溃栈，确认计算 worker 构造 `SearchSimulator` 时遗漏了 `result_queue`。因此 `_search_root` 的 `stream_staged_edges` 一直为假，边原因持续积累到 checkpoint 保存阶段，最终在大批量 `pending_reasons` 序列化时触发 Windows 原生 access violation。
- `_position_task` 现在显式把 `result_queue` 传入 worker 模拟器；检查点边在保存前通过有界结果队列分批交给父进程并从 worker 暂存中释放，checkpoint 不再重复持有已写入边的原因对象。
- 从站位内检查点恢复时，父进程先重建/清空暂存站位；worker 将已有边从游标 0 重新流式回放，兼容此前未流式保存的旧检查点，避免恢复后再次把历史原因聚合进 pickle。
- 新增检查点回归测试，验证边原因先进入 `position_stage_edges`，且保存后的 checkpoint `pending_reasons` 为空。

### 实际验证

- `pytest tests/test_search_simulator_tree.py -q`：47 项通过。
- 3 人、1 狼、无神职、单站位强制 DFS：1/1 完成，3 个状态、2 条边、2 个终局。
- 同一 SQLite 第二次 DFS：输出 `SOLUTION_LOADED`，未创建新的 worker。
- 同配置显式 BFS：1/1 完成，3 个状态、2 条边、2 个终局。
- `py_compile _tree_search.py`、`ruff check _tree_search.py` 和 `git diff --check` 通过；隔离 CLI 输出目录已清理，正式 `crash_log/` 按约束保留。

## 2026-08-24 关闭 GUI 实时预览负载

- 根据 `BrokenProcessPool` 后续 crash 日志确认：GUI 树渲染已经关闭，但 worker 仍在构造完整节点快照、边原因和实时预览事件；这会额外占用 worker 堆、跨进程队列和父进程内存，增加状态编码与 SQLite 写入并发压力。
- `SearchSimulator` 增加显式 `live_preview_enabled` 边界参数。GUI 固定传 `False`；worker 在该开关关闭时只发布已展开/已发现/frontier/边/终局计数和 path/interval 后处理事件，不构造或传输 `preview_nodes`、`preview_edges`。CLI/API 未显式关闭时保持原有默认值 `True`。
- 该改动不改变 DFS/BFS 分支、状态签名、检查点格式、结果队列背压和 SQLite 持久化语义；关闭的是观测负载，不是后台迭代。
- 新增回归测试，验证关闭实时预览时仍有进度事件且不会调用紧凑状态到完整 GameState 的预览重建函数。

### 实际验证

- 共享 Python 3.12 `.venv`：`pytest tests/test_search_simulator_tree.py -q`，48 项通过。
- `py_compile`、`ruff check`（`_tree_search.py`、`_simulator.py`、`__main__.py`、`_gui.py` 及测试）和 `git diff --check` 通过。
- 3 人、1 狼、无神职单站位：DFS 完成 1/1；同一 SQLite 第二次命中 `SOLUTION_LOADED`；显式 BFS 强制重算完成 1/1，三次退出码均为 0。
- 使用 Computer Use 启动真实 Windows GUI，确认崩溃提示可见且可关闭；关闭提示后真实窗口显示“迭代树渲染已暂时关闭”，后台 DFS/检查点/结果统计说明可见，未进入 DAG 绘制路径。验收窗口随后已关闭。
- 随后在真实 GUI 设置 3 人、1 狼、无预言家/女巫/守卫、单站位，第一次点击命中已有 solution；第二次点击“重新迭代 · DFS”确实进入“正在计算 1/1 个站位”，完成弹窗显示 1/1、3 状态、2 边、2 终局。该启动对应 crash 日志大小为 0，未再次出现 access violation。
- 用户提供的 `run_id=6f640f206c4c4b269b45adec27fd37a4` 仍保留在正式日志和 crash 日志中作为故障证据；该运行的 `BrokenProcessPool` 重试曾从 70,000 推进到 90,000 个已处理状态，但最终未形成终态，不能将其记为完成。
