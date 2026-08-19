"""投票可行性判定与 Dinic 最大流。

把「智能投票剪枝」用到的有下界流网络与 Dinic 最大流独立成模块，
全部使用显式传参（不依赖外部可变状态），并保持中文注释。

- ``bounded_vote_flow_feasible``：判断给定「每人一票 + 目标票数上下界」是否可行。
- ``vote_outcome_is_feasible``：判断某组候选人在给定票数约束下能否同时成为最高票。
- ``_flow_*``：有下界流 -> 无下界流（增设超级源/汇）转换 + Dinic 增广原语。
"""

from __future__ import annotations

from collections import deque


def _flow_add_capacity_edge(graph, capacity, from_node, to_node, cap):
    """加一条容量边（用于 Dinic 邻接表 + 容量矩阵）。"""
    if capacity[from_node][to_node] == 0 and capacity[to_node][from_node] == 0:
        graph[from_node].append(to_node)
        graph[to_node].append(from_node)
    capacity[from_node][to_node] += cap


def _flow_add_bounded_edge(graph, capacity, balance, from_node, to_node, lower, upper):
    """加一条带下界/上界的边，并维护节点供需 balance。"""
    if upper < lower:
        return
    balance[from_node] -= lower
    balance[to_node] += lower
    _flow_add_capacity_edge(graph, capacity, from_node, to_node, upper - lower)


def _flow_bfs_level(graph, capacity, node_count, super_source):
    """Dinic BFS 分层。"""
    level = [-1] * node_count
    level[super_source] = 0
    queue = deque([super_source])
    while queue:
        node = queue.popleft()
        for next_node in graph[node]:
            if level[next_node] < 0 and capacity[node][next_node] > 0:
                level[next_node] = level[node] + 1
                queue.append(next_node)
    return level


def _flow_dfs(node, flow, level, cursor, graph, capacity, super_sink):
    """Dinic DFS 增广（迭代 + 显式栈，规避 CPython 3.14 递归特化缺陷）。"""
    stack = [(node, flow)]
    path_edges: list[tuple[int, int]] = []
    while stack:
        current, cur_flow = stack[-1]
        if current == super_sink:
            pushed = cur_flow
            for u, v in reversed(path_edges):
                capacity[u][v] -= pushed
                capacity[v][u] += pushed
            return pushed

        advanced = False
        while cursor[current] < len(graph[current]):
            nxt = graph[current][cursor[current]]
            if level[nxt] == level[current] + 1 and capacity[current][nxt] > 0:
                path_edges.append((current, nxt))
                next_flow = cur_flow
                if capacity[current][nxt] < next_flow:
                    next_flow = capacity[current][nxt]
                stack.append((nxt, next_flow))
                advanced = True
                break
            cursor[current] += 1

        if not advanced:
            stack.pop()
            if path_edges:
                path_edges.pop()
            if stack:
                cursor[stack[-1][0]] += 1
    return 0


def bounded_vote_flow_feasible(
    voter_indices: list[int],
    target_indices: list[int],
    allowed_targets_by_voter: dict[int, list[int]],
    target_lower: dict[int, int],
    target_upper: dict[int, int],
) -> bool:
    """判断「每人投一票、目标票数在上下界内」的票型是否存在（有下界流）。"""
    voter_count = len(voter_indices)
    target_count = len(target_indices)
    source = 0
    first_voter = 1
    first_target = first_voter + voter_count
    sink = first_target + target_count
    super_source = sink + 1
    super_sink = sink + 2
    node_count = super_sink + 1
    target_node_by_index = {
        target_index: first_target + offset
        for offset, target_index in enumerate(target_indices)
    }
    graph: list[list[int]] = [[] for _ in range(node_count)]
    capacity: list[list[int]] = [
        [0 for _ in range(node_count)] for _ in range(node_count)
    ]
    balance = [0 for _ in range(node_count)]

    for offset, voter_index in enumerate(voter_indices):
        voter_node = first_voter + offset
        _flow_add_bounded_edge(graph, capacity, balance, source, voter_node, 1, 1)
        for target_index in allowed_targets_by_voter.get(voter_index, []):
            target_node = target_node_by_index[target_index]
            _flow_add_bounded_edge(
                graph, capacity, balance, voter_node, target_node, 0, 1
            )

    for target_index in target_indices:
        _flow_add_bounded_edge(
            graph,
            capacity,
            balance,
            target_node_by_index[target_index],
            sink,
            target_lower[target_index],
            target_upper[target_index],
        )

    _flow_add_bounded_edge(
        graph, capacity, balance, sink, source, 0, len(voter_indices)
    )

    required_flow = 0
    for bal_node, node_balance in enumerate(balance):
        if node_balance > 0:
            _flow_add_capacity_edge(
                graph, capacity, super_source, bal_node, node_balance
            )
            required_flow += node_balance
        elif node_balance < 0:
            _flow_add_capacity_edge(
                graph, capacity, bal_node, super_sink, -node_balance
            )

    max_flow = 0
    while True:
        level = _flow_bfs_level(graph, capacity, node_count, super_source)
        if level[super_sink] < 0:
            break
        cursor = [0] * node_count
        while True:
            pushed = _flow_dfs(
                super_source, 10**9, level, cursor, graph, capacity, super_sink
            )
            if pushed == 0:
                break
            max_flow += pushed
            if max_flow == required_flow:
                return True
    return max_flow == required_flow


def vote_outcome_is_feasible(
    alive_indices: list[int],
    allowed_targets_by_voter: dict[int, list[int]],
    top_candidates: tuple[int, ...],
) -> bool:
    """判断 top_candidates 能否同时成为最高票（遍历可能的最大票数 max_votes）。"""
    alive_count = len(alive_indices)
    top_candidate_set = set(top_candidates)
    non_top_count = alive_count - len(top_candidates)
    for max_votes in range(1, alive_count + 1):
        if len(top_candidates) * max_votes > alive_count:
            continue
        if alive_count > len(top_candidates) * max_votes + non_top_count * (
            max_votes - 1
        ):
            continue
        target_lower = {
            index: max_votes if index in top_candidate_set else 0
            for index in alive_indices
        }
        target_upper = {
            index: max_votes if index in top_candidate_set else max_votes - 1
            for index in alive_indices
        }
        if bounded_vote_flow_feasible(
            alive_indices,
            alive_indices,
            allowed_targets_by_voter,
            target_lower,
            target_upper,
        ):
            return True
    return False
