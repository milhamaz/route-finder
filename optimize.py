"""Solve visiting order for a milk-run route (closed loop starting/ending at depot)."""
import math

import networkx as nx
from networkx.algorithms.approximation import traveling_salesman_problem

# Held-Karp exact DP is O(2^n * n^2); ~2.6s at n=18, safe up to ~18 points
# (1 depot + 17 drops) before falling back to the approximation.
EXACT_LIMIT = 18


def _solve_exact(durations: list[list[float]]) -> list[int]:
    n = len(durations)
    size = 1 << n
    dp = [[math.inf] * n for _ in range(size)]
    parent = [[-1] * n for _ in range(size)]
    dp[1][0] = 0.0

    for mask in range(size):
        if not (mask & 1):
            continue
        for j in range(n):
            if not (mask & (1 << j)) or dp[mask][j] == math.inf:
                continue
            cur = dp[mask][j]
            for k in range(n):
                if mask & (1 << k):
                    continue
                nmask = mask | (1 << k)
                cand = cur + durations[j][k]
                if cand < dp[nmask][k]:
                    dp[nmask][k] = cand
                    parent[nmask][k] = j

    full = size - 1
    best_cost, best_j = math.inf, 0
    for j in range(1, n):
        cost = dp[full][j] + durations[j][0]
        if cost < best_cost:
            best_cost, best_j = cost, j

    order = []
    mask, j = full, best_j
    while True:
        order.append(j)
        if j == 0:
            break
        pj = parent[mask][j]
        mask ^= (1 << j)
        j = pj
    order.reverse()
    return order + [0]


def _solve_approx(durations: list[list[float]]) -> list[int]:
    n = len(durations)
    g = nx.complete_graph(n)
    for i in range(n):
        for j in range(n):
            if i != j:
                g[i][j]["weight"] = durations[i][j]

    tour = traveling_salesman_problem(g, cycle=True, weight="weight")
    core = tour[:-1]
    start = core.index(0)
    rotated = core[start:] + core[:start]
    return rotated + [rotated[0]]


def solve_order(durations: list[list[float]]) -> list[int]:
    """durations: NxN matrix (seconds), index 0 = depot. Returns visiting order of indices,
    starting and ending at 0 (round trip). Exact optimum when point count allows it."""
    n = len(durations)
    if n <= EXACT_LIMIT:
        return _solve_exact(durations)
    return _solve_approx(durations)


def cw_clusters(
    dp_local_idx: list[int],
    demand: dict[int, float],
    durations: list[list[float]],
    ceiling: float,
) -> list[tuple[list[int], float]]:
    """Clarke & Wright (1964) savings merge phase only — groups drop points into
    routes bounded by `ceiling` (pass the largest available vehicle's capacity so a
    small vehicle elsewhere in the fleet can't block a merge two big trucks could
    carry). Doesn't assign routes to specific vehicles; see assign_clusters().

    dp_local_idx: local matrix indices of the drop points (depot is always index 0
    in `durations`). demand: local idx -> demand.
    Returns a list of (cluster, total_load) tuples.
    """
    if not dp_local_idx:
        return []

    routes = {i: [i] for i in dp_local_idx}
    load = {i: demand[i] for i in dp_local_idx}
    owner = {i: i for i in dp_local_idx}

    savings = []
    for a in range(len(dp_local_idx)):
        for b in range(a + 1, len(dp_local_idx)):
            i, j = dp_local_idx[a], dp_local_idx[b]
            savings.append((durations[0][i] + durations[0][j] - durations[i][j], i, j))
    savings.sort(key=lambda t: t[0], reverse=True)

    for s, i, j in savings:
        if s <= 0:
            break  # remaining pairs sorted descending — no more beneficial merges
        ri, rj = owner[i], owner[j]
        if ri == rj:
            continue
        route_i, route_j = routes[ri], routes[rj]
        if i not in (route_i[0], route_i[-1]) or j not in (route_j[0], route_j[-1]):
            continue  # only route endpoints can be joined (interior stops are locked in)
        if load[ri] + load[rj] > ceiling:
            continue
        if route_i[0] == i:
            route_i = route_i[::-1]
        if route_j[-1] == j:
            route_j = route_j[::-1]
        merged = route_i + route_j
        routes[ri] = merged
        load[ri] += load[rj]
        del routes[rj], load[rj]
        for node in merged:
            owner[node] = ri

    return [(routes[k], load[k]) for k in routes]


def assign_clusters(
    clusters_with_load: list[tuple[list[int], float]],
    vehicles_ordered: list[tuple[str, float]],
) -> tuple[dict[str, list[int]], list[list[int]]]:
    """Match CW clusters to specific vehicles: biggest cluster first, each one going
    to the highest-priority not-yet-used vehicle (in `vehicles_ordered`'s given order)
    that still has enough capacity — first-fit-by-priority, not tightest-fit. Callers
    control what "priority" means by how they order `vehicles_ordered`: capacity-
    ascending for a plain best-fit (single-wave case), or availability-time-ascending
    when some vehicles are already out on an earlier trip (multi-trip case).

    Returns (assignment, unassigned): assignment maps vehicle_id -> dp local indices
    (caller runs solve_order() per vehicle for the actual visiting order); unassigned
    holds any leftover clusters that didn't fit any vehicle — the caller should
    surface these as an infeasible-selection error rather than silently dropping stops.
    """
    order = sorted(range(len(clusters_with_load)), key=lambda k: clusters_with_load[k][1], reverse=True)
    used_vehicles = set()
    assignment: dict[str, list[int]] = {}
    unassigned: list[list[int]] = []
    for k in order:
        cluster, need = clusters_with_load[k]
        chosen = None
        for vid, cap in vehicles_ordered:
            if vid in used_vehicles or cap < need:
                continue
            chosen = vid
            break
        if chosen:
            used_vehicles.add(chosen)
            assignment[chosen] = cluster
        else:
            unassigned.append(cluster)
    return assignment, unassigned


def split_clarke_wright(
    dp_local_idx: list[int],
    demand: dict[int, float],
    durations: list[list[float]],
    vehicle_capacities: list[tuple[str, float]],
) -> tuple[dict[str, list[int]], list[list[int]]]:
    """Single-wave convenience wrapper: cw_clusters() + assign_clusters() with
    plain best-fit (tightest-capacity-first) vehicle priority. See cw_clusters() and
    assign_clusters() docstrings, and use those two directly for multi-trip waves
    where vehicle priority needs to follow availability instead of capacity."""
    if not dp_local_idx:
        return {}, []
    ceiling = max(cap for _, cap in vehicle_capacities) if vehicle_capacities else math.inf
    clusters = cw_clusters(dp_local_idx, demand, durations, ceiling)
    best_fit_order = sorted(vehicle_capacities, key=lambda t: t[1])
    return assign_clusters(clusters, best_fit_order)
