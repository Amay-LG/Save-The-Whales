"""Turn threats into actions: prefer SLOWDOWN; DETOUR only if land-safe."""
from typing import List

from contract.schema import Action


def plan(obs, threats) -> List[Action]:
    """TODO: per threatened ship, choose SLOWDOWN (target ~10 kn, valid_until ~ TCPA + margin)
    or DETOUR (waypoints off land, rejoin original_route). Return [] for no change."""
    return []
