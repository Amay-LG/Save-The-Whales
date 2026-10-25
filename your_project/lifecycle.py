"""Recommendation lifecycle management.

Tracks active advisories, incidents, revisions, cooldowns, and valid_until
expiry for the whale-strike risk advisor system.
"""

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone, timedelta
from enum import Enum


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def _parse_iso(ts: str) -> datetime:
    """Parse ISO-8601 timestamp. Handle 'Z' suffix."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _format_iso(dt: datetime) -> str:
    """Format datetime to ISO-8601 with Z suffix."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class RecommendationState(Enum):
    """Lifecycle states for a recommendation."""
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    EXPIRED = "EXPIRED"
    RESOLVED = "RESOLVED"
    WITHDRAWN = "WITHDRAWN"


class DecisionOutcome(Enum):
    """Possible outcomes from the planning process."""
    ACTION_ISSUED = "ACTION_ISSUED"
    NO_FEASIBLE_IMPROVEMENT = "NO_FEASIBLE_IMPROVEMENT"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    MAINTAIN_CURRENT = "MAINTAIN_CURRENT"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ActiveRecommendation:
    """A currently active recommendation for a vessel."""
    recommendation_id: str
    revision: int
    ship_id: str
    action: str              # "SLOWDOWN", "DETOUR", "COMBINED", "RESUME"
    target_speed_kn: float | None
    waypoints_latlon: list[dict] | None   # for contract output
    issued_at: str           # ISO-8601
    valid_until: str         # ISO-8601
    risk_current: float      # risk score at time of issue
    risk_candidate: float    # risk score of recommended action
    evidence_ids: list[str] = field(default_factory=list)
    state: RecommendationState = RecommendationState.ACTIVE
    superseded_by: str | None = None


