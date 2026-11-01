"""Pure decision policy: screening, bounded candidates, hard checks, scoring, decisions, explanations.

``make_decider(config, geometry) -> decide(observation) -> Decision``. The closure is stateless: no
network, simulator imports, hidden truth, randomness or wall-clock time. IDs derive from observable
state (``action_id = <ship_id>:r<latest_revision + 1>``), so identical observations give identical
decisions.

Per ship:

1. **Screen** the current plan over ``lookahead_s`` at its actual speed (an active slowdown restores
   service speed at its lease end). Triggers: ``WHALE_WATCH`` (risk >= ``whale_risk_tolerance``),
   ``WEATHER_BLOCKED``, ``WEATHER_EXPOSURE`` (normalized exposure >= ``weather_trigger_normalized``),
   ``LEASE_EXPIRING``, ``RETURN_OPPORTUNITY`` (off-route or slowed with no threat). With none, keep the
   plan without generating candidates (status ``INSUFFICIENT_FRESH_EVIDENCE`` when no usable reports
   exist: missing evidence is not declared absence).
2. **Gate**: an invalid map blocks changes (``INVALID_MAP``); ``cooldown_s`` since ``last_action_at``
   holds non-urgent changes (``COOLDOWN``); at most ``max_planned_ships`` ships plan per decision
   (``NOT_EVALUATED_BUDGET``).
3. **Common endpoint**: the first baseline index past the hazard plus bypass and taper (or the clip
   exit), never behind the active rejoin. Every candidate is scored from now to that point.
4. **Candidates**: <= 4 geometries x <= 3 speeds (``candidates.py``), complete paths (start -> detour ->
   explicit rejoin -> remaining baseline), timed from the forecast response delay.
5. **Hard checks before scoring** (``constraints.py``): land/hull clearance on every leg including the
   return and continuation (cached per geometry across speeds), region bounds, forward progress, the
   incident caps, the ``max_rejoin_s`` horizon, weather coverage and blocked zones at arrival times.
6. **Score** (Architecture §8.4)::

       J = w_whale*whale_risk + w_weather*exposure/ref_eta + w_delay*added_delay/ref_eta
         + w_distance*extra_distance/ref_distance + w_change*changed

   against the current plan to the same endpoint (the AIS baseline at first planning).
7. **Decide**: change only if J improves by ``min_objective_improvement``, whale risk is not materially
   worse, and a whale-only trigger reduces risk by ``min_whale_improvement``. If the current plan fails
   a hard check and nothing passes: ``NO_FEASIBLE_ROUTE``. A lease is renewed (new revision) only
   while its threat persists.

Scores are model proxies, not probabilities. F11-F14 are explicitly not modeled.
"""
from __future__ import annotations

import math
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from save_the_whales.advisor import candidates as K
from save_the_whales.advisor import weather as W
from save_the_whales.advisor.constraints import (ShipView, budget_reasons, forward_reasons, legs_xy,
                                                 path_reasons, planner_config, resolve_budget, timed_legs,
                                                 weather_reasons)
from save_the_whales.advisor.risk import parse_utc, path_risk, select_evidence
from save_the_whales.shared import contracts as C

ACTION, NO_ACTION, COOLDOWN = "ACTION", "NO_ACTION", "COOLDOWN"
WHALE_WATCH, WEATHER_EXPOSURE, LEASE_EXPIRING, RETURN_OPPORTUNITY = (
    "WHALE_WATCH", "WEATHER_EXPOSURE", "LEASE_EXPIRING", "RETURN_OPPORTUNITY")
NO_THREAT, ROUTE_COMPLETE, INVALID_INPUT = "NO_THREAT", "ROUTE_COMPLETE", "INVALID_INPUT"
CURRENT_PLAN_BEST, BENEFIT_BELOW_THRESHOLD = "CURRENT_PLAN_BEST", "BENEFIT_BELOW_THRESHOLD"
CURRENT_PLAN_INFEASIBLE, LEASE_RENEWED = "CURRENT_PLAN_INFEASIBLE", "LEASE_RENEWED"
EXCEEDS_MAX_REJOIN_S = "EXCEEDS_MAX_REJOIN_S"
ASSESSED_FACTORS = ["F1_LAND_CLEARANCE", "F2_WHALE_PROXIMITY", "F3_WEATHER", "F4_SIZE_SPEED",
                    "F5_DELAY", "F6_DISTANCE", "F7_FORWARD_REJOIN", "F8_INPUT_QUALITY",
                    "F9_ROUTE_STABILITY", "F10_COMPUTE_BUDGET"]
