"""Streamlit + Folium dashboard for the Edinburgh Urban Heat Island & Green Space Optimizer.

Implements ``docs/ARCHITECTURE.md`` §7. The app is deliberately thin: it reads only the small,
pre-computed EPSG:4326 layers in ``data/processed/web/`` plus ``scoring_metadata.json`` and never
touches raw rasters, Overpass or the heavy pipeline modules. All geometry here is already in
WGS84 for Leaflet; no reprojection happens in this file.

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

import altair as alt
import branca.colormap as cm
import folium
import geopandas as gpd
import numpy as np
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

from edinburgh_uhi import config
from edinburgh_uhi.config import Weights
from edinburgh_uhi.scoring import priority, select_top_zones

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
INTERVENTION_LABELS = {
    "green_roofs": "Green roofs",
    "street_trees": "Street trees",
    "depave_pocket_park": "Depave & pocket park",
}
CELL_AREA_HA = config.GRID_CELL_M**2 / 10_000
BASEMAP_ATTR = "© OpenStreetMap contributors © CARTO"
CYAN = "#00E5FF"
AMBER = "#FFB000"
PARK_GREEN = "#2ecc71"
COMPONENT_COLUMNS = ["c_thermal", "c_distance", "c_impervious"]
SCORE_COLUMNS = ["s_thermal", "s_distance", "s_impervious"]
DEFAULT_WEIGHTS = (config.WEIGHTS.thermal, config.WEIGHTS.distance, config.WEIGHTS.impervious)


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
# Pure logic
# --------------------------------------------------------------------------------------
def normalise_weights(raw: tuple[float, float, float]) -> Weights:
    """Scale three non-negative slider values to sum to 1 (all zero falls back to defaults)."""
    total = float(sum(raw))
    if total <= 0:
        return config.WEIGHTS
    t, d, i = (v / total for v in raw)
    return Weights(thermal=t, distance=d, impervious=i)


def rescore(grid: gpd.GeoDataFrame, weights: Weights) -> gpd.GeoDataFrame:
    """Recompute contributions and score from the stored component scores (no geometry ops)."""
    prio = priority(grid[SCORE_COLUMNS], weights)
    out = grid.drop(columns=[*COMPONENT_COLUMNS, "priority_score"]).join(prio)
    return gpd.GeoDataFrame(out, geometry="geometry", crs=grid.crs)


def rerank(
    grid: gpd.GeoDataFrame, weights: Weights, k: int, min_sep_m: float
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Return ``(rescored_grid, top_zones)`` for the given weights and K."""
    scored = rescore(grid, weights)
    zones = select_top_zones(scored, k, min_sep_m)
    return scored, gpd.GeoDataFrame(zones, geometry="geometry", crs=grid.crs)


def format_tooltips(scored: pd.DataFrame) -> pd.DataFrame:
    """Pre-format the six tooltip strings (GeoJsonTooltip cannot format numbers itself)."""
    park = scored["nearest_park_name"].fillna("Unnamed greenspace").astype(str)
    anomaly = scored["lst_anomaly_c"]
    heat = pd.Series(
        np.where(anomaly.notna(), anomaly.map("{:+.1f} °C".format, na_action="ignore"), "n/a"),
        index=scored.index,
    )
    action = scored["recommendation"].where(scored["eligible"], None).fillna("—").astype(str)
    return pd.DataFrame(
        {
            "tt_area": scored["neighbourhood"].fillna("Edinburgh").astype(str),
            "tt_heat": heat,
            "tt_park": scored["dist_park_m"].round().astype(int).astype(str) + " m (" + park + ")",
            "tt_sealed": (scored["impervious_frac"] * 100).round().astype(int).astype(str) + " %",
            "tt_score": scored["priority_score"].round().astype(int).astype(str) + " / 100",
            "tt_action": action,
        },
        index=scored.index,
    )


