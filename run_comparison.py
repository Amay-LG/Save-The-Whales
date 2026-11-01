"""Small paired comparison: ``NO_ACTION`` versus ``ADVISOR`` on identical frozen inputs (Architecture §11).

    python run_comparison.py --bundle data/ais/route_bundle.json --land data/land/santa_barbara.geojson \
        --scenario crossing --seed 0

Both policies run through the same ``run_scenario`` (separate world instances, same route bundle, whales,
weather, schedules, compliance draws and keyed sensor noise), so outcome accounting is never bypassed.
``compare_runs`` reports paired differences only where both values exist: an incomplete voyage's null
delay/distance stays null rather than being subtracted, and no confidence intervals are invented.
Artifacts go to ``runs/<run_id>/`` (git-ignored).
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from save_the_whales.shared import contracts as C

ROOT = Path(__file__).resolve().parent
NO_ACTION, ADVISOR = "NO_ACTION", "ADVISOR"
METRICS = (  # (summary key, unit)
    ("modeled_contacts", "count"), ("close_approaches", "count"), ("whale_exposure_proxy", "proxy"),
    ("weather_exposure_s", "severity-s"), ("added_delay_s", "s"), ("extra_distance_m", "m"),
    ("alerts", "count"), ("route_changes", "count"), ("rejected_actions", "count"),
    ("voyages_completed", "count"), ("voyages_failed", "count"), ("simulated_duration_s", "s"),
    ("elapsed_wall_s", "s"),
)
PAIRING_KEYS = ("seed", "scenario_id", "start_at", "config_sha256")
NOTES = [
    "All outcomes are in the simulated world; real AIS supplies baseline geometry only.",
    "Modeled contacts/close approaches are geometric counts; whale_exposure_proxy is a smooth diagnostic. "
    "Neither is a count of deaths avoided.",
    "Null deltas mean a voyage did not complete; they are never subtracted or replaced with zero.",
    "One seed and scenario: no confidence intervals or significance claims.",
]


def no_action(observation: Dict[str, Any]) -> Dict[str, Any]:
    """The ``NO_ACTION`` policy: keep every baseline plan."""
    return {"decision_id": observation["decision_id"], "actions": [], "assessments": []}


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _pairing(base: Dict[str, Any], adv: Dict[str, Any]) -> List[str]:
    mb, ma = base.get("manifest") or {}, adv.get("manifest") or {}
    out = [k for k in PAIRING_KEYS if mb.get(k) != ma.get(k)]
    if (mb.get("route_bundle") or {}).get("sha256") != (ma.get("route_bundle") or {}).get("sha256"):
        out.append("route_bundle")
    if (mb.get("land") or {}).get("dict_sha256") != (ma.get("land") or {}).get("dict_sha256"):
        out.append("land")
    if mb.get("weather") != ma.get("weather"):
        out.append("weather")
    return out


def rejected_candidates_by_factor(result: Dict[str, Any]) -> Dict[str, int]:
    """Hard-constraint reasons across all advisor assessments (``NOT_SELECTED`` is not a rejection)."""
    counts: Counter = Counter()
    for d in result.get("decisions") or []:
        for a in d.get("assessments") or []:
            for c in a.get("rejected_candidates") or []:
                counts.update(r for r in c.get("reasons") or [] if r != "NOT_SELECTED")
    return dict(sorted(counts.items()))


def rejected_actions_by_reason(result: Dict[str, Any]) -> Dict[str, int]:
    counts: Counter = Counter()
    for e in result.get("events") or []:
        if e.get("type") == "ACTION_REJECTED":
            counts.update(e.get("details", {}).get("reasons") or [])
    return dict(sorted(counts.items()))


def compare_runs(baseline_result: Dict[str, Any], advisor_result: Dict[str, Any], *,
                 hard_constraint_checks: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Paired summary differences (advisor minus baseline). Pure: reads only the two results and, when
    ``run_pair`` supplies them, the blocked-zone entries observed on each policy's actual motion."""
    sb, sa = baseline_result.get("summary") or {}, advisor_result.get("summary") or {}
    rows = []
    for key, unit in METRICS:
        b, a = _num(sb.get(key)), _num(sa.get(key))
        rows.append({"metric": key, "unit": unit, "no_action": b, "advisor": a,
                     "difference": (a - b) if a is not None and b is not None else None})
    statuses = {NO_ACTION: sb.get("status"), ADVISOR: sa.get("status")}
    if C.RUN_ERROR in statuses.values():
        status = C.RUN_ERROR
    elif all(s == C.RUN_COMPLETE for s in statuses.values()):
        status = C.RUN_COMPLETE
    else:
        status = C.RUN_INCOMPLETE
    voyages: Dict[str, Dict[str, Any]] = {}
    for label, res in ((NO_ACTION, baseline_result), (ADVISOR, advisor_result)):
        for v in (res.get("manifest") or {}).get("voyages") or []:
            voyages.setdefault(v["voyage_id"], {"voyage_id": v["voyage_id"], "route_id": v.get("route_id")})[label] = {
                k: v.get(k) for k in ("status", "fail_reason", "added_delay_s", "extra_distance_m",
                                      "weather_exposure_s", "modeled_contacts", "close_approaches",
                                      "whale_exposure_proxy", "censored")}
    mismatches = _pairing(baseline_result, advisor_result)
    mb = baseline_result.get("manifest") or {}
    return {
        "status": status, "statuses": statuses,
        "status_reasons": {NO_ACTION: list(mb.get("status_reasons") or []),
                           ADVISOR: list((advisor_result.get("manifest") or {}).get("status_reasons") or [])},
        "paired": not mismatches, "pairing_mismatches": mismatches,
        "fixture_only": bool(mb.get("fixture_only") or (advisor_result.get("manifest") or {}).get("fixture_only")),
        "rows": rows, "differences": {r["metric"]: r["difference"] for r in rows},
        "voyages": [voyages[k] for k in sorted(voyages)],
        "hard_constraint_violations": copy.deepcopy(hard_constraint_checks or {NO_ACTION: [], ADVISOR: []}),
        "rejected_candidates_by_factor": rejected_candidates_by_factor(advisor_result),
        "rejected_actions_by_reason": {NO_ACTION: rejected_actions_by_reason(baseline_result),
                                       ADVISOR: rejected_actions_by_reason(advisor_result)},
        "notes": list(NOTES),
    }


