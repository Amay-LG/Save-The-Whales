"""Constraint checking for candidate vessel manoeuvres.

Every candidate action (slowdown, detour, resume) is validated against
geometric and policy constraints before the advisor can recommend it.
"""
import math
from dataclasses import dataclass, field

from your_project.projection import (
    project, unproject, unproject_to_latlon, project_array,
    distance_m, path_length_m, path_hits_land, point_in_bbox_xy,
    point_in_polygon, segment_hits_polygon, KN_TO_MS, MS_TO_KN,
)


# ── Result type ─────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    """Result of validating a candidate action against all configured checks."""
    passed: bool
    assessed_checks: list[str] = field(default_factory=list)    # checks that were run
    unverified_checks: list[str] = field(default_factory=list)  # checks NOT run (stated honestly)
    rejection_reasons: list[str] = field(default_factory=list)  # why it failed (if it did)
    warnings: list[str] = field(default_factory=list)           # non-fatal issues


# The checks we CAN do in the initial release
ASSESSED = [
    "LAND_CLEARANCE", "REGION_BOUNDS", "SPEED_BOUNDS",
    "LANE_CORRIDOR", "REJOIN_VALID", "DEVIATION_CAP",
]

# The checks we explicitly CANNOT do yet
UNVERIFIED = ["SHIP_SHIP", "REALISTIC_MANEUVERING", "DEPTH_CLEARANCE"]


# ── Helper: point-to-segment distance ──────────────────────────────────────

def _point_to_segment_dist(px: float, py: float,
                           ax: float, ay: float,
                           bx: float, by: float) -> float:
    """Minimum distance from point (px, py) to line segment (ax, ay)-(bx, by)."""
    dx, dy = bx - ax, by - ay
    len_sq = dx * dx + dy * dy
    if len_sq == 0.0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len_sq))
    proj_x = ax + t * dx
    proj_y = ay + t * dy
    return math.hypot(px - proj_x, py - proj_y)


# ── Point-to-polyline distance ──────────────────────────────────────────────

def point_to_polyline_distance(pt: tuple[float, float],
                               polyline: list[tuple[float, float]]) -> float:
    """Minimum distance from a point to any segment of a polyline.

    For each segment (a, b) in the polyline:
    - Project *pt* onto the infinite line through a–b.
    - Clamp the projection parameter to the segment bounds [0, 1].
    - Compute the distance from *pt* to the clamped projection.
    Return the minimum across all segments.
    """
    if len(polyline) == 0:
        return math.inf
    if len(polyline) == 1:
        return math.hypot(pt[0] - polyline[0][0], pt[1] - polyline[0][1])

    px, py = pt
    best = math.inf
    for i in range(len(polyline) - 1):
        ax, ay = polyline[i]
        bx, by = polyline[i + 1]
        d = _point_to_segment_dist(px, py, ax, ay, bx, by)
        if d < best:
            best = d
    return best


# ── Individual check functions ──────────────────────────────────────────────

def check_land_clearance(path_xy: list[tuple[float, float]],
                         land_polys: list[list[tuple[float, float]]],
                         buffer_m: float = 100.0) -> tuple[bool, str]:
    """Check that a path does not intersect any land polygon.

    If *buffer_m* > 0, also check that no endpoint or segment midpoint on the
    path is within *buffer_m* of any land polygon edge.

    Returns ``(passed, reason_if_failed)``.
    """
    if not path_xy or len(path_xy) < 2:
        return (True, "")
    if not land_polys:
        return (True, "")

    # 1. Direct intersection test
    if path_hits_land(path_xy, land_polys):
        return (False, "Path intersects land")

    # 2. Buffer proximity test
    if buffer_m > 0.0:
        # Gather sample points: all waypoints plus segment midpoints
        sample_pts: list[tuple[float, float]] = []
        for pt in path_xy:
            sample_pts.append(pt)
        for i in range(len(path_xy) - 1):
            mx = (path_xy[i][0] + path_xy[i + 1][0]) / 2.0
            my = (path_xy[i][1] + path_xy[i + 1][1]) / 2.0
            sample_pts.append((mx, my))

        for pt in sample_pts:
            for poly in land_polys:
                n = len(poly)
                for j in range(n):
                    ax, ay = poly[j]
                    bx, by = poly[(j + 1) % n]
                    d = _point_to_segment_dist(pt[0], pt[1], ax, ay, bx, by)
                    if d < buffer_m:
                        return (False,
                                f"Path point ({pt[0]:.0f}, {pt[1]:.0f}) is "
                                f"{d:.0f} m from land (buffer={buffer_m:.0f} m)")

    return (True, "")