# --------------------------------------------------------------------------------------
# Map
# --------------------------------------------------------------------------------------
def _heat_layer(scored: gpd.GeoDataFrame, m: folium.Map) -> folium.FeatureGroup:
    anomalies = scored["lst_anomaly_c"].dropna()
    lo, hi = np.percentile(anomalies, [2, 98])
    cmap = cm.LinearColormap(
        ["#2c7bb6", "#abd9e9", "#ffffbf", "#fdae61", "#d7191c"],
        vmin=float(lo),
        vmax=float(hi),
        caption="Summer surface temperature vs. typical Edinburgh street (°C)",
    )
    tt = format_tooltips(scored)
    colours = scored["lst_anomaly_c"].map(
        lambda v: cmap(min(max(v, lo), hi)) if pd.notna(v) else "#808080"
    )
    data = gpd.GeoDataFrame(
        pd.concat([scored[["cell_id"]], tt, colours.rename("_color")], axis=1),
        geometry=scored.geometry,
        crs=scored.crs,
    )
    group = folium.FeatureGroup(name="Surface heat anomaly")
    folium.GeoJson(
        data,
        style_function=lambda f: {
            "fillColor": f["properties"]["_color"],
            "fillOpacity": 0.55,
            "weight": 0,
        },
        tooltip=folium.GeoJsonTooltip(
            fields=["tt_area", "tt_heat", "tt_park", "tt_sealed", "tt_score", "tt_action"],
            aliases=[
                "Area",
                "Heat vs. typical street",
                "Nearest ≥0.5 ha greenspace",
                "Sealed surface",
                "Greening priority",
                "Suggested action",
            ],
            sticky=True,
        ),
    ).add_to(group)
    cmap.add_to(m)
    return group


def _parks_layer(parks: gpd.GeoDataFrame) -> folium.FeatureGroup:
    data = parks[["park_id", "name", "area_ha", "geometry"]].copy()
    data["name"] = data["name"].fillna("Unnamed greenspace")
    data["area_ha"] = data["area_ha"].map("{:.1f} ha".format)
    group = folium.FeatureGroup(name="Cooling greenspace ≥ 0.5 ha")
    folium.GeoJson(
        data,
        style_function=lambda _f: {
            "fillColor": PARK_GREEN,
            "color": PARK_GREEN,
            "fillOpacity": 0.35,
            "weight": 0.5,
        },
        tooltip=folium.GeoJsonTooltip(fields=["name", "area_ha"], aliases=["Park", "Area"]),
    ).add_to(group)
    return group


def _zones_layer(
    zones: gpd.GeoDataFrame, corridors: gpd.GeoDataFrame, n_label: int
) -> folium.FeatureGroup:
    group = folium.FeatureGroup(name=f"Top {n_label} greening zones")
    cells = zones[["cell_id", "zone_rank", "neighbourhood", "geometry"]].copy()
    cells["label"] = "#" + cells["zone_rank"].astype(str) + " " + cells["neighbourhood"].astype(str)
    folium.GeoJson(
        cells,
        style_function=lambda _f: {
            "color": CYAN,
            "fillColor": CYAN,
            "weight": 3,
            "fillOpacity": 0.1,
        },
        tooltip=folium.GeoJsonTooltip(fields=["label"], aliases=["Zone"], sticky=True),
    ).add_to(group)

    lines = corridors.loc[corridors["cell_id"].isin(zones["cell_id"])]
    if len(lines):
        folium.GeoJson(
            lines[["cell_id", "main_street", "geometry"]],
            style_function=lambda _f: {"color": AMBER, "weight": 6, "opacity": 0.95},
            tooltip=folium.GeoJsonTooltip(fields=["main_street"], aliases=["Corridor"]),
        ).add_to(group)

    for row in zones.itertuples():
        bounds = row.geometry.bounds
        lat, lon = (bounds[1] + bounds[3]) / 2, (bounds[0] + bounds[2]) / 2
        badge = (
            f'<div style="background:{CYAN};color:#000;border-radius:50%;width:26px;height:26px;'
            f"line-height:26px;text-align:center;font-weight:700;font-size:13px;"
            f'border:2px solid #fff;box-shadow:0 0 4px #000">{row.zone_rank}</div>'
        )
        folium.Marker(
            [lat, lon],
            icon=folium.DivIcon(html=badge, icon_size=(26, 26), icon_anchor=(13, 13)),
        ).add_to(group)
    return group


