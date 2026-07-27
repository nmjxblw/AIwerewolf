from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ArgumentSpec:
    flags: tuple[str, ...]
    kwargs: dict[str, Any]


PARSER_DESCRIPTION = "BFS Simulator"

ARGUMENT_SPECS: tuple[ArgumentSpec, ...] = (
    ArgumentSpec(
        ("-p", "--number_of_players"),
        {"type": int, "default": 5, "help": "玩家人数(5-16),默认5人"},
    ),
    ArgumentSpec(
        ("-w", "--number_of_wolves"),
        {"type": int, "default": 1, "help": "狼人数量，默认1人"},
    ),
    ArgumentSpec(
        ("--include_seer",),
        {"action": "store_true", "help": "是否包含预言家（默认不包含）"},
    ),
    ArgumentSpec(
        ("--include_witch",),
        {"action": "store_true", "help": "是否包含女巫（默认不包含）"},
    ),
    ArgumentSpec(
        ("--include_guard",),
        {"action": "store_true", "help": "是否包含守卫（默认不包含）"},
    ),
    ArgumentSpec(
        ("--include_hunter",),
        {"action": "store_true", "help": "是否包含猎人（默认不包含）"},
    ),
    ArgumentSpec(
        ("--include_white_werewolf_king",),
        {"action": "store_true", "help": "是否包含白狼王（默认不包含）"},
    ),
    ArgumentSpec(
        ("--include_sheriff",),
        {"action": "store_true", "help": "是否启用警长归票机制（默认不启用）"},
    ),
    ArgumentSpec(
        ("--smart_vote",),
        {
            "action": "store_true",
            "help": "启用智能投票剪枝：白天不自投，预言家夜晚查验会影响后续投票",
        },
    ),
    ArgumentSpec(
        ("--search_mode",),
        {
            "type": str,
            "choices": ["dfs", "bfs"],
            "default": "dfs",
            "help": "搜索模式：dfs(默认) 或 bfs",
        },
    ),
    ArgumentSpec(
        ("--max_processed_states",),
        {"type": int, "default": None, "help": "最多处理的状态节点数（默认不限）"},
    ),
    ArgumentSpec(
        ("--max_queue_size",),
        {
            "type": int,
            "default": None,
            "help": "BFS 队列最大长度，超出后新状态会被裁剪（默认不限）",
        },
    ),
    ArgumentSpec(
        ("--max_runtime_seconds",),
        {
            "type": float,
            "default": None,
            "help": "最大运行时长（秒），到达后提前停止（默认不限）",
        },
    ),
    ArgumentSpec(
        ("--max_night_branches_per_state",),
        {
            "type": int,
            "default": None,
            "help": "单个状态夜晚阶段最多保留分支数（默认不限）",
        },
    ),
    ArgumentSpec(
        ("--max_day_branches_per_state",),
        {
            "type": int,
            "default": None,
            "help": "单个状态白天阶段最多保留分支数（默认不限）",
        },
    ),
    ArgumentSpec(
        ("--gc_interval",),
        {
            "type": int,
            "default": 2000,
            "help": "每处理多少个状态主动触发一次 gc.collect（默认 2000）",
        },
    ),
    ArgumentSpec(
        ("--parallel_workers",),
        {"type": int, "default": 1, "help": "并行线程数（默认 1，表示单线程）"},
    ),
    ArgumentSpec(
        ("--disable_plot",),
        {"action": "store_true", "help": "禁用状态树绘图（用于避免图形后端错误）"},
    ),
    ArgumentSpec(
        ("--max_nodes_for_plot",),
        {
            "type": int,
            "default": 2500,
            "help": "绘图节点上限，超出则跳过绘图（默认 2500）",
        },
    ),
    ArgumentSpec(
        ("--plot_dpi",),
        {"type": int, "default": 140, "help": "输出图 DPI（默认 140）"},
    ),
    ArgumentSpec(
        ("--export_text_tree",),
        {
            "action": "store_true",
            "help": "导出类似 tree 命令的文本状态树（默认不导出）",
        },
    ),
    ArgumentSpec(
        ("--text_tree_output_path",),
        {
            "type": str,
            "default": "search_tree.txt",
            "help": "文本状态树输出路径（默认 search_tree.txt）",
        },
    ),
    ArgumentSpec(
        ("--max_text_tree_nodes",),
        {
            "type": int,
            "default": 100000,
            "help": "文本状态树节点上限，超出则跳过导出（默认 100000）",
        },
    ),
    ArgumentSpec(
        ("--gui",),
        {
            "action": "store_true",
            "help": "打开可视化参数设置界面（默认无参数启动时自动打开）",
        },
    ),
    ArgumentSpec(
        ("--cli",),
        {"action": "store_true", "help": "强制命令行模式（即使无参数也不打开 GUI）"},
    ),
)

