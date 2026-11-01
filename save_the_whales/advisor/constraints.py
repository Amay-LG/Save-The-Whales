"""Shared hard constraints and the final action validator (F1, F3 blocks, F4-F8, F10).

The planner calls the same check functions before scoring; ``make_validator`` re-derives every path from
the action itself, independent of candidate generation. Nothing is silently clamped or repaired: an
action is accepted as submitted or rejected with named reasons.

``geometry`` is module-like (``shared.geo`` in production) and must provide ``project``, ``unproject``,
``distance_m``, ``path_length_m``, ``knots_to_mps``, ``ship_clearance_m``, ``check_water_path`` and
``segment_zone_intervals``.

Validation purposes:

- ``ISSUE``: schema, revision newer than ``latest_revision``, unexpired lease of bounded length, fresh
  ``issued_at``, speed bounds, forward rejoin, full path from the current position (land/hull clearance,
  region bounds, forward progress), weather coverage and blocked zones, incident caps.
- ``APPLY``: the same physical checks from the ship's position after response lag; revision must equal
  the latest accepted one; geometry the ship has already passed is ``STALE_ACTION``.
- ``EXPIRY_FALLBACK`` (simulator-internal): the unpassed route and stored rejoin, revision equal to the
  expired command's; skips only the lease-expiry and incident-cap checks, never physical or weather ones.
"""
from __future__ import annotations

import copy
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from save_the_whales.advisor import weather as W
from save_the_whales.advisor.candidates import Polyline
from save_the_whales.advisor.risk import parse_utc
from save_the_whales.shared import contracts as C

# Agreed Tasks.md defaults; config["planner"] overrides them. Evidence limits fall back to
# config["sensors"] and the forecast response delay to config["response"]["response_lag_s"].
PLANNER_DEFAULTS: Dict[str, Any] = {
    "lookahead_s": 900, "max_rejoin_s": 3600, "offsets_m": [500, 1000], "max_detour_waypoints": 8,
    "max_geometries": 4, "max_speeds": 3, "max_candidates": 12, "alt_speeds_knots": [10, 8],
    "bypass_half_length_m": 600, "rejoin_taper_m": 500, "min_lead_m": 200,
    "clearance_margin_m": 100, "min_speed_knots": 6, "max_path_ratio": 1.5, "max_added_delay_s": 900,
    "w_whale": 5, "w_weather": 2, "w_delay": 1, "w_distance": 0.5, "w_change": 0.1,
    "min_objective_improvement": 0.01, "min_whale_improvement": 0.05, "whale_risk_tolerance": 0.01,
    "cooldown_s": 60, "max_reports": 50, "max_detection_age_s": 1800, "drift_mps": 1.5, "sigma_min_m": 50,
    "watch_distance_m": 3000, "weather_trigger_normalized": 0.05, "lease_margin_s": 60,
    "max_planned_ships": 5, "stale_issue_s": 120, "progress_tolerance_m": 1.0,
}

UNKNOWN_SHIP = "UNKNOWN_SHIP"
UNSUPPORTED_COMMAND = "UNSUPPORTED_COMMAND"
MISSING_FIELDS = "MISSING_FIELDS"
BAD_FIELD_TYPE = "BAD_FIELD_TYPE"
BAD_TIMESTAMP = "BAD_TIMESTAMP"
LEASE_TOO_LONG = "LEASE_TOO_LONG"
ISSUED_IN_FUTURE = "ISSUED_IN_FUTURE"
SPEED_OUT_OF_BOUNDS = "SPEED_OUT_OF_BOUNDS"
TOO_MANY_DETOUR_WAYPOINTS = "TOO_MANY_DETOUR_WAYPOINTS"
EMPTY_DETOUR = "EMPTY_DETOUR"
INVALID_REJOIN = "INVALID_REJOIN"
SLOWDOWN_CHANGES_PATH = "SLOWDOWN_CHANGES_PATH"
OUT_OF_BOUNDS = "OUT_OF_BOUNDS"
BACKTRACKING = "BACKTRACKING"
DISTANCE_BUDGET_EXCEEDED = "DISTANCE_BUDGET_EXCEEDED"
DELAY_BUDGET_EXCEEDED = "DELAY_BUDGET_EXCEEDED"
FALLBACK_PATH_MISMATCH = "FALLBACK_PATH_MISMATCH"
NON_FINITE_SHIP_STATE = "NON_FINITE_SHIP_STATE"
UNKNOWN_PURPOSE = "UNKNOWN_PURPOSE"
ACTION_FIELDS = ("ship_id", "issued_at", "command", "target_speed_knots", "detour_waypoints", "valid_until",
                 "metadata", "action_id", "revision")


