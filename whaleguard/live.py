"""Read-only Acartia adapter. Report receipt age is not whale-position freshness."""
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import re
from urllib.request import Request, urlopen

from .routing import Zone

URL = 'https://acartia.io/api/v1/sightings/current'
MAX_BYTES = 4_000_000


@dataclass(frozen=True)
class Sighting:
    id: str
    species: str
    latitude: float
    longitude: float
    received: datetime
    observed_raw: str


def fetch_reports():
    request = Request(URL, headers={'Accept': 'application/json', 'User-Agent': 'WhaleGuard-Hackathon-Simulation/1.0'})
    with urlopen(request, timeout=10) as response:
        payload = response.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise ValueError('Feed response exceeds size limit')
    data = json.loads(payload)
    if not isinstance(data, list):
        raise ValueError('Expected an Acartia report list')
    return data


def parse_reports(data, now, max_age_hours=6):
    """Use explicit UTC ingestion dates. Never invent a timezone for `created`."""
    if not isinstance(data, list):
        raise ValueError('Expected a report list')
    unique = {}
    for row in data:
        try:
            if not isinstance(row, dict) or row.get('trusted') not in (1, True):
                continue
            species = str(row['type'])[:80]
            if not re.search(r'whale|orca|humpback|beluga|bowhead', species, re.I):
                continue
            lat, lon = float(row['latitude']), float(row['longitude'])
            if not math.isfinite(lat + lon) or not (-85 < lat < 85 and -180 <= lon <= 180):
                continue
            stamp = row['ssemmi_date_added'].split(' (')[0]
            try:
                received = datetime.strptime(stamp, '%a %b %d %Y %H:%M:%S GMT%z')
            except ValueError:
                received = parsedate_to_datetime(stamp)
            if received.tzinfo is None:
                continue
            received = received.astimezone(timezone.utc)
            age = (now - received).total_seconds()
            if not 0 <= age <= max_age_hours * 3600:
                continue
            sid = str(row['ssemmi_id'])[:120]
            if not sid:
                continue
            sighting = Sighting(sid, species, lat, lon, received, str(row.get('created', 'unknown'))[:80])
            if sid not in unique or unique[sid].received < received:
                unique[sid] = sighting
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return sorted(unique.values(), key=lambda s: (s.received, s.id), reverse=True)


class LiveFeed:
    """Fixed frame after first report; simulated buffers expire by wall-clock age.

    Cache accepted observations until expiry, including during outages. Missing
    records in a later response do not prematurely erase an existing buffer.
    """
    def __init__(self, max_age_hours=6, radius_km=2, speed_kmh=6):
        self.max_age_hours = max_age_hours
        self.radius_km, self.speed_kmh = radius_km, speed_kmh
        self.center = None
        self.reports = {}
        self.status = 'connecting'
        self.last_success = None
        self.error = None
        self.total_received = 0

    def update(self, data, now):
        parsed = parse_reports(data, now, self.max_age_hours)
        self.total_received = len(data)
        for report in parsed:
            previous = self.reports.get(report.id)
            if previous is None or previous.received <= report.received:
                self.reports[report.id] = report
        if self.center is None and parsed:
            self.center = (parsed[0].latitude, parsed[0].longitude)
        self.status, self.last_success, self.error = 'connected', now.isoformat(), None
        self.expire(now)

    def fail(self):
        self.status = 'unavailable'
        self.error = 'Fetch failed. Retaining accepted reports until their configured expiry.'

    def expire(self, now):
        self.reports = {k: r for k, r in self.reports.items()
                        if 0 <= (now - r.received).total_seconds() <= self.max_age_hours * 3600}

    def xy(self, report):
        lat0, lon0 = self.center
        return (15 + 6371 * math.radians(report.longitude - lon0) * math.cos(math.radians(lat0)),
                9 + 6371 * math.radians(report.latitude - lat0))

    def nearby(self):
        if self.center is None:
            return []
        out = []
        for report in sorted(self.reports.values(), key=lambda r: r.id):
            x, y = self.xy(report)
            r = self.radius_km
            if -r <= x <= 30 + r and -r <= y <= 18 + r:
                out.append((report, x, y))
        return out

    def zones(self):
        return [Zone(f'{report.species} · simulated buffer',
                     tuple((x + self.radius_km * math.cos(i * math.tau / 12),
                            y + self.radius_km * math.sin(i * math.tau / 12)) for i in range(12)),
                     self.speed_kmh) for report, x, y in self.nearby()]

    def snapshot(self, now):
        return {'provider': 'Acartia', 'source_url': URL, 'status': self.status,
                'last_success': self.last_success, 'error': self.error, 'center': self.center,
                'receipt_max_age_hours': self.max_age_hours, 'total_received': self.total_received,
                'radius_km': self.radius_km, 'speed_kmh': self.speed_kmh,
                'reports': [{'id': r.id, 'species': r.species, 'position': (x, y),
                             'received_utc': r.received.isoformat(),
                             'receipt_age_min': max(0, (now - r.received).total_seconds() / 60),
                             'observed_raw': r.observed_raw} for r, x, y in self.nearby()]}
