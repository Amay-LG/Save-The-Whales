"""Person 2: simulator, sightings, command execution and truth metrics (no network, no real planner)."""
import copy
import json
import math
import random
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "fixtures" / "simulation"))

import sim_fixtures as F  # noqa: E402
from save_the_whales.shared import contracts as C  # noqa: E402
from save_the_whales.shared import geo  # noqa: E402
from save_the_whales.simulation import metrics as M  # noqa: E402
from save_the_whales.simulation.engine import ANALYST_TRUTH_LABEL, run_scenario  # noqa: E402
from save_the_whales.simulation.ships import Piece  # noqa: E402
from save_the_whales.simulation.whales import Whale, WhalePiece, make_background_whales  # noqa: E402
from save_the_whales.simulation.world import Frame, canonical_sha256  # noqa: E402
from save_the_whales.sources import land as landmod, routes  # noqa: E402

COMMON = F.COMMON
SPEED = geo.knots_to_mps(18.0)


def _deep_update(base, over):
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
    return base


def setup(land_name="land_far.fixture.geojson", cfg_over=None):
    cfg = _deep_update(F.fixture_config(), cfg_over)
    land = landmod.load_land(COMMON / land_name, cfg)
    bundle = routes.load_route_bundle(COMMON / "route_bundle.fixture.json", land, cfg)
    return cfg, land, bundle


def run(decide=F.no_action, *, scenario=None, land_name="land_far.fixture.geojson", cfg_over=None,
        wrap=None, weather=F.zero_weather, project=geo.project, on_frame=None):
    cfg, land, bundle = setup(land_name, cfg_over)
    inputs = F.make_inputs(bundle, land, scenario or F.fixture_scenario(), cfg)
    validator = F.make_fixture_validator(cfg, geo)
    if wrap:
        validator = wrap(validator)
    return run_scenario(inputs, decide, validator, weather, project=project, on_frame=on_frame), inputs


def types(res):
    return [e["type"] for e in res["events"]]


def events(res, etype):
    return [e for e in res["events"] if e["type"] == etype]


def strip_wall(res):
    out = copy.deepcopy(res)
    out["summary"].pop("elapsed_wall_s")
    return out


def logging_validator(log):
    def wrap(v):
        def inner(action, obs, *, purpose="ISSUE"):
            log.append((purpose, obs["sim_time_s"], obs["ships"][0]["lon"] if obs["ships"] else None))
            return v(action, obs, purpose=purpose)
        return inner
    return wrap


# --- Completion check: a seeded fixture run without Person 1's API worker or Person 3's planner -----
def test_fixture_run_completes_with_private_projection_and_contract_shapes():
    res, _ = run(project=F.fixture_project)
    s = res["summary"]
    assert set(res) == set(C.RunResult.__annotations__)
    assert set(s) == set(C.RunSummary.__annotations__)
    assert s["status"] == C.RUN_COMPLETE and s["voyages_completed"] == 1 and s["voyages_failed"] == 0
    assert s["modeled_contacts"] == 1 and s["close_approaches"] == 1      # baseline runs over the whale
    assert abs(s["added_delay_s"]) < 1e-6 and abs(s["extra_distance_m"]) < 1e-6
    assert res["manifest"]["fixture_only"] is True and res["manifest"]["mode"] == "AIS_SEEDED_SIMULATION"
    v = res["manifest"]["voyages"][0]
    assert v["ship_id"] == "sim:999000001:v1" and v["source_mmsi"] == "999000001"
    # Same result with the injected shared projection.
    res2, _ = run(project=geo.project)
    assert strip_wall(res)["summary"] == strip_wall(res2)["summary"]


def test_projection_inverse_round_trips():
    fr = Frame(geo.project)
    for lat, lon in [(34.2, -119.75), (33.9, -120.4), (34.5, -119.1), (34.2, -120.0001)]:
        p = fr.point(*geo.project({"lat": lat, "lon": lon}))
        assert abs(p["lat"] - lat) < 1e-9 and abs(p["lon"] - lon) < 1e-9


