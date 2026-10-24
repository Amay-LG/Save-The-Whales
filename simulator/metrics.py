"""Run ledger and summary (§8.2)."""
from dataclasses import asdict, dataclass, field


@dataclass
class Ledger:
    expected_lethal: float = 0.0
    expected_strikes: float = 0.0
    close_approaches: int = 0
    delay_h: float = 0.0
    extra_nm: float = 0.0
    fuel_proxy: float = 0.0
    voyages: int = 0
    accepted_commands: int = 0
    invalid_commands: int = 0
    ignored_commands: int = 0
    advisor_ms: list = field(default_factory=list)
    log: list = field(default_factory=list)

    def record_voyage(self, transit_h, ideal_h, sailed_nm, original_nm):
        self.voyages += 1
        self.delay_h += transit_h - ideal_h
        self.extra_nm += sailed_nm - original_nm

    def summary(self) -> dict:
        ms = sorted(self.advisor_ms) or [0.0]
        out = {k: v for k, v in asdict(self).items() if k not in ("advisor_ms", "log")}
        out["alerts_per_transit"] = self.accepted_commands / max(self.voyages, 1)
        out["advisor_ms_p50"] = ms[len(ms) // 2]
        out["advisor_ms_p95"] = ms[int(len(ms) * 0.95) - 1 if len(ms) > 1 else 0]
        return out
