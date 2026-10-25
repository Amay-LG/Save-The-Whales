"""Explicit synthetic UI preview: streamlit run tests/ui_preview.py."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tests'))
from fixtures.ui.services import preview_services
from save_the_whales.ui.dashboard import render_dashboard
render_dashboard(preview_services())