@st.cache_resource(show_spinner="Building map…")
def build_map(
    toggles: tuple[bool, bool, bool],
    weights_key: tuple[float, float, float],
    k: int,
    min_sep_m: float,
) -> folium.Map:
    """Build the Folium map; cached on ``(layer toggles, weights, k)``."""
    layers = load_layers()
    scored, zones = rerank(layers["grid"], Weights(*weights_key), k, min_sep_m)
    show_heat, show_parks, show_zones = toggles

    m = folium.Map(
        location=list(config.EDINBURGH_CENTER_LATLON),
        zoom_start=12,
        tiles=None,
        control_scale=True,
        prefer_canvas=True,
    )
    for style, label in (("dark_all", "Dark basemap"), ("light_all", "Light basemap")):
        folium.TileLayer(
            tiles=f"https://{{s}}.basemaps.cartocdn.com/{style}/{{z}}/{{x}}/{{y}}{{r}}.png",
            attr=BASEMAP_ATTR,
            name=label,
            overlay=False,
        ).add_to(m)

    if show_heat:
        _heat_layer(scored, m).add_to(m)
    if show_parks:
        _parks_layer(layers["parks"]).add_to(m)
    if show_zones:
        _zones_layer(zones, layers["corridors"], len(zones)).add_to(m)

    minx, miny, maxx, maxy = scored.total_bounds
    m.fit_bounds([[miny, minx], [maxy, maxx]])
    folium.LayerControl(collapsed=True).add_to(m)
    return m


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------
def render_kpis(meta: dict[str, Any], n_zones: int) -> None:
    """Four KPI cards, all values read from ``scoring_metadata.json``."""
    kpis, lst = meta["kpis"], meta.get("lst") or {}
    c1, c2, c3, c4 = st.columns(4)
    c1.metric(
        "Hottest priority zone vs. typical Edinburgh street",
        f"+{kpis['peak_zone_anomaly_c']:.1f} °C",
        help="Median summer daytime surface-temperature anomaly of the hottest top-ranked cell.",
    )
    c2.metric(
        "Urban blocks > 200 m from a ≥ 0.5 ha greenspace",
        f"{kpis['pct_urban_cells_beyond_doorstep']:.0f} %",
        help="Natural England's doorstep standard is used as the benchmark.",
    )
    c3.metric(
        "Recommended for greening",
        f"{n_zones} zones · {n_zones * CELL_AREA_HA:.0f} ha",
    )
    scenes = lst.get("n_scenes_used")
    dates, air = lst.get("date_range"), lst.get("mean_air_tmax_c")
    help_txt = None
    if dates:
        help_txt = f"{dates[0]} to {dates[1]}" + (f"; mean air Tmax {air:.1f} °C" if air else "")
    c4.metric(
        "Clear-sky Landsat summer passes",
        "n/a" if scenes is None else str(scenes),
        help=help_txt,
    )


def render_zone_table(zones: pd.DataFrame) -> None:
    """Ranked Top-K table plus a stacked score-breakdown chart."""
    table = pd.DataFrame(
        {
            "Rank": zones["zone_rank"].to_numpy(),
            "Area": zones["neighbourhood"].astype(str).to_numpy(),
            "Action": zones["intervention"].map(INTERVENTION_LABELS).to_numpy(),
            "Score": zones["priority_score"].round(1).to_numpy(),
        }
    )
    tab_table, tab_chart = st.tabs(["Ranking", "Score breakdown"])
    with tab_table:
        st.dataframe(
            table,
            hide_index=True,
            height=36 * (len(table) + 1) + 3,
            column_config={
                "Rank": st.column_config.NumberColumn("#", width="small"),
                "Score": st.column_config.ProgressColumn(
                    "Score", min_value=0, max_value=100, format="%.0f"
                ),
            },
        )
    with tab_chart:
        long = zones[["zone_rank", "neighbourhood", *COMPONENT_COLUMNS]].melt(
            id_vars=["zone_rank", "neighbourhood"], var_name="Component", value_name="Points"
        )
        long["Component"] = long["Component"].map(
            {"c_thermal": "Heat", "c_distance": "Park gap", "c_impervious": "Sealed surface"}
        )
        long["Zone"] = "#" + long["zone_rank"].astype(str) + " " + long["neighbourhood"].astype(str)
        chart = (
            alt.Chart(long)
            .mark_bar()
            .encode(
                y=alt.Y("Zone:N", sort=alt.SortField("zone_rank"), title=None),
                x=alt.X("Points:Q", title="Greening priority (0–100)"),
                color=alt.Color(
                    "Component:N",
                    scale=alt.Scale(
                        domain=["Heat", "Park gap", "Sealed surface"],
                        range=["#e4572e", "#2ecc71", "#9aa5b1"],
                    ),
                ),
                tooltip=["Zone", "Component", alt.Tooltip("Points:Q", format=".1f")],
            )
            .properties(height=36 * len(zones) + 30)
        )
        st.altair_chart(chart, width="stretch")


