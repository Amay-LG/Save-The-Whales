"""Bounded ``run_scenario`` loop: the one callback boundary between the simulated world and the advisor.

    run_scenario(inputs, decide, validate_action, weather_exposure, *, project, on_frame=None) -> RunResult

Per event time ``t`` (tick boundaries every ``dt_s``, plus spawns, command applications, lease expiries,
sensor samples and report releases):

    spawn due voyages -> apply due commands / expire leases -> sample sensors, release due reports
    -> decide when due (every ``decision_dt_s``; ISSUE-validate, queue, lag-0 APPLY)
    -> record a frame when due -> advance ships and whales to the next event time, evaluating truth
       and weather on every linear sub-interval

The run ends early when no pending or active voyage remains, at ``duration_s + drain_max_s``, or at the
``wall_limit_s`` stop (INCOMPLETE). Callback exceptions end the run as ERROR with a diagnostic event.
Input-readiness failures (no usable routes, missing config sections, ...) return an ERROR result.

``config["simulation"]`` keys: ``dt_s``, ``decision_dt_s``, ``drain_max_s``, ``wall_limit_s``,
``frame_every_s``, ``max_frames``, ``max_events``, ``max_decisions``, ``record_analyst_truth`` and the
whale defaults (see ``whales.py``). ``config["response"]``: ``response_lag_s``, ``compliance``.
``config["metrics"]``: ``sigma_eval_m``, ``encounter_enter_m``, ``encounter_exit_m``,
``encounter_exit_dwell_s``, ``whale_body_radius_m``. ``config["sensors"]``: see ``sensors.py``.
"""
from __future__ import annotations

import copy
import math
import time
from typing import Any, Callable, Dict, List, Optional

from save_the_whales.shared import contracts as C
from save_the_whales.simulation import metrics as M
from save_the_whales.simulation.commands import CallbackError, CommandExecutor
from save_the_whales.simulation.sensors import SensorModel, build_observation
from save_the_whales.simulation.ships import ACTIVE, COMPLETE, FAILED, PENDING, Ship
from save_the_whales.simulation.whales import Whale
from save_the_whales.simulation.world import (ENTRY_AFTER_DURATION, Frame, InputError, canonical_sha256,
                                              check_inputs, freeze_inputs, iso_at, knots_to_mps, parse_utc,
                                              usable_routes)

MODE = "AIS_SEEDED_SIMULATION"
ANALYST_TRUTH_LABEL = "ANALYST_TRUTH_NOT_ADVISOR_INPUT"
WALL_LIMIT, DRAIN_LIMIT = "WALL_LIMIT", "DRAIN_LIMIT"
CALLBACK_ERROR = "CALLBACK_ERROR"
DUPLICATE_VOYAGE = "DUPLICATE_VOYAGE"
T_EPS = 1e-9


