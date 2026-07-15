import argparse
from collections import deque
import logging
import os
import sys
import threading
import time

"""基于 BFS/DFS 的狼人杀全树搜索模拟入口（支持 GUI 参数配置）。"""
logging.basicConfig(
    format=r"[%(asctime)s.%(msecs)03d][%(pathname)s:%(lineno)d][%(levelname)s]"
    + os.linesep
    + r"%(message)s"
    + os.linesep,
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


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
    "max_processed_states": "最大处理状态数",
    "max_queue_size": "最大队列长度",
    "max_runtime_seconds": "最大运行时长(秒)",
    "max_night_branches_per_state": "夜晚分支上限",
    "max_day_branches_per_state": "白天分支上限",
    "gc_interval": "GC间隔",
    "parallel_workers": "并行线程数",
    "disable_plot": "禁用绘图",
    "max_nodes_for_plot": "绘图节点上限",
    "max_plot_width_inches": "最大绘图宽度(英寸)",
    "max_plot_height_inches": "最大绘图高度(英寸)",
    "plot_dpi": "绘图DPI",
}


def _import_simulator():
    try:
        from search_simulator._simulator import SearchSimulator
    except ImportError:
        from ._simulator import SearchSimulator
    return SearchSimulator


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BFS Simulator")
    parser.add_argument(
        "-p",
        "--number_of_players",
        type=int,
        default=5,
        help="玩家人数(5-16),默认5人",
    )
    parser.add_argument(
        "-w",
        "--number_of_wolves",
        type=int,
        default=1,
        help="狼人数量，默认1人",
    )
    parser.add_argument(
        "--include_seer", action="store_true", help="是否包含预言家（默认不包含）"
    )
    parser.add_argument(
        "--include_witch", action="store_true", help="是否包含女巫（默认不包含）"
    )
    parser.add_argument(
        "--include_guard", action="store_true", help="是否包含守卫（默认不包含）"
    )
    parser.add_argument(
        "--include_hunter", action="store_true", help="是否包含猎人（默认不包含）"
    )
    parser.add_argument(
        "--include_white_werewolf_king",
        action="store_true",
        help="是否包含白狼王（默认不包含）",
    )
    parser.add_argument(
        "--include_sheriff",
        action="store_true",
        help="是否启用警长归票机制（默认不启用）",
    )
    parser.add_argument(
        "--search_mode",
        type=str,
        choices=["dfs", "bfs"],
        default="dfs",
        help="搜索模式：dfs(默认) 或 bfs",
    )
    parser.add_argument(
        "--max_processed_states",
        type=int,
        default=None,
        help="最多处理的状态节点数（默认不限）",
    )
    parser.add_argument(
        "--max_queue_size",
        type=int,
        default=None,
        help="BFS 队列最大长度，超出后新状态会被裁剪（默认不限）",
    )
    parser.add_argument(
        "--max_runtime_seconds",
        type=float,
        default=None,
        help="最大运行时长（秒），到达后提前停止（默认不限）",
    )
    parser.add_argument(
        "--max_night_branches_per_state",
        type=int,
        default=None,
        help="单个状态夜晚阶段最多保留分支数（默认不限）",
    )
    parser.add_argument(
        "--max_day_branches_per_state",
        type=int,
        default=None,
        help="单个状态白天阶段最多保留分支数（默认不限）",
    )
    parser.add_argument(
        "--gc_interval",
        type=int,
        default=2000,
        help="每处理多少个状态主动触发一次 gc.collect（默认 2000）",
    )
    parser.add_argument(
        "--parallel_workers",
        type=int,
        default=1,
        help="并行线程数（默认 1，表示单线程）",
    )
    parser.add_argument(
        "--disable_plot",
        action="store_true",
        help="禁用状态树绘图（用于避免图形后端错误）",
    )
    parser.add_argument(
        "--max_nodes_for_plot",
        type=int,
        default=2500,
        help="绘图节点上限，超出则跳过绘图（默认 2500）",
    )
    parser.add_argument(
        "--max_plot_width_inches",
        type=float,
        default=60.0,
        help="输出图最大宽度（英寸，默认 60）",
    )
    parser.add_argument(
        "--max_plot_height_inches",
        type=float,
        default=40.0,
        help="输出图最大高度（英寸，默认 40）",
    )
    parser.add_argument(
        "--plot_dpi",
        type=int,
        default=140,
        help="输出图 DPI（默认 140）",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="打开可视化参数设置界面（默认无参数启动时自动打开）",
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="强制命令行模式（即使无参数也不打开 GUI）",
    )
    return parser


