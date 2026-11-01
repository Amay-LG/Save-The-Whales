"""Person 4: config, scenario assembly, comparison formatting and the dashboard over injected fixture services.

Fixture-only: the preview services and canned results under tests/fixtures/ui are SYNTHETIC_FIXTURE data.
"""
from __future__ import annotations

import ast
import copy
import json
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_FIX = ROOT / "tests" / "fixtures" / "ui"
COMMON = ROOT / "tests" / "fixtures" / "common"
for p in (ROOT, UI_FIX):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import run_comparison as RC  # noqa: E402
import ui_fixtures as UF  # noqa: E402
from save_the_whales.shared import contracts as C  # noqa: E402
from save_the_whales.shared import geo  # noqa: E402
from save_the_whales.ui import plots as P  # noqa: E402
from save_the_whales.ui import scenarios as S  # noqa: E402

CFG = json.loads((ROOT / "config" / "default.json").read_text())


def bundle():
    return json.loads((COMMON / "route_bundle.fixture.json").read_text())


def land():
    return UF.fixture_land()


# --- config/default.json -------------------------------------------------------------------------
def test_default_config_carries_the_agreed_handshake_defaults_in_one_place():
    sim, plan, sen, resp, met = (CFG[k] for k in ("simulation", "planner", "sensors", "response", "metrics"))
    assert {k: sim[k] for k in ("dt_s", "decision_dt_s", "duration_s", "drain_max_s", "wall_limit_s")} == \
        {"dt_s": 30, "decision_dt_s": 60, "duration_s": 7200, "drain_max_s": 1800, "wall_limit_s": 30}
    assert (plan["max_geometries"], plan["max_speeds"], plan["max_candidates"], plan["max_detour_waypoints"]) == (4, 3, 12, 8)
    assert (plan["lookahead_s"], plan["max_rejoin_s"], plan["offsets_m"]) == (900, 3600, [500, 1000])
    assert (plan["clearance_margin_m"], plan["min_speed_knots"], plan["max_path_ratio"], plan["max_added_delay_s"]) == \
        (100, 6, 1.5, 900)
    assert [plan[k] for k in ("w_whale", "w_weather", "w_delay", "w_distance", "w_change")] == [5, 2, 1, 0.5, 0.1]
    assert [plan[k] for k in ("min_objective_improvement", "min_whale_improvement", "whale_risk_tolerance",
                              "cooldown_s")] == [0.01, 0.05, 0.01, 60]
    assert (plan["max_reports"], plan["max_detection_age_s"], plan["drift_mps"]) == (50, 1800, 1.5)
    assert (sen["max_reports"], sen["max_detection_age_s"]) == (50, 1800)
    assert (met["sigma_eval_m"], met["encounter_enter_m"], met["encounter_exit_m"], met["encounter_exit_dwell_s"]) == \
        (300, 500, 750, 120)
    assert set(resp) == {"response_lag_s", "compliance"}
    assert (CFG["ais"]["clearance_margin_m"], CFG["ais"]["min_speed_knots"]) == (plan["clearance_margin_m"],
                                                                                  plan["min_speed_knots"])
    assert (CFG["scenarios"]["default_voyages"], CFG["scenarios"]["default_whales"]) == (5, 20)


def test_planner_config_matches_the_advisor_defaults_so_nothing_is_scattered():
    from save_the_whales.advisor.constraints import PLANNER_DEFAULTS
    assert {k: CFG["planner"][k] for k in PLANNER_DEFAULTS} == PLANNER_DEFAULTS


