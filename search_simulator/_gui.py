"""Pygame 研究者视角的 DFS 分页与状态 DAG 可视化界面。"""

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


def _yes_no(value: Any) -> str:
    return "是" if bool(value) else "否"


def _seat_label(value: Any) -> str:
    if value is None:
        return "无"
    return f"{int(value) + 1}号"


def _seat_list(values: Any) -> str:
    items = list(values or [])
    if not items:
        return "无"
    return "、".join(_seat_label(value) for value in items)


def _phase_label(value: Any) -> str:
    return {
        "night": "黑夜",
        "day": "白天",
        "complete": "已完成",
    }.get(str(value), str(value) or "未知")


def _interval_label(value: Any) -> str:
    values = list(value or [-1.0, 1.0])
    if len(values) < 2:
        return "未计算"
    return f"[{float(values[0]):.4f}, {float(values[1]):.4f}]"


def _format_game_state_hover(
    node_id: int,
    node: dict[str, Any],
    state: dict[str, Any],
) -> list[str]:
    """把完整 GameState 转成研究者可读的分区 UI 文本。"""

    if state.get("unavailable"):
        return [
            "【节点概览】",
            f"节点编号：N{node_id}    搜索结果：{node.get('result', '未结束')}",
            f"Wide 区间：{_interval_label(node.get('wide_interval'))}    "
            f"Narrow 区间：{_interval_label(node.get('narrow_interval'))}",
            "【状态详情】",
            str(state["unavailable"]),
        ]

    seer_checks = state.get("seer_check_results") or {}
    check_text = "；".join(
        f"{_seat_label(index)}＝{'狼人' if bool(value) else '好人'}"
        for index, value in sorted(seer_checks.items(), key=lambda item: int(item[0]))
    ) or "无"
    role_claims = state.get("public_role_claims") or {}
    claim_text = "；".join(
        f"{_seat_label(index)}声明{role}"
        for index, role in sorted(role_claims.items(), key=lambda item: int(item[0]))
    ) or "无"
    day_votes = state.get("last_day_votes") or {}
    vote_text = "；".join(
        f"{_seat_label(voter)}→{_seat_label(target)}"
        for voter, target in sorted(day_votes.items(), key=lambda item: int(item[0]))
    ) or "无"
    snapshot_values = []
    for raw_value in state.get("players_snapshot") or []:
        snapshot_values.append(
            str(raw_value).replace(":alive", "·存活").replace(":dead", "·死亡")
        )
    snapshot_text = "；".join(snapshot_values) or "无"
    lines = [
        "【节点概览】",
        f"节点编号：N{node_id}    搜索结果：{node.get('result', '未结束')}",
        f"Wide 区间：{_interval_label(node.get('wide_interval'))}    "
        f"Narrow 区间：{_interval_label(node.get('narrow_interval'))}",
        "【对局进度】",
        f"当前阶段：{_phase_label(state.get('phase'))}    "
        f"白天轮次：{int(state.get('day_count', 0))}    "
        f"黑夜轮次：{int(state.get('night_count', 0))}",
        f"是否终局：{_yes_no(state.get('is_game_over'))}    "
        f"搜索深度：{int(state.get('depth', 0))}",
        f"父节点：{_seat_or_node(state.get('parent_state_id'))}    "
        f"派生动作：{state.get('action_label') or '根状态'}",
        "【身份与公共信息】",
        f"预言家已公开：{_yes_no(state.get('seer_revealed'))}    "
        f"上夜守护目标：{_seat_label(state.get('last_guard_target_index'))}",
        f"预言家查验：{check_text}",
        f"公开确认好人：{_seat_list(state.get('revealed_good_indices'))}",
        f"公开确认狼人：{_seat_list(state.get('revealed_wolf_indices'))}",
        f"公开身份声明：{claim_text}",
        f"已揭示愚者：{_seat_list(state.get('idiot_revealed_indices'))}",
        f"狼人优先目标：{_seat_list(state.get('wolf_priority_targets'))}",
        "【投票与战术】",
        f"上轮白天票型：{vote_text}",
        f"上轮白天战术：{state.get('last_day_strategy') or '无'}",
        "【站位与状态标识】",
        f"站位签名：{state.get('position_signature') or '无'}",
        f"状态编号：{int(state.get('state_id', -1))}    玩家快照：{snapshot_text}",
        "【玩家详情】",
    ]
    players = list(state.get("players") or [])
    for player_index, player in enumerate(players):
        skills = player.get("skills") or {}
        skill_text = "；".join(
            f"{name}：{_skill_count_label(count)}"
            for name, count in sorted(skills.items(), key=lambda item: str(item[0]))
        ) or "无"
        lines.append(
            f"{player_index + 1}号玩家｜角色：{player.get('role', '未知')}｜"
            f"状态：{'存活' if bool(player.get('is_alive')) else '死亡'}｜技能：{skill_text}"
        )
    if not players:
        lines.append("无玩家数据")
    return lines