def check_region_bounds(path_xy: list[tuple[float, float]],
                        bbox_xy: tuple[float, float, float, float]) -> tuple[bool, str]:
    """Check that all points of the path are within the region bounding box.

    *bbox_xy* = ``(x_min, y_min, x_max, y_max)`` in metres.

    Returns ``(passed, reason_if_failed)``.
    """
    if not path_xy:
        return (True, "")

    for pt in path_xy:
        if not point_in_bbox_xy(pt, bbox_xy):
            return (False,
                    f"Waypoint ({pt[0]:.0f}, {pt[1]:.0f}) is outside the "
                    f"advisory region bounding box")
    return (True, "")


def check_speed_bounds(target_speed_kn: float,
                       min_speed_kn: float,
                       service_speed_kn: float) -> tuple[bool, float, str]:
    """Check and clamp speed to ``[min_speed, service_speed]``.

    Returns ``(passed, clamped_speed_kn, warning_if_clamped)``:

    - Speed within bounds → ``(True, speed, "")``.
    - Speed outside but clampable → ``(True, clamped_value, "clamped from X to Y")``.
    - Speed nonsensical (negative, NaN) → ``(False, target_speed_kn, reason)``.
    """
    # Reject nonsensical values first
    if math.isnan(target_speed_kn) or math.isinf(target_speed_kn):
        return (False, target_speed_kn,
                f"Speed is not a finite number ({target_speed_kn})")
    if target_speed_kn < 0.0:
        return (False, target_speed_kn,
                f"Speed is negative ({target_speed_kn:.2f} kn)")

    # Clamp
    if target_speed_kn < min_speed_kn:
        return (True, min_speed_kn,
                f"Speed clamped from {target_speed_kn:.2f} to "
                f"{min_speed_kn:.2f} kn (minimum)")
    if target_speed_kn > service_speed_kn:
        return (True, service_speed_kn,
                f"Speed clamped from {target_speed_kn:.2f} to "
                f"{service_speed_kn:.2f} kn (service speed)")

    return (True, target_speed_kn, "")


def check_lane_corridor(path_xy: list[tuple[float, float]],
                        lanes: dict,
                        max_deviation_m: float = 10000.0) -> tuple[bool, str]:
    """Check that the path stays within *max_deviation_m* of a lane corridor.

    *lanes*: ``dict`` of ``lane_id → {"waypoints_xy": [...], ...}``

    For each point on the path, compute its minimum distance to **any** lane
    centreline.  If any point exceeds *max_deviation_m*, fail.

    Returns ``(passed, reason_if_failed)``.
    """
    if not path_xy:
        return (True, "")
    if not lanes:
        # No lanes defined — cannot enforce corridor; pass with no info
        return (True, "")

    # Collect all lane polylines
    lane_polylines: list[list[tuple[float, float]]] = []
    for lane_info in lanes.values():
        wps = lane_info.get("waypoints_xy", [])
        if len(wps) >= 2:
            lane_polylines.append(wps)

    if not lane_polylines:
        return (True, "")

    for pt in path_xy:
        # Minimum distance to any lane centreline
        best_dist = math.inf
        for polyline in lane_polylines:
            d = point_to_polyline_distance(pt, polyline)
            if d < best_dist:
                best_dist = d
        if best_dist > max_deviation_m:
            return (False,
                    f"Waypoint ({pt[0]:.0f}, {pt[1]:.0f}) is {best_dist:.0f} m "
                    f"from nearest lane centreline (max {max_deviation_m:.0f} m)")

    return (True, "")


