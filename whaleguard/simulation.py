"""Single simulated ship; deterministic movement with demo or live-derived zones."""
from dataclasses import asdict
import math

from .routing import Zone, SERVICE_SPEED, segment_parts, route_time, shortest_route, lerp


def rectangle(name, x0, y0, x1, y1, speed=None):
    return Zone(name, ((x0, y0), (x1, y0), (x1, y1), (x0, y1)), speed)


class Simulation:
    def __init__(self, live=False):
        self.live = live
        self.live_zones = []
        self.feed_state = None
        self.start, self.destination = (2, 9), (28, 9)
        self.position = self.start
        self.zones = [rectangle("Fictional island · blocked", 13, 8, 16, 12),
                      rectangle("Whale zone A · simulated", 5, 10, 9, 16, 12)]
        self.event_zone = rectangle("New whale zone B · simulated", 9, 0, 22, 9.5, 4)
        self.route = shortest_route(self.start, self.destination, self.zones)
        self.original_route = list(self.route)
        self.planned_route = list(self.route)
        self.old_route = []
        self.elapsed = 0.0
        self.event_at = 12 / 60  # hours of simulated time
        self.event_done = live
        self.status = "navigating"
        self.advisories = []
        self.impact = None
        self.baseline = route_time(self.route, self.zones)
        self.announce("WhaleGuard simulation initialized. Waiting for live whale reports. Ship WG-01 and all navigation constraints remain simulated." if live else f"WhaleGuard simulation underway. Simulated ship WG-01 is navigating automatically. Estimated voyage time {self.baseline * 60:.1f} simulated minutes.")

    def announce(self, text):
        self.advisories.append({"id": len(self.advisories) + 1, "minute": self.elapsed * 60, "text": text})

    def replan(self):
        self.event_done = True
        self.change_zones(self.zones + [self.event_zone],
                          "A fictional whale-sensitive zone now limits speed to 4 kilometres per hour.")

    def apply_live_zones(self, zones):
        if zones == self.live_zones:
            return
        fixed = self.zones[:len(self.zones) - len(self.live_zones)] if self.live_zones else self.zones
        self.live_zones = list(zones)
        self.change_zones(fixed + list(zones),
                          f"Whale reports updated. {len(zones)} simulated sighting buffers are active. "
                          "These buffers and speed limits are demonstration assumptions, not official restrictions.")

    def change_zones(self, zones, reason):
        before = route_time(self.route, self.zones) if self.status != "holding" else None
        self.old_route = list(self.route)
        self.zones = list(zones)
        if self.status == "arrived":
            return
        unchanged = route_time(self.old_route, self.zones)
        try:
            replacement = shortest_route(self.position, self.destination, self.zones)
        except ValueError:
            self.status = "holding"
            self.impact = None
            self.announce("Simulation advisory: no traversable route. Simulated ship is holding position.")
            return
        self.status = "navigating"
        self.route = replacement
        self.planned_route = list(replacement)
        after = route_time(self.route, self.zones)
        delay = (after - before) * 60 if before is not None and math.isfinite(before) else None
        saved = (unchanged - after) * 60 if math.isfinite(unchanged) else None
        self.impact = {"previous_remaining_min": before * 60 if before is not None and math.isfinite(before) else None,
                       "new_remaining_min": after * 60,
                       "old_route_new_limits_min": unchanged * 60 if math.isfinite(unchanged) else None,
                       "arrival_change_min": delay, "saved_vs_old_route_min": saved,
                       "replan_position": self.position, "at_min": self.elapsed * 60}
        timing = (f"Arrival is {abs(delay):.1f} simulated minutes {'later' if delay >= 0 else 'earlier'} than the previous estimate. "
                  if delay is not None else f"Estimated time to arrival is {after * 60:.1f} simulated minutes. ")
        comparison = (f"The new route saves {saved:.1f} simulated minutes compared with following the old route under the new speed limits."
                      if saved is not None else "The previous route is no longer traversable.")
        self.announce(f"Simulation advisory. {reason} Route automatically recalculated from the ship's current position. "
                      + timing + comparison)

    def move(self, hours):
        remaining = hours
        while remaining > 1e-12 and len(self.route) > 1:
            a, b = self.route[:2]
            parts = segment_parts(a, b, self.zones)
            if parts is None:
                self.status = "holding"
                self.announce("Simulation advisory: blocked segment detected. Holding position.")
                return hours - remaining
            reached = True
            for p, q, speed in parts:
                duration = math.dist(p, q) / speed
                if duration <= remaining + 1e-12:
                    self.position = q
                    remaining = max(0, remaining - duration)
                else:
                    self.position = lerp(p, q, remaining / duration)
                    remaining = 0
                    reached = False
                    break
            if reached:
                self.route.pop(0)
            else:
                self.route[0] = self.position
        return hours - remaining

    def advance(self, hours):
        if hours < 0:
            raise ValueError("Cannot reverse simulation time")
        while hours > 1e-12 and self.status == "navigating":
            if not self.event_done and self.elapsed >= self.event_at - 1e-12:
                self.replan()
                continue
            step = min(hours, self.event_at - self.elapsed) if not self.event_done else hours
            used = self.move(step)
            self.elapsed += used
            hours -= step
            if len(self.route) == 1:
                self.status = "arrived"
                self.announce(f"Simulation complete. WG-01 has arrived. Total simulated voyage time {self.elapsed * 60:.1f} minutes. All outcomes are fictional.")

    def snapshot(self):
        return {"mode": "live" if self.live else "demo", "live_feed": self.feed_state,
                "ship": "WG-01 · simulated", "position": self.position, "destination": self.destination,
                "start": self.start, "route": self.route, "old_route": self.old_route,
                "original_route": self.original_route, "planned_route": self.planned_route, "zones": [asdict(z) for z in self.zones],
                "elapsed_min": self.elapsed * 60, "remaining_min": route_time(self.route, self.zones) * 60 if self.status not in ("holding", "waiting") else None,
                "status": self.status, "event_done": self.event_done, "event_min": self.event_at * 60,
                "advisories": self.advisories, "impact": self.impact, "service_speed": SERVICE_SPEED}
