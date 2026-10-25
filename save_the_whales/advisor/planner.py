"""Pure, bounded numerical advisor. Public factories are the integration seam."""
from copy import deepcopy
from hashlib import sha256
import json
from ._common import settings, UNMODELED, utc, length, prefix_to, KNOT, timed_path
from .constraints import (make_validator, evaluate_action, check_observation, ship_state,
                          choose_rejoin, Rejection)
from .candidates import action_for, generate_candidates
from .risk import fresh_reports, predict_risk


def _code(exc):
    return str(exc) if isinstance(exc, (Rejection, ValueError)) else "INVALID_OBSERVATION"


def _assessment(ship_id, status, reasons, **extra):
    return dict(ship_id=ship_id, status=status, reason_codes=reasons,
                selected_path=[], costs=None, rejected_candidates=[],
                assessed_factors=[f"F{i}" for i in range(1, 11)],
                unmodeled_factors=list(UNMODELED), **extra)


def _cost(resolved, prediction, reference_distance, reference_eta, change, cfg):
    delay = resolved["local_eta_s"]-reference_eta
    extra = resolved["local_distance_m"]-reference_distance
    weather = resolved["weather_exposure_s"]/max(1, reference_eta)
    contributions = dict(whale=cfg["w_whale"]*prediction["whale_risk"],
        weather=cfg["w_weather"]*weather, delay=cfg["w_delay"]*delay/max(1, reference_eta),
        distance=cfg["w_distance"]*extra/max(1, reference_distance), change=cfg["w_change"]*bool(change))
    return dict(whale_risk=prediction["whale_risk"], weather_exposure_s=resolved["weather_exposure_s"],
                weather_cost_normalized=weather, added_delay_s=delay, extra_distance_m=extra,
                objective=sum(contributions.values()), contributions=contributions,
                reference_distance_m=reference_distance, reference_eta_s=reference_eta,
                reference_rejoin_route_index=resolved["rejoin_route_index"],
                incident_budget=deepcopy(resolved["incident_budget"]))


def _current(ship, observation, config, cfg, geometry, cache):
    k = choose_rejoin(ship, cfg, geometry)
    lease = cfg["lease_s"]
    active = ship.get("active_command")
    speed = ship["sog_knots"]
    if active:
        lease = (utc(active["valid_until"])-utc(observation["now"])).total_seconds()
        if lease <= 0:
            # Assess the prescribed fallback, never revive an expired lease.
            lease, speed = cfg["lease_s"], ship["service_speed_knots"]
    action = action_for(ship, observation, "SLOWDOWN", speed, [], k, cfg, lease_s=lease)
    no_delay = deepcopy(config)
    no_delay.setdefault("response", {})["response_lag_s"] = 0
    return evaluate_action(action, observation, no_delay, geometry, water_cache=cache)


