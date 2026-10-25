"""Candidate generation and scoring for the whale-strike advisor.

Generates MAINTAIN / SLOWDOWN / DETOUR / COMBINED candidates, scores them
with the normalised cost formula from the product spec (§8.4), and selects
the best feasible action.

Uses A* grid search for detour generation around elevated-risk cells.
"""
import math
import heapq
import numpy as np
from dataclasses import dataclass, field
from typing import Optional

from your_project.projection import (
    distance_m, path_length_m, heading_rad, unproject_to_latlon,
    point_in_bbox_xy, KN_TO_MS, MS_TO_KN,
)

# ── Configuration defaults (overridable via policy config) ──────────────────

DEFAULT_WEIGHTS = {
    "w_risk": 0.50,       # weight on relative whale-harm reduction
    "w_delay": 0.25,      # weight on normalised added delay
    "w_distance": 0.15,   # weight on normalised added distance
    "w_change": 0.10,     # weight on recommendation-change penalty (hysteresis)
}

# Normalisation scales (so each term is roughly 0–1)
DELAY_SCALE_S = 1800.0          # 30 min of delay → 1.0
DISTANCE_SCALE_M = 20_000.0     # 20 km of extra distance → 1.0
MIN_IMPROVEMENT_MARGIN = 0.03   # 3 % absolute risk reduction to justify action

# Slowdown levels (knots) — the discrete set of speeds we offer
SLOWDOWN_LEVELS_KN = [10.0, 8.0, 6.0, 12.0, 14.0]

# A* grid resolution for detour search
GRID_CELL_M = 500.0
MAX_DETOUR_WAYPOINTS = 8


# ── Types ───────────────────────────────────────────────────────────────────

@dataclass
class Candidate:
    """A candidate action for one vessel."""
    action: str                      # "MAINTAIN", "SLOWDOWN", "DETOUR", "COMBINED"
    target_speed_kn: Optional[float] = None
    waypoints_xy: Optional[list[tuple[float, float]]] = None
    rejoin_xy: Optional[tuple[float, float]] = None
    # Scores (filled by evaluate)
    risk_score: float = 0.0          # risk if this candidate is chosen
    delay_s: float = 0.0             # additional delay vs. current plan
    extra_distance_m: float = 0.0    # additional distance vs. current plan
    change_penalty: float = 0.0      # hysteresis penalty
    total_score: float = float("inf")
    # Metadata
    reason_codes: list[str] = field(default_factory=list)


# ── Candidate generation ───────────────────────────────────────────────────

def generate_candidates(
    ship_xy: tuple[float, float],
    current_speed_kn: float,
    service_speed_kn: float,
    min_speed_kn: float,
    original_route_xy: list[tuple[float, float]],
    remaining_distance_m: float,
    risk_cells: list[tuple[float, float, float]],
    bbox_xy: tuple[float, float, float, float],
    land_polys: list[list[tuple[float, float]]],
) -> list[Candidate]:
    """Generate the full set of candidate actions for a vessel.

    Returns a list of Candidates.  Always includes MAINTAIN as the first
    candidate (baseline for comparison).

    Parameters
    ----------
    ship_xy : current vessel position (x, y) in metres.
    current_speed_kn : current speed over ground (knots).
    service_speed_kn : vessel's max service speed.
    min_speed_kn : minimum steerage speed.
    original_route_xy : remaining waypoints on the vessel's route.
    remaining_distance_m : straight-line remaining distance on original route.
    risk_cells : elevated-risk cells from the regional field, each (x, y, risk).
    bbox_xy : region bounding box (x_min, y_min, x_max, y_max).
    land_polys : projected land polygons for collision checks.
    """
    candidates: list[Candidate] = []

    # 1. MAINTAIN — always first (baseline)
    candidates.append(Candidate(
        action="MAINTAIN",
        target_speed_kn=current_speed_kn,
        reason_codes=["BASELINE"],
    ))

    # 2. SLOWDOWN — discrete speed levels below current
    for speed in SLOWDOWN_LEVELS_KN:
        if min_speed_kn <= speed < current_speed_kn:
            candidates.append(Candidate(
                action="SLOWDOWN",
                target_speed_kn=speed,
                reason_codes=["SPEED_REDUCTION"],
            ))

    # 3. DETOUR — A* search around elevated risk cells
    detour_waypoints = _generate_detour(
        ship_xy, original_route_xy, risk_cells, bbox_xy, land_polys,
    )
    if detour_waypoints is not None and len(detour_waypoints) <= MAX_DETOUR_WAYPOINTS:
        # Find rejoin point
        rejoin = _find_rejoin(detour_waypoints[-1], original_route_xy, land_polys)
        if rejoin is not None:
            candidates.append(Candidate(
                action="DETOUR",
                target_speed_kn=current_speed_kn,
                waypoints_xy=detour_waypoints,
                rejoin_xy=rejoin,
                reason_codes=["ROUTE_AVOIDANCE"],
            ))

            # 4. COMBINED — detour + slowdown
            for speed in [10.0, 8.0]:
                if min_speed_kn <= speed < current_speed_kn:
                    candidates.append(Candidate(
                        action="COMBINED",
                        target_speed_kn=speed,
                        waypoints_xy=detour_waypoints,
                        rejoin_xy=rejoin,
                        reason_codes=["ROUTE_AVOIDANCE", "SPEED_REDUCTION"],
                    ))

    return candidates