# --- Completion check: scripted detour, checksum, forward rejoin, expiry without teleport ----------
def test_scripted_detour_changes_motion_and_rejoins_forward():
    cfg, land, bundle = setup()
    before = canonical_sha256(bundle)
    res, inputs = run(F.make_scripted_decider({60: F.detour_at()}), cfg_over={"simulation": {"frame_every_s": 30}})
    assert canonical_sha256(inputs["route_bundle"]) == before                 # caller's bundle untouched
    assert res["manifest"]["route_bundle"]["sha256"] == before
    s = res["summary"]
    assert s["status"] == C.RUN_COMPLETE and s["route_changes"] == 1 and s["alerts"] == 1
    assert s["modeled_contacts"] == 0 and s["added_delay_s"] > 0 and s["extra_distance_m"] > 0
    assert "REJOINED" in types(res)
    frames = [f for f in res["frames"] if f["ships"]]
    assert min(f["ships"][0]["lat"] for f in frames) < 34.1955               # really went south
    progress = [f["ships"][0]["baseline_progress_index"] for f in frames]
    assert progress == sorted(progress)                                        # monotonic, never reset
    orig = bundle["routes"][0]["waypoints"]
    assert all(f["ships"][0]["original_route"] == orig for f in frames)
    _assert_no_teleport(res)


def _assert_no_teleport(res, speed=SPEED):
    pts = [(f["sim_time_s"], f["ships"][0]) for f in res["frames"] if f["ships"]]
    for (t0, a), (t1, b) in zip(pts, pts[1:]):
        assert geo.distance_m(a, b) <= speed * (t1 - t0) + 1.0


def test_detour_lease_expiry_uses_validated_fallback_to_forward_rejoin():
    log = []
    res, _ = run(F.make_scripted_decider({60: F.detour_at(lease_s=120)}), wrap=logging_validator(log),
                 cfg_over={"simulation": {"frame_every_s": 30}})
    assert [p for p, _, _ in log] == [C.PURPOSE_ISSUE, C.PURPOSE_APPLY, C.PURPOSE_EXPIRY_FALLBACK]
    order = types(res)
    assert order.index("EXPIRY_FALLBACK") < order.index("REJOINED") < order.index("VOYAGE_COMPLETE")
    assert res["summary"]["status"] == C.RUN_COMPLETE
    _assert_no_teleport(res)


def test_slowdown_expiry_restores_service_speed():
    script = {60: lambda o: [F.make_action(o["ships"][0], o["now"], C.SLOWDOWN, lease_s=180, speed=10.0)]}
    res, _ = run(F.make_scripted_decider(script), cfg_over={"simulation": {"frame_every_s": 30}})
    sog = {f["sim_time_s"]: f["ships"][0]["sog_knots"] for f in res["frames"] if f["ships"]}
    assert sog[120.0] == 10.0 and sog[270.0] == 18.0
    assert "LEASE_EXPIRED" in types(res) and res["summary"]["status"] == C.RUN_COMPLETE
    assert res["summary"]["added_delay_s"] > 0 and res["summary"]["route_changes"] == 0


def test_failed_expiry_fallback_stops_voyage_incomplete():
    def wrap(v):
        def inner(action, obs, *, purpose="ISSUE"):
            if purpose == C.PURPOSE_EXPIRY_FALLBACK:
                return {"ok": False, "reasons": ["LAND_CROSSING"], "validated_action": None}
            return v(action, obs, purpose=purpose)
        return inner
    res, _ = run(F.make_scripted_decider({60: F.detour_at(lease_s=120)}), wrap=wrap)
    s = res["summary"]
    assert s["status"] == C.RUN_INCOMPLETE and s["voyages_failed"] == 1
    assert s["added_delay_s"] is None and s["extra_distance_m"] is None
    assert "VOYAGE_FAILED:EXPIRY_FALLBACK_FAILED" in res["manifest"]["status_reasons"]
    assert res["manifest"]["voyages"][0]["censored"] is True


# --- Completion check: lag / ignored / superseded / duplicate --------------------------------------
def test_response_lag_revalidates_from_moved_position():
    log = []
    res, _ = run(F.make_scripted_decider({60: F.detour_at()}), wrap=logging_validator(log),
                 cfg_over={"response": {"response_lag_s": 45}})
    (p1, t1, lon1), (p2, t2, lon2) = log
    assert (p1, t1, p2, t2) == (C.PURPOSE_ISSUE, 60.0, C.PURPOSE_APPLY, 105.0) and lon2 > lon1
    assert events(res, "COMMAND_APPLIED")[0]["sim_time_s"] == 105.0


