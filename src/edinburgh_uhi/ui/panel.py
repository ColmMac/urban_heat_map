"""Streamlit renderers for the floating panel / bottom sheet.

Layout, top to bottom::

    header + KPI strip          always visible
    peek card (selected zone)   hidden while the Zone section is open (the inspector repeats it)
    [Zone | Tune | Ranking | About]   st.segmented_control bound to state.VIEW_KEY
    section body                only drawn when a section is open (st.container key="panel_body")

On phones the panel is a bottom sheet: collapsed to the always-visible part when no section is
open ("peek"), and a half-height sheet when one is. The CSS keys off the presence of
``.st-key-panel_body`` (``theme.FELT_CSS``), so opening or closing a section needs no JavaScript.
The HTML builders live in ``components`` (pure); the settings come from ``state``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd
import streamlit as st

from edinburgh_uhi import config
from edinburgh_uhi.ui.components import (
    header_html,
    inspector_html,
    kpi_glossary_md,
    kpi_items,
    kpi_strip_html,
    peek_html,
    ranked_list_html,
    weight_mix_html,
    zone_rank,
    zones_csv,
)
from edinburgh_uhi.ui.state import (
    PERSIST,
    SHEET_HIDDEN_KEY,
    VIEW_KEY,
    ZONE_VIEW,
    ModelState,
    hide_sheet,
    reset_model,
    show_sheet,
)

SECTIONS = {ZONE_VIEW: "Zone", "tune": "Tune", "rank": "Ranking", "about": "About"}
BODY_KEY = "panel_body"
K_OPTIONS = list(range(10, 16))


def _html(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


def _heading(text: str) -> None:
    _html(f'<div class="uhi-side-h">{text}</div>')


# --------------------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------------------
def render_zone(selected: pd.Series, rank: int | None, model: ModelState) -> None:
    """Inspector card for the selected cell (default: zone #1)."""
    _html(inspector_html(selected, rank, model.weights))
    _html('<div class="uhi-hint">Select any cell, pin or corridor on the map to inspect it.</div>')


def render_tune(meta: dict[str, Any], model: ModelState) -> None:
    """Layer toggles, weight sliders and K.

    Widgets carry ``key=`` and ``persist_state`` only: values live in session state (seeded by
    ``state.init_state``) and are read back through ``state.model_from_state``.
    """
    _heading("Map layers")
    st.toggle("Surface heat anomaly", key="t_heat", persist_state=PERSIST)
    st.toggle("Cooling greenspace", key="t_parks", persist_state=PERSIST)
    st.toggle("Top greening zones", key="t_zones", persist_state=PERSIST)

    _heading("Model weights")
    _html(weight_mix_html(model.weights))
    st.slider("Heat", 0.0, 1.0, step=0.05, key="w_thermal", persist_state=PERSIST)
    st.slider("Park gap", 0.0, 1.0, step=0.05, key="w_distance", persist_state=PERSIST)
    st.slider("Sealed surface", 0.0, 1.0, step=0.05, key="w_impervious", persist_state=PERSIST)
    if model.weights_all_zero:
        st.warning("All weights are zero; using defaults.")
    st.segmented_control(
        "Zones to recommend (K)",
        K_OPTIONS,
        key="k_zones",
        required=True,
        persist_state=PERSIST,
        width="stretch",
    )
    st.button("Restore defaults", on_click=reset_model, args=(st.session_state,), width="stretch")
    st.caption(
        "Live re-ranking. Robustness of the default top-15 to ±0.1 weight changes: "
        f"mean Jaccard **{meta['sensitivity']['jaccard_mean']:.2f}**."
    )


def render_rank(zones: pd.DataFrame, selected_id: object) -> None:
    """Ranked list of the top-K zones with their score breakdown, plus a CSV download."""
    _html(ranked_list_html(zones, selected_id))
    st.download_button(
        "Download CSV",
        zones_csv(zones),
        file_name=f"edinburgh_top{len(zones)}_greening_zones.csv",
        mime="text/csv",
        width="stretch",
    )


def render_about(meta: dict[str, Any]) -> None:
    """Method, KPI definitions and data attribution (ARCHITECTURE §12)."""
    _heading("Method")
    st.markdown(
        f"- A {config.GRID_CELL_M} m British National Grid (EPSG:27700) grid covers the City of "
        "Edinburgh.\n"
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
    _heading("Key figures")
    st.markdown(kpi_glossary_md(meta))
    _heading("Data")
    st.caption(
        "© OpenStreetMap contributors (ODbL) · Landsat 8/9 courtesy of the U.S. Geological "
        "Survey · ESA WorldCover 2021 © ESA, CC BY 4.0 · Access via Microsoft Planetary "
        "Computer · Air temperature: Open-Meteo (CC BY 4.0) · Basemap © CARTO or © Esri."
    )


# --------------------------------------------------------------------------------------
# Panel
# --------------------------------------------------------------------------------------
def render_panel(
    meta: dict[str, Any], model: ModelState, zones: pd.DataFrame, selected: pd.Series
) -> None:
    """Draw the whole panel; call inside ``st.container(key="felt_panel")``.

    Phones can close the sheet completely: the handle at its top is a "Hide panel" button,
    and while hidden only a floating "Show panel" pill is visible (``theme.FELT_CSS`` keys off
    ``.st-key-sheet_show``). The rest of the panel is still drawn, so desktop (where neither
    button is displayed) is unaffected and widget state is untouched.
    """
    if st.session_state.get(SHEET_HIDDEN_KEY):
        st.button(
            "Show panel",
            key="sheet_show",
            icon=":material/keyboard_arrow_up:",
            on_click=show_sheet,
            args=(st.session_state,),
        )
    else:
        st.button(
            "Hide panel",
            key="sheet_hide",
            icon=":material/keyboard_arrow_down:",
            on_click=hide_sheet,
            args=(st.session_state,),
            width="stretch",
        )
    rank = zone_rank(zones, selected["cell_id"])
    _html(header_html(meta) + kpi_strip_html(kpi_items(meta, len(zones))))

    view = st.session_state.get(VIEW_KEY)
    if view != ZONE_VIEW:
        _html(peek_html(selected, rank))

    view = st.segmented_control(
        "Panel section",
        list(SECTIONS),
        format_func=SECTIONS.__getitem__,
        key=VIEW_KEY,
        label_visibility="collapsed",
        width="stretch",
    )
    if view is None:
        return

    sections: dict[str, Callable[[], None]] = {
        ZONE_VIEW: lambda: render_zone(selected, rank, model),
        "tune": lambda: render_tune(meta, model),
        "rank": lambda: render_rank(zones, selected["cell_id"]),
        "about": lambda: render_about(meta),
    }
    with st.container(key=BODY_KEY):
        sections[view]()
