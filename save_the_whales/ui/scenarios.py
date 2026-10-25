"""Scenario presets and ``build_run_inputs``: resolve route IDs, voyages, whales and simulated weather.

``build_run_inputs(bundle, land, scenario_settings, config) -> RunInputs`` turns an editable preset
(``config/scenarios/*.json``) into the frozen inputs Person 2's ``run_scenario`` consumes. Placement is
relative to the selected AIS-derived route, so the same preset works on any route bundle:

    {"preset_id": "crossing", "label": str, "description": str, "seed": 0, "duration_s": 7200,
     "route_ids": [] | ["<route_id>", ...],     # empty = every usable route, round-robin
     "voyages": 1, "voyage_spacing_s": 600,      # entry at i * spacing (virtual seconds)
     "designated_whale": null | {"at_fraction": 0.5, "lateral_m": 0, "speed_mps": 0,
                                 "heading_rel_deg": 90, "exposure": "ALWAYS" | "CYCLE", "species": str},
     "background_whales": 0,                     # seeded random walkers around the selected routes
     "weather": {"preset": "CLEAR" | "STORM", "zones": [zone, ...]},
     "land_override": null | "<repo-relative fixture land path>",
     "fixture_only": false}

A zone is route-relative (``{"kind": "ROUTE_RELATIVE", "from_fraction", "to_fraction", "side":
"LEFT" | "RIGHT" | "ACROSS", "near_m", "far_m", ...}``; LEFT/RIGHT are relative to travel direction) or
an absolute ``{"kind": "RECT", "lat_min", "lat_max", "lon_min", "lon_max", ...}``; both carry
``zone_id``, ``severity`` in [0, 1], ``active_from_s``, ``active_until_s`` (null = coverage end) and
``blocked``. Weather coverage spans the scenario, the bounded drain and the planner's local forecast,
so in-run weather is never unknown by accident.

Data readiness (no usable routes, invalid map) is not raised here: the inputs are still built and
``run_scenario`` reports the failure codes. ``ScenarioError`` is only for settings that can't be resolved
(unknown preset field values, unknown route IDs, too many storm zones).
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from save_the_whales.shared import contracts as C
from save_the_whales.shared import geo

REPO_ROOT = Path(__file__).resolve().parents[2]
PRESET_NAMES = ("crossing", "no_threat", "storm_detour", "land_blocked_side")
SIDES = ("LEFT", "RIGHT", "ACROSS")
UNKNOWN_ROUTE_ID = "UNKNOWN_ROUTE_ID"
BAD_SETTING = "BAD_SETTING"
TOO_MANY_STORM_ZONES = "TOO_MANY_STORM_ZONES"
WHALE_START_ON_LAND = "WHALE_START_ON_LAND"


class ScenarioError(ValueError):
    """Unresolvable scenario settings; ``codes`` are machine-readable."""

    def __init__(self, codes: List[str]):
        super().__init__(", ".join(codes))
        self.codes = list(codes)


def _canonical(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# --- presets -------------------------------------------------------------------------------------
def presets_dir(config: Dict[str, Any]) -> Path:
    d = Path((config.get("scenarios") or {}).get("presets_dir", "config/scenarios"))
    return d if d.is_absolute() else REPO_ROOT / d


def load_presets(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """``{preset_id: settings}`` for every ``*.json`` preset, sorted by file name."""
    out: Dict[str, Dict[str, Any]] = {}
    for path in sorted(presets_dir(config).glob("*.json")):
        p = json.loads(path.read_text(encoding="utf-8"))
        out[str(p.get("preset_id", path.stem))] = p
    return out


def land_override_path(settings: Dict[str, Any]) -> Optional[Path]:
    """The preset's fixture land map, if it needs one (``land_blocked_side`` uses a toy obstacle)."""
    rel = settings.get("land_override")
    if not rel:
        return None
    p = Path(rel)
    return p if p.is_absolute() else REPO_ROOT / p


