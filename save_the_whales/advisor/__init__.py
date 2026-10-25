"""Person 3 integration API: pure, deterministic numerical callbacks."""
from .planner import make_decider
from .constraints import make_validator
from .weather import weather_exposure

__all__ = ["make_decider", "make_validator", "weather_exposure"]