def _run_simulation(args: argparse.Namespace):
    SearchSimulator = _import_simulator()
    simulator = SearchSimulator(
        number_of_players=args.number_of_players,
        number_of_wolves=args.number_of_wolves,
        include_seer=args.include_seer,
        include_witch=args.include_witch,
        include_guard=args.include_guard,
        include_hunter=args.include_hunter,
        include_white_werewolf_king=args.include_white_werewolf_king,
        include_sheriff=args.include_sheriff,
        search_mode=args.search_mode,
        max_processed_states=args.max_processed_states,
        max_queue_size=args.max_queue_size,
        max_runtime_seconds=args.max_runtime_seconds,
        max_night_branches_per_state=args.max_night_branches_per_state,
        max_day_branches_per_state=args.max_day_branches_per_state,
        gc_interval=args.gc_interval,
        parallel_workers=args.parallel_workers,
        enable_plot=not args.disable_plot,
        max_nodes_for_plot=args.max_nodes_for_plot,
        max_plot_width_inches=args.max_plot_width_inches,
        max_plot_height_inches=args.max_plot_height_inches,
        plot_dpi=args.plot_dpi,
        iteration_callback=getattr(args, "iteration_callback", None),
    )
    simulator.run()
    return simulator


def _parse_optional_int(raw_value: str) -> int | None:
    text = raw_value.strip()
    if not text:
        return None
    return int(text)


def _parse_optional_float(raw_value: str) -> float | None:
    text = raw_value.strip()
    if not text:
        return None
    return float(text)


def _format_config_summary(args: argparse.Namespace) -> list[str]:
    ordered_keys = [
        "number_of_players",
        "number_of_wolves",
        "search_mode",
        "include_seer",
        "include_witch",
        "include_guard",
        "include_hunter",
        "include_white_werewolf_king",
        "include_sheriff",
        "max_processed_states",
        "max_queue_size",
        "max_runtime_seconds",
        "max_night_branches_per_state",
        "max_day_branches_per_state",
        "gc_interval",
        "parallel_workers",
        "disable_plot",
        "max_nodes_for_plot",
        "max_plot_width_inches",
        "max_plot_height_inches",
        "plot_dpi",
    ]
    lines: list[str] = []
    for key in ordered_keys:
        value = getattr(args, key)
        if value is None:
            display = "不限"
        elif isinstance(value, bool):
            display = "是" if value else "否"
        else:
            display = str(value)
        lines.append(f"{UI_LABELS.get(key, key)}: {display}")
    return lines


