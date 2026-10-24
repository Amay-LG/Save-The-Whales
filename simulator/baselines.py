"""Comparison advisors; all use the same contract as your_project (§8.1)."""


class NoAction:
    oracle = False

    def advise(self, obs):
        return []


class BlanketVSR:
    """SLOWDOWN 10 kn to ships in (or entering before next decision) the VSR zone; RESUME on exit."""
    oracle = False

    def __init__(self, vsr_zone, speed_kn=10.0):
        self.zone, self.speed = vsr_zone, speed_kn

    def advise(self, obs):
        raise NotImplementedError


class Oracle:
    """Wraps any advisor; engine feeds it perfect whale observations incl. dive state."""
    oracle = True

    def __init__(self, inner):
        self.inner = inner

    def advise(self, obs):
        return self.inner.advise(obs)