def test_ignored_command_is_seeded_and_reproducible():
    over = {"response": {"compliance": 0.0}}
    res, _ = run(F.make_scripted_decider({60: F.detour_at()}), cfg_over=over)
    assert "COMMAND_IGNORED" in types(res) and res["summary"]["route_changes"] == 0
    assert res["summary"]["alerts"] == 1 and res["summary"]["modeled_contacts"] == 1
    res2, _ = run(F.make_scripted_decider({60: F.detour_at()}), cfg_over=over)
    assert strip_wall(res) == strip_wall(res2)


def test_superseded_duplicate_and_stale_revisions():
    sent = {}

    def first(o):
        sent["r1"] = F.make_action(o["ships"][0], o["now"], C.DETOUR, detour=F.SOUTH_DETOUR, rejoin=2)
        return [sent["r1"]]

    def second(o):
        sent["r2"] = F.make_action(o["ships"][0], o["now"], C.DETOUR, rejoin=2,
                                   detour=[{"lat": 34.194, "lon": -120.07}, {"lat": 34.194, "lon": -120.058}])
        return [sent["r2"]]

    script = {60: first, 120: second, 180: lambda o: [copy.deepcopy(sent["r2"])],
              240: lambda o: [dict(sent["r1"], action_id="stale-r1")]}
    res, _ = run(F.make_scripted_decider(script), cfg_over={"response": {"response_lag_s": 90}})
    sup = events(res, "ACTION_SUPERSEDED")
    assert [e["details"]["action_id"] for e in sup] == [sent["r1"]["action_id"]]
    assert [e["details"]["action_id"] for e in events(res, "COMMAND_APPLIED")] == [sent["r2"]["action_id"]]
    assert len(events(res, "ACTION_DUPLICATE_IGNORED")) == 1
    rej = events(res, "ACTION_REJECTED")
    assert len(rej) == 1 and C.SUPERSEDED in rej[0]["details"]["reasons"]
    assert res["summary"]["rejected_actions"] == 1 and res["summary"]["route_changes"] == 1


def test_conflicting_hold_and_unknown_ship_rejected_visibly():
    def acts(o):
        ship = o["ships"][0]
        a = F.make_action(ship, o["now"], C.SLOWDOWN, speed=10.0)
        return [a, dict(a, action_id="other")]
    res, _ = run(F.make_scripted_decider({
        60: acts,
        120: lambda o: [dict(F.make_action(o["ships"][0], o["now"], C.SLOWDOWN, speed=10.0), command="HOLD")],
        180: lambda o: [dict(F.make_action(o["ships"][0], o["now"], C.SLOWDOWN, speed=10.0), ship_id="sim:x:y")]}))
    reasons = [e["details"]["reasons"] for e in events(res, "ACTION_REJECTED")]
    assert reasons[0][1] == reasons[1][1] == "CONFLICTING_ACTIONS"
    assert reasons[2][1] == "UNSUPPORTED_COMMAND" and reasons[3][1] == "UNKNOWN_SHIP"
    assert res["summary"]["rejected_actions"] == 4 and res["summary"]["alerts"] == 0


def test_land_blocked_side_detour_is_rejected_by_shared_geometry():
    res, _ = run(F.make_scripted_decider({60: F.detour_at(detour=F.NORTH_DETOUR)}),
                 land_name="land_blocked_side.fixture.geojson")
    rej = events(res, "ACTION_REJECTED")
    assert rej and rej[0]["details"]["reasons"][0] == C.POINT_ON_LAND
    assert res["summary"]["route_changes"] == 0 and res["summary"]["status"] == C.RUN_COMPLETE


def test_no_feasible_route_pauses_voyage_and_insufficient_evidence_does_not():
    nf = {120: lambda o: [{"ship_id": o["ships"][0]["ship_id"], "status": C.NO_FEASIBLE_ROUTE,
                           "reason_codes": [C.NO_FEASIBLE_ROUTE], "selected_path": [], "costs": {},
                           "rejected_candidates": []}]}
    res, _ = run(F.make_scripted_decider({}, assessments=nf))
    s = res["summary"]
    assert s["status"] == C.RUN_INCOMPLETE and s["voyages_failed"] == 1 and s["added_delay_s"] is None
    assert events(res, "VOYAGE_FAILED")[0]["details"]["reason"] == C.NO_FEASIBLE_ROUTE
    ie = {120: lambda o: [{"ship_id": o["ships"][0]["ship_id"], "status": C.INSUFFICIENT_FRESH_EVIDENCE,
                           "reason_codes": [C.INSUFFICIENT_FRESH_EVIDENCE], "selected_path": [], "costs": {},
                           "rejected_candidates": []}]}
    res2, _ = run(F.make_scripted_decider({}, assessments=ie))
    assert res2["summary"]["status"] == C.RUN_COMPLETE and res2["summary"]["voyages_completed"] == 1