def _plan_ship(ship, observation, config, cfg, geometry, reports):
    ship_id = ship["ship_id"]
    try:
        ship_state(ship, cfg)
        k = choose_rejoin(ship, cfg, geometry)
        # Replans use remaining CURRENT intent to the same fixed incident endpoint.
        reference_path = [ship]+prefix_to(ship["current_route"], ship["original_route"][k])
        reference_distance = length(reference_path, geometry)
        reference_eta = reference_distance/(ship["service_speed_knots"]*KNOT)
        # Current remaining timing includes its current lease/fallback, below.
        cache, rejected = {}, []
        current, current_prediction, current_cost = None, None, None
        try:
            current = _current(ship, observation, config, cfg, geometry, cache)
            reference_eta = current["local_eta_s"]
            current_prediction = predict_risk(current["timed_legs"], reports, observation["sim_time_s"],
                ship["service_speed_knots"], current["clearance_radius_m"]-cfg["clearance_margin_m"], cfg, geometry)
            current_cost = _cost(current, current_prediction, reference_distance, reference_eta, False, cfg)
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
            rejected.append(dict(candidate="current", reason_codes=[_code(exc)]))
            if str(exc) == "UNKNOWN_WEATHER":
                return _assessment(ship_id, "UNKNOWN_WEATHER", ["UNKNOWN_WEATHER"]), None
            # A hard-invalid current route is still the whale-risk reference.
            # Otherwise a storm/land rejection could disable the whale-risk gate.
            active = ship.get("active_command")
            old_lease = max(0, (utc(active["valid_until"])-utc(observation["now"])).total_seconds()) if active else float("inf")
            raw_legs = timed_path([ship]+ship["current_route"], observation["sim_time_s"],
                ship["sog_knots"], ship["service_speed_knots"], old_lease, geometry)
            hull = geometry.ship_clearance_m(ship.get("length_m", 400), ship.get("beam_m", 60),
                100 if "length_m" not in ship or "beam_m" not in ship else 0)
            current_prediction = predict_risk(raw_legs, reports, observation["sim_time_s"],
                                             ship["service_speed_knots"], hull, cfg, geometry)
        active = ship.get("active_command")
        expiring = bool(active and (utc(active["valid_until"])-utc(observation["now"])).total_seconds()
                        <= max(observation.get("decision_interval_min", 1)*60, cfg["cooldown_s"]))
        whale_trigger = bool(current_prediction and
            (current_prediction["watched"] or current_prediction["whale_risk"] >= cfg["watch_risk"]))
        weather_trigger = bool(current and current["weather_exposure_s"] >= cfg["weather_trigger_s"])
        maneuver = bool(active and active.get("command") in ("DETOUR", "SLOWDOWN"))
        if current is not None and not (whale_trigger or weather_trigger or expiring or maneuver):
            status = "CURRENT_PLAN_VALID" if reports else "INSUFFICIENT_FRESH_EVIDENCE"
            assessment = _assessment(ship_id, status, [] if reports else [status])
            assessment.update(selected_path=current["selected_path"], costs=current_cost,
                closest_approach=current_prediction["closest_approach"], candidate_count=1)
            return assessment, None
        if current is not None and not reports and not weather_trigger:
            # Losing sightings cannot itself justify faster sailing/a shortcut.
            # The simulator still performs its separately validated finite fallback.
            assessment = _assessment(ship_id, "INSUFFICIENT_FRESH_EVIDENCE", ["INSUFFICIENT_FRESH_EVIDENCE"])
            assessment.update(selected_path=current["selected_path"], costs=current_cost, candidate_count=1)
            return assessment, None
        last = ship.get("last_action_at")
        if current is not None and last and not expiring:
            if (utc(observation["now"])-utc(last)).total_seconds() < cfg["cooldown_s"]:
                assessment = _assessment(ship_id, "COOLDOWN", ["COOLDOWN"])
                assessment.update(selected_path=current["selected_path"], costs=current_cost, candidate_count=1)
                return assessment, None
        hazard = None
        if current_prediction and current_prediction["closest_approach"]:
            rid = current_prediction["closest_approach"]["report_id"]
            hazard = next((r for r in reports if r["detection_id"] == rid), None)
        if not whale_trigger:
            zones = observation["static"].get("weather", {}).get("zones", [])
            storm = next((z for z in zones if z.get("blocked") or z.get("severity", 0) > 0), None)
            if storm:
                hazard = dict(lat=sum(p["lat"] for p in storm["polygon"])/len(storm["polygon"]),
                              lon=sum(p["lon"] for p in storm["polygon"])/len(storm["polygon"]))
        candidates = generate_candidates(ship, observation, cfg, geometry, hazard)
        if (current is not None and not whale_trigger and not weather_trigger
            and ship["current_route"] == ship["original_route"][ship["baseline_progress_index"]+1:]):
            # A speed lease reassessment on a clear baseline has no dogleg hazard.
            candidates = [c for c in candidates if c["name"].startswith("current@")]
        evaluated = []
        for candidate in candidates:
            try:
                resolved = evaluate_action(candidate["action"], observation, config, geometry, water_cache=cache)
                prediction = predict_risk(resolved["timed_legs"], reports, observation["sim_time_s"],
                    ship["service_speed_knots"], resolved["clearance_radius_m"]-cfg["clearance_margin_m"], cfg, geometry)
                costs = _cost(resolved, prediction, reference_distance, reference_eta, candidate["change"], cfg)
                evaluated.append(dict(candidate, resolved=resolved, prediction=prediction, costs=costs))
            except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
                rejected.append(dict(candidate=candidate["name"], reason_codes=[_code(exc)]))
        if not evaluated and current is None:
            reason = "UNKNOWN_WEATHER" if any("UNKNOWN_WEATHER" in r["reason_codes"] for r in rejected) else "NO_FEASIBLE_ROUTE"
            assessment = _assessment(ship_id, reason, [reason])
            assessment.update(rejected_candidates=rejected, candidate_count=len(candidates))
            return assessment, None
        # Constrain improvement before selecting, so an inadmissible cheapest
        # candidate cannot hide a slightly dearer but valid improvement.
        admissible = []
        for item in evaluated:
            costs = item["costs"]
            reason = None
            if current_prediction:
                delta = current_prediction["whale_risk"]-costs["whale_risk"]
                if delta < -cfg["whale_risk_tolerance"]:
                    reason = "WHALE_RISK_INCREASE"
                elif whale_trigger and delta < cfg["min_whale_improvement"]:
                    reason = "INSUFFICIENT_WHALE_BENEFIT"
                elif current_cost and current_cost["objective"]-costs["objective"] < cfg["min_objective_improvement"]:
                    reason = "INSUFFICIENT_OBJECTIVE_BENEFIT"
                # Renewal must not erase a slowdown's benefit relative to its
                # imminent service-speed fallback; compare that freshly too.
                if reason and current_cost and expiring and item["action"]["command"] == "SLOWDOWN" and item["action"]["target_speed_knots"] == ship["sog_knots"]:
                    if costs["objective"]+cfg["min_objective_improvement"] < current_cost["objective"] and delta >= -cfg["whale_risk_tolerance"]:
                        reason = None
            if not reports and item["action"]["command"] == "RESUME" and not weather_trigger:
                reason = "INSUFFICIENT_FRESH_EVIDENCE"
            if reason:
                rejected.append(dict(candidate=item["name"], reason_codes=[reason], costs=costs))
            else:
                admissible.append(item)
        if not admissible:
            if current is None:
                assessment = _assessment(ship_id, "NO_FEASIBLE_ROUTE", ["NO_FEASIBLE_ROUTE"])
                assessment.update(rejected_candidates=rejected, candidate_count=len(candidates))
                return assessment, None
            assessment = _assessment(ship_id, "KEEP_CURRENT_PLAN" if reports else "INSUFFICIENT_FRESH_EVIDENCE",
                ["NO_MATERIAL_IMPROVEMENT"]+([] if reports else ["INSUFFICIENT_FRESH_EVIDENCE"]))
            assessment.update(selected_path=current["selected_path"] if current else [], costs=current_cost,
                              rejected_candidates=rejected, candidate_count=len(candidates))
            return assessment, None
        winner = min(admissible, key=lambda item: (item["costs"]["objective"], item["change"], item["name"]))
        action = winner["action"]
        metadata = dict(costs=winner["costs"], reason_codes=["WHALE_WATCH"] if whale_trigger else ["WEATHER_OR_PLAN_REASSESSMENT"],
            assessed_factors=[f"F{i}" for i in range(1, 11)], unmodeled_factors=UNMODELED,
            assumptions=winner["prediction"]["assumptions"], closest_approach=winner["prediction"]["closest_approach"])
        if not reports:
            metadata["reason_codes"].append("INSUFFICIENT_FRESH_EVIDENCE")
        action["metadata"] = metadata
        digest = sha256(json.dumps(dict(action=action, decision_id=observation.get("decision_id")),
                                  sort_keys=True, allow_nan=False).encode()).hexdigest()[:24]
        action["action_id"] = f'{ship_id}:{action["revision"]}:{digest}'
        assessment = _assessment(ship_id, "ACTION_PROPOSED", metadata["reason_codes"])
        assessment.update(selected_path=winner["resolved"]["selected_path"], costs=winner["costs"],
            rejected_candidates=rejected, candidate_count=len(candidates),
            closest_approach=winner["prediction"]["closest_approach"],
            evidence_count=len(reports), assumptions=winner["prediction"]["assumptions"])
        return assessment, action
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
        return _assessment(ship_id, _code(exc), [_code(exc)]), None