# --- route-relative geometry ---------------------------------------------------------------------
def usable_routes(bundle: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Routes that passed source validation (same rule as the simulator), in bundle order."""
    return [r for r in bundle.get("routes") or []
            if isinstance(r, dict) and (r.get("validation") or {}).get("ok") and len(r.get("waypoints") or []) >= 2]


def _route_xy(route: Dict[str, Any]) -> Tuple[List[Tuple[float, float]], List[float]]:
    xy = [geo.project(p) for p in route["waypoints"]]
    arcs = [0.0]
    for a, b in zip(xy, xy[1:]):
        arcs.append(arcs[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    return xy, arcs


def route_frame(route: Dict[str, Any], fraction: float) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    """Projected point and unit travel direction at ``fraction`` of the route's length."""
    xy, arcs = _route_xy(route)
    s = max(0.0, min(1.0, float(fraction))) * arcs[-1]
    for i in range(len(xy) - 1):
        seg = arcs[i + 1] - arcs[i]
        if seg > 0 and (s <= arcs[i + 1] or i == len(xy) - 2):
            f = (s - arcs[i]) / seg
            a, b = xy[i], xy[i + 1]
            return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f), ((b[0] - a[0]) / seg, (b[1] - a[1]) / seg)
    return xy[0], (1.0, 0.0)


def _left(t: Tuple[float, float]) -> Tuple[float, float]:
    return -t[1], t[0]                       # left normal of a travel direction


def route_offset_point(route: Dict[str, Any], fraction: float, lateral_m: float) -> Dict[str, float]:
    (x, y), t = route_frame(route, fraction)
    n = _left(t)
    return geo.unproject(x + n[0] * lateral_m, y + n[1] * lateral_m)


def zone_polygon(route: Optional[Dict[str, Any]], z: Dict[str, Any]) -> List[Dict[str, float]]:
    """Closed-ring-free polygon (4 corners) for a RECT or ROUTE_RELATIVE zone spec."""
    kind = z.get("kind", "ROUTE_RELATIVE")
    if kind == "RECT":
        a, b, c, d = (float(z[k]) for k in ("lat_min", "lat_max", "lon_min", "lon_max"))
        return [{"lat": a, "lon": c}, {"lat": a, "lon": d}, {"lat": b, "lon": d}, {"lat": b, "lon": c}]
    if kind != "ROUTE_RELATIVE" or route is None:
        raise ScenarioError([f"{BAD_SETTING}:zone.kind"])
    side = z.get("side", "ACROSS")
    if side not in SIDES:
        raise ScenarioError([f"{BAD_SETTING}:zone.side"])
    near, far = float(z.get("near_m", 0.0)), float(z["far_m"])
    lo, hi = {"LEFT": (near, far), "RIGHT": (-far, -near), "ACROSS": (-far, far)}[side]
    (p0, _), (p1, _) = route_frame(route, z["from_fraction"]), route_frame(route, z["to_fraction"])
    L = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
    if L <= 0:
        raise ScenarioError([f"{BAD_SETTING}:zone.fraction"])
    n = _left(((p1[0] - p0[0]) / L, (p1[1] - p0[1]) / L))   # chord-aligned rectangle
    corners = [(p0, lo), (p1, lo), (p1, hi), (p0, hi)]
    return [geo.unproject(p[0] + n[0] * o, p[1] + n[1] * o) for p, o in corners]


# --- assembly ------------------------------------------------------------------------------------
def weather_field(settings: Dict[str, Any], route: Optional[Dict[str, Any]], coverage_end_s: float,
                  max_zones: int) -> Dict[str, Any]:
    w = settings.get("weather") or {"preset": "CLEAR"}
    preset = w.get("preset", "CLEAR")
    if preset not in ("CLEAR", "STORM"):
        raise ScenarioError([f"{BAD_SETTING}:weather.preset"])
    specs = list(w.get("zones") or []) if preset == "STORM" else []
    if len(specs) > max_zones:
        raise ScenarioError([TOO_MANY_STORM_ZONES])
    zones = []
    for i, z in enumerate(specs):
        sev = float(z.get("severity", 0.0))
        if not (0.0 <= sev <= 1.0):
            raise ScenarioError([f"{BAD_SETTING}:zone.severity"])
        until = z.get("active_until_s")
        zones.append({"zone_id": str(z.get("zone_id", f"storm-{i + 1}")), "polygon": zone_polygon(route, z),
                      "severity": sev, "active_from_s": float(z.get("active_from_s", 0.0)),
                      "active_until_s": float(coverage_end_s if until is None else until),
                      "blocked": bool(z.get("blocked", False))})
    return {"source": C.WEATHER_SIMULATED, "coverage_start_s": 0.0, "coverage_end_s": float(coverage_end_s),
            "zones": zones}


