"""Person 3: whale risk, candidates, weather integral, planner decisions and the final validator.

Unit tests use fixed JSON observations (no simulator, no network). The closed-loop tests at the end
wire the real advisor into Person 2's simulator with Person 1's geometry.
"""
import copy
import json
import math
import sys
from functools import partial
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "fixtures" / "advisor"))

import adv_fixtures as A  # noqa: E402
from save_the_whales.advisor import constraints as K  # noqa: E402
from save_the_whales.advisor import risk as RK  # noqa: E402
from save_the_whales.advisor import weather as W  # noqa: E402
from save_the_whales.advisor.constraints import make_validator  # noqa: E402
from save_the_whales.advisor.planner import make_advise, make_decider  # noqa: E402
from save_the_whales.shared import contracts as C  # noqa: E402
from save_the_whales.shared import geo  # noqa: E402

CFG = A.config()
DECIDE = make_decider(CFG, geo)
VALIDATE = make_validator(CFG, geo)
FIXED_OBS = json.loads((ROOT / "tests" / "fixtures" / "advisor" / "obs_crossing.fixture.json").read_text())
P = lambda lat, lon: {"lat": lat, "lon": lon}  # noqa: E731
EVCFG = K.planner_config(CFG)


def only(decision, ship_id="sim:999000001:v1"):
    return next(a for a in decision["assessments"] if a["ship_id"] == ship_id)


def assert_no_hold(decision):
    assert all(a["command"] in C.COMMANDS for a in decision["actions"])


# --- risk: CPA, zero relative speed, aged reports, later-leg timing --------------------------------
def test_leg_cpa_known_case_and_zero_relative_speed():
    assert RK.leg_cpa((0, 0), (1000, 0), 100, 200, (500, 300)) == pytest.approx((150.0, 300.0))
    assert RK.leg_cpa((0, 0), (1000, 0), 100, 200, (-400, 300)) == pytest.approx((100.0, 500.0))   # clamped
    assert RK.leg_cpa((5, 5), (5, 5), 100, 400, (8, 9)) == pytest.approx((100.0, 5.0))             # stationary leg
    # Whale moving with the ship: zero relative velocity, distance constant, CPA at leg start.
    assert RK.leg_cpa((0, 0), (1000, 0), 0, 100, (0, 200), wv=(10.0, 0.0)) == pytest.approx((0.0, 200.0))


def test_aged_report_has_wider_uncertainty_and_lower_peak_risk():
    ev = {"detection_id": "r", "xy": (0.0, 0.0), "position_error_m": 100.0, "observed_s": 0.0, "age_s": 0.0}
    assert RK.sigma_at(ev, 0, EVCFG) == 100.0
    assert RK.sigma_at(ev, 600, EVCFG) == pytest.approx(math.hypot(100, 1.5 * 600))
    assert RK.sigma_at(dict(ev, position_error_m=0.0), 10, EVCFG) == 50.0        # floor
    legs = [((-1000.0, 0.0), (1000.0, 0.0), 0.0, 200.0, 18.0)]
    fresh = RK.path_risk(legs, [ev], 250.0, 18.0, EVCFG)["whale_risk"]
    old = RK.path_risk(legs, [dict(ev, observed_s=-1200.0)], 250.0, 18.0, EVCFG)["whale_risk"]
    assert fresh > old > 0
    slow = RK.path_risk([(legs[0][0], legs[0][1], 0.0, 200.0, 9.0)], [ev], 250.0, 18.0, EVCFG)["whale_risk"]
    assert slow == pytest.approx(fresh * 0.25)                                    # (speed/service)^2


def test_later_leg_risk_uses_its_actual_arrival_time():
    ev = {"detection_id": "r", "xy": (5000.0, 0.0), "position_error_m": 100.0, "observed_s": 0.0, "age_s": 0.0}
    legs = [((0.0, 0.0), (4000.0, 0.0), 0.0, 600.0, 18.0), ((4000.0, 0.0), (6000.0, 0.0), 600.0, 900.0, 18.0)]
    out = RK.path_risk(legs, [ev], 250.0, 18.0, EVCFG)
    assert out["leg_index"] == 1 and out["cpa_time_s"] == pytest.approx(750.0)
    assert out["sigma_m"] == pytest.approx(math.hypot(100, 1.5 * 750))          # not as if the leg began now
    naive = RK.path_risk([((4000.0, 0.0), (6000.0, 0.0), 0.0, 300.0, 18.0)], [ev], 250.0, 18.0, EVCFG)
    assert naive["whale_risk"] > out["whale_risk"]


