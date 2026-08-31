"""Pygame 研究者视角的完整分支树与发言收益展示界面。"""

# ruff: noqa: I001  # pygame 的提示抑制环境变量必须在导入 pygame 前设置。

from __future__ import annotations

import argparse
import html
import logging
import math
import multiprocessing
import os
import queue
import threading
import time
import traceback
from pathlib import Path
from typing import Any
from typing import Callable

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import pygame
import pygame_gui
from pygame_gui.elements import UIButton
from pygame_gui.elements import UIHorizontalSlider
from pygame_gui.elements import UIProgressBar
from pygame_gui.elements import UITextEntryLine
from pygame_gui.windows import UIMessageWindow

from ._config import GUI_WINDOW_SIZE
from ._crash_handler import crash_log_path
from ._crash_handler import mark_crash_log_reported
from ._crash_handler import previous_unreported_crash_log
from ._game_state import game_state_dict_from_compact
from ._i18n import set_language
from ._i18n import t
from ._interval import RewardInterval
from ._interval import interval_branch_color
from ._interval import interval_camp
from ._runtime_logging import runtime_log_path
from ._tree_search import recompute_graph_intervals

logger = logging.getLogger(__name__)


BACKGROUND = pygame.Color("#0B1220")
PANEL = pygame.Color("#111C2E")
PANEL_ALT = pygame.Color("#17243A")
BORDER = pygame.Color("#2A3B55")
TEXT = pygame.Color("#E5EDF8")
MUTED = pygame.Color("#93A4BA")
ACCENT = pygame.Color("#2F80ED")
GOOD = pygame.Color("#3B82F6")
WOLF = pygame.Color("#EF4444")
BALANCED = pygame.Color("#111111")

ROLE_KEYS = (
    "include_seer",
    "include_witch",
    "include_guard",
    "include_hunter",
    "include_idiot",
    "include_white_werewolf_king",
    "all_positions",
)
DAY_TACTICS = ("seer_hide", "villager_decoy", "wolf_bloc")
NIGHT_TACTICS = ("wolf_self_kill", "wolf_no_kill")
LIVE_PREVIEW_NODE_LIMIT = 80
DAG_VISIBLE_NODE_LIMIT = 240
UI_DATA_REFRESH_SECONDS = 0.5
LIVE_NODE_FADE_SECONDS = 0.4
# 先关闭迭代树的实时/持久化绘制，保留后台搜索、检查点、结果表和统计卡片。
# 进度队列仍由 GUI 消费，避免 worker 因有界队列背压而停在 UI 渲染上。
ENABLE_ITERATION_TREE_RENDERING = False
# DAG 网格的列/行间距固定，避免深度或同层节点数量变化时发生重叠。
# 坐标单位是未缩放的画布像素；用户缩放时只改变可视投影，不改变深度编号。
GRAPH_GRID_COLUMN_SPACING = 124.0
GRAPH_GRID_ROW_SPACING = 34.0
GRAPH_GRID_DASH_LENGTH = 7
GRAPH_GRID_GAP_LENGTH = 5
GRAPH_AXIS_BOTTOM_MARGIN = 24
GRAPH_SAFE_COORDINATE_LIMIT = 8_192


def _yes_no(value: Any) -> str:
    return t("common.yes") if bool(value) else t("common.no")


def _seat_label(value: Any) -> str:
    if value is None:
        return t("common.none")
    return t("gui.state.seat", seat=int(value) + 1)


def _seat_list(values: Any) -> str:
    items = list(values or [])
    if not items:
        return t("common.none")
    return "、".join(_seat_label(value) for value in items)


def _phase_label(value: Any) -> str:
    phase = str(value)
    if phase in {"night", "day", "complete"}:
        return t(f"gui.state.phase.{phase}")
    return phase or t("common.unknown")


def _interval_label(value: Any) -> str:
    values = list(value or [-1.0, 1.0])
    if len(values) < 2:
        return t("gui.state.interval_unavailable")
    return f"[{float(values[0]):.4f}, {float(values[1]):.4f}]"


def _format_game_state_hover(
    node_id: int,
    node: dict[str, Any],
    state: dict[str, Any],
) -> list[str]:
    """把完整 GameState 转成研究者可读的分区 UI 文本。"""

    if state.get("unavailable"):
        return [
            t("gui.state.node_overview"),
            t(
                "gui.state.node_line",
                node=node_id,
                result=node.get("result", t("common.not_finished")),
            ),
            t(
                "gui.state.interval_line",
                wide=_interval_label(node.get("wide_interval")),
                narrow=_interval_label(node.get("narrow_interval")),
            ),
            t("gui.state.details"),
            str(state["unavailable"]),
        ]

    seer_checks = state.get("seer_check_results") or {}
    check_text = "；".join(
        f"{_seat_label(index)}＝{t('common.wolf') if bool(value) else t('common.good')}"
        for index, value in sorted(seer_checks.items(), key=lambda item: int(item[0]))
    ) or t("common.none")
    role_claims = state.get("public_role_claims") or {}
    claim_text = "；".join(
        f"{_seat_label(index)} {role}" for index, role in sorted(role_claims.items(), key=lambda item: int(item[0]))
    ) or t("common.none")
    day_votes = state.get("last_day_votes") or {}
    vote_text = "；".join(
        f"{_seat_label(voter)}→{_seat_label(target)}"
        for voter, target in sorted(day_votes.items(), key=lambda item: int(item[0]))
    ) or t("common.none")
    snapshot_values = []
    for raw_value in state.get("players_snapshot") or []:
        snapshot_values.append(
            str(raw_value).replace(":alive", f"·{t('common.alive')}").replace(":dead", f"·{t('common.dead')}")
        )
    snapshot_text = "；".join(snapshot_values) or t("common.none")
    lines = [
        t("gui.state.node_overview"),
        t(
            "gui.state.node_line",
            node=node_id,
            result=node.get("result", t("common.not_finished")),
        ),
        t(
            "gui.state.interval_line",
            wide=_interval_label(node.get("wide_interval")),
            narrow=_interval_label(node.get("narrow_interval")),
        ),
        t("gui.state.progress"),
        t(
            "gui.state.rounds",
            phase=_phase_label(state.get("phase")),
            day=int(state.get("day_count", 0)),
            night=int(state.get("night_count", 0)),
        ),
        t(
            "gui.state.terminal_depth",
            terminal=_yes_no(state.get("is_game_over")),
            depth=int(state.get("depth", 0)),
        ),
        t(
            "gui.state.parent_action",
            parent=_seat_or_node(state.get("parent_state_id")),
            action=state.get("action_label") or t("action.root"),
        ),
        t("gui.state.identity"),
        t(
            "gui.state.seer_guard",
            seer=_yes_no(state.get("seer_revealed")),
            guard=_seat_label(state.get("last_guard_target_index")),
        ),
        t("gui.state.seer_checks", value=check_text),
        t("gui.state.revealed_good", value=_seat_list(state.get("revealed_good_indices"))),
        t("gui.state.revealed_wolf", value=_seat_list(state.get("revealed_wolf_indices"))),
        t("gui.state.role_claims", value=claim_text),
        t("gui.state.idiot", value=_seat_list(state.get("idiot_revealed_indices"))),
        t("gui.state.wolf_targets", value=_seat_list(state.get("wolf_priority_targets"))),
        t("gui.state.vote_tactic"),
        t("gui.state.votes", value=vote_text),
        t("gui.state.tactic", value=state.get("last_day_strategy") or t("common.none")),
        t("gui.state.identifiers"),
        t("gui.state.position_signature", value=state.get("position_signature") or t("common.none")),
        t("gui.state.snapshot", state_id=int(state.get("state_id", -1)), value=snapshot_text),
        t("gui.state.players"),
    ]
    players = list(state.get("players") or [])
    for player_index, player in enumerate(players):
        skills = player.get("skills") or {}
        skill_text = "；".join(
            f"{name}：{_skill_count_label(count)}"
            for name, count in sorted(skills.items(), key=lambda item: str(item[0]))
        ) or t("common.none")
        lines.append(
            t(
                "gui.state.player",
                seat=player_index + 1,
                role=player.get("role", t("common.unknown")),
                state=t("common.alive") if bool(player.get("is_alive")) else t("common.dead"),
                skills=skill_text,
            )
        )
    if not players:
        lines.append(t("gui.state.no_players"))
    return lines


def _seat_or_node(value: Any) -> str:
    if value is None:
        return t("gui.state.root")
    return f"N{int(value)}"


def _skill_count_label(value: Any) -> str:
    count = int(value)
    if count < 0:
        return t("gui.state.skill.infinite", count=count)
    if count == 0:
        return t("gui.state.skill.empty")
    return t("gui.state.skill.remaining", count=count)


def _faded_color(
    color: pygame.Color,
    alpha: float,
    *,
    background: pygame.Color = PANEL,
) -> pygame.Color:
    ratio = max(0.0, min(1.0, alpha))
    return pygame.Color(
        round(background.r + (color.r - background.r) * ratio),
        round(background.g + (color.g - background.g) * ratio),
        round(background.b + (color.b - background.b) * ratio),
    )


def _system_font_path() -> str | None:
    for candidate in (
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc",
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "simhei.ttf",
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf",
    ):
        if candidate.exists():
            return str(candidate)
    return None


def _theme() -> dict[str, Any]:
    font_path = _system_font_path()
    font = {
        "name": "Microsoft YaHei",
        "size": 14,
        "script": "Hani",
        "direction": "ltr",
    }
    if font_path is not None:
        font["regular_path"] = font_path.replace("\\", "/")
    return {
        "defaults": {
            "colours": {
                "normal_bg": "#17243A",
                "hovered_bg": "#233755",
                "disabled_bg": "#111827",
                "selected_bg": "#2F80ED",
                "normal_text": "#E5EDF8",
                "hovered_text": "#FFFFFF",
                "disabled_text": "#64748B",
                "selected_text": "#FFFFFF",
                "normal_border": "#334A68",
                "hovered_border": "#4B6B91",
                "disabled_border": "#1F2937",
                "selected_border": "#60A5FA",
            },
            "font": font,
        }
    }


def _compact_integer(value: int) -> str:
    text = str(int(value))
    if len(text) <= 10:
        return f"{int(value):,}"
    return f"{text[:4]}.{text[4:7]}e{len(text) - 1}"


