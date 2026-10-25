"""Dashboard preview over explicitly injected TEST services (fixture_only). Never a real-data claim.

    .venv/bin/streamlit run tests/ui_preview.py -- --case incomplete

Cases: complete, incomplete, no_sightings, invalid_map, key_missing, no_routes, error
(see ``tests/fixtures/ui/ui_fixtures.py``). Production services are bound only in ``app.py``.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "tests" / "fixtures" / "ui"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from save_the_whales.ui.dashboard import render_dashboard  # noqa: E402
from ui_fixtures import CASES, preview_services  # noqa: E402


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", choices=CASES, default="complete")
    render_dashboard(preview_services(ap.parse_args(argv).case))


if __name__ == "__main__":
    main()
