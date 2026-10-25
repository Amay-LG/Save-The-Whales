"""Final, fail-closed validation shared by planning and simulated execution."""
from copy import deepcopy
from math import hypot
from ._common import (KNOT, finite, point, utc, settings, distance, length,
                      same, prefix_to, timed_path)
from .weather import weather_details


class Rejection(ValueError):
    pass


def reject(code):
    raise Rejection(code)


def check_observation(observation, cfg):
    if not isinstance(observation, dict) or observation.get("schema_version") != "2.0":
        reject("INVALID_OBSERVATION")
    try:
        utc(observation["now"])
    except (KeyError, ValueError, TypeError):
        reject("INVALID_OBSERVATION")
    if not finite(observation.get("sim_time_s")) or observation["sim_time_s"] < 0:
        reject("INVALID_OBSERVATION")
    ships = observation.get("ships")
    if not isinstance(ships, list) or len(ships) > 10:
        reject("NOT_EVALUATED_BUDGET")
    reports = observation.get("whale_detections")
    if not isinstance(reports, list):
        reject("INVALID_OBSERVATION")
    if len(reports) > 5000:
        reject("NOT_EVALUATED_BUDGET")
    ids = [s.get("ship_id") if isinstance(s, dict) else None for s in ships]
    if any(not isinstance(s, str) or not s for s in ids) or len(set(ids)) != len(ids):
        reject("INVALID_OBSERVATION")
    box = observation.get("bbox", {})
    if (not isinstance(box, dict) or not all(finite(box.get(k)) for k in ("lat_min", "lat_max", "lon_min", "lon_max"))
        or not -90 <= box["lat_min"] < box["lat_max"] <= 90
        or not -180 <= box["lon_min"] < box["lon_max"] <= 180):
        reject("INVALID_OBSERVATION")
    static = observation.get("static", {})
    land = static.get("land", {}) if isinstance(static, dict) else {}
    if (not isinstance(land, dict) or land.get("validated") is not True or not land.get("rings")
        or any(not isinstance(r, list) or len(r) < 3 or not all(point(p) for p in r) for r in land["rings"])):
        reject("INVALID_MAP")


def ship_state(ship, cfg):
    if not point(ship) or not finite(ship.get("sog_knots")) or ship["sog_knots"] < 0:
        reject("INVALID_SHIP")
    service = ship.get("service_speed_knots")
    if not finite(service) or not cfg["min_speed_knots"] <= service <= 60 or ship["sog_knots"] > service:
        reject("INVALID_SPEED")
    original, current = ship.get("original_route"), ship.get("current_route")
    if not isinstance(original, list) or not 2 <= len(original) <= cfg["max_route_points"] or not all(point(p) for p in original):
        reject("INVALID_BASELINE_ROUTE")
    if not isinstance(current, list) or not current or len(current) > cfg["max_route_points"] or not all(point(p) for p in current):
        reject("INVALID_CURRENT_ROUTE")
    progress = ship.get("baseline_progress_index")
    revision = ship.get("latest_revision")
    if (not isinstance(progress, int) or isinstance(progress, bool) or not 0 <= progress < len(original)-1
        or not isinstance(revision, int) or isinstance(revision, bool) or revision < 0):
        reject("INVALID_SHIP")
    for key, default in (("length_m", 400), ("beam_m", 60)):
        value = ship.get(key, default)
        if not finite(value) or not 0 < value <= 1000:
            reject("INVALID_DIMENSIONS")
    if isinstance(ship.get("validation"), dict) and not ship["validation"].get("ok"):
        reject("INVALID_BASELINE_ROUTE")


def choose_rejoin(ship, cfg, geometry):
    ship_state(ship, cfg)
    budget = ship.get("incident_budget")
    if budget:
        k = budget.get("rejoin_route_index")
        if not isinstance(k, int) or isinstance(k, bool) or not ship["baseline_progress_index"] < k < len(ship["original_route"]):
            reject("INVALID_INCIDENT_BUDGET")
        return k
    dist, pos = 0.0, ship
    chosen = ship["baseline_progress_index"]+1
    for i in range(chosen, len(ship["original_route"])):
        p = ship["original_route"][i]
        dist += distance(pos, p, geometry)
        if dist/(ship["service_speed_knots"]*KNOT) > cfg["lookahead_s"]:
            break
        chosen = i
        pos = p
    return chosen


