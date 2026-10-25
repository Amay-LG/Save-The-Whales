"""Integration trace: Person 1 artifacts/geometry -> Person 2 simulator, through the real boundaries.

Runs ``run_scenario`` on inputs produced by Person 1's own loaders (``load_land``, ``load_route_bundle``,
``prepare_routes``) and wires Person 1's ``shared.geo`` into the injected ``project``, the TEST-ONLY
fixture validator and the fixture weather integral. While it runs, a ``sys.setprofile`` tracer records
every call that crosses into ``shared``/``sources`` (caller module -> callee function) and any call from
``simulation`` into forbidden areas (sources, advisor, ui, network, file I/O). Wrappers check every
payload at the boundary against the shared TypedDicts, and the simulated motion is re-checked with
Person 1's geometry (units, projection, continuity, land clearance).

    .venv/bin/python tests/fixtures/simulation/trace_p1.py     # prints the report; exit 1 on failure
"""
from __future__ import annotations

import ast
import copy
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for p in (str(ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import sim_fixtures as F  # noqa: E402
from save_the_whales.shared import contracts as C  # noqa: E402
from save_the_whales.shared import geo  # noqa: E402
from save_the_whales.simulation.engine import run_scenario  # noqa: E402
from save_the_whales.simulation.world import canonical_sha256  # noqa: E402
from save_the_whales.sources import ais, land as landmod, routes  # noqa: E402

SIM_PKG = "save_the_whales.simulation"
P1_PKGS = ("save_the_whales.shared", "save_the_whales.sources")
FORBIDDEN_PKGS = ("save_the_whales.sources", "save_the_whales.advisor", "save_the_whales.ui", "streamlit",
                  "websockets", "socket", "urllib", "http", "requests")
FORBIDDEN_C = {"open", "connect", "create_connection", "urlopen"}
REAL_LAND = ROOT / "data" / "land" / "santa_barbara.geojson"
REAL_CAPTURE = ROOT / "data" / "ais" / "capture_20261024.jsonl"


# --- tracing -------------------------------------------------------------------------------------
class CallTracer:
    def __init__(self):
        self.crossings: Counter = Counter()     # (caller module, callee) -> count
        self.forbidden: Counter = Counter()

    def __call__(self, frame, event, arg):
        if event == "call":
            callee_mod = frame.f_globals.get("__name__", "")
            back = frame.f_back
            caller_mod = back.f_globals.get("__name__", "") if back is not None else ""
            if callee_mod.startswith(P1_PKGS) and not caller_mod.startswith(P1_PKGS):
                self.crossings[(caller_mod, f"{callee_mod}.{frame.f_code.co_name}")] += 1
            if caller_mod.startswith(SIM_PKG) and callee_mod.startswith(FORBIDDEN_PKGS):
                self.forbidden[(caller_mod, f"{callee_mod}.{frame.f_code.co_name}")] += 1
        elif event == "c_call":
            mod = frame.f_globals.get("__name__", "")
            if mod.startswith(SIM_PKG) and getattr(arg, "__name__", "") in FORBIDDEN_C:
                self.forbidden[(mod, f"builtin.{arg.__name__}")] += 1

    def __enter__(self):
        sys.setprofile(self)
        return self

    def __exit__(self, *exc):
        sys.setprofile(None)


def static_imports() -> Dict[str, List[str]]:
    """Modules each simulation file imports (the engine must not import geometry/sources/advisor/ui)."""
    out = {}
    for path in sorted((ROOT / "save_the_whales" / "simulation").glob("*.py")):
        mods = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                mods.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.update(f"{node.module}.{a.name}" for a in node.names)
        out[path.name] = sorted(mods)
    return out


# --- shape checks --------------------------------------------------------------------------------
def shape_problems(obj: Any, td, where: str) -> List[str]:
    if not isinstance(obj, dict):
        return [f"{where}: not a dict"]
    req, allowed = set(td.__required_keys__), set(td.__annotations__)
    out = [f"{where}: missing {sorted(req - set(obj))}"] if req - set(obj) else []
    if set(obj) - allowed:
        out.append(f"{where}: unexpected {sorted(set(obj) - allowed)}")
    return out


class Boundary:
    """Wraps the injected callbacks and records contract problems seen at the boundary."""

    def __init__(self, cfg, land):
        self.problems: List[str] = []
        self.counts: Counter = Counter()
        self.legs: List[Dict[str, Any]] = []
        self.land = land
        self.validator = F.make_fixture_validator(cfg, geo)
        self.weather_fn = F.make_fixture_weather_exposure(geo)

    def _obs(self, obs, where):
        p = shape_problems(obs, C.Observation, where)
        for s in obs.get("ships", []):
            p += shape_problems(s, C.ObservableShip, f"{where}.ship")
            if s.get("incident_budget") is not None:
                p += shape_problems(s["incident_budget"], C.IncidentBudget, f"{where}.incident_budget")
            if s.get("active_command") is not None:
                p += shape_problems(s["active_command"], C.Action, f"{where}.active_command")
        for d in obs.get("whale_detections", []):
            p += shape_problems(d, C.WhaleDetection, f"{where}.detection")
        land = obs["static"]["land"]
        if (land.get("map_id"), land.get("sha256"), land.get("rings")) != \
                (self.land.get("map_id"), self.land.get("sha256"), self.land.get("rings")):
            p.append(f"{where}: static.land differs from Person 1's LandMap")
        p += shape_problems(obs["static"]["weather"], C.WeatherField, f"{where}.weather")
        self.problems += p

    def decide(self, inner):
        def wrapped(obs):
            self.counts["decide"] += 1
            self._obs(obs, "decide.obs")
            d = inner(obs)
            self.problems += shape_problems(d, C.Decision, "decide.return")
            for a in d.get("actions", []):
                self.problems += shape_problems(a, C.Action, "decide.action")
            return d
        return wrapped

    def validate(self, action, obs, *, purpose="ISSUE"):
        self.counts[f"validate:{purpose}"] += 1
        self._obs(obs, f"validate[{purpose}].obs")
        self.problems += shape_problems(action, C.Action, f"validate[{purpose}].action")
        res = self.validator(action, obs, purpose=purpose)
        self.problems += shape_problems(res, C.ValidationResult, "validate.return")
        return res

    def weather(self, legs, field):
        self.counts["weather_exposure"] += 1
        for leg in legs:
            self.problems += shape_problems(leg, C.TimedLeg, "weather.leg")
            # Units/projection agree with Person 1: planar length == knots_to_mps(speed) * duration.
            d = geo.distance_m(leg["start"], leg["end"])
            expect = geo.knots_to_mps(leg["speed_knots"]) * (leg["end_s"] - leg["start_s"])
            if abs(d - expect) > 1e-3:
                self.problems.append(f"weather.leg: length {d:.4f} m != speed*time {expect:.4f} m")
        self.legs += copy.deepcopy(legs)
        return self.weather_fn(legs, field)


def trajectory_checks(legs: List[Dict[str, Any]], land, length_m=300.0, beam_m=40.0) -> Dict[str, Any]:
    """Single-ship run: legs must chain in time and space; the travelled polyline must pass Person 1's
    water/clearance check at the modeled hull clearance."""
    problems = []
    for a, b in zip(legs, legs[1:]):
        if abs(a["end_s"] - b["start_s"]) > 1e-6 or geo.distance_m(a["end"], b["start"]) > 1e-6:
            problems.append(f"discontinuity at t={a['end_s']:.3f}")
    poly = [legs[0]["start"]] + [leg["end"] for leg in legs] if legs else []
    clearance = geo.ship_clearance_m(length_m, beam_m, 100.0)
    water = geo.check_water_path(poly, land, clearance) if poly else {"ok": False, "reasons": ["NO_LEGS"]}
    return {"legs": len(legs), "continuity_problems": problems, "water_ok": water["ok"],
            "water_reasons": water["reasons"], "min_clearance_m": water.get("min_clearance_m"),
            "hull_clearance_m": clearance, "travelled_m": geo.path_length_m(poly) if poly else 0.0}


# --- scenarios -----------------------------------------------------------------------------------
def _run(name, inputs, decide, cfg, land, tracer, single_ship=True, weather=None):
    b = Boundary(cfg, land)
    with tracer:
        res = run_scenario(inputs, b.decide(decide), b.validate, weather or b.weather, project=geo.project)
    rec = {"scenario": name, "status": res["summary"]["status"],
           "status_reasons": res["manifest"]["status_reasons"], "summary": res["summary"],
           "boundary_calls": dict(b.counts), "boundary_problems": b.problems,
           "result_problems": shape_problems(res, C.RunResult, "result")
           + shape_problems(res["summary"], C.RunSummary, "summary"),
           "rejections": [e["details"]["reasons"] for e in res["events"] if e["type"] == "ACTION_REJECTED"],
           "manifest_land": res["manifest"].get("land"), "manifest_bundle": res["manifest"].get("route_bundle"),
           "input_bundle_sha256": canonical_sha256(inputs["route_bundle"]),
           "input_land_sha256": inputs["land"].get("sha256")}
    if single_ship and b.legs:
        rec["trajectory"] = trajectory_checks(b.legs, land)
    return rec, res


def trace() -> Dict[str, Any]:
    cfg = F.fixture_config()
    tracer = CallTracer()
    report: Dict[str, Any] = {"runs": [], "checks": {}}
    with tracer:   # Person 1 loaders are traced too (test harness -> sources)
        real_land = landmod.load_land(REAL_LAND, cfg)
        toy_blocked = landmod.load_land(F.COMMON / "land_blocked_side.fixture.geojson", cfg)
        fixture_bundle = routes.load_route_bundle(F.COMMON / "route_bundle.fixture.json", real_land, cfg)
        cap_records = ais.load_capture(ROOT / "tests" / "fixtures" / "sources" / "capture.fixture.jsonl")
        prepared = routes.prepare_routes(cap_records, real_land, cfg)
        real_records = ais.load_capture(REAL_CAPTURE) if REAL_CAPTURE.exists() else []
        real_bundle = routes.prepare_routes(real_records, real_land, cfg)
    prov = json.loads((ROOT / "data" / "land" / "provenance.json").read_text())
    report["inputs"] = {
        "real_land": {k: real_land.get(k) for k in ("map_id", "validated", "fixture_only", "sha256", "issues")}
        | {"rings": len(real_land["rings"]), "provenance_sha256": prov.get("sha256")},
        "fixture_bundle": {"source": fixture_bundle["source"], "issues": fixture_bundle["issues"],
                           "routes_ok": [r["validation"]["ok"] for r in fixture_bundle["routes"]]},
        "prepared_from_capture_fixture": {"routes": [r["route_id"] for r in prepared["routes"]],
                                          "source": prepared["source"], "issues": prepared["issues"]},
        "real_capture": {"records": len(real_records), "source": real_bundle["source"],
                         "issues": real_bundle["issues"], "routes": len(real_bundle["routes"]),
                         "stats": real_bundle.get("preparation_stats")},
    }
    scen = F.fixture_scenario()
    runs = [
        ("fixture route on real map, no action", F.make_inputs(fixture_bundle, real_land, scen, cfg), F.no_action, real_land),
        ("fixture route on real map, scripted south detour", F.make_inputs(fixture_bundle, real_land, scen, cfg),
         F.make_scripted_decider({60: F.detour_at()}), real_land),
        ("toy blocked-side land, north detour into land", F.make_inputs(
            routes.load_route_bundle(F.COMMON / "route_bundle.fixture.json", toy_blocked, cfg), toy_blocked, scen, cfg),
         F.make_scripted_decider({60: F.detour_at(detour=F.NORTH_DETOUR)}), toy_blocked),
        ("prepare_routes(capture fixture) on real map, detour", F.make_inputs(
            prepared, real_land, dict(scen, voyages=[{"voyage_id": "v1", "route_id": prepared["routes"][0]["route_id"],
                                                      "entry_s": 0}]), cfg),
         F.make_scripted_decider({60: F.detour_at()}), real_land),
        ("storm south, detour through storm", F.make_inputs(fixture_bundle, real_land, dict(
            scen, weather=F.load_json(F.COMMON / "weather_storm_south.fixture.json")), cfg),
         F.make_scripted_decider({60: F.detour_at()}), real_land),
        ("real AIS capture bundle", F.make_inputs(real_bundle, real_land, scen, cfg), F.no_action, real_land),
    ]
    results = {}
    for name, inputs, decide, land in runs:
        rec, res = _run(name, inputs, decide, cfg, land, tracer)
        report["runs"].append(rec)
        results[name] = res
    report["crossings"] = sorted(([c, f, n] for (c, f), n in tracer.crossings.items()), key=lambda r: (r[0], r[1]))
    report["forbidden_calls"] = sorted(([c, f, n] for (c, f), n in tracer.forbidden.items()))
    imports = static_imports()
    bad_imports = {f: [m for m in mods if m.startswith(("save_the_whales.shared.geo", "save_the_whales.sources",
                                                        "save_the_whales.advisor", "save_the_whales.ui"))]
                   for f, mods in imports.items()}
    report["static_imports"] = {f: [m for m in mods if m.startswith("save_the_whales")] for f, mods in imports.items()}

    by = {r["scenario"]: r for r in report["runs"]}
    sim_to_geo = {f for c, f, _ in report["crossings"] if c.startswith(SIM_PKG)}
    c = report["checks"]
    c["P1 real land loads validated, non-fixture, hash == provenance"] = (
        real_land["validated"] and real_land["fixture_only"] is False and real_land["sha256"] == prov.get("sha256"))
    c["P1 bundle/map identity preserved in every P2 manifest"] = all(
        r["manifest_bundle"]["sha256"] == r["input_bundle_sha256"]
        and r["manifest_land"]["sha256"] == r["input_land_sha256"]
        for r in report["runs"] if r["status"] != C.RUN_ERROR)
    c["no boundary contract problems"] = not any(r["boundary_problems"] or r["result_problems"] for r in report["runs"])
    c["simulation reaches P1 geometry only via injected project"] = sim_to_geo == {"save_the_whales.shared.geo.project"}
    c["simulation never calls sources/advisor/ui/network/file I/O"] = not report["forbidden_calls"]
    c["simulation does not import geo/sources/advisor/ui"] = not any(bad_imports.values())
    c["baseline on real map: COMPLETE, 1 contact, travelled path passes P1 clearance"] = (
        by["fixture route on real map, no action"]["status"] == C.RUN_COMPLETE
        and by["fixture route on real map, no action"]["summary"]["modeled_contacts"] == 1
        and by["fixture route on real map, no action"]["trajectory"]["water_ok"])
    det = by["fixture route on real map, scripted south detour"]
    c["detour accepted by P1 check_water_path, motion continuous and in water"] = (
        det["summary"]["route_changes"] == 1 and det["summary"]["modeled_contacts"] == 0
        and not det["trajectory"]["continuity_problems"] and det["trajectory"]["water_ok"])
    blk = by["toy blocked-side land, north detour into land"]
    c["north detour rejected with P1 code POINT_ON_LAND"] = (
        blk["summary"]["route_changes"] == 0 and any(C.POINT_ON_LAND in r for r in blk["rejections"]))
    prep = by["prepare_routes(capture fixture) on real map, detour"]
    c["prepare_routes output runs unchanged in the simulator"] = (
        prep["status"] == C.RUN_COMPLETE and prep["summary"]["route_changes"] == 1 and prep["trajectory"]["water_ok"])
    storm = by["storm south, detour through storm"]
    c["storm exposure via P1 segment_zone_intervals > 0"] = (storm["summary"]["weather_exposure_s"] or 0) > 0
    real = by["real AIS capture bundle"]
    c["real capture without usable routes -> structured ERROR AIS_ROUTE_UNAVAILABLE"] = (
        real["status"] == C.RUN_ERROR and C.AIS_ROUTE_UNAVAILABLE in real["status_reasons"]) \
        if not report["inputs"]["real_capture"]["routes"] else real["status"] == C.RUN_COMPLETE
    report["ok"] = all(c.values())
    return report


def main() -> int:
    rep = trace()
    print("== Inputs from Person 1 ==")
    print(json.dumps(rep["inputs"], indent=1, default=str))
    print("\n== Cross-module calls into Person 1 (caller module -> callee: count) ==")
    for caller, callee, n in rep["crossings"]:
        print(f"  {caller:45s} -> {callee:52s} {n:7d}")
    print("\n== Forbidden calls from simulation ==", rep["forbidden_calls"] or "none")
    print("\n== Simulation package imports ==")
    for f, mods in rep["static_imports"].items():
        print(f"  {f:12s} {', '.join(mods) or '-'}")
    print("\n== Runs ==")
    for r in rep["runs"]:
        s = r["summary"]
        line = (f"  {r['scenario']:52s} {r['status']:10s} contacts={s['modeled_contacts']} "
                f"approaches={s['close_approaches']} changes={s['route_changes']} rejected={s['rejected_actions']} "
                f"weather_s={s['weather_exposure_s']} delay_s={s['added_delay_s']}")
        print(line)
        if r.get("trajectory"):
            t = r["trajectory"]
            print(f"      trajectory: legs={t['legs']} travelled={t['travelled_m']:.1f} m continuity_issues="
                  f"{len(t['continuity_problems'])} water_ok={t['water_ok']} min_clearance={t['min_clearance_m']:.0f} m "
                  f"(hull radius {t['hull_clearance_m']:.1f} m)")
        print(f"      boundary calls: {r['boundary_calls']}  reasons: {r['status_reasons']}")
        if r["rejections"]:
            print(f"      rejections: {r['rejections']}")
        for p in r["boundary_problems"] + r["result_problems"]:
            print(f"      PROBLEM: {p}")
    print("\n== Checks ==")
    for name, ok in rep["checks"].items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
