"""i18n：t(key, *args) 统一文本转化与格式化，避免散落硬编码。

约定：
- 所有面向用户的文案都收敛到本文件的 STRINGS / EN_STRINGS 两张表；
- 通过 t(key, *args, **kwargs) 取文本并做格式化（等价 f-string）；
- 领域数据（角色名、技能名、胜负结果串、phase 机器值）不进入本表，
  它们同时参与逻辑判断与 JSON 序列化，属于数据而非界面文案。
"""

from __future__ import annotations

STRINGS: dict[str, str] = {
    # ---- 字段标签 ----
    "label.number_of_players": "玩家人数",
    "label.number_of_wolves": "狼人数量",
    "label.search_mode": "搜索模式",
    "label.include_seer": "预言家",
    "label.include_witch": "女巫",
    "label.include_guard": "守卫",
    "label.include_hunter": "猎人",
    "label.include_white_werewolf_king": "白狼王",
    "label.include_sheriff": "启用警长归票",
    "label.smart_vote": "智能投票剪枝",
    "label.max_processed_states": "最大处理状态数",
    "label.max_queue_size": "最大队列长度",
    "label.max_runtime_seconds": "最大运行时长(秒)",
    "label.max_night_branches_per_state": "夜晚分支上限",
    "label.max_day_branches_per_state": "白天分支上限",
    "label.gc_interval": "GC间隔",
    "label.parallel_workers": "并行线程数",
    "label.disable_plot": "禁用绘图",
    "label.max_nodes_for_plot": "绘图节点上限",
    "label.plot_dpi": "绘图DPI",
    "label.export_text_tree": "导出文本树",
    "label.text_tree_output_path": "文本树输出路径",
    "label.max_text_tree_nodes": "文本树节点上限",
    "label.policy": "运行模式",
    "label.lambda_risk": "迭代风险 λ",
    "label.toggle": "乐观/保守",
    "label.lookahead_depth": "前瞻深度",
    "label.tactics": "夜间战术",
    "label.online_trace_path": "轨迹输出路径",
    "label.compare_with_exact": "精确对照",
    "label.start_state_json": "起始状态 JSON",
    "label.start_state_path": "起始状态文件",
    "label.lang": "语言",

    # ---- hover 提示 ----
    "tip.number_of_players": "本局玩家总人数，当前模拟器支持 5-16 人。",
    "tip.number_of_wolves": "狼人阵营数量，必须小于玩家总人数。",
    "tip.search_mode": "状态树遍历方式：深度优先更省队列内存，广度优先更适合按层观察分支。",
    "tip.include_seer": "加入预言家角色；启用智能投票时，夜晚查验会影响后续白天投票分支。",
    "tip.include_witch": "加入女巫角色，夜晚会产生救药和毒药相关分支。",
    "tip.include_guard": "加入守卫角色，夜晚会产生守护目标分支。",
    "tip.include_hunter": "加入猎人角色，满足条件时会产生开枪分支。",
    "tip.include_white_werewolf_king": "加入白狼王角色，满足条件时会产生自爆分支。",
    "tip.include_sheriff": "启用警长机制，模拟警徽竞选、警长归票和警徽流转。",
    "tip.smart_vote": "启用智能投票剪枝：白天不自投；预言家会按已缓存查验结果定向投票。",
    "tip.max_processed_states": "最多处理多少个状态节点；留空表示不限制，建议试跑时先填较小值。",
    "tip.max_queue_size": "广度优先队列最大长度；只对广度优先有明显影响，留空表示不限制。",
    "tip.max_runtime_seconds": "模拟最长运行秒数；到达后提前停止，留空表示不限制。",
    "tip.max_night_branches_per_state": "单个状态的夜晚行动最多保留多少个分支，留空表示不剪枝。",
    "tip.max_day_branches_per_state": "单个状态的白天行动最多保留多少个分支，留空表示不剪枝。",
    "tip.gc_interval": "每处理多少个状态主动触发一次垃圾回收；数值越小回收越频繁。",
    "tip.parallel_workers": "并行处理线程数；1 表示单线程，较大值可能提高速度但增加资源占用。",
    "tip.disable_plot": "跳过状态树图片输出，只保留统计和可选文本树。",
    "tip.max_nodes_for_plot": "绘图节点数量上限；超过后跳过图片输出，避免图像过大或后端卡住。",
    "tip.plot_dpi": "状态树图片输出分辨率；只控制清晰度，画布尺寸由叶节点数量和标签宽度自动计算。",
    "tip.export_text_tree": "导出类似树形命令的文本状态树，适合查看大分支或避免图片标签遮挡。",
    "tip.text_tree_output_path": "文本状态树输出路径，启用“导出文本树”后生效。",
    "tip.max_text_tree_nodes": "文本状态树最多导出多少个节点；超过后跳过导出，避免生成过大文件。",
    "tip.policy": "穷举搜索=全树搜索；在线决策=在线参考决策。",
    "tip.lambda_risk": "迭代风险参数 λ ∈ [0,1]：1=对抗(并集/交集)，0=均值。",
    "tip.toggle": "乐观=放大(并集)；保守=缩小(交集)。",
    "tip.lookahead_depth": "前瞻深度(按决策点计)；负数或留空=全深度。",
    "tip.tactics": "夜间战术，逗号分隔：骗刀、空刀。",
    "tip.online_trace_path": "在线参考轨迹输出路径。",
    "tip.compare_with_exact": "与全深度精确值对照（仅在线决策模式）。",
    "tip.start_state_json": "自定义起始状态 JSON 字符串。",
    "tip.start_state_path": "自定义起始状态 JSON 文件路径。",
    "tip.lang": "图形界面语言。",

    # ---- GUI 静态文案 ----
    "gui.title": "搜索模拟器 参数配置",
    "gui.panel.fields": "基础参数",
    "gui.panel.bools": "角色与规则",
    "gui.panel.limits": "性能与绘图",
    "gui.panel.status": "运行状态",
    "gui.panel.nodes": "迭代树预览",
    "gui.initial_summary": "点击“开始模拟”后将在后台执行。\n",
    "gui.run_button": "开始模拟",
    "gui.running_button": "正在模拟",
    "gui.waiting_status": "等待运行",
    "gui.running_status": "运行中... 已运行 {elapsed_seconds:.1f}s",
    "gui.failure_status": "运行失败",
    "gui.finished_status": "运行完成",
    "gui.nodes_hint": "展示最近处理的节点（含未结束节点），实时刷新",
    "gui.limits_hint": "提示：留空表示不限；可以先设置较小 {limit_label} 做试跑。",
    "gui.start_summary": "开始模拟...",
    "gui.config_summary_title": "当前参数配置:",
    "gui.param_error_title": "参数错误",
    "gui.param_error_template": "请检查参数格式：{error}",
    "gui.run_error_title": "运行失败",
    "gui.finish_title": "运行完成",
    "gui.error_summary_template": "错误: {error}",
    "gui.finish_processed": "处理状态数: {processed_states}",
    "gui.finish_endings": "终局数量: {ending_count}",
    "gui.finish_stop_reason": "停止原因: {stop_reason}",
    "gui.finish_wins": "胜负统计: {wins}",
    "gui.finish_message": "处理状态数: {processed_states}\n终局数量: {ending_count}\n停止原因: {stop_reason}",
    "node.finished": "结束",
    "node.ongoing": "未结束",
    "node.unknown_value": "?",
    "node.unknown_action": "未知",
    "node.line": (
        "#{state_id}({status}) p={parent_id} 存活{alive_count}/{total_players} "
        "Q={queue_length} 已处理={processed_states} | {action_label}"
    ),

    # ---- 下拉显示值（机器值映射见各模块） ----
    "opt.search_mode.dfs": "深度优先",
    "opt.search_mode.bfs": "广度优先",
    "opt.policy.exhaustive": "穷举搜索",
    "opt.policy.online": "在线决策",
    "opt.toggle.conservative": "保守",
    "opt.toggle.optimistic": "乐观",
    "opt.phase.night": "夜晚",
    "opt.phase.day": "白天",

    # ---- 夜间战术标签 ----
    "tactic.self_kill": "骗刀",
    "tactic.no_kill": "空刀",

    # ---- GUI 自定义起始状态编辑器 ----
    "gui.vote_tactics": "智能投票与战术",
    "gui.custom_state": "自定义起始状态",
    "gui.use_custom_state": "启用自定义起始状态",
    "gui.col.role": "职业",
    "gui.col.alive": "存活",
    "gui.col.skills": "技能",
    "gui.alive": "存活",
    "gui.dead": "死亡",
    "gui.edit_player": "编辑玩家",
    "gui.skills_hint": "技能(名:次数,逗号分隔)",
    "gui.save": "保存",
    "gui.add_player": "添加玩家",
    "gui.remove_selected": "删除选中",
    "gui.edit_selected": "编辑选中",
    "gui.phase_field": "phase",
    "gui.night_day": "night/day",
    "gui.guard_last_target": "守卫上轮守护索引",
    "gui.seer_checks": "预言家查验(索引:狼,逗号)",
    "gui.phase.idle": "待机",
    "gui.phase.search": "搜索迭代",
    "gui.phase.report": "报告导出",
    "gui.phase.plot": "图像绘制",
    "gui.phase.text_tree": "文本树导出",
    "gui.phase.done": "完成",
    "gui.too_many_nodes": "… 节点过多，仅显示前 {0} 个",
    "gui.tkinter_missing": "当前环境缺少 tkinter，无法启动 GUI。",

    # ---- 命令行 help ----
    "parser.description": "BFS Simulator",
    "help.number_of_players": "玩家人数(5-16),默认5人",
    "help.number_of_wolves": "狼人数量，默认1人",
    "help.include_seer": "是否包含预言家（默认不包含）",
    "help.include_witch": "是否包含女巫（默认不包含）",
    "help.include_guard": "是否包含守卫（默认不包含）",
    "help.include_hunter": "是否包含猎人（默认不包含）",
    "help.include_white_werewolf_king": "是否包含白狼王（默认不包含）",
    "help.include_sheriff": "是否启用警长归票机制（默认不启用）",
    "help.smart_vote": "启用智能投票剪枝：白天不自投，预言家夜晚查验会影响后续投票",
    "help.search_mode": "搜索模式：dfs(默认) 或 bfs",
    "help.max_processed_states": "最多处理的状态节点数（默认不限）",
    "help.max_queue_size": "BFS 队列最大长度，超出后新状态会被裁剪（默认不限）",
    "help.max_runtime_seconds": "最大运行时长（秒），到达后提前停止（默认不限）",
    "help.max_night_branches_per_state": "单个状态夜晚阶段最多保留分支数（默认不限）",
    "help.max_day_branches_per_state": "单个状态白天阶段最多保留分支数（默认不限）",
    "help.gc_interval": "每处理多少个状态主动触发一次 gc.collect（默认 2000）",
    "help.parallel_workers": "并行线程数（默认 1，表示单线程）",
    "help.policy": "运行模式：exhaustive(穷举) 或 online(在线参考决策)",
    "help.lambda_risk": "迭代风险参数 λ，范围 [0,1]",
    "help.toggle": "乐观/保守开关：optimistic(并集) 或 conservative(交集)",
    "help.lookahead_depth": "前瞻深度（决策点计；负数=全深度）",
    "help.tactics": "启用的夜间战术，逗号分隔：self_kill(骗刀),no_kill(空刀)",
    "help.online_trace_path": "在线参考轨迹输出路径",
    "help.compare_with_exact": "与全深度精确值对照（仅 online 模式）",
    "help.start_state_json": "自定义起始状态 JSON 字符串",
    "help.start_state_path": "自定义起始状态 JSON 文件路径",
    "help.lang": "GUI 界面语言",
    "help.disable_plot": "禁用状态树绘图（用于避免图形后端错误）",
    "help.max_nodes_for_plot": "绘图节点上限，超出则跳过绘图（默认 2500）",
    "help.plot_dpi": "输出图 DPI（默认 140）",
    "help.export_text_tree": "导出类似 tree 命令的文本状态树（默认不导出）",
    "help.text_tree_output_path": "文本状态树输出路径（默认 search_tree.txt）",
    "help.max_text_tree_nodes": "文本状态树节点上限，超出则跳过导出（默认 100000）",
    "help.gui": "打开可视化参数设置界面（默认无参数启动时自动打开）",
    "help.cli": "强制命令行模式（即使无参数也不打开 GUI）",

    # ---- 日志 ----
    "log.init_start": "开始初始化Search Simulator",
    "log.roles": "角色列表: {0}",
    "log.load_config": "加载Search Simulator配置",
    "log.callback_failed": "迭代回调执行失败，已忽略。",
    "log.run_start": "开始运行 Simulator (search_mode={0})",
    "log.game_end_stats": "游戏结束统计:\n{0}",
    "log.text_tree_empty_index": "状态索引为空，跳过文本树导出",
    "log.text_tree_no_nodes": "没有可导出的文本树节点，跳过文本树导出",
    "log.text_tree_too_many": "文本树节点数为 {0}，超过阈值 {1}，跳过文本树导出",
    "log.text_tree_saved": "文本状态树已保存到: {0}",
    "log.plot_empty_index": "状态索引为空，跳过绘图",
    "log.plot_no_nodes": "没有可绘制的节点，跳过绘图",
    "log.plot_too_many": "可绘制节点数为 {0}，超过阈值 {1}，跳过绘图以避免后端崩溃",
    "log.plot_size": "绘图尺寸自动计算为 {0:.2f}x{1:.2f} 英寸，叶子节点={2}，leaf_gap={3:.2f}，depth_gap={4:.2f}",
    "log.plot_saved": "状态树图已保存到: {0}",
    "log.ref_path_empty": "无决策步，跳过参考路径绘图",
    "log.ref_path_saved": "参考路径图已保存: {0}",
    "log.ref_path_plot_failed": "参考路径绘图失败，已忽略: {0}",
    "log.plot_disabled": "已禁用绘图（enable_plot=False）",
    "log.online_trace_saved": "在线参考轨迹已保存: {0}",
    "log.online_result": "在线参考结果: {0}",
    "log.online_root_interval": "根节点区间: {0}",
    "log.online_step_count": "决策步数: {0}",
    "log.online_step": "  #{0} [{1}/{2}] {3} chosen={4}",
    "log.online_summary": "在线参考摘要:\n{0}",
    "log.exact_saved": "精确对照已保存: {0}\n{1}",

    # ---- 报告文本 ----
    "report.total_endings": "总共模拟了 {0} 个终局\n",
    "report.result_count": "{0:<50} \t次数: {1:>5}\n",
    "report.search_mode": "搜索模式: {0}\n",
    "report.stop_reason": "停止原因: {0}\n",
    "report.processed": "已处理状态数: {0}\n",
    "report.queue_length": "当前待处理容器长度: {0}\n",
    "report.pruned": "因阈值裁剪分支数: {0}\n",
    "report.runtime": "运行耗时(秒): {0:.2f}\n",
    "report.cache_stats_title": "签名缓存统计:\n",
    "report.cache_db": "  sqlite文件: {0}\n",
    "report.cache_lru_capacity": "  LRU容量: {0}\n",
    "report.cache_lru_hits": "  LRU命中: {0}\n",
    "report.cache_sqlite_hits": "  SQLite命中: {0}\n",
    "report.cache_inserted": "  新增签名: {0}\n",
    "report.cache_visited_size": "  visited LRU大小: {0}\n",
    "report.cache_ending_size": "  ending LRU大小: {0}\n",

    # ---- 绘图标签 ----
    "plot.title": "搜索模拟器状态树",
    "plot.intermediate": "中间状态",
    "plot.village_win": "好人胜终局",
    "plot.wolf_win": "狼人胜终局",
    "plot.axis_branch": "分支序号",
    "plot.axis_depth": "深度（TD）",
    "plot.root_action": "根状态",
    "plot.none": "无",
    "plot.unfinished": "未结束",
    "plot.ref_title": "在线参考路径",
    "plot.ref_ylabel": "reward 区间",
    "plot.action_label": "行动",
    "plot.alive_status": "存活状态",
    "plot.result_label": "对局结果",

    # ---- 停止原因 / 动作标签 ----
    "stop.sim_done": "模拟完成",
    "stop.max_runtime": "到达最大运行时间",
    "stop.max_processed": "达到最大处理状态数",
    "action.root": "根状态",
    "action.unknown": "未知",

    # ---- 摘要展示 ----
    "summary.unlimited": "不限",
    "summary.yes": "是",
    "summary.no": "否",
}