def _terminal_popup_content(
    status: str,
    result: dict[str, Any],
    *,
    error: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """构造三种互斥运行终态的中文弹窗标题与 HTML 正文。

    参数：
        status: ``complete``、``interrupted`` 或 ``failed``。
        result: 运行标识、检查点计数和下一站位等终态上下文。
        error: 失败时的异常类型、消息和 traceback 摘要。

    返回：
        弹窗标题与已转义的 HTML 正文。
    """

    run_id = str(result.get("run_id") or t("common.unknown"))
    completed = int(result.get("position_count", 0))
    total = int(result.get("total_position_count", completed or 1))
    next_position = result.get("next_position_index")
    next_text = t("common.none") if next_position is None else f"#{int(next_position)}"
    runtime_path = str(runtime_log_path())
    crash_path = str(crash_log_path())
    if status == "complete":
        title = t("gui.popup.run.complete.title")
        headline = t("gui.popup.run.complete.headline")
    elif status == "interrupted":
        title = t("gui.popup.run.interrupted.title")
        headline = t("gui.popup.run.interrupted.headline")
    else:
        error = error or {}
        error_type = str(error.get("error_type") or "UnknownError")
        error_message = str(error.get("error") or t("common.unavailable"))
        if error.get("iteration_status") == "complete":
            title = t("gui.popup.run.output_failed.title")
            headline = t("gui.popup.run.output_failed.headline", error_type=error_type, error=error_message)
        else:
            title = t("gui.popup.run.failed.title")
            headline = t("gui.popup.run.failed.headline", error_type=error_type, error=error_message)
    lines = (
        headline,
        t("gui.popup.field.run_id", value=run_id),
        t("gui.popup.field.checkpoints", completed=completed, total=total),
        t("gui.popup.field.next", value=next_text),
        t("gui.popup.field.runtime_log", path=runtime_path),
        t("gui.popup.field.crash_log", path=crash_path),
    )
    return title, "<br>".join(html.escape(line) for line in lines)


def _matrix_terminal_popup_content(
    status: str,
    payload: dict[str, Any],
) -> tuple[str, str]:
    """构造矩阵完成、中断和失败三种互斥终态弹窗。"""

    matrix_id = str(payload.get("matrix_id") or t("common.unknown"))
    committed = int(payload.get("committed_batches", 0))
    total = int(payload.get("total_batches", committed))
    runtime_path = str(payload.get("runtime_log") or runtime_log_path())
    crash_path = str(payload.get("crash_log") or crash_log_path())
    if status == "complete":
        title = t("gui.popup.matrix.complete.title")
        headline = (
            t("gui.popup.matrix.complete.cached")
            if payload.get("cache_hit")
            else t("gui.popup.matrix.complete.headline")
        )
    elif status == "interrupted":
        title = t("gui.popup.matrix.interrupted.title")
        headline = t("gui.popup.matrix.interrupted.headline")
    else:
        title = t("gui.popup.matrix.failed.title")
        headline = t(
            "gui.popup.matrix.failed.headline",
            error_type=payload.get("error_type", "UnknownError"),
            error=payload.get("error", t("common.unavailable")),
        )
    lines = (
        headline,
        t("gui.popup.field.matrix_id", value=matrix_id),
        t("gui.popup.field.units", completed=committed, total=total),
        t("gui.popup.field.runtime_log", path=runtime_path),
        t("gui.popup.field.crash_log", path=crash_path),
    )
    return title, "<br>".join(html.escape(line) for line in lines)


def _matrix_action_label(action: dict[str, Any]) -> str:
    """把结构化动作转换为紧凑中文行标题，不显示规范键或 JSON。"""

    family = str(action.get("family") or "unknown")
    target = action.get("target_id")
    claim_target = action.get("claim_target")
    claim_result = {"good": t("matrix.result.good"), "wolf": t("matrix.result.wolf")}.get(
        str(action.get("claim_result")),
        "",
    )
    if family == "baseline":
        return t("matrix.action.baseline")
    if family == "silence":
        return t("matrix.action.silence")
    if family == "accusation":
        return t("matrix.action.accusation", target=_seat_label(target))
    if family == "support":
        return t("matrix.action.support", target=_seat_label(target))
    if family == "vote_intent":
        return t("matrix.action.vote_intent", target=_seat_label(target))
    if family == "seer_claim":
        if claim_target is None:
            return t("matrix.action.seer_claim_weak")
        return t(
            "matrix.action.seer_claim",
            target=_seat_label(claim_target),
            result=claim_result,
        )
    return family


def _matrix_row_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """按一级族和席位稳定排序矩阵行，baseline 固定置顶。"""

    action = dict(row.get("action") or {})
    family_order = {
        "baseline": 0,
        "silence": 1,
        "accusation": 2,
        "support": 3,
        "vote_intent": 4,
        "seer_claim": 5,
    }
    return (
        family_order.get(str(action.get("family")), 99),
        -1 if action.get("target_id") is None else int(action["target_id"]),
        -1 if action.get("claim_target") is None else int(action["claim_target"]),
        str(action.get("claim_result") or ""),
        str(row.get("action_key") or ""),
    )


class PygameSimulatorUI:
    def __init__(
        self,
        parser: argparse.ArgumentParser,
        run_simulation: Callable[..., Any],
    ) -> None:
        set_language("zh-CN")
        pygame.init()
        pygame.display.set_caption(t("gui.title"))
        self.screen = pygame.display.set_mode(GUI_WINDOW_SIZE)
        self.clock = pygame.time.Clock()
        self.manager = pygame_gui.UIManager(
            GUI_WINDOW_SIZE,
            _theme(),
            starting_language="zh",
        )
        self.parser = parser
        self.run_simulation = run_simulation
        self.defaults = parser.parse_args([])
        # 两类计算固定复用同一 SQLite；发言收益页不提供路径修改入口。
        self.matrix_database_path = str(Path(self.defaults.signature_cache_db_path).expanduser().resolve())
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.control_manager = multiprocessing.Manager()
        self.resume_event = self.control_manager.Event()
        self.resume_event.set()
        self.worker_progress_queue = self.control_manager.Queue(maxsize=32)
        self.worker_result_queue = self.control_manager.Queue(maxsize=8)
        self.active_page = "tree"
        self.running = False
        # solution 命中后首次点击只载入结果；下一次点击才强制创建新批次。
        self.force_recompute_next_run = False
        self.paused = False
        self.keep_running = True
        self.rows: list[dict[str, Any]] = []
        self.page_index = 0
        self.selected_row: int | None = None
        self.graph: dict[str, list[dict[str, Any]]] = {"nodes": [], "edges": []}
        self.live_graphs: dict[int, dict[str, Any]] = {}
        self.preview_position = 0
        self.selected_node: int | None = None
        self.last_data_refresh_at = 0.0
        self.simulator: Any | None = None
        self.terminal_popup: UIMessageWindow | None = None
        self.pending_previous_crash_log: Path | None = None
        self.graph_zoom = 0.78
        self.graph_pan = [0.0, 0.0]
        self.dragging_graph = False
        self.drag_origin = (0, 0)
        self.pan_origin = (0.0, 0.0)
        self.status = t("gui.ready")
        self.progress = 0.0
        self.last_interval_lambda = round(float(self.defaults.lambda_risk), 2)
        self.interval_recompute_running = False
        self.interval_recompute_requested_lambda: float | None = None
        self.live_stats = {
            "terminal_count": 0,
            "good_paths": 0,
            "wolf_paths": 0,
            "expanded_nodes": 0,
            "discovered_nodes": 0,
            "frontier_size": 0,
            "edge_count": 0,
            "completed_positions": 0,
            "total_positions": 0,
        }
        self.position_progress: dict[int, dict[str, Any]] = {}
        self.active_position = 0
        self.day_expanded = True
        self.night_expanded = True
        # 矩阵协调器必须是非 daemon 的隔离进程，才能继续创建计算 worker。
        # Pygame 主进程只保存有界消息队列、停止事件和 JSON-safe 输出。
        self.matrix_context = multiprocessing.get_context("spawn")
        self.matrix_process: multiprocessing.Process | None = None
        self.matrix_output_queue: Any | None = None
        self.matrix_stop_event: Any | None = None
        self.matrix_running = False
        self.matrix_stop_requested = False
        self.matrix_terminal_seen = False
        self.matrix_exit_seen_at: float | None = None
        self.matrix_force_recompute = False
        self.matrix_rows: list[dict[str, Any]] = []
        self.matrix_result: dict[str, Any] | None = None
        self.matrix_page_index = 0
        self.matrix_selected_row: int | None = None
        self.matrix_table_row_rects: list[tuple[pygame.Rect, int]] = []
        self.matrix_force_rect = pygame.Rect(20, 258, 280, 24)
        self.matrix_id = ""
        self.matrix_cache_hit = False
        self.matrix_committed_batches = 0
        self.matrix_total_batches = 0
        self.matrix_status = t("gui.matrix.ready")

        self.font_path = _system_font_path()
        self.fonts = {
            12: pygame.font.Font(self.font_path, 12),
            14: pygame.font.Font(self.font_path, 14),
            16: pygame.font.Font(self.font_path, 16),
            20: pygame.font.Font(self.font_path, 20),
        }
        self.fonts[20].set_bold(True)

        self.values = {
            "include_seer": bool(self.defaults.include_seer),
            "include_witch": bool(self.defaults.include_witch),
            "include_guard": bool(self.defaults.include_guard),
            "include_hunter": bool(self.defaults.include_hunter),
            "include_idiot": bool(self.defaults.include_idiot),
            "include_white_werewolf_king": bool(self.defaults.include_white_werewolf_king),
            "all_positions": bool(self.defaults.all_positions),
            "smart_vote": bool(self.defaults.smart_vote),
        }
        default_tactics = set(str(self.defaults.tactics).split(","))
        self.tactics = {key: key in default_tactics for key in (*DAY_TACTICS, *NIGHT_TACTICS)}
        self.toggle_rects: dict[str, pygame.Rect] = {}
        self.tactic_rects: dict[str, pygame.Rect] = {}
        self.header_rects: dict[str, pygame.Rect] = {}
        self.table_row_rects: list[tuple[pygame.Rect, int]] = []
        self.node_screen_positions: dict[int, tuple[float, float]] = {}
        self.hovered_node: int | None = None
        self.hovered_edge: dict[str, Any] | None = None
        self.hover_tooltip: list[str] = []

        self._create_controls()
        self._switch_page("tree")
        self._show_previous_crash_popup()

    def _create_controls(self) -> None:
        self.tree_page_button = UIButton(
            pygame.Rect(18, 18, 136, 38),
            text=t("gui.page.tree_active"),
            manager=self.manager,
            object_id="#page_tree",
        )
        self.matrix_page_button = UIButton(
            pygame.Rect(166, 18, 136, 38),
            text=t("gui.page.matrix"),
            manager=self.manager,
            object_id="#page_matrix",
        )
        self.entries: dict[str, UITextEntryLine] = {}
        entry_specs = (
            ("number_of_players", 18, 102, self.defaults.number_of_players),
            ("number_of_wolves", 170, 102, self.defaults.number_of_wolves),
            ("page_size", 18, 158, min(10, int(self.defaults.page_size))),
        )
        for key, x, y, value in entry_specs:
            entry = UITextEntryLine(
                pygame.Rect(x, y, 135, 32),
                manager=self.manager,
                object_id=f"#{key}",
            )
            entry.set_text(str(value))
            self.entries[key] = entry
        self.lambda_slider = UIHorizontalSlider(
            pygame.Rect(170, 158, 96, 32),
            start_value=float(self.defaults.lambda_risk),
            value_range=(0.0, 1.0),
            click_increment=0.01,
            manager=self.manager,
            object_id="#lambda_risk",
        )

        self.start_button = UIButton(
            pygame.Rect(18, 790, 182, 44),
            text=t("gui.start"),
            manager=self.manager,
        )
        self.pause_button = UIButton(
            pygame.Rect(206, 790, 98, 44),
            text=t("gui.pause"),
            manager=self.manager,
        )
        self.pause_button.disable()
        self.first_button = UIButton(pygame.Rect(346, 327, 58, 30), text=t("gui.first"), manager=self.manager)
        self.previous_button = UIButton(pygame.Rect(410, 327, 74, 30), text=t("gui.previous"), manager=self.manager)
        self.next_button = UIButton(pygame.Rect(490, 327, 74, 30), text=t("gui.next"), manager=self.manager)
        self.last_button = UIButton(pygame.Rect(570, 327, 58, 30), text=t("gui.last"), manager=self.manager)
        self.expand_all_button = UIButton(
            pygame.Rect(1232, 382, 94, 26),
            text=t("gui.expand_all"),
            manager=self.manager,
        )
        self.collapse_all_button = UIButton(
            pygame.Rect(1334, 382, 94, 26),
            text=t("gui.collapse_all"),
            manager=self.manager,
        )
        self.locate_root_button = UIButton(
            pygame.Rect(1122, 382, 100, 26),
            text=t("gui.locate_root"),
            manager=self.manager,
        )
        if not ENABLE_ITERATION_TREE_RENDERING:
            # 暂停树 UI 时连同展开/收起/定位控件一起隐藏，避免用户触发
            # 仍会读取大图或启动 interval 回传的旧交互路径。
            for button in (
                self.expand_all_button,
                self.collapse_all_button,
                self.locate_root_button,
            ):
                button.hide()
        self.progress_bar = UIProgressBar(pygame.Rect(650, 327, 430, 30), manager=self.manager)

        self.matrix_entries: dict[str, UITextEntryLine] = {}
        matrix_entry_specs = (
            ("position_index", 18, 102, self.defaults.matrix_position_index, 135),
            ("actor_seat", 170, 102, int(self.defaults.matrix_actor_id) + 1, 135),
            ("samples", 18, 158, self.defaults.matrix_samples, 135),
        )
        for key, x, y, value, width in matrix_entry_specs:
            entry = UITextEntryLine(
                pygame.Rect(x, y, width, 32),
                manager=self.manager,
                object_id=f"#matrix_{key}",
            )
            entry.set_text(str(value))
            self.matrix_entries[key] = entry
        self.matrix_start_button = UIButton(
            pygame.Rect(18, 790, 182, 44),
            text=t("gui.matrix.start"),
            manager=self.manager,
            object_id="#matrix_start",
        )
        self.matrix_stop_button = UIButton(
            pygame.Rect(206, 790, 98, 44),
            text=t("gui.matrix.stop"),
            manager=self.manager,
            object_id="#matrix_stop",
        )
        self.matrix_stop_button.disable()
        self.matrix_first_button = UIButton(
            pygame.Rect(346, 327, 58, 30),
            text=t("gui.first"),
            manager=self.manager,
        )
        self.matrix_previous_button = UIButton(
            pygame.Rect(410, 327, 74, 30),
            text=t("gui.previous"),
            manager=self.manager,
        )
        self.matrix_next_button = UIButton(
            pygame.Rect(490, 327, 74, 30),
            text=t("gui.next"),
            manager=self.manager,
        )
        self.matrix_last_button = UIButton(
            pygame.Rect(570, 327, 58, 30),
            text=t("gui.last"),
            manager=self.manager,
        )
        self.matrix_progress_bar = UIProgressBar(
            pygame.Rect(650, 327, 430, 30),
            manager=self.manager,
        )
        self.tree_controls = [
            *self.entries.values(),
            self.lambda_slider,
            self.start_button,
            self.pause_button,
            self.first_button,
            self.previous_button,
            self.next_button,
            self.last_button,
            self.progress_bar,
            self.expand_all_button,
            self.collapse_all_button,
            self.locate_root_button,
        ]
        self.matrix_controls = [
            *self.matrix_entries.values(),
            self.matrix_start_button,
            self.matrix_stop_button,
            self.matrix_first_button,
            self.matrix_previous_button,
            self.matrix_next_button,
            self.matrix_last_button,
            self.matrix_progress_bar,
        ]

    def _switch_page(self, page: str) -> None:
        """切换可见页面，不销毁运行任务或修改矩阵/搜索请求。"""

        if page not in {"tree", "matrix"}:
            raise ValueError(t("gui.error.unknown_page", page=page))
        self.active_page = page
        self.tree_page_button.set_text(t("gui.page.tree_active" if page == "tree" else "gui.page.tree"))
        self.matrix_page_button.set_text(t("gui.page.matrix_active" if page == "matrix" else "gui.page.matrix"))
        for control in self.tree_controls:
            if page == "tree":
                control.show()
            else:
                control.hide()
        for control in self.matrix_controls:
            if page == "matrix":
                control.show()
            else:
                control.hide()
        if page == "tree" and not ENABLE_ITERATION_TREE_RENDERING:
            for control in (
                self.expand_all_button,
                self.collapse_all_button,
                self.locate_root_button,
            ):
                control.hide()
        if page == "tree" and self.matrix_running:
            self.status = t("gui.conflict.matrix_running")
        if page == "matrix" and self.running:
            self.matrix_status = t("gui.conflict.tree_running")
        self._sync_run_control_states()

    def _sync_run_control_states(self) -> None:
        """保证树搜索与矩阵计算不会在同一 GUI 会话内并发启动。"""

        if self.running or self.matrix_running:
            self.start_button.disable()
            self.matrix_start_button.disable()
        else:
            self.start_button.enable()
            self.matrix_start_button.enable()
        if self.running:
            self.pause_button.enable()
        else:
            self.pause_button.disable()
        if self.matrix_running:
            self.matrix_stop_button.enable()
        else:
            self.matrix_stop_button.disable()

    def _font(self, size: int) -> pygame.font.Font:
        return self.fonts[min(self.fonts, key=lambda item: abs(item - size))]

    def _text(
        self,
        text: str,
        position: tuple[int, int],
        *,
        size: int = 14,
        color: pygame.Color = TEXT,
        max_width: int | None = None,
    ) -> None:
        value = str(text)
        font = self._font(size)
        if max_width is not None:
            while value and font.size(value + "…")[0] > max_width:
                value = value[:-1]
            if value != str(text):
                value += "…"
        self.screen.blit(font.render(value, True, color), position)

    def _panel(self, rect: pygame.Rect, title: str) -> None:
        pygame.draw.rect(self.screen, PANEL, rect, border_radius=8)
        pygame.draw.rect(self.screen, BORDER, rect, width=1, border_radius=8)
        self._text(title, (rect.x + 12, rect.y + 9), size=16)

    def _draw_checkbox(
        self,
        rect: pygame.Rect,
        label: str,
        checked: bool,
        *,
        enabled: bool = True,
        hovered: bool = False,
    ) -> None:
        color = TEXT if enabled else pygame.Color("#596779")
        if hovered and enabled:
            pygame.draw.rect(
                self.screen,
                pygame.Color("#213B61"),
                rect.inflate(6, 4),
                border_radius=4,
            )
        box = pygame.Rect(rect.x, rect.y + 2, 18, 18)
        pygame.draw.rect(self.screen, PANEL_ALT, box, border_radius=3)
        pygame.draw.rect(self.screen, ACCENT if checked else BORDER, box, width=2, border_radius=3)
        if checked:
            pygame.draw.line(self.screen, ACCENT, (box.x + 4, box.y + 9), (box.x + 8, box.y + 14), 2)
            pygame.draw.line(self.screen, ACCENT, (box.x + 8, box.y + 14), (box.x + 15, box.y + 4), 2)
        self._text(label, (rect.x + 25, rect.y), size=14, color=color, max_width=rect.width - 25)

    def _sync_night_tactics(self) -> None:
        valid = self.values["include_witch"] or self.values["include_guard"]
        if not valid:
            for key in NIGHT_TACTICS:
                self.tactics[key] = False

    def _draw_config(self) -> None:
        self._panel(pygame.Rect(12, 12, 310, 766), "")
        self._text(t("gui.tree.subtitle"), (18, 62), size=12, color=MUTED)
        labels = (
            (t("label.number_of_players"), 18, 80),
            (t("label.number_of_wolves_short"), 170, 80),
            (t("label.page_size"), 18, 136),
            (t("label.lambda_risk_short"), 170, 136),
        )
        for label, x, y in labels:
            self._text(label, (x, y), size=12, color=MUTED)
        self._text(
            f"{float(self.lambda_slider.get_current_value()):.2f}",
            (272, 166),
            size=14,
            color=TEXT,
        )
        self._text(
            t("gui.tree.resume_notice"),
            (18, 204),
            size=12,
            color=MUTED,
        )

        self._text(t("gui.tree.roles"), (18, 260), size=16)
        toggle_y = 292
        self.toggle_rects.clear()
        for offset, key in enumerate((*ROLE_KEYS, "smart_vote")):
            column = offset % 2
            row = offset // 2
            rect = pygame.Rect(20 + column * 145, toggle_y + row * 31, 136, 24)
            self.toggle_rects[key] = rect
            self._draw_checkbox(
                rect,
                t(f"label.{key}"),
                self.values[key],
                hovered=rect.collidepoint(pygame.mouse.get_pos()),
            )

        if not self.values["smart_vote"]:
            self._text(t("gui.tree.tactics_disabled"), (20, 432), size=12, color=MUTED)
            self.tactic_rects.clear()
            return

        self.tactic_rects.clear()
        day_header = pygame.Rect(20, 432, 278, 26)
        self.header_rects["day"] = day_header
        if day_header.collidepoint(pygame.mouse.get_pos()):
            pygame.draw.rect(self.screen, pygame.Color("#213B61"), day_header, border_radius=4)
        self._text(("▼ " if self.day_expanded else "▶ ") + t("tactic.day"), (22, 434), size=14)
        y = 462
        if self.day_expanded:
            for key in DAY_TACTICS:
                rect = pygame.Rect(32, y, 260, 24)
                self.tactic_rects[key] = rect
                self._draw_checkbox(
                    rect,
                    t(f"tactic.{key}"),
                    self.tactics[key],
                    hovered=rect.collidepoint(pygame.mouse.get_pos()),
                )
                y += 27
        night_header = pygame.Rect(20, y + 3, 278, 26)
        self.header_rects["night"] = night_header
        if night_header.collidepoint(pygame.mouse.get_pos()):
            pygame.draw.rect(self.screen, pygame.Color("#213B61"), night_header, border_radius=4)
        self._text(("▼ " if self.night_expanded else "▶ ") + t("tactic.night"), (22, y + 5), size=14)
        y += 34
        if self.night_expanded:
            valid = self.values["include_witch"] or self.values["include_guard"]
            for key in NIGHT_TACTICS:
                rect = pygame.Rect(32, y, 260, 24)
                self.tactic_rects[key] = rect
                self._draw_checkbox(
                    rect,
                    t(f"tactic.{key}"),
                    self.tactics[key],
                    enabled=valid,
                    hovered=rect.collidepoint(pygame.mouse.get_pos()),
                )
                y += 27
            if not valid:
                self._text(t("gui.tree.night_tactic_requirement"), (34, y), size=12, color=MUTED)

    def _draw_matrix_config(self) -> None:
        """绘制发言收益页的必要输入和简短阅读说明。"""

        self._panel(pygame.Rect(12, 12, 310, 766), "")
        self._text(t("gui.matrix.subtitle"), (18, 62), size=12, color=MUTED)
        labels = (
            (t("gui.matrix.position"), 18, 80),
            (t("gui.matrix.actor"), 170, 80),
            (t("gui.matrix.samples"), 18, 136),
        )
        for label, x, y in labels:
            self._text(label, (x, y), size=12, color=MUTED)
        self._text(
            t("gui.matrix.purpose_line_1"),
            (20, 206),
            size=12,
            color=MUTED,
            max_width=280,
        )
        self._text(
            t("gui.matrix.purpose_line_2"),
            (20, 230),
            size=12,
            color=MUTED,
            max_width=280,
        )
        self._draw_checkbox(
            self.matrix_force_rect,
            t("gui.matrix.recompute"),
            self.matrix_force_recompute,
            hovered=self.matrix_force_rect.collidepoint(pygame.mouse.get_pos()),
        )
        self._text(t("gui.matrix.how_to_read"), (18, 314), size=16)
        guide_lines = (
            t("gui.matrix.guide_action"),
            t("gui.matrix.guide_strength"),
            t("gui.matrix.guide_delta"),
        )
        for offset, line in enumerate(guide_lines):
            self._text(line, (20, 348 + offset * 28), size=12, color=MUTED)
        self._text(t("gui.matrix.status_title"), (18, 458), size=16)
        self._text(
            t(
                "gui.matrix.progress",
                completed=self.matrix_committed_batches,
                total=self.matrix_total_batches,
            ),
            (20, 494),
            size=12,
            color=ACCENT if self.matrix_running else MUTED,
            max_width=280,
        )
        self._text(
            t("gui.matrix.result_rows", rows=len(self.matrix_rows)),
            (20, 522),
            size=12,
            color=MUTED,
        )
        self._text(
            t("gui.matrix.stop_notice"),
            (20, 564),
            size=12,
            color=MUTED,
            max_width=280,
        )

    def _matrix_visible_rows(self) -> tuple[list[dict[str, Any]], int]:
        """返回矩阵当前页的二级动作行，固定每页十行。"""

        pages = max(1, math.ceil(len(self.matrix_rows) / 10))
        self.matrix_page_index = max(0, min(self.matrix_page_index, pages - 1))
        start = self.matrix_page_index * 10
        return self.matrix_rows[start : start + 10], pages

    @staticmethod
    def _matrix_cell_text(row: dict[str, Any], credibility: float) -> str:
        """格式化一个可信度单元格的收益均值与配对差。"""

        values = dict(row.get("by_credibility") or {}).get(str(float(credibility)))
        if not values:
            return "—"
        return f"{float(values.get('mean', 0.0)):+.3f} / {float(values.get('baseline_delta', 0.0)):+.3f}"

    def _draw_matrix_table(self) -> None:
        """绘制二级动作分页主表，三个可信度档位不合并。"""

        rect = pygame.Rect(334, 12, 1114, 352)
        self._panel(rect, t("gui.matrix.table_title"))
        visible, pages = self._matrix_visible_rows()
        columns = (
            ("#", 346),
            (t("gui.matrix.action"), 382),
            (t("gui.matrix.credibility_none"), 676),
            (t("gui.matrix.credibility_medium"), 890),
            (t("gui.matrix.credibility_high"), 1104),
            (t("gui.matrix.sample_short"), 1330),
        )
        for label, x in columns:
            self._text(label, (x, 48), size=12, color=MUTED)
        self.matrix_table_row_rects.clear()
        for offset, row in enumerate(visible):
            row_index = self.matrix_page_index * 10 + offset
            y = 72 + offset * 24
            row_rect = pygame.Rect(342, y - 2, 1098, 23)
            self.matrix_table_row_rects.append((row_rect, row_index))
            if row_index == self.matrix_selected_row:
                pygame.draw.rect(self.screen, pygame.Color("#213B61"), row_rect, border_radius=3)
            elif row_rect.collidepoint(pygame.mouse.get_pos()):
                pygame.draw.rect(self.screen, pygame.Color("#1A2D48"), row_rect, border_radius=3)
            credibility_values = dict(row.get("by_credibility") or {})
            sample_count = 0
            if credibility_values:
                sample_count = min(int(value.get("sample_count", 0)) for value in credibility_values.values())
            values = (
                (str(row_index + 1), 346, 32),
                (_matrix_action_label(dict(row.get("action") or {})), 382, 282),
                (self._matrix_cell_text(row, 0.0), 676, 202),
                (self._matrix_cell_text(row, 0.5), 890, 202),
                (self._matrix_cell_text(row, 0.8), 1104, 202),
                (str(sample_count), 1330, 80),
            )
            for value, x, width in values:
                self._text(value, (x, y), size=12, color=TEXT, max_width=width)
        self._text(
            t(
                "gui.matrix.page",
                page=self.matrix_page_index + 1,
                pages=pages,
                rows=len(self.matrix_rows),
            ),
            (1090, 343),
            size=11,
            color=MUTED,
            max_width=330,
        )

    def _draw_matrix_detail(self) -> None:
        """用简短字段展示所选动作的三档统计和反应情景。"""

        rect = pygame.Rect(334, 374, 1114, 472)
        self._panel(rect, t("gui.matrix.detail_title"))
        if not self.matrix_rows:
            headline = t("gui.matrix.empty_running") if self.matrix_running else t("gui.matrix.empty_ready")
            self._text(headline, (358, 432), size=16, color=ACCENT)
            self._text(
                t("gui.matrix.empty_note"),
                (358, 470),
                size=13,
                color=MUTED,
            )
            return
        selected_index = self.matrix_selected_row
        if selected_index is None or not 0 <= selected_index < len(self.matrix_rows):
            selected_index = 0
            self.matrix_selected_row = 0
        row = self.matrix_rows[selected_index]
        action = dict(row.get("action") or {})
        request = dict((self.matrix_result or {}).get("request") or {})
        self._text(_matrix_action_label(action), (358, 414), size=20, color=ACCENT, max_width=760)
        self._text(
            t(
                "gui.matrix.actor_summary",
                seat=int(request.get("actor_id", 0)) + 1,
                role=request.get("actor_role", t("common.unknown")),
            ),
            (358, 450),
            size=12,
            color=MUTED,
            max_width=790,
        )
        family_labels = {
            family: t(f"matrix.family.{family}")
            for family in ("baseline", "silence", "accusation", "support", "vote_intent", "seer_claim")
        }
        tactic_labels = {
            "villager_decoy": t("matrix.tactic.villager_decoy"),
            "wolf_jump": t("matrix.tactic.wolf_jump"),
        }
        claim_result_label = {"good": t("matrix.result.good"), "wolf": t("matrix.result.wolf")}.get(
            str(action.get("claim_result")),
            t("common.none"),
        )
        detail_lines = (
            t(
                "gui.matrix.detail_family",
                value=family_labels.get(str(action.get("family")), action.get("family") or t("common.unknown")),
            ),
            t("gui.matrix.detail_target", value=_seat_label(action.get("target_id"))),
            t("gui.matrix.detail_claim", value=action.get("claim_role") or t("common.none")),
            t("gui.matrix.detail_check", seat=_seat_label(action.get("claim_target")), result=claim_result_label),
            t("gui.matrix.detail_intensity", value=float(action.get("intensity", 0.0))),
            t(
                "gui.matrix.detail_tactic",
                value=tactic_labels.get(str(action.get("tactic")), action.get("tactic") or t("common.none")),
            ),
        )
        for offset, line in enumerate(detail_lines):
            column = offset % 3
            row_offset = offset // 3
            self._text(
                line,
                (358 + column * 300, 484 + row_offset * 28),
                size=12,
                color=TEXT,
                max_width=280,
            )
        scenario_labels = {
            key: t(f"matrix.scenario.{key}")
            for key in (
                "backlash",
                "target_transfer",
                "claim_accepted",
                "claim_contested",
                "ignored",
                "other",
            )
        }
        credibility_keys = (
            "gui.matrix.credibility_none",
            "gui.matrix.credibility_medium",
            "gui.matrix.credibility_high",
        )
        for credibility_index, credibility in enumerate((0.0, 0.5, 0.8)):
            value = dict(row.get("by_credibility") or {}).get(str(credibility), {})
            x = 358 + credibility_index * 350
            self._text(t(credibility_keys[credibility_index]), (x, 558), size=16, color=ACCENT)
            self._text(
                t(
                    "gui.matrix.value_mean",
                    mean=float(value.get("mean", 0.0)),
                    error=float(value.get("standard_error", 0.0)),
                ),
                (x, 590),
                size=12,
                max_width=330,
            )
            self._text(
                t(
                    "gui.matrix.value_delta",
                    delta=float(value.get("baseline_delta", 0.0)),
                    error=float(value.get("baseline_delta_standard_error", 0.0)),
                ),
                (x, 616),
                size=12,
                max_width=330,
            )
            self._text(
                t("gui.matrix.value_samples", count=int(value.get("sample_count", 0))),
                (x, 642),
                size=12,
                color=MUTED,
            )
            scenarios = dict(value.get("scenario_counts") or {})
            scenario_text = "  ".join(f"{label}{int(scenarios.get(key, 0))}" for key, label in scenario_labels.items())
            # 六类情景分两行，避免三列详情互相覆盖。
            first_three = "  ".join(scenario_text.split("  ")[:3])
            last_three = "  ".join(scenario_text.split("  ")[3:])
            self._text(first_three, (x, 674), size=11, color=MUTED, max_width=330)
            self._text(last_three, (x, 698), size=11, color=MUTED, max_width=330)
        self._text(
            str((self.matrix_result or {}).get("notice") or t("matrix.notice.model_scope")),
            (358, 758),
            size=12,
            color=MUTED,
            max_width=1040,
        )

    def _page_size(self) -> int:
        try:
            return min(10, max(1, int(self.entries["page_size"].get_text())))
        except ValueError:
            return 20

    def _visible_rows(self) -> tuple[list[dict[str, Any]], int]:
        ordered = sorted(self.rows, key=lambda item: int(item["position_index"]))
        pages = max(1, math.ceil(len(ordered) / self._page_size()))
        self.page_index = max(0, min(self.page_index, pages - 1))
        start = self.page_index * self._page_size()
        return ordered[start : start + self._page_size()], pages

    def _draw_table(self) -> None:
        rect = pygame.Rect(334, 12, 1114, 352)
        self._panel(rect, t("gui.tree.results_title"))
        visible, pages = self._visible_rows()
        columns = (
            ("#", 346, 45),
            (t("gui.col.position"), 392, 335),
            (t("gui.col.states"), 732, 76),
            (t("gui.col.edges"), 812, 68),
            (t("gui.col.terminals"), 884, 64),
            (t("gui.col.wide"), 952, 142),
            (t("gui.col.narrow"), 1098, 142),
            (t("gui.col.camp"), 1244, 76),
            (t("gui.col.runtime"), 1324, 100),
        )
        for label, x, _width in columns:
            self._text(label, (x, 48), size=12, color=MUTED)
        self.table_row_rects.clear()
        max_rows = min(10, max(1, self._page_size()))
        for offset, item in enumerate(visible[:max_rows]):
            y = 72 + offset * 24
            row_rect = pygame.Rect(342, y - 2, 1098, 23)
            global_index = self.rows.index(item)
            self.table_row_rects.append((row_rect, global_index))
            if global_index == self.selected_row:
                pygame.draw.rect(self.screen, pygame.Color("#213B61"), row_rect, border_radius=3)
            elif row_rect.collidepoint(pygame.mouse.get_pos()):
                pygame.draw.rect(self.screen, pygame.Color("#1A2D48"), row_rect, border_radius=3)
            processing = bool(item.get("processing", False))
            interval_recomputing = bool(item.get("interval_recomputing", False))
            busy = processing or interval_recomputing
            processing_phase = str(item.get("processing_phase", "node_progress"))
            postprocess_total = int(item.get("postprocess_total", 0))
            postprocess_completed = int(item.get("postprocess_completed", 0))
            postprocess_percent = int(100.0 * postprocess_completed / max(1, postprocess_total))
            interval_cell = (
                t("gui.interval_short", percent=postprocess_percent)
                if busy and processing_phase == "interval_progress"
                else t("postprocess.path_counts")
                if busy and processing_phase == "path_progress"
                else t("gui.computing")
            )
            camp = str(item["camp"])
            camp_color = ACCENT if busy else GOOD if camp == "good" else WOLF if camp == "wolf" else MUTED
            wide = item["wide_interval"]
            narrow = item["narrow_interval"]
            values = (
                (("▶" if busy else "") + str(item["position_index"]), 346, 42),
                (
                    item.get("position_display", " | ".join(f"{i + 1}:{r}" for i, r in enumerate(item["roles"]))),
                    392,
                    330,
                ),
                (f"{int(item['state_count']):,}", 732, 72),
                (f"{int(item['edge_count']):,}", 812, 64),
                (f"{int(item['terminal_count']):,}", 884, 60),
                (interval_cell if busy else f"[{wide[0]:.3f},{wide[1]:.3f}]", 952, 138),
                ("—" if busy else f"[{narrow[0]:.3f},{narrow[1]:.3f}]", 1098, 138),
                (t("gui.computing") if busy else t(f"camp.{camp}"), 1244, 72),
                (f"{float(item['runtime_seconds']):.2f}s", 1324, 96),
            )
            for value, x, width in values:
                self._text(value, (x, y), size=12, color=camp_color if x == 1244 else TEXT, max_width=width)
        self._text(
            t("gui.page", page=self.page_index + 1, pages=pages, rows=len(self.rows)),
            (1090, 343),
            size=11,
            color=MUTED,
        )

    def _graph_rect(self) -> pygame.Rect:
        return pygame.Rect(334, 374, 1114, 472)

    @staticmethod
    def _graph_depth(node: dict[str, Any]) -> int:
        """读取节点的迭代深度；兼容旧图时再退化到昼夜轮次之和。

        新的实时节点和持久化节点优先使用显式 ``depth``。历史图可能只
        保存 ``state_observation``，其第四个字段同样是搜索深度；最后才
        使用 ``day_count + night_count``，保证旧缓存仍能显示而不会把
        可视化坐标写回游戏状态。
        """

        raw_depth = node.get("depth")
        if raw_depth is None:
            state = node.get("state")
            if isinstance(state, dict):
                raw_depth = state.get("depth")
        if raw_depth is None:
            observation = node.get("state_observation")
            if isinstance(observation, (list, tuple)) and len(observation) > 3:
                raw_depth = observation[3]
        if raw_depth is None:
            raw_depth = int(node.get("day_count", 0)) + int(node.get("night_count", 0))
        try:
            return max(0, int(raw_depth))
        except (TypeError, ValueError):
            return 0

    def _layout_graph(self, graph: dict[str, Any]) -> dict[int, tuple[float, float]]:
        """按固定网格生成 DAG 画布坐标。

        X 轴严格使用迭代深度，深度每增加一级就前进一个固定网格列；
        同一深度的节点按稳定 ``node_id`` 排序后占用连续网格行。这样
        节点数量变化不会改变列宽或相邻节点间距，超出视口的部分仍由
        原有平移/缩放交互查看。坐标仅供 UI 布局使用，不写入搜索状态。
        """
        groups: dict[int, list[int]] = {}
        for node in graph.get("nodes", []):
            depth = self._graph_depth(node)
            groups.setdefault(depth, []).append(int(node["node_id"]))
        positions: dict[int, tuple[float, float]] = {}
        for depth, node_ids in sorted(groups.items()):
            ordered_ids = sorted(node_ids)
            row_center = (len(ordered_ids) - 1) / 2.0
            for row_index, node_id in enumerate(ordered_ids):
                positions[node_id] = (
                    depth * GRAPH_GRID_COLUMN_SPACING,
                    (row_index - row_center) * GRAPH_GRID_ROW_SPACING,
                )
        return positions

    @staticmethod
    def _safe_surface_point(
        surface: pygame.Surface,
        point: tuple[float, float],
    ) -> tuple[int, int] | None:
        """把绘制坐标收敛为有限整数，避免把异常浮点值传入 SDL。

        搜索图的画布坐标来自平移、缩放和深度计算。正常情况下它们都在
        视口附近，但原生 Pygame 绘制接口不应接收 NaN、无穷大或超大浮点
        数；这类值在 Windows 下可能绕过 Python 异常直接触发 access
        violation。先做有限性检查、有限范围裁剪和整数化，再由
        ``clipline`` 负责视口外裁剪。
        """

        try:
            x_value = float(point[0])
            y_value = float(point[1])
        except (TypeError, ValueError, IndexError):
            return None
        if not math.isfinite(x_value) or not math.isfinite(y_value):
            return None
        width, height = surface.get_size()
        coordinate_limit = max(
            GRAPH_SAFE_COORDINATE_LIMIT,
            max(width, height) * 4,
        )
        return (
            max(-coordinate_limit, min(coordinate_limit, int(round(x_value)))),
            max(-coordinate_limit, min(coordinate_limit, int(round(y_value)))),
        )

    @classmethod
    def _draw_safe_line(
        cls,
        surface: pygame.Surface,
        color: pygame.Color,
        start: tuple[float, float],
        end: tuple[float, float],
        width: int = 1,
    ) -> None:
        """只向 Pygame 传递已经裁剪过的整数线段。"""

        start_point = cls._safe_surface_point(surface, start)
        end_point = cls._safe_surface_point(surface, end)
        if start_point is None or end_point is None:
            return
        safe_width = max(1, min(64, int(width)))
        if start_point[1] == end_point[1]:
            left = min(start_point[0], end_point[0])
            cls._fill_safe_rect(
                surface,
                color,
                left,
                start_point[1] - safe_width // 2,
                abs(end_point[0] - start_point[0]) + 1,
                safe_width,
            )
            return
        if start_point[0] == end_point[0]:
            top = min(start_point[1], end_point[1])
            cls._fill_safe_rect(
                surface,
                color,
                start_point[0] - safe_width // 2,
                top,
                safe_width,
                abs(end_point[1] - start_point[1]) + 1,
            )
            return
        clipped = surface.get_rect().clipline(start_point, end_point)
        if not clipped:
            return
        pygame.draw.line(
            surface,
            color,
            clipped[0],
            clipped[1],
            safe_width,
        )

    @staticmethod
    def _fill_safe_rect(
        surface: pygame.Surface,
        color: pygame.Color,
        left: int,
        top: int,
        width: int,
        height: int,
    ) -> None:
        """用纯整数边界填充线段，避免 Rect/line 的原生裁剪路径。"""

        surface_width, surface_height = surface.get_size()
        right = min(surface_width, left + max(0, width))
        bottom = min(surface_height, top + max(0, height))
        left = max(0, left)
        top = max(0, top)
        if right <= left or bottom <= top:
            return
        surface.fill(color, (left, top, right - left, bottom - top))

    @classmethod
    def _draw_dashed_line(
        cls,
        surface: pygame.Surface,
        start: tuple[float, float],
        end: tuple[float, float],
        color: pygame.Color,
        *,
        dash_length: int = GRAPH_GRID_DASH_LENGTH,
        gap_length: int = GRAPH_GRID_GAP_LENGTH,
        width: int = 1,
    ) -> None:
        """在离屏小 surface 上合成虚线，再一次性 blit 到视口。

        Pygame-ce 2.5.8 在 Windows 真窗口上对显示 surface 连续执行
        细粒度 ``draw.line``/``fill`` 时可能破坏 SDL surface；具体版本
        的崩溃栈还会落在调用者的 Python 算术行，无法由异常处理捕获。
        网格线先在有界离屏 surface 上合成，显示 surface 每条线只接收
        一次 blit；节点、边和搜索数据不依赖这些观测装饰。
        """

        safe_start = cls._safe_surface_point(surface, start)
        safe_end = cls._safe_surface_point(surface, end)
        if safe_start is None or safe_end is None:
            return
        dx = safe_end[0] - safe_start[0]
        dy = safe_end[1] - safe_start[1]
        length = math.hypot(dx, dy)
        if length <= 0.0:
            return
        dash_length = max(1, int(dash_length))
        gap_length = max(0, int(gap_length))
        safe_width = max(1, min(64, int(width)))
        if safe_start[1] == safe_end[1]:
            left = min(safe_start[0], safe_end[0])
            span = abs(safe_end[0] - safe_start[0]) + 1
            pattern = pygame.Surface((span, safe_width), flags=pygame.SRCALPHA)
            pattern.fill((0, 0, 0, 0))
            cursor = 0
            while cursor < span:
                dash_end = min(span, cursor + dash_length)
                pattern.fill(color, (cursor, 0, dash_end - cursor, safe_width))
                cursor += dash_length + gap_length
            surface.blit(pattern, (left, safe_start[1] - safe_width // 2))
            return
        if safe_start[0] == safe_end[0]:
            top = min(safe_start[1], safe_end[1])
            span = abs(safe_end[1] - safe_start[1]) + 1
            pattern = pygame.Surface((safe_width, span), flags=pygame.SRCALPHA)
            pattern.fill((0, 0, 0, 0))
            cursor = 0
            while cursor < span:
                dash_end = min(span, cursor + dash_length)
                pattern.fill(color, (0, cursor, safe_width, dash_end - cursor))
                cursor += dash_length + gap_length
            surface.blit(pattern, (safe_start[0] - safe_width // 2, top))
            return
        ux = dx / length
        uy = dy / length
        cursor = 0.0
        while cursor < length:
            dash_end = min(length, cursor + dash_length)
            dash_start = (
                int(round(safe_start[0] + ux * cursor)),
                int(round(safe_start[1] + uy * cursor)),
            )
            dash_stop = (
                int(round(safe_start[0] + ux * dash_end)),
                int(round(safe_start[1] + uy * dash_end)),
            )
            cls._draw_safe_line(surface, color, dash_start, dash_stop, safe_width)
            cursor += dash_length + gap_length

    def _graph_view_rect(self, rect: pygame.Rect) -> pygame.Rect:
        """返回节点绘制区，底部为坐标轴和选中节点详情预留空间。"""

        return pygame.Rect(
            rect.x + 8,
            rect.y + 110,
            rect.width - 16,
            rect.height - 228,
        )

    def _draw_graph_grid(
        self,
        rect: pygame.Rect,
        graph: dict[str, Any],
    ) -> None:
        """绘制迭代深度坐标轴、深度虚线和固定网格行。"""

        view = self._graph_view_rect(rect)
        axis_y = view.bottom - GRAPH_AXIS_BOTTOM_MARGIN
        grid_color = pygame.Color("#263852")
        depth_color = pygame.Color("#3A5578")
        axis_color = pygame.Color("#8BA6C7")

        # 横向网格线只覆盖当前视口附近的行，避免大 DAG 造成额外绘制开销。
        visible_row_min = (
            math.floor((view.top - (view.centery + self.graph_pan[1])) / (GRAPH_GRID_ROW_SPACING * self.graph_zoom)) - 1
        )
        visible_row_max = (
            math.ceil((axis_y - (view.centery + self.graph_pan[1])) / (GRAPH_GRID_ROW_SPACING * self.graph_zoom)) + 1
        )
        for row_index in range(visible_row_min, visible_row_max + 1):
            row_y = self._screen_graph_point((0.0, row_index * GRAPH_GRID_ROW_SPACING), rect)[1]
            if view.top <= row_y <= axis_y:
                self._draw_dashed_line(
                    self.screen,
                    (view.left, row_y),
                    (view.right, row_y),
                    grid_color,
                    dash_length=4,
                    gap_length=8,
                )

        depths = sorted({self._graph_depth(node) for node in graph.get("nodes", [])})
        for depth in depths:
            x = self._screen_graph_point((depth * GRAPH_GRID_COLUMN_SPACING, 0.0), rect)[0]
            if view.left - 12 <= x <= view.right + 12:
                self._draw_dashed_line(
                    self.screen,
                    (x, view.top),
                    (x, axis_y),
                    depth_color,
                )
                self._draw_safe_line(
                    self.screen,
                    depth_color,
                    (x, axis_y - 4),
                    (x, axis_y + 4),
                )
                self._text(
                    f"D{depth}",
                    (x - 14, axis_y + 5),
                    size=11,
                    color=axis_color,
                    max_width=34,
                )

        self._draw_safe_line(
            self.screen,
            axis_color,
            (view.left, axis_y),
            (view.right - 8, axis_y),
        )
        pygame.draw.polygon(
            self.screen,
            axis_color,
            [
                (view.right - 8, axis_y),
                (view.right - 15, axis_y - 4),
                (view.right - 15, axis_y + 4),
            ],
        )
        self._text(
            t("gui.tree.depth_axis"),
            (view.left + 8, axis_y - 18),
            size=11,
            color=axis_color,
            max_width=110,
        )

    def _screen_graph_point(
        self,
        canvas: tuple[float, float],
        rect: pygame.Rect,
    ) -> tuple[float, float]:
        """将画布坐标投影到屏幕，并让 DAG 根层从左侧内边距起步。

        横向基准使用画布左边界而不是中心点，保证预览初始状态即可看到
        根节点和后续深度的左到右展开；纵向基准使用网格视口中心，保证
        同深度节点从上到下排列时不会与坐标轴争用顶部空间。平移与缩放
        仍通过 ``graph_pan`` 和 ``graph_zoom`` 保持原有交互语义。
        """
        view = self._graph_view_rect(rect)
        return (
            rect.x + 42 + self.graph_pan[0] + canvas[0] * self.graph_zoom,
            view.centery + self.graph_pan[1] + canvas[1] * self.graph_zoom,
        )

    def _draw_live_stats(self, rect: pygame.Rect) -> None:
        good_paths = int(self.live_stats["good_paths"])
        wolf_paths = int(self.live_stats["wolf_paths"])
        if good_paths > wolf_paths:
            relation = t("camp.good")
            relation_color = GOOD
        elif wolf_paths > good_paths:
            relation = t("camp.wolf")
            relation_color = WOLF
        else:
            relation = t("camp.balanced")
            relation_color = MUTED
        cards = (
            (t("gui.stat.expanded"), _compact_integer(int(self.live_stats["expanded_nodes"])), ACCENT),
            (t("gui.stat.discovered"), _compact_integer(int(self.live_stats["discovered_nodes"])), TEXT),
            (t("gui.stat.frontier"), _compact_integer(int(self.live_stats["frontier_size"])), TEXT),
            (t("gui.stat.edges"), _compact_integer(int(self.live_stats["edge_count"])), TEXT),
            (t("gui.stat.terminals"), _compact_integer(int(self.live_stats["terminal_count"])), TEXT),
            (t("gui.stat.good"), _compact_integer(good_paths), GOOD),
            (t("gui.stat.wolf"), _compact_integer(wolf_paths), WOLF),
            (t("gui.stat.relation"), relation, relation_color),
        )
        width = (rect.width - 82) // 8
        for index, (label, value, color) in enumerate(cards):
            card = pygame.Rect(rect.x + 10 + index * (width + 8), rect.y + 38, width, 48)
            pygame.draw.rect(self.screen, PANEL_ALT, card, border_radius=5)
            pygame.draw.rect(self.screen, BORDER, card, width=1, border_radius=5)
            self._text(label, (card.x + 9, card.y + 5), size=11, color=MUTED)
            self._text(value, (card.x + 9, card.y + 24), size=14, color=color, max_width=card.width - 18)

    @staticmethod
    def _distance_to_segment(
        point: tuple[float, float],
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> float:
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        if dx == 0.0 and dy == 0.0:
            return math.dist(point, start)
        ratio = max(
            0.0,
            min(
                1.0,
                ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / (dx * dx + dy * dy),
            ),
        )
        projection = (start[0] + ratio * dx, start[1] + ratio * dy)
        return math.dist(point, projection)

    def _selected_node_details(self, node_id: int | None = None) -> list[str]:
        target_node = self.selected_node if node_id is None else node_id
        if target_node is None:
            return []
        graph = self._display_graph()
        node = self._graph_structure(graph)["node_by_id"].get(target_node)
        if node is None:
            return []
        if node.get("live_preview", False):
            state_label = t("gui.graph.expanded") if node.get("expanded", False) else t("gui.graph.frontier")
            details = [
                t(
                    "gui.graph.live_node",
                    node=target_node,
                    phase=_phase_label(node["phase"]),
                    state=state_label,
                )
            ]
            position_display = str(graph.get("position_display", ""))
            if position_display:
                details.append(position_display)
        else:
            details = [
                t(
                    "gui.graph.saved_node",
                    node=target_node,
                    phase=_phase_label(node["phase"]),
                    wide=node["wide_interval"],
                    narrow=node["narrow_interval"],
                )
            ]
        if self.selected_row is not None:
            try:
                state = node.get("state")
                if not isinstance(state, dict):
                    compact = node.get("state_compact") or []
                    roles = self.rows[self.selected_row].get("roles", [])
                    if compact and roles:
                        state = game_state_dict_from_compact(
                            compact,
                            roles=roles,
                            position_signature=str(graph.get("position_signature", "")),
                            is_game_over=bool(node.get("is_terminal", False)),
                            state_id=target_node,
                            observation=node.get("state_observation"),
                        )
                if isinstance(state, dict) and not state.get("unavailable"):
                    players = state.get("players") or []
                    chips = [
                        t(
                            "gui.graph.player_chip",
                            seat=index + 1,
                            role=player.get("role", t("common.unknown")),
                            suffix=(t("gui.graph.dead_suffix") if not player.get("is_alive", False) else ""),
                        )
                        for index, player in enumerate(players)
                    ]
                    if chips:
                        details.append("  ".join(chips))
            except (TypeError, ValueError, KeyError, IndexError):
                # 旧节点快照可能无法按当前角色列表恢复；详情区不能因此
                # 让主绘制循环抛出异常或中断后续 Pygame 事件处理。
                details.append(t("gui.graph.snapshot_unavailable"))
        structure = self._graph_structure(graph)
        incident = [
            *structure["edges_by_parent"].get(target_node, []),
            *structure["edges_by_child"].get(target_node, []),
        ]
        for edge in incident[:5]:
            for reason in edge.get("reasons", []):
                details.append(
                    f"{edge['parent_id']}→{edge['child_id']}: {reason.get('action_label', reason.get('action_key', ''))}"
                )
        return details

    def _node_game_state_details(self, node_id: int) -> list[str]:
        graph = self._display_graph()
        node = self._graph_structure(graph)["node_by_id"].get(node_id)
        if node is None:
            return []
        state = node.get("state")
        if not isinstance(state, dict):
            compact = node.get("state_compact") or []
            roles = list(graph.get("roles", []))
            if not roles and self.selected_row is not None:
                roles = list(self.rows[self.selected_row].get("roles", []))
            try:
                # 历史嵌套快照与当前扁平快照统一交给契约解码器校验，
                # GUI 不猜测内部字段偏移。
                if not compact or not roles:
                    raise ValueError(t("common.unavailable"))
                state = game_state_dict_from_compact(
                    compact,
                    roles=roles,
                    position_signature=str(graph.get("position_signature", "")),
                    is_game_over=bool(node.get("is_terminal", False)),
                    state_id=node_id,
                    observation=node.get("state_observation"),
                )
            except (TypeError, ValueError):
                state = {"unavailable": t("common.unavailable")}
        return _format_game_state_hover(node_id, node, state)

    def _draw_graph(self) -> None:
        rect = self._graph_rect()
        raw_graph = self._display_graph()
        graph = self._visible_graph(raw_graph)
        live_preview = self.running and self.preview_position in self.live_graphs
        title = (
            t(
                "gui.tree.live_preview",
                position=self.preview_position,
                limit=LIVE_PREVIEW_NODE_LIMIT,
            )
            if live_preview
            else t("gui.tree.saved_preview", limit=DAG_VISIBLE_NODE_LIMIT)
        )
        self._panel(rect, title)
        self._draw_live_stats(rect)
        position_display = str(graph.get("position_display", ""))
        if live_preview and position_display:
            self._text(
                position_display,
                (rect.x + 12, rect.y + 92),
                size=12,
                color=ACCENT,
                max_width=rect.width - 24,
            )
        clip_before = self.screen.get_clip()
        view_rect = self._graph_view_rect(rect)
        self.screen.set_clip(view_rect)
        canvas_positions = self._layout_graph(graph)
        self.node_screen_positions = {
            node_id: self._screen_graph_point(position, rect) for node_id, position in canvas_positions.items()
        }
        self._draw_graph_grid(rect, graph)
        mouse = pygame.mouse.get_pos()
        self.hovered_node = None
        nearest_node_distance = 24.0
        for node_id, point in self.node_screen_positions.items():
            distance = math.dist(mouse, point)
            if distance < nearest_node_distance:
                self.hovered_node = node_id
                nearest_node_distance = distance
        self.hovered_edge = None
        nearest_edge_distance = 9.0
        for edge in graph.get("edges", []):
            parent = self.node_screen_positions.get(int(edge["parent_id"]))
            child = self.node_screen_positions.get(int(edge["child_id"]))
            if parent is None or child is None:
                continue
            distance = self._distance_to_segment(mouse, parent, child)
            if distance < nearest_edge_distance:
                self.hovered_edge = edge
                nearest_edge_distance = distance

        self.hover_tooltip = []
        now = time.monotonic()
        node_alpha = {
            int(node["node_id"]): max(
                0.12,
                min(
                    1.0,
                    (now - float(node.get("_visible_since", now - LIVE_NODE_FADE_SECONDS))) / LIVE_NODE_FADE_SECONDS,
                ),
            )
            for node in graph.get("nodes", [])
        }
        for edge in graph.get("edges", []):
            parent = self.node_screen_positions.get(int(edge["parent_id"]))
            child = self.node_screen_positions.get(int(edge["child_id"]))
            if parent is None or child is None:
                continue
            if edge.get("live_preview", False):
                color = pygame.Color("#64748B")
            else:
                interval = RewardInterval(*edge["wide_interval"])
                color = pygame.Color(interval_branch_color(interval))
            alpha = min(
                node_alpha.get(int(edge["parent_id"]), 1.0),
                node_alpha.get(int(edge["child_id"]), 1.0),
            )
            color = _faded_color(color, alpha)
            is_hovered = edge is self.hovered_edge and self.hovered_node is None
            self._draw_safe_line(
                self.screen,
                color,
                parent,
                child,
                max(1, int(2 * self.graph_zoom)) + (3 if is_hovered else 0),
            )
            if is_hovered:
                edge_summary = (
                    t(
                        "gui.graph.live_edge",
                        parent=edge["parent_id"],
                        child=edge["child_id"],
                    )
                    if edge.get("live_preview", False)
                    else t(
                        "gui.graph.saved_edge",
                        parent=edge["parent_id"],
                        child=edge["child_id"],
                        wide=edge["wide_interval"],
                    )
                )
                self.hover_tooltip = [
                    edge_summary,
                    t("gui.graph.edge_reasons"),
                    *[
                        str(reason.get("action_label") or reason.get("action_key") or t("gui.graph.unnamed_reason"))
                        for reason in edge.get("reasons", [])
                    ],
                ]
        node_lookup = {int(node["node_id"]): node for node in graph.get("nodes", [])}
        nodes_with_children = set(self._graph_structure(raw_graph)["children"])
        expanded_nodes = raw_graph.setdefault("_expanded_node_ids", set())
        node_width = max(30, int(42 * self.graph_zoom))
        node_height = max(18, int(24 * self.graph_zoom))
        for node_id, point in self.node_screen_positions.items():
            node = node_lookup[node_id]
            alpha = node_alpha.get(node_id, 1.0)
            if node["is_terminal"]:
                color = GOOD if "好人" in str(node["result"]) else WOLF
            else:
                color = pygame.Color("#CBD5E1")
            color = _faded_color(color, alpha)
            node_rect = pygame.Rect(
                int(point[0] - node_width / 2),
                int(point[1] - node_height / 2),
                node_width,
                node_height,
            )
            pygame.draw.rect(
                self.screen,
                _faded_color(pygame.Color("#0D1727"), alpha),
                node_rect,
                border_radius=5,
            )
            pygame.draw.rect(
                self.screen,
                _faded_color(
                    ACCENT if node_id == self.selected_node else BORDER,
                    alpha,
                ),
                node_rect.inflate(6 if node_id == self.hovered_node else 0, 6 if node_id == self.hovered_node else 0),
                width=3 if node_id == self.hovered_node else 2,
                border_radius=6,
            )
            phase_color = _faded_color(
                pygame.Color("#FBBF24") if node["phase"] == "day" else pygame.Color("#8B5CF6"),
                alpha,
            )
            pygame.draw.circle(
                self.screen,
                phase_color,
                (node_rect.x + 7, node_rect.centery),
                max(3, int(4 * self.graph_zoom)),
            )
            self._text(
                f"N{node_id}",
                (node_rect.x + 14, node_rect.y + max(1, (node_rect.height - 14) // 2)),
                size=12,
                color=color,
                max_width=node_rect.width - 17,
            )
            if node_id in nodes_with_children:
                marker_center = (node_rect.right + 6, node_rect.centery)
                marker_color = _faded_color(ACCENT, alpha)
                pygame.draw.circle(self.screen, PANEL_ALT, marker_center, 6)
                pygame.draw.circle(self.screen, marker_color, marker_center, 6, width=1)
                pygame.draw.line(
                    self.screen,
                    marker_color,
                    (marker_center[0] - 3, marker_center[1]),
                    (marker_center[0] + 3, marker_center[1]),
                    1,
                )
                if node_id not in expanded_nodes:
                    pygame.draw.line(
                        self.screen,
                        marker_color,
                        (marker_center[0], marker_center[1] - 3),
                        (marker_center[0], marker_center[1] + 3),
                        1,
                    )
            if node_id == self.hovered_node:
                self.hover_tooltip = self._node_game_state_details(node_id)
        self.screen.set_clip(clip_before)

        details = self._selected_node_details(self.hovered_node)
        if not details:
            details = self._selected_node_details()
        if details:
            detail_rect = pygame.Rect(rect.x + 8, rect.bottom - 92, rect.width - 16, 84)
            pygame.draw.rect(self.screen, pygame.Color("#0D1727"), detail_rect, border_radius=5)
            for offset, line in enumerate(details[:4]):
                self._text(
                    line,
                    (detail_rect.x + 8, detail_rect.y + 7 + offset * 18),
                    size=12,
                    max_width=detail_rect.width - 16,
                )
        elif not graph.get("nodes"):
            self._text(
                t("gui.tree.waiting_preview") if self.running else t("gui.tree.select_preview"),
                (rect.x + 24, rect.y + 112),
                color=MUTED,
            )

    def _draw_iteration_tree_disabled(self) -> None:
        """显示后台迭代状态，不进入迭代树的布局和原生绘制路径。

        worker 进度仍由 ``_drain_events`` 消费并汇总到统计卡片，确保有界
        进度队列不会因暂时关闭树 UI 而阻塞后台搜索。这里不读取实时 DAG、
        不加载 SQLite 图，也不执行节点/边 hover、布局或 interval 重算。
        """

        rect = self._graph_rect()
        self._panel(rect, t("gui.tree.rendering_disabled_title"))
        self._draw_live_stats(rect)
        self._text(
            t("gui.tree.rendering_disabled_line_1"),
            (rect.x + 24, rect.y + 116),
            size=16,
            color=ACCENT,
            max_width=rect.width - 48,
        )
        self._text(
            t("gui.tree.rendering_disabled_line_2"),
            (rect.x + 24, rect.y + 150),
            size=13,
            color=MUTED,
            max_width=rect.width - 48,
        )
        self.node_screen_positions.clear()
        self.hovered_node = None
        self.hovered_edge = None
        self.hover_tooltip = []

    def _build_args(self) -> argparse.Namespace:
        values = vars(self.parser.parse_args([])).copy()
        values.update(
            {
                "number_of_players": int(self.entries["number_of_players"].get_text()),
                "number_of_wolves": int(self.entries["number_of_wolves"].get_text()),
                "parallel_workers": 1,
                "lambda_risk": round(float(self.lambda_slider.get_current_value()), 2),
                "page_size": self._page_size(),
                "search_mode": "dfs",
                "lang": "zh-CN",
                "smart_vote": self.values["smart_vote"],
                "disable_plot": True,
                # 迭代树渲染暂时关闭；后台仍保留计数、阶段进度、检查点和结果。
                "live_preview_enabled": False,
                "tactics": ",".join(
                    key for key, selected in self.tactics.items() if selected and self.values["smart_vote"]
                ),
            }
        )
        values.update({key: self.values[key] for key in ROLE_KEYS})
        if values["number_of_players"] < 3:
            raise ValueError(t("gui.validation.players"))
        if values["number_of_wolves"] < 1:
            raise ValueError(t("gui.validation.wolves"))
        if not 0.0 <= values["lambda_risk"] <= 1.0:
            raise ValueError(t("gui.validation.lambda"))
        return argparse.Namespace(**values)

    def _worker(self, args: argparse.Namespace) -> None:
        """执行后台模拟，并把异常上下文显式传回 Pygame 主线程。"""

        try:
            args.iteration_callback = lambda event: self.events.put(("iteration", event))
            simulator = self.run_simulation(
                args,
                phase_callback=lambda phase: self.events.put(("phase", phase)),
            )
            self.events.put(("done", simulator))
        except BaseException as exc:
            error_payload = {
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
                "iteration_status": getattr(exc, "iteration_status", "failed"),
                "run_id": getattr(exc, "run_id", ""),
                "position_count": int(getattr(exc, "completed_positions", 0)),
                "total_position_count": int(getattr(exc, "total_positions", 0)),
                "next_position_index": getattr(exc, "next_position_index", None),
            }
            logger.error(
                t(
                    "log.gui.worker_failed",
                    run_id=error_payload["run_id"] or t("common.unknown"),
                    completed=error_payload["position_count"],
                    total=error_payload["total_position_count"],
                    next_position=error_payload["next_position_index"] or t("common.none"),
                    error_type=error_payload["error_type"],
                    error=error_payload["error"],
                ),
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            try:
                from ._crash_handler import record_caught_failure

                record_caught_failure(
                    exc,
                    category="gui_worker",
                    context={
                        "run_id": error_payload["run_id"] or "unknown",
                        "checkpoints": (f"{error_payload['position_count']}/{error_payload['total_position_count']}"),
                        "next_position": (error_payload["next_position_index"] or "none"),
                        "error_type": error_payload["error_type"],
                    },
                )
            except BaseException:
                logger.critical(
                    t(
                        "log.gui.crash_log_fallback_failed",
                        error_type=error_payload["error_type"],
                    ),
                    exc_info=True,
                )
            self.events.put(("error", error_payload))

    def _show_terminal_popup(
        self,
        status: str,
        result: dict[str, Any],
        *,
        error: dict[str, Any] | None = None,
    ) -> None:
        """在 Pygame 主线程显示完成、中断或失败模态弹窗。"""

        if self.terminal_popup is not None and self.terminal_popup.alive():
            self._acknowledge_previous_crash_popup()
            self.terminal_popup.kill()
        title, message = _terminal_popup_content(status, result, error=error)
        width, height = 620, 330
        screen_width, screen_height = self.screen.get_size()
        self.terminal_popup = UIMessageWindow(
            pygame.Rect(
                max(20, (screen_width - width) // 2),
                max(20, (screen_height - height) // 2),
                width,
                height,
            ),
            html_message=message,
            manager=self.manager,
            window_title=title,
            object_id=f"#terminal_{status}",
            always_on_top=True,
        )

    def _show_previous_crash_popup(self) -> None:
        """启动时补发上一次原生崩溃无法当场绘制的提示。"""

        previous = previous_unreported_crash_log()
        if previous is None:
            return
        self.pending_previous_crash_log = previous
        message = "<br>".join(
            html.escape(line)
            for line in (
                t("gui.popup.previous_crash.line_1"),
                t("gui.popup.previous_crash.line_2"),
                t("gui.popup.field.crash_log", path=previous),
                t("gui.popup.field.runtime_log", path=runtime_log_path()),
            )
        )
        width, height = 650, 300
        screen_width, screen_height = self.screen.get_size()
        self.terminal_popup = UIMessageWindow(
            pygame.Rect(
                max(20, (screen_width - width) // 2),
                max(20, (screen_height - height) // 2),
                width,
                height,
            ),
            html_message=message,
            manager=self.manager,
            window_title=t("gui.popup.previous_crash.title"),
            object_id="#terminal_previous_crash",
            always_on_top=True,
        )
        logger.warning(t("log.gui.previous_native_crash", path=previous))

    def _acknowledge_previous_crash_popup(self) -> None:
        """用户关闭历史崩溃提示后写入一次性通知标记。"""

        if self.pending_previous_crash_log is None:
            return
        try:
            mark_crash_log_reported(self.pending_previous_crash_log)
        except OSError:
            logger.exception(
                t(
                    "log.gui.crash_notification_mark_failed",
                    path=self.pending_previous_crash_log,
                )
            )
        self.pending_previous_crash_log = None

    def _start(self) -> None:
        if self.running or self.matrix_running:
            if self.matrix_running:
                self.status = t("gui.conflict.matrix_running")
            return
        if self.simulator is not None:
            # 上一次结果的只读连接在新请求前释放，避免新批次与旧连接
            # 同时持有 SQLite 文件句柄；内存中的 UI 图仍可继续被清理。
            cache = getattr(self.simulator, "signature_cache", None)
            if cache is not None:
                try:
                    cache.close()
                except Exception:
                    logger.exception(t("log.gui.solution_cache_close_failed"))
        try:
            args = self._build_args()
        except (TypeError, ValueError) as exc:
            self.status = t("gui.invalid", error=exc)
            return
        self.rows.clear()
        self.graph = {"nodes": [], "edges": []}
        self.live_graphs.clear()
        self.preview_position = 0
        self.selected_row = None
        self.page_index = 0
        self.progress = 0.0
        self.position_progress.clear()
        self.active_position = 0
        self.live_stats.update(
            terminal_count=0,
            good_paths=0,
            wolf_paths=0,
            expanded_nodes=0,
            discovered_nodes=0,
            frontier_size=0,
            edge_count=0,
            completed_positions=0,
            total_positions=0,
        )
        while True:
            try:
                self.worker_progress_queue.get_nowait()
            except queue.Empty:
                break
        while True:
            try:
                self.worker_result_queue.get_nowait()
            except queue.Empty:
                break
        self.last_data_refresh_at = 0.0
        self.resume_event.set()
        args.resume_event = self.resume_event
        args.progress_queue = self.worker_progress_queue
        args.result_queue = self.worker_result_queue
        args.force_recompute = self.force_recompute_next_run
        if args.force_recompute:
            # 本次请求已经明确是重算；无论随后完成或中断，下一次点击
            # 都重新回到 solution 优先查询，避免重复隐式重算。
            self.force_recompute_next_run = False
        self.progress_bar.set_current_progress(0.0)
        self.running = True
        self.paused = False
        self.start_button.disable()
        self.start_button.set_text(t("gui.running_short"))
        self.pause_button.set_text(t("gui.pause"))
        self.pause_button.enable()
        self.status = t("gui.starting")
        self._sync_run_control_states()
        threading.Thread(target=self._worker, args=(args,), daemon=True).start()

    def _build_matrix_process_kwargs(self) -> dict[str, Any]:
        """校验矩阵页输入，并转换为协调进程使用的具名零基参数。"""

        position_index = int(self.matrix_entries["position_index"].get_text())
        actor_seat = int(self.matrix_entries["actor_seat"].get_text())
        samples = int(self.matrix_entries["samples"].get_text())
        workers = int(self.defaults.matrix_workers)
        batch_size = int(self.defaults.matrix_batch_size)
        if not 1 <= position_index <= 1260:
            raise ValueError(t("gui.validation.position"))
        if not 1 <= actor_seat <= 7:
            raise ValueError(t("gui.validation.actor"))
        if samples <= 0:
            raise ValueError(t("gui.validation.samples"))
        if not 1 <= workers <= 16:
            raise ValueError(t("help.matrix_workers"))
        if batch_size <= 0:
            raise ValueError(t("help.matrix_batch_size"))
        return {
            "database_path": self.matrix_database_path,
            "actor_id": actor_seat - 1,
            "position_index": position_index,
            "workers": workers,
            "batch_size": batch_size,
            "samples_per_cell": samples,
            "force_recompute": self.matrix_force_recompute,
            "memory_reserve_gib": float(self.defaults.memory_reserve_gib),
            "memory_reserve_ratio": float(self.defaults.memory_reserve_ratio),
        }

    def _start_matrix(self) -> None:
        """创建独立矩阵协调进程；Pygame 主线程不执行 Monte Carlo。"""

        if self.running or self.matrix_running:
            self.matrix_status = t("gui.matrix.tree_running") if self.running else t("gui.matrix.already_running")
            return
        try:
            process_kwargs = self._build_matrix_process_kwargs()
        except (TypeError, ValueError) as exc:
            self.matrix_status = t("gui.matrix.invalid", error=exc)
            return
        if self.matrix_process is not None and not self.matrix_process.is_alive():
            self.matrix_process.join(timeout=0.1)
            try:
                self.matrix_process.close()
            except (OSError, ValueError):
                pass
        try:
            from ._decision_matrix_gui_runner import run_matrix_gui_process

            self.matrix_output_queue = self.matrix_context.Queue(maxsize=64)
            self.matrix_stop_event = self.matrix_context.Event()
            self.matrix_process = self.matrix_context.Process(
                target=run_matrix_gui_process,
                kwargs={
                    "output_queue": self.matrix_output_queue,
                    "stop_event": self.matrix_stop_event,
                    **process_kwargs,
                },
                name="speech-value-gui-coordinator",
                daemon=False,
            )
            self.matrix_process.start()
        except BaseException as exc:
            self.matrix_running = False
            self.matrix_status = t(
                "gui.matrix.start_failed",
                error_type=type(exc).__name__,
                error=exc,
            )
            try:
                from ._crash_handler import record_caught_failure

                record_caught_failure(
                    exc,
                    category="gui_matrix_start",
                    context={"error_type": type(exc).__name__},
                )
            except BaseException:
                logger.critical(
                    t("log.gui.matrix_start_crash_log_failed"),
                    exc_info=True,
                )
            return
        samples = int(process_kwargs["samples_per_cell"])
        batch_size = int(process_kwargs["batch_size"])
        self.matrix_total_batches = 3 * math.ceil(samples / batch_size)
        self.matrix_committed_batches = 0
        self.matrix_id = ""
        self.matrix_cache_hit = False
        self.matrix_rows.clear()
        self.matrix_result = None
        self.matrix_page_index = 0
        self.matrix_selected_row = None
        self.matrix_running = True
        self.matrix_stop_requested = False
        self.matrix_terminal_seen = False
        self.matrix_exit_seen_at = None
        self.matrix_progress_bar.set_current_progress(0.0)
        self.matrix_start_button.set_text(t("gui.matrix.running"))
        self.matrix_stop_button.set_text(t("gui.matrix.stop"))
        self.matrix_status = t("gui.matrix.starting")
        self._sync_run_control_states()

    def _request_matrix_stop(self) -> None:
        """请求在批次边界停止派发，并保留已经提交的恢复检查点。"""

        if not self.matrix_running or self.matrix_stop_event is None:
            return
        self.matrix_stop_event.set()
        self.matrix_stop_requested = True
        self.matrix_stop_button.set_text(t("gui.matrix.stopping"))
        self.matrix_stop_button.disable()
        self.matrix_status = t("gui.matrix.stop_requested")

    def _show_matrix_terminal_popup(
        self,
        status: str,
        payload: dict[str, Any],
    ) -> None:
        """在 Pygame 主进程显示矩阵三种互斥终态。"""

        if self.terminal_popup is not None and self.terminal_popup.alive():
            self._acknowledge_previous_crash_popup()
            self.terminal_popup.kill()
        title, message = _matrix_terminal_popup_content(status, payload)
        width, height = 650, 320
        screen_width, screen_height = self.screen.get_size()
        self.terminal_popup = UIMessageWindow(
            pygame.Rect(
                max(20, (screen_width - width) // 2),
                max(20, (screen_height - height) // 2),
                width,
                height,
            ),
            html_message=message,
            manager=self.manager,
            window_title=title,
            object_id=f"#matrix_terminal_{status}",
            always_on_top=True,
        )

    def _apply_matrix_message(self, payload: dict[str, Any]) -> None:
        """把协调进程消息归并为 GUI 状态；不读取隐藏世界或轨迹。"""

        kind = str(payload.get("kind") or "")
        if payload.get("matrix_id"):
            self.matrix_id = str(payload["matrix_id"])
        self.matrix_committed_batches = int(payload.get("committed_batches", self.matrix_committed_batches))
        self.matrix_total_batches = int(payload.get("total_batches", self.matrix_total_batches))
        self.matrix_cache_hit = bool(payload.get("cache_hit", self.matrix_cache_hit))
        progress = 100.0 * self.matrix_committed_batches / max(1, self.matrix_total_batches)
        self.matrix_progress_bar.set_current_progress(progress)
        if kind == "matrix_starting":
            self.matrix_status = t("gui.matrix.worker_started")
            return
        if kind == "matrix_progress":
            if payload.get("status") == "complete":
                self.matrix_status = t("gui.matrix.cache_loading")
            elif payload.get("resumed"):
                self.matrix_status = t(
                    "gui.matrix.resumed",
                    completed=self.matrix_committed_batches,
                    total=self.matrix_total_batches,
                )
            else:
                self.matrix_status = t(
                    "gui.matrix.progress_status",
                    completed=self.matrix_committed_batches,
                    total=self.matrix_total_batches,
                )
            return
        if kind == "matrix_done":
            self.matrix_result = dict(payload.get("result") or {})
            self.matrix_rows = sorted(
                [dict(row) for row in self.matrix_result.get("action_rows", [])],
                key=_matrix_row_sort_key,
            )
            self.matrix_selected_row = 0 if self.matrix_rows else None
            self.matrix_page_index = 0
            self.matrix_committed_batches = self.matrix_total_batches
            self.matrix_progress_bar.set_current_progress(100.0)
            self.matrix_status = (
                t("gui.matrix.loaded", rows=len(self.matrix_rows))
                if self.matrix_cache_hit
                else t("gui.matrix.completed", rows=len(self.matrix_rows))
            )
            self.matrix_terminal_seen = True
            self.matrix_running = False
            self.matrix_stop_requested = False
            self.matrix_start_button.set_text(t("gui.matrix.start"))
            self.matrix_stop_button.set_text(t("gui.matrix.stop"))
            self._sync_run_control_states()
            self._show_matrix_terminal_popup("complete", payload)
            return
        if kind == "matrix_interrupted":
            self.matrix_status = t(
                "gui.matrix.interrupted",
                completed=self.matrix_committed_batches,
                total=self.matrix_total_batches,
            )
            self.matrix_terminal_seen = True
            self.matrix_running = False
            self.matrix_stop_requested = False
            self.matrix_start_button.set_text(t("gui.matrix.continue"))
            self.matrix_stop_button.set_text(t("gui.matrix.stop"))
            self._sync_run_control_states()
            self._show_matrix_terminal_popup("interrupted", payload)
            return
        if kind == "matrix_failed":
            self.matrix_status = t(
                "gui.matrix.failed",
                error_type=payload.get("error_type", "UnknownError"),
                error=payload.get("error", t("common.unavailable")),
            )
            self.matrix_terminal_seen = True
            self.matrix_running = False
            self.matrix_stop_requested = False
            self.matrix_start_button.set_text(t("gui.matrix.restart"))
            self.matrix_stop_button.set_text(t("gui.matrix.stop"))
            self._sync_run_control_states()
            self._show_matrix_terminal_popup("failed", payload)

    def _matrix_process_failed_without_message(self) -> None:
        """把协调进程无终态退出转换为可见失败并写入 crash 证据。"""

        exitcode = None if self.matrix_process is None else self.matrix_process.exitcode
        exc = RuntimeError(t("gui.error.matrix_process_exit", exitcode=exitcode))
        payload = {
            "kind": "matrix_failed",
            "status": "failed",
            "matrix_id": self.matrix_id,
            "committed_batches": self.matrix_committed_batches,
            "total_batches": self.matrix_total_batches,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "runtime_log": str(runtime_log_path()),
            "crash_log": str(crash_log_path()),
        }
        try:
            from ._crash_handler import record_caught_failure

            record_caught_failure(
                exc,
                category="gui_matrix_process_exit",
                context={
                    "matrix_id": self.matrix_id or "unknown",
                    "batches": f"{self.matrix_committed_batches}/{self.matrix_total_batches}",
                    "exitcode": exitcode,
                },
            )
        except BaseException:
            logger.critical(t("log.gui.matrix_exit_crash_log_failed"), exc_info=True)
        self._apply_matrix_message(payload)

    def _drain_matrix_messages(self) -> None:
        """每 0.5 秒批量消费有界队列，并检测无终态的子进程退出。"""

        if self.matrix_output_queue is not None:
            for _ in range(256):
                try:
                    self._apply_matrix_message(self.matrix_output_queue.get_nowait())
                except queue.Empty:
                    break
        process = self.matrix_process
        if not self.matrix_running or process is None or process.is_alive():
            return
        if self.matrix_terminal_seen:
            return
        now = time.monotonic()
        if self.matrix_exit_seen_at is None:
            self.matrix_exit_seen_at = now
            return
        if now - self.matrix_exit_seen_at >= 1.0:
            self._matrix_process_failed_without_message()

    def _shutdown_matrix_process(self) -> None:
        """关闭窗口时优先形成可恢复中断，超时才终止孤立协调器。"""

        process = self.matrix_process
        if process is None:
            return
        if process.is_alive():
            if self.matrix_stop_event is not None:
                self.matrix_stop_event.set()
            process.join(timeout=5.0)
        if process.is_alive():
            logger.critical(
                t(
                    "log.gui.matrix_force_terminate",
                    matrix_id=self.matrix_id or t("common.unknown"),
                    completed=self.matrix_committed_batches,
                    total=self.matrix_total_batches,
                )
            )
            process.terminate()
            process.join(timeout=5.0)
        try:
            process.close()
        except (OSError, ValueError):
            pass

    def _load_selected_graph(self) -> None:
        if not ENABLE_ITERATION_TREE_RENDERING:
            return
        if self.selected_row is None or self.simulator is None:
            return
        row = self.rows[self.selected_row]
        cache = self.simulator.signature_cache
        if cache is None:
            return
        self.graph = cache.get_position_graph(
            self.simulator.run_id,
            row["position_signature"],
        )
        self.graph["roles"] = list(row["roles"])
        self.graph["position_signature"] = str(row["position_signature"])
        self.graph["position_display"] = " | ".join(f"{index + 1}:{role}" for index, role in enumerate(row["roles"]))
        self.graph["_expanded_node_ids"] = set()
        persisted_lambda = float(
            row.get(
                "interval_lambda",
                getattr(self.simulator, "lambda_risk", self.last_interval_lambda),
            )
        )
        self.last_interval_lambda = round(persisted_lambda, 2)
        current_lambda = round(float(self.lambda_slider.get_current_value()), 2)
        if current_lambda != self.last_interval_lambda:
            self._recompute_loaded_graph()
        self.selected_node = 0 if self.graph.get("nodes") else None
        self.graph_pan = [0.0, 0.0]
        self.graph_zoom = 0.78

    def _recompute_loaded_graph(self) -> None:
        """请求在后台重算选中 DAG，避免阻塞 Pygame 绘制循环。"""

        if not self.graph.get("nodes"):
            return
        current_lambda = round(float(self.lambda_slider.get_current_value()), 2)
        self.interval_recompute_requested_lambda = current_lambda
        if self.interval_recompute_running:
            return

        graph = self.graph
        graph_identity = id(graph)
        selected_position = int(self.rows[self.selected_row]["position_index"]) if self.selected_row is not None else 0
        self.interval_recompute_requested_lambda = None
        self.interval_recompute_running = True

        def worker() -> None:
            last_emitted_at = 0.0

            def publish(stage: str, completed: int, total: int) -> None:
                nonlocal last_emitted_at
                now = time.monotonic()
                if completed not in {0, total} and now - last_emitted_at < UI_DATA_REFRESH_SECONDS:
                    return
                self.events.put(
                    (
                        "local_interval_progress",
                        {
                            "graph_identity": graph_identity,
                            "position_index": selected_position,
                            "lambda_risk": current_lambda,
                            "postprocess_stage": stage,
                            "postprocess_completed": completed,
                            "postprocess_total": total,
                        },
                    )
                )
                last_emitted_at = now

            try:
                robust = recompute_graph_intervals(
                    graph,
                    lambda_risk=current_lambda,
                    progress_callback=publish,
                )
                self.events.put(
                    (
                        "local_interval_done",
                        {
                            "graph_identity": graph_identity,
                            "position_index": selected_position,
                            "lambda_risk": current_lambda,
                            "wide_interval": robust.wide.to_list(),
                            "narrow_interval": robust.narrow.to_list(),
                            "camp": interval_camp(robust.wide),
                        },
                    )
                )
            except BaseException as exc:
                logger.error(
                    t(
                        "log.gui.local_interval_failed",
                        position=selected_position,
                        lambda_value=current_lambda,
                        error_type=type(exc).__name__,
                        error=exc,
                    ),
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                self.events.put(
                    (
                        "local_interval_error",
                        {
                            "graph_identity": graph_identity,
                            "position_index": selected_position,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        },
                    )
                )

        threading.Thread(
            target=worker,
            name="interval-recompute",
            daemon=True,
        ).start()

    def _display_graph(self) -> dict[str, Any]:
        if self.running and self.preview_position in self.live_graphs:
            return self.live_graphs[self.preview_position]
        return self.graph

    @staticmethod
    def _graph_structure(graph: dict[str, Any]) -> dict[str, Any]:
        nodes = graph.get("nodes", [])
        edges = graph.get("edges", [])
        revision = (
            len(nodes),
            len(edges),
            int(graph.get("_view_revision", 0)),
        )
        cached = graph.get("_view_structure")
        if isinstance(cached, dict) and cached.get("revision") == revision:
            return cached
        node_by_id = {int(node["node_id"]): node for node in nodes}
        incoming = dict.fromkeys(node_by_id, 0)
        children: dict[int, set[int]] = {}
        edges_by_parent: dict[int, list[dict[str, Any]]] = {}
        edges_by_child: dict[int, list[dict[str, Any]]] = {}
        for edge in edges:
            parent_id = int(edge["parent_id"])
            child_id = int(edge["child_id"])
            if parent_id not in node_by_id or child_id not in node_by_id:
                continue
            children.setdefault(parent_id, set()).add(child_id)
            edges_by_parent.setdefault(parent_id, []).append(edge)
            edges_by_child.setdefault(child_id, []).append(edge)
            incoming[child_id] += 1
        entry_nodes = sorted(node_id for node_id, incoming_count in incoming.items() if incoming_count == 0)
        if not entry_nodes and node_by_id:
            entry_nodes = [min(node_by_id)]
        cached = {
            "revision": revision,
            "node_by_id": node_by_id,
            "children": children,
            "edges_by_parent": edges_by_parent,
            "edges_by_child": edges_by_child,
            "entry_nodes": entry_nodes,
        }
        graph["_view_structure"] = cached
        return cached

    @classmethod
    def _visible_graph(cls, graph: dict[str, Any]) -> dict[str, Any]:
        structure = cls._graph_structure(graph)
        node_by_id = structure["node_by_id"]
        if not node_by_id:
            return {**graph, "nodes": [], "edges": []}
        expanded = graph.setdefault("_expanded_node_ids", set())
        visible_ids = set(structure["entry_nodes"][:DAG_VISIBLE_NODE_LIMIT])
        pending = list(visible_ids)
        while pending and len(visible_ids) < DAG_VISIBLE_NODE_LIMIT:
            parent_id = pending.pop()
            if parent_id not in expanded:
                continue
            for child_id in sorted(structure["children"].get(parent_id, set())):
                if child_id not in visible_ids:
                    visible_ids.add(child_id)
                    pending.append(child_id)
                    if len(visible_ids) >= DAG_VISIBLE_NODE_LIMIT:
                        break
        visible_edges = []
        for parent_id in visible_ids:
            visible_edges.extend(
                edge for edge in structure["edges_by_parent"].get(parent_id, []) if int(edge["child_id"]) in visible_ids
            )
        return {
            **graph,
            "nodes": [node_by_id[node_id] for node_id in sorted(visible_ids)],
            "edges": visible_edges,
        }

    @staticmethod
    def _mark_newly_visible_nodes(
        graph: dict[str, Any],
        previous_ids: set[int],
    ) -> None:
        now = time.monotonic()
        visible_ids = {int(node["node_id"]) for node in PygameSimulatorUI._visible_graph(graph).get("nodes", [])}
        for node in graph.get("nodes", []):
            if int(node["node_id"]) in visible_ids - previous_ids:
                node["_visible_since"] = now

    def _toggle_node_children(self, node_id: int) -> None:
        graph = self._display_graph()
        previous_ids = {int(node["node_id"]) for node in self._visible_graph(graph).get("nodes", [])}
        expanded = graph.setdefault("_expanded_node_ids", set())
        if node_id in expanded:
            expanded.remove(node_id)
        else:
            expanded.add(node_id)
        self._mark_newly_visible_nodes(graph, previous_ids)
        self.selected_node = node_id

    def _set_all_nodes_expanded(self, *, expanded: bool) -> None:
        graph = self._display_graph()
        previous_ids = {int(node["node_id"]) for node in self._visible_graph(graph).get("nodes", [])}
        expanded_nodes = graph.setdefault("_expanded_node_ids", set())
        if expanded:
            structure = self._graph_structure(graph)
            reachable = set(structure["entry_nodes"][:DAG_VISIBLE_NODE_LIMIT])
            pending = list(reachable)
            while pending and len(reachable) < DAG_VISIBLE_NODE_LIMIT:
                parent_id = pending.pop(0)
                expanded_nodes.add(parent_id)
                for child_id in sorted(structure["children"].get(parent_id, set())):
                    if child_id not in reachable:
                        reachable.add(child_id)
                        pending.append(child_id)
                        if len(reachable) >= DAG_VISIBLE_NODE_LIMIT:
                            break
        else:
            expanded_nodes.clear()
        self._mark_newly_visible_nodes(graph, previous_ids)
        visible_nodes = self._visible_graph(graph).get("nodes", [])
        self.selected_node = int(visible_nodes[0]["node_id"]) if visible_nodes else None

    def _locate_root(self) -> None:
        """把当前 DAG 的根节点移回缩略图左侧可视区域。

        该操作只调整本地画布的平移和选中状态，不改变节点展开集合、
        搜索 frontier 或 SQLite 图数据。若图存在多个入口，使用稳定的
        最小入口节点作为根定位目标。
        """

        graph = self._display_graph()
        entry_nodes = self._graph_structure(graph)["entry_nodes"]
        if not entry_nodes:
            self.status = t("gui.tree.no_root")
            self.selected_node = None
            return
        root_id = int(entry_nodes[0])
        visible_graph = self._visible_graph(graph)
        canvas_position = self._layout_graph(visible_graph).get(root_id, (0.0, 0.0))
        self.graph_pan[0] = -canvas_position[0] * self.graph_zoom
        self.graph_pan[1] = -canvas_position[1] * self.graph_zoom
        self.selected_node = root_id
        self.status = t("gui.root_located", node=root_id)

    def _upsert_progress_row(self, payload: dict[str, Any]) -> None:
        position_index = int(payload["position_index"])
        current_row = next(
            (row for row in self.rows if int(row["position_index"]) == position_index),
            None,
        )
        if current_row is not None and not current_row.get("processing", False):
            return
        row = current_row or {
            "position_index": position_index,
            "position_signature": str(payload.get("position_signature", "")),
            "roles": list(payload.get("roles", [])),
            "position_display": str(payload.get("position_display", "")),
            "good_paths": 0,
            "wolf_paths": 0,
            "wide_interval": [-1.0, 1.0],
            "narrow_interval": [-1.0, 1.0],
            "camp": "balanced",
            "processing": True,
        }
        row.update(
            state_count=int(payload.get("discovered_states", 0)),
            edge_count=int(payload.get("edge_count", 0)),
            terminal_count=int(payload.get("terminal_count", 0)),
            processed_states=int(payload.get("processed_states", 0)),
            runtime_seconds=float(payload.get("runtime_seconds", 0.0)),
            processing_phase=str(payload.get("kind", row.get("processing_phase", "node_progress"))),
            postprocess_stage=str(
                payload.get(
                    "postprocess_stage",
                    row.get("postprocess_stage", ""),
                )
            ),
            postprocess_completed=int(
                payload.get(
                    "postprocess_completed",
                    row.get("postprocess_completed", 0),
                )
            ),
            postprocess_total=int(
                payload.get(
                    "postprocess_total",
                    row.get("postprocess_total", 0),
                )
            ),
            processing=True,
        )
        if current_row is None:
            self.rows.append(row)

    def _upsert_result_row(self, result: dict[str, Any]) -> None:
        position_index = int(result["position_index"])
        completed = {**result, "processing": False}
        for row_index, row in enumerate(self.rows):
            if int(row["position_index"]) == position_index:
                self.rows[row_index] = completed
                return
        self.rows.append(completed)

    def _merge_live_preview(self, payload: dict[str, Any]) -> None:
        position_index = int(payload["position_index"])
        graph = self.live_graphs.setdefault(
            position_index,
            {
                "nodes": [],
                "edges": [],
                "roles": list(payload.get("roles", [])),
                "position_display": str(payload.get("position_display", "")),
                "focus_node_id": 0,
                "_expanded_node_ids": set(),
            },
        )
        previous_visible_ids = {int(node["node_id"]) for node in self._visible_graph(graph).get("nodes", [])}
        graph["roles"] = list(payload.get("roles", graph.get("roles", [])))
        graph["position_display"] = str(payload.get("position_display", graph.get("position_display", "")))
        graph["focus_node_id"] = int(payload.get("focus_node_id", graph.get("focus_node_id", 0)))

        incoming_nodes = [dict(node) for node in payload.get("preview_nodes", [])]
        incoming_edges = [dict(edge) for edge in payload.get("preview_edges", [])]
        node_by_id = {int(node["node_id"]): node for node in graph["nodes"]}
        for node in incoming_nodes:
            node_id = int(node["node_id"])
            existing = node_by_id.get(node_id)
            if existing is not None and "_visible_since" in existing:
                node["_visible_since"] = existing["_visible_since"]
            node_by_id[node_id] = node

        edge_by_key = {(int(edge["parent_id"]), int(edge["child_id"])): edge for edge in graph["edges"]}
        for edge in incoming_edges:
            edge_by_key[(int(edge["parent_id"]), int(edge["child_id"]))] = edge

        keep_ids: set[int] = set()

        def keep_node(node_id: int) -> None:
            if node_id in node_by_id and len(keep_ids) < LIVE_PREVIEW_NODE_LIMIT:
                keep_ids.add(node_id)

        if node_by_id:
            keep_node(min(node_by_id))
        keep_node(int(graph["focus_node_id"]))
        priority_edges = [*reversed(incoming_edges), *reversed(graph["edges"])]
        for edge in priority_edges:
            endpoints = {int(edge["parent_id"]), int(edge["child_id"])}
            missing = endpoints - keep_ids
            if len(keep_ids) + len(missing) <= LIVE_PREVIEW_NODE_LIMIT:
                for node_id in endpoints:
                    keep_node(node_id)
        for node in reversed(incoming_nodes):
            keep_node(int(node["node_id"]))
        for node_id in sorted(node_by_id, reverse=True):
            keep_node(node_id)

        graph["nodes"] = [node_by_id[node_id] for node_id in sorted(keep_ids)]
        graph["edges"] = [
            edge
            for (parent_id, child_id), edge in edge_by_key.items()
            if parent_id in keep_ids and child_id in keep_ids
        ]
        graph["_view_revision"] = int(graph.get("_view_revision", 0)) + 1
        graph.pop("_view_structure", None)
        graph.setdefault("_expanded_node_ids", set()).intersection_update(keep_ids)
        self._mark_newly_visible_nodes(graph, previous_visible_ids)
        focus_node_id = int(graph["focus_node_id"])
        if focus_node_id in keep_ids:
            self.selected_node = focus_node_id

    def _choose_preview_position(self) -> None:
        active_positions = sorted(
            position_index
            for position_index, snapshot in self.position_progress.items()
            if not snapshot.get("completed", False) and position_index in self.live_graphs
        )
        if self.preview_position not in active_positions and active_positions:
            self.preview_position = active_positions[0]
            graph = self.live_graphs[self.preview_position]
            self.selected_node = int(graph.get("focus_node_id", 0))

    def _sync_live_node_totals(self) -> None:
        snapshots = tuple(self.position_progress.values())
        self.live_stats["expanded_nodes"] = sum(int(item.get("processed_states", 0)) for item in snapshots)
        self.live_stats["discovered_nodes"] = sum(int(item.get("discovered_states", 0)) for item in snapshots)
        self.live_stats["frontier_size"] = sum(int(item.get("frontier_size", 0)) for item in snapshots)
        self.live_stats["edge_count"] = sum(int(item.get("edge_count", 0)) for item in snapshots)
        self.live_stats["terminal_count"] = sum(int(item.get("terminal_count", 0)) for item in snapshots)

    def _apply_iteration_event(self, payload: dict[str, Any]) -> None:
        event_kind = str(payload.get("kind", ""))
        position_index = int(payload.get("position_index", 0))
        if event_kind in {
            "position_started",
            "node_progress",
            "path_progress",
            "interval_progress",
        }:
            current = self.position_progress.get(position_index, {})
            if current.get("completed"):
                return
            if int(payload.get("processed_states", 0)) < int(current.get("processed_states", 0)):
                return
            self.position_progress[position_index] = {
                **current,
                **payload,
                "completed": False,
            }
            self._upsert_progress_row(payload)
            if ENABLE_ITERATION_TREE_RENDERING and event_kind in {
                "position_started",
                "node_progress",
            }:
                self._merge_live_preview(payload)
            if ENABLE_ITERATION_TREE_RENDERING:
                self._choose_preview_position()
            self.active_position = position_index
            self.live_stats["total_positions"] = int(payload.get("total_positions", self.live_stats["total_positions"]))
            self._sync_live_node_totals()
            if self.paused:
                self.status = t(
                    "gui.paused",
                    expanded=_compact_integer(int(self.live_stats["expanded_nodes"])),
                )
            elif event_kind == "path_progress":
                self.status = t(
                    "gui.path_progress",
                    position=position_index,
                    completed=_compact_integer(int(payload.get("postprocess_completed", 0))),
                    total=_compact_integer(int(payload.get("postprocess_total", 0))),
                )
            elif event_kind == "interval_progress":
                stage = str(payload.get("postprocess_stage", "node_intervals"))
                self.status = t(
                    "gui.interval_progress",
                    position=position_index,
                    stage=t(f"postprocess.{stage}"),
                    completed=_compact_integer(int(payload.get("postprocess_completed", 0))),
                    total=_compact_integer(int(payload.get("postprocess_total", 0))),
                )
            else:
                self.status = t(
                    "gui.node_progress",
                    position=position_index,
                    expanded=_compact_integer(int(self.live_stats["expanded_nodes"])),
                    discovered=_compact_integer(int(self.live_stats["discovered_nodes"])),
                )
            return
        if event_kind != "position_result":
            return

        self._upsert_result_row(payload)
        done = int(payload["completed_positions"])
        total = int(payload["total_positions"])
        self.position_progress[position_index] = {
            "processed_states": int(payload["processed_states"]),
            "discovered_states": int(payload["state_count"]),
            "frontier_size": 0,
            "edge_count": int(payload["edge_count"]),
            "terminal_count": int(payload["terminal_count"]),
            "completed": True,
        }
        if ENABLE_ITERATION_TREE_RENDERING:
            self._choose_preview_position()
        self._sync_live_node_totals()
        self.progress = done * 100.0 / max(1, total)
        self.progress_bar.set_current_progress(self.progress)
        if not self.paused:
            self.status = t("gui.running", done=done, total=total)
        self.live_stats["good_paths"] = sum(int(row["good_paths"]) for row in self.rows)
        self.live_stats["wolf_paths"] = sum(int(row["wolf_paths"]) for row in self.rows)
        self.live_stats["completed_positions"] = done
        self.live_stats["total_positions"] = total

    def _toggle_pause(self) -> None:
        if not self.running:
            return
        if self.paused:
            self.resume_event.set()
            self.paused = False
            self.pause_button.set_text(t("gui.pause"))
            self.start_button.set_text(t("gui.running_short"))
            active = self.position_progress.get(self.active_position, {})
            active_kind = str(active.get("kind", "node_progress"))
            if active_kind == "path_progress":
                self.status = t(
                    "gui.path_progress",
                    position=self.active_position or "?",
                    completed=_compact_integer(int(active.get("postprocess_completed", 0))),
                    total=_compact_integer(int(active.get("postprocess_total", 0))),
                )
            elif active_kind == "interval_progress":
                stage = str(active.get("postprocess_stage", "node_intervals"))
                self.status = t(
                    "gui.interval_progress",
                    position=self.active_position or "?",
                    stage=t(f"postprocess.{stage}"),
                    completed=_compact_integer(int(active.get("postprocess_completed", 0))),
                    total=_compact_integer(int(active.get("postprocess_total", 0))),
                )
            else:
                self.status = t(
                    "gui.node_progress",
                    position=self.active_position or "?",
                    expanded=_compact_integer(int(self.live_stats["expanded_nodes"])),
                    discovered=_compact_integer(int(self.live_stats["discovered_nodes"])),
                )
            return
        self.resume_event.clear()
        self.paused = True
        self.pause_button.set_text(t("gui.resume"))
        self.start_button.set_text(t("gui.paused_short"))
        self.status = t(
            "gui.paused",
            expanded=_compact_integer(int(self.live_stats["expanded_nodes"])),
        )

    def _drain_events(self) -> None:
        now = time.monotonic()
        if now - self.last_data_refresh_at >= UI_DATA_REFRESH_SECONDS:
            for _ in range(512):
                try:
                    self._apply_iteration_event(self.worker_progress_queue.get_nowait())
                except queue.Empty:
                    break
            self._drain_matrix_messages()
            self.last_data_refresh_at = now
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "iteration":
                    self._apply_iteration_event(payload)
                elif kind == "local_interval_progress":
                    if int(payload["graph_identity"]) != id(self.graph):
                        continue
                    stage = str(payload.get("postprocess_stage", "node_intervals"))
                    completed = int(payload.get("postprocess_completed", 0))
                    total = int(payload.get("postprocess_total", 0))
                    position_index = int(payload.get("position_index", 0))
                    for row in self.rows:
                        if int(row["position_index"]) == position_index:
                            row["interval_recomputing"] = True
                            row["processing_phase"] = "interval_progress"
                            row["postprocess_stage"] = stage
                            row["postprocess_completed"] = completed
                            row["postprocess_total"] = total
                            break
                    self.status = t(
                        "gui.interval_progress",
                        position=position_index,
                        stage=t(f"postprocess.{stage}"),
                        completed=_compact_integer(completed),
                        total=_compact_integer(total),
                    )
                elif kind == "local_interval_done":
                    graph_matches = int(payload["graph_identity"]) == id(self.graph)
                    completed_lambda = round(float(payload["lambda_risk"]), 2)
                    position_index = int(payload.get("position_index", 0))
                    if graph_matches:
                        self.last_interval_lambda = completed_lambda
                        for row in self.rows:
                            if int(row["position_index"]) != position_index:
                                continue
                            row["wide_interval"] = list(payload["wide_interval"])
                            row["narrow_interval"] = list(payload["narrow_interval"])
                            row["camp"] = str(payload["camp"])
                            row["interval_lambda"] = completed_lambda
                            row["interval_recomputing"] = False
                            break
                        self.status = t(
                            "gui.tree.interval_updated",
                            value=completed_lambda,
                        )
                    requested_lambda = self.interval_recompute_requested_lambda
                    self.interval_recompute_requested_lambda = None
                    self.interval_recompute_running = False
                    if self.graph.get("nodes") and (
                        not graph_matches
                        or (requested_lambda is not None and round(float(requested_lambda), 2) != completed_lambda)
                    ):
                        self._recompute_loaded_graph()
                elif kind == "local_interval_error":
                    graph_matches = int(payload["graph_identity"]) == id(self.graph)
                    if graph_matches:
                        position_index = int(payload.get("position_index", 0))
                        for row in self.rows:
                            if int(row["position_index"]) == position_index:
                                row["interval_recomputing"] = False
                        self.last_interval_lambda = round(
                            float(self.lambda_slider.get_current_value()),
                            2,
                        )
                        self.status = t(
                            "gui.tree.interval_failed",
                            error_type=payload["error_type"],
                            error=payload["error"],
                        )
                    self.interval_recompute_requested_lambda = None
                    self.interval_recompute_running = False
                elif kind == "done":
                    self.simulator = payload
                    result = self.simulator.last_result or {}
                    for result_row in result.get("positions", []):
                        self._upsert_result_row(result_row)
                    for row in self.rows:
                        index = int(row["position_index"])
                        self.position_progress[index] = {
                            "processed_states": int(row["processed_states"]),
                            "discovered_states": int(row["state_count"]),
                            "frontier_size": 0,
                            "edge_count": int(row["edge_count"]),
                            "terminal_count": int(row["terminal_count"]),
                            "completed": True,
                        }
                    self._sync_live_node_totals()
                    self.live_stats["good_paths"] = sum(int(row["good_paths"]) for row in self.rows)
                    self.live_stats["wolf_paths"] = sum(int(row["wolf_paths"]) for row in self.rows)
                    completed = int(result.get("position_count", 0))
                    total = int(result.get("total_position_count", completed or 1))
                    self.progress = completed * 100.0 / max(1, total)
                    self.progress_bar.set_current_progress(self.progress)
                    if result.get("loaded_solution"):
                        self.status = t(
                            "gui.solution_loaded",
                            positions=completed,
                            states=result.get("processed_states", 0),
                        )
                        self.force_recompute_next_run = True
                        self.start_button.enable()
                        self.start_button.set_text(t("gui.recompute"))
                    elif result.get("status") == "interrupted":
                        self.status = t(
                            "gui.interrupted",
                            done=completed,
                            total=total,
                        )
                        self._show_terminal_popup("interrupted", result)
                    else:
                        self.status = t(
                            "gui.finished",
                            positions=completed,
                            states=result.get(
                                "processed_states",
                                result.get("state_count", 0),
                            ),
                        )
                        self._show_terminal_popup("complete", result)
                    self.running = False
                    self.paused = False
                    self.resume_event.set()
                    self.start_button.enable()
                    if not result.get("loaded_solution"):
                        self.start_button.set_text(t("gui.start"))
                    self.pause_button.set_text(t("gui.pause"))
                    self.pause_button.disable()
                    self._sync_run_control_states()
                elif kind == "error":
                    self.running = False
                    self.paused = False
                    self.resume_event.set()
                    self.start_button.enable()
                    self.start_button.set_text(t("gui.start"))
                    self.pause_button.set_text(t("gui.pause"))
                    self.pause_button.disable()
                    self.status = t(
                        "gui.failed",
                        error=f"{payload['error_type']}: {payload['error']}",
                    )
                    self._sync_run_control_states()
                    self._show_terminal_popup("failed", payload, error=payload)
        except queue.Empty:
            pass

    def _handle_custom_click(self, position: tuple[int, int]) -> None:
        if self.active_page == "matrix":
            if self.matrix_force_rect.collidepoint(position) and not self.matrix_running:
                self.matrix_force_recompute = not self.matrix_force_recompute
                return
            for rect, row_index in self.matrix_table_row_rects:
                if rect.collidepoint(position):
                    self.matrix_selected_row = row_index
                    return
            return
        for key, rect in self.toggle_rects.items():
            if rect.collidepoint(position):
                self.values[key] = not self.values[key]
                if key in {"include_witch", "include_guard"}:
                    self._sync_night_tactics()
                return
        if self.values["smart_vote"]:
            if self.header_rects.get("day", pygame.Rect(0, 0, 0, 0)).collidepoint(position):
                self.day_expanded = not self.day_expanded
                return
            if self.header_rects.get("night", pygame.Rect(0, 0, 0, 0)).collidepoint(position):
                self.night_expanded = not self.night_expanded
                return
            for key, rect in self.tactic_rects.items():
                if rect.collidepoint(position):
                    if key in NIGHT_TACTICS and not (self.values["include_witch"] or self.values["include_guard"]):
                        return
                    self.tactics[key] = not self.tactics[key]
                    return
        for rect, row_index in self.table_row_rects:
            if rect.collidepoint(position):
                self.selected_row = row_index
                if ENABLE_ITERATION_TREE_RENDERING:
                    self._load_selected_graph()
                return
        if not ENABLE_ITERATION_TREE_RENDERING:
            return
        graph_rect = self._graph_rect()
        if graph_rect.collidepoint(position):
            nearest = self._node_at(position)
            if nearest is not None:
                self._toggle_node_children(nearest)

    def _node_at(self, position: tuple[int, int]) -> int | None:
        nearest = None
        nearest_distance = 18.0
        for node_id, point in self.node_screen_positions.items():
            distance = math.dist(position, point)
            if distance < nearest_distance:
                nearest = node_id
                nearest_distance = distance
        return nearest

    def _handle_event(self, event: pygame.event.Event) -> None:
        if event.type == pygame.QUIT:
            self.resume_event.set()
            if self.matrix_stop_event is not None:
                self.matrix_stop_event.set()
            self.keep_running = False
        elif event.type == pygame_gui.UI_WINDOW_CLOSE:
            if event.ui_element == self.terminal_popup:
                self._acknowledge_previous_crash_popup()
                self.terminal_popup = None
        elif event.type == pygame_gui.UI_BUTTON_PRESSED:
            if event.ui_element == self.tree_page_button:
                self._switch_page("tree")
            elif event.ui_element == self.matrix_page_button:
                self._switch_page("matrix")
            elif event.ui_element == self.matrix_start_button:
                self._start_matrix()
            elif event.ui_element == self.matrix_stop_button:
                self._request_matrix_stop()
            elif event.ui_element == self.matrix_first_button:
                self.matrix_page_index = 0
            elif event.ui_element == self.matrix_previous_button:
                self.matrix_page_index = max(0, self.matrix_page_index - 1)
            elif event.ui_element == self.matrix_next_button:
                self.matrix_page_index += 1
            elif event.ui_element == self.matrix_last_button:
                self.matrix_page_index = max(0, math.ceil(len(self.matrix_rows) / 10) - 1)
            elif event.ui_element == self.start_button:
                self._start()
            elif event.ui_element == self.pause_button:
                self._toggle_pause()
            elif event.ui_element == self.first_button:
                self.page_index = 0
            elif event.ui_element == self.previous_button:
                self.page_index = max(0, self.page_index - 1)
            elif event.ui_element == self.next_button:
                self.page_index += 1
            elif event.ui_element == self.last_button:
                self.page_index = max(0, math.ceil(len(self.rows) / self._page_size()) - 1)
            elif event.ui_element == self.expand_all_button:
                self._set_all_nodes_expanded(expanded=True)
            elif event.ui_element == self.collapse_all_button:
                self._set_all_nodes_expanded(expanded=False)
            elif event.ui_element == self.locate_root_button:
                self._locate_root()
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            graph_node = (
                self._node_at(event.pos)
                if self.active_page == "tree"
                and ENABLE_ITERATION_TREE_RENDERING
                and self._graph_rect().collidepoint(event.pos)
                else None
            )
            self._handle_custom_click(event.pos)
            graph_control_clicked = (
                ENABLE_ITERATION_TREE_RENDERING
                and self.active_page == "tree"
                and (
                    self.expand_all_button.rect.collidepoint(event.pos)
                    or self.collapse_all_button.rect.collidepoint(event.pos)
                    or self.locate_root_button.rect.collidepoint(event.pos)
                )
            )
            if (
                ENABLE_ITERATION_TREE_RENDERING
                and self.active_page == "tree"
                and self._graph_rect().collidepoint(event.pos)
                and graph_node is None
                and not graph_control_clicked
            ):
                self.dragging_graph = True
                self.drag_origin = event.pos
                self.pan_origin = tuple(self.graph_pan)
        elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            self.dragging_graph = False
        elif event.type == pygame.MOUSEMOTION and self.dragging_graph:
            self.graph_pan[0] = self.pan_origin[0] + event.pos[0] - self.drag_origin[0]
            self.graph_pan[1] = self.pan_origin[1] + event.pos[1] - self.drag_origin[1]
        elif event.type == pygame.MOUSEWHEEL:
            if (
                self.active_page == "tree"
                and ENABLE_ITERATION_TREE_RENDERING
                and self._graph_rect().collidepoint(pygame.mouse.get_pos())
            ):
                self.graph_zoom = max(0.25, min(2.4, self.graph_zoom * (1.12**event.y)))
        self.manager.process_events(event)

    def _draw_hover_tooltip(self) -> None:
        if not self.hover_tooltip:
            return
        # 边 hover 的原因文本必须走换行/多列渲染，不能落入短提示的截断路径。
        if len(self.hover_tooltip) > 7 or self.hovered_edge is not None:
            font = self._font(12)
            line_height = 14
            max_rows = max(1, (self.screen.get_height() - 54) // line_height)
            column_count = min(
                3,
                max(1, math.ceil(len(self.hover_tooltip) / max_rows)),
            )
            for _ in range(3):
                column_width = (self.screen.get_width() - 32) // column_count
                wrapped: list[str] = []
                for source_line in self.hover_tooltip:
                    line = str(source_line)
                    indent = line[: len(line) - len(line.lstrip())]
                    current = ""
                    for character in line:
                        if current and font.size(current + character)[0] > column_width - 22:
                            wrapped.append(current)
                            current = indent + character
                        else:
                            current += character
                    wrapped.append(current)
                required_columns = min(
                    3,
                    max(1, math.ceil(len(wrapped) / max_rows)),
                )
                if required_columns == column_count:
                    break
                column_count = required_columns
            visible_rows = math.ceil(len(wrapped) / column_count)
            rect = pygame.Rect(
                12,
                12,
                self.screen.get_width() - 24,
                min(self.screen.get_height() - 24, 26 + visible_rows * line_height),
            )
            pygame.draw.rect(self.screen, pygame.Color("#07101E"), rect, border_radius=7)
            pygame.draw.rect(self.screen, ACCENT, rect, width=2, border_radius=7)
            rows_per_column = math.ceil(len(wrapped) / column_count)
            for line_index, line in enumerate(wrapped):
                column = line_index // rows_per_column
                row = line_index % rows_per_column
                self._text(
                    line,
                    (
                        rect.x + 10 + column * column_width,
                        rect.y + 8 + row * line_height,
                    ),
                    size=12,
                    color=ACCENT if str(line).startswith("【") else TEXT,
                )
            return
        lines = self.hover_tooltip
        width = min(
            650,
            max(260, max(self._font(12).size(str(line))[0] for line in lines) + 24),
        )
        height = 16 + len(lines) * 19
        mouse_x, mouse_y = pygame.mouse.get_pos()
        x = min(self.screen.get_width() - width - 8, mouse_x + 18)
        y = min(self.screen.get_height() - height - 8, mouse_y + 18)
        rect = pygame.Rect(max(8, x), max(8, y), width, height)
        pygame.draw.rect(self.screen, pygame.Color("#07101E"), rect, border_radius=6)
        pygame.draw.rect(self.screen, ACCENT, rect, width=1, border_radius=6)
        for offset, line in enumerate(lines):
            self._text(
                str(line),
                (rect.x + 10, rect.y + 8 + offset * 19),
                size=12,
                max_width=rect.width - 20,
            )

    def _update_input_hover_tooltip(self) -> None:
        if self.active_page != "tree":
            return
        mouse = pygame.mouse.get_pos()
        extra = {
            "number_of_players": t("gui.hover.players"),
            "number_of_wolves": t("gui.hover.wolves"),
            "page_size": t("gui.hover.page_size"),
        }
        if self.lambda_slider.rect.collidepoint(mouse):
            self.hover_tooltip = [
                t("label.lambda_risk"),
                t("gui.hover.lambda.range"),
                t("gui.hover.lambda.effect"),
            ]
            return
        if self.pause_button.rect.collidepoint(mouse):
            self.hover_tooltip = [
                t("gui.resume" if self.paused else "gui.pause"),
                t("gui.hover.pause.line_1"),
                t("gui.hover.pause.line_2"),
            ]
            return
        if self.expand_all_button.rect.collidepoint(mouse):
            self.hover_tooltip = [
                t("gui.expand_all"),
                t("gui.hover.expand"),
            ]
            return
        if self.collapse_all_button.rect.collidepoint(mouse):
            self.hover_tooltip = [
                t("gui.collapse_all"),
                t("gui.hover.collapse"),
            ]
            return
        if self.locate_root_button.rect.collidepoint(mouse):
            self.hover_tooltip = [
                t("gui.locate_root"),
                t("gui.hover.locate.line_1"),
                t("gui.hover.locate.line_2"),
            ]
            return
        for key, entry in self.entries.items():
            if entry.rect.collidepoint(mouse):
                self.hover_tooltip = [
                    t(f"label.{key}"),
                    extra[key],
                    t(f"help.{key}"),
                ]
                return

    def _update_option_hover_tooltip(self) -> None:
        if self.active_page != "tree":
            return
        mouse = pygame.mouse.get_pos()
        option_help = {
            "include_seer": t("help.include_seer"),
            "include_witch": t("help.include_witch"),
            "include_guard": t("help.include_guard"),
            "include_hunter": t("help.include_hunter"),
            "include_idiot": t("gui.hover.idiot"),
            "include_white_werewolf_king": t("help.include_white_werewolf_king"),
            "all_positions": t("help.all_positions"),
            "smart_vote": t("gui.hover.smart_vote"),
        }
        for key, rect in self.toggle_rects.items():
            if rect.collidepoint(mouse):
                self.hover_tooltip = [t(f"label.{key}"), option_help[key]]
                return
        tactic_help = {key: t(f"gui.hover.tactic.{key}") for key in (*DAY_TACTICS, *NIGHT_TACTICS)}
        for key, rect in self.tactic_rects.items():
            if rect.collidepoint(mouse):
                lines = [t(f"tactic.{key}"), tactic_help[key]]
                if key in NIGHT_TACTICS and not (self.values["include_witch"] or self.values["include_guard"]):
                    lines.append(t("gui.hover.tactic.disabled"))
                self.hover_tooltip = lines
                return
        for group, rect in self.header_rects.items():
            if rect.collidepoint(mouse):
                self.hover_tooltip = [
                    t("tactic.day" if group == "day" else "tactic.night"),
                    t("gui.hover.tactic.group"),
                ]
                return

    def _update_matrix_hover_tooltip(self) -> None:
        """为矩阵页参数、停止操作和结果行提供可见 hover 说明。"""

        if self.active_page != "matrix":
            return
        mouse = pygame.mouse.get_pos()
        entry_help = {
            "position_index": t("gui.matrix.hover.position"),
            "actor_seat": t("gui.matrix.hover.actor"),
            "samples": t("gui.matrix.hover.samples"),
        }
        for key, entry in self.matrix_entries.items():
            if entry.rect.collidepoint(mouse):
                self.hover_tooltip = [entry_help[key]]
                return
        if self.matrix_force_rect.collidepoint(mouse):
            self.hover_tooltip = [
                t("gui.matrix.hover.recompute_title"),
                t("gui.matrix.hover.recompute_body"),
            ]
            return
        if self.matrix_stop_button.rect.collidepoint(mouse):
            self.hover_tooltip = [
                t("gui.matrix.hover.stop_title"),
                t("gui.matrix.hover.stop_body"),
            ]
            return
        for rect, row_index in self.matrix_table_row_rects:
            if rect.collidepoint(mouse) and 0 <= row_index < len(self.matrix_rows):
                row = self.matrix_rows[row_index]
                self.hover_tooltip = [
                    _matrix_action_label(dict(row.get("action") or {})),
                    t("gui.matrix.hover.row"),
                ]
                return

    def draw(self) -> None:
        current_lambda = round(float(self.lambda_slider.get_current_value()), 2)
        if (
            self.active_page == "tree"
            and ENABLE_ITERATION_TREE_RENDERING
            and not self.running
            and not self.paused
            and self.graph.get("nodes")
            and current_lambda != self.last_interval_lambda
        ):
            self._recompute_loaded_graph()
        self.screen.fill(BACKGROUND)
        self.hover_tooltip = []
        if self.active_page == "tree":
            self._draw_config()
            self._draw_table()
            if ENABLE_ITERATION_TREE_RENDERING:
                self._draw_graph()
            else:
                self._draw_iteration_tree_disabled()
            self._update_option_hover_tooltip()
            self._update_input_hover_tooltip()
            self._text(self.status, (18, 752), size=12, color=MUTED, max_width=285)
            self._text(
                t(
                    "gui.tree.footer",
                    states=_compact_integer(sum(int(row.get("state_count", 0)) for row in self.rows)),
                ),
                (1090, 325),
                size=11,
                color=MUTED,
                max_width=330,
            )
        else:
            self._draw_matrix_config()
            self._draw_matrix_table()
            self._draw_matrix_detail()
            self._update_matrix_hover_tooltip()
            self._text(
                self.matrix_status,
                (18, 752),
                size=12,
                color=MUTED,
                max_width=285,
            )
            self._text(
                t("gui.matrix.footer", rows=len(self.matrix_rows)),
                (1090, 325),
                size=11,
                color=MUTED,
                max_width=330,
            )
        self.manager.draw_ui(self.screen)
        self._draw_hover_tooltip()
        pygame.display.flip()

    def run(self, *, max_frames: int | None = None) -> None:
        frames = 0
        while self.keep_running:
            delta = self.clock.tick(60) / 1000.0
            for event in pygame.event.get():
                self._handle_event(event)
            self._drain_events()
            self.manager.update(delta)
            self.draw()
            frames += 1
            if max_frames is not None and frames >= max_frames:
                break
        self._shutdown_matrix_process()
        try:
            self.control_manager.shutdown()
        except (AttributeError, EOFError, OSError):
            pass
        pygame.quit()


def launch_gui(
    parser: argparse.ArgumentParser,
    run_simulation: Callable[..., Any],
    *,
    max_frames: int | None = None,
) -> None:
    """启动默认中文的 Pygame 策略展示界面。"""

    PygameSimulatorUI(parser, run_simulation).run(max_frames=max_frames)