def test_resume_during_detour_returns_forward_without_shortcut_past_rejoin():
    script = {60: F.detour_at(),
              180: lambda o: [F.make_action(o["ships"][0], o["now"], C.RESUME, rejoin=2)]}
    res, _ = run(F.make_scripted_decider(script))
    applied = events(res, "COMMAND_APPLIED")
    assert [e["details"]["command"] for e in applied] == [C.DETOUR, C.RESUME]
    assert applied[1]["details"]["rejoin_route_index"] == 2 and "REJOINED" in types(res)
    assert res["summary"]["route_changes"] == 2 and res["summary"]["status"] == C.RUN_COMPLETE


# --- Completion check: swept contact, exit dwell, hidden truth, reproducibility --------------------
def test_between_tick_contact_counted_once_even_with_huge_steps():
    big = {"simulation": {"dt_s": 600, "decision_dt_s": 600, "frame_every_s": 600}, "sensors": {"sample_dt_s": 600}}
    res, _ = run(cfg_over=big)
    assert res["summary"]["modeled_contacts"] == 1 and res["summary"]["close_approaches"] == 1
    # Whale exactly on a waypoint: the contact spans a piece boundary and still counts once.
    on_wp = F.fixture_scenario()
    on_wp["whales"][0]["start"] = {"lat": 34.2, "lon": -120.075}
    res2, _ = run(scenario=on_wp)
    assert res2["summary"]["modeled_contacts"] == 1 and res2["summary"]["close_approaches"] == 1


def test_deep_whale_gives_close_approach_but_no_contact():
    scen = F.fixture_scenario()
    scen["whales"][0]["exposure"] = [{"from_s": 0, "until_s": 100000, "exposed": False}]
    res, _ = run(scenario=scen)
    assert res["summary"]["close_approaches"] == 1 and res["summary"]["modeled_contacts"] == 0
    assert res["summary"]["whale_exposure_proxy"] == 0.0


def _piece(ta, tb, x0, x1, speed_knots=18.0):
    v = (x1 - x0) / (tb - ta)
    return Piece(ta, tb, x0, 0.0, v, 0.0, speed_knots, {"lat": 0, "lon": 0}, {"lat": 0, "lon": 0})


class _FakeShip:
    ship_id, length_m, beam_m, service_speed_knots = "s", 0.0, 0.0, 18.0


class _FakeWhale:
    whale_id = "w"


def _tracker():
    got = []
    tr = M.EncounterTracker({"whale_body_radius_m": 0.0}, lambda t, k, s, d: got.append((round(t, 6), k)))
    return tr, got


def test_episode_exit_requires_continuous_dwell():
    # Ship sits at x (1-D) while a stationary whale is at 0; clearance == |x|.
    def stay(ta, tb, x):
        return Piece(ta, tb, x, 0.0, 0.0, 0.0, 18.0, {}, {})
    w = [(_FakeWhale, [WhalePiece(0, 1000, 0.0, 0.0, 0.0, 0.0, True)])]
    tr, got = _tracker()
    seq = [stay(0, 10, 400), stay(10, 110, 800), stay(110, 120, 700), stay(120, 250, 800), stay(250, 260, 400)]
    for p in seq:
        tr.evaluate(_FakeShip, [p], [(wh, [wp for wp in pcs if wp.tb > p.ta and wp.ta < p.tb]) for wh, pcs in w])
    # 100 s outside (< 120) does not end the episode; 130 s does (exit at 240); 400 m again re-enters.
    assert [k for _, k in got] == ["ENCOUNTER_START", "ENCOUNTER_END", "ENCOUNTER_START"]
    assert got[1][0] == 240.0 and tr.close_approaches == 2 and tr.contacts == 0


def test_swept_contact_detected_between_endpoints():
    tr, got = _tracker()
    # Endpoints are 1000 m either side of the whale; only the swept check sees the pass.
    tr.evaluate(_FakeShip, [_piece(0, 100, -1000, 1000)], [(_FakeWhale, [WhalePiece(0, 100, 0, 0, 0, 0, True)])])
    assert tr.contacts == 1 and tr.close_approaches == 1


