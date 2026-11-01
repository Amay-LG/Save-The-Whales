"""Scenario initialization, the virtual clock, keyed randomness and the injected projection.

``World`` is built from a private JSON copy of frozen ``RunInputs``: later edits to the caller's dicts,
the route-cache file or the AIS capture cannot reach it. Nothing here reads files or the network.

Geometry uses only the injected ``project(Point) -> (x_m, y_m)``. Its inverse is solved numerically
(chord-Newton on a Jacobian measured once), which is exact in one step for the shared affine
tangent-plane projection.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from save_the_whales.shared import contracts as C

XY = Tuple[float, float]
REQUIRED_CONFIG = ("simulation", "sensors", "response", "metrics")
CONFIG_MISSING = "CONFIG_MISSING"
NO_VOYAGES = "NO_VOYAGES"
ENTRY_AFTER_DURATION = "ENTRY_AFTER_DURATION"
INVALID_INPUTS = "INVALID_INPUTS"
INVALID_WEATHER = "INVALID_WEATHER"
SCENARIO_CAP_EXCEEDED = "SCENARIO_CAP_EXCEEDED"


class InputError(Exception):
    """Inputs that make a run impossible; reported as an ERROR result, never raised to callers."""

    def __init__(self, codes: List[str]):
        super().__init__(", ".join(codes))
        self.codes = codes


# --- Clock --------------------------------------------------------------------------------------------
def parse_utc(text: str) -> datetime:
    """``YYYY-MM-DDTHH:MM:SS[.fff]Z`` -> aware UTC datetime."""
    if not isinstance(text, str) or not text.endswith("Z"):
        raise ValueError(f"not a UTC 'Z' timestamp: {text!r}")
    body = text[:-1]
    fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in body else "%Y-%m-%dT%H:%M:%S"
    return datetime.strptime(body, fmt).replace(tzinfo=timezone.utc)


def iso_at(start: datetime, t_s: float) -> str:
    """Virtual elapsed seconds -> UTC string (millisecond resolution, fraction only when needed)."""
    ms = int(round(t_s * 1000.0))
    dt = start + timedelta(milliseconds=ms)
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{ms % 1000:03d}Z" if ms % 1000 else base + "Z"


def seconds_since(start: datetime, text: str) -> float:
    return (parse_utc(text) - start).total_seconds()


# --- Keyed randomness ---------------------------------------------------------------------------------
# Draws are keyed by name (seed, sensor, sample, entity, ...) rather than taken from one shared stream,
# so a policy that changes ship paths or message counts never shifts unrelated random draws.
def _digest(keys: Tuple[Any, ...]) -> bytes:
    return hashlib.sha256("|".join(str(k) for k in keys).encode()).digest()


def stable_uniform(*keys: Any) -> float:
    """Deterministic U[0, 1) from the key tuple (process-independent, unlike ``hash()``)."""
    return (int.from_bytes(_digest(keys)[:8], "big") >> 11) / float(1 << 53)


def stable_normal(*keys: Any) -> float:
    """Deterministic standard normal (Box-Muller on two keyed uniforms)."""
    u1 = 1.0 - stable_uniform(*keys, "n1")   # (0, 1]
    u2 = stable_uniform(*keys, "n2")
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def named_stream(*keys: Any) -> random.Random:
    """A private ``random.Random`` per named entity (e.g. one per whale)."""
    return random.Random(int.from_bytes(_digest(keys)[:8], "big"))


def opaque_id(prefix: str, *keys: Any) -> str:
    return f"{prefix}-{_digest(keys).hex()[:12]}"


def canonical_sha256(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# --- Projection frame ---------------------------------------------------------------------------------
class Frame:
    """Wraps the injected ``project`` and provides its numerical inverse."""

    def __init__(self, project: Callable[[Dict[str, float]], XY]):
        self._project = project
        ref = dict(C.PROJECTION_ORIGIN)
        x0, y0 = self.xy(ref)
        h = 1e-3
        xa, ya = self.xy({"lat": ref["lat"] + h, "lon": ref["lon"]})
        xb, yb = self.xy({"lat": ref["lat"], "lon": ref["lon"] + h})
        # Jacobian d(x, y)/d(lat, lon) and its inverse.
        j11, j12, j21, j22 = (xa - x0) / h, (xb - x0) / h, (ya - y0) / h, (yb - y0) / h
        det = j11 * j22 - j12 * j21
        if not math.isfinite(det) or abs(det) < 1e-9:
            raise ValueError("project() is degenerate near the shared origin")
        self._inv = (j22 / det, -j12 / det, -j21 / det, j11 / det)
        self._ref, self._ref_xy = ref, (x0, y0)

    def xy(self, p: Dict[str, float]) -> XY:
        x, y = self._project(p)
        return (float(x), float(y))

    def point(self, x: float, y: float, guess: Optional[Dict[str, float]] = None) -> Dict[str, float]:
        """(x_m, y_m) -> Point such that ``project(point)`` matches within 1e-6 m."""
        p = dict(guess) if guess else dict(self._ref)
        i11, i12, i21, i22 = self._inv
        for _ in range(8):
            px, py = self.xy(p)
            ex, ey = x - px, y - py
            if abs(ex) < 1e-6 and abs(ey) < 1e-6:
                break
            p = {"lat": p["lat"] + i11 * ex + i12 * ey, "lon": p["lon"] + i21 * ex + i22 * ey}
        return p


def knots_to_mps(speed_knots: float) -> float:
    return speed_knots * C.KNOT_MPS


def mps_to_knots(speed_mps: float) -> float:
    return speed_mps / C.KNOT_MPS


def compass_deg(dx: float, dy: float) -> float:
    """Course over ground in compass degrees (0 = north/+y, 90 = east/+x)."""
    return math.degrees(math.atan2(dx, dy)) % 360.0


# --- Frozen inputs ------------------------------------------------------------------------------------
def freeze_inputs(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Private JSON copy; also proves the inputs are JSON-compatible."""
    try:
        return json.loads(json.dumps(inputs))
    except (TypeError, ValueError) as exc:
        raise InputError([INVALID_INPUTS]) from exc