def test_evidence_dedup_due_age_and_cap():
    obs = A.observation(t_s=2000.0, detections=[
        A.report(rid="a", observed_s=1990, reported_s=1995), A.report(rid="a", observed_s=1990, reported_s=1995),
        A.report(rid="future", observed_s=1990, reported_s=2100), A.report(rid="stale", observed_s=100, reported_s=120),
        A.report(rid="b", observed_s=1980, reported_s=1985)])
    ev, stats = RK.select_evidence(obs, dict(EVCFG, max_reports=1), geo.project)
    assert [e["detection_id"] for e in ev] == ["a"]
    assert (stats["duplicates"], stats["not_due"], stats["stale"], stats["capped"]) == (1, 1, 1, 1)
    # Duplicated or repeated reports never inflate risk (maximum over evidence).
    one = DECIDE(A.observation(detections=[A.report()]))
    many = DECIDE(A.observation(detections=[A.report(), A.report(), A.report(rid="rpt-2")]))
    assert only(one)["costs"]["whale_risk"] == pytest.approx(only(many)["costs"]["whale_risk"])


# --- weather integral --------------------------------------------------------------------------------
def _leg(a, b, s0, s1, kn=18.0):
    return {"start": a, "end": b, "start_s": s0, "end_s": s1, "speed_knots": kn}


def test_weather_exposure_max_severity_active_windows_and_unknown():
    field = A.storm(south=True)
    across = _leg(P(34.19, -120.08), P(34.19, -120.05), 0.0, 300.0)   # inside for lon in [-120.075, -120.055]
    assert W.weather_exposure([across], A.CLEAR, geometry=geo) == 0.0
    assert W.weather_exposure([across], field, geometry=geo) == pytest.approx(0.8 * 300 * 0.02 / 0.03, rel=1e-6)
    overlap = copy.deepcopy(field)
    overlap["zones"].append(dict(copy.deepcopy(field["zones"][0]), zone_id="weak", severity=0.3))
    assert W.weather_exposure([across], overlap, geometry=geo) == pytest.approx(0.8 * 200, rel=1e-6)   # max, not sum
    late = copy.deepcopy(field)
    late["zones"][0]["active_from_s"], late["zones"][0]["active_until_s"] = 150, 3600
    assert W.weather_exposure([across], late, geometry=geo) == pytest.approx(0.8 * (250 - 150), rel=1e-6)
    stay = _leg(P(34.19, -120.065), P(34.19, -120.065), 0.0, 100.0)
    assert W.weather_exposure([stay], field, geometry=geo) == pytest.approx(80.0)   # stationary still accrues
    with pytest.raises(ValueError, match=C.UNKNOWN_WEATHER):
        W.weather_exposure([_leg(P(34.19, -120.08), P(34.19, -120.05), 12000.0, 13000.0)], field, geometry=geo)
    with pytest.raises(ValueError, match=C.UNKNOWN_WEATHER):
        W.weather_exposure([across], None, geometry=geo)


def test_blocked_zone_hits_use_arrival_time_and_boundary_contact():
    field = A.storm(south=True, blocked=True, active=(100, 200))
    across = _leg(P(34.19, -120.08), P(34.19, -120.05), 0.0, 300.0)   # inside during [50, 250]
    assert W.blocked_hits([across], field, geometry=geo) == ["storm"]
    assert W.blocked_hits([dict(across, start_s=300.0, end_s=600.0)], field, geometry=geo) == []
    touch = _leg(P(34.180, -120.070), P(34.190, -120.080), 120.0, 160.0)   # passes the corner (34.185, -120.075) only
    assert W.blocked_hits([touch], field, geometry=geo) == ["storm"]


