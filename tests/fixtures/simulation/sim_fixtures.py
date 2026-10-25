"""TEST-ONLY helpers for Person 2: private projection, scripted deciders, a strict fixture validator and
fixture weather integrals. Nothing here is a production fallback; Person 3 owns the real advisor.

Import from tests with ``sys.path.insert(0, <this directory>)``.
"""
from __future__ import annotations

import copy
import json
import math
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from save_the_whales.shared import contracts as C
from save_the_whales.simulation.world import iso_at, parse_utc

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
COMMON = ROOT / "tests" / "fixtures" / "common"

# --- Private projection copy (Tasks.md formulas), so the engine can run with or without shared.geo ----
_R, _LAT0, _LON0 = 6_371_000.0, 34.2, -119.75
_COS0 = math.cos(math.radians(_LAT0))


def fixture_project(p: Dict[str, float]):
    return (_R * math.radians(p["lon"] - _LON0) * _COS0, _R * math.radians(p["lat"] - _LAT0))


def load_json(path) -> Any:
    return json.loads(Path(path).read_text())


def fixture_config() -> Dict[str, Any]:
    """Person 2 config sections plus the repo's ``ais`` section (needed by Person 1 loaders)."""
    cfg = load_json(ROOT / "config" / "default.json")
    cfg.update(load_json(HERE / "config.fixture.json"))
    return cfg


def fixture_scenario(**overrides) -> Dict[str, Any]:
    s = load_json(HERE / "scenario_crossing.fixture.json")
    s.update(overrides)
    return s


def make_inputs(bundle, land, scenario, config, run_id="fixture-run") -> Dict[str, Any]:
    fixture_only = bool(bundle["source"].get("fixture_only") or land.get("fixture_only"))
    return {"run_id": run_id, "route_bundle": bundle, "land": land, "scenario": scenario, "config": config,
            "fixture_only": fixture_only}


# --- Deciders ------------------------------------------------------------------------------------
def no_action(obs: Dict[str, Any]) -> Dict[str, Any]:
    return {"decision_id": obs["decision_id"], "actions": [], "assessments": []}


def make_scripted_decider(script: Dict[int, Callable[[Dict[str, Any]], List[Dict[str, Any]]]],
                          log: Optional[list] = None, assessments: Optional[Dict[int, Callable]] = None):
    """``script[int(sim_time_s)](obs) -> actions``. Every received observation is appended to ``log``."""
    def decide(obs):
        if log is not None:
            log.append(obs)
        k = int(round(obs["sim_time_s"]))
        acts = script[k](obs) if k in script else []
        ass = assessments[k](obs) if assessments and k in assessments else []
        return {"decision_id": obs["decision_id"], "actions": acts, "assessments": ass}
    return decide


def make_action(ship: Dict[str, Any], now: str, command: str, *, lease_s: float = 900, detour=(),
                rejoin: Optional[int] = None, speed: Optional[float] = None, revision: Optional[int] = None,
                action_id: Optional[str] = None) -> Dict[str, Any]:
    rev = ship["latest_revision"] + 1 if revision is None else revision
    start = parse_utc(now)
    act = {"ship_id": ship["ship_id"], "issued_at": now, "command": command, "target_speed_knots": speed,
           "detour_waypoints": [dict(p) for p in detour], "valid_until": iso_at(start, lease_s),
           "metadata": {"source": "SCRIPTED_FIXTURE"}, "action_id": action_id or f"{ship['ship_id']}:r{rev}",
           "revision": rev}
    if rejoin is not None:
        act["rejoin_route_index"] = rejoin
    return act


# South-side dogleg around the recipe whale at (34.2, -120.065): ~556 m south, rejoin at index 2.
SOUTH_DETOUR = [{"lat": 34.195, "lon": -120.072}, {"lat": 34.195, "lon": -120.058}]
NORTH_DETOUR = [{"lat": 34.2045, "lon": -120.072}, {"lat": 34.2045, "lon": -120.058}]   # into toy land


def first_index_east_of(ship: Dict[str, Any], lon: float) -> int:
    """First baseline index ahead of progress lying east of ``lon`` (the route's exit if none). Routes
    from ``prepare_routes`` keep only bends, so a straight track has just [start, end]."""
    orig = ship["original_route"]
    return next((i for i in range(ship["baseline_progress_index"] + 1, len(orig)) if orig[i]["lon"] > lon),
                len(orig) - 1)


