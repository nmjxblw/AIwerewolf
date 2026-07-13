from dataclasses import dataclass


@dataclass
class GameState:
    players: list  # 玩家列表
    is_game_over: bool = False  # 游戏是否结束
    stack = []  # 用于存储游戏状态的栈