def check_deviation_cap(new_path_length_m: float,
                        original_remaining_m: float,
                        max_ratio: float = 1.5) -> tuple[bool, str]:
    """Check that the new path length does not exceed *max_ratio* × original remaining distance.

    Returns ``(passed, reason_if_failed)``.
    """
    if original_remaining_m <= 0.0:
        # Cannot meaningfully compare — allow
        return (True, "")

    ratio = new_path_length_m / original_remaining_m
    if ratio > max_ratio:
        return (False,
                f"Detour path is {ratio:.2f}× the original remaining "
                f"distance ({new_path_length_m:.0f} m vs "
                f"{original_remaining_m:.0f} m, cap {max_ratio:.2f}×)")
    return (True, "")


# ── Rejoin validation ───────────────────────────────────────────────────────

def find_rejoin_waypoint(last_detour_xy: tuple[float, float],
                         original_route_xy: list[tuple[float, float]],
                         land_polys: list[list[tuple[float, float]]]) -> tuple[float, float] | None:
    """Find the first original-route waypoint ahead whose leg from
    *last_detour_xy* is land-free.

    Algorithm:
    - For each waypoint in *original_route_xy* (in order):
      - Check if the segment from *last_detour_xy* to that waypoint crosses land.
      - If land-free: return that waypoint.
    - If none found: return ``None`` (detour is invalid — can't rejoin).
    """
    if not original_route_xy:
        return None

    for wp in original_route_xy:
        path_seg = [last_detour_xy, wp]
        if not path_hits_land(path_seg, land_polys):
            return wp
    return None


def check_rejoin(detour_waypoints_xy: list[tuple[float, float]],
                 original_route_xy: list[tuple[float, float]],
                 land_polys: list[list[tuple[float, float]]]) -> tuple[bool, tuple | None, str]:
    """Validate that a detour can rejoin the original route.

    Uses :func:`find_rejoin_waypoint` from the last detour waypoint.

    Returns ``(passed, rejoin_xy_or_None, reason_if_failed)``.
    """
    if not detour_waypoints_xy:
        return (False, None, "Detour has no waypoints")
    if not original_route_xy:
        return (False, None, "No original route to rejoin")

    last_detour = detour_waypoints_xy[-1]
    rejoin = find_rejoin_waypoint(last_detour, original_route_xy, land_polys)
    if rejoin is None:
        return (False, None,
                "Cannot rejoin original route from last detour waypoint "
                f"({last_detour[0]:.0f}, {last_detour[1]:.0f}) — all legs "
                "cross land")
    return (True, rejoin, "")


# ── Main validation entry point ────────────────────────────────────────────

