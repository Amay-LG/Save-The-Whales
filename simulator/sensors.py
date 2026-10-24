"""Observation layer: what the advisor may see (§5.6)."""
import hashlib


def stable_uniform(*key) -> float:
    """Counter-based U[0,1): identical across scenarios for the same key (§5.6, I2.6)."""
    h = hashlib.blake2b(repr(key).encode(), digest_size=8).digest()
    return int.from_bytes(h, "big") / 2 ** 64


class SensorSuite:
    def __init__(self, cfg, seed):
        self.cfg, self.seed = cfg, seed
        self.pending = []   # (reported_at, detection dict)

    def observe(self, t, tick, whales, fleet):
        """Visual MVP: SURFACE whale, daylight, within r_visual of a ship, stable_uniform < p_detect.
        Add sigma_obs position noise and U[0, latency_max] report delay; append to self.pending."""
        raise NotImplementedError

    def reported_by(self, t):
        """Detections with reported_at <= t and within the last window_h hours."""
        return [d for r, d in self.pending if r <= t]