def _launch_gui(parser: argparse.ArgumentParser) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError as exc:
        raise RuntimeError("当前环境缺少 tkinter，无法启动 GUI。") from exc

    root = tk.Tk()
    root.title("Search Simulator 参数配置")
    root.geometry("1100x680")
    root.minsize(920, 560)

    container = ttk.Frame(root, padding=12)
    container.pack(fill=tk.BOTH, expand=True)

    content_frame = ttk.Frame(container)
    content_frame.pack(fill=tk.BOTH, expand=True)
    content_frame.columnconfigure(0, weight=1)
    content_frame.columnconfigure(1, weight=1)
    content_frame.rowconfigure(0, weight=1)

    left_panel = ttk.Frame(content_frame)
    left_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

    right_panel = ttk.Frame(content_frame)
    right_panel.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
    right_panel.rowconfigure(1, weight=1)

    fields_frame = ttk.LabelFrame(left_panel, text="基础参数", padding=10)
    fields_frame.pack(fill=tk.X)

    bools_frame = ttk.LabelFrame(left_panel, text="角色与规则", padding=10)
    bools_frame.pack(fill=tk.X, pady=(10, 0))

    limits_frame = ttk.LabelFrame(left_panel, text="性能与绘图", padding=10)
    limits_frame.pack(fill=tk.X, pady=(10, 0))

    controls_frame = ttk.Frame(right_panel)
    controls_frame.pack(fill=tk.X, pady=(10, 0))

    status_frame = ttk.LabelFrame(right_panel, text="运行状态", padding=10)
    status_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

    defaults = {
        action.dest: parser.get_default(action.dest) for action in parser._actions
    }

    def add_labeled_entry(
        frame: ttk.LabelFrame,
        row: int,
        col_block: int,
        label: str,
        default_value: str,
    ):
        label_col = col_block * 2
        entry_col = label_col + 1
        left_pad = 0 if col_block == 0 else 18
        ttk.Label(frame, text=label).grid(
            row=row,
            column=label_col,
            sticky="w",
            padx=(left_pad, 8),
            pady=4,
        )
        var = tk.StringVar(value=default_value)
        entry = ttk.Entry(frame, textvariable=var, width=16)
        entry.grid(row=row, column=entry_col, sticky="w", pady=4)
        return var

    number_of_players_var = add_labeled_entry(
        fields_frame,
        0,
        0,
        UI_LABELS["number_of_players"],
        str(defaults["number_of_players"]),
    )
    number_of_wolves_var = add_labeled_entry(
        fields_frame,
        1,
        0,
        UI_LABELS["number_of_wolves"],
        str(defaults["number_of_wolves"]),
    )

    ttk.Label(fields_frame, text=UI_LABELS["search_mode"]).grid(
        row=2, column=0, sticky="w", padx=(0, 8), pady=4
    )
    search_mode_var = tk.StringVar(value=defaults["search_mode"])
    search_mode_box = ttk.Combobox(
        fields_frame,
        textvariable=search_mode_var,
        values=["dfs", "bfs"],
        state="readonly",
        width=19,
    )
    search_mode_box.grid(row=2, column=1, sticky="w", pady=4)

    include_seer_var = tk.BooleanVar(value=defaults["include_seer"])
    include_witch_var = tk.BooleanVar(value=defaults["include_witch"])
    include_guard_var = tk.BooleanVar(value=defaults["include_guard"])
    include_hunter_var = tk.BooleanVar(value=defaults["include_hunter"])
    include_white_werewolf_king_var = tk.BooleanVar(
        value=defaults["include_white_werewolf_king"]
    )
    include_sheriff_var = tk.BooleanVar(value=defaults["include_sheriff"])

    checkboxes = [
        (UI_LABELS["include_seer"], include_seer_var),
        (UI_LABELS["include_witch"], include_witch_var),
        (UI_LABELS["include_guard"], include_guard_var),
        (UI_LABELS["include_hunter"], include_hunter_var),
        (UI_LABELS["include_white_werewolf_king"], include_white_werewolf_king_var),
        (UI_LABELS["include_sheriff"], include_sheriff_var),
    ]
    for index, (title, var) in enumerate(checkboxes):
        ttk.Checkbutton(bools_frame, text=title, variable=var).grid(
            row=index // 3,
            column=index % 3,
            sticky="w",
            padx=(0, 16),
            pady=4,
        )

    max_processed_states_var = add_labeled_entry(
        limits_frame,
        0,
        0,
        UI_LABELS["max_processed_states"],
        (
            ""
            if defaults["max_processed_states"] is None
            else str(defaults["max_processed_states"])
        ),
    )
    max_queue_size_var = add_labeled_entry(
        limits_frame,
        1,
        0,
        UI_LABELS["max_queue_size"],
        "" if defaults["max_queue_size"] is None else str(defaults["max_queue_size"]),
    )
    max_runtime_seconds_var = add_labeled_entry(
        limits_frame,
        2,
        0,
        UI_LABELS["max_runtime_seconds"],
        (
            ""
            if defaults["max_runtime_seconds"] is None
            else str(defaults["max_runtime_seconds"])
        ),
    )
    max_night_branches_var = add_labeled_entry(
        limits_frame,
        3,
        0,
        UI_LABELS["max_night_branches_per_state"],
        (
            ""
            if defaults["max_night_branches_per_state"] is None
            else str(defaults["max_night_branches_per_state"])
        ),
    )
    max_day_branches_var = add_labeled_entry(
        limits_frame,
        4,
        0,
        UI_LABELS["max_day_branches_per_state"],
        (
            ""
            if defaults["max_day_branches_per_state"] is None
            else str(defaults["max_day_branches_per_state"])
        ),
    )
    gc_interval_var = add_labeled_entry(
        limits_frame,
        5,
        0,
        UI_LABELS["gc_interval"],
        str(defaults["gc_interval"]),
    )
    parallel_workers_var = add_labeled_entry(
        limits_frame,
        0,
        1,
        UI_LABELS["parallel_workers"],
        str(defaults["parallel_workers"]),
    )
    max_nodes_for_plot_var = add_labeled_entry(
        limits_frame,
        1,
        1,
        UI_LABELS["max_nodes_for_plot"],
        str(defaults["max_nodes_for_plot"]),
    )
    max_plot_width_var = add_labeled_entry(
        limits_frame,
        2,
        1,
        UI_LABELS["max_plot_width_inches"],
        str(defaults["max_plot_width_inches"]),
    )
    max_plot_height_var = add_labeled_entry(
        limits_frame,
        3,
        1,
        UI_LABELS["max_plot_height_inches"],
        str(defaults["max_plot_height_inches"]),
    )
    plot_dpi_var = add_labeled_entry(
        limits_frame,
        4,
        1,
        UI_LABELS["plot_dpi"],
        str(defaults["plot_dpi"]),
    )
    disable_plot_var = tk.BooleanVar(value=defaults["disable_plot"])
    ttk.Checkbutton(
        limits_frame,
        text=UI_LABELS["disable_plot"],
        variable=disable_plot_var,
    ).grid(row=5, column=2, columnspan=2, sticky="w", pady=(8, 4), padx=(18, 0))

    status_var = tk.StringVar(value="等待运行")
    status_label = ttk.Label(status_frame, textvariable=status_var)
    status_label.pack(anchor="w")
    run_started_at: float | None = None
    timer_job_id: str | None = None

    nodes_frame = ttk.LabelFrame(status_frame, text="最近10个迭代节点", padding=8)
    nodes_frame.pack(fill=tk.X, pady=(8, 0))
    nodes_listbox = tk.Listbox(nodes_frame, height=10)
    nodes_listbox.pack(fill=tk.X)
    nodes_hint_label = ttk.Label(
        nodes_frame,
        text="展示最近处理的节点（含未结束节点），实时刷新",
    )
    nodes_hint_label.pack(anchor="w", pady=(6, 0))

    recent_nodes: deque[dict] = deque(maxlen=10)
    pending_nodes: deque[dict] = deque()
    pending_nodes_lock = threading.Lock()

    summary_text = tk.Text(status_frame, height=10, wrap="word")
    summary_text.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
    summary_text.insert("1.0", "点击“开始模拟”后将在后台执行。\n")
    summary_text.config(state="disabled")

    run_button = ttk.Button(controls_frame, text="开始模拟")
    run_button.pack(side=tk.LEFT, fill=tk.X)
    ttk.Label(
        controls_frame,
        text=f"提示：留空表示不限；可以先设置较小 {UI_LABELS['max_processed_states']} 做试跑。",
    ).pack(side=tk.LEFT, padx=(12, 0))

    def append_summary(line: str) -> None:
        summary_text.config(state="normal")
        summary_text.insert(tk.END, line + "\n")
        summary_text.see(tk.END)
        summary_text.config(state="disabled")

    def _format_node_line(node: dict) -> str:
        status = "结束" if node.get("is_game_over") else "未结束"
        state_id = node.get("state_id", "?")
        parent_id = node.get("parent_state_id", "?")
        alive_count = node.get("alive_count", "?")
        total_players = node.get("total_players", "?")
        queue_length = node.get("queue_length", "?")
        processed_states = node.get("processed_states", "?")
        action_label = str(node.get("action_label", "未知"))
        return (
            f"#{state_id}({status}) p={parent_id} 存活{alive_count}/{total_players} "
            f"Q={queue_length} 已处理={processed_states} | {action_label}"
        )

    def _refresh_nodes_listbox() -> None:
        nodes_listbox.delete(0, tk.END)
        for node in reversed(recent_nodes):
            nodes_listbox.insert(tk.END, _format_node_line(node))

    def _drain_pending_nodes() -> None:
        has_update = False
        with pending_nodes_lock:
            while pending_nodes:
                recent_nodes.append(pending_nodes.popleft())
                has_update = True
        if has_update:
            _refresh_nodes_listbox()
        root.after(120, _drain_pending_nodes)

    def _iteration_callback(node_snapshot: dict) -> None:
        with pending_nodes_lock:
            pending_nodes.append(node_snapshot)

    _drain_pending_nodes()

    def _update_elapsed_status() -> None:
        nonlocal timer_job_id
        if run_started_at is None:
            return
        elapsed_seconds = max(0.0, time.perf_counter() - run_started_at)
        status_var.set(f"运行中... 已运行 {elapsed_seconds:.1f}s")
        timer_job_id = root.after(100, _update_elapsed_status)

    def _start_elapsed_timer() -> None:
        nonlocal run_started_at, timer_job_id
        run_started_at = time.perf_counter()
        if timer_job_id is not None:
            root.after_cancel(timer_job_id)
            timer_job_id = None
        _update_elapsed_status()

    def _stop_elapsed_timer() -> None:
        nonlocal run_started_at, timer_job_id
        if timer_job_id is not None:
            try:
                root.after_cancel(timer_job_id)
            except Exception:
                pass
            timer_job_id = None
        run_started_at = None

    def collect_args() -> argparse.Namespace:
        return argparse.Namespace(
            number_of_players=int(number_of_players_var.get().strip()),
            number_of_wolves=int(number_of_wolves_var.get().strip()),
            include_seer=bool(include_seer_var.get()),
            include_witch=bool(include_witch_var.get()),
            include_guard=bool(include_guard_var.get()),
            include_hunter=bool(include_hunter_var.get()),
            include_white_werewolf_king=bool(include_white_werewolf_king_var.get()),
            include_sheriff=bool(include_sheriff_var.get()),
            search_mode=search_mode_var.get().strip(),
            max_processed_states=_parse_optional_int(max_processed_states_var.get()),
            max_queue_size=_parse_optional_int(max_queue_size_var.get()),
            max_runtime_seconds=_parse_optional_float(max_runtime_seconds_var.get()),
            max_night_branches_per_state=_parse_optional_int(
                max_night_branches_var.get()
            ),
            max_day_branches_per_state=_parse_optional_int(max_day_branches_var.get()),
            gc_interval=int(gc_interval_var.get().strip()),
            parallel_workers=max(1, int(parallel_workers_var.get().strip())),
            disable_plot=bool(disable_plot_var.get()),
            max_nodes_for_plot=int(max_nodes_for_plot_var.get().strip()),
            max_plot_width_inches=float(max_plot_width_var.get().strip()),
            max_plot_height_inches=float(max_plot_height_var.get().strip()),
            plot_dpi=int(plot_dpi_var.get().strip()),
            iteration_callback=_iteration_callback,
            gui=True,
        )

    def on_finish(simulator, error: Exception | None) -> None:
        _stop_elapsed_timer()
        run_button.config(state="normal")
        run_button.config(text="开始模拟")
        if error is not None:
            status_var.set("运行失败")
            append_summary(f"错误: {error}")
            messagebox.showerror("运行失败", str(error))
            return

        status_var.set("运行完成")
        append_summary(f"处理状态数: {simulator.processed_states}")
        append_summary(f"终局数量: {len(simulator.endings)}")
        append_summary(f"停止原因: {simulator.stop_reason}")
        append_summary(f"胜负统计: {simulator.wins}")
        messagebox.showinfo(
            "运行完成",
            f"处理状态数: {simulator.processed_states}\n"
            f"终局数量: {len(simulator.endings)}\n"
            f"停止原因: {simulator.stop_reason}",
        )

    def start_run() -> None:
        try:
            args = collect_args()
        except Exception as exc:
            messagebox.showerror("参数错误", f"请检查参数格式：{exc}")
            return

        recent_nodes.clear()
        with pending_nodes_lock:
            pending_nodes.clear()
        _refresh_nodes_listbox()

        run_button.config(state="disabled")
        run_button.config(text="正在模拟")
        _start_elapsed_timer()
        append_summary("开始模拟...")
        append_summary("当前参数配置:")
        for line in _format_config_summary(args):
            append_summary(f"- {line}")

        def worker() -> None:
            simulator = None
            run_error: Exception | None = None
            try:
                simulator = _run_simulation(args)
            except Exception as exc:
                run_error = exc
            root.after(0, lambda: on_finish(simulator, run_error))

        threading.Thread(target=worker, daemon=True).start()

    run_button.config(command=start_run)
    root.mainloop()


def main() -> None:
    parser = _build_parser()
    args: argparse.Namespace = parser.parse_args()
    # 双击 exe 通常无额外参数，此时默认进入 GUI。
    no_extra_args = len(sys.argv) <= 1
    if args.gui or (no_extra_args and not args.cli):
        _launch_gui(parser)
        return
    _run_simulation(args)


if __name__ == "__main__":
    main()
