"""Bounded background AIS capture from AISStream (Architecture §4.2).

``start_capture(config)`` opens one websocket subscribed to the research bbox, normalizes position and
static messages into ``AISRecord`` dicts, and keeps them in a bounded deque. It never blocks the caller:
missing credentials or no coverage show up in ``handle.status()``. The API key comes from the
``AISSTREAM_API_KEY`` environment variable and is never logged, returned, or written to artifacts.

CLI (bounded capture to a JSONL cache)::

    python -m save_the_whales.sources.ais --seconds 900 --out data/ais/capture.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional

from save_the_whales.shared import contracts as C

AISSTREAM_URL = "wss://stream.aisstream.io/v0/stream"
ADAPTER_ID = "save_the_whales.sources.ais/1"
POSITION_TYPES = ("PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport")
STATIC_TYPES = ("ShipStaticData", "StaticDataReport")
KEY_ENV = "AISSTREAM_API_KEY"
REPO_ROOT = Path(__file__).resolve().parents[2]


# =====================================================================================================
# Time helpers
# =====================================================================================================
def utc_iso(dt: datetime) -> str:
    """UTC ISO-8601 with millisecond precision and a trailing Z."""
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (dt.microsecond // 1000)


def parse_utc(text: Any) -> Optional[datetime]:
    """Parse ISO-8601 or AISStream/Go-style ``2022-12-29 18:22:32.318353 +0000 UTC``; None if unparseable."""
    if not isinstance(text, str):
        return None
    m = re.match(r"^\s*(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?\s*(Z|[+-]\d{2}:?\d{2})?", text)
    if not m:
        return None
    day, hh, mm, ss, frac, tz = m.groups()
    try:
        dt = datetime.strptime(f"{day} {hh}:{mm}:{ss}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if frac:
        dt = dt.replace(microsecond=int(frac[:6].ljust(6, "0")))
    offset = timedelta(0)
    if tz and tz != "Z":
        sign = -1 if tz[0] == "-" else 1
        digits = tz[1:].replace(":", "")
        offset = sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    return (dt - offset).replace(tzinfo=timezone.utc)


def _reconcile_second(base: datetime, utc_second: Optional[int]) -> Optional[datetime]:
    """Place the AIS UTC-second field at or shortly before ``base`` (a full date-time).

    The AIS Timestamp is only 0-59 seconds of the minute in which the report was generated, so it is
    meaningful only next to a full date-time. Accept a reconciled time up to 30 s before (or 2 s after,
    for clock skew) ``base``; otherwise return None.
    """
    if utc_second is None:
        return None
    cand = base.replace(second=utc_second, microsecond=0)
    if cand > base + timedelta(seconds=2):
        cand -= timedelta(minutes=1)
    if timedelta(seconds=-2) <= base - cand <= timedelta(seconds=30):
        return cand
    return None


# =====================================================================================================
# Normalization
# =====================================================================================================
def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _lookup(d: Dict[str, Any], *names: str) -> Any:
    """Case-insensitive key lookup (AISStream metadata uses both ``latitude`` and ``Latitude``)."""
    if not isinstance(d, dict):
        return None
    for name in names:
        if name in d:
            return d[name]
    lowered = {str(k).lower(): v for k, v in d.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _dimensions(dim: Any) -> tuple:
    """AIS Dimension {A,B,C,D}: length = A+B, beam = C+D. 0 means unavailable; 511/63 mean 'at least'."""
    if not isinstance(dim, dict):
        return None, None
    a, b, c, d = (_num(_lookup(dim, k)) for k in ("A", "B", "C", "D"))
    length = a + b if a and b and a < 511 and b < 511 else None
    beam = c + d if c and d and c < 63 and d < 63 else None
    return length, beam


def _clean_sog(v: Any) -> Optional[float]:
    v = _num(v)
    return v if v is not None and 0 <= v < 102.2 else None   # 102.3 = not available


def _clean_cog(v: Any) -> Optional[float]:
    v = _num(v)
    return v if v is not None and 0 <= v < 360 else None     # 360 = not available


def _clean_heading(v: Any) -> Optional[float]:
    v = _num(v)
    return v if v is not None and 0 <= v < 360 else None     # 511 = not available


def normalize_message(raw: Any, received_at: datetime, receipt_seq: int,
                      bbox: Optional[Dict[str, float]] = None) -> Optional[Dict[str, Any]]:
    """Normalize one AISStream message into an ``AISRecord``; None when invalid or unsupported.

    POSITION records need finite in-bbox coordinates. Sentinels (SOG 102.3, COG 360, heading 511,
    UTC second >= 60, dimension 0) become None. ``observed_at`` uses the provider metadata time when
    present, else local receipt time, refined by the AIS UTC-second field when consistent; the choice is
    recorded in ``time_quality``.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    mtype = raw.get("MessageType")
    meta = raw.get("MetaData") or raw.get("Metadata") or {}
    body = (raw.get("Message") or {}).get(mtype) if isinstance(raw.get("Message"), dict) else None
    if mtype not in POSITION_TYPES + STATIC_TYPES or not isinstance(body, dict):
        return None

    mmsi_raw = _lookup(meta, "MMSI") or _lookup(body, "UserID")
    try:
        mmsi = str(int(mmsi_raw))
    except (TypeError, ValueError):
        return None
    if not 1 <= len(mmsi) <= 9:
        return None

    provider_time = parse_utc(_lookup(meta, "time_utc", "TimeUtc"))
    utc_second = None
    ts = _num(_lookup(body, "Timestamp"))
    if ts is not None and 0 <= ts <= 59 and ts == int(ts):
        utc_second = int(ts)
    base = provider_time or received_at
    reconciled = _reconcile_second(base, utc_second) if mtype in POSITION_TYPES else None
    if reconciled is not None:
        observed = reconciled
        quality = C.TIME_PROVIDER_AIS_SECOND if provider_time else C.TIME_RECEIPT_AIS_SECOND
    else:
        observed = base
        quality = C.TIME_PROVIDER if provider_time else C.TIME_RECEIPT_APPROX

    rec: Dict[str, Any] = {
        "record_type": "POSITION" if mtype in POSITION_TYPES else "STATIC",
        "mmsi": mmsi,
        "observed_at": utc_iso(observed),
        "received_at": utc_iso(received_at),
        "time_quality": quality,
        "provider": C.PROVIDER_AISSTREAM,
        "adapter": ADAPTER_ID,
        "message_type": mtype,
        "receipt_seq": int(receipt_seq),
        "lat": None, "lon": None, "sog_knots": None, "cog_deg": None, "heading_deg": None,
        "nav_status": None, "length_m": None, "beam_m": None, "ship_type": None, "ship_name": None,
    }
    name = _lookup(meta, "ShipName") or _lookup(body, "Name")
    if isinstance(name, str) and name.strip():
        rec["ship_name"] = name.strip()

    if mtype in POSITION_TYPES:
        if body.get("Valid") is False:
            return None
        lat = _num(_lookup(body, "Latitude"))
        lon = _num(_lookup(body, "Longitude"))
        if lat is None or lon is None:
            lat, lon = _num(_lookup(meta, "latitude")), _num(_lookup(meta, "longitude"))
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None   # also rejects 91/181 "not available" sentinels
        if bbox and not (bbox["lat_min"] <= lat <= bbox["lat_max"] and bbox["lon_min"] <= lon <= bbox["lon_max"]):
            return None
        rec.update(lat=lat, lon=lon, sog_knots=_clean_sog(_lookup(body, "Sog")),
                   cog_deg=_clean_cog(_lookup(body, "Cog")),
                   heading_deg=_clean_heading(_lookup(body, "TrueHeading")))
        status = _num(_lookup(body, "NavigationalStatus"))
        rec["nav_status"] = int(status) if status is not None and 0 <= status <= 15 else None
    else:
        report_b = _lookup(body, "ReportB") if mtype == "StaticDataReport" else None
        src = report_b if isinstance(report_b, dict) else body
        rec["length_m"], rec["beam_m"] = _dimensions(_lookup(src, "Dimension"))
        stype = _num(_lookup(src, "Type", "ShipType"))
        rec["ship_type"] = int(stype) if stype is not None and 1 <= stype <= 99 else None
        if mtype == "StaticDataReport" and not rec["ship_name"]:
            report_a = _lookup(body, "ReportA")
            nm = _lookup(report_a, "Name") if isinstance(report_a, dict) else None
            rec["ship_name"] = nm.strip() if isinstance(nm, str) and nm.strip() else None
        if rec["length_m"] is None and rec["beam_m"] is None and rec["ship_type"] is None:
            return None   # static message carries nothing usable
    return rec


