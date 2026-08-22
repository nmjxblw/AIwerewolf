"""Search Simulator 树分支算法的轻量验收脚本。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from search_simulator import SearchSimulator
from search_simulator._interval import RewardInterval
from search_simulator._interval import interval_branch_color
from search_simulator._positions import build_role_roster
from search_simulator._positions import enumerate_position_layouts


def main() -> int:
    checks: list[tuple[str, bool]] = []
    roster = build_role_roster(
        number_of_players=7,
        number_of_wolves=2,
        include_seer=True,
        include_witch=True,
        include_guard=True,
        include_idiot=False,
        include_hunter=False,
        include_white_werewolf_king=False,
    )
    checks.append(("7P/2W/Seer/Witch/Guard unique positions = 1260", len(enumerate_position_layouts(roster)) == 1260))

    results = {}
    for mode in ("bfs", "dfs"):
        simulator = SearchSimulator(
            number_of_players=4,
            number_of_wolves=1,
            include_seer=False,
            include_witch=False,
            include_guard=False,
            tactics="",
            search_mode=mode,
            persistence_enabled=False,
        )
        results[mode] = simulator.run(start_state=simulator.initial_state)
    keys = ("state_count", "edge_count", "good_paths", "wolf_paths", "wide_interval")
    checks.append(
        (
            "BFS and DFS are result-equivalent",
            all(results["bfs"][key] == results["dfs"][key] for key in keys),
        )
    )
    checks.append(
        (
            "vote branches retain multiplicity",
            any(edge["multiplicity"] > 1 for edge in results["bfs"]["edges"]),
        )
    )
    checks.extend(
        [
            (
                "good interval uses blue",
                interval_branch_color(RewardInterval(-0.2, 0.8)) == "#2563EB",
            ),
            (
                "wolf interval uses red",
                interval_branch_color(RewardInterval(-0.8, 0.2)) == "#DC2626",
            ),
            (
                "balanced interval uses black",
                interval_branch_color(RewardInterval(-0.4004, 0.4)) == "#111111",
            ),
        ]
    )

    failures = 0
    for label, passed in checks:
        print(f"{'PASS' if passed else 'FAIL'}  {label}")
        failures += int(not passed)
    print(f"\n{len(checks) - failures} PASS, {failures} FAIL")
    return int(failures > 0)


if __name__ == "__main__":
    sys.exit(main())
