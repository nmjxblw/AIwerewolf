import copy
import gc
import json
import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations
from pathlib import Path
from typing import Any
from typing import Callable
from typing import cast

from ._game_state import GameState
from ._i18n import t
from ._player import Player
from ._sqlite_lru_signature_store import _SQLiteLRUSignatureStore

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def _flow_add_capacity_edge(graph, capacity, from_node, to_node, cap):
    """流网络辅助：加一条容量边。"""
    if capacity[from_node][to_node] == 0 and capacity[to_node][from_node] == 0:
        graph[from_node].append(to_node)
        graph[to_node].append(from_node)
    capacity[from_node][to_node] += cap


def _flow_add_bounded_edge(graph, capacity, balance, from_node, to_node, lower, upper):
    """流网络辅助：加一条带下界/上界的边。"""
    if upper < lower:
        return
    balance[from_node] -= lower
    balance[to_node] += lower
    _flow_add_capacity_edge(graph, capacity, from_node, to_node, upper - lower)


def _flow_bfs_level(graph, capacity, node_count, super_source):
    """Dinic BFS 分层。"""
    level = [-1] * node_count
    level[super_source] = 0
    queue = deque([super_source])
    while queue:
        node = queue.popleft()
        for next_node in graph[node]:
            if level[next_node] < 0 and capacity[node][next_node] > 0:
                level[next_node] = level[node] + 1
                queue.append(next_node)
    return level


def _flow_dfs(node, flow, level, cursor, graph, capacity, super_sink):
    """Dinic DFS 增广（迭代 + 显式栈）。

    用显式栈替代递归实现，规避 CPython 3.14.0 特化解释器在递归调用上
    触发的 ``_PyEval_EvalFrameDefault: Executing a cache`` 致命错误。
    """
    stack = [(node, flow)]
    path_edges: list[tuple[int, int]] = []
    while stack:
        current, cur_flow = stack[-1]
        if current == super_sink:
            pushed = cur_flow
            for u, v in reversed(path_edges):
                capacity[u][v] -= pushed
                capacity[v][u] += pushed
            return pushed

        advanced = False
        while cursor[current] < len(graph[current]):
            nxt = graph[current][cursor[current]]
            if level[nxt] == level[current] + 1 and capacity[current][nxt] > 0:
                path_edges.append((current, nxt))
                next_flow = cur_flow
                if capacity[current][nxt] < next_flow:
                    next_flow = capacity[current][nxt]
                stack.append((nxt, next_flow))
                advanced = True
                break
            cursor[current] += 1

        if not advanced:
            stack.pop()
            if path_edges:
                path_edges.pop()
            if stack:
                cursor[stack[-1][0]] += 1
    return 0


