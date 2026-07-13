from dataclasses import dataclass

try:
    from player import Player
except ImportError:
    from .player import Player


@dataclass
class GameState:
    players: list[Player]  # 玩家列表
    is_game_over: bool = False  # 游戏是否结束
    night_count: int = 0  # 已完成的夜晚次数（首夜为 0）
    last_guard_target_index: int | None = None  # 上一晚守卫守护的玩家索引
    state_id: int = -1  # 当前状态节点 ID
    parent_state_id: int | None = None  # 父状态节点 ID
