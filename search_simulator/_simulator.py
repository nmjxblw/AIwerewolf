import sys
import os
import logging
from collections import defaultdict, deque, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations
import time
import copy
import json
import gc
import textwrap
import threading
from typing import Any, Callable, cast
import matplotlib

matplotlib.use(os.environ.get("SEARCH_SIMULATOR_MPL_BACKEND", "Agg"), force=True)

# 在导入 pyplot 之前先下调第三方库 logger，避免导入阶段刷屏。
# logging.getLogger("matplotlib").setLevel(logging.WARNING)
# logging.getLogger("matplotlib.font_manager").setLevel(logging.WARNING)
# logging.getLogger("PIL").setLevel(logging.WARNING)

import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.lines import Line2D
import pandas as pd
from pathlib import Path
from tqdm import tqdm

from ._player import Player
from ._game_state import GameState
from ._sqlite_lru_signature_store import _SQLiteLRUSignatureStore

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class SearchSimulator:
    """全树搜索模拟器，包含可视化，用于探索狼人杀游戏的所有可能局面。"""

    def __init__(self, **kwargs):
        logger.debug("开始初始化Search Simulator")
        self.has_clergies = False  # 标记是否包含神职角色
        """ 标记是否包含神职角色 """
        self.include_sheriff = False  # 标记是否启用警长归票机制
        """ 标记是否启用警长归票机制 """
        self.endings: list[tuple[GameState, str]] = []  # 存储游戏结束的状态
        """ 存储游戏结束的状态，包含状态对象和结果描述 """

        # 以下成员先给出默认值，便于类型提示与阅读；真实配置会在 load_config 中重置。
        self.visited_states: set[str] = set()
        """ 存储已访问的状态指纹，用于去重 """
        self.ending_signatures: set[str] = set()
        """ 存储已收敛的终局状态指纹，用于去重 """
        self.signature_cache: _SQLiteLRUSignatureStore | None = None
        """ 状态签名缓存（内存 LRU + SQLite 持久化） """
        self.signature_cache_db_path: Path = Path("search_simulator_cache.sqlite3")
        """ 状态签名 SQLite 路径 """
        self.signature_lru_capacity: int = 150_000
        """ 状态签名内存 LRU 容量 """
        self.signature_commit_interval: int = 2_000
        """ 状态签名批量写入 SQLite 的提交间隔 """
        self.state_parent_index: dict[int, int | None] = {}
        """ 存储每个状态节点的父节点 ID，用于回溯路径 """
        self.state_action_index: dict[int, str] = {}
        """ 存储每个状态节点的动作，用于回溯路径 """
        self.state_players_snapshot: dict[int, list[str]] = {}
        """ 存储每个状态节点的玩家存活快照（用于可视化标签） """
        self._next_state_id: int = 0
        """ 用于分配唯一的状态节点 ID """
        self._state_index_lock = threading.Lock()
        """ 并行扩展时用于保护 state_id 与索引写入 """

        self.max_processed_states: int | None = None
        """ 最多处理的状态节点数（默认不限） """
        self.max_queue_size: int | None = None
        """ 搜索队列最大长度，超出后新状态会被裁剪（默认不限） """
        self.max_runtime_seconds: float | None = None
        """ 最大运行时长（秒），到达后提前停止（默认不限） """
        self.search_mode: str = "dfs"
        """ 搜索模式：dfs 或 bfs """
        self.max_night_branches_per_state: int | None = None
        """ 单个状态夜晚阶段最多保留分支数（默认不限） """
        self.max_day_branches_per_state: int | None = None
        """ 单个状态白天阶段最多保留分支数（默认不限） """
        self.gc_interval: int = 2000
        """ 垃圾回收间隔（默认 2000） """
        self.parallel_workers: int = 1
        """ 并行扩展线程数（1 表示关闭并行） """
        self.enable_plot: bool = True
        """ 是否在运行结束后绘制状态树 """
        self.max_nodes_for_plot: int = 2500
        """ 超过该节点数时跳过绘图，避免图形后端崩溃 """
        self.max_plot_width_inches: float = 60.0
        """ 绘图最大宽度（英寸） """
        self.max_plot_height_inches: float = 40.0
        """ 绘图最大高度（英寸） """
        self.plot_dpi: int = 140
        """ 绘图输出 DPI """

        self.pruned_by_limits: int = 0
        """ 记录因阈值裁剪分支数 """
        self.stop_reason: str = "模拟完成"
        """ 模拟停止的原因 """

        self.players: list[Player] = []
        """ 玩家列表，包含所有参与游戏的角色对象 """
        self.queue: deque[GameState] = deque()
        """ 待展开状态队列（dfs 当栈使用，bfs 当队列使用） """
        self.wins: dict[str, int] = {}
        """ 终局结果计数器 """
        self.processed_states: int = 0
        """ 已处理的状态节点总数 """
        self.start_time: float = 0.0
        """ 模拟开始时间戳（monotonic） """
        self.iteration_callback: Callable[[dict[str, Any]], None] | None = None
        """ 每处理一个节点后触发的回调（用于 GUI 实时展示） """

        self.load_config(**kwargs)

    def __del__(self) -> None:
        cache: _SQLiteLRUSignatureStore | None = getattr(self, "signature_cache", None)
        if cache is not None:
            try:
                cache.close()
            except Exception:
                pass

    def _assign_state_identity(
        self,
        game_state: GameState,
        *,
        parent_state_id: int | None,
        action_label: str,
    ) -> None:
        """为游戏状态分配唯一的 state_id，并记录父节点和动作标签。"""

        with self._state_index_lock:
            game_state.state_id = self._next_state_id
            game_state.parent_state_id = parent_state_id
            self.state_parent_index[game_state.state_id] = parent_state_id
            self.state_action_index[game_state.state_id] = action_label
            players = self._normalize_players(game_state)
            self.state_players_snapshot[game_state.state_id] = [
                f"{index}:{player.role}{'存活' if player.is_alive else '死亡'}"
                for index, player in enumerate(players)
            ]
            self._next_state_id += 1

    def _build_state_path(self, state_id: int) -> list[int]:
        """构建从根节点到当前节点的 state_id 路径。"""
        path: list[int] = []
        current_id: int | None = state_id
        visited: set[int] = set()
        while current_id is not None:
            if current_id in visited:
                break
            visited.add(current_id)
            path.append(current_id)
            current_id = self.state_parent_index.get(current_id)
        path.reverse()
        return path

    def _build_labeled_state_path(
        self, state_id: int
    ) -> list[dict[str, int | str | None]]:
        """构建从根节点到当前节点的全路径，并附带每步动作标签。"""
        id_path = self._build_state_path(state_id)
        return [
            {
                "state_id": node_id,
                "parent_state_id": self.state_parent_index.get(node_id),
                "action_label": self.state_action_index.get(node_id, "未知"),
            }
            for node_id in id_path
        ]

    def _is_wolf_role(self, role: str) -> bool:
        return role in {"狼人", "白狼王"}

    def _alive_indices(
        self,
        game_state: GameState,
        *,
        exclude_indices: set[int] | None = None,
        predicate=None,
    ) -> list[int]:
        """返回当前游戏状态中存活玩家的索引列表，可选排除指定索引或按条件过滤。"""

        exclude_indices = exclude_indices or set()
        players = self._normalize_players(game_state)
        alive_indices: list[int] = []
        for index, player in enumerate(players):
            if index in exclude_indices or not player.is_alive:
                continue
            if predicate is not None and not predicate(player):
                continue
            alive_indices.append(index)
        return alive_indices

    def _normalize_players(self, game_state: GameState) -> list[Player]:
        """兼容异常数据形态，确保 `players` 总是可迭代的玩家列表。"""
        players = game_state.players
        if isinstance(players, list):
            return players
        if isinstance(players, Player):
            game_state.players = [players]
            return game_state.players
        try:
            game_state.players = list(players)
        except TypeError:
            game_state.players = [players]
        return game_state.players

    def _consume_skill(self, player: Player, skill_name: str) -> None:
        """消耗玩家技能使用次数，如果技能不存在或已用完则不做任何操作。"""

        if skill_name not in player.skills:
            return
        skill_value = player.skills[skill_name]
        if skill_value > 0:
            player.skills[skill_name] = skill_value - 1

    def _state_signature(self, game_state: GameState) -> str:
        """生成当前游戏状态的唯一签名，用于去重。"""

        # 用稳定 JSON 串做状态指纹，避免大规模运行时的 tuple/generator 异常。
        players = self._normalize_players(game_state)
        signature_payload = [
            game_state.night_count,
            game_state.last_guard_target_index,
            [
                [
                    player.role,
                    player.is_alive,
                    sorted(player.skills.items()),
                ]
                for player in players
            ],
        ]
        return json.dumps(
            signature_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _register_signature(self, namespace: str, signature: str) -> bool:
        """写入签名并返回是否首次出现。"""

        if self.signature_cache is None:
            target = (
                self.visited_states
                if namespace == "visited"
                else self.ending_signatures
            )
            if signature in target:
                return False
            target.add(signature)
            return True
        return self.signature_cache.add(namespace, signature)

    def _apply_deaths_with_chain(
        self, game_state: GameState, death_indices: list[int]
    ) -> list[GameState]:
        """应用死亡连锁规则，处理指定索引的玩家死亡，并展开可能的后续分支。"""

        branches = [game_state]
        unique_indices = list(dict.fromkeys(death_indices))
        for dead_index in unique_indices:
            next_branches: list[GameState] = []
            for state in branches:
                if dead_index < 0 or dead_index >= len(state.players):
                    next_branches.append(state)
                    continue
                if not state.players[dead_index].is_alive:
                    next_branches.append(state)
                    continue
                next_branches.extend(self._resolve_death_chain(state, dead_index))
            branches = next_branches
        return branches

    def _kill_player(self, game_state: GameState, player_index: int) -> None:
        """标记指定索引的玩家死亡，如果索引无效则不做任何操作。"""

        if player_index < 0 or player_index >= len(game_state.players):
            return
        game_state.players[player_index].is_alive = False

    def _resolve_death_chain(
        self, game_state: GameState, player_index: int
    ) -> list[GameState]:
        """解析玩家死亡连锁，返回可能的后续分支。"""

        # 死亡连锁：先标记该角色死亡，再按角色类型展开猎人开枪、白狼王爆炸等后续分支。
        if player_index < 0 or player_index >= len(game_state.players):
            return [game_state]

        player = game_state.players[player_index]
        if player.is_alive:
            player.is_alive = False
        branches = [game_state]

        if player.role == "猎人" and player.skills.get("开枪", 0) > 0:
            # 猎人死亡后可以带走一名其他存活角色，因此这里要对所有可选目标分别展开。
            next_branches: list[GameState] = []
            for state in branches:
                targets = self._alive_indices(state, exclude_indices={player_index})
                if not targets:
                    next_branches.append(state)
                    continue
                for target_index in targets:
                    branch = copy.deepcopy(state)
                    self._consume_skill(branch.players[player_index], "开枪")
                    self._kill_player(branch, target_index)
                    next_branches.extend(
                        self._resolve_death_chain(branch, target_index)
                    )
            branches = next_branches

        if player.role == "白狼王" and player.skills.get("带走击杀", 0) > 0:
            # 白狼王倒地后同样会触发带走一人，这里和猎人一样做分支展开。
            next_branches = []
            for state in branches:
                targets = self._alive_indices(state, exclude_indices={player_index})
                if not targets:
                    next_branches.append(state)
                    continue
                for target_index in targets:
                    branch = copy.deepcopy(state)
                    self._consume_skill(branch.players[player_index], "带走击杀")
                    self._kill_player(branch, target_index)
                    next_branches.extend(
                        self._resolve_death_chain(branch, target_index)
                    )
            branches = next_branches

        return branches

    def _resolve_night(self, game_state: GameState) -> list[GameState]:
        """解析夜晚阶段，返回可能的后续分支。"""

        # 夜晚阶段：狼人刀人 -> 守卫保护 -> 女巫单药决策 -> 死亡连锁。
        wolf_targets = self._alive_indices(
            game_state,
            predicate=lambda player: not self._is_wolf_role(player.role),
        )
        if not wolf_targets:
            idle_state = copy.deepcopy(game_state)
            idle_state.night_count += 1
            self._assign_state_identity(
                idle_state,
                parent_state_id=game_state.state_id,
                action_label="夜晚空闲(无目标)",
            )
            return [idle_state]

        night_states: list[GameState] = []
        local_seen: set[str] = set()

        guard_indices = self._alive_indices(
            game_state,
            predicate=lambda player: player.role == "守卫"
            and player.skills.get("保护", 0) != 0,
        )
        guard_index = guard_indices[0] if guard_indices else None

        witch_indices = self._alive_indices(
            game_state,
            predicate=lambda player: player.role == "女巫",
        )
        witch_index = witch_indices[0] if witch_indices else None

        for wolf_target_index in wolf_targets:
            guard_targets: list[int | None] = [None]
            if guard_index is not None:
                guard_targets.extend(
                    self._alive_indices(
                        game_state,
                        exclude_indices=(
                            {game_state.last_guard_target_index}
                            if game_state.last_guard_target_index is not None
                            else set()
                        ),
                    )
                )

            for guard_target in guard_targets:
                base_state = copy.deepcopy(game_state)
                if guard_index is not None:
                    self._consume_skill(base_state.players[guard_index], "保护")
                base_state.last_guard_target_index = guard_target

                guard_saved = guard_target == wolf_target_index

                witch_actions: list[tuple[str, int | None]] = [("无", None)]
                if witch_index is not None and base_state.players[witch_index].is_alive:
                    witch_player = base_state.players[witch_index]
                    can_self_save = (
                        wolf_target_index == witch_index and game_state.night_count == 0
                    )
                    can_save = witch_player.skills.get("解药", 0) > 0 and (
                        wolf_target_index != witch_index or can_self_save
                    )
                    if can_save:
                        witch_actions.append(("救活", None))

                    if witch_player.skills.get("毒药", 0) > 0:
                        poison_targets = self._alive_indices(
                            base_state, exclude_indices={witch_index}
                        )
                        for poison_target_index in poison_targets:
                            witch_actions.append(("毒杀", poison_target_index))

                for witch_action, poison_target_index in witch_actions:
                    branch_state = copy.deepcopy(base_state)
                    witch_saved = False
                    if witch_index is not None and witch_action == "救活":
                        self._consume_skill(branch_state.players[witch_index], "解药")
                        witch_saved = True
                    elif (
                        witch_index is not None
                        and witch_action == "毒杀"
                        and poison_target_index is not None
                    ):
                        self._consume_skill(branch_state.players[witch_index], "毒药")

                    # 规则：守卫和女巫同时救同一目标时，目标依然死亡。
                    if guard_saved and witch_saved:
                        wolf_kill_applies = True
                    elif guard_saved or witch_saved:
                        wolf_kill_applies = False
                    else:
                        wolf_kill_applies = True

                    deaths: list[int] = []
                    if wolf_kill_applies:
                        deaths.append(wolf_target_index)
                    if witch_action == "毒杀" and poison_target_index is not None:
                        deaths.append(poison_target_index)

                    resolved_states = self._apply_deaths_with_chain(
                        branch_state, deaths
                    )
                    for resolved_state in resolved_states:
                        resolved_state.night_count += 1
                        action_parts: list[str] = [f"夜晚 狼刀→{wolf_target_index}"]
                        if guard_index is not None:
                            guard_text = (
                                "无" if guard_target is None else str(guard_target)
                            )
                            action_parts.append(f"守卫→{guard_text}")
                        if witch_index is not None:
                            if (
                                witch_action == "毒杀"
                                and poison_target_index is not None
                            ):
                                witch_text = f"毒杀→{poison_target_index}"
                            elif witch_action == "救活":
                                witch_text = "救活"
                            else:
                                witch_text = "无"
                            action_parts.append(f"女巫={witch_text}")
                        action_parts.append(f"死亡={sorted(set(deaths))}")
                        action_label = "; ".join(action_parts)
                        self._assign_state_identity(
                            resolved_state,
                            parent_state_id=game_state.state_id,
                            action_label=action_label,
                        )
                        signature = self._state_signature(resolved_state)
                        if signature in local_seen:
                            continue
                        local_seen.add(signature)
                        night_states.append(resolved_state)
                        if (
                            self.max_night_branches_per_state is not None
                            and len(night_states) >= self.max_night_branches_per_state
                        ):
                            self.pruned_by_limits += 1
                            return night_states

        return night_states

    def _resolve_day_vote(self, game_state: GameState) -> list[GameState]:
        """解析白天投票阶段，返回可能的后续分支。"""

        # 白天投票建模：
        # 1) 单人最高票：该玩家直接出局；
        # 2) 启用警长时，平票最高票由警长归票，强制放逐 1 人并展开分支。
        alive_indices = self._alive_indices(game_state)
        if len(alive_indices) <= 1:
            return [copy.deepcopy(game_state)]

        vote_outcomes: set[tuple[int, ...]] = {
            (player_index,) for player_index in alive_indices
        }
        if self.include_sheriff:
            # 先覆盖最常见的双人平票，再补充“多人全平票”场景。
            vote_outcomes.update(combinations(alive_indices, 2))
            if len(alive_indices) > 2:
                vote_outcomes.add(tuple(alive_indices))

        day_states: list[GameState] = []
        local_seen: set[str] = set()

        for top_candidates in vote_outcomes:
            # 单人最高票：直接出局；平票：必须在平票者中淘汰一人。
            for vote_target_index in top_candidates:
                day_state = copy.deepcopy(game_state)
                for branched_state in self._resolve_death_chain(
                    day_state, vote_target_index
                ):
                    action_label = (
                        f"白天 投票最高={list(top_candidates)};"
                        f" 放逐={vote_target_index}"
                    )
                    self._assign_state_identity(
                        branched_state,
                        parent_state_id=game_state.state_id,
                        action_label=action_label,
                    )
                    signature = self._state_signature(branched_state)
                    if signature in local_seen:
                        continue
                    local_seen.add(signature)
                    day_states.append(branched_state)
                    if (
                        self.max_day_branches_per_state is not None
                        and len(day_states) >= self.max_day_branches_per_state
                    ):
                        self.pruned_by_limits += 1
                        return day_states

        return day_states

    def _check_game_over(self, game_state: GameState) -> tuple[bool, str]:
        """检查游戏是否结束"""
        # 狼人全灭直接好人胜；否则继续检查人数过半、屠边等结束条件。
        players = self._normalize_players(game_state)
        alive_players = [player for player in players if player.is_alive]
        alive_roles = [player.role for player in alive_players]
        if not any(self._is_wolf_role(role) for role in alive_roles):
            return True, "好人阵营胜利"  # 村民胜利
        alive_werewolves = [
            player for player in alive_players if self._is_wolf_role(player.role)
        ]

        if len(alive_werewolves) >= len(alive_players) / 2:
            return True, "狼人阵营胜利（人数过半）"  # 狼人胜利
        alive_clergies = [
            player
            for player in alive_players
            if player.role in ["预言家", "女巫", "守卫", "猎人"]
        ]
        if self.has_clergies and not alive_clergies:
            return True, "狼人阵营胜利（神职角色已被消灭）"  # 屠边规则
        alive_villagers = [player for player in alive_players if player.role == "村民"]
        if not alive_villagers:
            return True, "狼人阵营胜利（村民已被消灭）"  # 屠边规则
        return False, "未结束"  # 游戏继续

    def load_config(self, **kwargs):
        """加载配置,并重置模拟器状态。"""
        logger.debug("加载Search Simulator配置")
        # 这两个集合分别用于：过滤待展开的重复局面，以及去重已经收敛的终局。
        self.has_clergies = False
        """ 标记是否包含神职角色"""
        self.include_sheriff = bool(kwargs.get("include_sheriff", False))
        """ 标记是否启用警长归票机制"""
        self.visited_states = set()
        """ 存储已访问的状态指纹，用于去重"""
        self.ending_signatures = set()
        """ 存储已收敛的终局状态指纹，用于去重"""
        self.signature_cache_db_path = Path(
            kwargs.get("signature_cache_db_path", "search_simulator_cache.sqlite3")
        )
        """ 状态签名 SQLite 路径"""
        self.signature_lru_capacity = int(kwargs.get("signature_lru_capacity", 150_000))
        """ 状态签名内存 LRU 容量"""
        self.signature_commit_interval = int(
            kwargs.get("signature_commit_interval", 2_000)
        )
        """ 状态签名批量写入 SQLite 的提交间隔"""
        if self.signature_cache is not None:
            self.signature_cache.close()
        self.signature_cache = _SQLiteLRUSignatureStore(
            self.signature_cache_db_path,
            lru_capacity=self.signature_lru_capacity,
            commit_interval=self.signature_commit_interval,
        )
        self.signature_cache.reset()
        self.state_parent_index = {}
        """ 存储每个状态节点的父节点 ID，用于回溯路径"""
        self.state_action_index = {}
        """ 存储每个状态节点的动作，用于回溯路径"""
        self.state_players_snapshot = {}
        """ 存储每个状态节点的玩家存活快照（用于可视化标签）"""
        self._next_state_id = 0
        """ 用于分配唯一的状态节点 ID"""
        self.max_processed_states = kwargs.get("max_processed_states")
        """ 最多处理的状态节点数（默认不限）"""
        self.max_queue_size = kwargs.get("max_queue_size")
        """ 搜索队列最大长度，超出后新状态会被裁剪（默认不限）"""
        self.max_runtime_seconds = kwargs.get("max_runtime_seconds")
        """ 最大运行时长（秒），到达后提前停止（默认不限）"""
        self.search_mode = str(kwargs.get("search_mode", "dfs")).lower()
        """ 搜索模式：dfs 或 bfs"""
        if self.search_mode not in {"dfs", "bfs"}:
            self.search_mode = "dfs"
        self.max_night_branches_per_state = kwargs.get("max_night_branches_per_state")
        """ 单个状态夜晚阶段最多保留分支数（默认不限）"""
        self.max_day_branches_per_state = kwargs.get("max_day_branches_per_state")
        """ 单个状态白天阶段最多保留分支数（默认不限）"""
        self.gc_interval = int(kwargs.get("gc_interval", 2000))
        """ 垃圾回收间隔（默认 2000）"""
        self.parallel_workers = max(1, int(kwargs.get("parallel_workers", 1)))
        """ 并行扩展线程数（1 表示关闭并行） """
        self.enable_plot = bool(kwargs.get("enable_plot", True))
        """ 是否在运行结束后绘制状态树 """
        self.max_nodes_for_plot = int(kwargs.get("max_nodes_for_plot", 2500))
        """ 超过该节点数时跳过绘图，避免图形后端崩溃 """
        self.max_plot_width_inches = float(kwargs.get("max_plot_width_inches", 60.0))
        """ 绘图最大宽度（英寸） """
        self.max_plot_height_inches = float(kwargs.get("max_plot_height_inches", 40.0))
        """ 绘图最大高度（英寸） """
        self.plot_dpi = int(kwargs.get("plot_dpi", 140))
        """ 绘图输出 DPI """
        self.pruned_by_limits = 0
        """ 记录因阈值裁剪分支数"""
        self.stop_reason = "模拟完成"
        """ 模拟停止的原因"""
        self.players = []  # 初始化角色列表
        """ 玩家列表，包含所有参与游戏的角色对象 """
        if kwargs.get("include_seer", False):
            self.players.append(
                Player(role="预言家", is_alive=True, skills={"查验": -1})
            )
            self.has_clergies = True
        if kwargs.get("include_witch", False):
            self.players.append(
                Player(role="女巫", is_alive=True, skills={"解药": 1, "毒药": 1})
            )
            self.has_clergies = True
        if kwargs.get("include_guard", False):
            self.players.append(Player(role="守卫", is_alive=True, skills={"保护": -1}))
            self.has_clergies = True
        if kwargs.get("include_hunter", False):
            self.players.append(Player(role="猎人", is_alive=True, skills={"开枪": 1}))
            self.has_clergies = True
        if kwargs.get("include_white_werewolf_king", False):
            self.players.append(
                Player(role="白狼王", is_alive=True, skills={"带走击杀": 1})
            )
        if kwargs.get("number_of_wolves", 1) > 0:
            self.players.extend(
                [
                    Player(role="狼人", is_alive=True, skills={"攻击": -1})
                    for _ in range(kwargs.get("number_of_wolves", 1))
                ]
            )
        if kwargs.get("number_of_players", 5) > 0:
            self.players.extend(
                [
                    Player(role="村民", is_alive=True, skills={})
                    for _ in range(
                        kwargs.get("number_of_players", 5) - len(self.players)
                    )
                ]
            )  # 添加普通村民角色
        logger.debug(f"角色列表: {[player.role for player in self.players]}")
        initial_state = GameState(
            players=copy.deepcopy(self.players), is_game_over=False
        )
        self._assign_state_identity(
            initial_state, parent_state_id=None, action_label="根状态"
        )
        self.queue: deque[GameState] = deque([initial_state])  # 初始化队列
        self._register_signature("visited", self._state_signature(initial_state))
        self.wins = {}
        self.processed_states = 0
        self.start_time = 0.0
        callback = kwargs.get("iteration_callback")
        self.iteration_callback = (
            cast(Callable[[dict[str, Any]], None], callback)
            if callable(callback)
            else None
        )

    def _build_iteration_snapshot(self, game_state: GameState) -> dict[str, Any]:
        """构建当前迭代节点的摘要，供 GUI 实时展示。"""

        players = self._normalize_players(game_state)
        alive_count = sum(1 for player in players if player.is_alive)
        action_label = self.state_action_index.get(game_state.state_id, "未知")
        action_label = action_label.replace("\n", " ").strip()
        if len(action_label) > 56:
            action_label = action_label[:53] + "..."

        elapsed = 0.0
        if self.start_time > 0.0:
            elapsed = max(0.0, time.monotonic() - self.start_time)

        return {
            "state_id": game_state.state_id,
            "parent_state_id": game_state.parent_state_id,
            "night_count": game_state.night_count,
            "day_count": game_state.day_count,
            "alive_count": alive_count,
            "total_players": len(players),
            "is_game_over": bool(game_state.is_game_over),
            "action_label": action_label,
            "processed_states": self.processed_states,
            "queue_length": len(self.queue),
            "elapsed_seconds": elapsed,
        }

    def _emit_iteration_snapshot(self, game_state: GameState) -> None:
        """向外部发送迭代节点摘要，异常时吞掉以保证主流程稳定。"""

        if self.iteration_callback is None:
            return
        try:
            self.iteration_callback(self._build_iteration_snapshot(game_state))
        except Exception:
            logger.debug("迭代回调执行失败，已忽略。", exc_info=True)

    def _should_stop_run(self) -> bool:
        """检查是否触发停止条件。"""

        if (
            self.max_runtime_seconds is not None
            and time.monotonic() - self.start_time >= self.max_runtime_seconds
        ):
            self.stop_reason = "到达最大运行时间"
            return True
        if (
            self.max_processed_states is not None
            and self.processed_states >= self.max_processed_states
        ):
            self.stop_reason = "达到最大处理状态数"
            return True
        return False

    def _pop_next_state(self) -> GameState:
        """按搜索模式从容器取出下一个状态。"""

        if self.search_mode == "dfs":
            return self.queue.pop()
        return self.queue.popleft()

    def _iter_day_state_groups(
        self,
        night_states: list[GameState],
        executor: ThreadPoolExecutor | None,
    ):
        """统一包装白天阶段的串行/并行分支展开。"""

        if executor is not None and len(night_states) > 1:
            return executor.map(self._resolve_day_vote, night_states)
        return (self._resolve_day_vote(state) for state in night_states)

    def _handle_day_state(self, day_state: GameState) -> None:
        """处理单个白天结果状态：终局统计或继续入队。"""

        is_over, result = self._check_game_over(day_state)
        day_state.is_game_over = is_over
        if is_over:
            ending_signature = self._state_signature(day_state)
            if not self._register_signature("ending", ending_signature):
                return
            self.endings.append((day_state, result))
            self.wins[result] = self.wins.get(result, 0) + 1
            return

        state_signature = self._state_signature(day_state)
        if not self._register_signature("visited", state_signature):
            return
        if self.max_queue_size is not None and len(self.queue) >= self.max_queue_size:
            self.pruned_by_limits += 1
            return
        self.queue.append(day_state)

    def run(self):
        """运行模拟器，探索所有可能的游戏局面。"""

        logger.debug(f"开始运行 Simulator (search_mode={self.search_mode})")
        self.wins = {}
        self.start_time = time.monotonic()
        self.processed_states = 0
        day_expand_executor: ThreadPoolExecutor | None = None
        if self.parallel_workers > 1:
            day_expand_executor = ThreadPoolExecutor(
                max_workers=self.parallel_workers,
                thread_name_prefix="sim-day-expand",
            )

        try:
            while self.queue:
                if self._should_stop_run():
                    break

                current_state = self._pop_next_state()
                self.processed_states += 1
                self._emit_iteration_snapshot(current_state)
                if current_state.is_game_over:
                    continue

                # 先展开夜晚，再展开白天；每个新局面都先做去重，避免重复分支刷爆队列。
                night_states = self._resolve_night(current_state)
                day_state_groups = self._iter_day_state_groups(
                    night_states,
                    day_expand_executor,
                )

                for day_states in day_state_groups:
                    for day_state in day_states:
                        self._handle_day_state(day_state)

                if (
                    self.gc_interval > 0
                    and self.processed_states % self.gc_interval == 0
                ):
                    gc.collect()
        finally:
            if day_expand_executor is not None:
                day_expand_executor.shutdown(wait=True, cancel_futures=False)

        if self.signature_cache is not None:
            self.signature_cache.flush()
        self.report_results()
        if self.enable_plot:
            self.draw_plt()
        else:
            logger.info("已禁用绘图（enable_plot=False）")

    def report_results(self):
        """报告模拟结果，包括终局统计和运行信息。"""
        # 保存游戏结束状态到文件
        with open("endings.json", "w", encoding="utf-8") as f:
            json.dump(
                [
                    {
                        "state_id": state.state_id,
                        "parent_state_id": state.parent_state_id,
                        "state_path": self._build_state_path(state.state_id),
                        "state_path_with_actions": self._build_labeled_state_path(
                            state.state_id
                        ),
                        "player_state": [
                            {
                                "role": player.role,
                                "is_alive": player.is_alive,
                                "skills": player.skills,
                            }
                            for player in state.players
                        ],
                        "result": result,
                    }
                    for state, result in self.endings
                ],
                f,
                ensure_ascii=False,
                indent=4,
            )
        msg = f"总共模拟了 {len(self.endings)} 个终局\n"
        for result, count in sorted(self.wins.items()):
            msg += f"{result:<50} \t次数: {count:>5}\n"
        msg += (
            f"搜索模式: {self.search_mode}\n"
            f"停止原因: {self.stop_reason}\n"
            f"已处理状态数: {self.processed_states}\n"
            f"当前待处理容器长度: {len(self.queue)}\n"
            f"因阈值裁剪分支数: {self.pruned_by_limits}\n"
            f"运行耗时(秒): {time.monotonic() - self.start_time:.2f}\n"
        )
        if self.signature_cache is not None:
            cache_stats = self.signature_cache.stats_snapshot()
            msg += (
                "签名缓存统计:\n"
                f"  sqlite文件: {self.signature_cache_db_path}\n"
                f"  LRU容量: {cache_stats['lru_capacity']}\n"
                f"  LRU命中: {cache_stats['lru_hits']}\n"
                f"  SQLite命中: {cache_stats['sqlite_hits']}\n"
                f"  新增签名: {cache_stats['inserted']}\n"
                f"  visited LRU大小: {cache_stats['visited_lru_size']}\n"
                f"  ending LRU大小: {cache_stats['ending_lru_size']}\n"
            )

        logger.info(f"游戏结束统计:\n{msg}")

    def draw_plt(self):
        """根据缓存中的结果，从根节点绘制成树形图，涵盖节点过程"""
        if not self.state_parent_index:
            logger.info("状态索引为空，跳过绘图")
            return
        plt.axis("off")  # 关闭坐标轴显示
        # 仅绘制可以从根节点到达终局的路径，避免把重复/去重前的噪声分支全部铺开。
        terminal_state_ids = [
            state.state_id for state, _ in self.endings if state.state_id >= 0
        ]
        if terminal_state_ids:
            plotted_nodes: set[int] = set()
            for state_id in terminal_state_ids:
                plotted_nodes.update(self._build_state_path(state_id))
        else:
            # 未产生终局时退化为绘制当前已记录索引。
            plotted_nodes = set(self.state_parent_index.keys())

        if not plotted_nodes:
            logger.info("没有可绘制的节点，跳过绘图")
            return
        if len(plotted_nodes) > self.max_nodes_for_plot:
            logger.warning(
                "可绘制节点数为 %s，超过阈值 %s，跳过绘图以避免后端崩溃",
                len(plotted_nodes),
                self.max_nodes_for_plot,
            )
            return

        children_map: dict[int, list[int]] = defaultdict(list)
        roots: list[int] = []
        for node_id in sorted(plotted_nodes):
            parent_id = self.state_parent_index.get(node_id)
            if parent_id is None or parent_id not in plotted_nodes:
                roots.append(node_id)
                continue
            children_map[parent_id].append(node_id)

        for node_id in children_map:
            children_map[node_id].sort()
        roots = sorted(set(roots))

        x_pos: dict[int, float] = {}
        y_pos: dict[int, float] = {}
        # 增大节点间距，并根据节点数量做轻量自适应，优先保证标签可读性。
        node_count = len(plotted_nodes)
        leaf_gap = max(3.6, min(6.8, 3.8 + node_count * 0.01))
        depth_gap = 2.8
        next_leaf_x = 0.0

        def assign_position(node_id: int, depth: int) -> float:
            nonlocal next_leaf_x
            y_pos[node_id] = float(depth) * depth_gap
            children = children_map.get(node_id, [])
            if not children:
                x_pos[node_id] = next_leaf_x
                next_leaf_x += leaf_gap
                return x_pos[node_id]

            child_xs = [assign_position(child_id, depth + 1) for child_id in children]
            x_pos[node_id] = sum(child_xs) / len(child_xs)
            return x_pos[node_id]

        for root_id in roots:
            assign_position(root_id, depth=0)

        # 标记终局节点及颜色。
        terminal_result_by_id = {
            state.state_id: result for state, result in self.endings
        }
        node_colors: list[str] = []
        for node_id in sorted(plotted_nodes):
            result = terminal_result_by_id.get(node_id)
            if result is None:
                node_colors.append("#5B8FF9")  # 中间状态
            elif "好人" in result:
                node_colors.append("#52C41A")  # 好人胜
            else:
                node_colors.append("#F5222D")  # 狼人胜或其他终局

        def resolve_plot_font() -> tuple[str, bool]:
            preferred_fonts = [
                "Microsoft YaHei",
                "SimHei",
                "Noto Sans CJK SC",
                "PingFang SC",
                "WenQuanYi Zen Hei",
                "Source Han Sans SC",
                "Arial Unicode MS",
            ]
            available_fonts = {font.name for font in font_manager.fontManager.ttflist}
            for font_name in preferred_fonts:
                if font_name in available_fonts:
                    return font_name, True
            return "DejaVu Sans", False

        plot_font, has_cjk_font = resolve_plot_font()
        title_text = (
            "搜索模拟器状态树" if has_cjk_font else "Search Simulator State Tree"
        )
        xlabel_text = "深度" if has_cjk_font else "Depth"
        ylabel_text = "分支序号" if has_cjk_font else "Branch Order"
        intermediate_label = "中间状态" if has_cjk_font else "Intermediate"
        village_win_label = "好人胜终局" if has_cjk_font else "Village Win"
        wolf_win_label = "狼人胜终局" if has_cjk_font else "Wolf Win"

        max_x = max(x_pos.values()) if x_pos else 0.0
        max_y = max(y_pos.values()) if y_pos else 0.0
        raw_fig_width = max(16.0, max_x * 0.5 + 10.0)
        raw_fig_height = max(10.0, max_y * 1.0 + 6.0)
        fig_width = min(raw_fig_width, max(8.0, self.max_plot_width_inches))
        fig_height = min(raw_fig_height, max(6.0, self.max_plot_height_inches))
        dpi = min(max(self.plot_dpi, 72), 220)
        if fig_width < raw_fig_width or fig_height < raw_fig_height:
            logger.warning(
                "绘图尺寸从 %.2fx%.2f 英寸裁剪为 %.2fx%.2f 英寸，避免图形后端内存/像素限制",
                raw_fig_width,
                raw_fig_height,
                fig_width,
                fig_height,
            )
        with plt.rc_context({"font.family": plot_font, "axes.unicode_minus": False}):
            fig, ax = plt.subplots(
                figsize=(fig_width, fig_height), constrained_layout=True
            )

            # 先画边，再画点，保证节点覆盖在线条上方。
            for node_id in sorted(plotted_nodes):
                parent_id = self.state_parent_index.get(node_id)
                if parent_id is None or parent_id not in plotted_nodes:
                    continue
                ax.plot(
                    [x_pos[parent_id], x_pos[node_id]],
                    [y_pos[parent_id], y_pos[node_id]],
                    color="#BFBFBF",
                    linewidth=0.8,
                    zorder=1,
                )

            sorted_nodes = sorted(plotted_nodes)
            ax.scatter(
                [x_pos[node_id] for node_id in sorted_nodes],
                [y_pos[node_id] for node_id in sorted_nodes],
                s=24,
                c=node_colors,
                edgecolors="#333333",
                linewidths=0.3,
                zorder=2,
            )

            def wrap_action_text(
                text: str, *, width: int = 14, max_lines: int = 4
            ) -> str:
                normalized = text.strip() or ("未知动作" if has_cjk_font else "Unknown")
                wrapped_lines = textwrap.wrap(
                    normalized,
                    width=width,
                    break_long_words=False,
                    break_on_hyphens=False,
                )
                if not wrapped_lines:
                    wrapped_lines = [normalized]
                if len(wrapped_lines) > max_lines:
                    wrapped_lines = wrapped_lines[:max_lines]
                    last_line = wrapped_lines[-1]
                    wrapped_lines[-1] = (
                        f"{last_line[: max(1, width - 3)]}..."
                        if len(last_line) >= width
                        else f"{last_line}..."
                    )
                return "\n".join(wrapped_lines)

            def format_player_status_text(node_id: int) -> str:
                statuses = self.state_players_snapshot.get(node_id, [])
                if not statuses:
                    return "无" if has_cjk_font else "N/A"
                return wrap_action_text(", ".join(statuses), width=18, max_lines=5)

            # 节点标签展示 state_id + 父节点行动（即到达该节点时的动作）。
            # 标签固定在节点正下方，水平居中，不做水平偏移。
            def label_offset_for_node(node_id: int) -> tuple[int, int]:
                _ = node_id
                return 0, -18

            for node_id in sorted_nodes:
                action_label = self.state_action_index.get(node_id, "")
                if node_id in roots:
                    action_text = "根状态" if has_cjk_font else "Root"
                else:
                    action_text = wrap_action_text(action_label)
                is_leaf = len(children_map.get(node_id, [])) == 0
                if is_leaf:
                    result_text = terminal_result_by_id.get(
                        node_id,
                        "未结束" if has_cjk_font else "Ongoing",
                    )
                    player_text = format_player_status_text(node_id)
                    node_text = (
                        f"#{node_id}\n行动:\n{action_text}\n存活状态:\n{player_text}\n对局结果:\n{wrap_action_text(result_text)}"
                        if has_cjk_font
                        else f"#{node_id}\nParentAction:\n{action_text}\nAliveState:\n{player_text}\nResult:\n{wrap_action_text(result_text)}"
                    )
                else:
                    player_text = format_player_status_text(node_id)
                    node_text = (
                        f"#{node_id}\n行动:\n{action_text}\n存活状态:\n{player_text}"
                        if has_cjk_font
                        else f"#{node_id}\nParentAction:\n{action_text}\nAliveState:\n{player_text}"
                    )
                x_offset, y_offset = label_offset_for_node(node_id)
                ax.annotate(
                    node_text,
                    xy=(x_pos[node_id], y_pos[node_id]),
                    xytext=(x_offset, y_offset),
                    textcoords="offset points",
                    fontsize=5.1,
                    va="top",
                    ha="center",
                    color="#222222",
                    linespacing=1.25,
                    bbox={
                        "boxstyle": "round,pad=0.2",
                        "facecolor": "#FFFFFF",
                        "edgecolor": "#999999",
                        "linewidth": 0.5,
                        "alpha": 0.85,
                    },
                    zorder=3,
                )

            legend_handles = [
                Line2D(
                    [0],
                    [0],
                    marker="o",
                    color="w",
                    markerfacecolor="#5B8FF9",
                    markersize=6,
                    label=intermediate_label,
                ),
                Line2D(
                    [0],
                    [0],
                    marker="o",
                    color="w",
                    markerfacecolor="#52C41A",
                    markersize=6,
                    label=village_win_label,
                ),
                Line2D(
                    [0],
                    [0],
                    marker="o",
                    color="w",
                    markerfacecolor="#F5222D",
                    markersize=6,
                    label=wolf_win_label,
                ),
            ]
            ax.legend(handles=legend_handles, loc="upper right", fontsize=8)

            ax.set_title(title_text, fontsize=12)
            axis_branch_text = "分支序号" if has_cjk_font else "Branch Order"
            axis_depth_text = "深度（TD）" if has_cjk_font else "Depth (TD)"
            ax.set_xlabel(axis_branch_text, fontsize=10)
            ax.set_ylabel(axis_depth_text, fontsize=10)
            ax.set_yticks(
                [depth * depth_gap for depth in range(int(max_y / depth_gap) + 1)]
            )
            ax.grid(axis="y", linestyle="--", linewidth=0.5, alpha=0.3)
            ax.invert_yaxis()

            output_path = Path("search_tree.png")
            fig.savefig(output_path, dpi=dpi)
            plt.close(fig)
        logger.info("状态树图已保存到: %s", output_path)