def check_all(action_type: str,
              ship_xy: tuple[float, float],
              target_speed_kn: float | None,
              detour_waypoints_xy: list[tuple[float, float]] | None,
              original_route_xy: list[tuple[float, float]],
              service_speed_kn: float,
              min_speed_kn: float,
              bbox_xy: tuple[float, float, float, float],
              land_polys: list[list[tuple[float, float]]],
              lanes: dict,
              max_path_ratio: float = 1.5,
              max_waypoints: int = 8,
              max_lane_deviation_m: float = 10000.0,
              land_buffer_m: float = 100.0) -> CheckResult:
    """Run all applicable constraint checks on a candidate action.

    *action_type*: ``"SLOWDOWN"``, ``"DETOUR"``, ``"COMBINED"``, or ``"RESUME"``.

    - **SLOWDOWN**: check speed bounds only.
    - **DETOUR**: check waypoint count, build full path
      (ship → waypoints → rejoin), check land, bounds, corridor, deviation
      cap, rejoin.
    - **COMBINED**: check both speed and path constraints.
    - **RESUME**: always passes.

    Always includes :data:`UNVERIFIED` checks in the result.
    """
    result = CheckResult(passed=True)
    result.unverified_checks = list(UNVERIFIED)

    try:
        action_upper = action_type.upper()

        # ── RESUME: always passes ───────────────────────────────────────
        if action_upper == "RESUME":
            return result

        # ── Speed checks (SLOWDOWN or COMBINED) ────────────────────────
        if action_upper in ("SLOWDOWN", "COMBINED"):
            result.assessed_checks.append("SPEED_BOUNDS")
            if target_speed_kn is None:
                result.passed = False
                result.rejection_reasons.append(
                    "SPEED_BOUNDS: no target speed specified for slowdown")
            else:
                ok, clamped, msg = check_speed_bounds(
                    target_speed_kn, min_speed_kn, service_speed_kn)
                if not ok:
                    result.passed = False
                    result.rejection_reasons.append(f"SPEED_BOUNDS: {msg}")
                elif msg:
                    result.warnings.append(f"SPEED_BOUNDS: {msg}")

        # ── Path checks (DETOUR or COMBINED) ───────────────────────────
        if action_upper in ("DETOUR", "COMBINED"):
            waypoints = detour_waypoints_xy or []

            # Waypoint count
            if len(waypoints) == 0:
                result.passed = False
                result.rejection_reasons.append(
                    "DETOUR: no detour waypoints provided")
                return result

            if len(waypoints) > max_waypoints:
                result.passed = False
                result.rejection_reasons.append(
                    f"DETOUR: too many waypoints ({len(waypoints)} > "
                    f"{max_waypoints})")
                return result

            # Rejoin check
            result.assessed_checks.append("REJOIN_VALID")
            rejoin_ok, rejoin_xy, rejoin_msg = check_rejoin(
                waypoints, original_route_xy, land_polys)
            if not rejoin_ok:
                result.passed = False
                result.rejection_reasons.append(
                    f"REJOIN_VALID: {rejoin_msg}")
                return result

            # Build the full path: ship → waypoints → rejoin point
            full_path: list[tuple[float, float]] = [ship_xy] + list(waypoints)
            if rejoin_xy is not None:
                full_path.append(rejoin_xy)

            # Land clearance
            result.assessed_checks.append("LAND_CLEARANCE")
            land_ok, land_msg = check_land_clearance(
                full_path, land_polys, buffer_m=land_buffer_m)
            if not land_ok:
                result.passed = False
                result.rejection_reasons.append(
                    f"LAND_CLEARANCE: {land_msg}")

            # Region bounds
            result.assessed_checks.append("REGION_BOUNDS")
            bounds_ok, bounds_msg = check_region_bounds(full_path, bbox_xy)
            if not bounds_ok:
                result.passed = False
                result.rejection_reasons.append(
                    f"REGION_BOUNDS: {bounds_msg}")

            # Lane corridor
            result.assessed_checks.append("LANE_CORRIDOR")
            corridor_ok, corridor_msg = check_lane_corridor(
                full_path, lanes, max_deviation_m=max_lane_deviation_m)
            if not corridor_ok:
                result.passed = False
                result.rejection_reasons.append(
                    f"LANE_CORRIDOR: {corridor_msg}")

            # Deviation cap
            result.assessed_checks.append("DEVIATION_CAP")
            new_len = path_length_m(full_path)
            # Compute original remaining distance from ship to end of
            # original route through the remaining waypoints
            orig_remaining = 0.0
            if original_route_xy:
                remaining_path = [ship_xy] + list(original_route_xy)
                orig_remaining = path_length_m(remaining_path)
            dev_ok, dev_msg = check_deviation_cap(
                new_len, orig_remaining, max_ratio=max_path_ratio)
            if not dev_ok:
                result.passed = False
                result.rejection_reasons.append(
                    f"DEVIATION_CAP: {dev_msg}")

        # For SLOWDOWN-only, mark path checks as not assessed
        if action_upper == "SLOWDOWN":
            for check_name in ASSESSED:
                if check_name != "SPEED_BOUNDS" and check_name not in result.assessed_checks:
                    pass  # simply not assessed — not a failure

    except Exception as exc:
        result.passed = False
        result.rejection_reasons.append(f"INTERNAL_ERROR: {exc!s}")

    return result