def _is_water(land: Dict[str, Any]):
    if not land or not land.get("validated"):
        return None
    return lambda p: geo.check_water_path([p], land, 0.0)["ok"]


def _designated_whale(spec: Dict[str, Any], route: Dict[str, Any], land: Dict[str, Any], notes: List[str]
                      ) -> Dict[str, Any]:
    frac, lat_m = float(spec.get("at_fraction", 0.5)), float(spec.get("lateral_m", 0.0))
    start = route_offset_point(route, frac, lat_m)
    water = _is_water(land)
    if water is not None and not water(start) and lat_m:
        flipped = route_offset_point(route, frac, -lat_m)
        if water(flipped):
            start = flipped
            notes.append("DESIGNATED_WHALE_SIDE_FLIPPED_OFF_LAND")
    if water is not None and not water(start):
        raise ScenarioError([WHALE_START_ON_LAND])
    whale: Dict[str, Any] = {"whale_id": "designated", "start": start, "species": spec.get("species", "humpback"),
                             "speed_mps": float(spec.get("speed_mps", 0.0))}
    if whale["speed_mps"] > 0:
        _, t = route_frame(route, frac)
        rel = math.radians(float(spec.get("heading_rel_deg", 90.0)))      # 0 = along travel, 90 = to the left
        dx, dy = t[0] * math.cos(rel) - t[1] * math.sin(rel), t[0] * math.sin(rel) + t[1] * math.cos(rel)
        whale["heading_deg"] = math.degrees(math.atan2(dx, dy)) % 360.0     # compass: 0 = north
    exposure = spec.get("exposure", "ALWAYS")
    if exposure == "ALWAYS":
        whale["exposure"] = [{"from_s": 0, "until_s": 1e9, "exposed": True}]
    elif exposure != "CYCLE":                                               # CYCLE = simulator dive defaults
        raise ScenarioError([f"{BAD_SETTING}:designated_whale.exposure"])
    return whale


def background_area(routes: List[Dict[str, Any]], margin_m: float) -> Dict[str, float]:
    """Selected routes' bbox grown by ``margin_m``, clipped to the region."""
    pts = [p for r in routes for p in r["waypoints"]]
    if not pts:
        return dict(C.REGION_BBOX)
    dlat = margin_m / (C.EARTH_RADIUS_M * math.pi / 180.0)
    dlon = dlat / math.cos(math.radians(C.PROJECTION_ORIGIN["lat"]))
    R = C.REGION_BBOX
    return {"lat_min": max(R["lat_min"], min(p["lat"] for p in pts) - dlat),
            "lat_max": min(R["lat_max"], max(p["lat"] for p in pts) + dlat),
            "lon_min": max(R["lon_min"], min(p["lon"] for p in pts) - dlon),
            "lon_max": min(R["lon_max"], max(p["lon"] for p in pts) + dlon)}