# --- presets and build_run_inputs ----------------------------------------------------------------
def test_all_four_presets_load_and_build_scenario_shaped_inputs():
    presets = S.load_presets(CFG)
    assert set(presets) == set(S.PRESET_NAMES)
    for pid, settings in presets.items():
        inp = S.build_run_inputs(bundle(), land(), settings, CFG)
        assert set(inp) == {"run_id", "route_bundle", "land", "scenario", "config", "fixture_only"}
        sc = inp["scenario"]
        assert {"scenario_id", "seed", "start_at", "duration_s", "voyages", "whales", "weather"} <= set(sc)
        assert sc["scenario_id"] == pid and sc["start_at"].endswith("Z")
        assert all(set(v) == {"voyage_id", "route_id", "entry_s"} for v in sc["voyages"]) and sc["voyages"]
        w = sc["weather"]
        assert w["source"] == C.WEATHER_SIMULATED and w["coverage_start_s"] == 0
        assert w["coverage_end_s"] == sc["duration_s"] + CFG["simulation"]["drain_max_s"] + CFG["planner"]["max_rejoin_s"]
        assert inp["fixture_only"] is True
        json.dumps(inp)                                                    # JSON-compatible


def test_build_run_inputs_is_frozen_deterministic_and_reuses_routes_visibly():
    b = bundle()
    settings = dict(S.load_presets(CFG)["crossing"], voyages=3, voyage_spacing_s=300)
    a1, a2 = S.build_run_inputs(b, land(), settings, CFG), S.build_run_inputs(b, land(), settings, CFG)
    assert a1 == a2 and a1["run_id"].startswith("crossing-s0-")
    b["routes"][0]["waypoints"][0]["lat"] = 0.0                            # later source change
    assert a1["route_bundle"]["routes"][0]["waypoints"][0]["lat"] == 34.2
    assert [(v["route_id"], v["entry_s"]) for v in a1["scenario"]["voyages"]] == \
        [("fixture-route-001", 0.0), ("fixture-route-001", 300.0), ("fixture-route-001", 600.0)]
    a3 = S.build_run_inputs(bundle(), land(), dict(settings, seed=1), CFG)
    assert a3["run_id"] != a1["run_id"]


def test_designated_whale_and_storm_zones_are_placed_relative_to_the_route():
    route = bundle()["routes"][0]
    sc = S.build_run_inputs(bundle(), land(), S.load_presets(CFG)["crossing"], CFG)["scenario"]
    (whale,) = sc["whales"]
    assert whale["start"]["lat"] == pytest.approx(34.2, abs=1e-9)
    assert whale["start"]["lon"] == pytest.approx(-120.0625, abs=1e-6)        # half-way along the route
    assert whale["exposure"][0]["exposed"] is True and "detection_id" not in whale

    zone = S.build_run_inputs(bundle(), land(), S.load_presets(CFG)["storm_detour"], CFG)["scenario"]["weather"]["zones"][0]
    mid = geo.project({"lat": 34.2, "lon": -120.0625})
    from save_the_whales.shared.geo import point_in_ring
    assert point_in_ring(mid, [geo.project(p) for p in zone["polygon"]])         # ACROSS straddles the route
    assert (zone["severity"], zone["active_from_s"], zone["active_until_s"], zone["blocked"]) == (0.8, 0.0, 3600.0, False)
    right = S.zone_polygon(route, {"from_fraction": 0.2, "to_fraction": 0.8, "side": "RIGHT", "near_m": 100, "far_m": 900})
    assert all(p["lat"] < 34.2 for p in right)                                 # eastbound: right = south
    left = S.zone_polygon(route, {"from_fraction": 0.2, "to_fraction": 0.8, "side": "LEFT", "near_m": 100, "far_m": 900})
    assert all(p["lat"] > 34.2 for p in left)


def test_moving_designated_whale_heading_is_relative_to_travel_direction():
    s = dict(S.load_presets(CFG)["crossing"])
    s["designated_whale"] = dict(s["designated_whale"], speed_mps=0.5, heading_rel_deg=90)
    (w,) = S.build_run_inputs(bundle(), land(), s, CFG)["scenario"]["whales"]
    assert w["heading_deg"] == pytest.approx(0.0, abs=1e-6) or w["heading_deg"] == pytest.approx(360.0, abs=1e-6)


def test_background_whales_are_seeded_and_counted_toward_the_world():
    s = dict(S.load_presets(CFG)["crossing"], background_whales=19, voyages=5)
    a, b = (S.build_run_inputs(bundle(), land(), s, CFG)["scenario"]["whales"] for _ in range(2))
    assert len(a) == 20 and a == b
    assert all(w.get("random_walk") for w in a[1:])