class SearchSimulator:
    """全树搜索模拟器，用于探索狼人杀游戏的所有可能局面。"""

    def __init__(self, **kwargs):
        logger.debug(t("log.init_start"))
        self.has_clergies = False  # 标记是否包含神职角色
        """ 标记是否包含神职角色 """
        self.include_sheriff = False  # 标记是否启用警长归票机制
        """ 标记是否启用警长归票机制 """
        self.smart_vote = False
        """ 是否启用智能投票剪枝与预言家查验缓存 """
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
        self.state_depth_index: dict[int, int] = {}
        """ 存储每个状态节点的分支深度（根节点为 0） """
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
        self.pruned_by_limits: int = 0
        """ 记录因阈值裁剪分支数 """
        self.stop_reason: str = t("stop.sim_done")
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
            if parent_state_id is None:
                game_state.depth = 0
            else:
                game_state.depth = self.state_depth_index.get(parent_state_id, -1) + 1
            self.state_parent_index[game_state.state_id] = parent_state_id
            self.state_action_index[game_state.state_id] = action_label
            self.state_depth_index[game_state.state_id] = game_state.depth
            players = self._normalize_players(game_state)
            snapshot = [
                f"{index}:{player.role}{'存活' if player.is_alive else '死亡'}"
                for index, player in enumerate(players)
            ]
            game_state.action_label = action_label
            game_state.players_snapshot = snapshot
            self.state_players_snapshot[game_state.state_id] = snapshot
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
                "action_label": self.state_action_index.get(node_id, t("action.unknown")),
            }
            for node_id in id_path
        ]

    def _is_wolf_role(self, role: str) -> bool:
        return role in {"狼人", "白狼王"}

    def _seer_check_results(self, game_state: GameState) -> dict[int, bool]:
        """返回预言家查验缓存，必要时初始化。"""

        if game_state.seer_check_results is None:
            game_state.seer_check_results = {}
        return game_state.seer_check_results

    def _alive_seer_index(self, game_state: GameState) -> int | None:
        seer_indices = self._alive_indices(
            game_state,
            predicate=lambda player: player.role == "预言家"
            and player.skills.get("查验", 0) != 0,
        )
        return seer_indices[0] if seer_indices else None

    def _alive_indices(
        self,
        game_state: GameState,
        *,
        exclude_indices: set[int] | None = None,
        predicate: Callable[[Player], bool] | None = None,
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
            game_state.day_count,
            game_state.phase,
            game_state.last_guard_target_index,
            sorted(
                (int(index), bool(is_wolf))
                for index, is_wolf in (game_state.seer_check_results or {}).items()
            ),
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
        if game_state.players[player_index].role == "预言家":
            game_state.seer_check_results = {}

    def _white_wolf_king_boom_targets(
        self, game_state: GameState, player_index: int
    ) -> list[int]:
        targets = self._alive_indices(game_state, exclude_indices={player_index})
        if not self.smart_vote:
            return targets
        return [
            target_index
            for target_index in targets
            if not self._is_wolf_role(game_state.players[target_index].role)
        ]

    def _iter_seer_check_branches(
        self, game_state: GameState
    ) -> list[tuple[GameState, str]]:
        """在 smart_vote 下展开预言家夜晚查验分支。"""

        if not self.smart_vote:
            return [(game_state, "")]
        seer_index = self._alive_seer_index(game_state)
        if seer_index is None:
            return [(game_state, "")]

        known_results = self._seer_check_results(game_state)
        targets = [
            index
            for index in self._alive_indices(game_state, exclude_indices={seer_index})
            if index not in known_results
        ]
        if not targets:
            return [(game_state, "预言家查验=无")]

        branches: list[tuple[GameState, str]] = []
        for target_index in targets:
            checked_state = copy.deepcopy(game_state)
            checked_results = self._seer_check_results(checked_state)
            is_wolf = self._is_wolf_role(checked_state.players[target_index].role)
            checked_results[target_index] = is_wolf
            self._consume_skill(checked_state.players[seer_index], "查验")
            alignment_text = "狼人阵营" if is_wolf else "好人阵营"
            branches.append(
                (checked_state, f"预言家查验→{target_index}({alignment_text})")
            )
        return branches

    def _resolve_death_chain(
        self, game_state: GameState, player_index: int
    ) -> list[GameState]:
        """解析玩家死亡连锁，返回可能的后续分支。"""

        # 死亡连锁：先标记该角色死亡，再按角色类型展开猎人开枪、白狼王爆炸等后续分支。
        if player_index < 0 or player_index >= len(game_state.players):
            return [game_state]

        player = game_state.players[player_index]
        if player.is_alive:
            self._kill_player(game_state, player_index)
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
                targets = self._white_wolf_king_boom_targets(state, player_index)
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

    def _witch_has_antidote(self, game_state: GameState) -> bool:
        """是否存在存活且解药可用的女巫。"""
        return bool(
            self._alive_indices(
                game_state,
                predicate=lambda player: player.role == "女巫"
                and player.skills.get("解药", 0) > 0,
            )
        )

    def _wolf_targets_for_night(self, game_state: GameState) -> list[int]:
        """狼人可选刀口目标（含「骗刀」战术扩展）。"""
        alive = self._alive_indices(game_state)
        targets = [
            index
            for index in alive
            if not self._is_wolf_role(game_state.players[index].role)
        ]
        if "self_kill" in self.tactics and self._witch_has_antidote(game_state):
            targets = list(alive)
        return targets

    def _resolve_night(self, game_state: GameState) -> list[GameState]:
        """解析夜晚阶段，返回可能的后续分支。"""

        # 夜晚阶段：狼人刀人 -> 守卫保护 -> 女巫单药决策 -> 死亡连锁。
        wolf_targets = self._wolf_targets_for_night(game_state)
        if "no_kill" in self.tactics and wolf_targets:
            wolf_targets = list(wolf_targets) + [None]
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

                guard_saved = (
                    wolf_target_index is not None and guard_target == wolf_target_index
                )

                witch_actions: list[tuple[str, int | None]] = [("无", None)]
                if witch_index is not None and base_state.players[witch_index].is_alive:
                    witch_player = base_state.players[witch_index]
                    if wolf_target_index is not None:
                        can_self_save = (
                            wolf_target_index == witch_index
                            and game_state.night_count == 0
                        )
                        can_save = witch_player.skills.get("解药", 0) > 0 and (
                            wolf_target_index != witch_index or can_self_save
                        )
                        if can_save:
                            witch_actions.append(("使用解药", None))

                    if witch_player.skills.get("毒药", 0) > 0:
                        poison_targets = self._alive_indices(
                            base_state, exclude_indices={witch_index}
                        )
                        for poison_target_index in poison_targets:
                            witch_actions.append(("毒杀", poison_target_index))

                for witch_action, poison_target_index in witch_actions:
                    branch_state = copy.deepcopy(base_state)
                    witch_saved = False
                    if witch_index is not None and witch_action == "使用解药":
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
                    if wolf_target_index is None:
                        wolf_kill_applies = False  # 空刀：本夜无狼刀死亡

                    deaths: list[int] = []
                    if wolf_kill_applies and wolf_target_index is not None:
                        deaths.append(wolf_target_index)
                    if witch_action == "毒杀" and poison_target_index is not None:
                        deaths.append(poison_target_index)

                    seer_check_branches = self._iter_seer_check_branches(branch_state)
                    for checked_state, seer_action_text in seer_check_branches:
                        resolved_states = self._apply_deaths_with_chain(
                            checked_state, deaths
                        )
                        for resolved_state in resolved_states:
                            resolved_state.night_count += 1
                            wolf_target_text = (
                                "空刀"
                                if wolf_target_index is None
                                else str(wolf_target_index)
                            )
                            action_parts: list[str] = [
                                f"夜晚 狼刀→{wolf_target_text}"
                            ]
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
                                elif witch_action == "使用解药":
                                    witch_text = "使用解药"
                                else:
                                    witch_text = "无"
                                action_parts.append(f"女巫={witch_text}")
                            if seer_action_text:
                                action_parts.append(seer_action_text)
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
                                and len(night_states)
                                >= self.max_night_branches_per_state
                            ):
                                self.pruned_by_limits += 1
                                return night_states

        return night_states

    def _allowed_vote_targets(
        self, game_state: GameState, voter_index: int, alive_indices: list[int]
    ) -> list[int]:
        targets = [index for index in alive_indices if index != voter_index]
        if not self.smart_vote or game_state.players[voter_index].role != "预言家":
            return targets

        known_results = self._seer_check_results(game_state)
        known_wolves = [
            index
            for index in targets
            if known_results.get(index) is True and game_state.players[index].is_alive
        ]
        if known_wolves:
            return known_wolves

        known_good = {
            index
            for index, is_wolf in known_results.items()
            if not is_wolf and game_state.players[index].is_alive
        }
        return [index for index in targets if index not in known_good]

    def _bounded_vote_flow_feasible(
        self,
        voter_indices: list[int],
        target_indices: list[int],
        allowed_targets_by_voter: dict[int, list[int]],
        target_lower: dict[int, int],
        target_upper: dict[int, int],
    ) -> bool:
        voter_count = len(voter_indices)
        target_count = len(target_indices)
        source = 0
        first_voter = 1
        first_target = first_voter + voter_count
        sink = first_target + target_count
        super_source = sink + 1
        super_sink = sink + 2
        node_count = super_sink + 1
        target_node_by_index = {
            target_index: first_target + offset
            for offset, target_index in enumerate(target_indices)
        }
        graph: list[list[int]] = [[] for _ in range(node_count)]
        capacity: list[list[int]] = [
            [0 for _ in range(node_count)] for _ in range(node_count)
        ]
        balance = [0 for _ in range(node_count)]

        for offset, voter_index in enumerate(voter_indices):
            voter_node = first_voter + offset
            _flow_add_bounded_edge(graph, capacity, balance, source, voter_node, 1, 1)
            for target_index in allowed_targets_by_voter.get(voter_index, []):
                target_node = target_node_by_index[target_index]
                _flow_add_bounded_edge(
                    graph, capacity, balance, voter_node, target_node, 0, 1
                )

        for target_index in target_indices:
            _flow_add_bounded_edge(
                graph,
                capacity,
                balance,
                target_node_by_index[target_index],
                sink,
                target_lower[target_index],
                target_upper[target_index],
            )

        _flow_add_bounded_edge(
            graph, capacity, balance, sink, source, 0, len(voter_indices)
        )

        required_flow = 0
        for bal_node, node_balance in enumerate(balance):
            if node_balance > 0:
                _flow_add_capacity_edge(
                    graph, capacity, super_source, bal_node, node_balance
                )
                required_flow += node_balance
            elif node_balance < 0:
                _flow_add_capacity_edge(
                    graph, capacity, bal_node, super_sink, -node_balance
                )

        max_flow = 0
        while True:
            level = _flow_bfs_level(graph, capacity, node_count, super_source)
            if level[super_sink] < 0:
                break
            cursor = [0] * node_count
            while True:
                pushed = _flow_dfs(
                    super_source, 10**9, level, cursor, graph, capacity, super_sink
                )
                if pushed == 0:
                    break
                max_flow += pushed
                if max_flow == required_flow:
                    return True
        return max_flow == required_flow

    def _vote_outcome_is_feasible(
        self,
        alive_indices: list[int],
        allowed_targets_by_voter: dict[int, list[int]],
        top_candidates: tuple[int, ...],
    ) -> bool:
        alive_count = len(alive_indices)
        top_candidate_set = set(top_candidates)
        non_top_count = alive_count - len(top_candidates)
        for max_votes in range(1, alive_count + 1):
            if len(top_candidates) * max_votes > alive_count:
                continue
            if alive_count > len(top_candidates) * max_votes + non_top_count * (
                max_votes - 1
            ):
                continue
            target_lower = {
                index: max_votes if index in top_candidate_set else 0
                for index in alive_indices
            }
            target_upper = {
                index: max_votes if index in top_candidate_set else max_votes - 1
                for index in alive_indices
            }
            if self._bounded_vote_flow_feasible(
                alive_indices,
                alive_indices,
                allowed_targets_by_voter,
                target_lower,
                target_upper,
            ):
                return True
        return False

    def _build_smart_vote_outcomes(
        self, game_state: GameState, alive_indices: list[int]
    ) -> set[tuple[int, ...]]:
        allowed_targets_by_voter = {
            voter_index: self._allowed_vote_targets(
                game_state, voter_index, alive_indices
            )
            for voter_index in alive_indices
        }
        if any(not targets for targets in allowed_targets_by_voter.values()):
            return set()

        candidate_outcomes: set[tuple[int, ...]] = {
            (player_index,) for player_index in alive_indices
        }
        if self.include_sheriff:
            candidate_outcomes.update(combinations(alive_indices, 2))
            if len(alive_indices) > 2:
                candidate_outcomes.add(tuple(alive_indices))

        return {
            outcome
            for outcome in candidate_outcomes
            if self._vote_outcome_is_feasible(
                alive_indices,
                allowed_targets_by_voter,
                outcome,
            )
        }

    def _resolve_day_vote(self, game_state: GameState) -> list[GameState]:
        """解析白天投票阶段，返回可能的后续分支。"""

        # 白天投票建模：
        # 1) 单人最高票：该玩家直接出局；
        # 2) 启用警长时，平票最高票由警长归票，强制放逐 1 人并展开分支。
        alive_indices = self._alive_indices(game_state)
        if len(alive_indices) <= 1:
            return [copy.deepcopy(game_state)]

        if self.smart_vote:
            vote_outcomes = self._build_smart_vote_outcomes(game_state, alive_indices)
            if not vote_outcomes:
                idle_state = copy.deepcopy(game_state)
                self._assign_state_identity(
                    idle_state,
                    parent_state_id=game_state.state_id,
                    action_label="白天 无有效投票结果",
                )
                return [idle_state]
        else:
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
        logger.debug(t("log.load_config"))
        # 这两个集合分别用于：过滤待展开的重复局面，以及去重已经收敛的终局。
        self.has_clergies = False
        """ 标记是否包含神职角色"""
        self.include_sheriff = bool(kwargs.get("include_sheriff", False))
        """ 标记是否启用警长归票机制"""
        self.smart_vote = bool(kwargs.get("smart_vote", False))
        """ 是否启用智能投票剪枝与预言家查验缓存"""
        self.policy = str(kwargs.get("policy", "exhaustive")).lower()
        """ 运行模式：exhaustive（穷举）或 online（在线参考决策）"""
        self.lambda_risk = float(kwargs.get("lambda_risk", 1.0))
        """ 迭代风险参数 λ ∈ [0,1]"""
        self.toggle = str(kwargs.get("toggle", "conservative")).lower()
        """ 乐观/保守开关：optimistic | conservative"""
        _lookahead = kwargs.get("lookahead_depth")
        self.lookahead_depth = (
            None
            if _lookahead is None
            or (isinstance(_lookahead, (int, float)) and _lookahead < 0)
            else int(_lookahead)
        )
        """ 前瞻深度（决策点计；None=全深度）"""
        _tactics = kwargs.get("tactics")
        self.tactics = (
            {token.strip() for token in str(_tactics).split(",") if token.strip()}
            if _tactics
            else set()
        )
        """ 启用的夜间战术集合：self_kill(骗刀) / no_kill(空刀)"""
        self.online_trace_path = str(
            kwargs.get("online_trace_path", "online_trace.json")
        )
        """ 在线参考轨迹输出路径"""
        self.compare_with_exact = bool(kwargs.get("compare_with_exact", False))
        """ 是否与全深度精确值对照"""
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
        self.state_depth_index = {}
        """ 存储每个状态节点的分支深度（根节点为 0）"""
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
        self.pruned_by_limits = 0
        """ 记录因阈值裁剪分支数"""
        self.stop_reason = t("stop.sim_done")
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
        logger.debug(t("log.roles", [player.role for player in self.players]))
        initial_state = GameState(
            players=copy.deepcopy(self.players), is_game_over=False
        )
        self._assign_state_identity(
            initial_state, parent_state_id=None, action_label=t("action.root")
        )
        self.initial_state = initial_state
        """ 初始根状态（在线模式复用）"""
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
        action_label = self.state_action_index.get(game_state.state_id, t("action.unknown"))
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
            logger.debug(t("log.callback_failed"), exc_info=True)

    def _should_stop_run(self) -> bool:
        """检查是否触发停止条件。"""

        if (
            self.max_runtime_seconds is not None
            and time.monotonic() - self.start_time >= self.max_runtime_seconds
        ):
            self.stop_reason = t("stop.max_runtime")
            return True
        if (
            self.max_processed_states is not None
            and self.processed_states >= self.max_processed_states
        ):
            self.stop_reason = t("stop.max_processed")
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

        logger.debug(t("log.run_start", self.search_mode))
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

    def transition(self, current_game_state: GameState) -> list[GameState]:
        """封装 GameState 迭代更新：按 phase 分发到夜/昼结算。"""
        if current_game_state.phase == "night":
            next_states = self._resolve_night(current_game_state)
            for state in next_states:
                state.phase = "day"
        else:
            next_states = self._resolve_day_vote(current_game_state)
            for state in next_states:
                state.phase = "night"
                state.day_count = current_game_state.day_count + 1
        return next_states

    def run_online(self, start_state: GameState | None = None):
        """运行在线参考决策：从根/自定义状态沿参考路径决策到真终局。"""
        from ._online_policy import (
            evaluate_against_exact,
            emit_online_artifacts,
            run_online_reference,
        )

        if start_state is not None:
            root = copy.deepcopy(start_state)
            self._assign_state_identity(
                root, parent_state_id=None, action_label=t("action.root")
            )
        else:
            root = self.initial_state

        # 神职屠边判定以实际根状态为准（自定义状态可能与 include_* 不一致）
        self.has_clergies = any(
            player.role in {"预言家", "女巫", "守卫", "猎人"}
            for player in root.players
        )

        trace = run_online_reference(self, root, depth=self.lookahead_depth)
        emit_online_artifacts(self, trace)
        if self.compare_with_exact:
            evaluate_against_exact(self, root, trace)
        return trace