EN_STRINGS: dict[str, str] = {
    # 字段标签
    "label.number_of_players": "Players",
    "label.number_of_wolves": "Wolves",
    "label.search_mode": "Search Mode",
    "label.include_seer": "Seer",
    "label.include_witch": "Witch",
    "label.include_guard": "Guard",
    "label.include_hunter": "Hunter",
    "label.include_white_werewolf_king": "White Wolf King",
    "label.include_sheriff": "Sheriff",
    "label.smart_vote": "Smart Vote",
    "label.max_processed_states": "Max Processed States",
    "label.max_queue_size": "Max Queue Length",
    "label.max_runtime_seconds": "Max Runtime (s)",
    "label.max_night_branches_per_state": "Night Branch Cap",
    "label.max_day_branches_per_state": "Day Branch Cap",
    "label.gc_interval": "GC Interval",
    "label.parallel_workers": "Parallel Workers",
    "label.disable_plot": "Disable Plot",
    "label.max_nodes_for_plot": "Max Plot Nodes",
    "label.plot_dpi": "Plot DPI",
    "label.export_text_tree": "Export Text Tree",
    "label.text_tree_output_path": "Text Tree Path",
    "label.max_text_tree_nodes": "Max Text Tree Nodes",
    "label.policy": "Policy",
    "label.lambda_risk": "Risk λ",
    "label.toggle": "Optimism",
    "label.lookahead_depth": "Lookahead Depth",
    "label.tactics": "Night Tactics",
    "label.online_trace_path": "Trace Path",
    "label.compare_with_exact": "Compare with Exact",
    "label.start_state_json": "Start State JSON",
    "label.start_state_path": "Start State Path",
    "label.lang": "Language",

    # hover 提示（缺 key 回退中文）
    "tip.number_of_players": "Total player count (5-16).",
    "tip.number_of_wolves": "Wolf count, must be less than total players.",
    "tip.search_mode": "DFS saves queue memory; BFS suits level-by-level observation.",
    "tip.include_seer": "Add a Seer; night checks affect day votes under smart_vote.",
    "tip.include_witch": "Add a Witch; night generates antidote/poison branches.",
    "tip.include_guard": "Add a Guard; night generates protect-target branches.",
    "tip.include_hunter": "Add a Hunter; death triggers a shoot branch.",
    "tip.include_white_werewolf_king": "Add a White Wolf King; death triggers a take-down branch.",
    "tip.include_sheriff": "Enable sheriff badge mechanics.",
    "tip.smart_vote": "Smart voting: no self-vote; Seer votes by cached checks.",
    "tip.policy": "exhaustive = full-tree search; online = online reference decision.",
    "tip.lambda_risk": "Risk parameter λ ∈ [0,1]: 1 = adversarial (union/intersection), 0 = mean.",
    "tip.toggle": "optimistic = widen (union); conservative = narrow (intersection).",
    "tip.lookahead_depth": "Lookahead depth in decision points; negative = full depth.",
    "tip.tactics": "Night tactics, comma-separated: self_kill,no_kill.",
    "tip.online_trace_path": "Output path for the online reference trace.",
    "tip.compare_with_exact": "Compare against full-depth exact value (online only).",
    "tip.start_state_json": "Custom starting state as a JSON string.",
    "tip.start_state_path": "Custom starting state JSON file path.",
    "tip.lang": "GUI language.",
    "tip.disable_plot": "Skip the state-tree image, keep stats and optional text tree.",
    "tip.max_processed_states": "Max state nodes to process; leave empty for unlimited.",
    "tip.max_runtime_seconds": "Max run seconds; leave empty for unlimited.",

    # 下拉显示值
    "opt.search_mode.dfs": "DFS",
    "opt.search_mode.bfs": "BFS",
    "opt.policy.exhaustive": "Exhaustive",
    "opt.policy.online": "Online",
    "opt.toggle.conservative": "Conservative",
    "opt.toggle.optimistic": "Optimistic",
    "opt.phase.night": "Night",
    "opt.phase.day": "Day",

    # 战术标签
    "tactic.self_kill": "Self Kill",
    "tactic.no_kill": "No Kill",

    # 绘图英文（供无 CJK 字体回退）
    "plot.title": "Search Simulator State Tree",
    "plot.intermediate": "Intermediate",
    "plot.village_win": "Village Win",
    "plot.wolf_win": "Wolf Win",
    "plot.axis_branch": "Branch Order",
    "plot.axis_depth": "Depth (TD)",
    "plot.root_action": "Root",
    "plot.none": "None",
    "plot.unfinished": "Unfinished",
    "plot.ref_title": "Online Reference Path",
    "plot.ref_ylabel": "reward interval",
    "plot.action_label": "Action",
    "plot.alive_status": "Alive",
    "plot.result_label": "Result",

    # 摘要展示
    "summary.unlimited": "unlimited",
    "summary.yes": "yes",
    "summary.no": "no",
}

