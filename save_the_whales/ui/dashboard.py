"""One Streamlit page, **AIS-Seeded Whale Simulation**, over an injected ``services`` mapping.

``render_dashboard(services)`` performs no calculation of its own. Production services are bound only in
``app.py``; ``tests/ui_preview.py`` injects explicitly labeled fixture services. Keys:

    mode_label, preview, config, paths{bundle, land, runs}
    start_capture(config) -> handle        handle.status() / snapshot_records() / stop()
    save_capture(records, path), load_capture(path)
    prepare_routes(records, land, config), save_route_bundle(bundle, path), load_route_bundle(path, land, config)
    load_land(path, config), geometry, make_decider, make_validator, weather_exposure, blocked_hits, run_scenario
    load_presets(config), land_override_path(settings), build_run_inputs(bundle, land, settings, config)
    readiness(bundle, land), run_pair(inputs) -> {inputs, baseline, advisor, comparison}, save_run(pair, dir)

Session state keeps one capture handle across reruns; the status panel only polls ``handle.status()``,
which never blocks. A run happens only when the scenario form is submitted; reruns redraw the stored
result. Capture updates never touch an already built ``RunInputs`` or result.
"""
from __future__ import annotations

import copy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from save_the_whales.shared import contracts as C
from save_the_whales.ui import plots

TITLE = "AIS-Seeded Whale Simulation"


def _st():
    import streamlit as st
    return st


# --- data loading (cached in session state; cheap, never networked) ------------------------------
def _load_static(services: Dict[str, Any]) -> None:
    st = _st()
    ss = st.session_state
    cfg = services["config"]
    if "land" not in ss:
        ss["land_path"] = services["paths"]["land"]
        ss["land"] = services["load_land"](ss["land_path"], cfg)
    if "bundle" not in ss:
        ss["bundle_path"] = services["paths"]["bundle"]
        ss["bundle"] = services["load_route_bundle"](ss["bundle_path"], ss["land"], cfg)


def _render_capture(services: Dict[str, Any]) -> None:
    st = _st()
    ss = st.session_state
    cfg = services["config"]
    st.subheader("AIS capture (real AISStream source)")
    handle = ss.get("capture")
    c1, c2, c3 = st.columns(3)
    if c1.button("Start capture", disabled=handle is not None and
                 handle.status().get("state") not in (C.CAPTURE_STOPPED, C.CAPTURE_ERROR)):
        ss["capture"] = handle = services["start_capture"](cfg)
    if c2.button("Stop capture", disabled=handle is None):
        handle.stop()
    prepare = c3.button("Prepare routes from capture", disabled=handle is None)
    status = handle.status() if handle is not None else None
    st.table(plots.capture_rows(status))
    for code in (status or {}).get("reason_codes") or []:
        st.warning(f"{code}: {plots.explain(code)}")
    if prepare and handle is not None:
        records = handle.snapshot_records()                 # copied records; the worker keeps running
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        data_dir = Path(services["paths"]["bundle"]).parent
        services["save_capture"](records, data_dir / f"capture_{stamp}.jsonl")
        bundle = services["prepare_routes"](records, ss["land"], cfg)
        path = data_dir / f"route_bundle_{stamp}.json"
        services["save_route_bundle"](bundle, path)
        ss["bundle_path"], ss["bundle"] = str(path), services["load_route_bundle"](path, ss["land"], cfg)
        st.info(f"Prepared {len(records)} records into {path.name}; the next run uses this frozen bundle.")


def _render_sources(services: Dict[str, Any]) -> Dict[str, Any]:
    st = _st()
    ss = st.session_state
    bundle, land = ss["bundle"], ss["land"]
    st.subheader("Route cache and land map")
    ov = plots.bundle_overview(bundle)
    label = "recorded real AIS" if plots.is_genuine_ais(bundle) else "SYNTHETIC FIXTURE (not real AIS)"
    st.markdown(f"**Route bundle** `{ov['bundle_id']}` ({label}) from `{Path(ss['bundle_path']).name}`: "
                f"{ov['routes_usable']}/{ov['routes_total']} usable routes, captured {plots.fmt(ov['captured_at'])}, "
                f"source age {plots.fmt(ov['source_age_h'], 3)} h, capture sha256 {plots.fmt(ov['capture_sha256'])}.")
    if bundle.get("routes"):
        st.dataframe(plots.route_rows(bundle), width="stretch")
    st.markdown(f"**Land map** `{land.get('map_id')}`: {land.get('source')}; validated={land.get('validated')}, "
                f"fixture_only={land.get('fixture_only')}, rings={len(land.get('rings') or [])}, "
                f"sha256 {plots.fmt((land.get('sha256') or '')[:12] or None)}.")
    ready = services["readiness"](bundle, land)
    for code in ready["reasons"]:
        (st.error if code in (C.INVALID_MAP, C.AIS_ROUTE_UNAVAILABLE) else st.warning)(
            f"{code}: {plots.explain(code)}")
    if ready["demo_ready"]:
        st.success("Real-data gate passed: API-verified AIS routes and a validated non-fixture land map.")
    else:
        st.caption("The real demonstration stays incomplete until the gate above passes; fixture runs only "
                   "exercise the code.")
    return ready