def detour_at(lease_s=900, detour=SOUTH_DETOUR, rejoin=None, speed=None):
    """DETOUR via ``detour``; ``rejoin=None`` picks the first baseline point past the dogleg."""
    def build(obs):
        ship = obs["ships"][0]
        r = first_index_east_of(ship, detour[-1]["lon"]) if rejoin is None else rejoin
        return [make_action(ship, obs["now"], C.DETOUR, lease_s=lease_s, detour=detour, rejoin=r, speed=speed)]
    return build


# --- Strict fixture validator ---------------------------------------------------------------------
def make_fixture_validator(config: Dict[str, Any], geometry) -> Callable[..., Dict[str, Any]]:
    """Strict TEST-ONLY validator over a module-like ``geometry`` (``shared.geo`` at integration).

    Resolves remaining path = detour + original_route[rejoin:] (SLOWDOWN keeps current_route), checks
    revision/lease rules per purpose, speed bounds, region bounds, the whole path from the current
    position with ``check_water_path`` at hull clearance, and blocked weather zones (time-agnostic)."""
    planner = config.get("planner", {})
    margin = float(planner.get("clearance_margin_m", 100))
    min_speed = float(planner.get("min_speed_knots", 6))
    max_detour = int(planner.get("max_detour_waypoints", 8))
    ratio = float(planner.get("max_path_ratio", 1.5))
    required = ("ship_id", "issued_at", "command", "target_speed_knots", "detour_waypoints", "valid_until",
                "metadata", "action_id", "revision")

    def fail(*codes):
        return {"ok": False, "reasons": list(codes), "validated_action": None}

    def finite(p):
        return isinstance(p, dict) and all(isinstance(p.get(k), (int, float)) and math.isfinite(p[k]) for k in ("lat", "lon"))

    def validate_action(action, observation, *, purpose=C.PURPOSE_ISSUE):
        if purpose not in C.VALIDATION_PURPOSES:
            return fail(C.INVALID_ACTION, "UNKNOWN_PURPOSE")
        if not isinstance(action, dict) or any(k not in action for k in required):
            return fail(C.INVALID_ACTION, "MISSING_FIELDS")
        cmd = action["command"]
        if cmd not in C.COMMANDS:
            return fail(C.INVALID_ACTION, "UNSUPPORTED_COMMAND")
        ship = next((s for s in observation["ships"] if s["ship_id"] == action["ship_id"]), None)
        if ship is None:
            return fail(C.INVALID_ACTION, "UNKNOWN_SHIP")
        rev = action["revision"]
        if not isinstance(rev, int) or isinstance(rev, bool):
            return fail(C.INVALID_ACTION, "BAD_REVISION")
        if purpose == C.PURPOSE_ISSUE and rev <= ship["latest_revision"]:
            return fail(C.SUPERSEDED)
        if purpose == C.PURPOSE_APPLY and rev != ship["latest_revision"]:
            return fail(C.SUPERSEDED)
        if purpose == C.PURPOSE_EXPIRY_FALLBACK and rev != (ship.get("active_command") or {}).get("revision"):
            return fail(C.SUPERSEDED)
        try:
            lease_left = (parse_utc(action["valid_until"]) - parse_utc(observation["now"])).total_seconds()
        except (TypeError, ValueError):
            return fail(C.INVALID_ACTION, "BAD_VALID_UNTIL")
        if purpose != C.PURPOSE_EXPIRY_FALLBACK and lease_left <= 0:
            return fail(C.STALE_ACTION)
        speed = action["target_speed_knots"]
        eff = ship["service_speed_knots"] if speed is None else speed
        if not isinstance(eff, (int, float)) or not math.isfinite(eff) or eff < min_speed \
                or eff > ship["service_speed_knots"] + 1e-9:
            return fail(C.INVALID_ACTION, "SPEED_OUT_OF_BOUNDS")
        orig, prog = ship["original_route"], ship["baseline_progress_index"]
        detour = action["detour_waypoints"]
        if not isinstance(detour, list) or len(detour) > max_detour or not all(finite(p) for p in detour):
            return fail(C.INVALID_ACTION, "BAD_DETOUR_WAYPOINTS")
        rejoin = action.get("rejoin_route_index")
        if cmd in (C.DETOUR, C.RESUME):
            if not isinstance(rejoin, int) or isinstance(rejoin, bool) or not (prog < rejoin < len(orig)):
                return fail(C.INVALID_ACTION, "INVALID_REJOIN")
            remaining = [dict(p) for p in detour] + [dict(p) for p in orig[rejoin:]]
        else:
            if detour or rejoin is not None:
                return fail(C.INVALID_ACTION, "SLOWDOWN_CHANGES_PATH")
            remaining = [dict(p) for p in ship["current_route"]]
        here = {"lat": ship["lat"], "lon": ship["lon"]}
        full = [here] + remaining
        bb = observation["bbox"]
        if not all(bb["lat_min"] <= p["lat"] <= bb["lat_max"] and bb["lon_min"] <= p["lon"] <= bb["lon_max"] for p in full):
            return fail(C.INVALID_ACTION, "OUT_OF_BOUNDS")
        clearance = geometry.ship_clearance_m(ship["length_m"], ship["beam_m"], margin)
        water = geometry.check_water_path(full, observation["static"]["land"], clearance)
        if not water["ok"]:
            return fail(*water["reasons"])
        for z in observation["static"]["weather"].get("zones") or []:
            if z.get("blocked") and any(geometry.segment_zone_intervals(a, b, z["polygon"]) for a, b in zip(full, full[1:])):
                return fail("WEATHER_BLOCKED")
        budget = copy.deepcopy(ship.get("incident_budget"))
        if cmd in (C.DETOUR, C.RESUME) and (budget is None or budget.get("rejoin_route_index") != rejoin):
            ref = geometry.path_length_m([here] + [dict(p) for p in orig[prog + 1:rejoin + 1]])
            budget = {"incident_id": f"{ship['ship_id']}:incident:{rev}", "rejoin_route_index": rejoin,
                      "reference_distance_m": ref, "reference_eta_s": ref / geometry.knots_to_mps(ship["service_speed_knots"]),
                      "max_distance_m": ratio * ref, "max_added_delay_s": float(planner.get("max_added_delay_s", 900)),
                      "accumulated_time_s": 0.0, "accumulated_distance_m": 0.0}
        if cmd in (C.DETOUR, C.RESUME):
            to_rejoin = geometry.path_length_m(full[:len(detour) + 2])
            if budget["accumulated_distance_m"] + to_rejoin > budget["max_distance_m"] + 1e-6:
                return fail("DISTANCE_BUDGET_EXCEEDED")
        return {"ok": True, "reasons": [], "validated_action": {
            "action": copy.deepcopy(action), "remaining_waypoints": remaining,
            "rejoin_route_index": rejoin if cmd in (C.DETOUR, C.RESUME) else None,
            "effective_target_speed_knots": float(eff), "incident_budget": budget}}

    return validate_action