# --- planner: detour, slowdown, land-blocked side, invalid return, weather preference ---------------
def test_fixed_crossing_observation_yields_detour_with_explicit_forward_rejoin():
    d = DECIDE(FIXED_OBS)
    assert set(d) == set(C.Decision.__annotations__) and d["decision_id"] == FIXED_OBS["decision_id"]
    (act,) = d["actions"]
    a = only(d)
    assert act["command"] == C.DETOUR and a["status"] == "ACTION" and "WHALE_WATCH" in a["reason_codes"]
    ship = FIXED_OBS["ships"][0]
    assert ship["baseline_progress_index"] < act["rejoin_route_index"] < len(ship["original_route"])
    assert 1 <= len(act["detour_waypoints"]) <= 8 and act["revision"] == 1
    assert act["action_id"] == "sim:999000001:v1:r1" and act["issued_at"] == FIXED_OBS["now"]
    assert set(C.Action.__required_keys__) <= set(act)
    costs = a["costs"]
    for k in ("whale_risk", "weather_exposure_s", "weather_cost_normalized", "added_delay_s", "extra_distance_m",
              "objective"):
        assert isinstance(costs[k], float)
    assert costs["candidates_evaluated"] <= 12 and costs["reference"]["rejoin_route_index"] == act["rejoin_route_index"]
    assert "F11_SHIP_SHIP" in costs["unmodeled_factors"]
    assert a["selected_path"][0] == P(ship["lat"], ship["lon"]) and a["selected_path"][-1] == ship["original_route"][-1]
    res = VALIDATE(act, FIXED_OBS, purpose=C.PURPOSE_ISSUE)
    assert res["ok"], res["reasons"]
    va = res["validated_action"]
    assert set(va) == set(C.ValidatedAction.__annotations__) and va["action"] == act
    assert va["remaining_waypoints"] == act["detour_waypoints"] + ship["original_route"][act["rejoin_route_index"]:]
    assert va["incident_budget"]["max_distance_m"] == pytest.approx(1.5 * va["incident_budget"]["reference_distance_m"])
    assert_no_hold(d)


def test_both_sides_blocked_yields_slowdown_without_route_change():
    obs = A.observation(land=A.load_land("land_blocked_side.fixture.geojson"), weather=A.storm(blocked=True))
    d = DECIDE(obs)
    (act,) = d["actions"]
    assert act["command"] == C.SLOWDOWN and act["detour_waypoints"] == [] and act["target_speed_knots"] in (10.0, 8.0)
    reasons = {r for c in only(d)["rejected_candidates"] for r in c["reasons"]}
    assert {C.POINT_ON_LAND, W.WEATHER_BLOCKED} <= reasons
    assert VALIDATE(act, obs)["ok"]


def test_land_blocked_side_rejects_north_and_detours_south():
    obs = A.observation(land=A.load_land("land_blocked_side.fixture.geojson"))
    d = DECIDE(obs)
    (act,) = d["actions"]
    assert act["command"] == C.DETOUR and all(p["lat"] < 34.2 for p in act["detour_waypoints"])
    north = [c for c in only(d)["rejected_candidates"] if c["kind"] == "DOGLEG_LEFT"]
    assert north and all(c["reasons"][0] in (C.POINT_ON_LAND, C.LAND_CROSSING) for c in north)


def test_water_only_waypoints_with_land_crossing_return_are_rejected():
    obs = A.observation(land=A.load_land("land_blocked_side.fixture.geojson"))
    ship = obs["ships"][0]
    detour = [P(34.22, -120.08), P(34.22, -120.06)]
    land = obs["static"]["land"]
    assert all(geo.check_water_path([p], land, 0.0)["ok"] for p in detour)       # every waypoint is water
    act = {"ship_id": ship["ship_id"], "issued_at": obs["now"], "command": C.DETOUR, "target_speed_knots": None,
           "detour_waypoints": detour, "valid_until": A.iso(900), "metadata": {}, "action_id": "x:r1",
           "revision": 1, "rejoin_route_index": 2}
    res = VALIDATE(act, obs)
    assert not res["ok"] and C.LAND_CROSSING in res["reasons"]


