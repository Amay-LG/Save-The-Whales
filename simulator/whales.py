"""Synthetic whales: two-state random walk + dive cycle, independent of ships (§5.3)."""
import random
from dataclasses import dataclass

SURFACE, SHALLOW, DEEP = "SURFACE", "SHALLOW", "DEEP"


@dataclass
class Whale:
    whale_id: int
    x: float
    y: float
    heading: float
    behaviour: str = "foraging"   # foraging | transiting
    dive_state: str = DEEP
    dive_left_min: float = 0.0
    rng: random.Random = None


class WhalePopulation:
    def __init__(self, cfg, seed, density, land):
        self.cfg, self.density, self.land = cfg, density, land
        self.whales = []   # TODO: sample n initial positions from density; rng = Random(f"{seed}:whale:{i}")

    def availability(self, w: Whale) -> float:
        return {SURFACE: 1.0, SHALLOW: self.cfg["whales"]["p_shallow"], DEEP: 0.0}[w.dive_state]

    def positions(self):
        return {w.whale_id: (w.x, w.y) for w in self.whales}

    def step(self, dt_min):
        """Per whale: switch behaviour, turn, move, drift up density gradient,
        reflect off land/bbox, advance dive semi-Markov chain. Must NOT read ship state."""
        raise NotImplementedError

    def grid(self, cell_km=5.0):
        """Rebuilt each tick: {(ix, iy): [whale_id]}. Returns object with query(bbox) -> ids."""
        raise NotImplementedError