# --- Fixture weather integrals --------------------------------------------------------------------
def _check_coverage(legs, weather):
    for leg in legs:
        if leg["start_s"] < weather["coverage_start_s"] or leg["end_s"] > weather["coverage_end_s"]:
            raise ValueError(C.UNKNOWN_WEATHER)


def zero_weather(legs, weather) -> float:
    """Clear-weather stub that still refuses unknown coverage."""
    _check_coverage(legs, weather)
    return 0.0


def make_fixture_weather_exposure(geometry):
    """Integral of max active severity dt over timed legs, using ``geometry.segment_zone_intervals``."""
    def weather_exposure(legs, weather) -> float:
        _check_coverage(legs, weather)
        total = 0.0
        for leg in legs:
            s0, s1 = float(leg["start_s"]), float(leg["end_s"])
            if s1 <= s0:
                continue
            pieces = []
            for z in weather.get("zones") or []:
                for f0, f1 in geometry.segment_zone_intervals(leg["start"], leg["end"], z["polygon"]):
                    a = max(s0 + f0 * (s1 - s0), float(z["active_from_s"]))
                    b = min(s0 + f1 * (s1 - s0), float(z["active_until_s"]))
                    if b > a:
                        pieces.append((a, b, float(z["severity"])))
            cuts = sorted({p[0] for p in pieces} | {p[1] for p in pieces})
            for a, b in zip(cuts, cuts[1:]):
                total += (b - a) * max((sev for lo, hi, sev in pieces if lo <= a and hi >= b), default=0.0)
        return total
    return weather_exposure
