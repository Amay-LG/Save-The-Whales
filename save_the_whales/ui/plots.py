"""Pure presentation helpers: map layer specs, legends and table rows. No Streamlit, no calculation.

Every layer has a fixed id, style and honest label, so recorded source tracks, simulated paths, storm
polygons, reported sightings and the optional analyst-truth overlay can't be confused. Values are read
from the run result as produced by the simulator and advisor; nothing here re-scores or re-simulates.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from save_the_whales.shared import contracts as C

# id -> (label, RGBA color, kind). Labels are part of the UI contract (tests check them).
LAYER_STYLES: Dict[str, Tuple[str, List[int], str]] = {
    "land": ("Land (research map, not a navigation chart)", [150, 130, 100, 170], "polygon"),
    "storms": ("Simulated storm zone", [120, 60, 200, 90], "polygon"),
    "baseline": ("Recorded AIS baseline route (real source track)", [20, 20, 20, 255], "path"),
    "baseline_fixture": ("SYNTHETIC FIXTURE baseline (not real AIS)", [200, 120, 0, 255], "path"),
    "tracks": ("Simulated ship track so far", [0, 120, 255, 200], "path"),
    "current_paths": ("Simulated current path / detour ahead", [0, 190, 120, 230], "path"),
    "ships": ("Simulated ship position (not a real vessel's current position)", [0, 80, 220, 255], "scatter"),
    "reports": ("Simulated whale sighting report (with position error)", [230, 50, 50, 160], "scatter"),
    "truth": ("ANALYST TRUTH: simulated whale (never advisor input)", [255, 0, 255, 255], "scatter"),
}
UNMODELED_FACTORS = [
    ("F11", "Ship-ship collisions, separation and coordination"),
    ("F12", "COLREGs, TSS/traffic-lane legality, maritime regulations"),
    ("F13", "Depth, draft and under-keel clearance"),
    ("F14", "Realistic acceleration, turn radius, stopping, currents, waves"),
]
EXPLAIN = {
    C.AIS_KEY_MISSING: "AISSTREAM_API_KEY is not set in the environment; no live capture is possible.",
    C.AIS_ROUTE_UNAVAILABLE: "No usable AIS-derived route: capture longer or load a cached route bundle.",
    C.INVALID_MAP: "Land map missing or invalid: detours are blocked until a validated map is loaded.",
    C.NO_FEASIBLE_ROUTE: "Every option, including the baseline, violates a hard constraint; the voyage stops.",
    C.INSUFFICIENT_FRESH_EVIDENCE: "No fresh sightings. Missing evidence is not proof that no whales are present.",
    C.UNKNOWN_WEATHER: "Weather coverage unknown for part of the path; exposure is not measurable.",
    C.NOT_EVALUATED_BUDGET: "Planner compute budget reached; this ship was not evaluated.",
    "ROUTES_NOT_GENUINE_AIS": "Routes are not API-verified AIS captures (fixture data): not acceptance evidence.",
    "LAND_IS_FIXTURE": "Land map is a test fixture, not the regional research map.",
    "WALL_LIMIT": "Stopped at the wall-clock limit: results are incomplete.",
    "DRAIN_LIMIT": "Voyages still active after the bounded drain period: results are incomplete.",
    "VOYAGES_UNFINISHED": "Some voyages did not finish.",
}


def explain(code: str) -> str:
    """Plain-language text for a machine code; ``PREFIX:CODE`` falls back to either part."""
    base, _, detail = code.partition(":")
    return EXPLAIN.get(code) or EXPLAIN.get(detail) or EXPLAIN.get(base) or code


def is_genuine_ais(bundle: Dict[str, Any]) -> bool:
    s = bundle.get("source") or {}
    return s.get("kind") == C.AISSTREAM_CAPTURE and bool(s.get("api_verified")) and not s.get("fixture_only", True)


def _ll(points: List[Dict[str, float]]) -> List[List[float]]:
    return [[p["lon"], p["lat"]] for p in points]


def _layer(layer_id: str, data: List[Dict[str, Any]]) -> Dict[str, Any]:
    label, color, kind = LAYER_STYLES[layer_id]
    return {"id": layer_id, "label": label, "color": list(color), "kind": kind, "data": data}


def frame_at(result: Optional[Dict[str, Any]], index: int) -> Optional[Dict[str, Any]]:
    frames = (result or {}).get("frames") or []
    if not frames:
        return None
    return frames[max(-len(frames), min(index, len(frames) - 1))]


def map_layers(bundle: Dict[str, Any], land: Optional[Dict[str, Any]], weather: Optional[Dict[str, Any]],
               result: Optional[Dict[str, Any]] = None, frame_index: int = -1,
               show_truth: bool = False) -> List[Dict[str, Any]]:
    """Layer specs (bottom to top). ``truth`` is present only when ``show_truth`` is set."""
    layers = [_layer("land", [{"polygon": _ll(r), "name": (land or {}).get("map_id")}
                              for r in (land or {}).get("rings") or []])]
    zones = (weather or {}).get("zones") or []
    layers.append(_layer("storms", [{"polygon": _ll(z["polygon"]), "name": z["zone_id"], "severity": z["severity"],
                                      "active": f"[{z['active_from_s']:.0f}, {z['active_until_s']:.0f}) s",
                                      "blocked": bool(z.get("blocked"))} for z in zones]))
    base_id = "baseline" if is_genuine_ais(bundle) else "baseline_fixture"
    layers.append(_layer(base_id, [{"path": _ll(r["waypoints"]), "name": r["route_id"],
                                    "source_mmsi": r.get("source_mmsi"),
                                    "captured": f"{r.get('source_start_at')} - {r.get('source_end_at')}"}
                                   for r in bundle.get("routes") or []]))
    frames = (result or {}).get("frames") or []
    frame = frame_at(result, frame_index)
    if frame is None:
        return layers
    upto = frames.index(frame) + 1
    trails: Dict[str, List[List[float]]] = {}
    for f in frames[:upto]:
        for s in f.get("ships") or []:
            trails.setdefault(s["ship_id"], []).append([s["lon"], s["lat"]])
    layers.append(_layer("tracks", [{"path": p, "name": sid} for sid, p in sorted(trails.items()) if len(p) > 1]))
    ships = frame.get("ships") or []
    layers.append(_layer("current_paths", [
        {"path": [[s["lon"], s["lat"]]] + _ll(s.get("current_route") or []), "name": s["ship_id"],
         "command": (s.get("active_command") or {}).get("command") or "BASELINE"} for s in ships]))
    layers.append(_layer("ships", [{"position": [s["lon"], s["lat"]], "name": s["ship_id"], "radius_m": 150,
                                    "sog_knots": round(float(s.get("sog_knots") or 0.0), 2)} for s in ships]))
    layers.append(_layer("reports", [{"position": [d["lon"], d["lat"]], "name": d["detection_id"],
                                      "radius_m": max(50.0, float(d.get("position_error_m") or 0.0)),
                                      "observed_at": d.get("observed_at"), "source": d.get("source")}
                                     for d in frame.get("whale_detections") or []]))
    if show_truth:
        truth = (frame.get("analyst_truth") or {}).get("whales") or []
        layers.append(_layer("truth", [{"position": [w["lon"], w["lat"]], "name": w["whale_id"], "radius_m": 60,
                                        "exposed": w.get("exposed")} for w in truth]))
    return layers


def view_center(bundle: Dict[str, Any]) -> Dict[str, float]:
    pts = [p for r in bundle.get("routes") or [] for p in r["waypoints"]]
    if not pts:
        R = C.REGION_BBOX
        return {"lat": (R["lat_min"] + R["lat_max"]) / 2, "lon": (R["lon_min"] + R["lon_max"]) / 2, "zoom": 8}
    return {"lat": sum(p["lat"] for p in pts) / len(pts), "lon": sum(p["lon"] for p in pts) / len(pts), "zoom": 12}


# --- tables --------------------------------------------------------------------------------------
def fmt(v: Any, digits: int = 4) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        return f"{v:.{digits}g}"
    return str(v)


def comparison_rows(comparison: Dict[str, Any]) -> List[Dict[str, str]]:
    """Display rows; a null value is shown as ``n/a (incomplete)``, never as zero."""
    out = []
    for r in comparison.get("rows") or []:
        cell = lambda v: "n/a (incomplete)" if v is None else fmt(v)
        out.append({"metric": r["metric"], "unit": r["unit"], "NO_ACTION": cell(r["no_action"]),
                    "ADVISOR": cell(r["advisor"]), "ADVISOR - NO_ACTION": cell(r["difference"])})
    return out


def capture_rows(status: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
    status = status or {"state": C.CAPTURE_IDLE}
    keys = ("state", "reason_codes", "records_received", "records_valid", "vessels_tracked", "last_received_at",
            "api_verified")
    return [{"field": k, "value": ", ".join(status.get(k) or []) if k == "reason_codes" else fmt(status.get(k))}
            for k in keys]


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not isinstance(ts, str) or not ts.endswith("Z"):
        return None
    body = ts[:-1]
    try:
        return datetime.strptime(body, "%Y-%m-%dT%H:%M:%S.%f" if "." in body else "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def bundle_overview(bundle: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    src = bundle.get("source") or {}
    usable = [r for r in bundle.get("routes") or [] if (r.get("validation") or {}).get("ok")]
    captured = _parse(src.get("captured_at"))
    age_h = ((now or datetime.utcnow()) - captured).total_seconds() / 3600 if captured else None
    return {"bundle_id": bundle.get("bundle_id"), "source_kind": src.get("kind"),
            "api_verified": bool(src.get("api_verified")), "fixture_only": bool(src.get("fixture_only", True)),
            "captured_at": src.get("captured_at"), "source_age_h": age_h,
            "capture_sha256": (src.get("capture_sha256") or "")[:12] or None,
            "routes_total": len(bundle.get("routes") or []), "routes_usable": len(usable),
            "issues": list(bundle.get("issues") or [])}


def route_rows(bundle: Dict[str, Any]) -> List[Dict[str, str]]:
    out = []
    for r in bundle.get("routes") or []:
        wps = r.get("waypoints") or []
        start, end = _parse(r.get("source_start_at")), _parse(r.get("source_end_at"))
        out.append({"route_id": r["route_id"], "source_mmsi": str(r.get("source_mmsi")),
                    "source time": f"{r.get('source_start_at')} - {r.get('source_end_at')}",
                    "span_s": fmt((end - start).total_seconds() if start and end else None),
                    "waypoints": str(len(wps)), "service_knots": fmt(r.get("service_speed_knots")),
                    "length x beam (m)": f"{fmt(r.get('length_m'))} x {fmt(r.get('beam_m'))}",
                    "assumed_fields": ", ".join(r.get("assumed_fields") or []) or "-",
                    "valid": "yes" if (r.get("validation") or {}).get("ok") else
                    "no: " + ", ".join((r.get("validation") or {}).get("reasons") or [])})
    return out


def _detections_by_id(result: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {d["detection_id"]: d for f in result.get("frames") or [] for d in f.get("whale_detections") or []}


def alert_rows(result: Dict[str, Any]) -> List[Dict[str, str]]:
    """One row per advisor action: command, rejoin, CPA/time, cost breakdown, evidence age."""
    dets = _detections_by_id(result)
    out = []
    for d in result.get("decisions") or []:
        for a in d.get("actions") or []:
            costs = (a.get("metadata") or {}).get("costs") or {}
            cpa = costs.get("cpa") or {}
            contrib = costs.get("contributions") or {}
            det = dets.get(cpa.get("detection_id") or "")
            issued, observed = _parse(a.get("issued_at")), _parse((det or {}).get("observed_at"))
            out.append({
                "issued_at": a.get("issued_at"), "ship_id": a.get("ship_id"), "command": a.get("command"),
                "revision": fmt(a.get("revision")), "rejoin_route_index": fmt(a.get("rejoin_route_index")),
                "target_speed_knots": fmt(a.get("target_speed_knots")),
                "detour_waypoints": "; ".join(f"{p['lat']:.4f},{p['lon']:.4f}" for p in a.get("detour_waypoints") or []) or "-",
                "valid_until": a.get("valid_until"),
                "triggers": ", ".join((a.get("metadata") or {}).get("triggers") or []),
                "whale_risk": fmt(costs.get("whale_risk")), "cpa_m": fmt(cpa.get("cpa_m")),
                "cpa_time_s": fmt(cpa.get("cpa_time_s")),
                "evidence_age_s": fmt((issued - observed).total_seconds() if issued and observed else None),
                "weather_exposure_s": fmt(costs.get("weather_exposure_s")),
                "added_delay_s": fmt(costs.get("added_delay_s")), "extra_distance_m": fmt(costs.get("extra_distance_m")),
                "objective": fmt(costs.get("objective")),
                "J parts (whale/weather/delay/distance/change)": " / ".join(
                    fmt(contrib.get(k), 3) for k in ("whale", "weather", "delay", "distance", "change")),
            })
    return out


def assessment_rows(result: Dict[str, Any], *, latest_only: bool = True) -> List[Dict[str, str]]:
    """Per-ship assessment status, reasons and hard-constraint rejections (latest per ship by default)."""
    rows: Dict[Any, Dict[str, str]] = {}
    seq = []
    for d in result.get("decisions") or []:
        for a in d.get("assessments") or []:
            rej = sorted({r for c in a.get("rejected_candidates") or [] for r in c.get("reasons") or []
                          if r != "NOT_SELECTED"})
            row = {"decision_id": d.get("decision_id"), "ship_id": a.get("ship_id"), "status": a.get("status"),
                   "reason_codes": ", ".join(a.get("reason_codes") or []) or "-",
                   "objective": fmt((a.get("costs") or {}).get("objective")),
                   "rejected_by": ", ".join(rej) or "-",
                   "candidates_evaluated": fmt((a.get("costs") or {}).get("candidates_evaluated"))}
            rows[a.get("ship_id")] = row
            seq.append(row)
    return list(rows.values()) if latest_only else seq


FLAGGED = (C.NO_FEASIBLE_ROUTE, C.INSUFFICIENT_FRESH_EVIDENCE, C.NOT_EVALUATED_BUDGET, C.INVALID_MAP, C.UNKNOWN_WEATHER)


def flagged_codes(rows: List[Dict[str, str]]) -> List[str]:
    """Failure/limitation codes present in assessment rows' status or reason codes."""
    seen = {r["status"] for r in rows} | {c.strip() for r in rows for c in r["reason_codes"].split(",")}
    return [c for c in FLAGGED if c in seen]


def status_counts(result: Dict[str, Any]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for d in result.get("decisions") or []:
        for a in d.get("assessments") or []:
            out[a.get("status")] = out.get(a.get("status"), 0) + 1
    return dict(sorted(out.items()))


def event_rows(result: Dict[str, Any], types: Optional[List[str]] = None) -> List[Dict[str, str]]:
    return [{"sim_time_s": fmt(e["sim_time_s"]), "type": e["type"], "ship_id": e.get("ship_id") or "-",
             "details": ", ".join(f"{k}={fmt(v) if not isinstance(v, (list, dict)) else v}"
                                  for k, v in sorted((e.get("details") or {}).items()))}
            for e in result.get("events") or [] if types is None or e["type"] in types]