def test_exposure_proxy_closed_form_matches_quadrature():
    rnd = random.Random(3)
    for _ in range(20):
        rx, ry, vx, vy = (rnd.uniform(-2000, 2000) for _ in range(4))
        vx, vy, T, sigma = vx / 200, vy / 200, rnd.uniform(10, 900), 300.0
        n = 4000
        h = T / n
        f = [math.exp(-((rx + vx * s) ** 2 + (ry + vy * s) ** 2) / (2 * sigma ** 2)) * math.hypot(vx, vy) / sigma
             for s in (i * h for i in range(n + 1))]
        simpson = h / 3 * (f[0] + f[-1] + 4 * sum(f[1:-1:2]) + 2 * sum(f[2:-1:2]))
        assert M.gaussian_proxy(rx, ry, vx, vy, T, sigma) == pytest.approx(simpson, rel=1e-6, abs=1e-12)


def test_advisor_sees_only_observable_state_and_cannot_mutate_world():
    log = []
    res, _ = run(F.make_scripted_decider({}, log=log))
    assert log and res["summary"]["status"] == C.RUN_COMPLETE
    obs_keys = set(C.Observation.__annotations__)
    for obs in log:
        assert set(obs) == obs_keys and set(obs["static"]) == {"land", "weather"}
        text = json.dumps(obs)
        assert "w-cross" not in text and "analyst_truth" not in text and "exposed" not in text
        for d in obs["whale_detections"]:
            assert set(C.WhaleDetection.__required_keys__) <= set(d) <= set(C.WhaleDetection.__annotations__)
            assert d["detection_id"].startswith("rpt-") and d["reported_at"] <= obs["now"]
        for s in obs["ships"]:
            assert set(C.ObservableShip.__required_keys__) <= set(s) <= set(C.ObservableShip.__annotations__)
    assert any(o["whale_detections"] for o in log)
    assert res["frames"][0]["analyst_truth"]["label"] == ANALYST_TRUTH_LABEL

    def vandal(obs):
        obs["ships"][0]["original_route"].clear()
        obs["static"]["land"]["rings"].clear()
        obs["whale_detections"].clear()
        return F.no_action(obs)
    res2, _ = run(vandal)
    assert strip_wall(res2)["summary"] == strip_wall(res)["summary"]


def test_reports_are_due_stable_capped_and_aged():
    log = []
    over = {"sensors": {"max_reports": 3, "observers": [{"sensor_id": "lookout", "kind": "SHIP_LOOKOUT",
            "range_m": 4000, "p_detect": 1.0, "position_error_m": 100, "delay_min_s": 120, "delay_max_s": 120}]}}
    res, _ = run(F.make_scripted_decider({}, log=log), cfg_over=over)
    seen = {}
    for obs in log:
        assert len(obs["whale_detections"]) <= 3
        for d in obs["whale_detections"]:
            assert d["reported_at"] <= obs["now"]
            assert seen.setdefault(d["detection_id"], d) == d          # redisplay never changes a report
    c = res["manifest"]["counters"]
    assert c["reports_generated"] > 3 and c["dropped_cap"] > 0
    assert all(int(d["observed_at"][14:16]) * 60 + int(d["observed_at"][17:19]) + 120
               == int(d["reported_at"][14:16]) * 60 + int(d["reported_at"][17:19]) for d in seen.values())


def test_seed_reproducible_and_policy_change_keeps_area_sensor_draws():
    area = {"sensors": {"observers": [{"sensor_id": "survey", "kind": "AREA", "p_detect": 0.5,
                                       "position_error_m": 200, "delay_min_s": 0, "delay_max_s": 90}]}}
    a, _ = run(cfg_over=area)
    b, _ = run(cfg_over=area)
    assert strip_wall(a) == strip_wall(b)
    c, _ = run(F.make_scripted_decider({60: F.detour_at()}), cfg_over=area)

    def reports(r):
        return {d["detection_id"]: d for f in r["frames"] for d in f["whale_detections"]}
    ra, rc = reports(a), reports(c)
    common = set(ra) & set(rc)
    assert common and all(ra[k] == rc[k] for k in common)
    other = F.fixture_scenario(seed=8)
    d, _ = run(scenario=other, cfg_over=area)
    assert set(reports(d)).isdisjoint(ra)                                # seed changes report draws
    assert d["summary"]["modeled_contacts"] == a["summary"]["modeled_contacts"]