def _seat_or_node(value: Any) -> str:
    if value is None:
        return "无（根节点）"
    return f"N{int(value)}"


def _skill_count_label(value: Any) -> str:
    count = int(value)
    if count < 0:
        return f"无限（{count}）"
    if count == 0:
        return "已耗尽（0）"
    return f"剩余{count}次（{count}）"


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

    run_id = str(result.get("run_id") or "未知")
    completed = int(result.get("position_count", 0))
    total = int(result.get("total_position_count", completed or 1))
    next_position = result.get("next_position_index")
    next_text = "无" if next_position is None else f"#{int(next_position)}"
    runtime_path = str(runtime_log_path())
    crash_path = str(crash_log_path())
    if status == "complete":
        title = "迭代完成"
        headline = "全部目标站位均已完成并持久化。"
    elif status == "interrupted":
        title = "迭代已中断（可恢复）"
        headline = "本次并非完成；已有检查点已保存，再次开始将自动续算。"
    else:
        error = error or {}
        error_type = str(error.get("error_type") or "UnknownError")
        error_message = str(error.get("error") or "未提供异常消息")
        if error.get("iteration_status") == "complete":
            title = "迭代已完成，但输出失败"
            headline = f"搜索已完成；结果文件或绘图失败：{error_type}: {error_message}"
        else:
            title = "迭代崩溃/失败（未完成）"
            headline = f"运行异常停止：{error_type}: {error_message}"
    lines = (
        headline,
        f"运行 ID：{run_id}",
        f"完整检查点：{completed}/{total}",
        f"下一恢复站位：{next_text}",
        f"运行日志：{runtime_path}",
        f"崩溃日志：{crash_path}",
    )
    return title, "<br>".join(html.escape(line) for line in lines)


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
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.control_manager = multiprocessing.Manager()
        self.resume_event = self.control_manager.Event()
        self.resume_event.set()
        self.worker_progress_queue = self.control_manager.Queue(maxsize=32)
        self.worker_result_queue = self.control_manager.Queue(maxsize=8)
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
            "include_white_werewolf_king": bool(
                self.defaults.include_white_werewolf_king
            ),
            "all_positions": bool(self.defaults.all_positions),
            "smart_vote": bool(self.defaults.smart_vote),
        }
        default_tactics = set(str(self.defaults.tactics).split(","))
        self.tactics = {
            key: key in default_tactics for key in (*DAY_TACTICS, *NIGHT_TACTICS)
        }
        self.toggle_rects: dict[str, pygame.Rect] = {}
        self.tactic_rects: dict[str, pygame.Rect] = {}
        self.header_rects: dict[str, pygame.Rect] = {}
        self.table_row_rects: list[tuple[pygame.Rect, int]] = []
        self.node_screen_positions: dict[int, tuple[float, float]] = {}
        self.hovered_node: int | None = None
        self.hovered_edge: dict[str, Any] | None = None
        self.hover_tooltip: list[str] = []

        self._create_controls()
        self._show_previous_crash_popup()

    def _create_controls(self) -> None:
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
            text=t("gui.start") + " · DFS",
            manager=self.manager,
        )
        self.pause_button = UIButton(
            pygame.Rect(206, 790, 98, 44),
            text=t("gui.pause"),
            manager=self.manager,
        )
        self.pause_button.disable()
        self.first_button = UIButton(
            pygame.Rect(346, 327, 58, 30), text=t("gui.first"), manager=self.manager
        )
        self.previous_button = UIButton(
            pygame.Rect(410, 327, 74, 30), text=t("gui.previous"), manager=self.manager
        )
        self.next_button = UIButton(
            pygame.Rect(490, 327, 74, 30), text=t("gui.next"), manager=self.manager
        )
        self.last_button = UIButton(
            pygame.Rect(570, 327, 58, 30), text=t("gui.last"), manager=self.manager
        )
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
        self.progress_bar = UIProgressBar(
            pygame.Rect(650, 327, 430, 30), manager=self.manager
        )

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
        self._panel(pygame.Rect(12, 12, 310, 766), "研究配置 · 固定 DFS")
        labels = (
            ("玩家数", 18, 80),
            ("普通狼", 170, 80),
            ("每页行数", 18, 136),
            ("风险 λ", 170, 136),
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
            "站位串行 · 自动检查点续算",
            (18, 204),
            size=12,
            color=MUTED,
        )

        self._text("角色与运行", (18, 260), size=16)
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
            self._text("智能投票关闭：战术不参与本次运行", (20, 432), size=12, color=MUTED)
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
                self._text("需启用女巫或守卫", (34, y), size=12, color=MUTED)

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
        self._panel(rect, "站位迭代结果")
        visible, pages = self._visible_rows()
        columns = (
            ("#", 346, 45),
            ("站位", 392, 335),
            ("状态", 732, 76),
            ("边", 812, 68),
            ("终局", 884, 64),
            ("wide", 952, 142),
            ("narrow", 1098, 142),
            ("占优", 1244, 76),
            ("耗时", 1324, 100),
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
            postprocess_percent = int(
                100.0 * postprocess_completed / max(1, postprocess_total)
            )
            interval_cell = (
                t("gui.interval_short", percent=postprocess_percent)
                if busy and processing_phase == "interval_progress"
                else t("postprocess.path_counts")
                if busy and processing_phase == "path_progress"
                else t("gui.computing")
            )
            camp = str(item["camp"])
            camp_color = (
                ACCENT
                if busy
                else GOOD
                if camp == "good"
                else WOLF
                if camp == "wolf"
                else MUTED
            )
            wide = item["wide_interval"]
            narrow = item["narrow_interval"]
            values = (
                (("▶" if busy else "") + str(item["position_index"]), 346, 42),
                (item.get("position_display", " | ".join(f"{i + 1}:{r}" for i, r in enumerate(item["roles"]))), 392, 330),
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

    def _layout_graph(
        self, graph: dict[str, Any]
    ) -> dict[int, tuple[float, float]]:
        """按搜索深度从左到右、同层节点从上到下生成 DAG 画布坐标。

        ``day_count + night_count`` 是节点距根节点的层级。层级固定映射到
        X 轴，避免把搜索深度误画成纵向时间线；同一层的节点按稳定的
        ``node_id`` 排序后映射到 Y 轴，保证相同输入下渲染顺序确定。
        """
        groups: dict[int, list[int]] = {}
        for node in graph.get("nodes", []):
            round_index = int(node["day_count"]) + int(node["night_count"])
            groups.setdefault(round_index, []).append(int(node["node_id"]))
        positions: dict[int, tuple[float, float]] = {}
        for round_index, node_ids in sorted(groups.items()):
            spacing = min(145.0, 900.0 / max(1, len(node_ids) - 1))
            for offset, node_id in enumerate(sorted(node_ids)):
                positions[node_id] = (
                    round_index * 105.0,
                    (offset - (len(node_ids) - 1) / 2.0) * spacing,
                )
        return positions

    def _screen_graph_point(
        self,
        canvas: tuple[float, float],
        rect: pygame.Rect,
    ) -> tuple[float, float]:
        return (
            rect.centerx + self.graph_pan[0] + canvas[0] * self.graph_zoom,
            rect.y + 150 + self.graph_pan[1] + canvas[1] * self.graph_zoom,
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
                ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy)
                / (dx * dx + dy * dy),
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
        compact = node.get("state_compact") or []
        if node.get("live_preview", False):
            state_label = "已展开" if node.get("expanded", False) else "frontier"
            details = [
                f"实时节点 {target_node} · {node['phase']} · {state_label} · interval 在站位完成后计算"
            ]
            position_display = str(graph.get("position_display", ""))
            if position_display:
                details.append(position_display)
        else:
            details = [
                f"节点 {target_node} · {node['phase']} · wide={node['wide_interval']} · narrow={node['narrow_interval']}"
            ]
        if len(compact) >= 12 and self.selected_row is not None:
            roles = self.rows[self.selected_row]["roles"]
            checks = {int(pair[0]): bool(pair[1]) for pair in compact[4]}
            public_good = {int(value) for value in compact[6]}
            public_wolf = {int(value) for value in compact[7]}
            claims = {int(pair[0]): str(pair[1]) for pair in compact[8]}
            idiots = {int(value) for value in compact[9]}
            player_states = compact[11]
            chips: list[str] = []
            for index, role in enumerate(roles):
                alive = bool(player_states[index][0])
                icons: list[str] = []
                if index in checks:
                    icons.append("验狼" if checks[index] else "验好")
                if index in claims:
                    icons.append("宣" + claims[index])
                if index in public_good:
                    icons.append("公好")
                if index in public_wolf:
                    icons.append("公狼")
                if index in idiots:
                    icons.append("身份揭示")
                chips.append(
                    f"{index + 1}:{role}{'·死' if not alive else ''}"
                    + ("[" + "/".join(icons) + "]" if icons else "")
                )
            details.append("  ".join(chips))
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
                # v1 的玩家数组位于 compact[11]，v2/v3 为扁平编码；统一
                # 交给契约解码器校验，GUI 不再猜测内部字段偏移。
                if not compact or not roles:
                    raise ValueError("缺少站位角色或紧凑状态")
                state = game_state_dict_from_compact(
                    compact,
                    roles=roles,
                    position_signature=str(graph.get("position_signature", "")),
                    is_game_over=bool(node.get("is_terminal", False)),
                    state_id=node_id,
                    observation=node.get("state_observation"),
                )
            except (TypeError, ValueError):
                state = {"unavailable": "该旧节点没有可恢复的 GameState 快照"}
        return _format_game_state_hover(node_id, node, state)

    def _draw_graph(self) -> None:
        rect = self._graph_rect()
        raw_graph = self._display_graph()
        graph = self._visible_graph(raw_graph)
        live_preview = self.running and self.preview_position in self.live_graphs
        title = (
            f"实时局部 DAG · 站位 #{self.preview_position} · 0.5s 刷新 · 观测窗口≤{LIVE_PREVIEW_NODE_LIMIT}"
            if live_preview
            else f"选中站位 DAG · 点击展开/收起 · 可见≤{DAG_VISIBLE_NODE_LIMIT} · hover 完整 GameState"
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
        self.screen.set_clip(
            pygame.Rect(rect.x + 8, rect.y + 110, rect.width - 16, rect.height - 118)
        )
        canvas_positions = self._layout_graph(graph)
        self.node_screen_positions = {
            node_id: self._screen_graph_point(position, rect)
            for node_id, position in canvas_positions.items()
        }
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
                    (now - float(node.get("_visible_since", now - LIVE_NODE_FADE_SECONDS)))
                    / LIVE_NODE_FADE_SECONDS,
                ),
            )
            for node in graph.get("nodes", [])
        }
        for edge_index, edge in enumerate(graph.get("edges", [])):
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
            pygame.draw.line(
                self.screen,
                color,
                parent,
                child,
                max(1, int(2 * self.graph_zoom)) + (3 if is_hovered else 0),
            )
            if self.graph_zoom >= 0.55:
                label = str(edge.get("action_label", ""))
                dx = child[0] - parent[0]
                dy = child[1] - parent[1]
                length = max(1.0, math.hypot(dx, dy))
                stagger = ((edge_index % 5) - 2) * 5
                self._text(
                    label,
                    (
                        int((parent[0] + child[0]) / 2 - dy / length * stagger),
                        int((parent[1] + child[1]) / 2 + dx / length * stagger),
                    ),
                    size=12,
                    color=color,
                    max_width=max(100, int(250 * self.graph_zoom)),
                )
            if is_hovered:
                edge_summary = (
                    f"实时分支 {edge['parent_id']}→{edge['child_id']} · interval 待站位完成"
                    if edge.get("live_preview", False)
                    else f"分支 {edge['parent_id']}→{edge['child_id']} · wide={edge['wide_interval']}"
                )
                self.hover_tooltip = [
                    edge_summary,
                    *[
                        str(reason.get("action_label", reason.get("action_key", "")))
                        for reason in edge.get("reasons", [])
                    ],
                ]
        node_lookup = {int(node["node_id"]): node for node in graph.get("nodes", [])}
        nodes_with_children = set(
            self._graph_structure(raw_graph)["children"]
        )
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
                pygame.Color("#FBBF24")
                if node["phase"] == "day"
                else pygame.Color("#8B5CF6"),
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
                self._text(line, (detail_rect.x + 8, detail_rect.y + 7 + offset * 18), size=12, max_width=detail_rect.width - 16)
        elif not graph.get("nodes"):
            self._text(
                "正在等待站位和节点预览…"
                if self.running
                else "运行完成后点击站位行查看持久化 DAG",
                (rect.x + 24, rect.y + 112),
                color=MUTED,
            )

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
                "tactics": ",".join(
                    key
                    for key, selected in self.tactics.items()
                    if selected and self.values["smart_vote"]
                ),
            }
        )
        values.update({key: self.values[key] for key in ROLE_KEYS})
        if values["number_of_players"] < 3:
            raise ValueError("玩家数必须不小于 3")
        if values["number_of_wolves"] < 1:
            raise ValueError("普通狼人数必须不小于 1")
        if not 0.0 <= values["lambda_risk"] <= 1.0:
            raise ValueError("lambda 必须位于 [0, 1]")
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
                "position_count": int(
                    getattr(exc, "completed_positions", 0)
                ),
                "total_position_count": int(getattr(exc, "total_positions", 0)),
                "next_position_index": getattr(exc, "next_position_index", None),
            }
            logger.error(
                "GUI_WORKER_TERMINAL status=failed run_id=%s checkpoints=%s/%s "
                "next_position=%s error_type=%s error=%s",
                error_payload["run_id"] or "unknown",
                error_payload["position_count"],
                error_payload["total_position_count"],
                error_payload["next_position_index"] or "none",
                error_payload["error_type"],
                error_payload["error"],
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            try:
                from ._crash_handler import record_caught_failure

                record_caught_failure(
                    exc,
                    category="gui_worker",
                    context={
                        "run_id": error_payload["run_id"] or "unknown",
                        "checkpoints": (
                            f"{error_payload['position_count']}/"
                            f"{error_payload['total_position_count']}"
                        ),
                        "next_position": (
                            error_payload["next_position_index"] or "none"
                        ),
                        "error_type": error_payload["error_type"],
                    },
                )
            except BaseException:
                logger.critical(
                    "GUI_CRASH_LOG_FALLBACK_FAILED error_type=%s",
                    error_payload["error_type"],
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
                "检测到上次运行发生原生崩溃；上次迭代不能视为完成。",
                "已完成站位仍可由 SQLite 检查点恢复。",
                f"崩溃日志：{previous}",
                f"本次运行日志：{runtime_log_path()}",
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
            window_title="检测到上次运行崩溃",
            object_id="#terminal_previous_crash",
            always_on_top=True,
        )
        logger.warning("PREVIOUS_NATIVE_CRASH log=%s", previous)

    def _acknowledge_previous_crash_popup(self) -> None:
        """用户关闭历史崩溃提示后写入一次性通知标记。"""

        if self.pending_previous_crash_log is None:
            return
        try:
            mark_crash_log_reported(self.pending_previous_crash_log)
        except OSError:
            logger.exception(
                "CRASH_NOTIFICATION_MARK_FAILED log=%s",
                self.pending_previous_crash_log,
            )
        self.pending_previous_crash_log = None

    def _start(self) -> None:
        if self.running:
            return
        if self.simulator is not None:
            # 上一次结果的只读连接在新请求前释放，避免新批次与旧连接
            # 同时持有 SQLite 文件句柄；内存中的 UI 图仍可继续被清理。
            cache = getattr(self.simulator, "signature_cache", None)
            if cache is not None:
                try:
                    cache.close()
                except Exception:
                    logger.exception("GUI_SOLUTION_CACHE_CLOSE_FAILED")
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
        self.start_button.set_text(t("gui.running_short") + " · DFS")
        self.pause_button.set_text(t("gui.pause"))
        self.pause_button.enable()
        self.status = t("gui.starting")
        threading.Thread(target=self._worker, args=(args,), daemon=True).start()

    def _load_selected_graph(self) -> None:
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
        self.graph["position_display"] = " | ".join(
            f"{index + 1}:{role}" for index, role in enumerate(row["roles"])
        )
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
        selected_position = (
            int(self.rows[self.selected_row]["position_index"])
            if self.selected_row is not None
            else 0
        )
        self.interval_recompute_requested_lambda = None
        self.interval_recompute_running = True

        def worker() -> None:
            last_emitted_at = 0.0

            def publish(stage: str, completed: int, total: int) -> None:
                nonlocal last_emitted_at
                now = time.monotonic()
                if (
                    completed not in {0, total}
                    and now - last_emitted_at < UI_DATA_REFRESH_SECONDS
                ):
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
                    "LOCAL_INTERVAL_RECOMPUTE_FAILED position=%s lambda=%.2f "
                    "error_type=%s error=%s",
                    selected_position,
                    current_lambda,
                    type(exc).__name__,
                    exc,
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
        entry_nodes = sorted(
            node_id for node_id, incoming_count in incoming.items() if incoming_count == 0
        )
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
                edge
                for edge in structure["edges_by_parent"].get(parent_id, [])
                if int(edge["child_id"]) in visible_ids
            )
        return {
            **graph,
            "nodes": [
                node_by_id[node_id] for node_id in sorted(visible_ids)
            ],
            "edges": visible_edges,
        }

    @staticmethod
    def _mark_newly_visible_nodes(
        graph: dict[str, Any],
        previous_ids: set[int],
    ) -> None:
        now = time.monotonic()
        visible_ids = {
            int(node["node_id"])
            for node in PygameSimulatorUI._visible_graph(graph).get("nodes", [])
        }
        for node in graph.get("nodes", []):
            if int(node["node_id"]) in visible_ids - previous_ids:
                node["_visible_since"] = now

    def _toggle_node_children(self, node_id: int) -> None:
        graph = self._display_graph()
        previous_ids = {
            int(node["node_id"])
            for node in self._visible_graph(graph).get("nodes", [])
        }
        expanded = graph.setdefault("_expanded_node_ids", set())
        if node_id in expanded:
            expanded.remove(node_id)
        else:
            expanded.add(node_id)
        self._mark_newly_visible_nodes(graph, previous_ids)
        self.selected_node = node_id

    def _set_all_nodes_expanded(self, *, expanded: bool) -> None:
        graph = self._display_graph()
        previous_ids = {
            int(node["node_id"])
            for node in self._visible_graph(graph).get("nodes", [])
        }
        expanded_nodes = graph.setdefault("_expanded_node_ids", set())
        if expanded:
            structure = self._graph_structure(graph)
            reachable = set(structure["entry_nodes"][:DAG_VISIBLE_NODE_LIMIT])
            pending = list(reachable)
            while pending and len(reachable) < DAG_VISIBLE_NODE_LIMIT:
                parent_id = pending.pop(0)
                expanded_nodes.add(parent_id)
                for child_id in sorted(
                    structure["children"].get(parent_id, set())
                ):
                    if child_id not in reachable:
                        reachable.add(child_id)
                        pending.append(child_id)
                        if len(reachable) >= DAG_VISIBLE_NODE_LIMIT:
                            break
        else:
            expanded_nodes.clear()
        self._mark_newly_visible_nodes(graph, previous_ids)
        visible_nodes = self._visible_graph(graph).get("nodes", [])
        self.selected_node = (
            int(visible_nodes[0]["node_id"]) if visible_nodes else None
        )

    def _upsert_progress_row(self, payload: dict[str, Any]) -> None:
        position_index = int(payload["position_index"])
        current_row = next(
            (
                row
                for row in self.rows
                if int(row["position_index"]) == position_index
            ),
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
            processing_phase=str(
                payload.get("kind", row.get("processing_phase", "node_progress"))
            ),
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
        previous_visible_ids = {
            int(node["node_id"])
            for node in self._visible_graph(graph).get("nodes", [])
        }
        graph["roles"] = list(payload.get("roles", graph.get("roles", [])))
        graph["position_display"] = str(
            payload.get("position_display", graph.get("position_display", ""))
        )
        graph["focus_node_id"] = int(
            payload.get("focus_node_id", graph.get("focus_node_id", 0))
        )

        incoming_nodes = [dict(node) for node in payload.get("preview_nodes", [])]
        incoming_edges = [dict(edge) for edge in payload.get("preview_edges", [])]
        node_by_id = {int(node["node_id"]): node for node in graph["nodes"]}
        for node in incoming_nodes:
            node_id = int(node["node_id"])
            existing = node_by_id.get(node_id)
            if existing is not None and "_visible_since" in existing:
                node["_visible_since"] = existing["_visible_since"]
            node_by_id[node_id] = node

        edge_by_key = {
            (int(edge["parent_id"]), int(edge["child_id"])): edge
            for edge in graph["edges"]
        }
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
            if not snapshot.get("completed", False)
            and position_index in self.live_graphs
        )
        if self.preview_position not in active_positions and active_positions:
            self.preview_position = active_positions[0]
            graph = self.live_graphs[self.preview_position]
            self.selected_node = int(graph.get("focus_node_id", 0))

    def _sync_live_node_totals(self) -> None:
        snapshots = tuple(self.position_progress.values())
        self.live_stats["expanded_nodes"] = sum(
            int(item.get("processed_states", 0)) for item in snapshots
        )
        self.live_stats["discovered_nodes"] = sum(
            int(item.get("discovered_states", 0)) for item in snapshots
        )
        self.live_stats["frontier_size"] = sum(
            int(item.get("frontier_size", 0)) for item in snapshots
        )
        self.live_stats["edge_count"] = sum(
            int(item.get("edge_count", 0)) for item in snapshots
        )
        self.live_stats["terminal_count"] = sum(
            int(item.get("terminal_count", 0)) for item in snapshots
        )

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
            if int(payload.get("processed_states", 0)) < int(
                current.get("processed_states", 0)
            ):
                return
            self.position_progress[position_index] = {
                **current,
                **payload,
                "completed": False,
            }
            self._upsert_progress_row(payload)
            if event_kind in {"position_started", "node_progress"}:
                self._merge_live_preview(payload)
            self._choose_preview_position()
            self.active_position = position_index
            self.live_stats["total_positions"] = int(
                payload.get("total_positions", self.live_stats["total_positions"])
            )
            self._sync_live_node_totals()
            if self.paused:
                self.status = t(
                    "gui.paused",
                    expanded=_compact_integer(
                        int(self.live_stats["expanded_nodes"])
                    ),
                )
            elif event_kind == "path_progress":
                self.status = t(
                    "gui.path_progress",
                    position=position_index,
                    completed=_compact_integer(
                        int(payload.get("postprocess_completed", 0))
                    ),
                    total=_compact_integer(
                        int(payload.get("postprocess_total", 0))
                    ),
                )
            elif event_kind == "interval_progress":
                stage = str(payload.get("postprocess_stage", "node_intervals"))
                self.status = t(
                    "gui.interval_progress",
                    position=position_index,
                    stage=t(f"postprocess.{stage}"),
                    completed=_compact_integer(
                        int(payload.get("postprocess_completed", 0))
                    ),
                    total=_compact_integer(
                        int(payload.get("postprocess_total", 0))
                    ),
                )
            else:
                self.status = t(
                    "gui.node_progress",
                    position=position_index,
                    expanded=_compact_integer(int(self.live_stats["expanded_nodes"])),
                    discovered=_compact_integer(
                        int(self.live_stats["discovered_nodes"])
                    ),
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
        self._choose_preview_position()
        self._sync_live_node_totals()
        self.progress = done * 100.0 / max(1, total)
        self.progress_bar.set_current_progress(self.progress)
        if not self.paused:
            self.status = t("gui.running", done=done, total=total)
        self.live_stats["good_paths"] = sum(
            int(row["good_paths"]) for row in self.rows
        )
        self.live_stats["wolf_paths"] = sum(
            int(row["wolf_paths"]) for row in self.rows
        )
        self.live_stats["completed_positions"] = done
        self.live_stats["total_positions"] = total

    def _toggle_pause(self) -> None:
        if not self.running:
            return
        if self.paused:
            self.resume_event.set()
            self.paused = False
            self.pause_button.set_text(t("gui.pause"))
            self.start_button.set_text(t("gui.running_short") + " · DFS")
            active = self.position_progress.get(self.active_position, {})
            active_kind = str(active.get("kind", "node_progress"))
            if active_kind == "path_progress":
                self.status = t(
                    "gui.path_progress",
                    position=self.active_position or "?",
                    completed=_compact_integer(
                        int(active.get("postprocess_completed", 0))
                    ),
                    total=_compact_integer(int(active.get("postprocess_total", 0))),
                )
            elif active_kind == "interval_progress":
                stage = str(active.get("postprocess_stage", "node_intervals"))
                self.status = t(
                    "gui.interval_progress",
                    position=self.active_position or "?",
                    stage=t(f"postprocess.{stage}"),
                    completed=_compact_integer(
                        int(active.get("postprocess_completed", 0))
                    ),
                    total=_compact_integer(int(active.get("postprocess_total", 0))),
                )
            else:
                self.status = t(
                    "gui.node_progress",
                    position=self.active_position or "?",
                    expanded=_compact_integer(int(self.live_stats["expanded_nodes"])),
                    discovered=_compact_integer(
                        int(self.live_stats["discovered_nodes"])
                    ),
                )
            return
        self.resume_event.clear()
        self.paused = True
        self.pause_button.set_text(t("gui.resume"))
        self.start_button.set_text(t("gui.paused_short") + " · DFS")
        self.status = t(
            "gui.paused",
            expanded=_compact_integer(int(self.live_stats["expanded_nodes"])),
        )

    def _drain_events(self) -> None:
        now = time.monotonic()
        if now - self.last_data_refresh_at >= UI_DATA_REFRESH_SECONDS:
            for _ in range(512):
                try:
                    self._apply_iteration_event(
                        self.worker_progress_queue.get_nowait()
                    )
                except queue.Empty:
                    break
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
                        self.status = (
                            f"λ={completed_lambda:.2f}：已动态回传选中 DAG"
                            "（未重新搜索）"
                        )
                    requested_lambda = self.interval_recompute_requested_lambda
                    self.interval_recompute_requested_lambda = None
                    self.interval_recompute_running = False
                    if self.graph.get("nodes") and (
                        not graph_matches
                        or (
                            requested_lambda is not None
                            and round(float(requested_lambda), 2) != completed_lambda
                        )
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
                        self.status = (
                            "动态 interval 回传失败："
                            f"{payload['error_type']}: {payload['error']}"
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
                    self.live_stats["good_paths"] = sum(
                        int(row["good_paths"]) for row in self.rows
                    )
                    self.live_stats["wolf_paths"] = sum(
                        int(row["wolf_paths"]) for row in self.rows
                    )
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
                        self.start_button.set_text(t("gui.recompute") + " · DFS")
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
                        self.start_button.set_text(t("gui.start") + " · DFS")
                    self.pause_button.set_text(t("gui.pause"))
                    self.pause_button.disable()
                elif kind == "error":
                    self.running = False
                    self.paused = False
                    self.resume_event.set()
                    self.start_button.enable()
                    self.start_button.set_text(t("gui.start") + " · DFS")
                    self.pause_button.set_text(t("gui.pause"))
                    self.pause_button.disable()
                    self.status = t(
                        "gui.failed",
                        error=f"{payload['error_type']}: {payload['error']}",
                    )
                    self._show_terminal_popup("failed", payload, error=payload)
        except queue.Empty:
            pass

    def _handle_custom_click(self, position: tuple[int, int]) -> None:
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
                    if key in NIGHT_TACTICS and not (
                        self.values["include_witch"] or self.values["include_guard"]
                    ):
                        return
                    self.tactics[key] = not self.tactics[key]
                    return
        for rect, row_index in self.table_row_rects:
            if rect.collidepoint(position):
                self.selected_row = row_index
                self._load_selected_graph()
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
            self.keep_running = False
        elif event.type == pygame_gui.UI_WINDOW_CLOSE:
            if event.ui_element == self.terminal_popup:
                self._acknowledge_previous_crash_popup()
                self.terminal_popup = None
        elif event.type == pygame_gui.UI_BUTTON_PRESSED:
            if event.ui_element == self.start_button:
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
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            graph_node = (
                self._node_at(event.pos)
                if self._graph_rect().collidepoint(event.pos)
                else None
            )
            self._handle_custom_click(event.pos)
            graph_control_clicked = (
                self.expand_all_button.rect.collidepoint(event.pos)
                or self.collapse_all_button.rect.collidepoint(event.pos)
            )
            if (
                self._graph_rect().collidepoint(event.pos)
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
            if self._graph_rect().collidepoint(pygame.mouse.get_pos()):
                self.graph_zoom = max(0.25, min(2.4, self.graph_zoom * (1.12 ** event.y)))
        self.manager.process_events(event)

    def _draw_hover_tooltip(self) -> None:
        if not self.hover_tooltip:
            return
        if len(self.hover_tooltip) > 12:
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
        lines = self.hover_tooltip[:7]
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
        mouse = pygame.mouse.get_pos()
        extra = {
            "number_of_players": "整数且不小于 3；默认测试板子为 7 人",
            "number_of_wolves": "整数且不小于 1；白狼王单独计数",
            "page_size": "范围 [1,10]；只影响 GUI 分页，不影响迭代结果",
        }
        if self.lambda_slider.rect.collidepoint(mouse):
            self.hover_tooltip = [
                t("label.lambda_risk"),
                "范围 [0,1]、步进 0.01、默认 0.5；拖动时右侧实时显示数值",
                "只放缩 wide/narrow 观测区间，不参与搜索或分支选择",
            ]
            return
        if self.pause_button.rect.collidepoint(mouse):
            self.hover_tooltip = [
                t("gui.resume" if self.paused else "gui.pause"),
                "暂停会保留当前 frontier、DAG 和节点计数；恢复后从原位置继续",
                "worker 会在展开下一节点前响应，正在生成的单个节点会先完成",
            ]
            return
        if self.expand_all_button.rect.collidepoint(mouse):
            self.hover_tooltip = [
                t("gui.expand_all"),
                "展开当前站位观测窗口内的全部节点；不读取完整 SQLite DAG",
            ]
            return
        if self.collapse_all_button.rect.collidepoint(mouse):
            self.hover_tooltip = [
                t("gui.collapse_all"),
                "收起当前站位的全部子树，只保留局部入口节点",
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
        mouse = pygame.mouse.get_pos()
        option_help = {
            "include_seer": t("help.include_seer"),
            "include_witch": t("help.include_witch"),
            "include_guard": t("help.include_guard"),
            "include_hunter": t("help.include_hunter"),
            "include_idiot": "启用后替换一名村民；愚者放逐免死、公开身份并永久失去投票权",
            "include_white_werewolf_king": t("help.include_white_werewolf_king"),
            "all_positions": t("help.all_positions"),
            "smart_vote": "默认开启；战术优先于阵营筛选，关闭时战术不显示且提交空集合",
        }
        for key, rect in self.toggle_rects.items():
            if rect.collidepoint(mouse):
                self.hover_tooltip = [t(f"label.{key}"), option_help[key]]
                return
        tactic_help = {
            "seer_hide": "在预言家公开身份的基线之外，增加预言家保持隐藏的白天分支",
            "villager_decoy": "枚举存活村民声明预言家的组合，并分别形成次夜指定刀口",
            "wolf_bloc": "所有存活狼人集中投给同一合法非狼目标；同级目标全部展开",
            "wolf_self_kill": "需女巫或守卫存在且存活狼不少于 2；枚举每名存活狼作为自刀目标",
            "wolf_no_kill": "需女巫或守卫存在；增加无狼刀目标的平安夜分支，其他夜间行动仍结算",
        }
        for key, rect in self.tactic_rects.items():
            if rect.collidepoint(mouse):
                lines = [t(f"tactic.{key}"), tactic_help[key]]
                if key in NIGHT_TACTICS and not (
                    self.values["include_witch"] or self.values["include_guard"]
                ):
                    lines.append("当前禁用：需要先勾选女巫或守卫")
                self.hover_tooltip = lines
                return
        for group, rect in self.header_rects.items():
            if rect.collidepoint(mouse):
                self.hover_tooltip = [
                    t("tactic.day" if group == "day" else "tactic.night"),
                    "点击展开或折叠；勾选战术会在正常基线之外增加对应分支",
                ]
                return

    def draw(self) -> None:
        current_lambda = round(float(self.lambda_slider.get_current_value()), 2)
        if (
            not self.running
            and not self.paused
            and self.graph.get("nodes")
            and current_lambda != self.last_interval_lambda
        ):
            self._recompute_loaded_graph()
        self.screen.fill(BACKGROUND)
        self._draw_config()
        self._draw_table()
        self._draw_graph()
        self._update_option_hover_tooltip()
        self._update_input_hover_tooltip()
        self._text(self.status, (18, 752), size=12, color=MUTED, max_width=285)
        self._text(
            f"缓存/图数据：{_compact_integer(sum(int(row.get('state_count', 0)) for row in self.rows))} 状态",
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
        pygame.quit()


def launch_gui(
    parser: argparse.ArgumentParser,
    run_simulation: Callable[..., Any],
    *,
    max_frames: int | None = None,
) -> None:
    """启动默认中文、固定 DFS 的 Pygame 可视化界面。"""

    PygameSimulatorUI(parser, run_simulation).run(max_frames=max_frames)
