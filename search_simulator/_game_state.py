from dataclasses import dataclass, field

try:
    from search_simulator._player import Player
except ImportError:
    from ._player import Player


@dataclass
class GameState:
    """游戏状态类，表示狼人杀游戏的一个状态节点"""

    players: list[Player]
    """ 玩家列表，包含所有参与游戏的角色对象"""
    is_game_over: bool = False
    """ 游戏是否结束"""
    night_count: int = 0
    """ 已完成的夜晚次数（首夜为 0）"""
    day_count: int = 0
    """ 已完成的白天次数（首日为 0）"""
    phase: str = "night"
    """ 当前游戏时间："day" 或 "night" """
    last_guard_target_index: int | None = None
    """ 上一晚守卫守护的玩家索引"""
    # 预言家查验缓存：目标索引 -> 是否狼人阵营。
    seer_check_results: dict[int, bool] | None = None
    """ 预言家查验缓存，预言家淘汰时清空。"""
    reward_interval: tuple[float, float] | None = None
    """ 计算后的 reward 区间 (lower, upper)，由在线算法写回。"""
    action_label: str = ""
    """ 进入该状态的动作标签（从 state_action_index 迁移）。"""
    players_snapshot: list[str] = field(default_factory=list)
    """ 存活快照展示文本（从 state_players_snapshot 迁移）。"""
    state_id: int = -1
    """ 当前状态节点 ID"""
    parent_state_id: int | None = None
    """ 父状态节点 ID"""
    depth: int = 0
    """ 当前分支节点深度（根节点为 0）"""

    def to_dict(self) -> dict:
        """序列化为 JSON 友好的字典（供 GUI/CLI 自定义起始状态）。"""
        return {
            "phase": self.phase,
            "night_count": self.night_count,
            "day_count": self.day_count,
            "last_guard_target_index": self.last_guard_target_index,
            "seer_check_results": (
                {str(k): v for k, v in self.seer_check_results.items()}
                if self.seer_check_results is not None
                else None
            ),
            "players": [
                {
                    "role": player.role,
                    "is_alive": player.is_alive,
                    "skills": dict(player.skills),
                }
                for player in self.players
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GameState":
        """从字典反序列化（GUI/CLI 自定义起始状态；parent=None、depth=0）。"""
        players = [
            Player(
                role=str(p.get("role", "村民")),
                is_alive=bool(p.get("is_alive", True)),
                skills={str(k): int(v) for k, v in (p.get("skills") or {}).items()},
            )
            for p in (data.get("players") or [])
        ]
        seer = data.get("seer_check_results")
        guard = data.get("last_guard_target_index")
        return cls(
            players=players,
            phase=str(data.get("phase", "night")),
            night_count=int(data.get("night_count", 0)),
            day_count=int(data.get("day_count", 0)),
            last_guard_target_index=(int(guard) if guard is not None else None),
            seer_check_results=(
                {int(k): bool(v) for k, v in seer.items()} if seer else None
            ),
            parent_state_id=None,
            depth=0,
        )

    def clone(self) -> "GameState":
        """手动深拷贝。

        用显式字段拷贝替代 ``copy.deepcopy``：CPython 3.14.0 的 ``copy.deepcopy``
        在长时间/多线程下会在 ``_keep_alive`` 触发 use-after-free（Windows access
        violation）。这里只复制可变容器（players/skills/seer_check_results/
        players_snapshot），标量字段直接共享。
        """
        return GameState(
            players=[
                Player(role=p.role, is_alive=p.is_alive, skills=dict(p.skills))
                for p in self.players
            ],
            is_game_over=self.is_game_over,
            night_count=self.night_count,
            day_count=self.day_count,
            phase=self.phase,
            last_guard_target_index=self.last_guard_target_index,
            seer_check_results=(
                dict(self.seer_check_results)
                if self.seer_check_results is not None
                else None
            ),
            reward_interval=self.reward_interval,
            action_label=self.action_label,
            players_snapshot=list(self.players_snapshot),
            state_id=self.state_id,
            parent_state_id=self.parent_state_id,
            depth=self.depth,
        )
