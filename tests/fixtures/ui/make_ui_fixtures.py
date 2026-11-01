"""Regenerate Person 4's canned UI fixtures (SYNTHETIC_FIXTURE, fixture_only).

    .venv/bin/python tests/fixtures/ui/make_ui_fixtures.py

Runs the fixture crossing case once through the real modules, then trims each result to two frames and
writes the canned files the preview and UI-contract tests read without computing anything. The
``incomplete`` variant is hand-derived from the advisor result: one ``NO_FEASIBLE_ROUTE`` voyage, so
delay/distance deltas are null. None of this is real AIS, real coastline or a real run.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))

from app import production_services                       # noqa: E402
from save_the_whales.shared import contracts as C         # noqa: E402

LABEL = "SYNTHETIC_FIXTURE canned UI result; not a real run"


def _trim(result, keep_times):
    res = copy.deepcopy(result)
    res["frames"] = [f for f in res["frames"] if f["sim_time_s"] in keep_times]
    res["decisions"] = res["decisions"][:6]
    res["manifest"]["fixture_only"] = True
    res["_fixture"] = LABEL
    return res


def main() -> None:
    cfg = json.loads((ROOT / "config" / "default.json").read_text())
    cfg["response"] = {"response_lag_s": 0, "compliance": 1.0}
    cfg["sensors"]["observers"][0].update(p_detect=1.0, position_error_m=100, delay_max_s=30)
    s = production_services(cfg)
    land = s["load_land"](ROOT / "tests" / "fixtures" / "common" / "land_far.fixture.geojson", cfg)
    bundle = s["load_route_bundle"](ROOT / "tests" / "fixtures" / "common" / "route_bundle.fixture.json", land, cfg)
    settings = s["load_presets"](cfg)["crossing"]
    inputs = s["build_run_inputs"](bundle, land, settings, cfg)
    pair = s["run_pair"](inputs)
    adv_times = [f["sim_time_s"] for f in pair["advisor"]["frames"]]
    keep = {adv_times[1], adv_times[min(5, len(adv_times) - 1)]}
    complete = {"inputs": dict(inputs, _fixture=LABEL), "baseline": _trim(pair["baseline"], keep),
                "advisor": _trim(pair["advisor"], keep)}
    assert complete["advisor"]["summary"]["route_changes"] >= 1 and len(complete["advisor"]["frames"]) == 2

    inc = copy.deepcopy(complete)
    adv = inc["advisor"]
    sid = adv["manifest"]["voyages"][0]["ship_id"]
    adv["summary"].update(status=C.RUN_INCOMPLETE, voyages_completed=0, voyages_failed=1, added_delay_s=None,
                          extra_distance_m=None, route_changes=0, alerts=0)
    adv["manifest"].update(status=C.RUN_INCOMPLETE, status_reasons=[f"VOYAGE_FAILED:{C.NO_FEASIBLE_ROUTE}"])
    adv["manifest"]["voyages"][0].update(status="FAILED", fail_reason=C.NO_FEASIBLE_ROUTE, added_delay_s=None,
                                         extra_distance_m=None, duration_s=None, censored=True)
    for d in adv["decisions"]:
        d["actions"] = []
        for a in d["assessments"]:
            a.update(status=C.NO_FEASIBLE_ROUTE, reason_codes=[C.NO_FEASIBLE_ROUTE, "WHALE_WATCH"], selected_path=[])
            for c in a["rejected_candidates"]:
                c["reasons"] = ["LAND_CROSSING", "WEATHER_BLOCKED"]
    adv["events"] = [e for e in adv["events"] if e["sim_time_s"] <= 60] + [
        {"sim_time_s": 60.0, "type": "VOYAGE_FAILED", "ship_id": sid,
         "details": {"reason": C.NO_FEASIBLE_ROUTE, "baseline_progress_index": 0}}]
    adv["frames"] = adv["frames"][:1] + [dict(adv["frames"][1], ships=[], alerts=[])]

    capture = {
        "key_missing": {"state": C.CAPTURE_ERROR, "reason_codes": [C.AIS_KEY_MISSING], "records_received": 0,
                        "records_valid": 0, "vessels_tracked": 0, "last_received_at": None, "api_verified": False},
        "capturing": {"state": C.CAPTURE_CAPTURING, "reason_codes": [], "records_received": 1250,
                      "records_valid": 1180, "vessels_tracked": 14, "last_received_at": "2026-07-01T11:59:58Z",
                      "api_verified": True},
        "degraded": {"state": C.CAPTURE_DEGRADED, "reason_codes": [C.AIS_NO_RECENT_MESSAGES], "records_received": 40,
                     "records_valid": 38, "vessels_tracked": 2, "last_received_at": "2026-07-01T11:40:00Z",
                     "api_verified": True},
    }
    out = {"run_pair_complete.fixture.json": complete, "run_pair_incomplete.fixture.json": inc,
           "capture_status.fixture.json": dict(capture, _fixture=LABEL)}
    for name, obj in out.items():
        (HERE / name).write_text(json.dumps(obj, indent=1, sort_keys=True) + "\n")
        print("wrote", name, f"{(HERE / name).stat().st_size} bytes")


if __name__ == "__main__":
    main()
