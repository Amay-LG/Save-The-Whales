"""Regional land map: preparation from public polygons and loading/validation into ``LandMap``.

Research geometry, not a navigation chart. Conservative steps are labelled in ``simplifications``:

- ``HOLES_FILLED_AS_LAND``: interior rings (lakes) are dropped, so the exterior ring counts as land.
- ``CLIPPED_TO_REGION_BBOX``: source polygons are clipped to the region rectangle. A polygon that
  crosses or encloses the region is kept even when none of its vertices lies inside it; the clip adds
  edges along the region boundary, which is a simulation limit anyway.

CLI (rebuild the committed subset from downloaded Natural Earth files)::

    python -m save_the_whales.sources.land --source ne_10m_land.geojson \
        --source ne_10m_minor_islands.geojson --version v5.1.2 --out data/land/santa_barbara.geojson
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from save_the_whales.shared import contracts as C
from save_the_whales.shared import geo

LonLat = Tuple[float, float]
_SELF_INTERSECTION_MAX_VERTICES = 5000


# =====================================================================================================
# GeoJSON helpers
# =====================================================================================================
def _region_bbox(config: Optional[Dict[str, Any]]) -> Dict[str, float]:
    config = config or {}
    return dict((config.get("ais") or {}).get("bbox") or config.get("bbox") or C.REGION_BBOX)


def iter_polygons(geojson: Dict[str, Any]) -> Iterable[List[List[List[float]]]]:
    """Yield polygon coordinate arrays ([exterior, *holes], each [[lon, lat], ...]) from any GeoJSON."""
    kind = geojson.get("type") if isinstance(geojson, dict) else None
    if kind == "FeatureCollection":
        for feat in geojson.get("features") or []:
            yield from iter_polygons(feat)
    elif kind == "Feature":
        if geojson.get("geometry"):
            yield from iter_polygons(geojson["geometry"])
    elif kind == "GeometryCollection":
        for g in geojson.get("geometries") or []:
            yield from iter_polygons(g)
    elif kind == "Polygon":
        yield geojson.get("coordinates") or []
    elif kind == "MultiPolygon":
        for poly in geojson.get("coordinates") or []:
            yield poly


def _clip_ring_to_bbox(ring: Sequence[LonLat], bbox: Dict[str, float]) -> List[LonLat]:
    """Sutherland-Hodgman clip of a ring (lon, lat) against the region rectangle."""
    def clip(points: List[LonLat], inside, cut) -> List[LonLat]:
        out: List[LonLat] = []
        for i, cur in enumerate(points):
            prev = points[i - 1]
            if inside(cur):
                if not inside(prev):
                    out.append(cut(prev, cur))
                out.append(cur)
            elif inside(prev):
                out.append(cut(prev, cur))
        return out

    def at_lon(lon):
        return lambda p, q: (lon, p[1] + (q[1] - p[1]) * (lon - p[0]) / (q[0] - p[0]))

    def at_lat(lat):
        return lambda p, q: (p[0] + (q[0] - p[0]) * (lat - p[1]) / (q[1] - p[1]), lat)

    pts = list(ring)
    for inside, cut in (
        (lambda p: p[0] >= bbox["lon_min"], at_lon(bbox["lon_min"])),
        (lambda p: p[0] <= bbox["lon_max"], at_lon(bbox["lon_max"])),
        (lambda p: p[1] >= bbox["lat_min"], at_lat(bbox["lat_min"])),
        (lambda p: p[1] <= bbox["lat_max"], at_lat(bbox["lat_max"])),
    ):
        if not pts:
            break
        pts = clip(pts, inside, cut)
    return pts


def _ring_area_deg2(ring: Sequence[LonLat]) -> float:
    return 0.5 * abs(sum(ring[i][0] * ring[(i + 1) % len(ring)][1] - ring[(i + 1) % len(ring)][0] * ring[i][1]
                         for i in range(len(ring))))


def _dedupe(ring: Sequence[LonLat]) -> List[LonLat]:
    out: List[LonLat] = []
    for p in ring:
        p = (round(float(p[0]), 7), round(float(p[1]), 7))
        if not out or p != out[-1]:
            out.append(p)
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    return out


def sha256_file(path: Any) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# =====================================================================================================
# Preparation (offline; produces data/land/*.geojson + provenance.json)
# =====================================================================================================
def prepare_land_subset(sources: Sequence[Tuple[Dict[str, Any], str]], bbox: Dict[str, float]
                        ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Clip polygons from ``[(geojson, source_name), ...]`` to ``bbox``.

    Returns ``(feature_collection, stats)``. Holes are filled; a polygon is kept when its clipped
    exterior has positive area, whether or not any original vertex lies inside the region.
    """
    features, holes_filled, kept_by_source, seen = [], 0, {}, set()
    for gj, name in sources:
        for poly in iter_polygons(gj):
            if not poly or not poly[0]:
                continue
            ext = [(float(x), float(y)) for x, y, *_ in poly[0]]
            lons, lats = [p[0] for p in ext], [p[1] for p in ext]
            if max(lons) < bbox["lon_min"] or min(lons) > bbox["lon_max"] or \
                    max(lats) < bbox["lat_min"] or min(lats) > bbox["lat_max"]:
                continue
            clipped = _dedupe(_clip_ring_to_bbox(_dedupe(ext), bbox))
            if len(clipped) < 3 or _ring_area_deg2(clipped) <= 1e-12:
                continue
            key = tuple(sorted(clipped))
            if key in seen:   # the same island can appear in several Natural Earth layers
                continue
            seen.add(key)
            holes_filled += max(0, len(poly) - 1)
            kept_by_source[name] = kept_by_source.get(name, 0) + 1
            coords = [[x, y] for x, y in clipped] + [[clipped[0][0], clipped[0][1]]]
            features.append({"type": "Feature", "properties": {"source": name},
                             "geometry": {"type": "Polygon", "coordinates": [coords]}})
    fc = {"type": "FeatureCollection", "features": features}
    stats = {"polygons_by_source": kept_by_source, "holes_filled": holes_filled,
             "vertices": sum(len(f["geometry"]["coordinates"][0]) - 1 for f in features)}
    return fc, stats


