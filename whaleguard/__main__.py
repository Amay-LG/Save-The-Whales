"""Run with python3 -m whaleguard; open http://127.0.0.1:8765."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time

from .simulation import Simulation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    sim, lock, stop = Simulation(), threading.Lock(), threading.Event()
    page = (Path(__file__).parent / "static" / "index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/state":
                with lock:
                    body = json.dumps(sim.snapshot(), allow_nan=False).encode()
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
        while not stop.wait(0.05):
            now = time.monotonic()
            with lock:
                sim.advance((now - last) * 90 / 3600)  # 1 wall second = 1.5 simulated minutes
            last = now

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    print(f"WhaleGuard SIMULATION: http://127.0.0.1:{args.port}", flush=True)
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