@pytest.mark.parametrize("change,code", [
    ({"route_ids": ["no-such-route"]}, "UNKNOWN_ROUTE_ID:no-such-route"),
    ({"weather": {"preset": "STORM", "zones": [{"from_fraction": 0.1, "to_fraction": 0.2, "far_m": 100}] * 4}},
     S.TOO_MANY_STORM_ZONES),
    ({"weather": {"preset": "STORM", "zones": [{"from_fraction": 0.1, "to_fraction": 0.2, "far_m": 100,
                                                "severity": 1.5}]}}, "BAD_SETTING:zone.severity"),
    ({"weather": {"preset": "HURRICANE"}}, "BAD_SETTING:weather.preset"),
])
def test_unresolvable_settings_raise_scenario_error_with_codes(change, code):
    with pytest.raises(S.ScenarioError) as exc:
        S.build_run_inputs(bundle(), land(), dict(S.load_presets(CFG)["crossing"], **change), CFG)
    assert code in exc.value.codes


def test_no_usable_routes_still_builds_inputs_for_the_simulator_to_report():
    b = bundle()
    b.update(routes=[], issues=[C.AIS_ROUTE_UNAVAILABLE])
    inp = S.build_run_inputs(b, land(), S.load_presets(CFG)["crossing"], CFG)
    assert inp["scenario"]["voyages"] == [] and inp["scenario"]["whales"] == []


def test_readiness_gates_the_real_demo_on_genuine_ais_and_non_fixture_land():
    r = S.readiness(bundle(), land())
    assert r["can_run"] and not r["demo_ready"] and set(r["reasons"]) == {"ROUTES_NOT_GENUINE_AIS", "LAND_IS_FIXTURE"}
    r = S.readiness(bundle(), UF.fixture_land(valid=False))
    assert C.INVALID_MAP in r["reasons"] and not r["detours_allowed"]
    empty = dict(bundle(), routes=[], issues=[C.AIS_ROUTE_UNAVAILABLE])
    assert C.AIS_ROUTE_UNAVAILABLE in S.readiness(empty, land())["reasons"]
    # Shape-only check of the gate logic (a test dict, not real data).
    shaped = dict(bundle(), source={"kind": C.AISSTREAM_CAPTURE, "api_verified": True, "fixture_only": False,
                                    "captured_at": "2026-07-01T00:00:00Z", "capture_sha256": "x"})
    assert S.readiness(shaped, dict(land(), fixture_only=False))["demo_ready"]


# --- comparison ----------------------------------------------------------------------------------
def test_compare_runs_keeps_null_deltas_null_and_flags_incomplete():
    pair = UF.load("run_pair_incomplete.fixture.json")
    cmp = RC.compare_runs(pair["baseline"], pair["advisor"])
    assert cmp["status"] == C.RUN_INCOMPLETE and cmp["paired"]
    assert cmp["differences"]["added_delay_s"] is None and cmp["differences"]["extra_distance_m"] is None
    assert cmp["differences"]["modeled_contacts"] == pair["advisor"]["summary"]["modeled_contacts"] - \
        pair["baseline"]["summary"]["modeled_contacts"]
    assert cmp["voyages"][0]["ADVISOR"]["status"] == "FAILED" and cmp["voyages"][0]["ADVISOR"]["censored"]
    assert set(cmp["differences"]) == {k for k, _ in RC.METRICS}            # no invented confidence intervals
    assert not any("interval" in k or k.startswith("ci") for k in list(cmp) + list(cmp["rows"][0]))
    rows = P.comparison_rows(cmp)
    delay = next(r for r in rows if r["metric"] == "added_delay_s")
    assert delay["ADVISOR"] == "n/a (incomplete)" and delay["ADVISOR - NO_ACTION"] == "n/a (incomplete)"


def test_compare_runs_detects_unpaired_inputs():
    pair = UF.load("run_pair_complete.fixture.json")
    adv = copy.deepcopy(pair["advisor"])
    adv["manifest"]["seed"] = 99
    adv["manifest"]["weather"] = {"source": "SIMULATED", "zones": ["changed"]}
    cmp = RC.compare_runs(pair["baseline"], adv)
    assert not cmp["paired"] and {"seed", "weather"} <= set(cmp["pairing_mismatches"])


