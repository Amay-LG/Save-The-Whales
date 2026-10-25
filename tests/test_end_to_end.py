"""Person 4 end-to-end tests: the real modules of all four owners, wired once through ``app.production_services``.

Routes are the SYNTHETIC_FIXTURE recipe bundle (no usable genuine AIS route exists yet) over the real
Natural Earth research land map unless a case needs the toy obstacle. These tests prove integration,
not real-data provenance (Tasks.md §5 step 2).
"""
from __future__ import annotations

import ast
import copy
import json
import math
import socket
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_FIX = ROOT / "tests" / "fixtures" / "ui"
COMMON = ROOT / "tests" / "fixtures" / "common"
for p in (ROOT, UI_FIX):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import run_comparison as RC  # noqa: E402
import trace_integration as T  # noqa: E402
from app import load_config, production_services  # noqa: E402
from save_the_whales.shared import contracts as C  # noqa: E402
from save_the_whales.shared import geo  # noqa: E402

REAL_LAND = ROOT / "data" / "land" / "santa_barbara.geojson"
FIXTURE_BUNDLE = COMMON / "route_bundle.fixture.json"
ROUTE_LAT = 34.2


def config(lag=0.0, compliance=1.0, **sim):
    cfg = load_config()
    cfg["response"] = {"response_lag_s": lag, "compliance": compliance}   # deterministic acceptance values
    cfg["simulation"].update(sim)
    return cfg


def run(preset="crossing", cfg=None, land_path=REAL_LAND, **changes):
    cfg = cfg or config()
    s = production_services(cfg)
    settings = dict(s["load_presets"](cfg)[preset], **changes)
    land = s["load_land"](s["land_override_path"](settings) or land_path, cfg)
    bundle = s["load_route_bundle"](FIXTURE_BUNDLE, land, cfg)
    return s["run_pair"](s["build_run_inputs"](bundle, land, settings, cfg))


def positions(result):
    """{ship_id: [(t, lat, lon, sog), ...]} from the frames."""
    out = {}
    for f in result["frames"]:
        for sh in f["ships"]:
            out.setdefault(sh["ship_id"], []).append((f["sim_time_s"], sh["lat"], sh["lon"], sh["sog_knots"]))
    return out


def lateral(result):
    lats = [p[1] - ROUTE_LAT for track in positions(result).values() for p in track]
    return max(lats), min(lats)


def events(result, etype):
    return [e for e in result["events"] if e["type"] == etype]


def without_wall(summary):
    return {k: v for k, v in summary.items() if k != "elapsed_wall_s"}


def zone(**kw):
    z = {"zone_id": "z", "kind": "ROUTE_RELATIVE", "from_fraction": 0.4, "to_fraction": 0.6, "side": "ACROSS",
         "near_m": 0, "far_m": 300, "severity": 1.0, "active_from_s": 0, "active_until_s": None, "blocked": False}
    z.update(kw)
    return {"preset": "STORM", "zones": [z]}


# --- interconnection (Architecture §5) -----------------------------------------------------------
def test_integration_trace_follows_the_architecture_arrows():
    rep = T.run_trace()
    failed = [f"{n}: {d}" for n, ok, d in rep["checks"] if not ok]
    assert rep["ok"], failed + rep["problems"]
    owners = {(a, b) for a, b, _ in rep["edges"]}
    assert {("harness", "simulation"), ("simulation", "advisor"), ("advisor", "shared"), ("sources", "shared"),
            ("ui", "shared")} <= owners
    assert not {(a, b) for a, b in owners if a == "advisor" and b in ("simulation", "sources", "ui", "harness")}
    assert not {(a, b) for a, b in owners if a == "sources" and b != "shared"}


def test_trace_detects_a_forbidden_owner_crossing():
    from save_the_whales.advisor import planner
    leaky = types.FunctionType(compile("def leak():\n    from save_the_whales.simulation.world import parse_utc\n"
                                       "    return parse_utc('2026-07-01T00:00:00Z')\n", "<leak>", "exec").co_consts[0],
                               vars(planner))                      # pretend the advisor reads simulator code
    tracer = T.Tracer()
    sys.setprofile(tracer)
    try:
        leaky()
    finally:
        sys.setprofile(None)
    assert any(v.startswith("advisor -> save_the_whales.simulation.world.parse_utc") for v in T.forbidden_edges(tracer.edges))


