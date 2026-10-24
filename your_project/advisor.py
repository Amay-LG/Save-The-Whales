"""Advisor entry point (contract §6). Pure function of the observation."""
from typing import List

from contract.schema import Action, Observation
from your_project import detector, router


class Advisor:
    oracle = False

    def __init__(self):
        self.static = None  # cached on first call

    def advise(self, obs: Observation) -> List[Action]:
        self.static = self.static or obs["static"]
        threats = detector.find_threats(obs)
        return router.plan(obs, threats)
