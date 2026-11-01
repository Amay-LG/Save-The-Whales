"""Simulated ships: waypoint motion along an immutable AIS-derived baseline or a validated detour.

Kinematics are research-grade: instantaneous turns and speed changes, constant speed between events,
leftover travel carried across waypoints within a step, and a stop at the last source waypoint (no
synthetic extension). Each ship moves in projected meters and emits linear pieces for metrics, plus
``TimedLeg`` dicts for the injected weather integral.

Path bookkeeping: ``path`` holds future waypoints (excluding the current position). Each carries its
``original_route`` index, or None for a detour point. ``progress`` (baseline_progress_index) is the
highest baseline index reached and never decreases. ``rejoin_index`` is set while off-route and cleared
when that baseline waypoint is reached.
"""
from __future__ import annotations

import copy
import math
from typing import Any, Dict, List, Optional, Tuple

from save_the_whales.shared import contracts as C
from save_the_whales.simulation.world import Frame, compass_deg, knots_to_mps

PENDING, ACTIVE, COMPLETE, FAILED = "PENDING", "ACTIVE", "COMPLETE", "FAILED"
TIME_EPS = 1e-9
MATCH_TOL_M = 1.0          # validated baseline tail must coincide with original_route within this
VALIDATED_ACTION_MALFORMED = "VALIDATED_ACTION_MALFORMED"