def usable_routes(bundle: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Routes that passed source validation and have >= 2 finite waypoints, keyed by ``route_id``."""
    out: Dict[str, Dict[str, Any]] = {}
    for r in bundle.get("routes") or []:
        if not isinstance(r, dict) or not (r.get("validation") or {}).get("ok"):
            continue
        wps = r.get("waypoints") or []
        if len(wps) < 2 or not all(isinstance(p, dict) and math.isfinite(p.get("lat", math.nan))
                                   and math.isfinite(p.get("lon", math.nan)) for p in wps):
            continue
        out[str(r["route_id"])] = r
    return out


def check_inputs(inputs: Dict[str, Any]) -> List[str]:
    """Hard input-readiness failures (ERROR results). Soft issues are reported in the manifest."""
    codes: List[str] = []
    config = inputs.get("config") or {}
    codes += [f"{CONFIG_MISSING}:{k}" for k in REQUIRED_CONFIG if not isinstance(config.get(k), dict)]
    bundle = inputs.get("route_bundle") or {}
    if bundle.get("schema_version") != C.SCHEMA_VERSION:
        codes.append(C.SCHEMA_VERSION_MISMATCH)
    if not usable_routes(bundle):
        codes.append(C.AIS_ROUTE_UNAVAILABLE)
    scenario = inputs.get("scenario") or {}
    if not scenario.get("voyages"):
        codes.append(NO_VOYAGES)
    try:
        parse_utc(scenario.get("start_at"))
    except (TypeError, ValueError):
        codes.append(INVALID_INPUTS)
    weather = scenario.get("weather")
    if not isinstance(weather, dict) or not isinstance(weather.get("zones"), list) or not all(
            isinstance(weather.get(k), (int, float)) for k in ("coverage_start_s", "coverage_end_s")):
        codes.append(INVALID_WEATHER)   # missing weather is unknown, never silently clear
    sim = config.get("simulation") if isinstance(config.get("simulation"), dict) else {}
    # Architecture §10 initial caps: 10 ships, 30 whales, six hours, 30-minute drain.
    if len(scenario.get("voyages") or []) > int(sim.get("max_voyages", 10)) \
            or len(scenario.get("whales") or []) > int(sim.get("max_whales", 30)) \
            or float(scenario.get("duration_s", 0)) > float(sim.get("max_duration_s", 21600)) \
            or float(sim.get("drain_max_s", 1800)) > float(sim.get("max_drain_s", 1800)):
        codes.append(SCENARIO_CAP_EXCEEDED)
    return codes
