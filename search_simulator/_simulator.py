import sys
import os
import logging
from collections import deque
from itertools import combinations
from random import Random
import time
import copy
import json
import gc

try:
    from .player import Player
except ImportError:
    from player import Player

try:
    from .game_state import GameState
except ImportError:
    from game_state import GameState

logger = logging.getLogger(__name__)


class BFS_Simulator:
    def __init__(self, **kwargs):
        logger.debug("开始初始化BFS Simulator")
        self.has_clergies = False  # 标记是否包含神职角色
        self.include_sheriff = False  # 标记是否启用警长归票机制
        self.endings: list[tuple[GameState, str]] = []  # 存储游戏结束的状态
        self.reload(**kwargs)

    def reload(self, **kwargs):
        """重新加载配置"""
        logger.debug("加载BFS Simulator配置")
        # 这两个集合分别用于：过滤待展开的重复局面，以及去重已经收敛的终局。
        self.has_clergies = False
        self.include_sheriff = bool(kwargs.get("include_sheriff", False))
        self.visited_states: set = set()
        self.ending_signatures: set = set()
        self.state_parent_index: dict[int, int | None] = {}
        self.state_action_index: dict[int, str] = {}
        self._next_state_id = 0
        self.max_processed_states: int | None = kwargs.get("max_processed_states")
        self.max_queue_size: int | None = kwargs.get("max_queue_size")
        self.max_runtime_seconds: float | None = kwargs.get("max_runtime_seconds")
        self.search_mode: str = str(kwargs.get("search_mode", "dfs")).lower()
        if self.search_mode not in {"dfs", "bfs"}:
            self.search_mode = "dfs"
        self.max_night_branches_per_state: int | None = kwargs.get(
            "max_night_branches_per_state"
        )
        self.max_day_branches_per_state: int | None = kwargs.get(
            "max_day_branches_per_state"
        )
        self.gc_interval: int = int(kwargs.get("gc_interval", 2000))
        self.pruned_by_limits: int = 0
        self.stop_reason: str = "completed"
        self.random: Random = Random(
            int(kwargs.get("random_seed", time.time_ns() % (2**32 - 1)))
        )  # 初始化随机数生成器
        self.players: list = []  # 初始化角色列表
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
            initial_state, parent_state_id=None, action_label="根节点"
        )
        self.queue: deque[GameState] = deque([initial_state])  # 初始化队列
        self.visited_states.add(self._state_signature(initial_state))

    def _assign_state_identity(
        self,
        game_state: GameState,
        *,
        parent_state_id: int | None,
        action_label: str,
    ) -> None:
        game_state.state_id = self._next_state_id
        game_state.parent_state_id = parent_state_id
        self.state_parent_index[game_state.state_id] = parent_state_id
        self.state_action_index[game_state.state_id] = action_label
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
        if skill_name not in player.skills:
            return
        skill_value = player.skills[skill_name]
        if skill_value > 0:
            player.skills[skill_name] = skill_value - 1

    def _state_signature(self, game_state: GameState) -> str:
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

    def _apply_deaths_with_chain(
        self, game_state: GameState, death_indices: list[int]
    ) -> list[GameState]:
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
        if player_index < 0 or player_index >= len(game_state.players):
            return
        game_state.players[player_index].is_alive = False

    def _resolve_death_chain(
        self, game_state: GameState, player_index: int
    ) -> list[GameState]:
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
                        guard_text = "无" if guard_target is None else str(guard_target)
                        if witch_action == "毒杀" and poison_target_index is not None:
                            witch_text = f"毒杀→{poison_target_index}"
                        elif witch_action == "救活":
                            witch_text = "救活"
                        else:
                            witch_text = "无"
                        action_label = (
                            f"夜晚 狼刀→{wolf_target_index};"
                            f" 守卫→{guard_text};"
                            f" 女巫={witch_text};"
                            f" 死亡={sorted(set(deaths))}"
                        )
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

    def check_game_over(self, game_state: GameState) -> tuple[bool, str]:
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

    def run(self):
        logger.debug(f"开始运行 Simulator (search_mode={self.search_mode})")
        wins = {}
        start_time = time.monotonic()
        processed_states = 0
        while self.queue:
            if (
                self.max_runtime_seconds is not None
                and time.monotonic() - start_time >= self.max_runtime_seconds
            ):
                self.stop_reason = "max_runtime_seconds_reached"
                break
            if (
                self.max_processed_states is not None
                and processed_states >= self.max_processed_states
            ):
                self.stop_reason = "max_processed_states_reached"
                break

            # DFS 使用栈顶弹出，BFS 使用队头弹出；DFS 在大分支场景下通常内存压力更小。
            if self.search_mode == "dfs":
                current_state = self.queue.pop()
            else:
                current_state = self.queue.popleft()
            processed_states += 1
            if current_state.is_game_over:
                continue

            # 先展开夜晚，再展开白天；每个新局面都先做去重，避免重复分支刷爆队列。
            for night_state in self._resolve_night(current_state):
                for day_state in self._resolve_day_vote(night_state):
                    is_over, result = self.check_game_over(day_state)
                    day_state.is_game_over = is_over
                    if is_over:
                        # 终局也按状态指纹去重，避免同一盘面因为不同路径被重复统计。
                        ending_signature = self._state_signature(day_state)
                        if ending_signature in self.ending_signatures:
                            continue
                        self.ending_signatures.add(ending_signature)
                        # logger.debug(f"游戏结束: {result}")
                        self.endings.append((day_state, result))
                        wins[result] = wins.get(result, 0) + 1
                    else:
                        # 只把未访问过的中间状态继续推进到下一轮 BFS。
                        state_signature = self._state_signature(day_state)
                        if state_signature in self.visited_states:
                            continue
                        self.visited_states.add(state_signature)
                        if (
                            self.max_queue_size is not None
                            and len(self.queue) >= self.max_queue_size
                        ):
                            self.pruned_by_limits += 1
                            continue
                        self.queue.append(day_state)

            if self.gc_interval > 0 and processed_states % self.gc_interval == 0:
                gc.collect()

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
        for result, count in sorted(wins.items()):
            msg += f"{result:<50} \t次数: {count:>5}\n"
        msg += (
            f"搜索模式: {self.search_mode}\n"
            f"停止原因: {self.stop_reason}\n"
            f"已处理状态数: {processed_states}\n"
            f"当前待处理容器长度: {len(self.queue)}\n"
            f"因阈值裁剪分支数: {self.pruned_by_limits}\n"
            f"运行耗时(秒): {time.monotonic() - start_time:.2f}\n"
        )

        logger.info(f"游戏结束统计:\n{msg}")