_current_lang = "zh-CN"


def set_language(lang: str) -> None:
    """切换当前语言；未知语言回退中文。"""
    global _current_lang
    _current_lang = lang if lang in {"zh-CN", "en-US"} else "zh-CN"


def _table() -> dict[str, str]:
    if _current_lang == "en-US":
        return {**STRINGS, **EN_STRINGS}
    return STRINGS


def t(key: str, *args, **kwargs) -> str:
    """按当前语言取文本并做格式化（等价 f-string）；缺 key 回退 key 本身。"""
    text = _table().get(key, key)
    if args or kwargs:
        text = text.format(*args, **kwargs)
    return text


def t_en(key: str, *args, **kwargs) -> str:
    """强制英文（供无 CJK 字体的绘图回退）。"""
    text = EN_STRINGS.get(key, STRINGS.get(key, key))
    if args or kwargs:
        text = text.format(*args, **kwargs)
    return text


# ---- 配置摘要展示键（原 _config.py，移至此处避免 _config -> _i18n 循环导入） ----
CONFIG_SUMMARY_KEYS: tuple[str, ...] = (
    "number_of_players",
    "number_of_wolves",
    "search_mode",
    "include_seer",
    "include_witch",
    "include_guard",
    "include_hunter",
    "include_white_werewolf_king",
    "include_sheriff",
    "smart_vote",
    "max_processed_states",
    "max_queue_size",
    "max_runtime_seconds",
    "max_night_branches_per_state",
    "max_day_branches_per_state",
    "gc_interval",
    "parallel_workers",
    "disable_plot",
    "max_nodes_for_plot",
    "plot_dpi",
    "export_text_tree",
    "text_tree_output_path",
    "max_text_tree_nodes",
)


def format_config_summary(args) -> list[str]:
    """把当前参数按标签格式化为摘要行。"""
    lines: list[str] = []
    for key in CONFIG_SUMMARY_KEYS:
        value = getattr(args, key)
        if value is None:
            display = t("summary.unlimited")
        elif isinstance(value, bool):
            display = t("summary.yes") if value else t("summary.no")
        else:
            display = str(value)
        lines.append(f"{t('label.' + key)}: {display}")
    return lines