def _imports(path: Path):
    tree = ast.parse(path.read_text())
    return {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | \
           {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}


@pytest.mark.parametrize("pkg,banned", [
    ("shared", ("save_the_whales.sources", "save_the_whales.simulation", "save_the_whales.advisor", "save_the_whales.ui")),
    ("sources", ("save_the_whales.simulation", "save_the_whales.advisor", "save_the_whales.ui")),
    ("simulation", ("save_the_whales.sources", "save_the_whales.advisor", "save_the_whales.ui", "streamlit")),
    ("advisor", ("save_the_whales.sources", "save_the_whales.simulation", "save_the_whales.ui", "save_the_whales.shared.geo",
                 "socket", "websockets", "urllib", "http", "requests")),
    ("ui", ("save_the_whales.sources", "save_the_whales.advisor")),
])
def test_static_imports_respect_owner_boundaries(pkg, banned):
    for path in (ROOT / "save_the_whales" / pkg).glob("*.py"):
        bad = {m for m in _imports(path) if m.startswith(banned)}
        assert not bad, f"{path.name}: {sorted(bad)}"


def test_production_services_bind_the_real_module_boundaries():
    from save_the_whales.advisor import constraints, planner
    from save_the_whales.simulation import engine
    from save_the_whales.sources import ais, land, routes
    from save_the_whales.ui import scenarios
    s = production_services(load_config())
    assert s["mode_label"] == "PRODUCTION" and not s["preview"] and s["geometry"] is geo
    assert (s["start_capture"], s["prepare_routes"], s["load_route_bundle"], s["load_land"]) == \
        (ais.start_capture, routes.prepare_routes, routes.load_route_bundle, land.load_land)
    assert (s["make_decider"], s["make_validator"], s["run_scenario"]) == \
        (planner.make_decider, constraints.make_validator, engine.run_scenario)
    assert s["weather_exposure"].func.__module__ == "save_the_whales.advisor.weather"
    assert s["weather_exposure"].keywords == {"geometry": geo}
    assert s["build_run_inputs"] is scenarios.build_run_inputs and s["compare_runs"] is RC.compare_runs


# --- Tasks.md §5 required integrated acceptance --------------------------------------------------
def test_whale_crossing_gives_an_accepted_route_change_with_forward_rejoin_on_real_land():
    pair = run("crossing")
    adv, base = pair["advisor"], pair["baseline"]
    assert pair["comparison"]["status"] == C.RUN_COMPLETE and pair["comparison"]["paired"]
    assert adv["manifest"]["land"]["validated"] and not adv["manifest"]["land"]["fixture_only"]
    assert adv["manifest"]["fixture_only"]                                   # fixture route: not acceptance evidence
    applied = events(adv, "COMMAND_APPLIED")
    assert applied and applied[0]["details"]["command"] == C.DETOUR          # a route change, not slowdown-only
    assert lateral(adv)[0] * 111_000 > 400                                    # really sailed the detour
    assert events(adv, "REJOINED") and adv["summary"]["voyages_completed"] == 1
    route = json.loads(FIXTURE_BUNDLE.read_text())["routes"][0]["waypoints"]
    assert all(s["original_route"] == route for f in adv["frames"] for s in f["ships"])
    progress = [s["baseline_progress_index"] for f in adv["frames"] for s in f["ships"]]
    assert progress == sorted(progress)
    assert base["summary"]["modeled_contacts"] == 1 and adv["summary"]["modeled_contacts"] == 0
    assert adv["summary"]["added_delay_s"] > 0 and adv["summary"]["extra_distance_m"] > 0
    land = production_services(config())["load_land"](REAL_LAND, config())
    hull = geo.ship_clearance_m(300, 40, 100)
    track = [{"lat": p[1], "lon": p[2]} for p in next(iter(positions(adv).values()))]
    assert geo.check_water_path(track, land, hull)["ok"]


def test_land_blocked_side_rejects_the_land_dogleg_and_passes_on_the_open_side():
    pair = run("land_blocked_side")
    adv = pair["advisor"]
    assert adv["manifest"]["land"]["fixture_only"] and pair["comparison"]["fixture_only"]
    north, south = lateral(adv)
    assert north <= 1e-9 and -south * 111_000 > 400                           # open (south) side only
    land_codes = {C.POINT_ON_LAND, C.LAND_CROSSING, C.LAND_CONTACT, C.INSUFFICIENT_CLEARANCE}
    rejected = [c for d in adv["decisions"] for a in d["assessments"] for c in a["rejected_candidates"]
                if land_codes & set(c["reasons"])]
    assert rejected and all(c["kind"] in ("DOGLEG_LEFT", "DOGLEG_WIDE") for c in rejected)
    assert adv["summary"]["modeled_contacts"] == 0 and adv["summary"]["rejected_actions"] == 0
    toy = production_services(config())["load_land"](COMMON / "land_blocked_side.fixture.geojson", config())
    track = [{"lat": p[1], "lon": p[2]} for p in next(iter(positions(adv).values()))]
    assert geo.check_water_path(track, toy, geo.ship_clearance_m(300, 40, 100))["ok"]


def test_otherwise_equal_alternatives_prefer_the_clear_side():
    clear = run("crossing")["advisor"]
    went_left = lateral(clear)[0] > -lateral(clear)[1]
    storm_side = "LEFT" if went_left else "RIGHT"
    stormy = run("crossing", weather=zone(side=storm_side, from_fraction=0.2, to_fraction=0.8, near_m=100,
                                          far_m=2000, severity=0.8))["advisor"]
    north, south = lateral(stormy)
    assert (south * 111_000 < -400 and north <= 1e-9) if went_left else (north * 111_000 > 400 and south >= -1e-9)
    assert stormy["summary"]["weather_exposure_s"] == 0.0 and stormy["summary"]["modeled_contacts"] == 0


def test_blocked_weather_is_enforced_at_actual_arrival_times_and_violations_stay_visible():
    now = run("storm_detour", weather=zone(zone_id="now", blocked=True))
    viol = now["comparison"]["hard_constraint_violations"]
    assert [h["zone_id"] for h in viol["NO_ACTION"]] == ["now"] and viol["ADVISOR"] == []
    assert now["advisor"]["summary"]["route_changes"] >= 1 and now["advisor"]["summary"]["weather_exposure_s"] == 0.0
    late = run("storm_detour", weather=zone(zone_id="late", blocked=True, active_from_s=2000))
    assert late["comparison"]["hard_constraint_violations"] == {"NO_ACTION": [], "ADVISOR": []}
    assert late["advisor"]["summary"]["route_changes"] == 0                  # zone not active when the ship passes


def test_storm_detour_preset_lowers_weather_exposure():
    pair = run("storm_detour")
    b, a = pair["baseline"]["summary"], pair["advisor"]["summary"]
    assert b["weather_exposure_s"] > 0 and a["weather_exposure_s"] < b["weather_exposure_s"]
    assert a["route_changes"] >= 1 and pair["comparison"]["differences"]["weather_exposure_s"] < 0


def test_no_feasible_route_fails_the_voyage_visibly_and_keeps_deltas_null():
    pair = run("storm_detour", weather=zone(zone_id="wall", blocked=True, from_fraction=0.3, to_fraction=0.7,
                                            far_m=2500))
    adv, cmp = pair["advisor"], pair["comparison"]
    assert adv["summary"]["status"] == C.RUN_INCOMPLETE and adv["summary"]["voyages_failed"] == 1
    assert f"VOYAGE_FAILED:{C.NO_FEASIBLE_ROUTE}" in adv["manifest"]["status_reasons"]
    assert any(a["status"] == C.NO_FEASIBLE_ROUTE for d in adv["decisions"] for a in d["assessments"])
    assert cmp["status"] == C.RUN_INCOMPLETE and cmp["differences"]["added_delay_s"] is None
    assert cmp["hard_constraint_violations"]["NO_ACTION"] and not cmp["hard_constraint_violations"]["ADVISOR"]
    assert all(d["actions"] == [] or all(a["command"] != "HOLD" for a in d["actions"]) for d in adv["decisions"])


def test_policies_share_routes_whales_weather_schedules_and_sensor_draws():
    pair = run("crossing", cfg=config(lag=30), voyages=5, background_whales=19)
    base, adv = pair["baseline"], pair["advisor"]
    assert pair["comparison"]["paired"]
    first_cmd = min(e["sim_time_s"] for e in events(adv, "COMMAND_APPLIED"))
    fbs, fas = ({f["sim_time_s"]: f for f in r["frames"]} for r in (base, adv))
    common = sorted(set(fbs) & set(fas))                                     # runs end at their own times
    assert len(common) >= len(base["frames"]) - 1
    for t in common:
        fb, fa = fbs[t], fas[t]
        assert fb["analyst_truth"] == fa["analyst_truth"]                    # whales never react to ships
        if fa["sim_time_s"] < first_cmd:
            assert fb["whale_detections"] == fa["whale_detections"]          # keyed sensor noise is shared
    spawn = lambda r: [(e["sim_time_s"], e["ship_id"]) for e in events(r, "VOYAGE_SPAWNED")]
    assert spawn(base) == spawn(adv) and len(spawn(adv)) == 5


def test_ignored_commands_stay_on_baseline_without_extra_compliance_from_resends():
    pair = run("crossing", cfg=config(compliance=0.0))
    adv, base = pair["advisor"], pair["baseline"]
    assert events(adv, "COMMAND_IGNORED") and adv["summary"]["route_changes"] == 0
    assert lateral(adv) == (0.0, 0.0) and adv["summary"]["modeled_contacts"] == base["summary"]["modeled_contacts"]
    assert adv["summary"]["alerts"] >= 1 and not events(adv, "COMMAND_APPLIED")


def test_response_lag_applies_later_without_stale_rejections():
    adv = run("crossing", cfg=config(lag=45))["advisor"]
    accepted, applied = events(adv, "ACTION_ACCEPTED"), events(adv, "COMMAND_APPLIED")
    assert applied and adv["summary"]["rejected_actions"] == 0
    assert applied[0]["sim_time_s"] == pytest.approx(accepted[0]["sim_time_s"] + 45)
    assert adv["summary"]["modeled_contacts"] == 0


def test_motion_is_continuous_with_finite_leases_in_the_default_world():
    pair = run("crossing", cfg=config(lag=30), voyages=5, background_whales=19)
    adv = pair["advisor"]
    assert adv["summary"]["status"] == C.RUN_COMPLETE and adv["summary"]["voyages_completed"] == 5
    for sid, track in positions(adv).items():
        for (t0, la0, lo0, _), (t1, la1, lo1, _) in zip(track, track[1:]):
            step = geo.distance_m({"lat": la0, "lon": lo0}, {"lat": la1, "lon": lo1})
            assert step <= geo.knots_to_mps(18.0) * (t1 - t0) + 1.0, f"{sid} jumped {step:.0f} m at {t1}"
    for f in adv["frames"]:
        for s in f["ships"]:
            if s["sog_knots"] < s["service_speed_knots"] - 1e-6:             # slowed: only under a finite lease
                assert s["active_command"] and s["active_command"]["valid_until"].endswith("Z")
    assert all(v["status"] == "COMPLETE" for v in adv["manifest"]["voyages"])


def test_hidden_truth_recording_does_not_change_advisor_decisions():
    a = run("crossing", cfg=config(record_analyst_truth=True))["advisor"]
    b = run("crossing", cfg=config(record_analyst_truth=False))["advisor"]
    strip = lambda ds: json.loads(json.dumps(ds).replace(a["manifest"]["run_id"], "RUN").replace(b["manifest"]["run_id"], "RUN"))
    assert "analyst_truth" in a["frames"][0] and "analyst_truth" not in b["frames"][0]
    assert without_wall(a["summary"]) == without_wall(b["summary"])
    assert [d["actions"] for d in strip(a["decisions"])] == [d["actions"] for d in strip(b["decisions"])]


def test_frozen_cache_reproduces_the_comparison_offline(tmp_path, monkeypatch):
    first = run("crossing")
    run_dir = RC.save_run(first, tmp_path)
    inputs = json.loads((run_dir / "inputs.json").read_text())

    def refuse(*_a, **_k):
        raise OSError("offline test: network blocked")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    again = production_services(inputs["config"])["run_pair"](inputs)
    for key in ("baseline", "advisor"):
        assert without_wall(again[key]["summary"]) == without_wall(first[key]["summary"])
        assert again[key]["decisions"] == first[key]["decisions"] and again[key]["events"] == first[key]["events"]


def test_runtime_is_measured_and_the_wall_cap_returns_incomplete():
    cfg = config(lag=30)
    t0 = time.monotonic()
    pair = run("crossing", cfg=cfg, voyages=5, background_whales=19)
    pair_s = time.monotonic() - t0
    adv_s = pair["advisor"]["summary"]["elapsed_wall_s"]
    assert adv_s < 5.0, f"default advisor run took {adv_s:.2f} s"
    assert pair_s < 30.0, f"paired comparison took {pair_s:.2f} s"
    t0 = time.monotonic()
    capped = run("crossing", cfg=config(wall_limit_s=0.0), voyages=5, background_whales=19)["advisor"]
    assert capped["summary"]["status"] == C.RUN_INCOMPLETE and "WALL_LIMIT" in capped["manifest"]["status_reasons"]
    assert time.monotonic() - t0 < 5.0


def test_cli_runs_the_paired_comparison_and_reports_missing_real_routes(tmp_path, capsys):
    assert RC.main(["--bundle", str(FIXTURE_BUNDLE), "--scenario", "crossing", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "NOT DEMO-READY (ROUTES_NOT_GENUINE_AIS)" in out and "modeled_contacts" in out and "saved:" in out
    empty = tmp_path / "empty_bundle.json"
    empty.write_text(json.dumps(dict(json.loads(FIXTURE_BUNDLE.read_text()), routes=[],
                                     issues=[C.AIS_ROUTE_UNAVAILABLE])))
    assert RC.main(["--bundle", str(empty), "--scenario", "crossing", "--no-save"]) == 1
    assert "AIS_ROUTE_UNAVAILABLE" in capsys.readouterr().out


def test_real_data_gate_never_passes_without_genuine_routes():
    cfg = config()
    s = production_services(cfg)
    land = s["load_land"](REAL_LAND, cfg)
    assert land["validated"] and not land["fixture_only"] and land["rings"]
    assert "LAND_IS_FIXTURE" not in s["readiness"](s["load_route_bundle"](FIXTURE_BUNDLE, land, cfg), land)["reasons"]
    real = ROOT / "data" / "ais" / "route_bundle.json"
    if not real.is_file():
        pytest.skip("no local AIS route bundle (data/ais/ is git-ignored)")
    bundle = s["load_route_bundle"](real, land, cfg)
    ready = s["readiness"](bundle, land)
    has_routes = any(r["validation"]["ok"] for r in bundle["routes"])
    assert ready["demo_ready"] == (has_routes and bundle["source"]["kind"] == C.AISSTREAM_CAPTURE)
    if not has_routes:
        assert C.AIS_ROUTE_UNAVAILABLE in ready["reasons"]


def _prod_app(root: str):
    import sys
    if root not in sys.path:
        sys.path.insert(0, root)
    import streamlit as st
    from app import production_services
    from save_the_whales.ui.dashboard import render_dashboard
    if "services" not in st.session_state:
        cfg = production_services()["config"]
        cfg["response"] = {"response_lag_s": 0, "compliance": 1.0}
        s = production_services(cfg)
        s["paths"] = dict(s["paths"], bundle=root + "/tests/fixtures/common/route_bundle.fixture.json")
        st.session_state["services"] = s
    render_dashboard(st.session_state["services"])


def test_dashboard_drives_the_real_pipeline_through_injected_production_services():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_function(_prod_app, args=(str(ROOT),), default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    next(b for b in at.button if b.label == "Run NO_ACTION vs ADVISOR").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert {"NO_ACTION: COMPLETE", "ADVISOR: COMPLETE"} <= {s.value for s in at.success}
    assert any(w.value.startswith("ROUTES_NOT_GENUINE_AIS") for w in at.warning)
    alerts = next(d.value for d in at.dataframe if "command" in d.value.columns)
    assert C.DETOUR in set(alerts["command"])