def test_compare_runs_reports_rejected_candidates_by_factor():
    pair = UF.load("run_pair_incomplete.fixture.json")
    cmp = RC.compare_runs(pair["baseline"], pair["advisor"])
    assert set(cmp["rejected_candidates_by_factor"]) == {"LAND_CROSSING", "WEATHER_BLOCKED"}
    assert "NOT_SELECTED" not in RC.compare_runs(*(UF.load("run_pair_complete.fixture.json")[k]
                                                   for k in ("baseline", "advisor")))["rejected_candidates_by_factor"]


def test_save_run_writes_a_unique_directory(tmp_path):
    pair = UF.canned_pair("complete")
    d1, d2 = RC.save_run(pair, tmp_path), RC.save_run(pair, tmp_path, frames=False)
    assert d1 != d2 and sorted(p.name for p in d1.iterdir()) == \
        ["comparison.json", "inputs.json", "result_advisor.json", "result_no_action.json"]
    assert json.loads((d2 / "result_advisor.json").read_text())["frames"] == []


# --- map layers and tables -----------------------------------------------------------------------
def test_map_layers_keep_source_simulated_reported_and_truth_layers_distinct():
    pair = UF.canned_pair("complete")
    inp = pair["inputs"]
    layers = P.map_layers(inp["route_bundle"], inp["land"], inp["scenario"]["weather"], pair["advisor"], -1)
    ids = [l["id"] for l in layers]
    assert ids == ["land", "storms", "baseline_fixture", "tracks", "current_paths", "ships", "reports"]
    assert "truth" not in ids                                              # overlay off by default
    assert len({tuple(l["color"]) for l in layers}) == len(layers) and len({l["label"] for l in layers}) == len(layers)
    labels = {l["id"]: l["label"] for l in layers}
    assert "SYNTHETIC FIXTURE" in labels["baseline_fixture"] and "not a real vessel" in labels["ships"]
    assert labels["reports"].startswith("Simulated whale sighting")
    with_truth = P.map_layers(inp["route_bundle"], inp["land"], inp["scenario"]["weather"], pair["advisor"], -1, True)
    truth = next(l for l in with_truth if l["id"] == "truth")
    assert "never advisor input" in truth["label"] and truth["data"]
    reports = next(l for l in layers if l["id"] == "reports")["data"]
    assert {d["name"] for d in reports}.isdisjoint({d["name"] for d in truth["data"]})


def test_genuine_ais_baseline_uses_the_recorded_style():
    b = dict(bundle(), source={"kind": C.AISSTREAM_CAPTURE, "api_verified": True, "fixture_only": False})
    layers = P.map_layers(b, land(), None)
    assert [l["id"] for l in layers] == ["land", "storms", "baseline"]
    assert layers[2]["label"].startswith("Recorded AIS baseline")


def test_alert_rows_show_action_rejoin_cpa_costs_and_evidence_age():
    (row,) = P.alert_rows(UF.canned_pair("complete")["advisor"])
    assert row["command"] == C.DETOUR and row["rejoin_route_index"] != "n/a"
    assert float(row["cpa_m"]) > 0 and float(row["evidence_age_s"]) >= 0
    assert len(row["J parts (whale/weather/delay/distance/change)"].split(" / ")) == 5
    assert P.alert_rows(UF.canned_pair("incomplete")["advisor"]) == []


def test_flagged_codes_and_explanations_cover_the_failure_states():
    rows = P.assessment_rows(UF.canned_pair("incomplete")["advisor"])
    assert P.flagged_codes(rows) == [C.NO_FEASIBLE_ROUTE]
    for code in (C.AIS_KEY_MISSING, C.AIS_ROUTE_UNAVAILABLE, C.INVALID_MAP, C.NO_FEASIBLE_ROUTE,
                 C.INSUFFICIENT_FRESH_EVIDENCE, "VOYAGE_FAILED:NO_FEASIBLE_ROUTE"):
        assert P.explain(code) != code
    assert "not proof" in P.explain(C.INSUFFICIENT_FRESH_EVIDENCE)


