from dataclasses import dataclass


@dataclass
class Player:
    role: str
    """ 玩家角色 """
    is_alive: bool
    """ 玩家是否存活 """
    skills: dict
    """ 玩家技能列表, 例如: {"check": -1, "potion": 1, "poison": 1} , 其中 -1 表示无限使用, 1 表示只能使用一次 """