# --- Completion check: route-cache run offline; file/input changes after init have no effect --------
def test_cached_bundle_changes_after_init_cannot_affect_world(tmp_path):
    cfg, land, _ = setup()
    path = tmp_path / "route_bundle.json"
    path.write_text((COMMON / "route_bundle.fixture.json").read_text())
    bundle = routes.load_route_bundle(path, land, cfg)
    inputs = F.make_inputs(bundle, land, F.fixture_scenario(), cfg)
    clean = run_scenario(copy.deepcopy(inputs), F.no_action, F.make_fixture_validator(cfg, geo), F.zero_weather,
                         project=geo.project)

    def tamper(frame):
        inputs["route_bundle"]["routes"][0]["waypoints"][1]["lat"] = 34.5
        inputs["scenario"]["whales"].clear()
        path.write_text("{}")
    dirty = run_scenario(inputs, F.no_action, F.make_fixture_validator(cfg, geo), F.zero_weather,
                         project=geo.project, on_frame=tamper)
    assert strip_wall(dirty) == strip_wall(clean)


# --- Input readiness, weather, bounds --------------------------------------------------------------
def test_real_empty_bundle_and_missing_config_are_structured_errors():
    cfg, land, bundle = setup()
    empty = dict(bundle, routes=[], issues=[C.AIS_ROUTE_UNAVAILABLE])
    res = run_scenario(F.make_inputs(empty, land, F.fixture_scenario(), cfg), F.no_action,
                       F.make_fixture_validator(cfg, geo), F.zero_weather, project=geo.project)
    assert res["summary"]["status"] == C.RUN_ERROR and C.AIS_ROUTE_UNAVAILABLE in res["manifest"]["status_reasons"]
    assert set(res) == set(C.RunResult.__annotations__) and set(res["summary"]) == set(C.RunSummary.__annotations__)
    no_sensors = {k: v for k, v in cfg.items() if k != "sensors"}
    res2 = run_scenario(F.make_inputs(bundle, land, F.fixture_scenario(), no_sensors), F.no_action,
                        F.make_fixture_validator(cfg, geo), F.zero_weather, project=geo.project)
    assert res2["summary"]["status"] == C.RUN_ERROR and "CONFIG_MISSING:sensors" in res2["manifest"]["status_reasons"]


def test_unknown_weather_marks_run_incomplete_not_zero():
    scen = F.fixture_scenario()
    scen["weather"]["coverage_end_s"] = 300
    res, _ = run(scenario=scen)
    assert res["summary"]["weather_exposure_s"] is None and res["summary"]["status"] == C.RUN_INCOMPLETE
    assert C.UNKNOWN_WEATHER in res["manifest"]["status_reasons"]


def test_storm_exposure_uses_actual_timed_legs():
    scen = F.fixture_scenario(weather=F.load_json(COMMON / "weather_storm_south.fixture.json"))
    wx = F.make_fixture_weather_exposure(geo)
    base, _ = run(scenario=scen, weather=wx)
    detour, _ = run(F.make_scripted_decider({60: F.detour_at()}), scenario=scen, weather=wx)
    assert base["summary"]["weather_exposure_s"] == 0.0
    # Inside the 0.8-severity zone: ~286 m of the approach leg, the 1287 m bypass leg, ~346 m of the
    # departure leg, all at 18 kn.
    assert detour["summary"]["weather_exposure_s"] == pytest.approx(0.8 * (286 + 1287 + 346) / SPEED, rel=0.01)


def test_callback_error_wall_limit_and_drain_limit():
    def boom(obs):
        raise RuntimeError("planner crashed")
    res, _ = run(boom)
    assert res["summary"]["status"] == C.RUN_ERROR and "CALLBACK_ERROR" in types(res)
    res2, _ = run(cfg_over={"simulation": {"wall_limit_s": 0}})
    assert res2["summary"]["status"] == C.RUN_INCOMPLETE and "WALL_LIMIT" in res2["manifest"]["status_reasons"]
    res3, _ = run(scenario=F.fixture_scenario(duration_s=300), cfg_over={"simulation": {"drain_max_s": 60}})
    s = res3["summary"]
    assert s["status"] == C.RUN_INCOMPLETE and s["simulated_duration_s"] == 360.0 and s["added_delay_s"] is None
    assert {"DRAIN_LIMIT", "VOYAGES_UNFINISHED"} <= set(res3["manifest"]["status_reasons"])