def run_scenario(inputs: Dict[str, Any], decide: Callable[[Dict[str, Any]], Dict[str, Any]],
                 validate_action: Callable[..., Dict[str, Any]],
                 weather_exposure: Callable[[List[Dict[str, Any]], Dict[str, Any]], float],
                 *, project: Callable[[Dict[str, float]], tuple],
                 on_frame: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
    wall0 = time.monotonic()
    try:
        frozen = freeze_inputs(inputs)
        codes = check_inputs(frozen)
        if codes:
            raise InputError(codes)
        sim = _Run(frozen, decide, validate_action, weather_exposure, project, on_frame, wall0)
    except InputError as exc:
        return _input_error_result(inputs, exc.codes, wall0)
    return sim.run()


def _input_error_result(inputs: Any, codes: List[str], wall0: float) -> Dict[str, Any]:
    inputs = inputs if isinstance(inputs, dict) else {}
    scenario = inputs.get("scenario") or {}
    bundle = inputs.get("route_bundle") or {}
    return {
        "manifest": {"run_id": inputs.get("run_id"), "schema_version": C.SCHEMA_VERSION, "mode": MODE,
                     "status": C.RUN_ERROR, "status_reasons": list(codes), "seed": scenario.get("seed"),
                     "scenario_id": scenario.get("scenario_id"), "fixture_only": bool(inputs.get("fixture_only")),
                     "route_bundle": {"bundle_id": bundle.get("bundle_id"), "issues": list(bundle.get("issues") or []),
                                      "source": copy.deepcopy(bundle.get("source"))}},
        "summary": _summary(C.RUN_ERROR, time.monotonic() - wall0, 0.0),
        "frames": [], "events": [{"sim_time_s": 0.0, "type": "INPUT_ERROR", "details": {"codes": list(codes)}}],
        "decisions": [],
    }


def _summary(status: str, wall_s: float, sim_s: float, **kw: Any) -> Dict[str, Any]:
    out = {"status": status, "elapsed_wall_s": wall_s, "simulated_duration_s": sim_s, "voyages_completed": 0,
           "voyages_failed": 0, "modeled_contacts": 0, "close_approaches": 0, "whale_exposure_proxy": 0.0,
           "weather_exposure_s": None, "added_delay_s": None, "extra_distance_m": None, "alerts": 0,
           "route_changes": 0, "rejected_actions": 0}
    out.update(kw)
    return out


class _Run:
    def __init__(self, inputs, decide, validate_action, weather_exposure, project, on_frame, wall0):
        self.inputs, self.wall0 = inputs, wall0
        self.decide, self.weather_fn, self.on_frame = decide, weather_exposure, on_frame
        cfg = inputs["config"]
        self.sim_cfg, self.metric_cfg = cfg["simulation"], cfg["metrics"]
        scen = inputs["scenario"]
        self.run_id = str(inputs.get("run_id"))
        self.seed = int(scen.get("seed", 0))
        self.start_dt = parse_utc(scen["start_at"])
        self.duration_s = float(scen.get("duration_s", self.sim_cfg.get("duration_s", 7200)))
        self.dt = float(self.sim_cfg.get("dt_s", 30))
        self.decision_dt = float(self.sim_cfg.get("decision_dt_s", 60))
        self.drain_max = float(self.sim_cfg.get("drain_max_s", 1800))
        self.wall_limit = float(self.sim_cfg.get("wall_limit_s", 30))
        self.frame_every = float(self.sim_cfg.get("frame_every_s", self.decision_dt))
        self.max_frames = int(self.sim_cfg.get("max_frames", 400))
        self.max_events = int(self.sim_cfg.get("max_events", 2000))
        self.max_decisions = int(self.sim_cfg.get("max_decisions", 300))
        self.end_s = self.duration_s + self.drain_max
        self.frame = Frame(project)
        self.bundle, self.land, self.weather = inputs["route_bundle"], inputs["land"], scen.get("weather")
        self.static = {"land": self.land, "weather": self.weather}
        self.bbox = self.bundle.get("bbox") or dict(C.REGION_BBOX)

        self.events: List[Dict[str, Any]] = []
        self.events_dropped = 0
        self.frames: List[Dict[str, Any]] = []
        self.decisions: List[Dict[str, Any]] = []
        self.decisions_dropped = 0
        self.status_reasons: List[str] = []
        self.issues: List[str] = []
        self.error: Optional[CallbackError] = None
        self.weather_unknown = False
        self.weather_by_ship: Dict[str, float] = {}
        self.n_decisions = 0
        self.n_obs = 0

        routes = usable_routes(self.bundle)
        self.ships: Dict[str, Ship] = {}
        self.voyage_issues: List[Dict[str, Any]] = []
        self.route_use: Dict[str, List[str]] = {}
        for v in sorted(scen["voyages"], key=lambda v: (float(v.get("entry_s", 0)), str(v.get("voyage_id")))):
            route = routes.get(str(v.get("route_id")))
            if route is None:
                self.voyage_issues.append({"voyage_id": v.get("voyage_id"), "route_id": v.get("route_id"),
                                           "reason": C.INVALID_BASELINE_ROUTE})
                continue
            if float(v.get("entry_s", 0)) >= self.duration_s:
                self.voyage_issues.append({"voyage_id": v.get("voyage_id"), "route_id": v.get("route_id"),
                                           "reason": ENTRY_AFTER_DURATION})
                continue
            ship = Ship(v, route, self.frame)
            if ship.ship_id in self.ships:
                self.voyage_issues.append({"voyage_id": v.get("voyage_id"), "reason": DUPLICATE_VOYAGE})
                continue
            self.ships[ship.ship_id] = ship
            self.route_use.setdefault(ship.route_id, []).append(ship.voyage_id)
        if not self.ships:
            raise InputError(sorted({vi["reason"] for vi in self.voyage_issues}) or [C.INVALID_BASELINE_ROUTE])

        horizon = self.end_s + self.dt
        self.whales = [Whale(w, self.seed, horizon, self.frame, self.sim_cfg) for w in scen.get("whales") or []]
        self.sensors = SensorModel(cfg["sensors"], self.seed, self.frame, self.start_dt)
        self.tracker = M.EncounterTracker(self.metric_cfg, self._emit)
        self.executor = CommandExecutor(ships=self.ships, validate=validate_action, make_obs=self._obs_for,
                                        emit=self._emit, on_fail=self._fail_ship, response_cfg=cfg["response"],
                                        seed=self.seed, start_dt=self.start_dt)
        if not self.land or not self.land.get("validated"):
            self.issues.append(C.INVALID_MAP)
        if not isinstance(self.weather, dict) or self.weather.get("source") != C.WEATHER_SIMULATED:
            self.issues.append("WEATHER_NOT_SIMULATED_FIELD")

    # --- recording ---------------------------------------------------------------------------------
    def _emit(self, t: float, etype: str, ship_id: Optional[str] = None, details: Optional[Dict[str, Any]] = None):
        if len(self.events) >= self.max_events:
            self.events_dropped += 1
            return
        ev = {"sim_time_s": t, "type": etype, "details": details or {}}
        if ship_id is not None:
            ev["ship_id"] = ship_id
        self.events.append(ev)

    def _active(self) -> List[Ship]:
        return sorted((s for s in self.ships.values() if s.status == ACTIVE), key=lambda s: s.ship_id)

    def _obs(self, t: float, decision_id: str) -> Dict[str, Any]:
        return build_observation(now=iso_at(self.start_dt, t), sim_time_s=t, decision_id=decision_id,
                                 decision_dt_s=self.decision_dt, bbox=self.bbox, static=self.static,
                                 ships=self._active(), detections=self.sensors.evidence(t))

    def _obs_for(self, t: float, purpose: str) -> Dict[str, Any]:
        self.n_obs += 1
        return self._obs(t, f"{self.run_id}:{purpose}:{self.n_obs:05d}")

    def _fail_ship(self, ship: Ship, t: float, reason: str) -> None:
        ship.fail(t, reason)
        self.executor.pending = [p for p in self.executor.pending if p["ship_id"] != ship.ship_id]
        self.tracker.close_ship(ship.ship_id, t)
        self._emit(t, "VOYAGE_FAILED", ship.ship_id, {"reason": reason, "baseline_progress_index": ship.progress})

    def _frame(self, t: float) -> None:
        active = self._active()
        frame = {"sim_time_s": t, "now": iso_at(self.start_dt, t),
                 "ships": [s.observable() for s in active],
                 "whale_detections": self.sensors.evidence(t),
                 "alerts": [{"ship_id": s.ship_id, "command": s.active["action"].get("command"),
                             "action_id": s.active["action"].get("action_id"),
                             "revision": s.active["action"].get("revision"),
                             "valid_until": s.active["action"].get("valid_until"),
                             "rejoin_route_index": s.rejoin_index} for s in active if s.active]}
        if self.sim_cfg.get("record_analyst_truth", True):
            frame["analyst_truth"] = {"label": ANALYST_TRUTH_LABEL,
                                      "whales": [w.analyst_truth(t, self.frame) for w in self.whales]}
        if len(self.frames) >= self.max_frames:          # downsample: keep every other frame
            self.frames = self.frames[::2]
            self.frame_every *= 2
        self.frames.append(frame)
        if self.on_frame is not None:
            try:
                self.on_frame(copy.deepcopy(frame))
            except Exception as exc:  # noqa: BLE001 - presentation errors never change the simulation
                self._emit(t, "FRAME_CALLBACK_ERROR", None, {"error": f"{type(exc).__name__}: {exc}"})

    # --- steps -------------------------------------------------------------------------------------
    def _spawn_due(self, t: float) -> None:
        for s in sorted(self.ships.values(), key=lambda s: (s.entry_s, s.ship_id)):
            if s.status == PENDING and s.entry_s <= t + T_EPS:
                s.spawn(t)
                self._emit(t, "VOYAGE_SPAWNED", s.ship_id, {"voyage_id": s.voyage_id, "route_id": s.route_id,
                                                           "source_mmsi": s.source_mmsi,
                                                           "route_reused": len(self.route_use[s.route_id]) > 1})

    def _decide(self, t: float) -> None:
        self.n_decisions += 1
        did = f"{self.run_id}:d{self.n_decisions:04d}"
        obs = self._obs(t, did)
        try:
            decision = self.decide(copy.deepcopy(obs))
        except Exception as exc:  # noqa: BLE001
            raise CallbackError("decide", exc) from exc
        if len(self.decisions) < self.max_decisions:
            self.decisions.append(copy.deepcopy(decision) if isinstance(decision, dict) else {"invalid": repr(decision)})
        else:
            self.decisions_dropped += 1
        self.executor.handle_decision(decision, obs, t)
        self.executor.process_due(t)

    def _advance(self, t0: float, t1: float) -> None:
        whale_pieces = [(w, w.pieces(t0, t1)) for w in self.whales]
        for ship in self._active():
            pieces, notes = ship.advance(t0, t1)
            self.tracker.evaluate(ship, pieces, whale_pieces)
            legs = [p.leg() for p in pieces if p.tb > p.ta]
            if legs and not self.weather_unknown:
                self._weather(ship, legs, t0)
            for kind, tn, details in notes:
                self._emit(tn, kind, ship.ship_id, details)
                if kind == "VOYAGE_COMPLETE":
                    self.tracker.close_ship(ship.ship_id, tn)

    def _weather(self, ship: Ship, legs: List[Dict[str, Any]], t: float) -> None:
        try:
            val = self.weather_fn(legs, copy.deepcopy(self.weather))
        except ValueError as exc:
            if C.UNKNOWN_WEATHER in str(exc):
                self.weather_unknown = True
                self._emit(t, C.UNKNOWN_WEATHER, ship.ship_id, {"legs": len(legs)})
                return
            raise CallbackError("weather_exposure", exc) from exc
        except Exception as exc:  # noqa: BLE001
            raise CallbackError("weather_exposure", exc) from exc
        if not isinstance(val, (int, float)) or not math.isfinite(val) or val < 0:
            raise CallbackError("weather_exposure", ValueError(f"invalid exposure {val!r}"))
        self.weather_by_ship[ship.ship_id] = self.weather_by_ship.get(ship.ship_id, 0.0) + float(val)

    def _next_time(self, t: float, next_decision: float) -> float:
        cands = [(math.floor(t / self.dt + T_EPS) + 1) * self.dt, next_decision, self.sensors.next_sample_s(t),
                 self.end_s]
        cands += [s.entry_s for s in self.ships.values() if s.status == PENDING]
        for extra in (self.sensors.next_release_s(), self.executor.next_event_s(t)):
            if extra is not None:
                cands.append(extra)
        return min(c for c in cands if c > t + T_EPS)

    # --- main loop ---------------------------------------------------------------------------------
    def run(self) -> Dict[str, Any]:
        t, next_decision, next_frame = 0.0, 0.0, 0.0
        try:
            while True:
                self._spawn_due(t)
                self.executor.process_due(t)
                if self.sensors.is_sample_time(t):
                    self.sensors.sample(t, self._active(), self.whales)
                self.sensors.release(t)
                if t >= next_decision - T_EPS:
                    if self._active():
                        self._decide(t)
                    next_decision = (math.floor(t / self.decision_dt + T_EPS) + 1) * self.decision_dt
                if t >= next_frame - T_EPS:
                    self._frame(t)
                    next_frame = (math.floor(t / self.frame_every + T_EPS) + 1) * self.frame_every
                if not any(s.status in (PENDING, ACTIVE) for s in self.ships.values()):
                    break
                if t >= self.end_s - T_EPS:
                    self.status_reasons.append(DRAIN_LIMIT)
                    break
                if time.monotonic() - self.wall0 > self.wall_limit:
                    self.status_reasons.append(WALL_LIMIT)
                    self._emit(t, WALL_LIMIT, None, {"wall_limit_s": self.wall_limit})
                    break
                t_next = self._next_time(t, next_decision)
                self._advance(t, t_next)
                t = t_next
        except CallbackError as exc:
            self.error = exc
            self._emit(t, CALLBACK_ERROR, None, {"callback": exc.callback, "error": str(exc)})
        if not self.frames or self.frames[-1]["sim_time_s"] != t:
            self._frame(t)
        return self._result(t)

    def _result(self, t_end: float) -> Dict[str, Any]:
        for s in self._active():
            self.tracker.close_ship(s.ship_id, t_end)
        ships = sorted(self.ships.values(), key=lambda s: (s.entry_s, s.ship_id))
        voyages = [M.voyage_metrics(s, None if self.weather_unknown else self.weather_by_ship.get(s.ship_id, 0.0),
                                    self.tracker.proxy_by_ship.get(s.ship_id, 0.0),
                                    self.tracker.contacts_by_ship.get(s.ship_id, 0),
                                    self.tracker.approaches_by_ship.get(s.ship_id, 0), knots_to_mps)
                   for s in ships]
        failed = [s for s in ships if s.status == FAILED]
        unfinished = [s for s in ships if s.status in (PENDING, ACTIVE)]
        reasons = list(self.status_reasons)
        reasons += sorted({f"VOYAGE_FAILED:{s.fail_reason}" for s in failed})
        reasons += sorted({vi["reason"] for vi in self.voyage_issues})
        if unfinished:
            reasons.append("VOYAGES_UNFINISHED")
        if self.weather_unknown:
            reasons.append(C.UNKNOWN_WEATHER)
        if self.error is not None:
            status = C.RUN_ERROR
            reasons.insert(0, f"{CALLBACK_ERROR}:{self.error.callback}")
        else:
            status = C.RUN_INCOMPLETE if reasons else C.RUN_COMPLETE
        all_done = not failed and not unfinished and not self.voyage_issues
        ex = self.executor.counters
        summary = _summary(
            status, time.monotonic() - self.wall0, t_end,
            voyages_completed=sum(1 for s in ships if s.status == COMPLETE), voyages_failed=len(failed),
            modeled_contacts=self.tracker.contacts, close_approaches=self.tracker.close_approaches,
            whale_exposure_proxy=self.tracker.proxy_total,
            weather_exposure_s=None if self.weather_unknown else sum(self.weather_by_ship.values()),
            added_delay_s=sum(v["added_delay_s"] for v in voyages) if all_done else None,
            extra_distance_m=sum(v["extra_distance_m"] for v in voyages) if all_done else None,
            alerts=ex["alerts"], route_changes=ex["route_changes"], rejected_actions=ex["rejected_actions"])
        source = self.bundle.get("source") or {}
        manifest = {
            "run_id": self.run_id, "schema_version": C.SCHEMA_VERSION, "mode": MODE,
            "status": status, "status_reasons": reasons, "issues": list(self.issues),
            "seed": self.seed, "scenario_id": self.inputs["scenario"].get("scenario_id"),
            "start_at": self.inputs["scenario"]["start_at"], "duration_s": self.duration_s,
            "fixture_only": bool(self.inputs.get("fixture_only") or source.get("fixture_only")
                                 or (self.land or {}).get("fixture_only")),
            "route_bundle": {"bundle_id": self.bundle.get("bundle_id"), "sha256": canonical_sha256(self.bundle),
                             "source": copy.deepcopy(source), "issues": list(self.bundle.get("issues") or []),
                             "routes_used": {rid: {"voyages": vids, "reused": len(vids) > 1,
                                                   "waypoints_sha256": canonical_sha256(
                                                       usable_routes(self.bundle)[rid]["waypoints"])}
                                             for rid, vids in sorted(self.route_use.items())}},
            "land": {"map_id": (self.land or {}).get("map_id"), "sha256": (self.land or {}).get("sha256"),
                     "validated": bool((self.land or {}).get("validated")),
                     "fixture_only": (self.land or {}).get("fixture_only"),
                     "issues": list((self.land or {}).get("issues") or []),
                     "dict_sha256": canonical_sha256(self.land)},
            "config_sha256": canonical_sha256(self.inputs["config"]),
            "weather": copy.deepcopy(self.weather),
            "voyages": voyages, "voyage_issues": copy.deepcopy(self.voyage_issues),
            "counters": dict(ex, **self.sensors.counters, decisions=self.n_decisions,
                             decisions_dropped=self.decisions_dropped, events_dropped=self.events_dropped,
                             frames=len(self.frames), frame_every_s=self.frame_every,
                             open_episodes_censored=sum(1 for e in self.events if e["type"] == "ENCOUNTER_CENSORED")),
            "assumptions": ["Ships, whales, sightings and weather are simulated; AIS supplies baseline geometry only.",
                            "Instantaneous turns/speed changes; enclosing-circle hull and whale bodies.",
                            "whale_exposure_proxy is a smooth diagnostic, not expected deaths.",
                            "No ship-ship, legality, depth or realistic-dynamics checks (F11-F14)."],
        }
        self.events.sort(key=lambda e: e["sim_time_s"])
        return {"manifest": manifest, "summary": summary, "frames": self.frames, "events": self.events,
                "decisions": self.decisions}
