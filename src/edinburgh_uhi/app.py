"""Streamlit + Folium dashboard for the Edinburgh Urban Heat Island & Green Space Optimizer.

Implements ``docs/ARCHITECTURE.md`` §7 as a dark-mode "command center": a map-first layout on
CartoDB Dark Matter, dark HTML hover cards, a ranked Top-K inspector and a matching Altair chart.
The app is deliberately thin: it reads only the small, pre-computed EPSG:4326 layers in
``data/processed/web/`` plus ``scoring_metadata.json`` and never touches raw rasters, Overpass or
the heavy pipeline modules. All geometry here is already in WGS84 for Leaflet; no reprojection
happens in this file.

This file only loads data and wires the page together; the presentation layer lives in
``edinburgh_uhi.ui`` (``theme``, ``components``, ``map_view``, ``panel``, ``state``, ``model``).

Run with: ``uv run streamlit run src/edinburgh_uhi/app.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

try:  # keep `streamlit run src/edinburgh_uhi/app.py` working without an installed package
    import edinburgh_uhi  # noqa: F401
except ImportError:  # pragma: no cover - depends on host environment
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
from typing import Any

import geopandas as gpd
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

from edinburgh_uhi import config
from edinburgh_uhi.ui import panel
from edinburgh_uhi.ui.map_view import MAP_HEIGHT_PX, build_map
from edinburgh_uhi.ui.model import rerank
from edinburgh_uhi.ui.state import (
    MAP_KEY,
    consume_map_click,
    init_state,
    model_from_state,
    selected_cell_id,
)
from edinburgh_uhi.ui.theme import inject_felt_css

st.set_page_config(
    page_title="Edinburgh Heat & Greening Optimizer",
    page_icon="🌳",
    layout="wide",
    initial_sidebar_state="collapsed",
)

METADATA_JSON = config.DATA_PROCESSED_DIR / "scoring_metadata.json"
WEB_FILES = {
    "grid": config.WEB_DIR / "grid_web.parquet",
    "parks": config.WEB_DIR / "parks_web.parquet",
    "corridors": config.WEB_DIR / "corridors_web.parquet",
    "zones": config.WEB_DIR / "zones_web.parquet",
}


# --------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------
def missing_files() -> list[Path]:
    """Return the required web/metadata files that do not exist."""
    return [p for p in (*WEB_FILES.values(), METADATA_JSON) if not p.exists()]


@st.cache_data(show_spinner=False)
def load_layers() -> dict[str, gpd.GeoDataFrame]:
    """Read the four EPSG:4326 web layers; ``cell_id`` and text columns become plain objects."""
    layers = {name: gpd.read_parquet(path) for name, path in WEB_FILES.items()}
    for layer in layers.values():
        for col in layer.columns:
            if col != "geometry" and pd.api.types.is_string_dtype(layer[col]):
                layer[col] = layer[col].astype(object).where(layer[col].notna(), None)
    return layers


@st.cache_data(show_spinner=False)
def load_metadata() -> dict[str, Any]:
    """Read ``scoring_metadata.json`` (KPIs, provenance, sensitivity)."""
    return json.loads(METADATA_JSON.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------
def main() -> None:
    """Render the dashboard.

    Order matters only for state: settings and the map selection are resolved from session state
    *before* anything is drawn, so the panel can render top to bottom in one pass. A map click
    reaches ``st.session_state["map"]`` (via st_folium's ``on_change``) before the rerun it
    triggers, so ``consume_map_click`` sees it on that rerun.
    """
    inject_felt_css()
    absent = missing_files()
    if absent:
        st.error(
            "Processed data not found:\n\n"
            + "\n".join(f"- `{p.relative_to(config.PROJECT_ROOT)}`" for p in absent)
            + "\n\nBuild it with:\n\n```bash\n"
            "uv run python -m edinburgh_uhi.ingest --step all\n"
            "uv run python -m edinburgh_uhi.spatial_grid\n"
            "uv run python -m edinburgh_uhi.scoring\n```"
        )
        st.stop()

    layers, meta = load_layers(), load_metadata()
    demo = str(st.query_params.get("demo", "0")) == "1"
    min_sep_m = float(meta["topk"]["min_separation_m"])

    init_state(st.session_state, demo)
    consume_map_click(st.session_state)
    model = model_from_state(st.session_state)
    scored, zones = rerank(layers["grid"], model.weights, model.k, min_sep_m)
    by_id = scored.set_index("cell_id", drop=False)
    selected = by_id.loc[selected_cell_id(st.session_state, by_id.index, zones.iloc[0]["cell_id"])]

    # One floating master panel (`position: fixed`) holds every control; the map fills the page.
    with st.container(key="felt_panel"):
        panel.render_hero(meta, model.weights, model.k)
        panel.render_kpis(meta, len(zones))
        panel.render_controls(meta, model)
        panel.render_inspector(selected, zones, model.weights)
        with st.expander("Ranked zones", expanded=False):
            panel.render_zone_table(zones)
        panel.render_footer()

    st_folium(
        build_map(scored, zones, layers, model.toggles),
        height=MAP_HEIGHT_PX,
        width=None,
        use_container_width=True,
        returned_objects=["last_active_drawing"],
        key=MAP_KEY,
    )


main()
