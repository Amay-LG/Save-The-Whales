"""Command execution: ISSUE/APPLY validation, response lag, compliance, supersession, finite leases and
the expiry fallback. The executor owns queued/applied state; the injected validator owns the rules.

Lifecycle of an action from ``Decision.actions``:

1. Reject visibly: malformed entries, unknown/inactive ships, ``HOLD``, and more than one action for a
   ship in one decision. Previously accepted ``action_id``s are ignored idempotently (no revalidation).
2. ``validate_action(action, obs, purpose="ISSUE")``; on success the ship's ``latest_revision`` and
   ``last_action_at`` advance, older queued revisions are ``SUPERSEDED``, and the action is queued for
   ``now + response_lag_s``.
3. When due: cancel if superseded or its lease ended (``STALE_ACTION``); skip if the voyage's seeded
   compliance draw for this incident says it is ignored; otherwise revalidate with ``purpose="APPLY"``
   against the ship's current position and apply the returned ``validated_action``.
4. At ``valid_until``: on the baseline, restore service speed. Off-route, build a fallback from the
   unpassed path and stored rejoin at service speed and validate it with ``purpose="EXPIRY_FALLBACK"``;
   if that fails the voyage stops as ``EXPIRY_FALLBACK_FAILED`` (incomplete, never a shortcut).

Validated object shape (``ValidationResult.validated_action``)::

    {"action": <the submitted action, unmodified>, "remaining_waypoints": [Point, ...],
     "rejoin_route_index": int | None, "effective_target_speed_knots": float | None,
     "incident_budget": IncidentBudget | None}
"""
from __future__ import annotations

import copy
import math
from typing import Any, Callable, Dict, List, Optional

from save_the_whales.shared import contracts as C
from save_the_whales.simulation.ships import ACTIVE, VALIDATED_ACTION_MALFORMED, ExecutionReject, Ship
from save_the_whales.simulation.world import iso_at, seconds_since, stable_uniform

CONFLICTING_ACTIONS = "CONFLICTING_ACTIONS"
UNKNOWN_SHIP = "UNKNOWN_SHIP"
UNSUPPORTED_COMMAND = "UNSUPPORTED_COMMAND"
INVALID_DECISION = "INVALID_DECISION"
INVALID_VALIDATION_RESULT = "INVALID_VALIDATION_RESULT"
EXPIRY_FALLBACK_FAILED = "EXPIRY_FALLBACK_FAILED"
_IDENTITY = ("ship_id", "command", "action_id", "revision", "target_speed_knots", "detour_waypoints",
             "valid_until", "rejoin_route_index")


class CallbackError(Exception):
    """An injected callback raised; the run ends as ERROR with this diagnostic."""

    def __init__(self, name: str, exc: BaseException):
        super().__init__(f"{name}: {type(exc).__name__}: {exc}")
        self.callback, self.exc = name, exc


