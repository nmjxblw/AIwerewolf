from __future__ import annotations

import argparse
import threading
import time
from collections import deque
from collections.abc import Callable

from ._config import GUI_BASIC_ENTRY_KEYS
from ._config import GUI_CONFIG_SUMMARY_TITLE
from ._config import GUI_ERROR_SUMMARY_TEMPLATE
from ._config import GUI_FAILURE_STATUS
from ._config import GUI_FINISH_MESSAGE_TEMPLATE
from ._config import GUI_FINISH_SUMMARY_TEMPLATES
from ._config import GUI_FINISH_TITLE
from ._config import GUI_FINISHED_STATUS
from ._config import GUI_GEOMETRY
from ._config import GUI_INITIAL_SUMMARY
from ._config import GUI_LIMIT_ENTRY_LAYOUT
from ._config import GUI_LIMITS_HINT
from ._config import GUI_MIN_SIZE
from ._config import GUI_NODE_FINISHED_STATUS
from ._config import GUI_NODE_LINE_TEMPLATE
from ._config import GUI_NODE_ONGOING_STATUS
from ._config import GUI_NODE_UNKNOWN_ACTION
from ._config import GUI_NODE_UNKNOWN_VALUE
from ._config import GUI_NODES_HINT
from ._config import GUI_PANEL_TITLES
from ._config import GUI_PARAM_ERROR_TEMPLATE
from ._config import GUI_PARAM_ERROR_TITLE
from ._config import GUI_ROLE_TOGGLE_KEYS
from ._config import GUI_RUN_BUTTON_TEXT
from ._config import GUI_RUN_ERROR_TITLE
from ._config import GUI_RUNNING_BUTTON_TEXT
from ._config import GUI_RUNNING_STATUS_TEMPLATE
from ._config import GUI_START_SUMMARY
from ._config import GUI_TITLE
from ._config import GUI_TOOLTIPS
from ._config import GUI_WAITING_STATUS
from ._config import UI_LABELS
from ._config import format_config_summary


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


def _optional_default_text(default_value) -> str:
    return "" if default_value is None else str(default_value)