def planner_config(config: Dict[str, Any]) -> Dict[str, Any]:
    cfg = dict(PLANNER_DEFAULTS)
    sensors = config.get("sensors") or {}
    for k in ("max_reports", "max_detection_age_s"):
        if k in sensors:
            cfg[k] = sensors[k]
    cfg.update(config.get("planner") or {})
    cfg.setdefault("response_delay_s", float((config.get("response") or {}).get("response_lag_s", 0)))
    return cfg


def finite_point(p: Any) -> bool:
    return isinstance(p, dict) and all(isinstance(p.get(k), (int, float)) and not isinstance(p.get(k), bool)
                                       and math.isfinite(p[k]) for k in ("lat", "lon"))


def _same(a: Dict[str, float], b: Dict[str, float]) -> bool:
    return abs(a["lat"] - b["lat"]) < 1e-9 and abs(a["lon"] - b["lon"]) < 1e-9


class ShipView:
    """Observable ship state resolved into projected geometry. Raises ValueError on malformed input."""

    def __init__(self, ship: Dict[str, Any], geometry, cfg: Dict[str, Any]):
        self.raw, self.geo = ship, geometry
        self.ship_id = ship["ship_id"]
        self.here = {"lat": float(ship["lat"]), "lon": float(ship["lon"])}
        nums = [ship[k] for k in ("sog_knots", "length_m", "beam_m", "service_speed_knots")]
        if not finite_point(self.here) or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in nums):
            raise ValueError(NON_FINITE_SHIP_STATE)
        self.orig = [dict(p) for p in ship["original_route"]]
        self.route = [dict(p) for p in ship["current_route"]]
        if len(self.orig) < 2 or not all(finite_point(p) for p in self.orig + self.route):
            raise ValueError(NON_FINITE_SHIP_STATE)
        self.n = len(self.orig)
        self.progress = int(ship["baseline_progress_index"])
        self.service = float(ship["service_speed_knots"])
        self.sog = float(ship["sog_knots"])
        self.length_m, self.beam_m = float(ship["length_m"]), float(ship["beam_m"])
        self.hull_radius_m = 0.5 * math.hypot(self.length_m, self.beam_m)
        self.clearance_m = geometry.ship_clearance_m(self.length_m, self.beam_m, float(cfg["clearance_margin_m"]))
        self.here_xy = tuple(geometry.project(self.here))
        self.orig_xy = [tuple(geometry.project(p)) for p in self.orig]
        self.line = Polyline(self.orig_xy)
        self.s_here, self.lateral_m = self.line.project(self.here_xy, self.progress)
        self.active = ship.get("active_command")
        self.budget = ship.get("incident_budget")
        self.latest_revision = int(ship["latest_revision"])
        # Split the current route into unpassed detour points + baseline tail original_route[rejoin:].
        self.cur_detour, self.cur_rejoin = self.route, None
        for k in range(len(self.route) + 1):
            tail = self.route[k:]
            m = len(tail)
            if m == 0 or (m <= self.n and all(_same(a, b) for a, b in zip(tail, self.orig[self.n - m:]))):
                self.cur_detour, self.cur_rejoin = self.route[:k], (self.n - m if m else None)
                break
        tol = float(cfg["progress_tolerance_m"])
        self.on_route = not self.cur_detour and self.cur_rejoin == self.progress + 1 and self.lateral_m <= tol

    def arc_of(self, p: Dict[str, float]) -> float:
        return self.line.project(tuple(self.geo.project(p)), self.progress)[0]


