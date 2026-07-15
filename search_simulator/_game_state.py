from dataclasses import dataclass

try:
    from search_simulator._player import Player
except ImportError:
    from ._player import Player


@dataclass
class GameState:
    """游戏状态类，表示狼人杀游戏的一个状态节点"""

    players: list[Player]  # 玩家列表
    """ 玩家列表，包含所有参与游戏的角色对象"""
    is_game_over: bool = False  # 游戏是否结束
    """ 游戏是否结束"""
    night_count: int = 0  # 已完成的夜晚次数（首夜为 0）
    """ 已完成的夜晚次数（首夜为 0）"""
    day_count: int = 0  # 已完成的白天次数（首日为 0）
    """ 已完成的白天次数（首日为 0）"""
    last_guard_target_index: int | None = None  # 上一晚守卫守护的玩家索引
    """ 上一晚守卫守护的玩家索引"""
    state_id: int = -1  # 当前状态节点 ID
    """ 当前状态节点 ID"""
    parent_state_id: int | None = None  # 父状态节点 ID
    """ 父状态节点 ID"""