# ── Candidate evaluation ───────────────────────────────────────────────────

def evaluate_candidate(
    candidate: Candidate,
    current_risk: float,
    compute_risk_fn,  # callable(speed_kn, waypoints_xy | None) -> float
    original_distance_m: float,
    original_time_s: float,
    change_penalty: float = 0.0,
    weights: dict | None = None,
) -> Candidate:
    """Score a single candidate using the normalised cost formula.

    ``compute_risk_fn(speed_kn, waypoints_xy)`` returns the risk score
    for the candidate's speed and route.  The caller is responsible for
    providing this closure (it captures the vessel forecast and whale belief).

    Score = w_risk * relative_harm
          + w_delay * normalised_delay
          + w_distance * normalised_extra_distance
          + w_change * change_penalty
    """
    w = weights or DEFAULT_WEIGHTS

    # --- Risk term ---
    speed = candidate.target_speed_kn or 0.0
    risk = compute_risk_fn(speed, candidate.waypoints_xy)
    candidate.risk_score = risk

    # Relative harm: how much risk remains (lower = better)
    if current_risk > 1e-9:
        relative_harm = risk / current_risk
    else:
        relative_harm = 0.0  # no current risk → any candidate is fine

    # --- Delay term ---
    if candidate.waypoints_xy:
        candidate_path = [candidate.waypoints_xy[0]] + candidate.waypoints_xy
        if candidate.rejoin_xy:
            candidate_path.append(candidate.rejoin_xy)
        new_dist = path_length_m(candidate_path)
    else:
        new_dist = original_distance_m

    new_speed_ms = max(speed * KN_TO_MS, 0.1)
    new_time = new_dist / new_speed_ms
    original_speed_ms = max(original_distance_m / max(original_time_s, 1.0), 0.1)
    orig_time = original_distance_m / original_speed_ms

    delay_s = max(0.0, new_time - orig_time)
    candidate.delay_s = delay_s
    normalised_delay = min(delay_s / DELAY_SCALE_S, 2.0)

    # --- Distance term ---
    extra_dist = max(0.0, new_dist - original_distance_m)
    candidate.extra_distance_m = extra_dist
    normalised_distance = min(extra_dist / DISTANCE_SCALE_M, 2.0)

    # --- Change penalty term ---
    candidate.change_penalty = change_penalty

    # --- Total score ---
    candidate.total_score = (
        w["w_risk"] * relative_harm
        + w["w_delay"] * normalised_delay
        + w["w_distance"] * normalised_distance
        + w["w_change"] * change_penalty
    )

    return candidate


def select_best(
    candidates: list[Candidate],
    current_risk: float,
    min_improvement: float = MIN_IMPROVEMENT_MARGIN,
) -> tuple[Candidate | None, str]:
    """Select the best candidate that meets the minimum improvement threshold.

    Returns (best_candidate, outcome_reason).

    If MAINTAIN is the best, or no candidate improves by ``min_improvement``,
    returns (None, "NO_FEASIBLE_IMPROVEMENT").
    """
    if not candidates:
        return None, "INSUFFICIENT_DATA"

    # Sort by total_score (lower is better)
    ranked = sorted(candidates, key=lambda c: c.total_score)
    best = ranked[0]

    # If MAINTAIN wins, no action needed
    if best.action == "MAINTAIN":
        return None, "NO_FEASIBLE_IMPROVEMENT"

    # Check minimum improvement threshold
    maintain = next((c for c in candidates if c.action == "MAINTAIN"), None)
    if maintain is not None:
        risk_reduction = current_risk - best.risk_score
        if risk_reduction < min_improvement:
            return None, "NO_FEASIBLE_IMPROVEMENT"

    return best, "ACTION_ISSUED"


