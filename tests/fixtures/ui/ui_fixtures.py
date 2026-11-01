"""TEST-ONLY preview services for the dashboard: a fake capture handle, fixture bundle/land and canned
run results. Every advisor/simulator/geometry entry raises, which proves the UI computes nothing itself.
Nothing here is real AIS, real coastline or a real run (``fixture_only``).

Cases: ``complete`` (accepted DETOUR), ``incomplete`` (NO_FEASIBLE_ROUTE, null deltas), ``no_sightings``
(no reports, INSUFFICIENT_FRESH_EVIDENCE), ``invalid_map``, ``key_missing`` (capture ERROR),
``no_routes`` (AIS_ROUTE_UNAVAILABLE, ERROR runs), ``error`` (callback ERROR).
"""
from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path
from typing import Any, Dict

from save_the_whales.shared import contracts as C

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
COMMON = ROOT / "tests" / "fixtures" / "common"
CASES = ("complete", "incomplete", "no_sightings", "invalid_map", "key_missing", "no_routes", "error")
MODE_LABEL = "TEST_PREVIEW (fixture_only)"


def load(name: str) -> Any:
    return json.loads((HERE / name).read_text())


class FakeCaptureHandle:
    """Canned ``CaptureHandle``: copied status/records, never a network connection."""

    def __init__(self, status: Dict[str, Any], records=()):
        self._status, self._records = copy.deepcopy(status), [dict(r) for r in records]
        self.stopped = False

    def status(self) -> Dict[str, Any]:
        return copy.deepcopy(self._status)

    def snapshot_records(self):
        return [dict(r) for r in self._records]

    def stop(self) -> None:
        self.stopped = True
        self._status["state"] = C.CAPTURE_STOPPED


def fixture_land(valid: bool = True) -> Dict[str, Any]:
    """Toy LandMap (the far rectangle from the common recipe), or an invalid empty map."""
    ring = [{"lat": 34.304, "lon": -120.075}, {"lat": 34.304, "lon": -120.055},
            {"lat": 34.316, "lon": -120.055}, {"lat": 34.316, "lon": -120.075}]
    if not valid:
        return {"map_id": "missing", "source": "unknown", "sha256": None, "validated": False, "fixture_only": True,
                "bbox": dict(C.REGION_BBOX), "rings": [], "issues": [C.INVALID_MAP, C.MAP_FILE_MISSING]}
    return {"map_id": "toy-land-far", "source": "SYNTHETIC_FIXTURE toy rectangle", "sha256": "0" * 64,
            "validated": True, "fixture_only": True, "bbox": dict(C.REGION_BBOX), "rings": [ring], "issues": []}


def fixture_bundle(routes: bool = True) -> Dict[str, Any]:
    b = json.loads((COMMON / "route_bundle.fixture.json").read_text())
    if not routes:
        b.update(routes=[], issues=[C.AIS_ROUTE_UNAVAILABLE])
    return b


def _error_result(codes, run_id="preview-error"):
    summary = {"status": C.RUN_ERROR, "elapsed_wall_s": 0.0, "simulated_duration_s": 0.0, "voyages_completed": 0,
               "voyages_failed": 0, "modeled_contacts": 0, "close_approaches": 0, "whale_exposure_proxy": 0.0,
               "weather_exposure_s": None, "added_delay_s": None, "extra_distance_m": None, "alerts": 0,
               "route_changes": 0, "rejected_actions": 0}
    return {"manifest": {"run_id": run_id, "status": C.RUN_ERROR, "status_reasons": list(codes), "fixture_only": True,
                         "voyages": []},
            "summary": summary, "frames": [], "events": [{"sim_time_s": 0.0, "type": "INPUT_ERROR",
                                                           "details": {"codes": list(codes)}}], "decisions": []}


def canned_pair(case: str) -> Dict[str, Any]:
    from run_comparison import compare_runs    # Person 4's own pure function; no simulation

    if case == "incomplete":
        pair = load("run_pair_incomplete.fixture.json")
    else:
        pair = load("run_pair_complete.fixture.json")
    if case == "no_sightings":
        for res in (pair["baseline"], pair["advisor"]):
            for f in res["frames"]:
                f["whale_detections"] = []
        adv = pair["advisor"]
        for d in adv["decisions"]:
            d["actions"] = []
            for a in d["assessments"]:
                a.update(status=C.INSUFFICIENT_FRESH_EVIDENCE, reason_codes=[C.INSUFFICIENT_FRESH_EVIDENCE])
        adv["summary"].update(route_changes=0, alerts=0)
    if case == "invalid_map":
        adv = pair["advisor"]
        adv["manifest"]["issues"] = [C.INVALID_MAP]
        for d in adv["decisions"]:
            d["actions"] = []
            for a in d["assessments"]:
                a.update(status="NO_ACTION", reason_codes=["WHALE_WATCH", C.INVALID_MAP])
        adv["summary"].update(route_changes=0, alerts=0, modeled_contacts=1)
    if case in ("no_routes", "error"):
        codes = [C.AIS_ROUTE_UNAVAILABLE, "NO_VOYAGES"] if case == "no_routes" else ["CALLBACK_ERROR:decide"]
        pair["baseline"], pair["advisor"] = _error_result(codes), _error_result(codes)
    pair["comparison"] = compare_runs(pair["baseline"], pair["advisor"])
    return pair


def _no_calculation(name: str):
    def fail(*_a, **_k):
        raise AssertionError(f"preview services perform no {name} calculation")
    return fail


def preview_services(case: str = "complete") -> Dict[str, Any]:
    from run_comparison import compare_runs, save_run
    from save_the_whales.ui import scenarios

    if case not in CASES:
        raise ValueError(f"unknown preview case {case!r}")
    cfg = json.loads((ROOT / "config" / "default.json").read_text())
    statuses = load("capture_status.fixture.json")
    status = statuses["key_missing" if case == "key_missing" else "capturing"]
    land = fixture_land(valid=case != "invalid_map")
    bundle = fixture_bundle(routes=case != "no_routes")
    calls: Dict[str, int] = {}

    def count(name, value):
        def f(*_a, **_k):
            calls[name] = calls.get(name, 0) + 1
            return copy.deepcopy(value() if callable(value) else value)
        return f

    pair = canned_pair(case)
    return {
        "mode_label": MODE_LABEL, "preview": True, "config": cfg, "calls": calls,
        "paths": {"bundle": str(COMMON / "route_bundle.fixture.json"), "land": "fixture:toy-land",
                  "runs": str(Path(tempfile.gettempdir()) / "stw-preview-runs")},
        "start_capture": count("start_capture", lambda: FakeCaptureHandle(status)),
        "save_capture": count("save_capture", None), "load_capture": count("load_capture", []),
        "prepare_routes": count("prepare_routes", bundle), "save_route_bundle": count("save_route_bundle", None),
        "load_route_bundle": count("load_route_bundle", bundle), "load_land": count("load_land", land),
        "geometry": None, "make_decider": _no_calculation("advisor"), "make_validator": _no_calculation("validator"),
        "weather_exposure": _no_calculation("weather"), "run_scenario": _no_calculation("simulation"),
        "load_presets": scenarios.load_presets, "land_override_path": lambda settings: None,
        "build_run_inputs": count("build_run_inputs", pair["inputs"]), "readiness": scenarios.readiness,
        "run_pair": count("run_pair", pair), "compare_runs": compare_runs, "save_run": save_run,
    }