def build_run_inputs(bundle: Dict[str, Any], land: Dict[str, Any], scenario_settings: Dict[str, Any],
                     config: Dict[str, Any]) -> Dict[str, Any]:
    """Frozen ``RunInputs`` for one scenario. Inputs are deep-copied; nothing here reads a live capture."""
    s = copy.deepcopy(scenario_settings)
    sc = config.get("scenarios") or {}
    sim = config.get("simulation") or {}
    plan = config.get("planner") or {}
    usable = usable_routes(bundle)
    by_id = {str(r["route_id"]): r for r in usable}
    wanted = [str(x) for x in s.get("route_ids") or []]
    missing = [rid for rid in wanted if rid not in by_id]
    if missing and usable:
        raise ScenarioError([f"{UNKNOWN_ROUTE_ID}:{rid}" for rid in missing])
    routes = [by_id[rid] for rid in wanted if rid in by_id] or usable

    seed = int(s.get("seed", 0))
    duration = float(s.get("duration_s", sim.get("duration_s", 7200)))
    n_voy = int(s.get("voyages", sc.get("default_voyages", 5)))
    spacing = float(s.get("voyage_spacing_s", sc.get("voyage_spacing_s", 600)))
    if n_voy < 0 or spacing < 0 or duration <= 0:
        raise ScenarioError([f"{BAD_SETTING}:voyages"])
    voyages = [{"voyage_id": f"v{i + 1}", "route_id": str(routes[i % len(routes)]["route_id"]),
                "entry_s": i * spacing} for i in range(n_voy)] if routes else []

    notes: List[str] = []
    whales: List[Dict[str, Any]] = []
    route0 = routes[0] if routes else None
    if s.get("designated_whale") and route0 is not None:
        whales.append(_designated_whale(s["designated_whale"], route0, land, notes))
    n_bg = s.get("background_whales")
    if n_bg is None:
        n_bg = max(0, int(sc.get("default_whales", 20)) - len(whales))
    if int(n_bg) > 0:
        from save_the_whales.simulation.whales import make_background_whales   # Person 2's seeded specs
        whales += make_background_whales(int(n_bg), seed, background_area(routes, float(sc.get("background_margin_m", 5000))),
                                         is_water=_is_water(land))

    coverage_end = duration + float(sim.get("drain_max_s", 1800)) + float(plan.get("max_rejoin_s", 3600))
    weather = weather_field(s, route0, coverage_end, int(sc.get("max_storm_zones", 3)))
    scenario = {"scenario_id": str(s.get("preset_id", "custom")), "seed": seed,
                "start_at": str(s.get("start_at", sc.get("start_at", "2026-07-01T12:00:00Z"))),
                "duration_s": duration, "voyages": voyages, "whales": whales, "weather": weather}
    if notes:
        scenario["assembly_notes"] = notes
    source = bundle.get("source") or {}
    fixture_only = bool(source.get("fixture_only", True) or (land or {}).get("fixture_only", True)
                        or s.get("fixture_only") or s.get("land_override"))
    digest = _canonical({"bundle": bundle.get("bundle_id"), "capture": source.get("capture_sha256"),
                         "land": (land or {}).get("sha256"), "scenario": scenario, "config": config})
    return copy.deepcopy({"run_id": f"{scenario['scenario_id']}-s{seed}-{digest[:10]}", "route_bundle": bundle,
                          "land": land, "scenario": scenario, "config": config, "fixture_only": fixture_only})


def readiness(bundle: Dict[str, Any], land: Dict[str, Any]) -> Dict[str, Any]:
    """Real-demo gate: genuine API-verified AIS routes plus validated non-fixture land.

    ``can_run`` only says the simulator has something to run; ``demo_ready`` is the acceptance gate.
    """
    source = bundle.get("source") or {}
    reasons: List[str] = []
    if not usable_routes(bundle):
        reasons.append(C.AIS_ROUTE_UNAVAILABLE)
    if source.get("kind") != C.AISSTREAM_CAPTURE or not source.get("api_verified") or source.get("fixture_only", True):
        reasons.append("ROUTES_NOT_GENUINE_AIS")
    if not land or not land.get("validated"):
        reasons.append(C.INVALID_MAP)
    elif land.get("fixture_only", True):
        reasons.append("LAND_IS_FIXTURE")
    return {"can_run": bool(usable_routes(bundle)), "detours_allowed": bool(land and land.get("validated")),
            "demo_ready": not reasons, "reasons": reasons}