def test_clear_side_preferred_when_otherwise_equal():
    for south_storm in (True, False):
        d = DECIDE(A.observation(weather=A.storm(south=south_storm)))
        (act,) = d["actions"]
        assert act["command"] == C.DETOUR
        on_north = all(p["lat"] > 34.2 for p in act["detour_waypoints"])
        assert on_north == south_storm                                         # away from the storm
        assert only(d)["costs"]["weather_exposure_s"] == 0.0
        assert all(c["objective"] >= only(d)["costs"]["objective"] for c in only(d)["rejected_candidates"]
                   if "objective" in c)


def test_blocked_weather_is_checked_at_arrival_time():
    zone = {"source": "SIMULATED", "coverage_start_s": 0, "coverage_end_s": 12600, "zones": [{
        "zone_id": "squall", "severity": 1.0, "blocked": True, "active_from_s": 0, "active_until_s": 100,
        "polygon": [P(34.198, -120.083), P(34.198, -120.079), P(34.202, -120.079), P(34.202, -120.083)]}]}
    gone = DECIDE(A.observation(weather=zone, detections=[]))                  # ship arrives ~170 s: expired
    assert gone["actions"] == [] and only(gone)["status"] == C.INSUFFICIENT_FRESH_EVIDENCE
    zone["zones"][0]["active_from_s"], zone["zones"][0]["active_until_s"] = 150, 400
    hit = DECIDE(A.observation(weather=zone, detections=[]))
    a = only(hit)
    assert W.WEATHER_BLOCKED in a["reason_codes"] and "CURRENT_PLAN_INFEASIBLE" in a["reason_codes"]
    assert hit["actions"] and hit["actions"][0]["command"] in (C.DETOUR, C.SLOWDOWN)


def test_no_feasible_route_is_explicit():
    wall = {"source": "SIMULATED", "coverage_start_s": 0, "coverage_end_s": 12600, "zones": [{
        "zone_id": "wall", "severity": 1.0, "blocked": True, "active_from_s": 0, "active_until_s": 12600,
        "polygon": [P(34.17, -120.085), P(34.17, -120.03), P(34.23, -120.03), P(34.23, -120.085)]}]}
    d = DECIDE(A.observation(weather=wall))
    assert d["actions"] == [] and only(d)["status"] == C.NO_FEASIBLE_ROUTE
    assert only(d)["rejected_candidates"] and all(W.WEATHER_BLOCKED in c["reasons"] for c in only(d)["rejected_candidates"])


def test_counts_budget_and_one_action_per_ship():
    ships = [A.ship_on_route(t, ship_id=f"sim:999000001:v{i}") for i, t in enumerate((0.0, 60.0, 120.0))]
    obs = A.observation(ships=ships)
    d = make_decider(dict(CFG, planner=dict(CFG["planner"], max_planned_ships=2)), geo)(obs)
    statuses = [a["status"] for a in d["assessments"]]
    assert statuses == ["ACTION", "ACTION", C.NOT_EVALUATED_BUDGET]
    assert len({a["ship_id"] for a in d["actions"]}) == len(d["actions"]) == 2
    for a in d["assessments"][:2]:
        assert a["costs"]["candidates_evaluated"] <= 12
        assert len(a["rejected_candidates"]) + 1 <= 12
    assert all(len(a["detour_waypoints"]) <= 8 for a in d["actions"])


def test_identical_observations_give_identical_decisions_and_hidden_fields_are_ignored():
    a = DECIDE(copy.deepcopy(FIXED_OBS))
    DECIDE(A.observation(weather=A.storm()))                                    # unrelated call in between
    b = DECIDE(json.loads(json.dumps(FIXED_OBS)))
    assert a == b
    noisy = dict(copy.deepcopy(FIXED_OBS), analyst_truth={"whales": [{"whale_id": "w", "lat": 34.0, "lon": -120.0}]})
    assert DECIDE(noisy) == a
    assert make_advise(CFG, geo)(copy.deepcopy(FIXED_OBS)) == a["actions"]