def _budget(ship, k, cfg, geometry, revision):
    existing = ship.get("incident_budget")
    if existing is not None:
        keys = ("reference_distance_m", "reference_eta_s", "max_distance_m", "max_added_delay_s",
                "accumulated_distance_m", "accumulated_time_s")
        if (not isinstance(existing, dict) or existing.get("rejoin_route_index") != k
            or not isinstance(existing.get("incident_id"), str)
            or any(not finite(existing.get(key)) or existing[key] < 0 for key in keys)
            or existing["reference_distance_m"] <= 0 or existing["reference_eta_s"] <= 0
            or existing["max_distance_m"] > existing["reference_distance_m"]*cfg["max_path_ratio"]+1e-6
            or existing["max_added_delay_s"] > cfg["max_added_delay_s"]):
            reject("INVALID_INCIDENT_BUDGET")
        return deepcopy(existing)
    base = [ship]+ship["original_route"][ship["baseline_progress_index"]+1:k+1]
    ref = length(base, geometry)
    if ref <= 1e-6:
        reject("INVALID_REJOIN")
    return dict(incident_id=f'{ship["ship_id"]}:{revision}',
                rejoin_route_index=k, reference_distance_m=ref,
                reference_eta_s=ref/(ship["service_speed_knots"]*KNOT),
                max_distance_m=cfg["max_path_ratio"]*ref,
                max_added_delay_s=cfg["max_added_delay_s"],
                accumulated_distance_m=0.0, accumulated_time_s=0.0)


def _prefix_for_delay(ship, observation, delay_s, geometry):
    now_s, service = observation["sim_time_s"], ship["service_speed_knots"]
    if not delay_s:
        return [dict(lat=ship["lat"], lon=ship["lon"])], list(ship["current_route"]), []
    active = ship.get("active_command")
    old_lease = float("inf") if ship["sog_knots"] else delay_s
    if active:
        try:
            old_lease = max(0, (utc(active["valid_until"])-utc(observation["now"])).total_seconds())
        except (KeyError, TypeError, ValueError):
            reject("INVALID_ACTIVE_COMMAND")
    legs = timed_path([ship]+ship["current_route"], now_s, ship["sog_knots"], service, old_lease, geometry)
    prefix, prior, travelled = [dict(lat=ship["lat"], lon=ship["lon"])], [], 0.0
    for leg in legs:
        if leg["start_s"] >= now_s+delay_s:
            break
        stop = min(leg["end_s"], now_s+delay_s)
        frac = (stop-leg["start_s"])/(leg["end_s"]-leg["start_s"])
        x, y = geometry.project(leg["start"])
        u, v = geometry.project(leg["end"])
        end = leg["end"] if frac >= 1 else geometry.unproject(x+(u-x)*frac, y+(v-y)*frac)
        prior.append(dict(leg, end=end, end_s=stop))
        travelled += distance(leg["start"], end, geometry)
        if not same(prefix[-1], end):
            prefix.append(end)
    # Remove only waypoints actually traversed, preserving the continuation.
    pending = list(ship["current_route"])
    pos, left = ship, travelled
    while pending and distance(pos, pending[0], geometry) <= left+1e-6:
        left -= distance(pos, pending[0], geometry)
        pos = pending.pop(0)
    return prefix, pending, prior


def _progress_on_baseline(p, baseline, geometry):
    px, py = geometry.project(p)
    best, total, best_s = float("inf"), 0.0, 0.0
    for a, b in zip(baseline, baseline[1:]):
        x, y = geometry.project(a)
        u, v = geometry.project(b)
        dx, dy = u-x, v-y
        size = hypot(dx, dy)
        if size <= 1e-8:
            continue
        t = max(0, min(1, ((px-x)*dx+(py-y)*dy)/(size*size)))
        d = hypot(px-x-t*dx, py-y-t*dy)
        if d < best:
            best, best_s = d, total+t*size
        total += size
    return best_s


