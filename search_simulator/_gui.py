from __future__ import annotations

import argparse
import json
import queue
import threading
import time
from collections import deque
from collections.abc import Callable

from ._config import GUI_BASIC_ENTRY_KEYS
from ._config import GUI_GEOMETRY
from ._config import GUI_LIMIT_ENTRY_LAYOUT
from ._config import GUI_MIN_SIZE
from ._config import GUI_ROLE_TOGGLE_KEYS
from ._i18n import format_config_summary
from ._i18n import set_language
from ._i18n import t
from ._game_state import GameState
from ._player import Player

# 下拉框：显示值 -> 机器值（显示值统一经 t() 取文案）
_SEARCH_MODE_ZH = {t("opt.search_mode.dfs"): "dfs", t("opt.search_mode.bfs"): "bfs"}
_POLICY_ZH = {t("opt.policy.exhaustive"): "exhaustive", t("opt.policy.online"): "online"}
_TOGGLE_ZH = {t("opt.toggle.conservative"): "conservative", t("opt.toggle.optimistic"): "optimistic"}
_PHASE_ZH = {t("opt.phase.night"): "night", t("opt.phase.day"): "day"}
_SEARCH_MODE_INV = {v: k for k, v in _SEARCH_MODE_ZH.items()}
_POLICY_INV = {v: k for k, v in _POLICY_ZH.items()}
_TOGGLE_INV = {v: k for k, v in _TOGGLE_ZH.items()}
_PHASE_INV = {v: k for k, v in _PHASE_ZH.items()}


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
        raise RuntimeError(t("gui.tkinter_missing")) from exc

    root = tk.Tk()
    root.title(t("gui.title"))
    root.geometry(GUI_GEOMETRY)
    root.minsize(*GUI_MIN_SIZE)

    container = ttk.Frame(root, padding=8)
    container.pack(fill=tk.BOTH, expand=True)

    content_frame = ttk.Frame(container)
    content_frame.pack(fill=tk.BOTH, expand=True)
    content_frame.columnconfigure(0, weight=1)
    content_frame.columnconfigure(1, weight=1)
    content_frame.columnconfigure(2, weight=1)
    content_frame.rowconfigure(0, weight=1)

    # 三栏弹性布局：左=基础/角色，中=性能/在线/战术，右=自定义状态/控制/状态
    left_inner = ttk.Frame(content_frame)
    left_inner.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
    mid_col = ttk.Frame(content_frame)
    mid_col.grid(row=0, column=1, sticky="nsew", padx=(6, 6))
    right_panel = ttk.Frame(content_frame)
    right_panel.grid(row=0, column=2, sticky="nsew", padx=(6, 0))
    right_panel.rowconfigure(1, weight=1)

    fields_frame = ttk.LabelFrame(
        left_inner, text=t("gui.panel.fields"), padding=10
    )
    fields_frame.pack(fill=tk.X)

    bools_frame = ttk.LabelFrame(
        left_inner, text=t("gui.panel.bools"), padding=10
    )
    bools_frame.pack(fill=tk.X, pady=(10, 0))

    limits_frame = ttk.LabelFrame(
        mid_col, text=t("gui.panel.limits"), padding=6
    )
    limits_frame.pack(fill=tk.X, pady=(8, 0))

    controls_frame = ttk.Frame(right_panel)
    controls_frame.pack(fill=tk.X, pady=(10, 0))

    status_frame = ttk.LabelFrame(
        right_panel, text=t("gui.panel.status"), padding=10
    )
    status_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

    defaults = {
        action.dest: parser.get_default(action.dest) for action in parser._actions
    }

    # i18n：按 --lang 设置当前语言（语言切换已隐藏，默认中文）
    set_language(defaults.get("lang", "zh-CN"))

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
        text = t("tip." + key)
        if text != "tip." + key:
            ToolTip(widget, text)

    def add_labeled_entry(
        frame: ttk.LabelFrame,
        row: int,
        col_block: int,
        key: str,
        default_value: str,
    ):
        label_col = col_block * 2
        entry_col = label_col + 1
        left_pad = 0 if col_block == 0 else 18
        label_widget = ttk.Label(frame, text=t("label." + key))
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
            str(defaults[key]),
        )

    search_mode_label = ttk.Label(fields_frame, text=t("label.search_mode"))
    search_mode_label.grid(
        row=len(GUI_BASIC_ENTRY_KEYS), column=0, sticky="w", padx=(0, 8), pady=4
    )
    search_mode_var = tk.StringVar(value=_SEARCH_MODE_INV[defaults["search_mode"]])
    search_mode_box = ttk.Combobox(
        fields_frame,
        textvariable=search_mode_var,
        values=list(_SEARCH_MODE_ZH),
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
            text=t("label." + key),
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
            _optional_default_text(defaults[key]),
        )

    disable_plot_var = tk.BooleanVar(value=defaults["disable_plot"])
    disable_plot_checkbutton = ttk.Checkbutton(
        limits_frame,
        text=t("label.disable_plot"),
        variable=disable_plot_var,
    )
    disable_plot_checkbutton.grid(
        row=7, column=2, columnspan=2, sticky="w", pady=(8, 4), padx=(18, 0)
    )
    attach_tooltip(disable_plot_checkbutton, "disable_plot")

    # ---- 在线决策参数面板 ----
    online_frame = ttk.LabelFrame(mid_col, text=t("label.policy"), padding=6)
    online_frame.pack(fill=tk.X, pady=(8, 0))

    policy_var = tk.StringVar(value=_POLICY_INV[defaults.get("policy", "exhaustive")])
    ttk.Label(online_frame, text=t("label.policy")).grid(
        row=0, column=0, sticky="w", padx=(0, 8), pady=4
    )
    policy_box = ttk.Combobox(
        online_frame,
        textvariable=policy_var,
        values=list(_POLICY_ZH),
        state="readonly",
        width=19,
    )
    policy_box.grid(row=0, column=1, sticky="w", pady=4)
    attach_tooltip(policy_box, "policy")

    toggle_var = tk.StringVar(value=_TOGGLE_INV[defaults.get("toggle", "conservative")])
    ttk.Label(online_frame, text=t("label.toggle")).grid(
        row=1, column=0, sticky="w", padx=(0, 8), pady=4
    )
    toggle_box = ttk.Combobox(
        online_frame,
        textvariable=toggle_var,
        values=list(_TOGGLE_ZH),
        state="readonly",
        width=19,
    )
    toggle_box.grid(row=1, column=1, sticky="w", pady=4)
    attach_tooltip(toggle_box, "toggle")

    lookahead_var = tk.StringVar(value=str(defaults.get("lookahead_depth", 2)))
    ttk.Label(online_frame, text=t("label.lookahead_depth")).grid(
        row=2, column=0, sticky="w", padx=(0, 8), pady=4
    )
    lookahead_entry = ttk.Entry(online_frame, textvariable=lookahead_var, width=16)
    lookahead_entry.grid(row=2, column=1, sticky="w", pady=4)
    attach_tooltip(lookahead_entry, "lookahead_depth")

    lambda_var = tk.DoubleVar(value=float(defaults.get("lambda_risk", 1.0)))
    ttk.Label(online_frame, text=t("label.lambda_risk")).grid(
        row=3, column=0, sticky="w", padx=(0, 8), pady=4
    )
    lambda_slider = ttk.Scale(
        online_frame, from_=0.0, to=1.0, variable=lambda_var, length=180
    )
    lambda_slider.grid(row=3, column=1, sticky="w", pady=4)
    attach_tooltip(lambda_slider, "lambda_risk")

    lambda_value_label = ttk.Label(online_frame, text=f"{lambda_var.get():.2f}")
    lambda_value_label.grid(row=3, column=2, sticky="w", padx=(8, 0), pady=4)

    def _update_lambda_label(*_args) -> None:
        lambda_value_label.config(text=f"{lambda_var.get():.2f}")

    lambda_var.trace_add("write", _update_lambda_label)

    # ---- 智能投票 + 夜间战术（父级勾选 + 缩进子勾选，避免 Treeview 的 +- 与勾选冲突） ----
    vote_tactics_frame = ttk.LabelFrame(mid_col, text=t("gui.vote_tactics"), padding=6)
    vote_tactics_frame.pack(fill=tk.X, pady=(8, 0))
    vote_enabled = tk.BooleanVar(value=defaults.get("smart_vote", False))
    tactics_labels = {"self_kill": t("tactic.self_kill"), "no_kill": t("tactic.no_kill")}
    tactic_vars: dict[str, tk.BooleanVar] = {
        "self_kill": tk.BooleanVar(value=False),
        "no_kill": tk.BooleanVar(value=False),
    }

    tactics_sub = ttk.Frame(vote_tactics_frame)

    def _refresh_vote_tactics() -> None:
        if vote_enabled.get():
            tactics_sub.pack(fill=tk.X, pady=(4, 0))
        else:
            # 智能投票未启用：隐藏子项勾选框，且所有战术失效
            for var in tactic_vars.values():
                var.set(False)
            tactics_sub.pack_forget()

    vote_check = ttk.Checkbutton(
        vote_tactics_frame,
        text=t("label.smart_vote"),
        variable=vote_enabled,
        command=_refresh_vote_tactics,
    )
    vote_check.pack(anchor="w")
    attach_tooltip(vote_check, "smart_vote")

    for iid, label in tactics_labels.items():
        cb = ttk.Checkbutton(tactics_sub, text=label, variable=tactic_vars[iid])
        cb.pack(anchor="w", padx=(30, 0), pady=(3, 0))
        attach_tooltip(cb, "tactics")

    _refresh_vote_tactics()

    # ---- 可视化自定义起始状态编辑器 ----
    custom_frame = ttk.LabelFrame(right_panel, text=t("gui.custom_state"), padding=6)
    custom_frame.pack(fill=tk.X, pady=(0, 8), before=controls_frame)
    use_custom_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(
        custom_frame, text=t("gui.use_custom_state"), variable=use_custom_var
    ).pack(anchor="w")

    players_rows: list[dict] = [
        {"role": "村民", "is_alive": True, "skills": {}},
        {"role": "狼人", "is_alive": True, "skills": {"攻击": -1}},
    ]

    players_tree = ttk.Treeview(
        custom_frame, columns=("role", "alive", "skills"), show="headings", height=4
    )
    players_tree.heading("role", text=t("gui.col.role"))
    players_tree.heading("alive", text=t("gui.col.alive"))
    players_tree.heading("skills", text=t("gui.col.skills"))
    players_tree.column("role", width=90)
    players_tree.column("alive", width=50)
    players_tree.column("skills", width=150)
    players_tree.pack(fill=tk.X)

    def _refresh_players_tree() -> None:
        players_tree.delete(*players_tree.get_children())
        for i, p in enumerate(players_rows):
            skills_text = ", ".join(f"{k}:{v}" for k, v in p["skills"].items()) or "-"
            players_tree.insert(
                "",
                "end",
                iid=str(i),
                values=(p["role"], t("gui.alive") if p["is_alive"] else t("gui.dead"), skills_text),
            )

    _refresh_players_tree()

    def _add_player() -> None:
        players_rows.append({"role": "村民", "is_alive": True, "skills": {}})
        _refresh_players_tree()

    def _remove_player() -> None:
        selection = players_tree.selection()
        if not selection:
            return
        index = int(selection[0])
        if 0 <= index < len(players_rows):
            players_rows.pop(index)
        _refresh_players_tree()

    def _edit_player() -> None:
        selection = players_tree.selection()
        if not selection:
            return
        index = int(selection[0])
        player = players_rows[index]

        dialog = tk.Toplevel(root)
        dialog.title(t("gui.edit_player"))
        role_var = tk.StringVar(value=player["role"])
        alive_var = tk.BooleanVar(value=player["is_alive"])
        skills_var = tk.StringVar(
            value=", ".join(f"{k}:{v}" for k, v in player["skills"].items())
        )
        ttk.Label(dialog, text=t("gui.col.role")).grid(row=0, column=0, sticky="w", padx=8, pady=4)
        ttk.Combobox(
            dialog,
            textvariable=role_var,
            values=["村民", "狼人", "白狼王", "预言家", "女巫", "守卫", "猎人"],
            state="readonly",
        ).grid(row=0, column=1, padx=8, pady=4)
        ttk.Checkbutton(dialog, text=t("gui.alive"), variable=alive_var).grid(
            row=1, column=0, columnspan=2, sticky="w", padx=8, pady=4
        )
        ttk.Label(dialog, text=t("gui.skills_hint")).grid(
            row=2, column=0, sticky="w", padx=8, pady=4
        )
        ttk.Entry(dialog, textvariable=skills_var, width=30).grid(
            row=2, column=1, padx=8, pady=4
        )

        def _save() -> None:
            skills: dict[str, int] = {}
            for token in skills_var.get().split(","):
                token = token.strip()
                if not token:
                    continue
                if ":" in token:
                    name, _, count = token.partition(":")
                    skills[name.strip()] = int(count.strip())
            player["role"] = role_var.get()
            player["is_alive"] = alive_var.get()
            player["skills"] = skills
            _refresh_players_tree()
            dialog.destroy()

        ttk.Button(dialog, text=t("gui.save"), command=_save).grid(
            row=3, column=0, columnspan=2, pady=8
        )

    button_bar = ttk.Frame(custom_frame)
    button_bar.pack(fill=tk.X, pady=(6, 0))
    ttk.Button(button_bar, text=t("gui.add_player"), command=_add_player).pack(side=tk.LEFT)
    ttk.Button(button_bar, text=t("gui.remove_selected"), command=_remove_player).pack(
        side=tk.LEFT, padx=(6, 0)
    )
    ttk.Button(button_bar, text=t("gui.edit_selected"), command=_edit_player).pack(
        side=tk.LEFT, padx=(6, 0)
    )

    fields_sub = ttk.Frame(custom_frame)
    fields_sub.pack(fill=tk.X, pady=(6, 0))

    phase_var = tk.StringVar(value=_PHASE_INV["night"])
    night_var = tk.StringVar(value="0")
    day_var = tk.StringVar(value="0")
    guard_var = tk.StringVar(value="")
    seer_var = tk.StringVar(value="")
    ttk.Label(fields_sub, text=t("gui.phase_field")).grid(row=0, column=0, sticky="w", pady=4)
    ttk.Combobox(
        fields_sub,
        textvariable=phase_var,
        values=list(_PHASE_ZH),
        state="readonly",
        width=8,
    ).grid(row=0, column=1, sticky="w", pady=4)
    ttk.Label(fields_sub, text=t("gui.night_day")).grid(
        row=1, column=0, sticky="w", pady=4
    )
    ttk.Entry(fields_sub, textvariable=night_var, width=6).grid(
        row=1, column=1, sticky="w", pady=4
    )
    ttk.Entry(fields_sub, textvariable=day_var, width=6).grid(
        row=1, column=2, sticky="w", pady=4
    )
    ttk.Label(fields_sub, text=t("gui.guard_last_target")).grid(
        row=2, column=0, sticky="w", pady=4
    )
    ttk.Entry(fields_sub, textvariable=guard_var, width=10).grid(
        row=2, column=1, sticky="w", pady=4
    )
    ttk.Label(fields_sub, text=t("gui.seer_checks")).grid(
        row=3, column=0, sticky="w", pady=4
    )
    ttk.Entry(fields_sub, textvariable=seer_var, width=24).grid(
        row=3, column=1, columnspan=2, sticky="w", pady=4
    )

    def _build_custom_state() -> GameState | None:
        if not use_custom_var.get():
            return None
        seer_results: dict[int, bool] = {}
        for token in seer_var.get().split(","):
            token = token.strip()
            if not token:
                continue
            if ":" in token:
                index, _, is_wolf = token.partition(":")
                seer_results[int(index.strip())] = is_wolf.strip() in {"1", "true", "狼", "是"}
        return GameState(
            players=[
                Player(role=p["role"], is_alive=p["is_alive"], skills=dict(p["skills"]))
                for p in players_rows
            ],
            phase=_PHASE_ZH[phase_var.get()],
            night_count=int(night_var.get()),
            day_count=int(day_var.get()),
            last_guard_target_index=(
                int(guard_var.get()) if guard_var.get().strip() else None
            ),
            seer_check_results=seer_results or None,
        )

    status_var = tk.StringVar(value=t("gui.waiting_status"))
    status_label = ttk.Label(status_frame, textvariable=status_var)
    status_label.pack(anchor="w")

    _PHASE_DISPLAY = {
        "idle": t("gui.phase.idle"),
        "search": t("gui.phase.search"),
        "report": t("gui.phase.report"),
        "plot": t("gui.phase.plot"),
        "text_tree": t("gui.phase.text_tree"),
        "done": t("gui.phase.done"),
    }
    phase_var = tk.StringVar(value=_PHASE_DISPLAY["idle"])
    phase_label = ttk.Label(status_frame, textvariable=phase_var, foreground="#888888")
    phase_label.pack(anchor="w", pady=(2, 0))

    main_queue: "queue.Queue" = queue.Queue()

    def _set_phase(phase: str) -> None:
        phase_var.set(_PHASE_DISPLAY.get(phase, phase))

    def _report_phase(phase: str) -> None:
        main_queue.put(("phase", phase))

    def _poll_main_queue() -> None:
        try:
            while True:
                msg = main_queue.get_nowait()
                if msg[0] == "phase":
                    _set_phase(msg[1])
                elif msg[0] == "finish":
                    on_finish(msg[1], msg[2])
        except queue.Empty:
            pass
        root.after(100, _poll_main_queue)

    _poll_main_queue()

    run_started_at: float | None = None
    timer_job_id: str | None = None

    nodes_frame = ttk.LabelFrame(
        status_frame, text=t("gui.panel.nodes"), padding=8
    )
    nodes_frame.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

    tree_row = ttk.Frame(nodes_frame)
    tree_row.pack(fill=tk.BOTH, expand=True)
    nodes_canvas = tk.Canvas(tree_row, highlightthickness=0, height=160)
    nodes_scroll = ttk.Scrollbar(
        tree_row, orient="vertical", command=nodes_canvas.yview
    )
    nodes_inner = ttk.Frame(nodes_canvas)
    nodes_inner_id = nodes_canvas.create_window(
        (0, 0), window=nodes_inner, anchor="nw"
    )
    nodes_canvas.configure(yscrollcommand=nodes_scroll.set)
    nodes_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    nodes_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    def _on_tree_mousewheel(event) -> None:
        nodes_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_inner_configure(_event=None) -> None:
        nodes_canvas.configure(scrollregion=nodes_canvas.bbox("all"))

    def _on_canvas_configure(event) -> None:
        nodes_canvas.itemconfigure(nodes_inner_id, width=event.width)

    nodes_inner.bind("<Configure>", _on_inner_configure)
    nodes_canvas.bind("<Configure>", _on_canvas_configure)

    def _bind_wheel(widget) -> None:
        widget.bind("<MouseWheel>", _on_tree_mousewheel, add="+")
        widget.bind(
            "<Button-4>", lambda _e: nodes_canvas.yview_scroll(-1, "units"), add="+"
        )
        widget.bind(
            "<Button-5>", lambda _e: nodes_canvas.yview_scroll(1, "units"), add="+"
        )

    _bind_wheel(nodes_canvas)
    _bind_wheel(nodes_inner)

    nodes_hint_label = ttk.Label(nodes_frame, text=t("gui.nodes_hint"))
    nodes_hint_label.pack(anchor="w", pady=(6, 0))

    all_nodes: dict[int, dict] = {}
    nodes_lock = threading.Lock()
    rendered_node_count = 0
    nodes_open: dict[int, bool] = {}
    _MAX_RENDERED_NODES = 1200

    summary_text = tk.Text(status_frame, height=6, wrap="word")
    summary_text.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
    summary_text.insert("1.0", t("gui.initial_summary"))
    summary_text.config(state="disabled")

    run_button = ttk.Button(controls_frame, text=t("gui.run_button"))
    run_button.pack(side=tk.LEFT, fill=tk.X)
    ttk.Label(
        controls_frame,
        text=t("gui.limits_hint", limit_label=t("label.max_processed_states")),
    ).pack(side=tk.LEFT, padx=(12, 0))

    def append_summary(line: str) -> None:
        summary_text.config(state="normal")
        summary_text.insert(tk.END, line + "\n")
        summary_text.see(tk.END)
        summary_text.config(state="disabled")

    def _format_node_line(node: dict) -> str:
        status = (
            t("node.finished")
            if node.get("is_game_over")
            else t("node.ongoing")
        )
        return t(
            "node.line",
            state_id=node.get("state_id", t("node.unknown_value")),
            status=status,
            parent_id=node.get("parent_state_id", t("node.unknown_value")),
            alive_count=node.get("alive_count", t("node.unknown_value")),
            total_players=node.get("total_players", t("node.unknown_value")),
            queue_length=node.get("queue_length", t("node.unknown_value")),
            processed_states=node.get("processed_states", t("node.unknown_value")),
            action_label=str(node.get("action_label", t("node.unknown_action"))),
        )

    def _toggle_node(sid: int, depth: int) -> None:
        # 展开状态的默认值须与 _render_row 一致（根节点默认展开）
        nodes_open[sid] = not nodes_open.get(sid, depth == 0)
        _refresh_nodes_tree()

    def _render_row(
        parent: ttk.Frame,
        sid: int,
        depth: int,
        children_map: dict,
        nodes: dict,
    ) -> None:
        is_open = nodes_open.get(sid, depth == 0)
        has_children = bool(children_map.get(sid))
        row = ttk.Frame(parent)
        row.pack(fill=tk.X)

        indent = ttk.Label(row, text="", width=depth * 3)
        indent.pack(side=tk.LEFT)
        _bind_wheel(indent)

        if has_children:
            toggle = tk.Label(
                row,
                text="▾" if is_open else "▸",
                width=2,
                cursor="hand2",
                anchor="center",
            )
            toggle.pack(side=tk.LEFT)
            _bind_wheel(toggle)
            toggle.bind("<Button-1>", lambda _e, s=sid, d=depth: _toggle_node(s, d))
        else:
            leaf = tk.Label(row, text="·", width=2, anchor="center")
            leaf.pack(side=tk.LEFT)
            _bind_wheel(leaf)

        text = tk.Label(row, text=_format_node_line(nodes[sid]), anchor="w")
        text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        _bind_wheel(text)

    def _refresh_nodes_tree() -> None:
        try:
            yview = nodes_canvas.yview()
        except Exception:
            yview = (0.0, 1.0)

        for child in nodes_inner.winfo_children():
            child.destroy()

        with nodes_lock:
            nodes = dict(all_nodes)
        children_map: dict[int | None, list[int]] = {}
        for sid, snap in nodes.items():
            pid = snap.get("parent_state_id")
            children_map.setdefault(pid, []).append(sid)
        for pid in children_map:
            children_map[pid].sort()

        roots = [
            sid
            for sid in nodes
            if nodes[sid].get("parent_state_id") is None
            or nodes[sid].get("parent_state_id") not in nodes
        ]
        roots.sort()

        visible: list[tuple[int, int]] = []
        truncated = [False]

        def _walk(sid: int, depth: int) -> None:
            if len(visible) >= _MAX_RENDERED_NODES:
                truncated[0] = True
                return
            visible.append((sid, depth))
            kids = children_map.get(sid, [])
            if kids and nodes_open.get(sid, depth == 0):
                for child_sid in kids:
                    _walk(child_sid, depth + 1)

        for root_sid in roots:
            _walk(root_sid, 0)

        for sid, depth in visible:
            _render_row(nodes_inner, sid, depth, children_map, nodes)

        if truncated[0]:
            ttk.Label(
                nodes_inner,
                text=t("gui.too_many_nodes", _MAX_RENDERED_NODES),
            ).pack(anchor="w", pady=(4, 0))

        nodes_inner.update_idletasks()
        nodes_canvas.configure(scrollregion=nodes_canvas.bbox("all"))
        try:
            nodes_canvas.yview_moveto(yview[0])
        except Exception:
            pass

    def _iteration_callback(node_snapshot: dict) -> None:
        sid = node_snapshot.get("state_id")
        if sid is None:
            return
        with nodes_lock:
            all_nodes[int(sid)] = node_snapshot

    def _periodic_tree_refresh() -> None:
        nonlocal rendered_node_count
        with nodes_lock:
            count = len(all_nodes)
        if count != rendered_node_count:
            _refresh_nodes_tree()
            rendered_node_count = count
        root.after(2000, _periodic_tree_refresh)

    _periodic_tree_refresh()

    def _update_elapsed_status() -> None:
        nonlocal timer_job_id
        if run_started_at is None:
            return
        elapsed_seconds = max(0.0, time.perf_counter() - run_started_at)
        status_var.set(
            t("gui.running_status", elapsed_seconds=elapsed_seconds)
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
        custom_state = _build_custom_state()
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
            smart_vote=bool(vote_enabled.get()),
            export_text_tree=bool(bool_vars["export_text_tree"].get()),
            search_mode=_SEARCH_MODE_ZH[search_mode_var.get().strip()],
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
            policy=_POLICY_ZH[policy_var.get().strip()],
            lambda_risk=float(lambda_var.get()),
            toggle=_TOGGLE_ZH[toggle_var.get().strip()],
            lookahead_depth=_parse_optional_int(lookahead_var.get()),
            tactics=",".join(
                sorted(iid for iid, var in tactic_vars.items() if var.get())
            )
            or None,
            lang="zh-CN",
            start_state_json=(
                json.dumps(custom_state.to_dict(), ensure_ascii=False)
                if custom_state is not None
                else None
            ),
            iteration_callback=_iteration_callback,
            gui=True,
        )

    def on_finish(simulator, error: Exception | None) -> None:
        _stop_elapsed_timer()
        _set_phase("done")
        _refresh_nodes_tree()  # 迭代完成后刷新一次，渲染完整迭代树
        run_button.config(state="normal")
        run_button.config(text=t("gui.run_button"))
        if error is not None:
            status_var.set(t("gui.failure_status"))
            append_summary(t("gui.error_summary_template", error=error))
            messagebox.showerror(t("gui.run_error_title"), str(error))
            return

        status_var.set(t("gui.finished_status"))
        finish_summary = {
            "processed_states": simulator.processed_states,
            "ending_count": len(simulator.endings),
            "stop_reason": simulator.stop_reason,
            "wins": simulator.wins,
        }
        for template in (
            t("gui.finish_processed"),
            t("gui.finish_endings"),
            t("gui.finish_stop_reason"),
            t("gui.finish_wins"),
        ):
            append_summary(template.format(**finish_summary))
        messagebox.showinfo(
            t("gui.finish_title"),
            t("gui.finish_message", **finish_summary),
        )

    def start_run() -> None:
        try:
            args = collect_args()
        except Exception as exc:
            messagebox.showerror(
                t("gui.param_error_title"),
                t("gui.param_error_template", error=exc),
            )
            return

        nonlocal rendered_node_count
        with nodes_lock:
            all_nodes.clear()
        rendered_node_count = 0
        _refresh_nodes_tree()
        _set_phase("search")

        run_button.config(state="disabled")
        run_button.config(text=t("gui.running_button"))
        _start_elapsed_timer()
        append_summary(t("gui.start_summary"))
        append_summary(t("gui.config_summary_title"))
        for line in format_config_summary(args):
            append_summary(f"- {line}")

        def worker() -> None:
            simulator = None
            run_error: Exception | None = None
            try:
                simulator = run_simulation(args, phase_callback=_report_phase)
            except Exception as exc:
                run_error = exc
            main_queue.put(("finish", simulator, run_error))

        threading.Thread(target=worker, daemon=True).start()

    run_button.config(command=start_run)
    root.mainloop()