class ExecutionReject(Exception):
    """A validated action the executor cannot apply consistently (never silently repaired)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


class Waypoint:
    __slots__ = ("pt", "xy", "base")

    def __init__(self, pt: Dict[str, float], xy: Tuple[float, float], base: Optional[int]):
        self.pt, self.xy, self.base = pt, xy, base


class Piece:
    """Linear motion over [ta, tb]: position (x, y) at ta, velocity (vx, vy) m/s."""
    __slots__ = ("ta", "tb", "x", "y", "vx", "vy", "speed_knots", "start", "end")

    def __init__(self, ta, tb, x, y, vx, vy, speed_knots, start, end):
        self.ta, self.tb, self.x, self.y, self.vx, self.vy = ta, tb, x, y, vx, vy
        self.speed_knots, self.start, self.end = speed_knots, start, end

    def leg(self) -> Dict[str, Any]:
        return {"start": dict(self.start), "end": dict(self.end), "start_s": self.ta, "end_s": self.tb,
                "speed_knots": self.speed_knots}


def ship_id_for(source_mmsi: str, voyage_id: str) -> str:
    return f"sim:{source_mmsi}:{voyage_id}"


class Ship:
    def __init__(self, voyage: Dict[str, Any], route: Dict[str, Any], frame: Frame):
        self.voyage_id = str(voyage["voyage_id"])
        self.route_id = str(route["route_id"])
        self.source_mmsi = str(route["source_mmsi"])
        self.ship_id = ship_id_for(self.source_mmsi, self.voyage_id)
        self.entry_s = float(voyage["entry_s"])
        self.length_m = float(route["length_m"])
        self.beam_m = float(route["beam_m"])
        self.service_speed_knots = float(route["service_speed_knots"])
        self.frame = frame
        # Immutable baseline: tuple of private copies; observations receive fresh copies.
        self.original_route = tuple({"lat": float(p["lat"]), "lon": float(p["lon"])} for p in route["waypoints"])
        self.original_xy = tuple(frame.xy(p) for p in self.original_route)
        self.status = PENDING
        self.fail_reason: Optional[str] = None
        self.pos = dict(self.original_route[0])
        self.xy = self.original_xy[0]
        self.path: List[Waypoint] = [Waypoint(dict(p), xy, i) for i, (p, xy) in
                                     enumerate(zip(self.original_route, self.original_xy)) if i > 0]
        self.progress = 0
        self.rejoin_index: Optional[int] = None
        self.commanded_speed_knots: Optional[float] = None
        self.active: Optional[Dict[str, Any]] = None     # {"action", "valid_until_s", "applied_s"}
        self.latest_revision = 0
        self.last_action_at: Optional[str] = None
        self.incident: Optional[Dict[str, Any]] = None
        self.accepted_action_ids: set = set()
        self.incidents_applied = 0        # compliance draws are keyed by incident ordinal
        self.incident_ordinal = 0
        self.cog_deg = self._heading()
        self.distance_m = 0.0
        self.completed_s: Optional[float] = None
        self.failed_s: Optional[float] = None
        self.reference_distance_m = sum(math.hypot(b[0] - a[0], b[1] - a[1])
                                        for a, b in zip(self.original_xy, self.original_xy[1:]))

    # --- state -------------------------------------------------------------------------------------
    @property
    def speed_knots(self) -> float:
        if self.status != ACTIVE:
            return 0.0
        cmd = self.commanded_speed_knots
        return self.service_speed_knots if cmd is None else min(cmd, self.service_speed_knots)

    @property
    def off_route(self) -> bool:
        return self.rejoin_index is not None

    def _heading(self) -> float:
        for w in self.path:
            dx, dy = w.xy[0] - self.xy[0], w.xy[1] - self.xy[1]
            if math.hypot(dx, dy) > 1e-6:
                return compass_deg(dx, dy)
        return getattr(self, "cog_deg", 0.0)

    def spawn(self, t: float) -> None:
        self.status = ACTIVE
        self.entry_s = t

    def fail(self, t: float, reason: str) -> None:
        self.status, self.fail_reason, self.failed_s = FAILED, reason, t

    # --- motion ------------------------------------------------------------------------------------
    def advance(self, t0: float, t1: float) -> Tuple[List[Piece], List[Tuple[str, float, Dict[str, Any]]]]:
        """Move from t0 to t1 at the current speed. Returns linear pieces and notes
        (``REJOINED``, ``VOYAGE_COMPLETE``) with their exact times."""
        pieces: List[Piece] = []
        notes: List[Tuple[str, float, Dict[str, Any]]] = []
        t = t0
        speed_kn = self.speed_knots
        v = knots_to_mps(speed_kn)
        while self.status == ACTIVE:
            if not self.path:
                self._complete(t, notes)
                break
            if t >= t1 - TIME_EPS:
                break
            w = self.path[0]
            dx, dy = w.xy[0] - self.xy[0], w.xy[1] - self.xy[1]
            d = math.hypot(dx, dy)
            if d <= 1e-9:                       # zero-length leg: arrive without spending time
                self._reach(w, t, notes)
                continue
            if v <= 0:
                break
            t_reach = t + d / v
            if t_reach <= t1 + TIME_EPS:
                t_reach = min(t_reach, t1) if t_reach > t1 else t_reach
                pieces.append(Piece(t, t_reach, self.xy[0], self.xy[1], dx / (t_reach - t), dy / (t_reach - t),
                                    speed_kn, self.pos, w.pt))
                self._accumulate(d, t_reach - t)
                self.cog_deg = compass_deg(dx, dy)
                self._reach(w, t_reach, notes)
                t = t_reach
            else:
                f = (t1 - t) * v / d
                nx, ny = self.xy[0] + dx * f, self.xy[1] + dy * f
                guess = {"lat": self.pos["lat"] + (w.pt["lat"] - self.pos["lat"]) * f,
                         "lon": self.pos["lon"] + (w.pt["lon"] - self.pos["lon"]) * f}
                end = self.frame.point(nx, ny, guess)
                pieces.append(Piece(t, t1, self.xy[0], self.xy[1], dx / d * v, dy / d * v, speed_kn, self.pos, end))
                self._accumulate(d * f, t1 - t)
                self.cog_deg = compass_deg(dx, dy)
                self.pos, self.xy = end, (nx, ny)
                t = t1
        return pieces, notes

    def _accumulate(self, dist: float, dt: float) -> None:
        self.distance_m += dist
        if self.incident is not None:
            self.incident["accumulated_distance_m"] += dist
            self.incident["accumulated_time_s"] += dt

    def _reach(self, w: Waypoint, t: float, notes: list) -> None:
        self.pos, self.xy = dict(w.pt), w.xy        # snap exactly onto the waypoint
        self.path.pop(0)
        if w.base is not None:
            self.progress = max(self.progress, w.base)
            if self.rejoin_index is not None and w.base >= self.rejoin_index:
                notes.append(("REJOINED", t, {"rejoin_route_index": self.rejoin_index}))
                self.rejoin_index = None
                # A detour/resume (and its speed) ends at the rejoin; a slowdown keeps its own lease.
                if self.active is None or self.active["action"]["command"] != C.SLOWDOWN:
                    self.active, self.commanded_speed_knots, self.incident = None, None, None
        if not self.path:
            self._complete(t, notes)

    def _complete(self, t: float, notes: list) -> None:
        if self.status == ACTIVE:
            self.status, self.completed_s = COMPLETE, t
            self.active, self.incident = None, None
            notes.append(("VOYAGE_COMPLETE", t, {}))

    # --- commands ----------------------------------------------------------------------------------
    def tag_remaining(self, remaining: List[Dict[str, Any]], rejoin: int) -> List[Waypoint]:
        """Resolve validated remaining waypoints into tagged path entries. The last
        ``len(original_route) - rejoin`` points must coincide with ``original_route[rejoin:]``."""
        n = len(self.original_route)
        if not isinstance(rejoin, int) or isinstance(rejoin, bool) or not (self.progress < rejoin < n):
            raise ExecutionReject(C.STALE_ACTION if isinstance(rejoin, int) and rejoin <= self.progress
                                  else VALIDATED_ACTION_MALFORMED, f"rejoin_route_index={rejoin!r}")
        tail = n - rejoin
        if not isinstance(remaining, list) or len(remaining) < tail:
            raise ExecutionReject(VALIDATED_ACTION_MALFORMED, "remaining_waypoints too short")
        out: List[Waypoint] = []
        for i, p in enumerate(remaining):
            if not (isinstance(p, dict) and math.isfinite(p.get("lat", math.nan)) and math.isfinite(p.get("lon", math.nan))):
                raise ExecutionReject(C.INVALID_ACTION, "non-finite remaining waypoint")
            pt = {"lat": float(p["lat"]), "lon": float(p["lon"])}
            k = i - (len(remaining) - tail)
            if k >= 0:
                base = rejoin + k
                bx, by = self.original_xy[base]
                x, y = self.frame.xy(pt)
                if math.hypot(x - bx, y - by) > MATCH_TOL_M:
                    raise ExecutionReject(VALIDATED_ACTION_MALFORMED, f"tail point {i} != original_route[{base}]")
                out.append(Waypoint(dict(self.original_route[base]), self.original_xy[base], base))
            else:
                out.append(Waypoint(pt, self.frame.xy(pt), None))
        return out

    def apply(self, va: Dict[str, Any], t: float, valid_until_s: float, iso_now: str) -> bool:
        """Apply a validated action. Returns True when the path geometry changed."""
        action = va["action"]
        cmd = action["command"]
        speed = va.get("effective_target_speed_knots")
        if speed is not None and not (isinstance(speed, (int, float)) and math.isfinite(speed) and speed > 0):
            raise ExecutionReject(VALIDATED_ACTION_MALFORMED, f"effective_target_speed_knots={speed!r}")
        rejoin = va.get("rejoin_route_index")
        changed = False
        if cmd in (C.DETOUR, C.RESUME):
            new_path = self.tag_remaining(va.get("remaining_waypoints"), rejoin)
            changed = [w.pt for w in new_path] != [w.pt for w in self.path]   # e.g. a RESUME on baseline: no
            self.path = new_path
            on_baseline = all(w.base is not None for w in new_path) and self._on_baseline_toward(rejoin)
            self.rejoin_index = None if on_baseline else rejoin
        elif cmd == C.SLOWDOWN:
            if rejoin is not None and rejoin != self.rejoin_index:
                raise ExecutionReject(VALIDATED_ACTION_MALFORMED, "SLOWDOWN may not move the rejoin")
        else:
            raise ExecutionReject(C.INVALID_ACTION, f"command={cmd!r}")
        self.commanded_speed_knots = None if speed is None else float(speed)
        self.active = {"action": copy.deepcopy(action), "valid_until_s": valid_until_s, "applied_s": t}
        if cmd == C.RESUME and self.rejoin_index is None and self.speed_knots >= self.service_speed_knots:
            self.active, self.commanded_speed_knots = None, None   # back on baseline: nothing to expire
        budget = va.get("incident_budget")
        if isinstance(budget, dict):
            same = self.incident is not None and self.incident.get("incident_id") == budget.get("incident_id")
            kept = {k: self.incident[k] for k in ("accumulated_time_s", "accumulated_distance_m")} if same else {}
            if not same:
                self.incidents_applied += 1
                self.incident_ordinal = self.incidents_applied
            self.incident = dict(copy.deepcopy(budget), **kept)
            self.incident.setdefault("accumulated_time_s", 0.0)
            self.incident.setdefault("accumulated_distance_m", 0.0)
        if self.active is None and self.rejoin_index is None:
            self.incident = None
        self.cog_deg = self._heading()
        return changed

    def _on_baseline_toward(self, rejoin: int) -> bool:
        """True when the ship already sits on the baseline leg ending at ``rejoin``."""
        if rejoin != self.progress + 1:
            return False
        a, b = self.original_xy[self.progress], self.original_xy[rejoin]
        ex, ey = b[0] - a[0], b[1] - a[1]
        L2 = ex * ex + ey * ey
        if L2 <= 0:
            return True
        s = max(0.0, min(1.0, ((self.xy[0] - a[0]) * ex + (self.xy[1] - a[1]) * ey) / L2))
        return math.hypot(a[0] + ex * s - self.xy[0], a[1] + ey * s - self.xy[1]) <= MATCH_TOL_M

    def end_lease(self) -> None:
        """Lease expiry while on the baseline: restore service speed."""
        self.active = None
        self.commanded_speed_knots = None
        if self.rejoin_index is None:
            self.incident = None

    def unpassed_detour_points(self) -> List[Dict[str, float]]:
        return [dict(w.pt) for w in self.path if w.base is None]

    # --- observable snapshot -----------------------------------------------------------------------
    def observable(self) -> Dict[str, Any]:
        return {
            "ship_id": self.ship_id,
            "lat": self.pos["lat"],
            "lon": self.pos["lon"],
            "sog_knots": self.speed_knots,
            "cog_deg": self.cog_deg,
            "length_m": self.length_m,
            "beam_m": self.beam_m,
            "service_speed_knots": self.service_speed_knots,
            "original_route": [dict(p) for p in self.original_route],
            "current_route": [dict(w.pt) for w in self.path],
            "active_command": copy.deepcopy(self.active["action"]) if self.active else None,
            "baseline_progress_index": self.progress,
            "latest_revision": self.latest_revision,
            "last_action_at": self.last_action_at,
            "incident_budget": copy.deepcopy(self.incident),
        }