def evaluate_action(action, observation, config, geometry, *, purpose="ISSUE", water_cache=None):
    """Validate and resolve once. Internal result also carries legs for scoring."""
    cfg = settings(config)
    check_observation(observation, cfg)
    if purpose not in ("ISSUE", "APPLY", "EXPIRY_FALLBACK") or not isinstance(action, dict):
        reject("INVALID_ACTION")
    matches = [s for s in observation["ships"] if s["ship_id"] == action.get("ship_id")]
    if not matches:
        reject("UNKNOWN_SHIP")
    ship = matches[0]
    ship_state(ship, cfg)
    command = action.get("command")
    if command not in ("DETOUR", "SLOWDOWN", "RESUME"):
        reject("INVALID_ACTION")
    if not isinstance(action.get("metadata"), dict):
        reject("INVALID_ACTION")
    if not isinstance(action.get("action_id"), str) or not action["action_id"]:
        reject("INVALID_ACTION")
    rev = action.get("revision")
    if not isinstance(rev, int) or isinstance(rev, bool) or rev < 1:
        reject("INVALID_ACTION")
    if (purpose == "ISSUE" and rev <= ship["latest_revision"]) or (purpose != "ISSUE" and rev != ship["latest_revision"]):
        reject("SUPERSEDED")
    try:
        now, issued, until = utc(observation["now"]), utc(action["issued_at"]), utc(action["valid_until"])
    except (KeyError, TypeError, ValueError):
        reject("INVALID_ACTION")
    if issued > now or until <= issued:
        reject("INVALID_ACTION")
    if purpose != "EXPIRY_FALLBACK" and until <= now:
        reject("EXPIRED_ACTION")
    if purpose == "ISSUE" and (now-issued).total_seconds() > max(120, observation.get("decision_interval_min", 1)*60):
        reject("STALE_ACTION")
    target = action.get("target_speed_knots")
    if not finite(target) or not cfg["min_speed_knots"] <= target <= ship["service_speed_knots"]:
        reject("INVALID_SPEED")
    if command == "RESUME" and target != ship["service_speed_knots"]:
        reject("INVALID_SPEED")
    points = action.get("detour_waypoints")
    if not isinstance(points, list) or len(points) > 8 or not all(point(p) for p in points):
        reject("INVALID_ACTION")
    if command != "DETOUR" and points:
        reject("INVALID_ACTION")
    k = action.get("rejoin_route_index")
    if not isinstance(k, int) or isinstance(k, bool) or not ship["baseline_progress_index"] < k < len(ship["original_route"]):
        reject("INVALID_REJOIN")
    budget = _budget(ship, k, cfg, geometry, rev)
    snapshot = action.get("source_snapshot")
    if snapshot is not None and purpose != "EXPIRY_FALLBACK":
        if (not isinstance(snapshot, dict) or not point(snapshot.get("position"))
            or not finite(snapshot.get("sim_time_s")) or not finite(snapshot.get("sog_knots"))
            or not 0 <= snapshot["sog_knots"] <= ship["service_speed_knots"]
            or snapshot.get("service_speed_knots") != ship["service_speed_knots"]
            or not isinstance(snapshot.get("current_route"), list) or not snapshot["current_route"]
            or not all(point(p) for p in snapshot["current_route"])):
            reject("INVALID_ACTION")
        elapsed = observation["sim_time_s"]-snapshot["sim_time_s"]
        if elapsed < 0:
            reject("STALE_ACTION")
        # Recorded intent is core data, never explanatory metadata. It detects
        # movement inconsistent with the source snapshot after response lag.
        try:
            if utc(snapshot["now"]) != issued:
                reject("STALE_ACTION")
        except (KeyError, ValueError, TypeError):
            reject("INVALID_ACTION")
        source_ship = dict(snapshot["position"], current_route=snapshot["current_route"],
            sog_knots=snapshot["sog_knots"], service_speed_knots=snapshot["service_speed_knots"],
            active_command=snapshot.get("active_command"))
        expected = _prefix_for_delay(source_ship, snapshot, elapsed, geometry)[0][-1]
        if distance(expected, ship, geometry) > cfg["stale_position_tolerance_m"]:
            reject("STALE_ACTION")
    if purpose == "APPLY" and command == "DETOUR" and snapshot is None:
        reject("STALE_ACTION")
    delay = config.get("response", {}).get("response_lag_s", 0) if purpose == "ISSUE" else 0
    if not finite(delay) or delay < 0:
        reject("INVALID_CONFIG")
    if purpose != "EXPIRY_FALLBACK" and delay >= (until-now).total_seconds():
        reject("EXPIRED_ACTION")
    prefix, pending, prefix_legs = _prefix_for_delay(ship, observation, delay, geometry)
    start = prefix[-1]
    endpoint = ship["original_route"][k]
    if command == "DETOUR" and purpose != "EXPIRY_FALLBACK":
        if not points or any(same(p, endpoint) or same(p, start) for p in points):
            reject("INVALID_ACTION")
        remaining = deepcopy(points+ship["original_route"][k:])
    elif purpose == "EXPIRY_FALLBACK" or command == "SLOWDOWN":
        remaining = deepcopy(pending)
        # Exact suffix is necessary to keep original intent, not just destination.
        before = prefix_to(remaining, endpoint)
        if remaining[len(before):] != ship["original_route"][k+1:]:
            reject("INVALID_CONTINUATION")
    else:
        # Already on baseline: preserve its bends and all unpassed points.
        # Off-route: RESUME explicitly proposes a checked connection to rejoin.
        on_baseline = ship["current_route"] == ship["original_route"][ship["baseline_progress_index"]+1:]
        remaining = deepcopy(pending if on_baseline else ship["original_route"][k:])
    if purpose == "EXPIRY_FALLBACK":
        target = ship["service_speed_knots"]
        stored = ship.get("incident_budget", {}).get("rejoin_route_index")
        if stored != k:
            reject("INVALID_INCIDENT_BUDGET")
    full_path = prefix+remaining
    if len(full_path) > cfg["max_route_points"]+10:
        reject("NOT_EVALUATED_BUDGET")
    box = observation["bbox"]
    if any(not box["lat_min"] <= p["lat"] <= box["lat_max"] or not box["lon_min"] <= p["lon"] <= box["lon_max"] for p in full_path):
        reject("OUT_OF_BOUNDS")
    baseline = ship["original_route"][ship["baseline_progress_index"]:k+1]
    last_progress = _progress_on_baseline(ship, baseline, geometry)
    for p in prefix_to(full_path, endpoint)[1:]:
        progress = _progress_on_baseline(p, baseline, geometry)
        if progress < last_progress-100:
            reject("BACKTRACKING")
        last_progress = max(progress, last_progress)
    extra_margin = 100 if "length_m" not in ship or "beam_m" not in ship else 0
    radius = geometry.ship_clearance_m(ship.get("length_m", 400), ship.get("beam_m", 60), cfg["clearance_margin_m"]+extra_margin)
    if not finite(radius) or radius <= 0:
        reject("INVALID_DIMENSIONS")
    key = (tuple((p["lat"], p["lon"]) for p in full_path), radius)
    water = water_cache.get(key) if water_cache is not None else None
    if water is None:
        water = geometry.check_water_path(full_path, observation["static"]["land"], radius)
        if water_cache is not None:
            water_cache[key] = water
    if not water.get("ok"):
        reject((water.get("reasons") or ["LAND_CLEARANCE"])[0])
    if not finite(water.get("min_clearance_m")) and water.get("min_clearance_m") != float("inf"):
        reject("INVALID_MAP")
    if water["min_clearance_m"] <= radius+1e-6:
        reject("LAND_CLEARANCE")
    lease = float("inf") if purpose == "EXPIRY_FALLBACK" else (until-now).total_seconds()-delay
    future_legs = timed_path([start]+remaining, observation["sim_time_s"]+delay,
                            target, ship["service_speed_knots"], lease, geometry)
    legs = prefix_legs+future_legs
    local = prefix_to(full_path, endpoint)
    rejoin_time = observation["sim_time_s"]
    for leg in legs:
        rejoin_time = leg["end_s"]
        if same(leg["end"], endpoint):
            break
    eta = rejoin_time-observation["sim_time_s"]
    if eta > cfg["max_rejoin_s"]:
        reject("REJOIN_TIME_BUDGET")
    local_distance = length(local, geometry)
    if local_distance+budget["accumulated_distance_m"] > budget["max_distance_m"]+1e-6:
        reject("DISTANCE_BUDGET")
    if eta+budget["accumulated_time_s"]-budget["reference_eta_s"] > budget["max_added_delay_s"]+1e-6:
        reject("DELAY_BUDGET")
    exposure, blocked = weather_details(legs, observation["static"].get("weather"), geometry=geometry)
    if blocked:
        reject("BLOCKED_WEATHER")
    return dict(action=deepcopy(action), remaining_waypoints=remaining,
                rejoin_route_index=k, target_speed_knots=target, incident_budget=budget,
                selected_path=full_path, timed_legs=legs, local_distance_m=local_distance,
                local_eta_s=eta, weather_exposure_s=exposure, clearance_radius_m=radius)


def make_validator(config, geometry):
    """Return validate_action(action, observation, *, purpose='ISSUE')."""
    frozen = deepcopy(config)
    settings(frozen)

    def validate_action(action, observation, *, purpose="ISSUE"):
        try:
            resolved = evaluate_action(action, observation, frozen, geometry, purpose=purpose)
            return dict(ok=True, reasons=[], validated_action=resolved)
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
            code = str(exc) if isinstance(exc, ValueError) and str(exc) in {
                "UNKNOWN_WEATHER", "INVALID_TIME", "MISSING_REJOIN", "INVALID_CONFIG"
            } else str(exc) if isinstance(exc, Rejection) else "INVALID_ACTION"
            return dict(ok=False, reasons=[code], validated_action=None)

    return validate_action
