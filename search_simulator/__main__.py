import sys
import os
import logging
import random
import time
import argparse

""" 基于BFS模拟狼人杀游戏的结果"""
logging.basicConfig(
    level=logging.DEBUG,
    format=r"[%(asctime)s.%(msecs)03d][%(pathname)s:%(lineno)d][%(levelname)s]"
    + os.linesep
    + r"%(message)s"
    + os.linesep,
)
logger = logging.getLogger(__name__)

if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="BFS Simulator")
    parser.add_argument(
        "-s",
        "--seed",
        type=int,
        default=time.time_ns() % (2**32 - 1),
        help="随机种子",
    )
    parser.add_argument(
        "-n",
        "--number_of_players",
        type=int,
        default=5,
        help="玩家人数(5-16)",
    )
    parser.add_argument(
        "-w",
        "--number_of_wolves",
        type=int,
        default=1,
        help="狼人数量",
    )
    parser.add_argument(
        "--include_seer", action="store_true", help="是否包含预言家（默认不包含）"
    )
    parser.add_argument(
        "--include_witch", action="store_true", help="是否包含女巫（默认不包含）"
    )
    parser.add_argument(
        "--include_guard", action="store_true", help="是否包含守卫（默认不包含）"
    )
    parser.add_argument(
        "--include_hunter", action="store_true", help="是否包含猎人（默认不包含）"
    )
    parser.add_argument(
        "--include_white_werewolf_king",
        action="store_true",
        help="是否包含白狼王（默认不包含）",
    )
    parser.add_argument(
        "--include_sheriff",
        action="store_true",
        help="是否启用警长归票机制（默认不启用）",
    )
    parser.add_argument(
        "--search_mode",
        type=str,
        choices=["dfs", "bfs"],
        default="dfs",
        help="搜索模式：dfs(默认) 或 bfs",
    )
    parser.add_argument(
        "--max_processed_states",
        type=int,
        default=None,
        help="最多处理的状态节点数（默认不限）",
    )
    parser.add_argument(
        "--max_queue_size",
        type=int,
        default=None,
        help="BFS 队列最大长度，超出后新状态会被裁剪（默认不限）",
    )
    parser.add_argument(
        "--max_runtime_seconds",
        type=float,
        default=None,
        help="最大运行时长（秒），到达后提前停止（默认不限）",
    )
    parser.add_argument(
        "--max_night_branches_per_state",
        type=int,
        default=None,
        help="单个状态夜晚阶段最多保留分支数（默认不限）",
    )
    parser.add_argument(
        "--max_day_branches_per_state",
        type=int,
        default=None,
        help="单个状态白天阶段最多保留分支数（默认不限）",
    )
    parser.add_argument(
        "--gc_interval",
        type=int,
        default=2000,
        help="每处理多少个状态主动触发一次 gc.collect（默认 2000）",
    )
    args: argparse.Namespace = parser.parse_args()
    try:
        from ._simulator import BFS_Simulator
    except ImportError:
        from _simulator import BFS_Simulator

    simulator = BFS_Simulator(
        random_seed=args.seed,
        number_of_players=args.number_of_players,
        number_of_wolves=args.number_of_wolves,
        include_seer=args.include_seer,
        include_witch=args.include_witch,
        include_guard=args.include_guard,
        include_hunter=args.include_hunter,
        include_white_werewolf_king=args.include_white_werewolf_king,
        include_sheriff=args.include_sheriff,
        search_mode=args.search_mode,
        max_processed_states=args.max_processed_states,
        max_queue_size=args.max_queue_size,
        max_runtime_seconds=args.max_runtime_seconds,
        max_night_branches_per_state=args.max_night_branches_per_state,
        max_day_branches_per_state=args.max_day_branches_per_state,
        gc_interval=args.gc_interval,
    )
    simulator.run()