def test_screening_skips_planning_without_threat_and_gates():
    d = DECIDE(A.observation(detections=[]))
    assert d["actions"] == [] and only(d)["status"] == C.INSUFFICIENT_FRESH_EVIDENCE
    assert "candidates_evaluated" not in only(d)["costs"]
    far = DECIDE(A.observation(detections=[A.report(lat=34.5, lon=-119.2)]))
    assert far["actions"] == [] and only(far)["status"] == "NO_ACTION" and "NO_THREAT" in only(far)["reason_codes"]
    cool = A.observation(ships=[A.ship_on_route(60.0, last_action_at=A.iso(30))])
    assert DECIDE(cool)["actions"] == [] and only(DECIDE(cool))["status"] == "COOLDOWN"
    bad_map = A.observation(land=dict(A.load_land(), validated=False))
    dm = DECIDE(bad_map)
    assert dm["actions"] == [] and C.INVALID_MAP in only(dm)["reason_codes"]


def test_active_slowdown_without_threat_resumes():
    slow = {"ship_id": "sim:999000001:v1", "issued_at": A.iso(0), "command": C.SLOWDOWN, "target_speed_knots": 10.0,
            "detour_waypoints": [], "valid_until": A.iso(900), "metadata": {}, "action_id": "sim:999000001:v1:r1",
            "revision": 1}
    obs = A.observation(detections=[], ships=[A.ship_on_route(60.0, speed=10.0, active_command=slow, latest_revision=1)])
    d = DECIDE(obs)
    (act,) = d["actions"]
    assert act["command"] == C.RESUME and act["rejoin_route_index"] == 1 and act["revision"] == 2
    assert "RETURN_OPPORTUNITY" in only(d)["reason_codes"]
    va = VALIDATE(act, obs)["validated_action"]
    assert va["incident_budget"] is None and va["remaining_waypoints"] == obs["ships"][0]["current_route"]


# --- final validator ---------------------------------------------------------------------------------
def _detour():
    return copy.deepcopy(DECIDE(FIXED_OBS)["actions"][0])


@pytest.mark.parametrize("mutate,purpose,code", [
    (lambda a, o: a.pop("valid_until"), C.PURPOSE_ISSUE, K.MISSING_FIELDS),
    (lambda a, o: a.update(command="HOLD"), C.PURPOSE_ISSUE, K.UNSUPPORTED_COMMAND),
    (lambda a, o: a.update(ship_id="sim:nobody"), C.PURPOSE_ISSUE, K.UNKNOWN_SHIP),
    (lambda a, o: a.update(revision=0), C.PURPOSE_ISSUE, C.SUPERSEDED),
    (lambda a, o: a.update(revision=True), C.PURPOSE_ISSUE, K.BAD_FIELD_TYPE),
    (lambda a, o: None, C.PURPOSE_APPLY, C.SUPERSEDED),                         # latest_revision is still 0
    (lambda a, o: a.update(valid_until=o["now"]), C.PURPOSE_ISSUE, C.STALE_ACTION),
    (lambda a, o: a.update(issued_at=A.iso(-600)), C.PURPOSE_ISSUE, C.STALE_ACTION),
    (lambda a, o: a.update(valid_until=A.iso(60 + 9000)), C.PURPOSE_ISSUE, K.LEASE_TOO_LONG),
    (lambda a, o: a["detour_waypoints"].append(P(35.0, -120.05)), C.PURPOSE_ISSUE, K.OUT_OF_BOUNDS),
    (lambda a, o: a["detour_waypoints"].__setitem__(0, P(float("nan"), -120.07)), C.PURPOSE_ISSUE, C.NON_FINITE_COORDINATE),
    (lambda a, o: a.update(target_speed_knots=4.0), C.PURPOSE_ISSUE, K.SPEED_OUT_OF_BOUNDS),
    (lambda a, o: a.update(target_speed_knots=25.0), C.PURPOSE_ISSUE, K.SPEED_OUT_OF_BOUNDS),
    (lambda a, o: a.update(detour_waypoints=[P(34.19, -120.09 + 0.002 * i) for i in range(9)]), C.PURPOSE_ISSUE,
     K.TOO_MANY_DETOUR_WAYPOINTS),
    (lambda a, o: a.update(rejoin_route_index=0), C.PURPOSE_ISSUE, C.STALE_ACTION),
    (lambda a, o: a.update(rejoin_route_index=9), C.PURPOSE_ISSUE, K.INVALID_REJOIN),
    (lambda a, o: a.pop("rejoin_route_index"), C.PURPOSE_ISSUE, K.INVALID_REJOIN),
    (lambda a, o: a.update(detour_waypoints=[P(34.19, -120.06), P(34.19, -120.08)]), C.PURPOSE_ISSUE, K.BACKTRACKING),
    (lambda a, o: a.update(detour_waypoints=[P(34.15, -120.08), P(34.15, -120.06)]), C.PURPOSE_ISSUE,
     K.DISTANCE_BUDGET_EXCEEDED),
    (lambda a, o: o["static"].update(land=dict(o["static"]["land"], validated=False)), C.PURPOSE_ISSUE, C.INVALID_MAP),
    (lambda a, o: o["static"].update(weather=dict(o["static"]["weather"], coverage_end_s=100)), C.PURPOSE_ISSUE,
     C.UNKNOWN_WEATHER),
    (lambda a, o: a.update(command=C.SLOWDOWN, target_speed_knots=10.0), C.PURPOSE_ISSUE, K.SLOWDOWN_CHANGES_PATH),
])
def test_validator_rejects(mutate, purpose, code):
    act, obs = _detour(), copy.deepcopy(FIXED_OBS)
    assert VALIDATE(act, obs, purpose=purpose if purpose != C.PURPOSE_APPLY else C.PURPOSE_ISSUE)["ok"]
    mutate(act, obs)
    res = VALIDATE(act, obs, purpose=purpose)
    assert not res["ok"] and code in res["reasons"] and res["validated_action"] is None