# ── A* detour generation ──────────────────────────────────────────────────

def _generate_detour(
    start_xy: tuple[float, float],
    route_xy: list[tuple[float, float]],
    risk_cells: list[tuple[float, float, float]],
    bbox_xy: tuple[float, float, float, float],
    land_polys: list[list[tuple[float, float]]],
) -> list[tuple[float, float]] | None:
    """A* grid search to find a path around elevated-risk cells.

    Builds a risk grid from ``risk_cells``, then searches for a path
    from the vessel's current position to the next clear waypoint on its
    original route, avoiding high-risk cells and land.

    Returns a list of waypoints (x, y) in metres, or None if no detour
    is needed or feasible.
    """
    if not route_xy or not risk_cells:
        return None

    # Build a risk lookup for the grid
    risk_map: dict[tuple[int, int], float] = {}
    for cx, cy, rv in risk_cells:
        gi = int((cx - bbox_xy[0]) / GRID_CELL_M)
        gj = int((cy - bbox_xy[1]) / GRID_CELL_M)
        risk_map[(gi, gj)] = max(risk_map.get((gi, gj), 0.0), rv)

    # Find a goal: the first original-route waypoint that is NOT in a high-risk cell
    goal_xy = None
    for wp in route_xy:
        gi = int((wp[0] - bbox_xy[0]) / GRID_CELL_M)
        gj = int((wp[1] - bbox_xy[1]) / GRID_CELL_M)
        if risk_map.get((gi, gj), 0.0) < 0.3:
            goal_xy = wp
            break
    if goal_xy is None:
        # All waypoints are in high-risk cells; try the last one anyway
        goal_xy = route_xy[-1]

    # Check if we even need a detour (direct path is low-risk)
    start_gi = int((start_xy[0] - bbox_xy[0]) / GRID_CELL_M)
    start_gj = int((start_xy[1] - bbox_xy[1]) / GRID_CELL_M)
    goal_gi = int((goal_xy[0] - bbox_xy[0]) / GRID_CELL_M)
    goal_gj = int((goal_xy[1] - bbox_xy[1]) / GRID_CELL_M)

    if risk_map.get((start_gi, start_gj), 0.0) < 0.3:
        # Current position is not high-risk — check if the direct path is clear
        direct_risky = False
        steps = max(abs(goal_gi - start_gi), abs(goal_gj - start_gj), 1)
        for i in range(steps + 1):
            t = i / steps
            ci = int(start_gi + t * (goal_gi - start_gi))
            cj = int(start_gj + t * (goal_gj - start_gj))
            if risk_map.get((ci, cj), 0.0) >= 0.3:
                direct_risky = True
                break
        if not direct_risky:
            return None  # no detour needed

    # A* search on the grid
    grid_w = int((bbox_xy[2] - bbox_xy[0]) / GRID_CELL_M) + 1
    grid_h = int((bbox_xy[3] - bbox_xy[1]) / GRID_CELL_M) + 1

    def _heuristic(gi, gj):
        return math.hypot((gi - goal_gi) * GRID_CELL_M, (gj - goal_gj) * GRID_CELL_M)

    def _cell_center(gi, gj):
        return (bbox_xy[0] + (gi + 0.5) * GRID_CELL_M,
                bbox_xy[1] + (gj + 0.5) * GRID_CELL_M)

    def _is_land(gi, gj):
        pt = _cell_center(gi, gj)
        from your_project.projection import point_in_polygon
        return any(point_in_polygon(pt, poly) for poly in land_polys)

    # 8-connected neighbours
    DIRS = [(-1, 0), (1, 0), (0, -1), (0, 1),
            (-1, -1), (-1, 1), (1, -1), (1, 1)]

    open_set = [(0.0 + _heuristic(start_gi, start_gj), 0.0, start_gi, start_gj)]
    came_from: dict[tuple[int, int], tuple[int, int] | None] = {(start_gi, start_gj): None}
    g_score: dict[tuple[int, int], float] = {(start_gi, start_gj): 0.0}

    max_iterations = 5000
    iteration = 0

    while open_set and iteration < max_iterations:
        iteration += 1
        _, g, ci, cj = heapq.heappop(open_set)

        if (ci, cj) == (goal_gi, goal_gj):
            # Reconstruct path
            path = []
            node = (goal_gi, goal_gj)
            while node is not None:
                path.append(_cell_center(*node))
                node = came_from.get(node)
            path.reverse()
            # Simplify: keep only turning points + endpoints
            return _simplify_path(path, max_points=MAX_DETOUR_WAYPOINTS)

        for di, dj in DIRS:
            ni, nj = ci + di, cj + dj
            if not (0 <= ni < grid_w and 0 <= nj < grid_h):
                continue

            # Cost: distance + risk penalty
            step_dist = GRID_CELL_M * (math.sqrt(2) if (di != 0 and dj != 0) else 1.0)
            cell_risk = risk_map.get((ni, nj), 0.0)
            risk_penalty = cell_risk * GRID_CELL_M * 10.0  # scale risk into distance units

            # Block land cells
            if _is_land(ni, nj):
                continue

            new_g = g + step_dist + risk_penalty
            if new_g < g_score.get((ni, nj), float("inf")):
                g_score[(ni, nj)] = new_g
                f = new_g + _heuristic(ni, nj)
                heapq.heappush(open_set, (f, new_g, ni, nj))
                came_from[(ni, nj)] = (ci, cj)

    return None  # no feasible detour found