def timed_legs(points: Sequence[Dict[str, float]], t0: float, speed_knots: float, geometry, *,
               switch_s: Optional[float] = None, speed_after: Optional[float] = None,
               until_s: Optional[float] = None) -> List[Dict[str, Any]]:
    """TimedLegs along ``points`` from absolute time ``t0``; speed may switch at ``switch_s``; legs are cut
    at ``until_s``. Zero-length legs are skipped (no time passes)."""
    legs: List[Dict[str, Any]] = []
    t, speed = float(t0), float(speed_knots)
    for a, b in zip(points, points[1:]):
        rest = dict(a)
        while True:
            if until_s is not None and t >= until_s - 1e-9:
                return legs
            d = geometry.distance_m(rest, b)
            if d <= 1e-9:
                break
            v = geometry.knots_to_mps(speed)
            if v <= 0:
                return legs
            t_end = t + d / v
            cut = None
            if switch_s is not None and speed_after is not None and t < switch_s < t_end - 1e-9:
                cut = switch_s
            if until_s is not None and until_s < t_end - 1e-9 and (cut is None or until_s < cut):
                cut = until_s
            if cut is None:
                legs.append({"start": dict(rest), "end": dict(b), "start_s": t, "end_s": t_end, "speed_knots": speed})
                t = t_end
                break
            f = (cut - t) / (t_end - t)
            (x0, y0), (x1, y1) = geometry.project(rest), geometry.project(b)
            mid = geometry.unproject(x0 + (x1 - x0) * f, y0 + (y1 - y0) * f)
            legs.append({"start": dict(rest), "end": dict(mid), "start_s": t, "end_s": cut, "speed_knots": speed})
            rest, t = mid, cut
            if switch_s is not None and abs(cut - switch_s) < 1e-9:
                speed, switch_s = float(speed_after), None
    return legs


def legs_xy(legs: Sequence[Dict[str, Any]], geometry):
    return [(tuple(geometry.project(l["start"])), tuple(geometry.project(l["end"])), l["start_s"], l["end_s"],
             l["speed_knots"]) for l in legs]


# --- hard checks ---------------------------------------------------------------------------------
def path_reasons(points: Sequence[Dict[str, float]], view: ShipView, land: Dict[str, Any],
                 bbox: Dict[str, float], geometry) -> Tuple[List[str], Optional[float]]:
    """Finite, inside the region box, and land/hull clearance on every vertex and leg (F1, F4)."""
    if not all(finite_point(p) for p in points):
        return [C.NON_FINITE_COORDINATE], None
    if not all(bbox["lat_min"] <= p["lat"] <= bbox["lat_max"] and bbox["lon_min"] <= p["lon"] <= bbox["lon_max"]
               for p in points):
        return [OUT_OF_BOUNDS], None
    water = geometry.check_water_path(list(points), land, view.clearance_m)
    return list(water["reasons"]), water.get("min_clearance_m")


def forward_reasons(view: ShipView, start: Dict[str, float], detour: Sequence[Dict[str, float]],
                    rejoin: int, cfg: Dict[str, Any]) -> List[str]:
    """Detour points must lie ahead of the start and before the rejoin along the baseline (F7)."""
    tol = float(cfg["progress_tolerance_m"])
    s_prev = view.arc_of(start)
    s_rejoin = view.line.arcs[rejoin]
    for p in detour:
        s = view.arc_of(p)
        if s < s_prev - tol or s > s_rejoin + tol:
            return [BACKTRACKING]
        s_prev = s
    return []


def weather_reasons(legs: Sequence[Dict[str, Any]], weather: Dict[str, Any], geometry
                    ) -> Tuple[Optional[float], List[str]]:
    """(exposure severity-seconds, reasons): UNKNOWN_WEATHER for missing coverage, WEATHER_BLOCKED for
    an active blocked zone touched at the actual arrival time (F3)."""
    try:
        exposure = W.weather_exposure(legs, weather, geometry=geometry)
        hits = W.blocked_hits(legs, weather, geometry=geometry)
    except ValueError as exc:
        code = str(exc)
        return None, [code if code in (C.UNKNOWN_WEATHER, W.INVALID_WEATHER, W.INVALID_LEG) else C.UNKNOWN_WEATHER]
    return exposure, ([W.WEATHER_BLOCKED] if hits else [])


