import sys
import os
import logging
from collections import deque
from random import Random
import time
import copy
import json

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
        self.endings = []  # 存储游戏结束的状态
        self.reload(**kwargs)

    def reload(self, **kwargs):
        """重新加载配置"""
        logger.debug("加载BFS Simulator配置")
        self.random: Random = Random(
            int(kwargs.get("random_seed", time.time_ns() % (2**32 - 1)))
        )  # 初始化随机数生成器
        self.players: list = []  # 初始化角色列表
        if kwargs.get("include_seer", False):
            self.players.append(
                Player(role="seer", is_alive=True, skills={"check": -1})
            )
            self.has_clergies = True
        if kwargs.get("include_witch", False):
            self.players.append(
                Player(role="witch", is_alive=True, skills={"potion": 1, "poison": 1})
            )
            self.has_clergies = True
        if kwargs.get("include_guard", False):
            self.players.append(
                Player(role="guard", is_alive=True, skills={"protect": -1})
            )
            self.has_clergies = True
        if kwargs.get("include_hunter", False):
            self.players.append(
                Player(role="hunter", is_alive=True, skills={"shoot": 1})
            )
            self.has_clergies = True
        if kwargs.get("include_white_werewolf_king", False):
            self.players.append(
                Player(
                    role="white_werewolf_king", is_alive=True, skills={"down_kill": 1}
                )
            )
        if kwargs.get("number_of_wolves", 1) > 0:
            self.players.extend(
                [
                    Player(role="werewolf", is_alive=True, skills={"attack": -1})
                    for _ in range(kwargs.get("number_of_wolves", 1))
                ]
            )
        if kwargs.get("number_of_players", 5) > 0:
            self.players.extend(
                [
                    Player(role="villager", is_alive=True, skills={})
                    for _ in range(
                        kwargs.get("number_of_players", 5) - len(self.players)
                    )
                ]
            )  # 添加普通村民角色
        logger.debug(f"角色列表: {[player.role for player in self.players]}")
        self.queue: deque = deque(
            [GameState(players=self.players, is_game_over=False)]
        )  # 初始化队列

    def check_game_over(self, game_state: GameState) -> tuple[bool, str]:
        """检查游戏是否结束"""
        alive_players = [player for player in game_state.players if player.is_alive]
        alive_roles = [player.role for player in alive_players]
        if "werewolf" not in alive_roles:
            return True, "好人阵营胜利"  # 村民胜利
        alive_werewolves = [
            player for player in alive_players if player.role == "werewolf"
        ]

        if len(alive_werewolves) >= len(alive_players) / 2:
            return True, "狼人阵营胜利（人数过半）"  # 狼人胜利
        alive_clergies = [
            player
            for player in alive_players
            if player.role in ["seer", "witch", "guard", "hunter"]
        ]
        if self.has_clergies and not alive_clergies:
            return True, "狼人阵营胜利（神职角色已被消灭）"  # 屠边规则
        alive_villagers = [
            player for player in alive_players if player.role == "villager"
        ]
        if not alive_villagers:
            return True, "狼人阵营胜利（村民已被消灭）"  # 屠边规则
        return False, "未结束"  # 游戏继续

    def run(self):
        logger.debug("开始运行BFS Simulator")
        werewolf_win_counter = 0
        villager_win_counter = 0
        while self.queue:
            current_state = self.queue.popleft()
            # bfs模拟淘汰放逐
            alive_players = [
                player for player in current_state.players if player.is_alive
            ]
            alive_werewolves = [
                player
                for player in alive_players
                if player.role == "werewolf" or player.role == "white_werewolf_king"
            ]
            alive_villagers = [
                player for player in alive_players if player.role != "werewolf"
            ]
            for alive_player in alive_players:
                # 模拟淘汰
                new_state = copy.deepcopy(current_state)
                for player in new_state.players:
                    if player.role == alive_player.role and player.is_alive:
                        player.is_alive = False
                        break
                is_over, result = self.check_game_over(new_state)
                new_state.is_game_over = is_over
                if is_over:
                    logger.debug(f"游戏结束: {result}")
                    self.endings.append((new_state, result))
                    if "狼人阵营胜利" in result:
                        werewolf_win_counter += 1
                    else:
                        villager_win_counter += 1
                else:
                    self.queue.append(new_state)

        # 保存游戏结束状态到文件
        with open("endings.json", "w", encoding="utf-8") as f:
            json.dump(
                [
                    {"state": str(state), "result": result}
                    for state, result in self.endings
                ],
                f,
                ensure_ascii=False,
                indent=4,
            )

        logger.info(f"狼人胜利次数: {werewolf_win_counter}")
        logger.info(f"好人胜利次数: {villager_win_counter}")