class CommandExecutor:
    def __init__(self, *, ships: Dict[str, Ship], validate: Callable[..., Dict[str, Any]],
                 make_obs: Callable[[float, str], Dict[str, Any]], emit: Callable[..., None],
                 on_fail: Callable[[Ship, float, str], None], response_cfg: Dict[str, Any],
                 seed: int, start_dt):
        self.ships, self.validate, self.make_obs, self.emit, self.on_fail = ships, validate, make_obs, emit, on_fail
        self.lag_s = float(response_cfg.get("response_lag_s", 0))
        self.compliance = float(response_cfg.get("compliance", 1.0))
        self.seed, self.start_dt = seed, start_dt
        self.pending: List[Dict[str, Any]] = []
        self.counters = {"alerts": 0, "route_changes": 0, "rejected_actions": 0, "applied": 0,
                         "ignored": 0, "superseded": 0, "expired_pending": 0, "duplicates": 0,
                         "lease_expiries": 0, "fallbacks": 0, "fallback_failures": 0, "no_feasible": 0}

    # --- helpers -----------------------------------------------------------------------------------
    def _call_validate(self, action: Dict[str, Any], obs: Dict[str, Any], purpose: str) -> Dict[str, Any]:
        try:
            return self.validate(copy.deepcopy(action), copy.deepcopy(obs), purpose=purpose)
        except Exception as exc:  # noqa: BLE001 - surfaced as an ERROR run, not swallowed
            raise CallbackError("validate_action", exc) from exc

    def _reject(self, t: float, ship_id: Optional[str], purpose: str, reasons: List[str],
                action: Any = None) -> None:
        self.counters["rejected_actions"] += 1
        aid = action.get("action_id") if isinstance(action, dict) else None
        self.emit(t, "ACTION_REJECTED", ship_id, {"purpose": purpose, "reasons": list(reasons), "action_id": aid})

    @staticmethod
    def _check_result(res: Any, action: Dict[str, Any]) -> Optional[str]:
        """None when ``res`` is a well-formed ValidationResult consistent with ``action``."""
        if not isinstance(res, dict) or not isinstance(res.get("ok"), bool) or not isinstance(res.get("reasons"), list):
            return INVALID_VALIDATION_RESULT
        if not res["ok"]:
            return None
        va = res.get("validated_action")
        if not isinstance(va, dict) or not isinstance(va.get("action"), dict):
            return VALIDATED_ACTION_MALFORMED
        if any(va["action"].get(k) != action.get(k) for k in _IDENTITY):
            return VALIDATED_ACTION_MALFORMED     # the validator may resolve, never rewrite
        return None

    def _compliant(self, ship: Ship, va: Dict[str, Any]) -> bool:
        """One seeded outcome per voyage incident, never per message: revisions of the applied incident
        reuse its draw, and anything else shares the next incident's draw until one is applied, so
        re-sending an ignored command cannot buy a fresh draw."""
        budget = va.get("incident_budget")
        same = isinstance(budget, dict) and ship.incident is not None \
            and budget.get("incident_id") == ship.incident.get("incident_id")
        ordinal = ship.incident_ordinal if same else ship.incidents_applied + 1
        return stable_uniform(self.seed, "compliance", ship.voyage_id, ordinal) < self.compliance

    # --- decisions ---------------------------------------------------------------------------------
    def handle_decision(self, decision: Any, obs: Dict[str, Any], t: float) -> None:
        if not isinstance(decision, dict) or not isinstance(decision.get("actions"), list) \
                or not isinstance(decision.get("assessments", []), list):
            self._reject(t, None, C.PURPOSE_ISSUE, [INVALID_DECISION])
            return
        for a in decision.get("assessments") or []:
            if isinstance(a, dict) and (a.get("status") == C.NO_FEASIBLE_ROUTE
                                        or C.NO_FEASIBLE_ROUTE in (a.get("reason_codes") or [])):
                ship = self.ships.get(a.get("ship_id"))
                if ship is not None and ship.status == ACTIVE:
                    self.counters["no_feasible"] += 1
                    self.on_fail(ship, t, C.NO_FEASIBLE_ROUTE)
        groups: Dict[Any, List[Any]] = {}
        for act in decision["actions"]:
            groups.setdefault(act.get("ship_id") if isinstance(act, dict) else None, []).append(act)
        for sid, acts in groups.items():
            if sid is None or not all(isinstance(a, dict) for a in acts):
                for a in acts:
                    self._reject(t, None, C.PURPOSE_ISSUE, [C.INVALID_ACTION], a)
                continue
            if len(acts) > 1:
                for a in acts:
                    self._reject(t, sid, C.PURPOSE_ISSUE, [C.INVALID_ACTION, CONFLICTING_ACTIONS], a)
                continue
            self._issue(acts[0], obs, t)

    def _issue(self, action: Dict[str, Any], obs: Dict[str, Any], t: float) -> None:
        sid = action["ship_id"]
        ship = self.ships.get(sid)
        if ship is None or ship.status != ACTIVE:
            self._reject(t, sid, C.PURPOSE_ISSUE, [C.INVALID_ACTION, UNKNOWN_SHIP], action)
            return
        if action.get("command") in C.UNSUPPORTED_COMMANDS or action.get("command") not in C.COMMANDS:
            self._reject(t, sid, C.PURPOSE_ISSUE, [C.INVALID_ACTION, UNSUPPORTED_COMMAND], action)
            return
        aid = action.get("action_id")
        if aid in ship.accepted_action_ids:
            self.counters["duplicates"] += 1
            self.emit(t, "ACTION_DUPLICATE_IGNORED", sid, {"action_id": aid})
            return
        res = self._call_validate(action, obs, C.PURPOSE_ISSUE)
        problem = self._check_result(res, action)
        if problem or not res["ok"]:
            self._reject(t, sid, C.PURPOSE_ISSUE, [C.INVALID_ACTION, problem] if problem else res["reasons"], action)
            return
        try:
            valid_until_s = seconds_since(self.start_dt, action["valid_until"])
        except (KeyError, TypeError, ValueError):
            self._reject(t, sid, C.PURPOSE_ISSUE, [C.INVALID_ACTION, "VALID_UNTIL_UNPARSEABLE"], action)
            return
        va = res["validated_action"]
        ship.accepted_action_ids.add(aid)
        ship.latest_revision = int(action["revision"])
        ship.last_action_at = iso_at(self.start_dt, t)
        self.counters["alerts"] += 1
        for p in [p for p in self.pending if p["ship_id"] == sid and p["revision"] < ship.latest_revision]:
            self.pending.remove(p)
            self.counters["superseded"] += 1
            self.emit(t, "ACTION_SUPERSEDED", sid, {"action_id": p["action"].get("action_id"),
                                                     "by_action_id": aid, "reasons": [C.SUPERSEDED]})
        self.pending.append({"ship_id": sid, "action": copy.deepcopy(action), "revision": ship.latest_revision,
                             "apply_at_s": t + self.lag_s, "valid_until_s": valid_until_s,
                             "compliant": self._compliant(ship, va)})
        self.emit(t, "ACTION_ACCEPTED", sid, {"action_id": aid, "command": action["command"],
                                              "revision": ship.latest_revision, "apply_at_s": t + self.lag_s})

    # --- timed processing --------------------------------------------------------------------------
    def next_event_s(self, t: float) -> Optional[float]:
        times = [p["apply_at_s"] for p in self.pending if p["apply_at_s"] > t + 1e-9]
        times += [s.active["valid_until_s"] for s in self.ships.values()
                  if s.status == ACTIVE and s.active and s.active["valid_until_s"] > t + 1e-9]
        return min(times, default=None)

    def process_due(self, t: float) -> None:
        due = sorted((p for p in self.pending if p["apply_at_s"] <= t + 1e-9),
                     key=lambda p: (p["apply_at_s"], p["ship_id"], p["revision"]))
        for p in due:
            self.pending.remove(p)
            self._apply(p, t)
        for ship in sorted(self.ships.values(), key=lambda s: s.ship_id):
            if ship.status == ACTIVE and ship.active and ship.active["valid_until_s"] <= t + 1e-9:
                self._expire(ship, t)

    def _apply(self, p: Dict[str, Any], t: float) -> None:
        sid, action = p["ship_id"], p["action"]
        ship = self.ships[sid]
        aid = action.get("action_id")
        if ship.status != ACTIVE:
            self.emit(t, "ACTION_CANCELLED", sid, {"action_id": aid, "reasons": ["SHIP_INACTIVE"]})
            return
        if p["revision"] != ship.latest_revision:
            self.counters["superseded"] += 1
            self.emit(t, "ACTION_SUPERSEDED", sid, {"action_id": aid, "reasons": [C.SUPERSEDED]})
            return
        if t >= p["valid_until_s"] - 1e-9:
            self.counters["expired_pending"] += 1
            self.emit(t, "ACTION_EXPIRED_PENDING", sid, {"action_id": aid, "reasons": [C.STALE_ACTION]})
            return
        if not p["compliant"]:
            self.counters["ignored"] += 1
            self.emit(t, "COMMAND_IGNORED", sid, {"action_id": aid, "reason": "SEEDED_NON_COMPLIANCE"})
            return
        res = self._call_validate(action, self.make_obs(t, "apply"), C.PURPOSE_APPLY)
        problem = self._check_result(res, action)
        if problem or not res["ok"]:
            self._reject(t, sid, C.PURPOSE_APPLY, [C.INVALID_ACTION, problem] if problem else res["reasons"], action)
            return
        try:
            changed = ship.apply(res["validated_action"], t, p["valid_until_s"], iso_at(self.start_dt, t))
        except ExecutionReject as exc:
            self._reject(t, sid, C.PURPOSE_APPLY, [r for r in (exc.code, exc.detail) if r], action)
            return
        self.counters["applied"] += 1
        if changed:
            self.counters["route_changes"] += 1
        self.emit(t, "COMMAND_APPLIED", sid, {"action_id": aid, "command": action["command"],
                                              "revision": p["revision"], "route_changed": changed,
                                              "rejoin_route_index": ship.rejoin_index,
                                              "speed_knots": ship.speed_knots})

    def _expire(self, ship: Ship, t: float) -> None:
        act = ship.active["action"]
        self.counters["lease_expiries"] += 1
        if not ship.off_route:
            ship.end_lease()
            self.emit(t, "LEASE_EXPIRED", ship.ship_id, {"action_id": act.get("action_id"),
                                                         "restored_speed_knots": ship.speed_knots})
            return
        detour = ship.unpassed_detour_points()
        fb = dict(copy.deepcopy(act), command=C.DETOUR if detour else C.RESUME, detour_waypoints=detour,
                  rejoin_route_index=ship.rejoin_index, target_speed_knots=ship.service_speed_knots,
                  action_id=f"{act.get('action_id')}:expiry-fallback", issued_at=iso_at(self.start_dt, t),
                  metadata={"executor": C.PURPOSE_EXPIRY_FALLBACK, "expired_action_id": act.get("action_id")})
        res = self._call_validate(fb, self.make_obs(t, "expiry"), C.PURPOSE_EXPIRY_FALLBACK)
        problem = self._check_result(res, fb)
        reasons = [problem] if problem else ([] if res["ok"] else res["reasons"])
        if not reasons:
            try:
                ship.apply(res["validated_action"], t, math.inf, iso_at(self.start_dt, t))
                ship.active = None       # no new lease: finish the validated path to the rejoin
            except ExecutionReject as exc:
                reasons = [r for r in (exc.code, exc.detail) if r]
        if reasons:
            self.counters["fallback_failures"] += 1
            self.emit(t, "EXPIRY_FALLBACK_FAILED", ship.ship_id, {"action_id": act.get("action_id"),
                                                                  "reasons": reasons})
            self.on_fail(ship, t, EXPIRY_FALLBACK_FAILED)
            return
        self.counters["fallbacks"] += 1
        self.emit(t, "EXPIRY_FALLBACK", ship.ship_id, {"action_id": act.get("action_id"),
                                                       "rejoin_route_index": ship.rejoin_index,
                                                       "speed_knots": ship.speed_knots})
