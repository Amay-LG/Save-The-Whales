import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    raw: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path=ROOT / "config" / "default.json", **overrides):
        raw = json.loads(Path(path).read_text())
        raw.update(overrides)
        return cls(raw)

    def __getitem__(self, k):
        return self.raw[k]

    @property
    def start(self) -> datetime:
        return datetime.fromisoformat(self.raw["start_date"].replace("Z", "+00:00"))

    @property
    def duration(self) -> timedelta:
        return timedelta(days=30.4375 * self.raw["months"])

    @property
    def dt(self) -> timedelta:
        return timedelta(minutes=self.raw["dt_min"])

    @property
    def ticks_per_decision(self) -> int:
        return self.raw["decision_dt_min"] // self.raw["dt_min"]

    @property
    def sigma_km(self) -> float:
        return self.raw["whales"]["jitter_sigma_m"] / 1000

    @property
    def margin_km(self) -> float:
        """Grid query margin: 4σ + hull half-width + whale drift per tick (§5.5)."""
        drift = self.raw["whales"]["speed_kmh"]["transiting"] * self.raw["dt_min"] / 60
        return 4 * self.sigma_km + 0.05 + drift