# --- scenario form -------------------------------------------------------------------------------
def _zone_inputs(st, key: str, z: Dict[str, Any]) -> Dict[str, Any]:
    c = st.columns(4)
    out = dict(z, kind="ROUTE_RELATIVE")
    out["from_fraction"] = c[0].slider("from (route fraction)", 0.0, 1.0, float(z.get("from_fraction", 0.35)), key=f"{key}:from")
    out["to_fraction"] = c[0].slider("to (route fraction)", 0.0, 1.0, float(z.get("to_fraction", 0.65)), key=f"{key}:to")
    out["side"] = c[1].selectbox("side", ["ACROSS", "LEFT", "RIGHT"],
                                 index=["ACROSS", "LEFT", "RIGHT"].index(z.get("side", "ACROSS")), key=f"{key}:side")
    out["near_m"] = c[1].number_input("near edge (m)", 0.0, 10000.0, float(z.get("near_m", 0.0)), key=f"{key}:near")
    out["far_m"] = c[2].number_input("far edge (m)", 10.0, 20000.0, float(z.get("far_m", 300.0)), key=f"{key}:far")
    out["severity"] = c[2].slider("severity", 0.0, 1.0, float(z.get("severity", 0.8)), key=f"{key}:sev")
    out["active_from_s"] = c[3].number_input("active from (s)", 0.0, 30000.0, float(z.get("active_from_s", 0.0)), key=f"{key}:a0")
    out["active_until_s"] = c[3].number_input("active until (s)", 0.0, 30000.0,
                                              float(z.get("active_until_s") or 3600.0), key=f"{key}:a1")
    out["blocked"] = c[3].checkbox("blocked (impassable)", bool(z.get("blocked", False)), key=f"{key}:blk")
    return out