def test_validator_apply_rejects_geometry_the_ship_has_passed():
    act = _detour()
    obs = A.observation(t_s=60.0, ships=[A.ship_on_route(60.0, latest_revision=1)])
    assert VALIDATE(act, obs, purpose=C.PURPOSE_APPLY)["ok"]
    later = A.observation(t_s=330.0, ships=[A.ship_on_route(330.0, latest_revision=1)])
    res = VALIDATE(act, later, purpose=C.PURPOSE_APPLY)
    assert not res["ok"] and res["reasons"][:2] == [C.STALE_ACTION, K.BACKTRACKING]


def _on_detour_obs(act, t_s, weather=A.CLEAR):
    """Ship between the two dogleg points, rejoin pending, lease state given by ``act``."""
    b1, b2 = act["detour_waypoints"]
    (x1, y1), (x2, y2) = geo.project(b1), geo.project(b2)
    here = geo.unproject((x1 + x2) / 2, (y1 + y2) / 2)
    ship = A.ship_on_route(60.0, lat=here["lat"], lon=here["lon"], latest_revision=1, active_command=act,
                           current_route=[b2] + A.ROUTE[act["rejoin_route_index"]:], baseline_progress_index=0)
    return A.observation(t_s=t_s, weather=weather, ships=[ship])


def test_expiry_fallback_uses_unpassed_route_and_keeps_physical_checks():
    act = _detour()
    obs = _on_detour_obs(act, t_s=2000.0)                                       # long after valid_until
    fb = dict(act, detour_waypoints=[act["detour_waypoints"][1]], target_speed_knots=18.0, issued_at=obs["now"],
              action_id=act["action_id"] + ":expiry-fallback", metadata={"executor": "EXPIRY_FALLBACK"})
    assert not VALIDATE(fb, obs, purpose=C.PURPOSE_APPLY)["ok"]                  # expired for normal purposes
    ok = VALIDATE(fb, obs, purpose=C.PURPOSE_EXPIRY_FALLBACK)
    assert ok["ok"], ok["reasons"]
    stale_points = dict(fb, detour_waypoints=act["detour_waypoints"])
    assert K.FALLBACK_PATH_MISMATCH in VALIDATE(stale_points, obs, purpose=C.PURPOSE_EXPIRY_FALLBACK)["reasons"]
    newer = dict(fb, revision=2)
    assert C.SUPERSEDED in VALIDATE(newer, obs, purpose=C.PURPOSE_EXPIRY_FALLBACK)["reasons"]
    south = act["detour_waypoints"][0]["lat"] < 34.2
    stormy = _on_detour_obs(act, t_s=2000.0, weather=A.storm(blocked=True, south=south, active=(0, 12600)))
    assert W.WEATHER_BLOCKED in VALIDATE(fb, stormy, purpose=C.PURPOSE_EXPIRY_FALLBACK)["reasons"]


