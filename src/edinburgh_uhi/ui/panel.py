"""Streamlit renderers for the floating control panel.

Each function draws one block of the panel. The HTML and chart builders they call live in
``components`` (pure); the model settings they display come from ``state``.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from edinburgh_uhi.config import Weights
from edinburgh_uhi.ui.components import (
    CELL_AREA_HA,
    breakdown_chart,
    hero_html,
    inspector_html,
    zone_table,
)
from edinburgh_uhi.ui.state import ModelState, reset_model


def render_hero(meta: dict[str, Any], weights: Weights, k: int) -> None:
    """Title block with live model chips."""
    st.markdown(hero_html(meta, weights, k), unsafe_allow_html=True)


def render_kpis(meta: dict[str, Any], n_zones: int) -> None:
    """Four KPI cards (2 x 2), all values read from ``scoring_metadata.json``."""
    kpis, lst = meta["kpis"], meta.get("lst") or {}
    (c1, c2), (c3, c4) = st.columns(2, gap="small"), st.columns(2, gap="small")
    c1.metric(
        "Peak zone anomaly",
        f"+{kpis['peak_zone_anomaly_c']:.1f} °C",
        help="Median summer daytime surface-temperature anomaly of the hottest top-ranked cell, "
        "vs. a typical Edinburgh street.",
    )
    c2.metric(
        "Blocks > 200 m from park",
        f"{kpis['pct_urban_cells_beyond_doorstep']:.0f} %",
        help="Urban blocks more than 200 m from a ≥ 0.5 ha greenspace (Natural England's "
        "doorstep standard).",
    )
    c3.metric(
        "Recommended zones",
        f"{n_zones} · {n_zones * CELL_AREA_HA:.0f} ha",
    )
    scenes = lst.get("n_scenes_used")
    dates, air = lst.get("date_range"), lst.get("mean_air_tmax_c")
    help_txt = None
    if dates:
        help_txt = f"{dates[0]} to {dates[1]}" + (f"; mean air Tmax {air:.1f} °C" if air else "")
    c4.metric(
        "Clear-sky passes",
        "n/a" if scenes is None else str(scenes),
        help=help_txt,
    )


def render_zone_table(zones: pd.DataFrame) -> None:
    """Ranked Top-K table plus a stacked score-breakdown chart."""
    tab_table, tab_chart = st.tabs(["🏆 Ranking", "📊 Score breakdown"])
    with tab_table:
        table = zone_table(zones)
        st.dataframe(
            table,
            hide_index=True,
            column_order=["Rank", "Zone", "Score", "LST", "Gap"],
            width="stretch",
            height=35 * (len(table) + 1) + 3,
            column_config={
                "Rank": st.column_config.NumberColumn("#", width=36, format="%d"),
                "Zone": st.column_config.TextColumn("Zone", width=120),
                "Street": st.column_config.TextColumn("Street", width=160),
                "Score": st.column_config.ProgressColumn(
                    "Priority", min_value=0, max_value=100, format="%.0f", width="small"
                ),
                "LST": st.column_config.NumberColumn("LST Δ", format="%+.1f °C", width="small"),
                "Gap": st.column_config.NumberColumn("Park gap", format="%.0f m", width="small"),
                "Sealed": st.column_config.NumberColumn("Impervious", format="%.0f %%"),
                "Action": st.column_config.TextColumn("Intervention"),
            },
        )
    with tab_chart:
        st.altair_chart(breakdown_chart(zones), width="stretch", theme=None)


def render_inspector(selected: pd.Series, zones: pd.DataFrame, weights: Weights) -> None:
    """Inspector card for the selected cell (default: zone #1)."""
    match = zones.loc[zones["cell_id"] == selected["cell_id"], "zone_rank"]
    rank = int(match.iloc[0]) if len(match) else None
    st.markdown(inspector_html(selected, rank, weights), unsafe_allow_html=True)
    st.markdown(
        '<div class="uhi-hint">Click any cell, pin or corridor on the map to inspect it.</div>',
        unsafe_allow_html=True,
    )


def render_footer() -> None:
    """Method expander and data attribution (ARCHITECTURE §12)."""
    with st.expander("Method ▸"):
        st.markdown(
            "- A 250 m British National Grid (EPSG:27700) grid covers the City of Edinburgh.\n"
            "- **Heat:** Landsat 8/9 summer surface-temperature anomaly vs. the typical urban cell "
            "(clipped 0 → P95).\n"
            "- **Park gap:** distance to a ≥ 0.5 ha greenspace (200 m doorstep → 600 m).\n"
            "- **Sealed surface:** max(WorldCover built-up, OSM building share), 20 % → 90 %.\n"
            "- Only urban, land, non-excluded, non-green cells with enough clear scenes are "
            "eligible.\n"
            "- Top zones are picked greedily with at least 500 m between centroids."
        )
        st.latex(
            r"P = 100\,(w_T S_T + w_D S_D + w_I S_I),\quad "
            r"S = \min\!\big(1,\max(0,\tfrac{x-lo}{hi-lo})\big)"
        )
        st.caption(
            "Surface temperature is a proxy for heat exposure, not air temperature, and the "
            "score ranks places, not people."
        )
    st.caption(
        "Data: © OpenStreetMap contributors (ODbL) · Landsat 8/9 courtesy of the U.S. Geological "
        "Survey · ESA WorldCover 2021 © ESA, CC BY 4.0 · Access via Microsoft Planetary "
        "Computer · Air temperature: Open-Meteo (CC BY 4.0) · Basemap © CARTO or © Esri."
    )


def _side_heading(text: str) -> None:
    st.markdown(f'<div class="uhi-side-h">{text}</div>', unsafe_allow_html=True)


def render_controls(meta: dict[str, Any], model: ModelState) -> None:
    """Layer toggles, model weights and K.

    Widgets carry ``key=`` only: their values live in session state (seeded by
    ``state.init_state``) and are read back through ``state.model_from_state``.
    """
    _side_heading("Map layers")
    st.toggle("Surface heat anomaly", key="t_heat")
    st.toggle("Cooling greenspace", key="t_parks")
    st.toggle("Top 15 greening zones", key="t_zones")

    st.divider()
    _side_heading("Model weights")
    st.slider("🔥 Heat", 0.0, 1.0, step=0.05, key="w_thermal")
    st.slider("🌳 Park gap", 0.0, 1.0, step=0.05, key="w_distance")
    st.slider("🧱 Sealed surface", 0.0, 1.0, step=0.05, key="w_impervious")
    weights = model.weights
    if model.weights_all_zero:
        st.warning("All weights are zero; using defaults.")
    st.caption(
        f"Normalised: heat {weights.thermal:.2f} · park gap {weights.distance:.2f} · "
        f"sealed {weights.impervious:.2f}"
    )
    st.slider("Zones (K)", 10, 15, key="k_zones")
    st.button("↺ Restore defaults", on_click=reset_model, args=(st.session_state,), width="stretch")
    st.caption(
        "Live re-ranking. Robustness of the default top-15 to ±0.1 weight changes: "
        f"mean Jaccard **{meta['sensitivity']['jaccard_mean']:.2f}**."
    )