def _scenario_form(services: Dict[str, Any], presets: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    st = _st()
    ss = st.session_state
    st.subheader("Scenario")
    pid = st.selectbox("Preset", list(presets), format_func=lambda k: presets[k].get("label", k), key="preset")
    base = presets[pid]
    st.caption(base.get("description", ""))
    route_ids = [r["route_id"] for r in ss["bundle"].get("routes") or [] if (r.get("validation") or {}).get("ok")]
    with st.form(f"scenario:{pid}"):
        k = f"{pid}:"
        c = st.columns(4)
        chosen = c[0].multiselect("Source route IDs (empty = all)", route_ids,
                                  [r for r in base.get("route_ids") or [] if r in route_ids], key=k + "routes")
        seed = c[1].number_input("Seed", 0, 10**6, int(base.get("seed", 0)), key=k + "seed")
        voyages = c[2].number_input("Voyages", 0, 10, int(base.get("voyages", 1)), key=k + "voy")
        spacing = c[2].number_input("Entry spacing (s)", 0, 7200, int(base.get("voyage_spacing_s", 600)), key=k + "sp")
        bg = c[3].number_input("Background whales", 0, 29, int(base.get("background_whales") or 0), key=k + "bg")
        dw = base.get("designated_whale") or {}
        has_whale = st.checkbox("Designated whale", bool(base.get("designated_whale")), key=k + "dw")
        c = st.columns(5)
        whale = {"at_fraction": c[0].slider("along route", 0.0, 1.0, float(dw.get("at_fraction", 0.5)), key=k + "wf"),
                 "lateral_m": c[1].number_input("lateral (m, + = left)", -20000.0, 20000.0, float(dw.get("lateral_m", 0.0)), key=k + "wl"),
                 "speed_mps": c[2].number_input("speed (m/s)", 0.0, 5.0, float(dw.get("speed_mps", 0.0)), key=k + "ws"),
                 "heading_rel_deg": c[3].number_input("heading vs route (deg, 90 = to left)", -180.0, 180.0,
                                                      float(dw.get("heading_rel_deg", 90.0)), key=k + "wh"),
                 "exposure": c[4].selectbox("exposure", ["ALWAYS", "CYCLE"],
                                            index=0 if dw.get("exposure", "ALWAYS") == "ALWAYS" else 1, key=k + "we"),
                 "species": dw.get("species", "humpback")}
        w = base.get("weather") or {"preset": "CLEAR"}
        wx = st.radio("Weather (simulated)", ["CLEAR", "STORM"], index=0 if w.get("preset") == "CLEAR" else 1,
                      horizontal=True, key=k + "wx")
        zones_in = list(w.get("zones") or []) or [{}]
        n_z = st.number_input("Storm zones", 1, int((services["config"].get("scenarios") or {}).get("max_storm_zones", 3)),
                              len(zones_in), key=k + "nz")
        zones = [_zone_inputs(st, f"{k}z{i}", zones_in[i] if i < len(zones_in) else {}) for i in range(int(n_z))]
        submitted = st.form_submit_button("Run NO_ACTION vs ADVISOR")
    if not submitted:
        return None
    settings = copy.deepcopy(base)
    settings.update({"route_ids": chosen, "seed": int(seed), "voyages": int(voyages), "voyage_spacing_s": int(spacing),
                     "background_whales": int(bg), "designated_whale": whale if has_whale else None,
                     "weather": {"preset": wx, "zones": zones if wx == "STORM" else []}})
    return settings


def _run(services: Dict[str, Any], settings: Dict[str, Any]) -> None:
    st = _st()
    ss = st.session_state
    cfg = services["config"]
    land, bundle = ss["land"], ss["bundle"]
    override = services["land_override_path"](settings)
    if override is not None:                     # fixture-only preset: recheck the frozen bundle on its toy map
        land = services["load_land"](override, cfg)
        bundle = services["load_route_bundle"](ss["bundle_path"], land, cfg)
    try:
        inputs = services["build_run_inputs"](bundle, land, settings, cfg)
    except ValueError as exc:
        ss["pair"], ss["run_error"] = None, getattr(exc, "codes", [str(exc)])
        return
    with st.spinner("Running both policies on the same frozen inputs..."):
        ss["pair"] = services["run_pair"](inputs)
    ss["run_error"] = None


# --- results -------------------------------------------------------------------------------------
def _deck(layers: List[Dict[str, Any]], center: Dict[str, float]):
    import pydeck as pdk
    out = []
    for spec in layers:
        common = {"id": spec["id"], "data": spec["data"], "pickable": True}
        if spec["kind"] == "polygon":
            out.append(pdk.Layer("PolygonLayer", get_polygon="polygon", get_fill_color=spec["color"],
                                 get_line_color=spec["color"][:3] + [255], stroked=True, **common))
        elif spec["kind"] == "path":
            width = 4 if spec["id"] in ("baseline", "baseline_fixture") else 3
            out.append(pdk.Layer("PathLayer", get_path="path", get_color=spec["color"], width_min_pixels=width,
                                 **common))
        else:
            out.append(pdk.Layer("ScatterplotLayer", get_position="position", get_radius="radius_m",
                                 get_fill_color=spec["color"], radius_min_pixels=3, **common))
    view = pdk.ViewState(latitude=center["lat"], longitude=center["lon"], zoom=center["zoom"])
    return pdk.Deck(layers=out, initial_view_state=view, map_style=None, tooltip={"text": "{name}"})


def _render_status(st, label: str, result: Dict[str, Any]) -> None:
    m = result.get("manifest") or {}
    status = (result.get("summary") or {}).get("status")
    reasons = m.get("status_reasons") or []
    text = f"{label}: {status}" + (f" ({', '.join(reasons)})" if reasons else "")
    {C.RUN_COMPLETE: st.success, C.RUN_INCOMPLETE: st.warning}.get(status, st.error)(text)
    for code in reasons:
        if plots.explain(code) != code:
            st.caption(f"{code}: {plots.explain(code)}")
    for code in m.get("issues") or []:
        st.error(f"{label} input issue {code}: {plots.explain(code)}")


def _render_results(services: Dict[str, Any]) -> None:
    st = _st()
    ss = st.session_state
    if ss.get("run_error"):
        st.error("Scenario settings could not be resolved: " + ", ".join(ss["run_error"]))
    pair = ss.get("pair")
    if not pair:
        st.info("No run yet. Choose a preset and press Run.")
        return
    cmp = pair["comparison"]
    st.subheader("Outcome comparison (simulated world)")
    if cmp["fixture_only"]:
        st.warning("fixture_only run: synthetic inputs exercise the code; this is not real-data acceptance evidence.")
    _render_status(st, "NO_ACTION", pair["baseline"])
    _render_status(st, "ADVISOR", pair["advisor"])
    for label, hits in (cmp.get("hard_constraint_violations") or {}).items():
        if hits:
            st.error(f"{label} violated a hard constraint: entered active blocked zone(s) "
                     f"{', '.join(sorted({h['zone_id'] for h in hits}))}")
    if not cmp["paired"]:
        st.error(f"Runs are not paired: {', '.join(cmp['pairing_mismatches'])}")
    st.dataframe(plots.comparison_rows(cmp), width="stretch")
    for note in cmp["notes"]:
        st.caption(note)

    st.subheader("Map")
    c = st.columns(3)
    policy = c[0].radio("Policy", ["ADVISOR", "NO_ACTION"], horizontal=True, key="map_policy")
    result = pair["advisor"] if policy == "ADVISOR" else pair["baseline"]
    n_frames = len(result.get("frames") or [])
    idx = c[1].slider("Frame", 0, max(0, n_frames - 1), max(0, n_frames - 1), key="frame") if n_frames > 1 else 0
    truth = c[2].checkbox("Analyst truth overlay (never advisor input)", False, key="truth")
    frame = plots.frame_at(result, idx)
    if frame:
        st.caption(f"t = {frame['sim_time_s']:.0f} s ({frame['now']}, virtual clock); "
                   f"{len(frame.get('whale_detections') or [])} sighting reports retained.")
        if not frame.get("whale_detections"):
            st.caption(plots.explain(C.INSUFFICIENT_FRESH_EVIDENCE))
    layers = plots.map_layers(pair["inputs"]["route_bundle"], pair["inputs"]["land"],
                              pair["inputs"]["scenario"]["weather"], result, idx, truth)
    st.pydeck_chart(_deck(layers, plots.view_center(pair["inputs"]["route_bundle"])))
    st.markdown("  \n".join(f"<span style='color:rgb({s['color'][0]},{s['color'][1]},{s['color'][2]})'>&#9632;</span> "
                            f"{s['label']}" for s in layers), unsafe_allow_html=True)

    st.subheader("Advisor alerts")
    alerts = plots.alert_rows(pair["advisor"])
    if alerts:
        st.dataframe(alerts, width="stretch")
    else:
        st.info("The advisor issued no actions in this run.")
    st.markdown("**Latest assessment per vessel**")
    latest = plots.assessment_rows(pair["advisor"])
    st.dataframe(latest, width="stretch")
    st.caption(f"Assessment statuses over the run: {plots.status_counts(pair['advisor'])}")
    for code in plots.flagged_codes(latest):
        st.warning(f"{code}: {plots.explain(code)}")
    if cmp["rejected_candidates_by_factor"]:
        st.markdown(f"**Rejected candidates by factor:** {cmp['rejected_candidates_by_factor']}")
    rejected = cmp["rejected_actions_by_reason"]["ADVISOR"]
    if rejected:
        st.markdown(f"**Actions rejected by the validator:** {rejected}")
    st.markdown("**Not modeled (excluded factors):** " + "; ".join(f"{k} {v}" for k, v in plots.UNMODELED_FACTORS))
    with st.expander("Command and voyage events"):
        st.dataframe(plots.event_rows(pair["advisor"], None), width="stretch")
    if st.button("Save run artifacts"):
        st.success(f"Saved to {services['save_run'](pair, services['paths']['runs'])}")


def render_dashboard(services: Dict[str, Any]) -> None:
    st = _st()
    st.set_page_config(page_title=TITLE, layout="wide")
    st.title(TITLE)
    mode = services.get("mode_label", "PRODUCTION")
    if services.get("preview"):
        st.warning(f"{mode}: injected test services and fixture data only. Nothing here is real AIS or a real run.")
    st.caption("Ships, whales, sightings and weather are simulated; recorded AIS supplies baseline route "
               "geometry only. No real vessel is controlled.")
    _load_static(services)
    with st.expander("AIS capture", expanded=False):
        _render_capture(services)
    _render_sources(services)
    presets = services["load_presets"](services["config"])
    settings = _scenario_form(services, presets)
    if settings is not None:
        _run(services, settings)
    _render_results(services)
