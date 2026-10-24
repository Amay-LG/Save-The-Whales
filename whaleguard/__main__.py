"""Run with python3 -m whaleguard; open http://127.0.0.1:8765."""
import argparse
from datetime import datetime, timezone
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time

from .simulation import Simulation
from .live import LiveFeed, fetch_reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--live", action="store_true", help="Poll Acartia whale reports; ship and constraints stay simulated")
    parser.add_argument("--poll-seconds", type=float, default=30, help="Live polling interval, minimum 10 seconds")
    parser.add_argument("--report-age-hours", type=float, default=6, help="Maximum report receipt age (not observation age)")
    parser.add_argument("--time-scale", type=float, default=None, help="Simulated seconds per wall second; defaults: live 10, demo 90")
    args = parser.parse_args()
    time_scale = args.time_scale if args.time_scale is not None else (10 if args.live else 90)
    if not all(math.isfinite(v) and v > 0 for v in (args.poll_seconds, args.report_age_hours, time_scale)) or args.poll_seconds < 10:
        parser.error("Timing values must be positive and finite; polling must be at least 10 seconds")
    sim, lock, stop = Simulation(live=args.live), threading.Lock(), threading.Event()
    feed = LiveFeed(max_age_hours=args.report_age_hours) if args.live else None
    if feed:
        sim.status = "waiting"

    def sync_feed(now):
        feed.expire(now)
        sim.apply_live_zones(feed.zones())
        sim.feed_state = feed.snapshot(now)

    if feed:
        sync_feed(datetime.now(timezone.utc))
    page = (Path(__file__).parent / "static" / "index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/state":
                with lock:
                    state = sim.snapshot()
                    state["time_scale"] = time_scale
                    body = json.dumps(state, allow_nan=False).encode()
                mime = "application/json"
            elif self.path == "/":
                body, mime = page, "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    def run():
        last = time.monotonic()
        last_expiry = last
        while not stop.wait(0.05):
            now = time.monotonic()
            with lock:
                if feed and now - last_expiry >= 1:
                    sync_feed(datetime.now(timezone.utc))
                    last_expiry = now
                sim.advance((now - last) * time_scale / 3600)
            last = now

    def poll_feed():
        while not stop.is_set():
            try:
                data = fetch_reports()  # Network I/O never blocks the simulation lock.
                with lock:
                    feed.update(data, datetime.now(timezone.utc))
                    sync_feed(datetime.now(timezone.utc))
            except Exception:
                with lock:
                    feed.fail()
                    sync_feed(datetime.now(timezone.utc))
            if stop.wait(args.poll_seconds):
                break

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    poller = threading.Thread(target=poll_feed, daemon=True) if feed else None
    if poller:
        poller.start()
    print(f"WhaleGuard SIMULATION: http://127.0.0.1:{args.port}", flush=True)
    print("Live Acartia reports + SIMULATED ship/constraints." if feed else "Fictional timed-event demo.", flush=True)
    print("Autonomous voyage started. Restart this command to replay. Ctrl-C stops.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        worker.join()


if __name__ == "__main__":
    main()