# --- closed loop: Person 1 geometry + Person 2 simulator + Person 3 advisor --------------------------
def _closed_loop(weather=None, land_name="land_far.fixture.geojson", decide=None):
    sys.path.insert(0, str(ROOT / "tests" / "fixtures" / "simulation"))
    import sim_fixtures as F
    from save_the_whales.simulation.engine import run_scenario
    from save_the_whales.sources import land as landmod, routes
    cfg = F.fixture_config()
    cfg["planner"] = dict(CFG["planner"])
    land = landmod.load_land(F.COMMON / land_name, cfg)
    bundle = routes.load_route_bundle(F.COMMON / "route_bundle.fixture.json", land, cfg)
    scen = F.fixture_scenario(**({"weather": weather} if weather else {}))
    inputs = F.make_inputs(bundle, land, scen, cfg)
    return run_scenario(inputs, decide or make_decider(cfg, geo), make_validator(cfg, geo),
                        partial(W.weather_exposure, geometry=geo), project=geo.project)


def test_closed_loop_whale_crossing_gives_accepted_route_change_and_forward_rejoin():
    sys.path.insert(0, str(ROOT / "tests" / "fixtures" / "simulation"))
    import sim_fixtures as F
    base, adv = _closed_loop(decide=F.no_action), _closed_loop()
    s = adv["summary"]
    assert s["status"] == C.RUN_COMPLETE and s["route_changes"] >= 1 and s["rejected_actions"] == 0
    applied = [e for e in adv["events"] if e["type"] == "COMMAND_APPLIED"]
    assert applied[0]["details"]["command"] == C.DETOUR and any(e["type"] == "REJOINED" for e in adv["events"])
    assert any(f["ships"] and abs(f["ships"][0]["lat"] - 34.2) > 0.005 for f in adv["frames"])   # really off-route
    assert base["summary"]["modeled_contacts"] == 1 and s["modeled_contacts"] == 0
    assert s["whale_exposure_proxy"] < base["summary"]["whale_exposure_proxy"]
    assert s["added_delay_s"] > 0 and s["extra_distance_m"] > 0
    for d in adv["decisions"]:
        assert_no_hold(d)


def test_closed_loop_storm_detours_on_the_clear_side():
    res = _closed_loop(weather=A.storm(south=True))
    lats = [f["ships"][0]["lat"] for f in res["frames"] if f["ships"]]
    assert max(lats) > 34.205 and min(lats) >= 34.2 - 1e-9
    assert res["summary"]["weather_exposure_s"] == 0.0 and res["summary"]["route_changes"] >= 1


def test_closed_loop_with_response_lag_plans_from_the_forecast_position():
    sys.path.insert(0, str(ROOT / "tests" / "fixtures" / "simulation"))
    import sim_fixtures as F
    from save_the_whales.simulation.engine import run_scenario
    from save_the_whales.sources import land as landmod, routes
    cfg = F.fixture_config()
    cfg["planner"] = dict(CFG["planner"])
    cfg["response"]["response_lag_s"] = 45
    land = landmod.load_land(F.COMMON / "land_far.fixture.geojson", cfg)
    bundle = routes.load_route_bundle(F.COMMON / "route_bundle.fixture.json", land, cfg)
    res = run_scenario(F.make_inputs(bundle, land, F.fixture_scenario(), cfg), make_decider(cfg, geo),
                       make_validator(cfg, geo), partial(W.weather_exposure, geometry=geo), project=geo.project)
    s = res["summary"]
    assert s["rejected_actions"] == 0 and s["route_changes"] == 1 and s["modeled_contacts"] == 0
    assert [e["sim_time_s"] for e in res["events"] if e["type"] == "COMMAND_APPLIED"] == [105.0]


def test_advisor_imports_no_simulator_sources_ui_or_network():
    import ast
    banned = ("save_the_whales.simulation", "save_the_whales.sources", "save_the_whales.ui", "streamlit",
              "websockets", "socket", "urllib", "requests", "random", "time")
    for path in (ROOT / "save_the_whales" / "advisor").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            assert not [n for n in names if n.split(".")[0] in banned or n.startswith(banned)], (path.name, names)