def launch_gui(
    parser: argparse.ArgumentParser,
    run_simulation: Callable[[argparse.Namespace], object],
) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox
        from tkinter import ttk
    except ImportError as exc:
        raise RuntimeError("当前环境缺少 tkinter，无法启动 GUI。") from exc

    root = tk.Tk()
    root.title(GUI_TITLE)
    root.geometry(GUI_GEOMETRY)
    root.minsize(*GUI_MIN_SIZE)

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

    fields_frame = ttk.LabelFrame(
        left_panel, text=GUI_PANEL_TITLES["fields"], padding=10
    )
    fields_frame.pack(fill=tk.X)

    bools_frame = ttk.LabelFrame(
        left_panel, text=GUI_PANEL_TITLES["bools"], padding=10
    )
    bools_frame.pack(fill=tk.X, pady=(10, 0))

    limits_frame = ttk.LabelFrame(
        left_panel, text=GUI_PANEL_TITLES["limits"], padding=10
    )
    limits_frame.pack(fill=tk.X, pady=(10, 0))

    controls_frame = ttk.Frame(right_panel)
    controls_frame.pack(fill=tk.X, pady=(10, 0))

    status_frame = ttk.LabelFrame(
        right_panel, text=GUI_PANEL_TITLES["status"], padding=10
    )
    status_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

    defaults = {
        action.dest: parser.get_default(action.dest) for action in parser._actions
    }

    class ToolTip:
        def __init__(self, widget, text: str) -> None:
            self.widget = widget
            self.text = text
            self.window = None
            self.after_id = None
            widget.bind("<Enter>", self.schedule, add="+")
            widget.bind("<Leave>", self.hide, add="+")
            widget.bind("<ButtonPress>", self.hide, add="+")

        def schedule(self, _event=None) -> None:
            self.cancel()
            self.after_id = self.widget.after(450, self.show)

        def cancel(self) -> None:
            if self.after_id is None:
                return
            try:
                self.widget.after_cancel(self.after_id)
            except Exception:
                pass
            self.after_id = None

        def show(self) -> None:
            self.cancel()
            if self.window is not None:
                return
            x = self.widget.winfo_pointerx() + 14
            y = self.widget.winfo_pointery() + 12
            self.window = tk.Toplevel(self.widget)
            self.window.wm_overrideredirect(True)
            self.window.wm_geometry(f"+{x}+{y}")
            label = tk.Label(
                self.window,
                text=self.text,
                justify=tk.LEFT,
                wraplength=360,
                background="#FFFFE0",
                relief=tk.SOLID,
                borderwidth=1,
                padx=7,
                pady=5,
            )
            label.pack()

        def hide(self, _event=None) -> None:
            self.cancel()
            if self.window is None:
                return
            self.window.destroy()
            self.window = None

    def attach_tooltip(widget, key: str) -> None:
        text = GUI_TOOLTIPS.get(key)
        if text:
            ToolTip(widget, text)

    def add_labeled_entry(
        frame: ttk.LabelFrame,
        row: int,
        col_block: int,
        key: str,
        label: str,
        default_value: str,
    ):
        label_col = col_block * 2
        entry_col = label_col + 1
        left_pad = 0 if col_block == 0 else 18
        label_widget = ttk.Label(frame, text=label)
        label_widget.grid(
            row=row,
            column=label_col,
            sticky="w",
            padx=(left_pad, 8),
            pady=4,
        )
        var = tk.StringVar(value=default_value)
        entry = ttk.Entry(frame, textvariable=var, width=16)
        entry.grid(row=row, column=entry_col, sticky="w", pady=4)
        attach_tooltip(label_widget, key)
        attach_tooltip(entry, key)
        return var

    entry_vars: dict[str, tk.StringVar] = {}
    for row, key in enumerate(GUI_BASIC_ENTRY_KEYS):
        entry_vars[key] = add_labeled_entry(
            fields_frame,
            row,
            0,
            key,
            UI_LABELS[key],
            str(defaults[key]),
        )

    search_mode_label = ttk.Label(fields_frame, text=UI_LABELS["search_mode"])
    search_mode_label.grid(
        row=len(GUI_BASIC_ENTRY_KEYS), column=0, sticky="w", padx=(0, 8), pady=4
    )
    search_mode_var = tk.StringVar(value=defaults["search_mode"])
    search_mode_box = ttk.Combobox(
        fields_frame,
        textvariable=search_mode_var,
        values=["dfs", "bfs"],
        state="readonly",
        width=19,
    )
    search_mode_box.grid(row=len(GUI_BASIC_ENTRY_KEYS), column=1, sticky="w", pady=4)
    attach_tooltip(search_mode_label, "search_mode")
    attach_tooltip(search_mode_box, "search_mode")

    bool_vars: dict[str, tk.BooleanVar] = {}
    for key in GUI_ROLE_TOGGLE_KEYS:
        bool_vars[key] = tk.BooleanVar(value=defaults[key])

    for index, key in enumerate(GUI_ROLE_TOGGLE_KEYS):
        checkbutton = ttk.Checkbutton(
            bools_frame,
            text=UI_LABELS[key],
            variable=bool_vars[key],
        )
        checkbutton.grid(
            row=index // 3,
            column=index % 3,
            sticky="w",
            padx=(0, 16),
            pady=4,
        )
        attach_tooltip(checkbutton, key)

    for key, row, col_block in GUI_LIMIT_ENTRY_LAYOUT:
        entry_vars[key] = add_labeled_entry(
            limits_frame,
            row,
            col_block,
            key,
            UI_LABELS[key],
            _optional_default_text(defaults[key]),
        )

    disable_plot_var = tk.BooleanVar(value=defaults["disable_plot"])
    disable_plot_checkbutton = ttk.Checkbutton(
        limits_frame,
        text=UI_LABELS["disable_plot"],
        variable=disable_plot_var,
    )
    disable_plot_checkbutton.grid(
        row=7, column=2, columnspan=2, sticky="w", pady=(8, 4), padx=(18, 0)
    )
    attach_tooltip(disable_plot_checkbutton, "disable_plot")

    status_var = tk.StringVar(value=GUI_WAITING_STATUS)
    status_label = ttk.Label(status_frame, textvariable=status_var)
    status_label.pack(anchor="w")
    run_started_at: float | None = None
    timer_job_id: str | None = None

    nodes_frame = ttk.LabelFrame(
        status_frame, text=GUI_PANEL_TITLES["nodes"], padding=8
    )
    nodes_frame.pack(fill=tk.X, pady=(8, 0))
    nodes_listbox = tk.Listbox(nodes_frame, height=10)
    nodes_listbox.pack(fill=tk.X)
    nodes_hint_label = ttk.Label(nodes_frame, text=GUI_NODES_HINT)
    nodes_hint_label.pack(anchor="w", pady=(6, 0))

    recent_nodes: deque[dict] = deque(maxlen=10)
    pending_nodes: deque[dict] = deque()
    pending_nodes_lock = threading.Lock()

    summary_text = tk.Text(status_frame, height=10, wrap="word")
    summary_text.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
    summary_text.insert("1.0", GUI_INITIAL_SUMMARY)
    summary_text.config(state="disabled")

    run_button = ttk.Button(controls_frame, text=GUI_RUN_BUTTON_TEXT)
    run_button.pack(side=tk.LEFT, fill=tk.X)
    ttk.Label(
        controls_frame,
        text=GUI_LIMITS_HINT.format(limit_label=UI_LABELS["max_processed_states"]),
    ).pack(side=tk.LEFT, padx=(12, 0))

    def append_summary(line: str) -> None:
        summary_text.config(state="normal")
        summary_text.insert(tk.END, line + "\n")
        summary_text.see(tk.END)
        summary_text.config(state="disabled")

    def _format_node_line(node: dict) -> str:
        status = (
            GUI_NODE_FINISHED_STATUS
            if node.get("is_game_over")
            else GUI_NODE_ONGOING_STATUS
        )
        return GUI_NODE_LINE_TEMPLATE.format(
            state_id=node.get("state_id", GUI_NODE_UNKNOWN_VALUE),
            status=status,
            parent_id=node.get("parent_state_id", GUI_NODE_UNKNOWN_VALUE),
            alive_count=node.get("alive_count", GUI_NODE_UNKNOWN_VALUE),
            total_players=node.get("total_players", GUI_NODE_UNKNOWN_VALUE),
            queue_length=node.get("queue_length", GUI_NODE_UNKNOWN_VALUE),
            processed_states=node.get("processed_states", GUI_NODE_UNKNOWN_VALUE),
            action_label=str(node.get("action_label", GUI_NODE_UNKNOWN_ACTION)),
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
        status_var.set(
            GUI_RUNNING_STATUS_TEMPLATE.format(elapsed_seconds=elapsed_seconds)
        )
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
            number_of_players=int(entry_vars["number_of_players"].get().strip()),
            number_of_wolves=int(entry_vars["number_of_wolves"].get().strip()),
            include_seer=bool(bool_vars["include_seer"].get()),
            include_witch=bool(bool_vars["include_witch"].get()),
            include_guard=bool(bool_vars["include_guard"].get()),
            include_hunter=bool(bool_vars["include_hunter"].get()),
            include_white_werewolf_king=bool(
                bool_vars["include_white_werewolf_king"].get()
            ),
            include_sheriff=bool(bool_vars["include_sheriff"].get()),
            smart_vote=bool(bool_vars["smart_vote"].get()),
            export_text_tree=bool(bool_vars["export_text_tree"].get()),
            search_mode=search_mode_var.get().strip(),
            max_processed_states=_parse_optional_int(
                entry_vars["max_processed_states"].get()
            ),
            max_queue_size=_parse_optional_int(entry_vars["max_queue_size"].get()),
            max_runtime_seconds=_parse_optional_float(
                entry_vars["max_runtime_seconds"].get()
            ),
            max_night_branches_per_state=_parse_optional_int(
                entry_vars["max_night_branches_per_state"].get()
            ),
            max_day_branches_per_state=_parse_optional_int(
                entry_vars["max_day_branches_per_state"].get()
            ),
            gc_interval=int(entry_vars["gc_interval"].get().strip()),
            parallel_workers=max(1, int(entry_vars["parallel_workers"].get().strip())),
            disable_plot=bool(disable_plot_var.get()),
            max_nodes_for_plot=int(entry_vars["max_nodes_for_plot"].get().strip()),
            plot_dpi=int(entry_vars["plot_dpi"].get().strip()),
            text_tree_output_path=entry_vars["text_tree_output_path"].get().strip(),
            max_text_tree_nodes=int(entry_vars["max_text_tree_nodes"].get().strip()),
            iteration_callback=_iteration_callback,
            gui=True,
        )

    def on_finish(simulator, error: Exception | None) -> None:
        _stop_elapsed_timer()
        run_button.config(state="normal")
        run_button.config(text=GUI_RUN_BUTTON_TEXT)
        if error is not None:
            status_var.set(GUI_FAILURE_STATUS)
            append_summary(GUI_ERROR_SUMMARY_TEMPLATE.format(error=error))
            messagebox.showerror(GUI_RUN_ERROR_TITLE, str(error))
            return

        status_var.set(GUI_FINISHED_STATUS)
        finish_summary = {
            "processed_states": simulator.processed_states,
            "ending_count": len(simulator.endings),
            "stop_reason": simulator.stop_reason,
            "wins": simulator.wins,
        }
        for template in GUI_FINISH_SUMMARY_TEMPLATES:
            append_summary(template.format(**finish_summary))
        messagebox.showinfo(
            GUI_FINISH_TITLE,
            GUI_FINISH_MESSAGE_TEMPLATE.format(
                **finish_summary,
            ),
        )

    def start_run() -> None:
        try:
            args = collect_args()
        except Exception as exc:
            messagebox.showerror(
                GUI_PARAM_ERROR_TITLE,
                GUI_PARAM_ERROR_TEMPLATE.format(error=exc),
            )
            return

        recent_nodes.clear()
        with pending_nodes_lock:
            pending_nodes.clear()
        _refresh_nodes_listbox()

        run_button.config(state="disabled")
        run_button.config(text=GUI_RUNNING_BUTTON_TEXT)
        _start_elapsed_timer()
        append_summary(GUI_START_SUMMARY)
        append_summary(GUI_CONFIG_SUMMARY_TITLE)
        for line in format_config_summary(args):
            append_summary(f"- {line}")

        def worker() -> None:
            simulator = None
            run_error: Exception | None = None
            try:
                simulator = run_simulation(args)
            except Exception as exc:
                run_error = exc
            root.after(0, lambda: on_finish(simulator, run_error))

        threading.Thread(target=worker, daemon=True).start()

    run_button.config(command=start_run)
    root.mainloop()