UI_LABELS: dict[str, str] = {
    "number_of_players": "玩家人数",
    "number_of_wolves": "狼人数量",
    "search_mode": "搜索模式",
    "include_seer": "预言家",
    "include_witch": "女巫",
    "include_guard": "守卫",
    "include_hunter": "猎人",
    "include_white_werewolf_king": "白狼王",
    "include_sheriff": "启用警长归票",
    "smart_vote": "智能投票剪枝",
    "max_processed_states": "最大处理状态数",
    "max_queue_size": "最大队列长度",
    "max_runtime_seconds": "最大运行时长(秒)",
    "max_night_branches_per_state": "夜晚分支上限",
    "max_day_branches_per_state": "白天分支上限",
    "gc_interval": "GC间隔",
    "parallel_workers": "并行线程数",
    "disable_plot": "禁用绘图",
    "max_nodes_for_plot": "绘图节点上限",
    "plot_dpi": "绘图DPI",
    "export_text_tree": "导出文本树",
    "text_tree_output_path": "文本树输出路径",
    "max_text_tree_nodes": "文本树节点上限",
}

GUI_TOOLTIPS: dict[str, str] = {
    "number_of_players": "本局玩家总人数，当前模拟器支持 5-16 人。",
    "number_of_wolves": "狼人阵营数量，必须小于玩家总人数。",
    "search_mode": "状态树遍历方式：DFS 更省队列内存，BFS 更适合按层观察分支。",
    "include_seer": "加入预言家角色；启用 smart_vote 时，夜晚查验会影响后续白天投票分支。",
    "include_witch": "加入女巫角色，夜晚会产生救药和毒药相关分支。",
    "include_guard": "加入守卫角色，夜晚会产生守护目标分支。",
    "include_hunter": "加入猎人角色，满足条件时会产生开枪分支。",
    "include_white_werewolf_king": "加入白狼王角色，满足条件时会产生自爆分支。",
    "include_sheriff": "启用警长机制，模拟警徽竞选、警长归票和警徽流转。",
    "smart_vote": "启用智能投票剪枝：白天不自投；预言家会按已缓存查验结果定向投票。",
    "max_processed_states": "最多处理多少个状态节点；留空表示不限制，建议试跑时先填较小值。",
    "max_queue_size": "BFS 队列最大长度；只对 BFS 有明显影响，留空表示不限制。",
    "max_runtime_seconds": "模拟最长运行秒数；到达后提前停止，留空表示不限制。",
    "max_night_branches_per_state": "单个状态的夜晚行动最多保留多少个分支，留空表示不剪枝。",
    "max_day_branches_per_state": "单个状态的白天行动最多保留多少个分支，留空表示不剪枝。",
    "gc_interval": "每处理多少个状态主动触发一次垃圾回收；数值越小回收越频繁。",
    "parallel_workers": "并行处理线程数；1 表示单线程，较大值可能提高速度但增加资源占用。",
    "disable_plot": "跳过 matplotlib 状态树图片输出，只保留统计和可选文本树。",
    "max_nodes_for_plot": "绘图节点数量上限；超过后跳过图片输出，避免图像过大或后端卡住。",
    "plot_dpi": "状态树图片输出 DPI；只控制清晰度，画布尺寸由叶节点数量和 label 宽度自动计算。",
    "export_text_tree": "导出类似 tree 命令的文本状态树，适合查看大分支或避免图片 label 遮挡。",
    "text_tree_output_path": "文本状态树输出路径，启用“导出文本树”后生效。",
    "max_text_tree_nodes": "文本状态树最多导出多少个节点；超过后跳过导出，避免生成过大文件。",
}

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

SIMULATOR_ARG_KEYS: tuple[str, ...] = (
    "number_of_players",
    "number_of_wolves",
    "include_seer",
    "include_witch",
    "include_guard",
    "include_hunter",
    "include_white_werewolf_king",
    "include_sheriff",
    "smart_vote",
    "search_mode",
    "max_processed_states",
    "max_queue_size",
    "max_runtime_seconds",
    "max_night_branches_per_state",
    "max_day_branches_per_state",
    "gc_interval",
    "parallel_workers",
)

