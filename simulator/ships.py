"""Voyage schedule, persistent fleet, waypoint kinematics (§5.4)."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

XY = Tuple[float, float]


@dataclass
class Voyage:
    voyage_id: str
    ship_id: str
    ship_class: str          # container | tanker | bulk
    length_m: float
    beam_m: float
    service_speed_kn: float
    entry_time: datetime
    route: List[XY]          # projected lane waypoints, entry -> exit gate
    complies: bool


@dataclass
class Segment:
    p0: XY
    p1: XY
    speed_kn: float
    t0_frac: float           # sub-segment start/end as fraction of the tick
    t1_frac: float


@dataclass
class Ship:
    voyage: Voyage
    pos: XY
    route: List[XY]          # remaining waypoints (may include a detour)
    speed_kn: float
    command: Optional[dict] = None
    sailed_nm: float = 0.0
    spawned_at: Optional[datetime] = None

    @property
    def beam_m(self):
        return self.voyage.beam_m


def build_schedule(cfg, seed, lanes) -> List[Voyage]:
    """Poisson arrivals per lane direction; class/speed/beam/complies drawn from Random(f'{seed}:voyages')."""
    raise NotImplementedError


class Fleet:
    def __init__(self, world):
        self.world = world
        self.active: dict = {}       # ship_id -> Ship
        self._queue: list = []       # pending (effective_at, ship_id, command)

    def spawn_due(self, voyages, t): raise NotImplementedError
    def submit(self, command, t, ledger): raise NotImplementedError
    def apply_due_commands(self, t): raise NotImplementedError   # lag, compliance, expiry -> rejoin

    def step(self, dt_min) -> List[Tuple[Ship, Segment]]:
        """Move each ship min(commanded, service) speed; split at waypoints, carry remainder (M7)."""
        raise NotImplementedError

    def despawn_arrived(self, t, ledger): raise NotImplementedError  # ledger.record_voyage(...)