def test_reused_route_voyages_get_distinct_simulated_ids():
    scen = F.fixture_scenario(voyages=[{"voyage_id": f"v{i}", "route_id": "fixture-route-001", "entry_s": 300 * i}
                                       for i in range(3)])
    res, _ = run(scenario=scen)
    ids = [v["ship_id"] for v in res["manifest"]["voyages"]]
    assert ids == ["sim:999000001:v0", "sim:999000001:v1", "sim:999000001:v2"]
    assert res["manifest"]["route_bundle"]["routes_used"]["fixture-route-001"]["reused"] is True
    assert res["summary"]["voyages_completed"] == 3 and res["summary"]["modeled_contacts"] == 3


def test_background_whales_are_seeded_and_stay_in_area():
    area = {"lat_min": 34.1, "lat_max": 34.3, "lon_min": -120.2, "lon_max": -119.9}
    a = make_background_whales(5, 1, area)
    assert a == make_background_whales(5, 1, area) and a != make_background_whales(5, 2, area)
    fr = Frame(geo.project)
    for spec in a:
        w = Whale(spec, 1, 9000, fr, {"whale_speed_range_mps": [1.5, 2.0]})
        for t in range(0, 9000, 60):
            p = fr.point(*w.state_at(t)[:2])
            assert area["lat_min"] - 1e-6 <= p["lat"] <= area["lat_max"] + 1e-6
            assert area["lon_min"] - 1e-6 <= p["lon"] <= area["lon_max"] + 1e-6


def test_default_world_runtime_is_measured():
    """Five voyages, twenty whales, two virtual hours: target under five seconds (measured)."""
    area = {"lat_min": 34.15, "lat_max": 34.25, "lon_min": -120.12, "lon_max": -120.0}
    scen = F.fixture_scenario(duration_s=7200, whales=make_background_whales(20, 0, area),
                              voyages=[{"voyage_id": f"v{i}", "route_id": "fixture-route-001", "entry_s": 1200 * i}
                                       for i in range(5)])
    t0 = time.monotonic()
    res, _ = run(scenario=scen)
    wall = time.monotonic() - t0
    assert res["summary"]["status"] == C.RUN_COMPLETE and res["summary"]["voyages_completed"] == 5
    assert wall < 5.0, wall


def test_invalid_weather_and_scenario_caps_are_input_errors():
    res, _ = run(scenario=F.fixture_scenario(weather=None))
    assert res["summary"]["status"] == C.RUN_ERROR and "INVALID_WEATHER" in res["manifest"]["status_reasons"]
    many = F.fixture_scenario(voyages=[{"voyage_id": f"v{i}", "route_id": "fixture-route-001", "entry_s": 0}
                                       for i in range(11)])
    res2, _ = run(scenario=many)
    assert "SCENARIO_CAP_EXCEEDED" in res2["manifest"]["status_reasons"]


# --- Integration with Person 1, traced through the real loaders and geometry ------------------------
def test_person1_integration_trace():
    import trace_p1
    rep = trace_p1.trace()
    failed = [name for name, ok in rep["checks"].items() if not ok]
    assert not failed, failed
    assert {f for c, f, _ in rep["crossings"] if c.startswith("save_the_whales.simulation")} == \
        {"save_the_whales.shared.geo.project"}


def test_resending_an_ignored_command_never_buys_a_fresh_compliance_draw():
    # Each resend is a new revision with a new incident id; all share the first incident's draw.
    over = {"response": {"compliance": 0.5}}
    script = {t: F.detour_at() for t in (60, 120, 180, 240)}
    outcomes = set()
    for seed in range(8):
        res, _ = run(F.make_scripted_decider(script), scenario=F.fixture_scenario(seed=seed), cfg_over=over)
        kinds = [e["type"] for e in res["events"] if e["type"] in ("COMMAND_IGNORED", "COMMAND_APPLIED")]
        if kinds[0] == "COMMAND_IGNORED":
            assert set(kinds) == {"COMMAND_IGNORED"}
        outcomes.add(kinds[0])
    assert outcomes == {"COMMAND_IGNORED", "COMMAND_APPLIED"}      # both outcomes occur across seeds