def make_decider(config, geometry):
    """Bind immutable settings. Each call depends exclusively on its observation."""
    frozen = deepcopy(config)
    cfg = settings(frozen)
    cfg["response_lag_s"] = frozen.get("response", {}).get("response_lag_s", 0)

    def decide(observation):
        result = dict(decision_id=observation.get("decision_id") if isinstance(observation, dict) else None,
                      actions=[], assessments=[])
        try:
            check_observation(observation, cfg)
            reports = fresh_reports(observation, cfg)
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
            ships = observation.get("ships", []) if isinstance(observation, dict) else []
            if not isinstance(ships, list):
                ships = []
            ids = [s.get("ship_id") for s in ships[:10] if isinstance(s, dict)] or [None]
            result["assessments"] = [_assessment(s, _code(exc), [_code(exc)]) for s in dict.fromkeys(ids)]
            return result
        for ship in observation["ships"]:
            assessment, action = _plan_ship(ship, observation, frozen, cfg, geometry, reports)
            if "length_m" not in ship or "beam_m" not in ship:
                assumption = "Missing dimensions use 400 m length / 60 m beam and an additional 100 m clearance margin"
                notes = assessment.setdefault("assumptions", [])
                notes.append(assumption)
                if action and action["metadata"]["assumptions"] is not notes:
                    action["metadata"]["assumptions"].append(assumption)
            result["assessments"].append(assessment)
            if action:
                result["actions"].append(action)
        return result

    return decide