@dataclass
class Incident:
    """Groups related recommendations for one vessel over a continuous threat period."""
    incident_id: str
    ship_id: str
    created_at: str
    recommendations: list[str] = field(default_factory=list)  # recommendation_ids
    resolved: bool = False
    resolved_at: str | None = None


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class RecommendationManager:
    """Manages the lifecycle of recommendations, incidents, and cooldowns.

    Responsibilities:
    - Track active recommendations per vessel
    - Group recommendations into incidents
    - Manage cooldowns (minimum interval between recommendations for same vessel)
    - Handle valid_until expiry
    - Support recommendation revision (supersede old, create new revision)
    - Provide explicit NO_FEASIBLE_IMPROVEMENT / INSUFFICIENT_DATA outcomes
    """

    # Configuration
    DEFAULT_TTL_S: int = 3600           # 60 minutes default validity
    MAX_TTL_S: int = 21600              # 6 hours maximum validity
    MIN_COOLDOWN_S: int = 300           # 5 minutes minimum between recs for same vessel
    MIN_IMPROVEMENT_THRESHOLD: float = 0.05  # 5 % risk reduction needed to revise

    def __init__(self) -> None:
        self.active: dict[str, ActiveRecommendation] = {}    # ship_id -> active rec
        self.history: list[ActiveRecommendation] = []         # all past recommendations
        self.incidents: dict[str, Incident] = {}              # incident_id -> incident
        self.vessel_incidents: dict[str, str] = {}            # ship_id -> active incident_id
        self._cooldowns: dict[str, str] = {}                  # ship_id -> last_issued_at ISO
        self._next_rec_id: int = 0
        self._next_incident_id: int = 0

    # ------------------------------------------------------------------
    # ID generators
    # ------------------------------------------------------------------

    def _gen_rec_id(self) -> str:
        """Generate a monotonically increasing recommendation id."""
        rid = f"rec-{self._next_rec_id:04d}"
        self._next_rec_id += 1
        return rid

    def _gen_incident_id(self) -> str:
        """Generate a monotonically increasing incident id."""
        iid = f"inc-{self._next_incident_id:04d}"
        self._next_incident_id += 1
        return iid

    # ------------------------------------------------------------------
    # Expiry
    # ------------------------------------------------------------------

    def expire_stale(self, now: str) -> None:
        """Expire recommendations past their valid_until.

        - Set state to EXPIRED
        - Move from active to history
        - Do NOT auto-generate a RESUME — the spec says expiry triggers
          reassessment by the calling layer.
        """
        now_dt = _parse_iso(now)
        expired_ships: list[str] = []

        for ship_id, rec in self.active.items():
            valid_dt = _parse_iso(rec.valid_until)
            if now_dt >= valid_dt:
                rec.state = RecommendationState.EXPIRED
                self.history.append(rec)
                expired_ships.append(ship_id)

        for ship_id in expired_ships:
            del self.active[ship_id]

    # ------------------------------------------------------------------
    # Cooldown / gate checks
    # ------------------------------------------------------------------

    def can_issue(self, ship_id: str, now: str) -> tuple[bool, str]:
        """Check if we can issue a new recommendation for this vessel.

        Returns ``(allowed, reason_if_not)``.
        Blocked if cooldown hasn't elapsed since last recommendation for this
        ship.
        """
        last_iso = self._cooldowns.get(ship_id)
        if last_iso is None:
            return True, ""

        now_dt = _parse_iso(now)
        last_dt = _parse_iso(last_iso)
        elapsed = (now_dt - last_dt).total_seconds()

        if elapsed < self.MIN_COOLDOWN_S:
            remaining = self.MIN_COOLDOWN_S - elapsed
            return False, (
                f"Cooldown active for {ship_id}: {remaining:.0f}s remaining "
                f"(minimum {self.MIN_COOLDOWN_S}s between recommendations)"
            )
        return True, ""

    # ------------------------------------------------------------------
    # Revision decision
    # ------------------------------------------------------------------

    def should_revise(self, ship_id: str, new_risk: float,
                      new_candidate_risk: float) -> bool:
        """Determine if the existing active recommendation should be revised.

        Returns ``True`` if:
        - There is no active recommendation (new issue).
        - The new candidate offers ``MIN_IMPROVEMENT_THRESHOLD`` better
          reduction than the current active recommendation.
        - The situation has materially changed (large risk shift in the
          ambient risk compared to when the current recommendation was
          issued).
        """
        current = self.active.get(ship_id)
        if current is None:
            return True

        # Current reduction ratio (how much the active rec reduces risk)
        if current.risk_current > 0.0:
            current_reduction = (
                (current.risk_current - current.risk_candidate)
                / current.risk_current
            )
        else:
            current_reduction = 0.0

        # New candidate reduction ratio
        if new_risk > 0.0:
            new_reduction = (new_risk - new_candidate_risk) / new_risk
        else:
            new_reduction = 0.0

        # The new candidate must be meaningfully better
        if new_reduction - current_reduction >= self.MIN_IMPROVEMENT_THRESHOLD:
            return True

        # Material change in ambient risk (absolute shift > 20 %)
        if current.risk_current > 0.0:
            risk_shift = abs(new_risk - current.risk_current) / current.risk_current
            if risk_shift > 0.20:
                return True

        return False

    # ------------------------------------------------------------------
    # Issue / revise
    # ------------------------------------------------------------------

    def issue(
        self,
        ship_id: str,
        action: str,
        now: str,
        target_speed_kn: float | None = None,
        waypoints_latlon: list[dict] | None = None,
        valid_until: str | None = None,
        risk_current: float = 0.0,
        risk_candidate: float = 0.0,
        evidence_ids: list[str] | None = None,
    ) -> ActiveRecommendation:
        """Issue a new recommendation or revise an existing one.

        Algorithm:
        1. If there's an active recommendation for this ship:
           a. Supersede it (state=SUPERSEDED, superseded_by=new_id)
           b. Increment revision number
        2. Create new :class:`ActiveRecommendation`
        3. Compute ``valid_until`` if not provided
           (``now + DEFAULT_TTL_S``, capped at ``MAX_TTL_S``)
        4. Create or update the vessel's incident
        5. Update cooldown timestamp
        6. Return the new recommendation
        """
        new_id = self._gen_rec_id()
        revision = 0

        # 1. Supersede existing recommendation if present
        old = self.active.get(ship_id)
        if old is not None:
            old.state = RecommendationState.SUPERSEDED
            old.superseded_by = new_id
            revision = old.revision + 1
            self.history.append(old)

        # 3. Compute valid_until
        if valid_until is None:
            valid_until = self.format_valid_until(now)

        # 2. Create new recommendation
        rec = ActiveRecommendation(
            recommendation_id=new_id,
            revision=revision,
            ship_id=ship_id,
            action=action,
            target_speed_kn=target_speed_kn,
            waypoints_latlon=waypoints_latlon,
            issued_at=now,
            valid_until=valid_until,
            risk_current=risk_current,
            risk_candidate=risk_candidate,
            evidence_ids=evidence_ids if evidence_ids is not None else [],
            state=RecommendationState.ACTIVE,
            superseded_by=None,
        )
        self.active[ship_id] = rec

        # 4. Create or update incident
        if ship_id in self.vessel_incidents:
            inc_id = self.vessel_incidents[ship_id]
            incident = self.incidents[inc_id]
            # If the incident was previously resolved, start a new one
            if incident.resolved:
                inc_id = self._gen_incident_id()
                incident = Incident(
                    incident_id=inc_id,
                    ship_id=ship_id,
                    created_at=now,
                    recommendations=[new_id],
                )
                self.incidents[inc_id] = incident
                self.vessel_incidents[ship_id] = inc_id
            else:
                incident.recommendations.append(new_id)
        else:
            inc_id = self._gen_incident_id()
            incident = Incident(
                incident_id=inc_id,
                ship_id=ship_id,
                created_at=now,
                recommendations=[new_id],
            )
            self.incidents[inc_id] = incident
            self.vessel_incidents[ship_id] = inc_id

        # 5. Update cooldown
        self._cooldowns[ship_id] = now

        return rec

    # ------------------------------------------------------------------
    # Resolution / withdrawal
    # ------------------------------------------------------------------

    def resolve(self, ship_id: str, now: str) -> None:
        """Mark the active recommendation and incident as resolved.

        Called when the threat has passed or the vessel has exited the
        region.
        """
        rec = self.active.pop(ship_id, None)
        if rec is not None:
            rec.state = RecommendationState.RESOLVED
            self.history.append(rec)

        inc_id = self.vessel_incidents.get(ship_id)
        if inc_id is not None:
            incident = self.incidents.get(inc_id)
            if incident is not None and not incident.resolved:
                incident.resolved = True
                incident.resolved_at = now

    def withdraw(self, ship_id: str, now: str) -> None:
        """Withdraw an active recommendation.

        Different from expiry: this is an active decision by the advisor
        that the recommendation is no longer needed.
        """
        rec = self.active.pop(ship_id, None)
        if rec is not None:
            rec.state = RecommendationState.WITHDRAWN
            self.history.append(rec)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_active(self, ship_id: str) -> ActiveRecommendation | None:
        """Get the currently active recommendation for a vessel, if any."""
        return self.active.get(ship_id)

    def get_active_action_type(self, ship_id: str) -> str | None:
        """Get the action type of the active recommendation, or None."""
        rec = self.active.get(ship_id)
        if rec is not None:
            return rec.action
        return None

    def get_change_penalty(self, ship_id: str, proposed_action: str) -> float:
        """Compute a penalty for changing recommendation type (hysteresis).

        Returns 0.0 if no active recommendation or same action type.
        Returns a value in [0, 1] if switching action types
        (e.g. SLOWDOWN → DETOUR).

        Penalty scale:
        - RESUME ↔ any non-RESUME: 0.15  (mild — resuming/un-resuming is light)
        - SLOWDOWN ↔ DETOUR:       0.30  (moderate — different manoeuvre family)
        - SLOWDOWN ↔ COMBINED:     0.20
        - DETOUR   ↔ COMBINED:     0.20
        - Same action:             0.00
        """
        rec = self.active.get(ship_id)
        if rec is None:
            return 0.0

        current = rec.action
        if current == proposed_action:
            return 0.0

        pair = frozenset((current, proposed_action))

        # Define penalties for specific transitions
        penalty_map: dict[frozenset[str], float] = {
            frozenset(("SLOWDOWN", "DETOUR")): 0.30,
            frozenset(("SLOWDOWN", "COMBINED")): 0.20,
            frozenset(("DETOUR", "COMBINED")): 0.20,
        }

        # Any transition involving RESUME gets a mild penalty
        if "RESUME" in pair:
            return 0.15

        return penalty_map.get(pair, 0.25)  # fallback for unknown pairs

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    def format_valid_until(self, now: str, ttl_s: float | None = None) -> str:
        """Compute and format the valid_until timestamp.

        ``now + ttl_s`` (or ``DEFAULT_TTL_S``), capped at ``MAX_TTL_S``.
        Returns ISO-8601 string.
        """
        now_dt = _parse_iso(now)

        if ttl_s is None:
            ttl_s = float(self.DEFAULT_TTL_S)

        # Cap at MAX_TTL_S
        ttl_s = min(ttl_s, float(self.MAX_TTL_S))

        valid_dt = now_dt + timedelta(seconds=ttl_s)
        return _format_iso(valid_dt)

    # ------------------------------------------------------------------
    # Summary / metrics
    # ------------------------------------------------------------------

    def summary(self) -> dict:
        """Return summary statistics for metrics.

        Keys:
        - ``total_issued``: total recommendations ever issued
        - ``active_count``: currently active recommendations
        - ``expired_count``: recommendations that expired
        - ``superseded_count``: recommendations that were superseded
        - ``recommendations_per_vessel``: ``dict[ship_id, count]``
        """
        all_recs = list(self.active.values()) + self.history

        expired_count = sum(
            1 for r in all_recs if r.state == RecommendationState.EXPIRED
        )
        superseded_count = sum(
            1 for r in all_recs if r.state == RecommendationState.SUPERSEDED
        )

        per_vessel: dict[str, int] = {}
        for r in all_recs:
            per_vessel[r.ship_id] = per_vessel.get(r.ship_id, 0) + 1

        return {
            "total_issued": len(all_recs),
            "active_count": len(self.active),
            "expired_count": expired_count,
            "superseded_count": superseded_count,
            "recommendations_per_vessel": per_vessel,
        }
