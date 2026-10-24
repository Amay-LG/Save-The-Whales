"""Demo: replay runs/<id>/trajectories.csv on a map + scoreboard from runs/results.json.
Run: streamlit run app.py"""
import json
from pathlib import Path

import streamlit as st

st.title("Save the Whales: Strike Risk Testbed")
res = Path("runs/results.json")
if not res.exists():
    st.info("Run `python run_comparison.py --quick` first.")
else:
    st.json(json.loads(res.read_text()))  # TODO: scoreboard table + strikes-vs-delay plot
# TODO: pydeck TripsLayer replay with a time slider over trajectories.csv