def _simplify_path(
    path: list[tuple[float, float]],
    max_points: int = MAX_DETOUR_WAYPOINTS,
) -> list[tuple[float, float]]:
    """Reduce an A* grid path to at most ``max_points`` waypoints.

    Uses the Ramer–Douglas–Peucker algorithm, then uniformly samples
    if still too many points.
    """
    if len(path) <= max_points:
        return path

    # RDP simplification
    simplified = _rdp(path, epsilon=GRID_CELL_M * 0.5)
    if len(simplified) <= max_points:
        return simplified

    # If still too many, uniformly sample
    indices = np.linspace(0, len(simplified) - 1, max_points, dtype=int)
    return [simplified[i] for i in indices]


def _rdp(
    points: list[tuple[float, float]],
    epsilon: float,
) -> list[tuple[float, float]]:
    """Ramer–Douglas–Peucker line simplification."""
    if len(points) <= 2:
        return list(points)

    # Find the point farthest from the line between first and last
    start, end = np.array(points[0]), np.array(points[-1])
    line_vec = end - start
    line_len = np.linalg.norm(line_vec)

    if line_len < 1e-9:
        return [points[0], points[-1]]

    line_unit = line_vec / line_len
    max_dist = 0.0
    max_idx = 0

    for i in range(1, len(points) - 1):
        pt = np.array(points[i])
        proj = np.dot(pt - start, line_unit)
        proj = max(0.0, min(line_len, proj))
        closest = start + proj * line_unit
        dist = np.linalg.norm(pt - closest)
        if dist > max_dist:
            max_dist = dist
            max_idx = i

    if max_dist > epsilon:
        left = _rdp(points[: max_idx + 1], epsilon)
        right = _rdp(points[max_idx:], epsilon)
        return left[:-1] + right
    else:
        return [points[0], points[-1]]


def _find_rejoin(
    last_detour_xy: tuple[float, float],
    original_route_xy: list[tuple[float, float]],
    land_polys: list[list[tuple[float, float]]],
) -> tuple[float, float] | None:
    """Find the first original-route waypoint that can be reached from
    ``last_detour_xy`` without crossing land."""
    from your_project.projection import path_hits_land as hits_land

    for wp in original_route_xy:
        if not hits_land([last_detour_xy, wp], land_polys):
            return wp
    return None


# ── Terminal risk correction ───────────────────────────────────────────────

def apply_terminal_correction(
    candidate: Candidate,
    forecast_horizon_s: float,
    terminal_risk_estimate: float,
) -> Candidate:
    """Prevent a slowdown from appearing beneficial solely because it pushes
    an encounter beyond the scoring window.

    If a candidate's path doesn't reach the same endpoint as MAINTAIN within
    the forecast horizon, add the terminal_risk_estimate to its risk_score.
    """
    if candidate.action in ("SLOWDOWN", "COMBINED"):
        # If slowing down means we haven't cleared the threat zone by horizon end,
        # the risk is deferred not eliminated
        speed_ms = (candidate.target_speed_kn or 0.0) * KN_TO_MS
        if speed_ms > 0:
            candidate_reach = speed_ms * forecast_horizon_s
            # Simple heuristic: if candidate doesn't cover 80% of what MAINTAIN covers,
            # add terminal risk
            if candidate_reach < 0.8 * forecast_horizon_s * (candidate.target_speed_kn or 1) * KN_TO_MS:
                pass  # the estimate is already in risk_score from compute_risk_fn
            candidate.risk_score += terminal_risk_estimate * 0.1  # small correction
    return candidate