ARTIFACT_ARG_KEYS: tuple[str, ...] = (
    "max_nodes_for_plot",
    "plot_dpi",
    "text_tree_output_path",
    "max_text_tree_nodes",
)

GUI_TITLE = "Search Simulator 参数配置"
GUI_GEOMETRY = "1100x680"
GUI_MIN_SIZE = (920, 560)
GUI_PANEL_TITLES = {
    "fields": "基础参数",
    "bools": "角色与规则",
    "limits": "性能与绘图",
    "status": "运行状态",
    "nodes": "最近10个迭代节点",
}
GUI_INITIAL_SUMMARY = "点击“开始模拟”后将在后台执行。\n"
GUI_RUN_BUTTON_TEXT = "开始模拟"
GUI_RUNNING_BUTTON_TEXT = "正在模拟"
GUI_WAITING_STATUS = "等待运行"
GUI_RUNNING_STATUS_TEMPLATE = "运行中... 已运行 {elapsed_seconds:.1f}s"
GUI_FAILURE_STATUS = "运行失败"
GUI_FINISHED_STATUS = "运行完成"
GUI_NODES_HINT = "展示最近处理的节点（含未结束节点），实时刷新"
GUI_LIMITS_HINT = "提示：留空表示不限；可以先设置较小 {limit_label} 做试跑。"
GUI_START_SUMMARY = "开始模拟..."
GUI_CONFIG_SUMMARY_TITLE = "当前参数配置:"
GUI_PARAM_ERROR_TITLE = "参数错误"
GUI_PARAM_ERROR_TEMPLATE = "请检查参数格式：{error}"
GUI_RUN_ERROR_TITLE = "运行失败"
GUI_FINISH_TITLE = "运行完成"
GUI_ERROR_SUMMARY_TEMPLATE = "错误: {error}"
GUI_FINISH_SUMMARY_TEMPLATES: tuple[str, ...] = (
    "处理状态数: {processed_states}",
    "终局数量: {ending_count}",
    "停止原因: {stop_reason}",
    "胜负统计: {wins}",
)
GUI_FINISH_MESSAGE_TEMPLATE = (
    "处理状态数: {processed_states}\n"
    "终局数量: {ending_count}\n"
    "停止原因: {stop_reason}"
)
GUI_NODE_FINISHED_STATUS = "结束"
GUI_NODE_ONGOING_STATUS = "未结束"
GUI_NODE_UNKNOWN_VALUE = "?"
GUI_NODE_UNKNOWN_ACTION = "未知"
GUI_NODE_LINE_TEMPLATE = (
    "#{state_id}({status}) p={parent_id} 存活{alive_count}/{total_players} "
    "Q={queue_length} 已处理={processed_states} | {action_label}"
)

GUI_BASIC_ENTRY_KEYS: tuple[str, ...] = ("number_of_players", "number_of_wolves")
GUI_ROLE_TOGGLE_KEYS: tuple[str, ...] = (
    "include_seer",
    "include_witch",
    "include_guard",
    "include_hunter",
    "include_white_werewolf_king",
    "include_sheriff",
    "smart_vote",
    "export_text_tree",
)
GUI_LIMIT_ENTRY_LAYOUT: tuple[tuple[str, int, int], ...] = (
    ("max_processed_states", 0, 0),
    ("max_queue_size", 1, 0),
    ("max_runtime_seconds", 2, 0),
    ("max_night_branches_per_state", 3, 0),
    ("max_day_branches_per_state", 4, 0),
    ("gc_interval", 5, 0),
    ("parallel_workers", 0, 1),
    ("max_nodes_for_plot", 1, 1),
    ("plot_dpi", 2, 1),
    ("text_tree_output_path", 3, 1),
    ("max_text_tree_nodes", 4, 1),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=PARSER_DESCRIPTION)
    for spec in ARGUMENT_SPECS:
        parser.add_argument(*spec.flags, **spec.kwargs)
    return parser


def format_config_summary(args: argparse.Namespace) -> list[str]:
    lines: list[str] = []
    for key in CONFIG_SUMMARY_KEYS:
        value = getattr(args, key)
        if value is None:
            display = "不限"
        elif isinstance(value, bool):
            display = "是" if value else "否"
        else:
            display = str(value)
        lines.append(f"{UI_LABELS.get(key, key)}: {display}")
    return lines