# =====================================================================================================
# Loading and validation
# =====================================================================================================
def _ring_self_intersects(ring_xy: Sequence[geo.XY]) -> bool:
    n = len(ring_xy)
    if n > _SELF_INTERSECTION_MAX_VERTICES:
        return False
    edges = [(ring_xy[i], ring_xy[(i + 1) % n]) for i in range(n)]
    boxes = [(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])) for a, b in edges]
    for i in range(n):
        ax0, ay0, ax1, ay1 = boxes[i]
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue   # adjacent through the closing vertex
            bx0, by0, bx1, by1 = boxes[j]
            if bx0 > ax1 or bx1 < ax0 or by0 > ay1 or by1 < ay0:
                continue
            if geo.segments_properly_cross(edges[i][0], edges[i][1], edges[j][0], edges[j][1]):
                return True
    return False


def _find_provenance(path: Path, gj: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Embedded ``provenance`` member (fixtures) or a sidecar ``provenance.json`` naming this file.

    Returns (provenance, is_sidecar).
    """
    if isinstance(gj.get("provenance"), dict):
        return gj["provenance"], False
    for cand in (path.with_name(path.stem + ".provenance.json"), path.with_name("provenance.json")):
        if cand.is_file():
            try:
                prov = json.loads(cand.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if isinstance(prov, dict) and prov.get("file") in (path.name, None):
                return prov, True
    return None, False


def _bbox_covers(outer: Dict[str, float], inner: Dict[str, float]) -> bool:
    return (outer["lat_min"] <= inner["lat_min"] and outer["lat_max"] >= inner["lat_max"]
            and outer["lon_min"] <= inner["lon_min"] and outer["lon_max"] >= inner["lon_max"])


def load_land(path: Any, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Load a land GeoJSON into the shared ``LandMap``; never raises for bad input.

    ``validated`` is True only for a nonempty map whose rings are all valid (>= 3 distinct finite
    vertices, positive area, no self-crossing), whose provenance is present (and, for a sidecar, whose
    recorded sha256 matches the file), and whose declared coverage contains the region bbox. Otherwise
    ``validated=False`` and ``issues`` contains ``INVALID_MAP`` plus specific codes. ``fixture_only``
    comes from provenance and defaults to True when undeclared.
    """
    path = Path(path)
    region = _region_bbox(config)
    result: Dict[str, Any] = {
        "map_id": path.stem, "source": "unknown", "sha256": None, "validated": False,
        "fixture_only": True, "bbox": region, "rings": [], "issues": [], "simplifications": [],
        "provenance": {}, "geojson": {},
    }

    def fail(*codes: str) -> Dict[str, Any]:
        result["issues"] = sorted(set(result["issues"]) | set(codes) | {C.INVALID_MAP})
        result["validated"] = False
        return result

    if not path.is_file():
        return fail(C.MAP_FILE_MISSING)
    raw = path.read_bytes()
    result["sha256"] = hashlib.sha256(raw).hexdigest()
    try:
        gj = json.loads(raw.decode("utf-8")) if raw.strip() else None
    except (ValueError, UnicodeDecodeError):
        gj = None
    if not isinstance(gj, dict):
        return fail(C.MAP_EMPTY if not raw.strip() else C.INVALID_MAP)
    result["geojson"] = gj

    prov, is_sidecar = _find_provenance(path, gj)
    if prov:
        result["provenance"] = prov
        result["source"] = str(prov.get("source") or "unknown")
        result["map_id"] = str(prov.get("map_id") or f"{path.stem}-{result['sha256'][:12]}")
        result["fixture_only"] = bool(prov.get("fixture_only", True))
        result["simplifications"] = list(prov.get("simplifications") or [])
        if isinstance(prov.get("bbox"), dict):
            result["bbox"] = dict(prov["bbox"])
    issues: List[str] = []
    if not prov or not prov.get("source"):
        issues.append(C.MAP_PROVENANCE_MISSING)
    elif is_sidecar and prov.get("sha256") != result["sha256"]:
        issues.append(C.MAP_HASH_MISMATCH)

    rings: List[List[Dict[str, float]]] = []
    holes = 0
    for poly in iter_polygons(gj):
        if not poly or not isinstance(poly[0], list):
            issues.append(C.INVALID_RING)
            continue
        holes += max(0, len(poly) - 1)
        try:
            ext = _dedupe([(float(p[0]), float(p[1])) for p in poly[0]])
        except (TypeError, ValueError, IndexError):
            issues.append(C.INVALID_RING)
            continue
        if len(ext) < 3 or not all(math.isfinite(x) and math.isfinite(y) and -180 <= x <= 180 and -90 <= y <= 90
                                   for x, y in ext):
            issues.append(C.INVALID_RING)
            continue
        ring = [{"lat": y, "lon": x} for x, y in ext]
        ring_xy = geo.normalize_ring_xy([geo.project(p) for p in ring])
        if len(ring_xy) < 3 or _ring_area_deg2(ext) <= 1e-12 or _ring_self_intersects(ring_xy):
            issues.append(C.INVALID_RING)
            continue
        rings.append(ring)
    if holes and C.HOLES_FILLED_AS_LAND not in result["simplifications"]:
        result["simplifications"].append(C.HOLES_FILLED_AS_LAND)
    result["rings"] = rings
    if not rings:
        issues.append(C.MAP_EMPTY)
    if not _bbox_covers(result["bbox"], region):
        issues.append("MAP_COVERAGE_INCOMPLETE")
    if issues:
        result["issues"] = sorted(set(issues))
        return fail()
    result["validated"] = True
    result["issues"] = []
    return result


# =====================================================================================================
# CLI
# =====================================================================================================
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Clip public land polygons to the research region.")
    ap.add_argument("--source", action="append", required=True, help="input GeoJSON (repeatable)")
    ap.add_argument("--version", required=True, help="source release tag, e.g. v5.1.2")
    ap.add_argument("--url", action="append", default=[], help="download URL per --source (optional)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    bbox = dict(C.REGION_BBOX)
    loaded = []
    for src in args.source:
        loaded.append((json.loads(Path(src).read_text(encoding="utf-8")), Path(src).stem))
    fc, stats = prepare_land_subset(loaded, bbox)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fc, separators=(",", ":")) + "\n", encoding="utf-8")
    prov = {
        "file": out.name,
        "map_id": f"santa-barbara-ne10m-{args.version}",
        "source": f"Natural Earth 10m physical vectors ({', '.join(Path(s).stem for s in args.source)})",
        "version": args.version,
        "urls": args.url,
        "license": "Public domain (Natural Earth terms of use)",
        "resolution": "1:10,000,000 generalized coastline; expect positional error of several hundred meters",
        "bbox": bbox,
        "simplifications": [C.CLIPPED_TO_REGION_BBOX] + ([C.HOLES_FILLED_AS_LAND] if stats["holes_filled"] else []),
        "input_sha256": {Path(s).name: sha256_file(s) for s in args.source},
        "stats": stats,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sha256": sha256_file(out),
        "fixture_only": False,
        "research_only": True,
        "note": "Research geometry for simulation, not a navigation chart.",
        "visual_check": {"coast": "PENDING", "routes": "PENDING",
                         "routes_note": "Overlay against AIS routes before marking ready."},
    }
    out.with_name("provenance.json").write_text(json.dumps(prov, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(out), **stats}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
