"""Observed AIS tracks -> immutable baseline route bundle (Architecture §4.3).

Pipeline per vessel (``prepare_routes``):

1. Keep valid in-region POSITION fixes; sort by observation time, then receipt order; drop duplicates.
2. Split at gaps > ``gap_split_s``, implausible jumps (> ``max_implied_speed_knots``) and any chord that
   crosses or touches land. Missing track sections are never assumed straight and navigable.
3. Trim stretches that violate the modeled hull clearance (repair by trimming, never by inventing).
4. Cut into clips of at most ``preferred_clip_s``; require ``min_fixes`` distinct positions,
   ``min_span_s`` and ``min_travel_m``.
5. Simplify with Douglas-Peucker (``simplify_tolerance_m``), halving the tolerance until every
   simplified leg passes the hull-clearance check; trim the clip if it still exceeds
   ``max_route_vertices`` instead of dropping bends.
6. Derive service speed from the median valid SOG and keep dimensions from static messages; every
   defaulted field is listed in ``assumed_fields``.

Only usable routes (``validation.ok``) enter a prepared bundle; an empty result carries
``AIS_ROUTE_UNAVAILABLE``. ``load_route_bundle`` rechecks shape, checksums and geometry against the
supplied land map and keeps failing routes visible with ``validation.ok=false``.

CLI::

    python -m save_the_whales.sources.routes --capture data/ais/capture_a.jsonl \
        --capture data/ais/capture_b.jsonl --land data/land/santa_barbara.geojson \
        --out data/ais/route_bundle.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from save_the_whales.shared import contracts as C
from save_the_whales.shared import geo
from save_the_whales.sources.ais import ADAPTER_ID, load_capture, parse_utc
from save_the_whales.sources.land import load_land

COMMERCIAL_SHIP_TYPES = range(60, 90)   # passenger, cargo, tanker
DETOUR_DEMO_RANGE_M = (2000.0, 5000.0)


# =====================================================================================================
# Config
# =====================================================================================================
def _route_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Route-preparation settings from ``config['ais']``.

    Hull-clearance margin and minimum speed are planner constraints. Use ``config['planner']`` when
    present; ``config['ais']`` carries the same agreed values until then, and a disagreement raises.
    """
    ais = (config or {}).get("ais")
    if not isinstance(ais, dict):
        raise KeyError("config['ais'] is required (see config/default.json)")
    out = dict(ais)
    planner = (config or {}).get("planner") or {}
    for key in ("clearance_margin_m", "min_speed_knots"):
        if key in planner:
            if key in ais and float(ais[key]) != float(planner[key]):
                raise ValueError(f"config ais.{key} ({ais[key]}) disagrees with planner.{key} ({planner[key]})")
            out[key] = planner[key]
        elif key not in out:
            raise KeyError(f"config is missing {key} (planner.{key} or ais.{key})")
    return out