# =====================================================================================================
# Capture handle
# =====================================================================================================
def _ais_config(config: Dict[str, Any]) -> Dict[str, Any]:
    ais = (config or {}).get("ais")
    if not isinstance(ais, dict):
        raise KeyError("config['ais'] is required (see config/default.json)")
    return ais


def _default_connect(url: str):
    """Return an async context manager for one websocket connection (per-message compression on)."""
    try:
        import websockets  # type: ignore
    except ImportError as exc:  # pragma: no cover - surfaced as a status reason
        raise RuntimeError(C.AIS_WEBSOCKET_CLIENT_MISSING) from exc
    return websockets.connect(url, open_timeout=15, close_timeout=5, max_size=2 ** 22,
                              compression="deflate")


class AISCaptureHandle:
    """Background AISStream capture. Thread-safe ``status()``/``snapshot_records()``/``stop()``."""

    def __init__(self, config: Dict[str, Any], api_key: Optional[str], *,
                 connect: Optional[Callable[[str], Any]] = None, url: str = AISSTREAM_URL):
        ais = _ais_config(config)
        self._bbox = dict(ais.get("bbox") or config.get("bbox") or C.REGION_BBOX)
        self._max_records = int(ais["max_records"])
        self._capture_max_s = float(ais["capture_max_s"])
        self._stale_after_s = float(ais["stale_after_s"])
        self._backoff_initial_s = float(ais["reconnect_initial_s"])
        self._backoff_max_s = float(ais["reconnect_max_s"])
        self._max_failures = int(ais["max_consecutive_failures"])
        self._message_types = list(ais["message_types"])
        self._key = api_key
        self._connect = connect or _default_connect
        self._url = url

        self._lock = threading.Lock()
        self._records: Deque[Dict[str, Any]] = deque(maxlen=self._max_records)
        self._mmsis_by_count: Dict[str, int] = {}
        self._state = C.CAPTURE_IDLE
        self._reasons: List[str] = []
        self._received = 0
        self._valid = 0
        self._last_received: Optional[datetime] = None
        self._api_verified = False
        self._seq = 0
        self._started = 0.0                       # monotonic seconds
        self._capturing_since: Optional[float] = None
        self._last_activity: Optional[float] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # --- public -------------------------------------------------------------------------------------
    def start(self) -> "AISCaptureHandle":
        if not self._key:
            self._set_state(C.CAPTURE_ERROR, [C.AIS_KEY_MISSING])
            return self
        self._set_state(C.CAPTURE_CONNECTING, [])
        self._started = time.monotonic()
        self._thread = threading.Thread(target=self._thread_main, name="ais-capture", daemon=True)
        self._thread.start()
        return self

    def status(self) -> Dict[str, Any]:
        with self._lock:
            reasons = list(self._reasons)
            state = self._state
            if state == C.CAPTURE_CAPTURING and self._silent_for_s() > self._stale_after_s:
                state = C.CAPTURE_DEGRADED
                reasons.append(C.AIS_NO_RECENT_MESSAGES)
            if self._valid > len(self._records):   # oldest records were dropped
                reasons.append(C.AIS_RETENTION_CAP_REACHED)
            return {
                "state": state,
                "reason_codes": sorted(set(reasons)),
                "records_received": self._received,
                "records_valid": self._valid,
                "vessels_tracked": len(self._mmsis_by_count),
                "last_received_at": utc_iso(self._last_received) if self._last_received else None,
                "api_verified": self._api_verified,
            }

    def snapshot_records(self) -> List[Dict[str, Any]]:
        """Copies of retained normalized records (never the worker's own dicts)."""
        with self._lock:
            return [dict(r) for r in self._records]

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
        with self._lock:
            if self._state not in (C.CAPTURE_ERROR,):
                self._state = C.CAPTURE_STOPPED

    # --- internals ----------------------------------------------------------------------------------
    def _silent_for_s(self) -> float:
        ref = self._last_activity or self._capturing_since
        return time.monotonic() - ref if ref else 0.0

    def _set_state(self, state: str, reasons: Iterable[str]) -> None:
        with self._lock:
            self._state = state
            self._reasons = list(reasons)

    def _subscription(self) -> str:
        b = self._bbox
        return json.dumps({
            "APIKey": self._key,
            # AISStream corners are [latitude, longitude].
            "BoundingBoxes": [[[b["lat_min"], b["lon_min"]], [b["lat_max"], b["lon_max"]]]],
            "FilterMessageTypes": self._message_types,
        })

    def _handle_raw(self, raw: Any) -> bool:
        """Process one frame; True only if it produced a valid record (errors don't count)."""
        now = datetime.now(timezone.utc)
        with self._lock:
            self._received += 1
            self._last_received = now
            self._last_activity = time.monotonic()
            self._seq += 1
            seq = self._seq
        try:
            parsed = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
        except (ValueError, UnicodeDecodeError, AttributeError):
            return False
        if isinstance(parsed, dict) and "error" in parsed and "MessageType" not in parsed:
            with self._lock:
                self._reasons = sorted(set(self._reasons) | {C.AIS_API_ERROR})
            return False
        rec = normalize_message(parsed, now, seq, self._bbox)
        if rec is None:
            return False
        with self._lock:
            if len(self._records) == self._records.maxlen:
                old = self._records[0]
                if old["record_type"] == "POSITION":
                    n = self._mmsis_by_count.get(old["mmsi"], 0) - 1
                    if n > 0:
                        self._mmsis_by_count[old["mmsi"]] = n
                    else:
                        self._mmsis_by_count.pop(old["mmsi"], None)
            self._records.append(rec)
            self._valid += 1
            self._api_verified = True
            if rec["record_type"] == "POSITION":
                self._mmsis_by_count[rec["mmsi"]] = self._mmsis_by_count.get(rec["mmsi"], 0) + 1
        return True

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception:  # never let the worker crash silently into an unknown state
            self._set_state(C.CAPTURE_ERROR, [C.AIS_CONNECTION_FAILED])

    def _deadline_passed(self) -> bool:
        return time.monotonic() - self._started >= self._capture_max_s

    async def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self._stop.is_set() and time.monotonic() < end and not self._deadline_passed():
            await asyncio.sleep(min(0.25, max(0.0, end - time.monotonic())))

    async def _run(self) -> None:
        failures, backoff = 0, self._backoff_initial_s
        while not self._stop.is_set():
            if self._deadline_passed():
                self._set_state(C.CAPTURE_STOPPED, [C.AIS_CAPTURE_LIMIT_REACHED])
                return
            got_message = False
            try:
                async with self._connect(self._url) as ws:
                    await ws.send(self._subscription())   # must arrive within 3 s of connecting
                    with self._lock:
                        self._state = C.CAPTURE_CAPTURING
                        self._reasons = [r for r in self._reasons if r == C.AIS_API_ERROR]
                        self._capturing_since = time.monotonic()
                    while not self._stop.is_set() and not self._deadline_passed():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue
                        if self._handle_raw(raw):
                            got_message = True
            except Exception:
                pass   # connection or stream failure; details may contain URLs/headers, so not logged
            if self._stop.is_set():
                return
            if self._deadline_passed():
                self._set_state(C.CAPTURE_STOPPED, [C.AIS_CAPTURE_LIMIT_REACHED])
                return
            failures = 0 if got_message else failures + 1
            backoff = self._backoff_initial_s if got_message else min(backoff * 2, self._backoff_max_s)
            if failures >= self._max_failures:
                self._set_state(C.CAPTURE_ERROR, [C.AIS_CONNECTION_FAILED])
                return
            self._set_state(C.CAPTURE_DEGRADED, [C.AIS_RECONNECTING])
            await self._sleep(backoff)