def test_ui_presentation_modules_import_no_simulator_advisor_or_sources():
    for name in ("dashboard.py", "plots.py"):
        tree = ast.parse((ROOT / "save_the_whales" / "ui" / name).read_text())
        mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | \
               {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not {m for m in mods if m.startswith(("save_the_whales.simulation", "save_the_whales.advisor",
                                                     "save_the_whales.sources"))}, name


# --- dashboard over injected fixture services (Streamlit AppTest) ----------------------------------
def _app(root: str, case: str):
    import sys
    for p in (root, root + "/tests/fixtures/ui"):
        if p not in sys.path:
            sys.path.insert(0, p)
    import streamlit as st
    from ui_fixtures import preview_services
    from save_the_whales.ui.dashboard import render_dashboard
    if "services" not in st.session_state:
        st.session_state["services"] = preview_services(case)
    render_dashboard(st.session_state["services"])


def _run_app(case: str, submit: bool = True):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_function(_app, args=(str(ROOT), case), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    if submit:
        next(b for b in at.button if b.label == "Run NO_ACTION vs ADVISOR").click().run()
        assert not at.exception, [e.value for e in at.exception]
    return at


def _texts(elements):
    return [e.value for e in elements]


def test_dashboard_preview_runs_once_and_redraws_without_recomputing():
    at = _run_app("complete")
    assert at.title[0].value == "AIS-Seeded Whale Simulation"
    assert any("TEST_PREVIEW (fixture_only)" in w for w in _texts(at.warning))
    assert {"NO_ACTION: COMPLETE", "ADVISOR: COMPLETE"} <= set(_texts(at.success))
    calls = at.session_state["services"]["calls"]
    assert calls["run_pair"] == 1 and calls["build_run_inputs"] == 1
    at.slider(key="frame").set_value(0).run()                              # UI refresh only
    at.checkbox(key="truth").check().run()
    assert not at.exception and calls["run_pair"] == 1
    table = at.dataframe[1].value
    assert list(table.columns) == ["metric", "unit", "NO_ACTION", "ADVISOR", "ADVISOR - NO_ACTION"]


def test_dashboard_renders_incomplete_and_no_feasible_states():
    at = _run_app("incomplete")
    warnings = _texts(at.warning)
    assert any(w.startswith("ADVISOR: INCOMPLETE") for w in warnings)
    assert any(w.startswith("NO_FEASIBLE_ROUTE:") for w in warnings)
    table = next(d.value for d in at.dataframe if "metric" in d.value.columns)
    assert "n/a (incomplete)" in set(table.loc[table["metric"] == "added_delay_s", "ADVISOR"])


@pytest.mark.parametrize("case,where,needle", [
    ("invalid_map", "error", "INVALID_MAP:"),
    ("no_routes", "error", "AIS_ROUTE_UNAVAILABLE:"),
    ("no_routes", "error", "ADVISOR: ERROR"),
    ("error", "error", "ADVISOR: ERROR (CALLBACK_ERROR:decide)"),
    ("no_sightings", "warning", "INSUFFICIENT_FRESH_EVIDENCE:"),
])
def test_dashboard_renders_failure_states_explicitly(case, where, needle):
    at = _run_app(case)
    assert any(t.startswith(needle) for t in _texts(getattr(at, where))), _texts(getattr(at, where))


def test_capture_failure_is_shown_and_one_handle_survives_reruns():
    at = _run_app("key_missing", submit=False)
    next(b for b in at.button if b.label == "Start capture").click().run()
    assert any(w.startswith("AIS_KEY_MISSING:") for w in _texts(at.warning))
    state = {r["field"]: r["value"] for r in at.table[0].value.to_dict("records")}
    assert state["state"] == C.CAPTURE_ERROR and state["api_verified"] == "False"
    at.run()                                                               # rerun keeps the same handle
    assert at.session_state["services"]["calls"]["start_capture"] == 1