def render_inspector(selected: pd.Series, zones: pd.DataFrame, weights: Weights) -> None:
    """Score bars and recommendation for the selected cell (default: zone #1)."""
    st.markdown("#### 🔍 Zone inspector")
    match = zones.loc[zones["cell_id"] == selected["cell_id"], "zone_rank"]
    prefix = f"#{int(match.iloc[0])} " if len(match) else ""
    score = float(selected["priority_score"])
    st.subheader(f"{prefix}{selected['neighbourhood'] or 'Edinburgh'} · Score {score:.0f}")
    rows = [
        ("Heat", "c_thermal", weights.thermal),
        ("Park gap", "c_distance", weights.distance),
        ("Sealed surface", "c_impervious", weights.impervious),
    ]
    for label, col, w in rows:
        cap = 100.0 * w
        value = float(selected[col])
        st.progress(min(value / cap, 1.0) if cap > 0 else 0.0)
        st.caption(f"{label}: {value:.0f} of {cap:.0f}")
    if bool(selected["eligible"]) and selected["recommendation"]:
        st.success(f"→ {selected['recommendation']}")
    else:
        st.info(f"Not eligible for recommendation ({selected['exclusion_reason'] or 'n/a'}).")


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
        "Computer · Air temperature: Open-Meteo (CC BY 4.0)."
    )


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------
def _reset_model() -> None:
    st.session_state.update(
        w_thermal=DEFAULT_WEIGHTS[0],
        w_distance=DEFAULT_WEIGHTS[1],
        w_impervious=DEFAULT_WEIGHTS[2],
        k_zones=config.TOPK_K,
    )


def main() -> None:
    """Render the dashboard."""
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

    with st.sidebar:
        st.header("Layers")
        show_heat = st.toggle("Surface heat anomaly", value=True, key="t_heat")
        show_parks = st.toggle("Cooling greenspace", value=True, key="t_parks")
        show_zones = st.toggle("Top greening zones", value=not demo, key="t_zones")
        with st.expander("Tune the model"):
            raw = (
                st.slider("Heat weight", 0.0, 1.0, DEFAULT_WEIGHTS[0], 0.05, key="w_thermal"),
                st.slider("Park-gap weight", 0.0, 1.0, DEFAULT_WEIGHTS[1], 0.05, key="w_distance"),
                st.slider(
                    "Sealed-surface weight", 0.0, 1.0, DEFAULT_WEIGHTS[2], 0.05, key="w_impervious"
                ),
            )
            k = st.slider("Zones (K)", 10, 15, config.TOPK_K, key="k_zones")
            weights = normalise_weights(raw)
            if sum(raw) <= 0:
                st.warning("All weights are zero; using defaults.")
            st.caption(
                f"Normalised: heat {weights.thermal:.2f} · park gap {weights.distance:.2f} · "
                f"sealed {weights.impervious:.2f}"
            )
            st.button("Restore defaults", on_click=_reset_model)
            st.caption(
                "Live re-ranking; robustness of default top-15 to ±0.1 weight changes: "
                f"mean Jaccard {meta['sensitivity']['jaccard_mean']:.2f}"
            )

    st.title("🌳 Where should Edinburgh plant its next trees?")
    lst = meta.get("lst") or {}
    years = (lst.get("date_range") or [f"{config.LST_YEARS[0]}", f"{config.LST_YEARS[1]}"])[:2]
    st.caption(
        f"Summer surface heat (Landsat 8/9, {years[0][:4]}–{years[1][:4]}) × distance to "
        "greenspace × sealed ground"
    )

    weights_key = (weights.thermal, weights.distance, weights.impervious)
    scored, zones = rerank(layers["grid"], weights, k, min_sep_m)
    render_kpis(meta, len(zones))

    map_col, side_col = st.columns([0.68, 0.32])
    with map_col:
        fmap = build_map((show_heat, show_parks, show_zones), weights_key, k, min_sep_m)
        out = st_folium(
            fmap,
            height=620,
            use_container_width=True,
            returned_objects=["last_active_drawing"],
            key="map",
        )
    clicked = ((out or {}).get("last_active_drawing") or {}).get("properties", {}).get("cell_id")
    by_id = scored.set_index("cell_id", drop=False)
    selected = by_id.loc[clicked] if clicked in by_id.index else zones.iloc[0]

    with side_col:
        st.markdown(f"#### 🏆 Top {len(zones)} greening zones")
        render_zone_table(zones)
        render_inspector(selected, zones, weights)

    render_footer()


main()