# =====================================================================================================
# Helpers
# =====================================================================================================
def _canonical_sha256(items: Sequence[Dict[str, Any]]) -> str:
    h = hashlib.sha256()
    for item in items:
        h.update(json.dumps(item, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def _record_sort_key(r: Dict[str, Any]) -> tuple:
    return (r.get("mmsi") or "", r.get("observed_at") or "", int(r.get("receipt_seq") or 0))


def capture_sha256(records: Sequence[Dict[str, Any]]) -> str:
    """Order-independent checksum of normalized capture records."""
    return _canonical_sha256(sorted(records, key=_record_sort_key))


def _track_point(fix: Dict[str, Any]) -> Dict[str, Any]:
    return {"lat": fix["lat"], "lon": fix["lon"], "observed_at": fix["observed_at"],
            "sog_knots": fix.get("sog_knots")}


def track_sha256(track: Sequence[Dict[str, Any]]) -> str:
    return _canonical_sha256(list(track))


def _xy(fix: Dict[str, Any]) -> geo.XY:
    return geo.project(fix)


def _dist(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    (ax, ay), (bx, by) = _xy(a), _xy(b)
    return math.hypot(bx - ax, by - ay)


def _douglas_peucker(points: Sequence[geo.XY], tol: float) -> List[int]:
    """Indices kept by Douglas-Peucker (endpoints always kept)."""
    n = len(points)
    if n <= 2 or tol <= 0:
        return list(range(n))
    keep = {0, n - 1}
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        best, idx = -1.0, None
        for k in range(i + 1, j):
            d = geo.point_segment_distance(points[k], points[i], points[j])
            if d > best:
                best, idx = d, k
        if idx is not None and best > tol:
            keep.add(idx)
            stack.extend([(i, idx), (idx, j)])
    return sorted(keep)


def _is_land_chord(a: Dict[str, Any], b: Dict[str, Any], land: Dict[str, Any]) -> bool:
    res = geo.check_water_path([_pt(a), _pt(b)], land, 0.0)
    return any(r in res["reasons"] for r in (C.POINT_ON_LAND, C.LAND_CROSSING, C.LAND_CONTACT, C.INVALID_MAP))


def _pt(fix: Dict[str, Any]) -> Dict[str, float]:
    return {"lat": float(fix["lat"]), "lon": float(fix["lon"])}


def _clean_fixes(records: Sequence[Dict[str, Any]], bbox: Dict[str, float]) -> Tuple[Dict[str, list], Dict[str, int]]:
    """Valid POSITION fixes grouped by MMSI, sorted and deduplicated."""
    stats = {"invalid_fixes": 0, "duplicate_fixes": 0}
    by_mmsi: Dict[str, list] = {}
    for r in records:
        if r.get("record_type", "POSITION") != "POSITION":
            continue
        lat, lon, t = r.get("lat"), r.get("lon"), parse_utc(r.get("observed_at"))
        ok = (isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and math.isfinite(lat)
              and math.isfinite(lon) and bbox["lat_min"] <= lat <= bbox["lat_max"]
              and bbox["lon_min"] <= lon <= bbox["lon_max"] and t is not None and r.get("mmsi"))
        if not ok:
            stats["invalid_fixes"] += 1
            continue
        fix = dict(r)
        fix["_t"] = t.timestamp()
        by_mmsi.setdefault(str(r["mmsi"]), []).append(fix)
    for mmsi, fixes in by_mmsi.items():
        fixes.sort(key=lambda f: (f["_t"], int(f.get("receipt_seq") or 0)))
        out = []
        for f in fixes:
            if out and abs(f["_t"] - out[-1]["_t"]) < 1e-6:
                stats["duplicate_fixes"] += 1   # same observation time: keep the first received
                continue
            out.append(f)
        by_mmsi[mmsi] = out
    return by_mmsi, stats


def _split_segments(fixes: list, land: Dict[str, Any], cfg: Dict[str, Any], stats: Dict[str, int]) -> List[list]:
    """Split a vessel's fixes at gaps, implausible jumps and land-crossing chords."""
    max_mps = geo.knots_to_mps(float(cfg["max_implied_speed_knots"]))
    segments, cur = [], []
    for f in fixes:
        if cur:
            prev = cur[-1]
            dt = f["_t"] - prev["_t"]
            d = _dist(prev, f)
            if dt > float(cfg["gap_split_s"]):
                stats["gap_splits"] = stats.get("gap_splits", 0) + 1
                segments.append(cur)
                cur = []
            elif dt <= 0 or d / dt > max_mps:
                stats["jump_splits"] = stats.get("jump_splits", 0) + 1
                segments.append(cur)
                cur = []
            elif d > geo.EPS_M and _is_land_chord(prev, f, land):
                stats["land_chord_splits"] = stats.get("land_chord_splits", 0) + 1
                segments.append(cur)
                cur = []
        cur.append(f)
    if cur:
        segments.append(cur)
    return segments


def _split_by_clearance(seg: list, land: Dict[str, Any], clearance: float, stats: Dict[str, int]) -> List[list]:
    """Trim stretches whose chords come within the hull clearance of land."""
    runs, cur = [], []
    for f in seg:
        if cur:
            res = geo.check_water_path([_pt(cur[-1]), _pt(f)], land, clearance)
            if not res["ok"]:
                stats["clearance_trims"] = stats.get("clearance_trims", 0) + 1
                runs.append(cur)
                cur = []
                if not geo.check_water_path([_pt(f)], land, clearance)["ok"]:
                    continue
        elif not geo.check_water_path([_pt(f)], land, clearance)["ok"]:
            continue
        cur.append(f)
    if cur:
        runs.append(cur)
    return runs


def _trim_stationary(seg: list, moving_sog: float) -> list:
    def moving(f):
        s = f.get("sog_knots")
        return s is None or s >= moving_sog
    i, j = 0, len(seg)
    while i < j and not moving(seg[i]):
        i += 1
    while j > i and not moving(seg[j - 1]):
        j -= 1
    return seg[i:j]


def _clips(seg: list, window_s: float) -> List[list]:
    out, cur = [], []
    for f in seg:
        if cur and f["_t"] - cur[0]["_t"] > window_s:
            out.append(cur)
            cur = [cur[-1]] if f["_t"] - cur[-1]["_t"] <= window_s else []
        cur.append(f)
    if cur:
        out.append(cur)
    return out


def _clip_requirements(clip: list, cfg: Dict[str, Any]) -> List[str]:
    distinct = {(round(f["lat"], 6), round(f["lon"], 6)) for f in clip}
    reasons = []
    if len(distinct) < int(cfg["min_fixes"]):
        reasons.append(C.TOO_FEW_FIXES)
    if not clip or clip[-1]["_t"] - clip[0]["_t"] < float(cfg["min_span_s"]):
        reasons.append(C.TOO_SHORT_DURATION)
    if sum(_dist(a, b) for a, b in zip(clip, clip[1:])) < float(cfg["min_travel_m"]):
        reasons.append(C.TOO_SHORT_DISTANCE)
    return reasons


def _simplify_valid(clip: list, land: Dict[str, Any], clearance: float, cfg: Dict[str, Any]
                    ) -> Tuple[Optional[List[int]], float]:
    """Simplified vertex indices whose every leg passes clearance; halve tolerance as needed."""
    pts = [_xy(f) for f in clip]
    tol = float(cfg["simplify_tolerance_m"])
    while True:
        idx = _douglas_peucker(pts, tol)
        if geo.check_water_path([_pt(clip[i]) for i in idx], land, clearance)["ok"]:
            return idx, tol
        if tol <= 0.5:
            idx = list(range(len(clip)))
            ok = geo.check_water_path([_pt(f) for f in clip], land, clearance)["ok"]
            return (idx, 0.0) if ok else (None, 0.0)
        tol /= 2.0


def _vessel_static(records: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    info: Dict[str, Dict[str, Any]] = {}
    for r in sorted(records, key=lambda r: int(r.get("receipt_seq") or 0)):
        d = info.setdefault(str(r.get("mmsi")), {})
        for key in ("length_m", "beam_m", "ship_type", "ship_name"):
            if r.get(key) not in (None, ""):
                d[key] = r[key]
    return info


def _iso_compact(ts: str) -> str:
    return ts.replace("-", "").replace(":", "").split(".")[0].rstrip("Z") + "Z"


def _build_route(mmsi: str, clip: list, idx: List[int], tol: float, static: Dict[str, Any],
                 cfg: Dict[str, Any]) -> Dict[str, Any]:
    assumed = []
    length = static.get("length_m")
    beam = static.get("beam_m")
    if not length:
        length = float(cfg["default_length_m"])
        assumed.append("length_m")
    if not beam:
        beam = float(cfg["default_beam_m"])
        assumed.append("beam_m")
    sogs = [f["sog_knots"] for f in clip if isinstance(f.get("sog_knots"), (int, float))
            and f["sog_knots"] >= float(cfg["moving_sog_knots"])]
    span = clip[-1]["_t"] - clip[0]["_t"]
    travel = sum(_dist(a, b) for a, b in zip(clip, clip[1:]))
    if sogs:
        service = statistics.median(sogs)
    else:
        service = geo.mps_to_knots(travel / span) if span > 0 else 0.0
        assumed.append("service_speed_knots")
    reasons = []
    if service < float(cfg["min_speed_knots"]):
        reasons.append(C.SERVICE_SPEED_BELOW_MIN)
    waypoints = [_pt(clip[i]) for i in idx]
    baseline_len = geo.path_length_m(waypoints)
    track = [_track_point(f) for f in clip]
    return {
        "route_id": f"ais-{mmsi}-{_iso_compact(clip[0]['observed_at'])}",
        "source_mmsi": mmsi,
        "source_start_at": clip[0]["observed_at"],
        "source_end_at": clip[-1]["observed_at"],
        "waypoints": waypoints,
        "service_speed_knots": round(float(service), 2),
        "length_m": float(length),
        "beam_m": float(beam),
        "assumed_fields": assumed,
        "validation": {"ok": not reasons, "reasons": reasons},
        "source_track": track,
        "source_track_sha256": track_sha256(track),
        "simplification_tolerance_m": tol,
        "baseline_length_m": round(baseline_len, 1),
        "source_duration_s": round(span, 1),
        "baseline_duration_s": round(baseline_len / geo.knots_to_mps(service), 1) if service > 0 else None,
        "source_ship_type": static.get("ship_type"),
    }


def _select(routes: List[Dict[str, Any]], max_routes: int) -> List[Dict[str, Any]]:
    """Up to ``max_routes`` routes: one per vessel first, detour-length and commercial ships preferred."""
    lo, hi = DETOUR_DEMO_RANGE_M

    def score(r):
        length = r["baseline_length_m"]
        return (lo <= length <= hi, r.get("source_ship_type") in COMMERCIAL_SHIP_TYPES,
                not ({"length_m", "beam_m"} & set(r["assumed_fields"])), min(length, hi))

    ranked = sorted(routes, key=score, reverse=True)
    chosen, seen = [], set()
    for r in ranked:
        if r["source_mmsi"] not in seen:
            chosen.append(r)
            seen.add(r["source_mmsi"])
    for r in ranked:
        if r not in chosen:
            chosen.append(r)
    return chosen[:max_routes]


# =====================================================================================================
# Public API
# =====================================================================================================
def prepare_routes(records: Sequence[Dict[str, Any]], land: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """Clean normalized AIS records into a ``RouteBundle`` of 1-5 validated observed segments."""
    cfg = _route_config(config)
    bbox = dict(cfg.get("bbox") or C.REGION_BBOX)
    records = list(records)
    positions = [r for r in records if r.get("record_type", "POSITION") == "POSITION"]
    is_api = bool(positions) and all(r.get("provider") == C.PROVIDER_AISSTREAM for r in records)
    kind = C.AISSTREAM_CAPTURE if is_api else C.SYNTHETIC_FIXTURE
    api_verified = is_api and all(r.get("adapter") == ADAPTER_ID for r in positions)
    received = [r.get("received_at") for r in records if r.get("received_at")]
    cap_sha = capture_sha256(records)
    issues: List[str] = []
    stats: Dict[str, Any] = {"records": len(records), "position_records": len(positions)}

    if not land or not land.get("validated"):
        issues.append(C.INVALID_MAP)
    elif land.get("fixture_only"):
        issues.append(C.VALIDATED_AGAINST_FIXTURE_MAP)

    routes: List[Dict[str, Any]] = []
    rejected: Dict[str, int] = {}
    if C.INVALID_MAP not in issues:
        by_mmsi, clean_stats = _clean_fixes(records, bbox)
        stats.update(clean_stats, vessels=len(by_mmsi))
        static = _vessel_static(records)
        for mmsi, fixes in by_mmsi.items():
            info = static.get(mmsi, {})
            clearance = geo.ship_clearance_m(float(info.get("length_m") or cfg["default_length_m"]),
                                             float(info.get("beam_m") or cfg["default_beam_m"]),
                                             float(cfg["clearance_margin_m"]))
            for seg in _split_segments(fixes, land, cfg, stats):
                for run in _split_by_clearance(seg, land, clearance, stats):
                    run = _trim_stationary(run, float(cfg["moving_sog_knots"]))
                    for clip in _clips(run, float(cfg["preferred_clip_s"])):
                        why = _clip_requirements(clip, cfg)
                        if why:
                            for w in why:
                                rejected[w] = rejected.get(w, 0) + 1
                            continue
                        idx, tol = _simplify_valid(clip, land, clearance, cfg)
                        if idx is None:
                            rejected[C.INSUFFICIENT_CLEARANCE] = rejected.get(C.INSUFFICIENT_CLEARANCE, 0) + 1
                            continue
                        max_v = int(cfg["max_route_vertices"])
                        if len(idx) > max_v:   # trim the clip rather than dropping bends
                            clip = clip[: idx[max_v - 1] + 1]
                            idx = idx[:max_v]
                            rejected[C.VERTEX_BUDGET_EXCEEDED + "_TRIMMED"] = \
                                rejected.get(C.VERTEX_BUDGET_EXCEEDED + "_TRIMMED", 0) + 1
                            if _clip_requirements(clip, cfg):
                                continue
                        route = _build_route(mmsi, clip, idx, tol, info, cfg)
                        if route["validation"]["ok"]:
                            routes.append(route)
                        else:
                            for w in route["validation"]["reasons"]:
                                rejected[w] = rejected.get(w, 0) + 1
    stats["clips_rejected"] = rejected
    stats["usable_clips"] = len(routes)
    selected = _select(routes, int(cfg["max_routes"]))
    if not selected:
        issues.append(C.AIS_ROUTE_UNAVAILABLE)
    stats["routes_selected"] = len(selected)
    return {
        "schema_version": C.SCHEMA_VERSION,
        "bundle_id": f"bundle-{cap_sha[:12]}",
        "bbox": bbox,
        "source": {
            "kind": kind,
            "api_verified": api_verified,
            "captured_at": max(received) if received else None,
            "capture_sha256": cap_sha,
            "fixture_only": kind == C.SYNTHETIC_FIXTURE,
        },
        "routes": selected,
        "issues": sorted(set(issues)),
        "preparation_stats": stats,
    }


def save_route_bundle(bundle: Dict[str, Any], path: Any) -> None:
    """Write the bundle as JSON (atomic replace). Bundles hold no credentials or raw payloads."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(bundle, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _route_shape_ok(r: Any) -> bool:
    keys = ("route_id", "source_mmsi", "source_start_at", "source_end_at", "waypoints",
            "service_speed_knots", "length_m", "beam_m", "assumed_fields", "validation")
    return isinstance(r, dict) and all(k in r for k in keys) and isinstance(r["waypoints"], list)


def load_route_bundle(path: Any, land: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """Load a saved bundle and recheck shape, provenance checksums and geometry on ``land``.

    Never raises for bad input: unreadable/malformed files return an empty bundle with
    ``INVALID_BUNDLE`` and ``AIS_ROUTE_UNAVAILABLE``. Each route's ``validation`` is recomputed;
    failing routes stay visible with ``ok=false``.
    """
    cfg = _route_config(config)
    empty = {"schema_version": C.SCHEMA_VERSION, "bundle_id": "invalid", "bbox": dict(cfg.get("bbox") or C.REGION_BBOX),
             "source": {"kind": C.SYNTHETIC_FIXTURE, "api_verified": False, "captured_at": None,
                        "capture_sha256": None, "fixture_only": True},
             "routes": [], "issues": [C.INVALID_BUNDLE, C.AIS_ROUTE_UNAVAILABLE]}
    try:
        bundle = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(bundle, dict) or not isinstance(bundle.get("routes"), list) \
            or not isinstance(bundle.get("source"), dict):
        return empty
    issues = set(i for i in bundle.get("issues") or [] if i not in (C.AIS_ROUTE_UNAVAILABLE, C.INVALID_MAP,
                                                                    C.VALIDATED_AGAINST_FIXTURE_MAP))
    if bundle.get("schema_version") != C.SCHEMA_VERSION:
        issues.add(C.SCHEMA_VERSION_MISMATCH)
    if not land or not land.get("validated"):
        issues.add(C.INVALID_MAP)
    elif land.get("fixture_only"):
        issues.add(C.VALIDATED_AGAINST_FIXTURE_MAP)
    source = dict(bundle["source"])
    if source.get("kind") not in C.SOURCE_KINDS:
        source["kind"] = C.SYNTHETIC_FIXTURE
    if source["kind"] == C.SYNTHETIC_FIXTURE:
        source["fixture_only"], source["api_verified"] = True, False

    routes = []
    for r in bundle["routes"]:
        if not _route_shape_ok(r):
            issues.add(C.INVALID_BUNDLE)
            continue
        r = json.loads(json.dumps(r))   # private copy
        reasons: List[str] = []
        if C.SCHEMA_VERSION_MISMATCH in issues:
            reasons.append(C.SCHEMA_VERSION_MISMATCH)
        wps = r["waypoints"]
        if len(wps) < 2 or not all(geo.is_finite_point(p) for p in wps):
            reasons.append(C.INVALID_BASELINE_ROUTE)
        if "source_track" in r and r.get("source_track_sha256") != track_sha256(r["source_track"]):
            reasons.append(C.SOURCE_TRACK_CHECKSUM_MISMATCH)
        if not (isinstance(r["service_speed_knots"], (int, float)) and r["service_speed_knots"] >= float(cfg["min_speed_knots"])):
            reasons.append(C.SERVICE_SPEED_BELOW_MIN)
        if C.INVALID_BASELINE_ROUTE not in reasons:
            clearance = geo.ship_clearance_m(float(r["length_m"]), float(r["beam_m"]), float(cfg["clearance_margin_m"]))
            reasons.extend(geo.check_water_path(wps, land, clearance)["reasons"])
        if reasons:
            reasons = sorted(set(reasons) | {C.INVALID_BASELINE_ROUTE})
        r["validation"] = {"ok": not reasons, "reasons": reasons}
        routes.append(r)
    if not any(r["validation"]["ok"] for r in routes):
        issues.add(C.AIS_ROUTE_UNAVAILABLE)
    out = dict(bundle, source=source, routes=routes, issues=sorted(issues))
    return out


# =====================================================================================================
# CLI
# =====================================================================================================
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Prepare an AIS route bundle from capture JSONL files.")
    ap.add_argument("--capture", action="append", required=True, help="capture JSONL (repeatable; merged)")
    ap.add_argument("--land", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", default=str(Path(__file__).resolve().parents[2] / "config" / "default.json"))
    args = ap.parse_args(argv)
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    land = load_land(args.land, config)
    records = [r for path in args.capture for r in load_capture(path)]
    bundle = prepare_routes(records, land, config)
    save_route_bundle(bundle, args.out)
    summary = {
        "bundle_id": bundle["bundle_id"], "issues": bundle["issues"], "source": bundle["source"],
        "stats": bundle["preparation_stats"],
        "routes": [dict({k: r[k] for k in ("route_id", "service_speed_knots", "baseline_length_m",
                                            "source_duration_s", "assumed_fields")},
                        vertices=len(r["waypoints"])) for r in bundle["routes"]],
    }
    print(json.dumps(summary, indent=1))
    return 0 if bundle["routes"] else 1


if __name__ == "__main__":
    sys.exit(main())