UNMODELED_FACTORS = ["F11_SHIP_SHIP", "F12_COLREG_TSS_LEGALITY", "F13_DEPTH_DRAFT", "F14_REALISTIC_DYNAMICS"]
ASSUMPTIONS = ["Whale motion unknown: stationary mean, uncertainty grows with age and lookahead.",
               "Speed severity weight (speed/service)^2 is illustrative.",
               "Instantaneous turns and speed changes; enclosing-circle hull clearance.",
               "Weather is a simulated field; scores are proxies, not probabilities."]


def _iso_plus(now_iso: str, seconds: float) -> str:
    dt = parse_utc(now_iso) + timedelta(milliseconds=int(round(seconds * 1000)))
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{dt.microsecond // 1000:03d}Z" if dt.microsecond // 1000 else base + "Z"


def _pt(geometry, xy) -> Dict[str, float]:
    p = geometry.unproject(xy[0], xy[1])
    return {"lat": p["lat"], "lon": p["lon"]}


def make_decider(config: Dict[str, Any], geometry):
    cfg = planner_config(config)

    def decide(observation: Dict[str, Any]) -> Dict[str, Any]:
        evidence, ev_stats = select_evidence(observation, cfg, geometry.project)
        actions, assessments, planned = [], [], 0
        for ship in sorted(observation.get("ships") or [], key=lambda s: str(s.get("ship_id"))):
            sp = _ShipPlanner(ship, observation, evidence, ev_stats, cfg, geometry)
            assessment, action = sp.run(planned < int(cfg["max_planned_ships"]))
            planned += int(sp.planned)
            assessments.append(assessment)
            if action is not None:
                actions.append(action)
        return {"decision_id": observation["decision_id"], "actions": actions, "assessments": assessments}

    return decide


def make_advise(config: Dict[str, Any], geometry):
    """Thin v2 wrapper ``advise(obs) -> list[Action]`` over the same decision implementation."""
    decide = make_decider(config, geometry)
    return lambda observation: decide(observation)["actions"]


class _ShipPlanner:
    def __init__(self, ship, obs, evidence, ev_stats, cfg, geometry):
        self.ship, self.obs, self.evidence, self.ev_stats = ship, obs, evidence, ev_stats
        self.cfg, self.geo = cfg, geometry
        self.now_s, self.now_iso = float(obs["sim_time_s"]), obs["now"]
        self.land, self.weather = obs["static"]["land"], obs["static"]["weather"]
        self.planned = False

    # --- output helpers ----------------------------------------------------------------------------
    def _assessment(self, status, reasons, path, costs, rejected=()):
        costs = dict(costs, evidence=dict(self.ev_stats), assessed_factors=list(ASSESSED_FACTORS),
                     unmodeled_factors=list(UNMODELED_FACTORS))
        return {"ship_id": self.ship.get("ship_id"), "status": status, "reason_codes": list(dict.fromkeys(reasons)),
                "selected_path": [dict(p) for p in path], "costs": costs, "rejected_candidates": list(rejected)}

    # --- main --------------------------------------------------------------------------------------
    def run(self, may_plan: bool):
        cfg, geo = self.cfg, self.geo
        try:
            v = ShipView(self.ship, geo, cfg)
        except (KeyError, TypeError, ValueError):
            return self._assessment(NO_ACTION, [INVALID_INPUT], [], {}), None
        self.v = v
        cur_points = [v.here] + v.route
        if not v.route:
            return self._assessment(NO_ACTION, [ROUTE_COMPLETE], cur_points, {}), None
        switch = None
        if v.active and v.sog < v.service:
            switch = self.now_s + (parse_utc(v.active["valid_until"]) - parse_utc(self.now_iso)).total_seconds()
        horizon = self.now_s + float(cfg["lookahead_s"])
        legs_h = timed_legs(cur_points, self.now_s, v.sog, geo, switch_s=switch, speed_after=v.service,
                            until_s=horizon)
        risk_h = path_risk(legs_xy(legs_h, geo), self.evidence, v.clearance_m, v.service, cfg)
        wx_h, wr_h = weather_reasons(legs_h, self.weather, geo)
        span_h = (legs_h[-1]["end_s"] - legs_h[0]["start_s"]) if legs_h else 0.0

        triggers, info = [], []
        if risk_h["whale_risk"] >= float(cfg["whale_risk_tolerance"]):
            triggers.append(WHALE_WATCH)
        if W.WEATHER_BLOCKED in wr_h:
            triggers.append(W.WEATHER_BLOCKED)
        elif wx_h is not None and span_h > 0 and wx_h / span_h >= float(cfg["weather_trigger_normalized"]):
            triggers.append(WEATHER_EXPOSURE)
        if C.UNKNOWN_WEATHER in wr_h:
            info.append(C.UNKNOWN_WEATHER)
        lease_left = None
        if v.active:
            lease_left = (parse_utc(v.active["valid_until"]) - parse_utc(self.now_iso)).total_seconds()
            if lease_left <= float(self.obs.get("decision_interval_min", 1)) * 60.0 + 1e-6:
                triggers.append(LEASE_EXPIRING)
        threat = any(t in triggers for t in (WHALE_WATCH, W.WEATHER_BLOCKED, WEATHER_EXPOSURE))
        if not threat and (not v.on_route or v.sog < v.service):
            triggers.append(RETURN_OPPORTUNITY)
        if not self.evidence:
            info.append(C.INSUFFICIENT_FRESH_EVIDENCE)
        screen_costs = {"whale_risk": risk_h["whale_risk"], "weather_exposure_s": wx_h,
                        "cpa": {k: risk_h[k] for k in ("cpa_m", "cpa_time_s", "detection_id", "sigma_m")},
                        "screen_horizon_s": float(cfg["lookahead_s"])}
        if not triggers:
            status = C.INSUFFICIENT_FRESH_EVIDENCE if not self.evidence else NO_ACTION
            return self._assessment(status, [NO_THREAT] + info, cur_points, screen_costs), None
        if not self.land or not self.land.get("validated"):
            return self._assessment(NO_ACTION, triggers + info + [C.INVALID_MAP], cur_points, screen_costs), None
        last = self.ship.get("last_action_at")
        if last and W.WEATHER_BLOCKED not in triggers and \
                (parse_utc(self.now_iso) - parse_utc(last)).total_seconds() < float(cfg["cooldown_s"]):
            return self._assessment(COOLDOWN, triggers + info, cur_points, screen_costs), None
        if not may_plan:
            return self._assessment(C.NOT_EVALUATED_BUDGET, triggers + info, cur_points, screen_costs), None
        self.planned = True
        return self._plan(triggers, info, cur_points, switch, risk_h, legs_h)

    # --- planning ----------------------------------------------------------------------------------
    def _hazard(self, triggers, risk_h, legs_h) -> Optional[Tuple[float, float, float]]:
        """(s_begin, s_end, signed side of the hazard) along the baseline, or None."""
        v = self.v
        if WHALE_WATCH in triggers:
            ev = next(e for e in self.evidence if e["detection_id"] == risk_h["detection_id"])
            s = v.line.project(ev["xy"], v.progress)[0]
            return s, s, v.line.side_of(s, ev["xy"])
        if W.WEATHER_BLOCKED in triggers or WEATHER_EXPOSURE in triggers:
            span = W.exposure_span(legs_h, self.weather, geometry=self.geo)
            if span is None:
                return None
            pts = []
            for i, f in span[:2]:
                (x0, y0), (x1, y1) = self.geo.project(legs_h[i]["start"]), self.geo.project(legs_h[i]["end"])
                pts.append((x0 + (x1 - x0) * f, y0 + (y1 - y0) * f))
            s0, s1 = sorted(v.line.project(p, v.progress)[0] for p in pts)
            poly = [self.geo.project(p) for p in span[2]["polygon"]]
            cx, cy = sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly)
            return s0, s1, v.line.side_of((s0 + s1) / 2, (cx, cy))
        return None

    def _plan(self, triggers, info, cur_points, switch, risk_h, legs_h):
        v, cfg, geo = self.v, self.cfg, self.geo
        rev = v.latest_revision + 1
        hazard = self._hazard(triggers, risk_h, legs_h)

        # Forecast response delay: the ship keeps its current plan until the change takes effect.
        delay = float(cfg["response_delay_s"])
        prefix = timed_legs(cur_points, self.now_s, v.sog, geo, switch_s=switch, speed_after=v.service,
                            until_s=self.now_s + delay) if delay > 0 else []
        start, t_start = (dict(prefix[-1]["end"]), prefix[-1]["end_s"]) if prefix else (dict(v.here), self.now_s)
        travelled = sum(geo.distance_m(l["start"], l["end"]) for l in prefix)
        rest_current, acc, passed = [], 0.0, v.progress
        tail0 = len(cur_points) - (v.n - v.cur_rejoin) if v.cur_rejoin is not None else len(cur_points)
        for i, (a, b) in enumerate(zip(cur_points, cur_points[1:]), start=1):
            acc += geo.distance_m(a, b)
            if acc > travelled + 1e-6:
                rest_current.append(dict(b))
            elif i >= tail0:                     # a baseline point reached during the forecast delay
                passed = max(passed, v.cur_rejoin + (i - tail0))
        s_start = v.arc_of(start)
        self.prefix_ends = [l["end"] for l in prefix]

        # Common forward endpoint (F7).
        half, taper = float(cfg["bypass_half_length_m"]), float(cfg["rejoin_taper_m"])
        s_need = (hazard[1] if hazard else s_start) + half + taper
        r = next((i for i in range(v.progress + 1, v.n) if v.line.arcs[i] >= s_need), v.n - 1)
        r = max(r, passed + 1, v.cur_rejoin or 0, int((v.budget or {}).get("rejoin_route_index", 0)))
        if r >= v.n:
            return self._assessment(NO_ACTION, triggers + info + [ROUTE_COMPLETE], cur_points, {}), None

        # Geometries (<= 4).
        geoms: List[Tuple[str, List[Dict[str, float]]]] = [(K.CURRENT, [dict(p) for p in v.cur_detour])]
        if not v.on_route:
            geoms.append((K.RETURN, []))
        if hazard and hazard[1] + 1.0 > s_start:
            s_min, s_max = s_start + float(cfg["min_lead_m"]), v.line.arcs[r] - 1.0
            o1, o2 = float(cfg["offsets_m"][0]), float(cfg["offsets_m"][-1])
            # A hazard inside the hull corridor has no meaningful "away" side (report noise decides it).
            away = 0 if abs(hazard[2]) <= v.clearance_m else (-1 if hazard[2] > 0 else 1)

            def leg(offset):
                xy = K.dogleg(v.line, hazard[0], hazard[1], offset, half, s_min, s_max)
                return None if xy is None else [_pt(geo, p) for p in xy]
            if v.on_route:
                left, right = leg(o1), leg(-o1)
                for kind, pts in ((K.DOGLEG_LEFT, left), (K.DOGLEG_RIGHT, right)):
                    if pts:
                        geoms.append((kind, pts))
                side = away or self._wide_side(((1, left), (-1, right)), start, t_start, r)
                wide = leg(side * o2)
                if wide:
                    geoms.append((K.DOGLEG_WIDE, wide))
            else:
                side = away or (1 if v.line.side_of(s_start, v.here_xy) >= 0 else -1)
                for kind, off in ((K.DOGLEG_LEFT if side > 0 else K.DOGLEG_RIGHT, side * o1), (K.DOGLEG_WIDE, side * o2)):
                    pts = leg(off)
                    if pts:
                        geoms.append((kind, pts))
        geoms = geoms[: int(cfg["max_geometries"])]

        speeds_new, speeds_cur = K.speed_set(v.service, v.sog, cfg)
        cands = [(kind, pts, spd) for kind, pts in geoms for spd in (speeds_cur if kind == K.CURRENT else speeds_new)]
        cands = cands[: int(cfg["max_candidates"])]

        # Reference: the current plan to the same endpoint, at service speed.
        cur_end_k = len(cur_points) - (v.n - r)
        ref_dist = max(1.0, geo.path_length_m(cur_points[:cur_end_k + 1]))
        ref_eta = ref_dist / geo.knots_to_mps(v.service)
        w = cfg
        evaluated, rejected = [], []
        geo_cache: Dict[Tuple, List[str]] = {}
        for kind, pts, spd in cands:
            cid = f"{kind}@{spd:g}"
            points = [start] + (rest_current if kind == K.CURRENT else pts + v.orig[r:])
            end_k = len(points) - (v.n - r)
            key = (kind, tuple((p["lat"], p["lon"]) for p in pts))
            if key not in geo_cache:
                geo_cache[key] = self._geometry_reasons(points, start, pts, r, check_forward=kind != K.CURRENT)
            reasons = list(geo_cache[key]) + (["ENDPOINT_PASSED"] if end_k < 1 else [])
            unchanged = kind == K.CURRENT and abs(spd - v.sog) < 1e-6
            legs = prefix + timed_legs(points[:max(end_k, 0) + 1], t_start, spd, geo,
                                       switch_s=switch if unchanged else None, speed_after=v.service)
            eta_end = legs[-1]["end_s"] if legs else t_start
            if eta_end - self.now_s > float(cfg["max_rejoin_s"]):
                reasons += [C.NOT_EVALUATED_BUDGET, EXCEEDS_MAX_REJOIN_S]
            exposure, wr = weather_reasons(legs, self.weather, geo)
            reasons += wr
            if not unchanged and (kind != K.CURRENT or not v.on_route):
                budget = resolve_budget(v, r, rev, geo, cfg)
                reasons += budget_reasons(budget, points[:end_k + 1], r, spd, v, geo)
            dist_end = sum(geo.distance_m(l["start"], l["end"]) for l in legs)
            added = (eta_end - self.now_s) - ref_eta
            if not unchanged and kind == K.CURRENT and v.on_route and added > float(cfg["max_added_delay_s"]) + 1e-6:
                reasons.append("DELAY_BUDGET_EXCEEDED")
            rec = {"candidate_id": cid, "kind": kind, "speed_knots": spd, "detour_waypoints": [dict(p) for p in pts],
                   "rejoin_route_index": r if kind != K.CURRENT else v.cur_rejoin, "points": points,
                   "legs": legs, "eta_end_s": eta_end, "unchanged": unchanged}
            if reasons:
                rejected.append({"candidate_id": cid, "kind": kind, "speed_knots": spd,
                                 "detour_waypoints": rec["detour_waypoints"],
                                 "reasons": list(dict.fromkeys(reasons))})
                continue
            risk = path_risk(legs_xy(legs, geo), self.evidence, v.clearance_m, v.service, cfg)
            contrib = {"whale": w["w_whale"] * risk["whale_risk"],
                       "weather": w["w_weather"] * (exposure / ref_eta),
                       "delay": w["w_delay"] * added / ref_eta,
                       "distance": w["w_distance"] * (dist_end - ref_dist) / ref_dist,
                       "change": 0.0 if unchanged else w["w_change"]}
            rec["costs"] = {"whale_risk": risk["whale_risk"], "weather_exposure_s": exposure,
                            "weather_cost_normalized": exposure / ref_eta, "added_delay_s": added,
                            "extra_distance_m": dist_end - ref_dist, "objective": sum(contrib.values()),
                            "contributions": contrib,
                            "cpa": {k: risk[k] for k in ("cpa_m", "cpa_time_s", "detection_id", "sigma_m")},
                            "reference": {"rejoin_route_index": r, "reference_distance_m": ref_dist,
                                          "reference_eta_s": ref_eta, "basis": "CURRENT_PLAN_TO_ENDPOINT"},
                            "candidates_evaluated": len(cands)}
            evaluated.append(rec)
        return self._select(triggers, info, evaluated, rejected, cands, r)

    def _wide_side(self, options, start, t_start, r) -> int:
        """Side for the larger offset when the hazard is on the line: the land-free first-offset side
        with less weather exposure at service speed (left on ties)."""
        v, best = self.v, None
        for sgn, pts in options:
            if not pts or self._geometry_reasons([start] + pts + v.orig[r:], start, pts, r):
                continue
            legs = timed_legs([start] + pts + [v.orig[r]], t_start, v.service, self.geo)
            exposure, wr = weather_reasons(legs, self.weather, self.geo)
            key = (math.inf if wr or exposure is None else exposure, -sgn)
            if best is None or key < best[0]:
                best = (key, sgn)
        return best[1] if best else 1

    def _geometry_reasons(self, points, start, detour, rejoin, check_forward=True) -> List[str]:
        v = self.v
        reasons = []
        if len(detour) > int(self.cfg["max_detour_waypoints"]):
            reasons.append("TOO_MANY_DETOUR_WAYPOINTS")
        if check_forward:
            reasons += forward_reasons(v, start, detour, rejoin, self.cfg)
        reasons += path_reasons(points, v, self.land, self.obs["bbox"], self.geo)[0]
        return reasons

    def _select(self, triggers, info, evaluated, rejected, cands, r):
        v, cfg = self.v, self.cfg
        c0 = next((c for c in evaluated if c["unchanged"]), None)
        whale_only = WHALE_WATCH in triggers and W.WEATHER_BLOCKED not in triggers and WEATHER_EXPOSURE not in triggers
        reasons = triggers + info
        if not evaluated:
            codes = {x for rc in rejected for x in rc["reasons"]}
            if codes and all(C.UNKNOWN_WEATHER in rc["reasons"] for rc in rejected):
                status = NO_ACTION
            elif codes and all(C.NOT_EVALUATED_BUDGET in rc["reasons"] for rc in rejected):
                status = C.NOT_EVALUATED_BUDGET
            else:
                status = C.NO_FEASIBLE_ROUTE
                reasons = reasons + [C.NO_FEASIBLE_ROUTE]
            return self._assessment(status, reasons, [v.here] + v.route, {"candidates_evaluated": len(cands)},
                                    rejected), None
        others = sorted((c for c in evaluated if not c["unchanged"]), key=lambda c: (c["costs"]["objective"], c["candidate_id"]))
        choice = None
        if c0 is None:
            reasons.append(CURRENT_PLAN_INFEASIBLE)
            choice = others[0]
        else:
            j0, risk0 = c0["costs"]["objective"], c0["costs"]["whale_risk"]
            for c in others:
                if j0 - c["costs"]["objective"] < float(cfg["min_objective_improvement"]):
                    break
                if c["costs"]["whale_risk"] > risk0 + float(cfg["whale_risk_tolerance"]):
                    continue
                if whale_only and risk0 - c["costs"]["whale_risk"] < float(cfg["min_whale_improvement"]):
                    continue
                choice = c
                break
            for c in others:
                if c is not choice:
                    rejected.append({"candidate_id": c["candidate_id"], "kind": c["kind"], "speed_knots": c["speed_knots"],
                                     "detour_waypoints": c["detour_waypoints"], "reasons": ["NOT_SELECTED"],
                                     "objective": c["costs"]["objective"]})
        if choice is None:
            threat = any(t in triggers for t in (WHALE_WATCH, W.WEATHER_BLOCKED, WEATHER_EXPOSURE))
            if LEASE_EXPIRING in triggers and threat and v.active:
                reasons.append(LEASE_RENEWED)
                return self._finish(ACTION, reasons, c0, rejected, renew=True)
            reasons.append(CURRENT_PLAN_BEST if not others or others[0]["costs"]["objective"] >= c0["costs"]["objective"]
                           else BENEFIT_BELOW_THRESHOLD)
            return self._finish(NO_ACTION, reasons, c0, rejected)
        return self._finish(ACTION, reasons, choice, rejected)

    def _finish(self, status, reasons, c, rejected, renew=False):
        v = self.v
        path = [v.here] + [dict(p) for p in self.prefix_ends] + c["points"][1:]   # through route completion
        action = self._action(c) if status == ACTION else None
        if action is not None:
            action["metadata"]["triggers"] = list(reasons)
            if renew:
                action["metadata"]["renewal_of"] = (v.active or {}).get("action_id")
        return self._assessment(status, reasons, path, c["costs"], rejected), action

    def _action(self, c) -> Dict[str, Any]:
        v, cfg = self.v, self.cfg
        rev = v.latest_revision + 1
        spd = c["speed_knots"]
        target = None if abs(spd - v.service) < 1e-6 else spd
        kind = c["kind"]
        if kind == K.CURRENT:
            if v.on_route:
                cmd, detour, rejoin = (C.SLOWDOWN, [], None) if target is not None else (C.RESUME, [], v.progress + 1)
            else:
                cmd = C.DETOUR if v.cur_detour else C.RESUME
                detour, rejoin = [dict(p) for p in v.cur_detour], v.cur_rejoin
        elif kind == K.RETURN:
            cmd, detour, rejoin = C.RESUME, [], c["rejoin_route_index"]
        else:
            cmd, detour, rejoin = C.DETOUR, [dict(p) for p in c["detour_waypoints"]], c["rejoin_route_index"]
        lease = min(float(cfg["max_rejoin_s"]), c["eta_end_s"] - self.now_s + float(cfg["lease_margin_s"]))
        action = {"ship_id": v.ship_id, "issued_at": self.now_iso, "command": cmd, "target_speed_knots": target,
                  "detour_waypoints": detour, "valid_until": _iso_plus(self.now_iso, lease),
                  "metadata": {"decision_id": self.obs["decision_id"], "candidate_id": c["candidate_id"],
                               "costs": c["costs"], "assessed_factors": list(ASSESSED_FACTORS),
                               "unmodeled_factors": list(UNMODELED_FACTORS), "assumptions": list(ASSUMPTIONS)},
                  "action_id": f"{v.ship_id}:r{rev}", "revision": rev}
        if rejoin is not None:
            action["rejoin_route_index"] = rejoin
        return action