def start_capture(config: Dict[str, Any], *, connect: Optional[Callable[[str], Any]] = None,
                  url: str = AISSTREAM_URL) -> AISCaptureHandle:
    """Start a bounded background capture. Returns immediately; inspect ``handle.status()``.

    ``connect`` (tests only) replaces the websocket factory: ``connect(url)`` must return an async
    context manager yielding an object with ``send(str)`` and ``recv()``.
    """
    return AISCaptureHandle(config, os.environ.get(KEY_ENV), connect=connect, url=url).start()


# =====================================================================================================
# Capture cache files and CLI
# =====================================================================================================
def save_capture(records: List[Dict[str, Any]], path: Any) -> None:
    """Write normalized records as JSONL (atomic replace). Records never contain credentials."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n")
    os.replace(tmp, path)


def load_capture(path: Any) -> List[Dict[str, Any]]:
    """Read a JSONL capture written by ``save_capture``; malformed lines are skipped."""
    out: List[Dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def load_key_from_dotenv(path: Any = REPO_ROOT / ".env") -> bool:
    """Set AISSTREAM_API_KEY from a dotenv file if it isn't already in the environment.

    Only that one variable is read; its value is never printed or returned. Returns True if the
    variable is set afterwards.
    """
    if os.environ.get(KEY_ENV):
        return True
    path = Path(path)
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if not line.startswith(KEY_ENV) or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() != KEY_ENV:
            continue
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ[KEY_ENV] = value
            return True
    return False


def _load_repo_config() -> Dict[str, Any]:
    path = REPO_ROOT / "config" / "default.json"
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded AISStream capture to a JSONL cache.")
    parser.add_argument("--seconds", type=float, default=None,
                        help="capture duration (default: config ais.capture_max_s)")
    parser.add_argument("--out", required=True, help="output JSONL path, e.g. data/ais/capture.jsonl")
    parser.add_argument("--every", type=float, default=30.0, help="status/save interval in seconds")
    args = parser.parse_args(argv)

    config = _load_repo_config()
    if args.seconds is not None:
        config = dict(config, ais=dict(config["ais"], capture_max_s=args.seconds))
    load_key_from_dotenv()
    handle = start_capture(config)
    try:
        while True:
            st = handle.status()
            print(json.dumps({"t": utc_iso(datetime.now(timezone.utc)), **st}), flush=True)
            save_capture(handle.snapshot_records(), args.out)
            if st["state"] in (C.CAPTURE_STOPPED, C.CAPTURE_ERROR):
                break
            time.sleep(args.every)
    except KeyboardInterrupt:
        pass
    finally:
        handle.stop()
        save_capture(handle.snapshot_records(), args.out)
        print(json.dumps({"final": handle.status(), "out": str(args.out)}), flush=True)
    return 0 if handle.status()["api_verified"] else 1


if __name__ == "__main__":
    sys.exit(main())
