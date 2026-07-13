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
    )
    simulator.run()