def resolve_budget(view: ShipView, rejoin: int, revision: int, geometry, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """The incident budget for a route change to ``rejoin``. An existing incident keeps its original caps
    (extended only by baseline beyond its rejoin); a new one is referenced to the baseline from here."""
    ratio, service_mps = float(cfg["max_path_ratio"]), geometry.knots_to_mps(view.service)
    if view.budget:
        b = copy.deepcopy(view.budget)
        old = int(b["rejoin_route_index"])
        if rejoin > old:
            ext = geometry.path_length_m(view.orig[old:rejoin + 1])
            b["reference_distance_m"] += ext
            b["max_distance_m"] += ratio * ext
            b["reference_eta_s"] += ext / service_mps
            b["rejoin_route_index"] = rejoin
        return b
    ref = geometry.path_length_m([view.here] + view.orig[view.progress + 1:rejoin + 1])
    return {"incident_id": f"{view.ship_id}:incident:{revision}", "rejoin_route_index": rejoin,
            "reference_distance_m": ref, "reference_eta_s": ref / service_mps, "max_distance_m": ratio * ref,
            "max_added_delay_s": float(cfg["max_added_delay_s"]), "accumulated_time_s": 0.0,
            "accumulated_distance_m": 0.0}


def budget_reasons(budget: Dict[str, Any], window_points: Sequence[Dict[str, float]], rejoin: int,
                   speed_knots: float, view: ShipView, geometry) -> List[str]:
    """Accumulated + remaining distance/time to the budget's rejoin within its caps (F5, F6)."""
    end = max(rejoin, int(budget["rejoin_route_index"]))
    path = list(window_points) + view.orig[rejoin + 1:end + 1]
    dist = geometry.path_length_m(path)
    eta = dist / geometry.knots_to_mps(speed_knots)
    out = []
    if budget["accumulated_distance_m"] + dist > budget["max_distance_m"] + 1e-6:
        out.append(DISTANCE_BUDGET_EXCEEDED)
    if budget["accumulated_time_s"] + eta > budget["reference_eta_s"] + budget["max_added_delay_s"] + 1e-6:
        out.append(DELAY_BUDGET_EXCEEDED)
    return out


# --- final validator -----------------------------------------------------------------------------
def make_validator(config: Dict[str, Any], geometry):
    cfg = planner_config(config)

    def fail(*codes: str) -> Dict[str, Any]:
        return {"ok": False, "reasons": list(dict.fromkeys(codes)), "validated_action": None}

    def validate_action(action: Any, observation: Dict[str, Any], *, purpose: str = C.PURPOSE_ISSUE) -> Dict[str, Any]:
        if purpose not in C.VALIDATION_PURPOSES:
            return fail(C.INVALID_ACTION, UNKNOWN_PURPOSE)
        if not isinstance(action, dict) or any(k not in action for k in ACTION_FIELDS):
            return fail(C.INVALID_ACTION, MISSING_FIELDS)
        cmd, rev, detour = action["command"], action["revision"], action["detour_waypoints"]
        if cmd in C.UNSUPPORTED_COMMANDS or cmd not in C.COMMANDS:
            return fail(C.INVALID_ACTION, UNSUPPORTED_COMMAND)
        speed = action["target_speed_knots"]
        if not isinstance(rev, int) or isinstance(rev, bool) or not isinstance(detour, list) \
                or not isinstance(action["metadata"], dict) or not isinstance(action["action_id"], str) \
                or not action["action_id"] or (speed is not None and (not isinstance(speed, (int, float))
                                                                    or isinstance(speed, bool))):
            return fail(C.INVALID_ACTION, BAD_FIELD_TYPE)
        ship = next((s for s in observation.get("ships") or [] if s.get("ship_id") == action["ship_id"]), None)
        if ship is None:
            return fail(C.INVALID_ACTION, UNKNOWN_SHIP)
        try:
            view = ShipView(ship, geometry, cfg)
        except (KeyError, TypeError, ValueError):
            return fail(C.INVALID_ACTION, NON_FINITE_SHIP_STATE)

        # Revision and lease (F9).
        if purpose == C.PURPOSE_ISSUE and rev <= view.latest_revision:
            return fail(C.SUPERSEDED)
        if purpose == C.PURPOSE_APPLY and rev != view.latest_revision:
            return fail(C.SUPERSEDED)
        if purpose == C.PURPOSE_EXPIRY_FALLBACK and (not view.active or rev != view.active.get("revision")
                                                     or rev > view.latest_revision):
            return fail(C.SUPERSEDED)
        try:
            now, vu, issued = (parse_utc(observation["now"]), parse_utc(action["valid_until"]),
                               parse_utc(action["issued_at"]))
        except (TypeError, ValueError):
            return fail(C.INVALID_ACTION, BAD_TIMESTAMP)
        now_s = float(observation["sim_time_s"])
        lease_left = (vu - now).total_seconds()
        if issued > now and (issued - now).total_seconds() > 1e-6:
            return fail(C.INVALID_ACTION, ISSUED_IN_FUTURE)
        if purpose != C.PURPOSE_EXPIRY_FALLBACK:
            if lease_left <= 0:
                return fail(C.STALE_ACTION)
            if lease_left > float(cfg["max_rejoin_s"]) + float(cfg["lease_margin_s"]):
                return fail(C.INVALID_ACTION, LEASE_TOO_LONG)
        if purpose == C.PURPOSE_ISSUE and (now - issued).total_seconds() > float(cfg["stale_issue_s"]):
            return fail(C.STALE_ACTION)

        # Speed (F4) and path resolution (F7).
        eff = view.service if speed is None else float(speed)
        if not math.isfinite(eff) or eff < float(cfg["min_speed_knots"]) or eff > view.service + 1e-9:
            return fail(C.INVALID_ACTION, SPEED_OUT_OF_BOUNDS)
        if len(detour) > int(cfg["max_detour_waypoints"]):
            return fail(C.INVALID_ACTION, TOO_MANY_DETOUR_WAYPOINTS)
        if not all(finite_point(p) for p in detour):
            return fail(C.INVALID_ACTION, C.NON_FINITE_COORDINATE)
        rejoin = action.get("rejoin_route_index")
        if cmd == C.SLOWDOWN:
            if detour or rejoin is not None:
                return fail(C.INVALID_ACTION, SLOWDOWN_CHANGES_PATH)
            remaining = [dict(p) for p in view.route]
            window = [view.here] + remaining
        else:
            if not isinstance(rejoin, int) or isinstance(rejoin, bool) or rejoin >= view.n or rejoin < 0:
                return fail(C.INVALID_ACTION, INVALID_REJOIN)
            if rejoin <= view.progress:
                return fail(C.STALE_ACTION, INVALID_REJOIN)
            if cmd == C.DETOUR and not detour:
                return fail(C.INVALID_ACTION, EMPTY_DETOUR)
            if purpose == C.PURPOSE_EXPIRY_FALLBACK and (rejoin != view.cur_rejoin or len(detour) != len(view.cur_detour)
                                                         or not all(_same(a, b) for a, b in zip(detour, view.cur_detour))):
                return fail(C.INVALID_ACTION, FALLBACK_PATH_MISMATCH)
            remaining = [{"lat": float(p["lat"]), "lon": float(p["lon"])} for p in detour] + view.orig[rejoin:]
            window = [view.here] + remaining[:len(detour) + 1]
            fwd = forward_reasons(view, view.here, detour, rejoin, cfg)
            if fwd:
                return fail(C.INVALID_ACTION if purpose == C.PURPOSE_ISSUE else C.STALE_ACTION, *fwd)
        reasons, _ = path_reasons([view.here] + remaining, view, observation["static"]["land"],
                                  observation["bbox"], geometry)
        if reasons:
            return fail(*reasons)

        # Weather at actual arrival times over the action's window (F3).
        until = None if cmd != C.SLOWDOWN else now_s + max(0.0, lease_left)
        legs = timed_legs(window, now_s, eff, geometry, until_s=until)
        _, wreasons = weather_reasons(legs, observation["static"]["weather"], geometry)
        if wreasons:
            return fail(*wreasons)

        # Incident caps (F5, F6); the fallback never gets a new allowance but is not re-capped.
        budget = copy.deepcopy(view.budget)
        if purpose != C.PURPOSE_EXPIRY_FALLBACK:
            if cmd == C.DETOUR or (cmd == C.RESUME and not view.on_route):
                budget = resolve_budget(view, rejoin, rev, geometry, cfg)
                breasons = budget_reasons(budget, window, rejoin, eff, view, geometry)
                if breasons:
                    return fail(*breasons)
            elif cmd == C.SLOWDOWN and lease_left * (1.0 - eff / view.service) > float(cfg["max_added_delay_s"]) + 1e-6:
                return fail(DELAY_BUDGET_EXCEEDED)
            elif cmd == C.RESUME and view.on_route:
                budget = None
        return {"ok": True, "reasons": [], "validated_action": {
            "action": copy.deepcopy(action), "remaining_waypoints": remaining,
            "rejoin_route_index": None if cmd == C.SLOWDOWN else rejoin,
            "effective_target_speed_knots": eff, "incident_budget": budget}}

    return validate_action