def _watch_blocked(weather_exposure: Callable, blocked_hits: Optional[Callable], hits: Dict[str, Dict[str, Any]]):
    """Pass-through weather callback that also records active blocked zones entered by the actual
    simulated legs (one entry per zone, first/last leg time), so a policy that sails through one stays
    visible in the comparison (Architecture §11)."""
    if blocked_hits is None:
        return weather_exposure

    def observed(legs, weather):
        value = weather_exposure(legs, weather)
        for zone_id in blocked_hits(legs, weather):
            h = hits.setdefault(zone_id, {"code": "WEATHER_BLOCKED", "zone_id": zone_id,
                                          "first_s": legs[0]["start_s"], "last_s": legs[-1]["end_s"]})
            h["last_s"] = legs[-1]["end_s"]
        return value
    return observed


def run_pair(inputs: Dict[str, Any], *, services: Dict[str, Any],
             on_frame: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
    """Run both policies on deep copies of the same frozen inputs with the injected real services."""
    geo, cfg = services["geometry"], inputs["config"]
    results, checks = {}, {}
    for label, decide in ((NO_ACTION, no_action), (ADVISOR, services["make_decider"](cfg, geo))):
        run_inputs = copy.deepcopy(inputs)
        run_inputs["run_id"] = f"{inputs['run_id']}-{label.lower()}"
        hits: Dict[str, Dict[str, Any]] = {}
        weather = _watch_blocked(services["weather_exposure"], services.get("blocked_hits"), hits)
        results[label] = services["run_scenario"](run_inputs, decide, services["make_validator"](cfg, geo),
                                                  weather, project=geo.project, on_frame=on_frame)
        checks[label] = [hits[z] for z in sorted(hits)]
    return {"inputs": inputs, "baseline": results[NO_ACTION], "advisor": results[ADVISOR],
            "comparison": compare_runs(results[NO_ACTION], results[ADVISOR], hard_constraint_checks=checks)}


def _dump(obj: Any, path: Path) -> None:
    path.write_text(json.dumps(obj, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")


def save_run(pair: Dict[str, Any], out_dir: Any, *, frames: bool = True) -> Path:
    """Write ``inputs.json``, one result file per policy and ``comparison.json`` under a unique directory."""
    base = Path(out_dir) / pair["inputs"]["run_id"]
    run_dir, n = base, 1
    while run_dir.exists():
        n += 1
        run_dir = base.with_name(f"{base.name}-{n}")
    run_dir.mkdir(parents=True)
    _dump(pair["inputs"], run_dir / "inputs.json")
    for label, key in ((NO_ACTION, "baseline"), (ADVISOR, "advisor")):
        res = dict(pair[key]) if frames else dict(pair[key], frames=[])
        _dump(res, run_dir / f"result_{label.lower()}.json")
    _dump(pair["comparison"], run_dir / "comparison.json")
    return run_dir


def _fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    return f"{v:.4g}" if isinstance(v, float) else str(v)


def print_comparison(cmp: Dict[str, Any], readiness: Dict[str, Any], out=None) -> None:
    w = (out or sys.stdout).write
    if not readiness.get("demo_ready"):
        w(f"NOT DEMO-READY ({', '.join(readiness.get('reasons') or [])}); results are "
          f"{'fixture_only' if cmp['fixture_only'] else 'not acceptance evidence'}.\n")
    w(f"status: {cmp['status']} {cmp['statuses']}  paired: {cmp['paired']} {cmp['pairing_mismatches'] or ''}\n")
    for label, reasons in cmp["status_reasons"].items():
        if reasons:
            w(f"  {label} reasons: {', '.join(reasons)}\n")
    w(f"{'metric':<24}{'unit':<12}{'NO_ACTION':>14}{'ADVISOR':>14}{'difference':>14}\n")
    for r in cmp["rows"]:
        w(f"{r['metric']:<24}{r['unit']:<12}{_fmt(r['no_action']):>14}{_fmt(r['advisor']):>14}"
          f"{_fmt(r['difference']):>14}\n")
    for label, hits in cmp["hard_constraint_violations"].items():
        if hits:
            w(f"HARD CONSTRAINT VIOLATED by {label}: entered active blocked zone(s) "
              f"{sorted({h['zone_id'] for h in hits})}\n")
    if cmp["rejected_candidates_by_factor"]:
        w(f"rejected candidates by factor: {cmp['rejected_candidates_by_factor']}\n")


def _settings(arg: str, services: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    p = Path(arg)
    if p.suffix == ".json" and p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))
    presets = services["load_presets"](cfg)
    if arg not in presets:
        raise SystemExit(f"unknown scenario {arg!r}; presets: {', '.join(sorted(presets))}")
    return presets[arg]


def main(argv: Optional[List[str]] = None) -> int:
    from app import DEFAULT_BUNDLE, DEFAULT_LAND, load_config, production_services

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bundle", default=str(DEFAULT_BUNDLE), help="route bundle JSON")
    ap.add_argument("--land", default=str(DEFAULT_LAND), help="land GeoJSON (presets may override with a fixture)")
    ap.add_argument("--scenario", default="crossing", help="preset id or a settings JSON file")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--voyages", type=int, default=None)
    ap.add_argument("--whales", type=int, default=None, help="background whales")
    ap.add_argument("--config", default=str(ROOT / "config" / "default.json"))
    ap.add_argument("--out", default=str(ROOT / "runs"))
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--no-frames", action="store_true", help="omit frames from saved results")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    services = production_services(cfg)
    settings = dict(_settings(args.scenario, services, cfg))
    for key, val in (("seed", args.seed), ("voyages", args.voyages), ("background_whales", args.whales)):
        if val is not None:
            settings[key] = val
    land_path = services["land_override_path"](settings) or args.land
    land = services["load_land"](land_path, cfg)
    bundle = services["load_route_bundle"](args.bundle, land, cfg)
    ready = services["readiness"](bundle, land)
    inputs = services["build_run_inputs"](bundle, land, settings, cfg)
    pair = services["run_pair"](inputs)
    print_comparison(pair["comparison"], ready)
    if not args.no_save:
        print(f"saved: {save_run(pair, args.out, frames=not args.no_frames)}")
    return 1 if pair["comparison"]["status"] == C.RUN_ERROR else 0


if __name__ == "__main__":
    sys.exit(main())
