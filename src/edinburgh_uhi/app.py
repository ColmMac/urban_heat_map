"""Streamlit + Folium dashboard for the Edinburgh Urban Heat Island & Green Space Optimizer.

Implements ``docs/ARCHITECTURE.md`` §7 as a dark-mode "command center": a map-first layout on
CartoDB Dark Matter, dark HTML hover cards, a ranked Top-K inspector and a matching Altair chart.
The app is deliberately thin: it reads only the small, pre-computed EPSG:4326 layers in
``data/processed/web/`` plus ``scoring_metadata.json`` and never touches raw rasters, Overpass or
the heavy pipeline modules. All geometry here is already in WGS84 for Leaflet; no reprojection
happens in this file.

Run with: ``uv run streamlit run src/edinburgh_uhi/app.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

try:  # keep `streamlit run src/edinburgh_uhi/app.py` working without an installed package
    import edinburgh_uhi  # noqa: F401
except ImportError:  # pragma: no cover - depends on host environment
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import html
import json
import os
from dataclasses import dataclass
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
EXCLUSION_LABELS = {  # scoring.eligibility() reason codes -> hover-card / inspector text
    "edge_cell": "Mostly outside the city boundary",
    "water": "Mostly water",
    "non_plantable_landuse": "Non-plantable land use",
    "not_urban": "Not urban fabric",
    "already_green": "Already greenspace",
    "insufficient_lst": "Too few clear-sky Landsat passes",
}
CELL_AREA_HA = config.GRID_CELL_M**2 / 10_000
CARTO_ATTR = "© OpenStreetMap contributors © CARTO"
CARTO_URL = "https://basemaps.cartocdn.com/rastertiles/{style}/{{z}}/{{x}}/{{y}}{{r}}.png?key={key}"
ESRI_ATTR = (
    "Tiles © Esri — Esri, HERE, Garmin, © OpenStreetMap contributors, and the GIS user community"
)
ESRI_URL = (
    "https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/{layer}/MapServer/tile/"
    "{{z}}/{{y}}/{{x}}"
)
COMPONENT_COLUMNS = ["c_thermal", "c_distance", "c_impervious"]
SCORE_COLUMNS = ["s_thermal", "s_distance", "s_impervious"]
DEFAULT_WEIGHTS = (config.WEIGHTS.thermal, config.WEIGHTS.distance, config.WEIGHTS.impervious)
MAP_HEIGHT_PX = 680

# Palette (kept in sync with .streamlit/config.toml)
BG = "#0B0F19"
PANEL = "#111827"
BORDER = "#1F2937"
TEXT = "#F3F4F6"
MUTED = "#9CA3AF"
ACCENT = "#F97316"
AMBER = "#F59E0B"
CRIMSON = "#DC2626"
EMERALD = "#10B981"
CORRIDOR = "#22D3EE"
SLATE = "#64748B"
COOL = "#3B82F6"
HEAT_RAMP = [COOL, "#1E293B", AMBER, "#EA580C", CRIMSON]
COMPONENTS = (  # (label, contribution column, colour)
    ("Heat", "c_thermal", ACCENT),
    ("Park gap", "c_distance", EMERALD),
    ("Sealed surface", "c_impervious", SLATE),
)


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


def _nullable(series: pd.Series) -> pd.Series:
    """Object series with missing values as ``None`` (serialises to JSON ``null``)."""
    return series.astype(object).where(series.notna(), None)


def exclusion_label(reasons: pd.Series) -> pd.Series:
    """Human-readable exclusion reasons; unknown codes pass through, missing stays missing."""
    return reasons.astype(object).map(lambda r: EXCLUSION_LABELS.get(r, r) if pd.notna(r) else r)


def tooltip_props(scored: pd.DataFrame) -> pd.DataFrame:
    """Compact per-cell properties consumed by the JavaScript hover card.

    Keys are short to keep the GeoJSON payload small; formatting happens in the browser.
    """
    eligible = scored["eligible"].astype(bool)
    return pd.DataFrame(
        {
            "nb": scored["neighbourhood"].fillna("Edinburgh").astype(str),
            "st": _nullable(scored["main_street"]),
            "ps": scored["priority_score"].round().astype(int),
            "an": _nullable(scored["lst_anomaly_c"].round(1)),
            "pd": scored["dist_park_m"].round().astype(int),
            "pk": scored["nearest_park_name"].fillna("Unnamed greenspace").astype(str),
            "im": (scored["impervious_frac"] * 100).round().astype(int),
            "rc": _nullable(scored["recommendation"].where(eligible)),
            "ex": _nullable(exclusion_label(scored["exclusion_reason"]).where(~eligible)),
        },
        index=scored.index,
    )


def heat_domain(anomaly: pd.Series) -> tuple[float, float]:
    """Colour-ramp domain ``(p2, p98)`` of the anomaly, forced to straddle 0 °C."""
    lo, hi = np.percentile(anomaly.dropna(), [2, 98])
    return min(float(lo), -0.1), max(float(hi), 0.1)


def score_colour(score: float) -> str:
    """Badge colour for a 0–100 priority score."""
    if score >= 75:
        return CRIMSON
    if score >= 50:
        return ACCENT
    if score >= 25:
        return AMBER
    return SLATE


@dataclass(frozen=True)
class Basemap:
    """One XYZ tile source."""

    name: str
    url: str
    attr: str


def carto_api_key() -> str | None:
    """CARTO basemap key from ``CARTO_API_KEY`` (env var or Streamlit secret), if configured.

    Since late 2026 CARTO serves an "API key required" watermark tile without a (free) key.
    """
    key = os.environ.get("CARTO_API_KEY")
    if not key:
        try:
            key = st.secrets.get("CARTO_API_KEY")
        except Exception:  # noqa: BLE001 - no secrets.toml at all is a normal local setup
            key = None
    key = str(key).strip() if key else ""
    return key or None


def basemap_tiles(carto_key: str | None) -> tuple[list[Basemap], Basemap | None]:
    """``(base layers, optional labels overlay)``; dark base first.

    With a CARTO key: CartoDB Dark Matter + Positron. Without one: Esri's keyless Dark/Light
    Gray Canvas, with Esri's reference labels drawn above the choropleth.
    """
    if carto_key:
        bases = [
            Basemap(
                "CartoDB Dark Matter", CARTO_URL.format(style="dark_all", key=carto_key), CARTO_ATTR
            ),
            Basemap(
                "CartoDB Positron", CARTO_URL.format(style="light_all", key=carto_key), CARTO_ATTR
            ),
        ]
        return bases, None
    bases = [
        Basemap("Dark Gray Canvas", ESRI_URL.format(layer="World_Dark_Gray_Base"), ESRI_ATTR),
        Basemap("Light Gray Canvas", ESRI_URL.format(layer="World_Light_Gray_Base"), ESRI_ATTR),
    ]
    labels = Basemap("Place labels", ESRI_URL.format(layer="World_Dark_Gray_Reference"), ESRI_ATTR)
    return bases, labels


# --------------------------------------------------------------------------------------
# Map
# --------------------------------------------------------------------------------------
# Hover-card builder shared by every interactive layer. Runs inside Leaflet's onEachFeature, so it
# must be self-contained (header <script> tags are not executed by streamlit-folium).
_CARD_JS = """
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const tier = (s) => s >= 75 ? '#DC2626' : s >= 50 ? '#F97316' : s >= 25 ? '#F59E0B' : '#64748B';
const card = (p) => {
  const an = p.an == null ? 'n/a' : (p.an > 0 ? '+' : '') + Number(p.an).toFixed(1) + ' °C';
  const rank = p.rk ? '<span class="uhi-rank">#' + p.rk + '</span>' : '';
  const street = p.st ? esc(p.st) : 'No named main street';
  const rec = p.rc
    ? '<div class="uhi-rec"><span>Recommended</span>' + esc(p.rc) + '</div>'
    : '<div class="uhi-rec uhi-off"><span>Not eligible</span>' + esc(p.ex || 'n/a') + '</div>';
  return '<div class="uhi-card">'
    + '<div class="uhi-head"><div class="uhi-title"><div class="uhi-nb">' + rank + esc(p.nb)
    + '</div><div class="uhi-st">' + street + '</div></div>'
    + '<div class="uhi-badge" style="background:' + tier(p.ps) + '">' + p.ps + '</div></div>'
    + '<div class="uhi-stats">'
    + '<div><span>LST anomaly</span><b>' + an + '</b></div>'
    + '<div><span>Park gap</span><b>' + p.pd + ' m</b></div>'
    + '<div><span>Impervious</span><b>' + p.im + ' %</b></div>'
    + '</div><div class="uhi-park">Nearest park: ' + esc(p.pk) + '</div>' + rec + '</div>';
};
const tipOpts = {sticky: true, className: 'uhi-tip', opacity: 1, direction: 'top',
  offset: [0, -12]};
"""


def _on_each_feature(body: str) -> folium.JsCode:
    """Wrap ``body`` (which may use ``p``, ``card``, ``esc``, ``tipOpts``) as onEachFeature."""
    return folium.JsCode(
        "function (feature, layer) {\n"
        + _CARD_JS
        + "const p = feature.properties;\n"
        + body
        + "\n}"
    )


CELL_TOOLTIP_JS = _on_each_feature("layer.bindTooltip(card(p), tipOpts);")
PIN_JS = _on_each_feature(
    "layer.setIcon(L.divIcon({className: 'uhi-pin-wrap', iconSize: [30, 30], "
    "iconAnchor: [15, 15], html: '<div class=\"uhi-pin' + (p.rk <= 3 ? ' uhi-pin-top' : '') "
    "+ '\">' + p.rk + '</div>'}));\n"
    "layer.bindTooltip(card(p), tipOpts);"
)
PARK_TOOLTIP_JS = _on_each_feature(
    'layer.bindTooltip(\'<div class="uhi-card uhi-mini"><div class="uhi-kicker" '
    'style="color:#10B981">Cooling greenspace</div><div class="uhi-nb">\' + esc(p.name) '
    "+ '</div><div class=\"uhi-st\">' + p.area_ha + '</div></div>', tipOpts);"
)
CORRIDOR_TOOLTIP_JS = _on_each_feature(
    'layer.bindTooltip(\'<div class="uhi-card uhi-mini"><div class="uhi-kicker" '
    'style="color:#22D3EE">Proposed tree corridor</div><div class="uhi-nb">\' '
    "+ esc(p.main_street) + '</div></div>', tipOpts);"
)

MAP_CSS = f"""
<style>
.leaflet-container {{ background: {BG}; font-family: Inter, ui-sans-serif, system-ui,
  -apple-system, "Segoe UI", Roboto, sans-serif; }}
.leaflet-bar {{ border: 1px solid {BORDER} !important; border-radius: 10px !important;
  overflow: hidden; box-shadow: 0 6px 20px rgba(0,0,0,.45) !important; }}
.leaflet-bar a, .leaflet-bar a:hover {{ background: {PANEL}; color: {TEXT};
  border-bottom: 1px solid {BORDER}; }}
.leaflet-bar a:hover {{ background: #1F2937; }}
.leaflet-control-layers {{ background: rgba(17,24,39,.92) !important; color: {TEXT};
  border: 1px solid {BORDER} !important; border-radius: 10px !important;
  box-shadow: 0 6px 20px rgba(0,0,0,.45) !important; }}
.leaflet-control-layers-toggle {{ filter: invert(.92); }}
.leaflet-control-layers-separator {{ border-top-color: {BORDER} !important; }}
.leaflet-control-layers label {{ font-size: 12px; }}
.leaflet-control-attribution {{ background: rgba(11,15,25,.75) !important;
  color: #6B7280 !important; font-size: 10px; }}
.leaflet-control-attribution a {{ color: {MUTED} !important; }}
.leaflet-control-scale-line {{ background: rgba(17,24,39,.8); color: #D1D5DB;
  border-color: #4B5563; }}
.leaflet-tooltip.uhi-tip {{ background: transparent; border: 0; box-shadow: none; padding: 0;
  white-space: normal; }}
.leaflet-tooltip.uhi-tip:before {{ display: none; }}
.uhi-card {{ width: 260px; background: rgba(11,15,25,.96); color: {TEXT};
  border: 1px solid {BORDER}; border-radius: 12px; padding: 12px 14px;
  box-shadow: 0 12px 32px rgba(0,0,0,.6); font-size: 12px; line-height: 1.35; }}
.uhi-card.uhi-mini {{ width: auto; min-width: 160px; max-width: 240px; padding: 9px 12px; }}
.uhi-kicker {{ font-size: 10px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase;
  margin-bottom: 2px; }}
.uhi-head {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; }}
.uhi-title {{ min-width: 0; }}
.uhi-nb {{ font-size: 14px; font-weight: 700; color: #F9FAFB; }}
.uhi-st {{ color: {MUTED}; font-size: 11.5px; margin-top: 1px; }}
.uhi-rank {{ color: {ACCENT}; margin-right: 6px; }}
.uhi-badge {{ flex: none; min-width: 38px; height: 26px; border-radius: 8px; color: #fff;
  font-weight: 800; font-size: 14px; text-align: center; line-height: 26px; padding: 0 6px;
  box-shadow: 0 0 14px rgba(249,115,22,.35); }}
.uhi-stats {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin: 10px 0 6px; }}
.uhi-stats div {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px;
  padding: 5px 6px; }}
.uhi-stats span, .uhi-rec span {{ display: block; font-size: 9.5px; letter-spacing: .06em;
  text-transform: uppercase; color: #6B7280; }}
.uhi-stats b {{ font-size: 12.5px; color: #F9FAFB; }}
.uhi-park {{ color: #6B7280; font-size: 11px; margin-bottom: 8px; }}
.uhi-rec {{ border-left: 3px solid {EMERALD}; background: rgba(16,185,129,.08);
  padding: 6px 8px; border-radius: 6px; color: #D1FAE5; }}
.uhi-rec.uhi-off {{ border-left-color: #4B5563; background: rgba(75,85,99,.15); color: {MUTED}; }}
.uhi-pin-wrap {{ background: transparent; border: 0; }}
.uhi-pin {{ position: relative; width: 30px; height: 30px; border-radius: 50%; background: {BG};
  border: 2px solid {ACCENT}; color: #FDBA74; font: 800 13px/26px Inter, system-ui, sans-serif;
  text-align: center; box-shadow: 0 0 0 3px rgba(249,115,22,.22), 0 0 16px 4px rgba(249,115,22,.6);
  }}
.uhi-pin-top {{ background: {ACCENT}; color: {BG}; border-color: #FED7AA; }}
.uhi-pin:after {{ content: ""; position: absolute; inset: -2px; border-radius: 50%;
  border: 2px solid {ACCENT}; animation: uhiPulse 2.4s ease-out infinite; }}
@keyframes uhiPulse {{ 0% {{ transform: scale(1); opacity: .8; }}
  100% {{ transform: scale(2.1); opacity: 0; }} }}
.uhi-legend {{ position: absolute; left: 12px; bottom: 28px; z-index: 1000; width: 250px;
  background: rgba(11,15,25,.9); border: 1px solid {BORDER}; border-radius: 12px;
  padding: 10px 12px; color: {TEXT}; font: 12px/1.35 Inter, system-ui, sans-serif;
  box-shadow: 0 12px 32px rgba(0,0,0,.5); backdrop-filter: blur(6px); }}
.uhi-legend .uhi-kicker {{ color: {MUTED}; }}
.uhi-ramp {{ height: 8px; border-radius: 4px; margin: 6px 0 3px; }}
.uhi-ticks {{ display: flex; justify-content: space-between; color: {MUTED}; font-size: 10.5px; }}
.uhi-keys {{ display: flex; flex-wrap: wrap; gap: 6px 12px; margin-top: 8px; font-size: 11px;
  color: #D1D5DB; }}
.uhi-keys i {{ display: inline-block; width: 12px; height: 12px; border-radius: 3px;
  vertical-align: -2px; margin-right: 5px; }}
</style>
"""


def _legend_html(lo: float, hi: float, index: list[float]) -> str:
    stops = ", ".join(
        f"{colour} {100 * (v - lo) / (hi - lo):.0f}%"
        for colour, v in zip(HEAT_RAMP, index, strict=True)
    )
    return (
        '<div class="uhi-legend">'
        '<div class="uhi-kicker">Summer surface heat vs. typical street</div>'
        f'<div class="uhi-ramp" style="background:linear-gradient(90deg, {stops})"></div>'
        f'<div class="uhi-ticks"><span>{lo:+.1f} °C</span><span>0</span>'
        f"<span>{hi:+.1f} °C</span></div>"
        '<div class="uhi-keys">'
        f'<span><i style="background:{EMERALD};opacity:.7"></i>Greenspace</span>'
        f'<span><i style="background:{CORRIDOR};height:4px;vertical-align:2px"></i>'
        "Tree corridor</span>"
        f'<span><i style="background:{ACCENT};border-radius:50%;'
        f'box-shadow:0 0 8px {ACCENT}"></i>Top zone</span>'
        "</div></div>"
    )


def _heat_layer(scored: gpd.GeoDataFrame, m: folium.Map) -> folium.FeatureGroup:
    lo, hi = heat_domain(scored["lst_anomaly_c"])
    index = [lo, 0.0, 0.35 * hi, 0.7 * hi, hi]
    cmap = cm.LinearColormap(HEAT_RAMP, index=index, vmin=lo, vmax=hi)
    anomaly = scored["lst_anomaly_c"]
    clipped = anomaly.clip(lo, hi)
    colours = clipped.map(lambda v: cmap(v) if pd.notna(v) else "#374151")
    # Hot cells glow through the dark basemap; cool/neutral cells recede.
    opacity = np.where(clipped >= 0, 0.28 + 0.5 * (clipped / hi), 0.28 + 0.14 * (clipped / lo))
    opacity = pd.Series(np.nan_to_num(opacity, nan=0.15).round(2), index=scored.index)
    data = gpd.GeoDataFrame(
        pd.concat(
            [
                scored[["cell_id"]],
                tooltip_props(scored),
                colours.rename("_c"),
                opacity.rename("_o"),
            ],
            axis=1,
        ),
        geometry=scored.geometry,
        crs=scored.crs,
    )
    group = folium.FeatureGroup(name="Surface heat anomaly")
    folium.GeoJson(
        data,
        style_function=lambda f: {
            "fillColor": f["properties"]["_c"],
            "fillOpacity": f["properties"]["_o"],
            "weight": 0,
        },
        highlight_function=lambda _f: {"weight": 1.5, "color": TEXT},
        on_each_feature=CELL_TOOLTIP_JS,
    ).add_to(group)
    m.get_root().html.add_child(folium.Element(_legend_html(lo, hi, index)))
    return group


def _parks_layer(parks: gpd.GeoDataFrame) -> folium.FeatureGroup:
    data = parks[["park_id", "name", "area_ha", "geometry"]].copy()
    data["name"] = data["name"].fillna("Unnamed greenspace")
    data["area_ha"] = data["area_ha"].map("{:.1f} ha".format)
    group = folium.FeatureGroup(name="Cooling greenspace ≥ 0.5 ha")
    folium.GeoJson(
        data,
        style_function=lambda _f: {
            "fillColor": EMERALD,
            "color": "#34D399",
            "opacity": 0.55,
            "fillOpacity": 0.24,
            "weight": 0.6,
        },
        highlight_function=lambda _f: {"fillOpacity": 0.6},
        on_each_feature=PARK_TOOLTIP_JS,
    ).add_to(group)
    return group


def _zones_layer(
    scored: gpd.GeoDataFrame,
    zones: gpd.GeoDataFrame,
    corridors: gpd.GeoDataFrame,
) -> folium.FeatureGroup:
    group = folium.FeatureGroup(name=f"Top {len(zones)} greening zones")
    by_id = scored.set_index("cell_id", drop=False)
    rows = by_id.loc[zones["cell_id"].to_numpy()]
    props = tooltip_props(rows).assign(cell_id=rows["cell_id"].to_numpy())
    props["rk"] = zones["zone_rank"].to_numpy()

    cells = gpd.GeoDataFrame(props, geometry=rows.geometry.to_numpy(), crs=scored.crs)
    folium.GeoJson(
        cells,
        style_function=lambda _f: {
            "color": TEXT,
            "weight": 2,
            "dashArray": "4 3",
            "fillColor": ACCENT,
            "fillOpacity": 0.14,
        },
        highlight_function=lambda _f: {"fillOpacity": 0.3, "weight": 3},
        on_each_feature=CELL_TOOLTIP_JS,
    ).add_to(group)

    lines = corridors.loc[corridors["cell_id"].isin(zones["cell_id"])]
    if len(lines):
        lines = lines[["cell_id", "main_street", "geometry"]]
        folium.GeoJson(  # soft glow underlay
            lines,
            style_function=lambda _f: {"color": CORRIDOR, "weight": 14, "opacity": 0.18},
        ).add_to(group)
        folium.GeoJson(
            lines,
            style_function=lambda _f: {"color": CORRIDOR, "weight": 4, "opacity": 0.95},
            on_each_feature=CORRIDOR_TOOLTIP_JS,
        ).add_to(group)

    centres = [((b[1] + b[3]) / 2, (b[0] + b[2]) / 2) for b in cells.geometry.bounds.to_numpy()]
    pins = gpd.GeoDataFrame(
        props,
        geometry=gpd.points_from_xy([c[1] for c in centres], [c[0] for c in centres]),
        crs=scored.crs,
    )
    folium.GeoJson(
        pins,
        marker=folium.Marker(icon=folium.DivIcon(html="", class_name="uhi-pin-wrap")),
        on_each_feature=PIN_JS,
    ).add_to(group)
    return group


def build_map(
    scored: gpd.GeoDataFrame,
    zones: gpd.GeoDataFrame,
    layers: dict[str, gpd.GeoDataFrame],
    toggles: tuple[bool, bool, bool],
) -> folium.Map:
    """Build a fresh Folium map for the current scores, zones and layer toggles.

    Deliberately not cached: streamlit-folium leaves stale variable names in its output when
    the same ``folium.Map`` object is rendered twice, which blanks the map on the next rerun
    (e.g. after a click). Construction is cheap; GeoJSON serialisation runs every rerun anyway.
    """
    show_heat, show_parks, show_zones = toggles

    m = folium.Map(
        location=list(config.EDINBURGH_CENTER_LATLON),
        zoom_start=12,
        tiles=None,
        control_scale=True,
        prefer_canvas=True,
    )
    m.get_root().header.add_child(folium.Element(MAP_CSS))
    bases, labels = basemap_tiles(carto_api_key())
    for i, base in enumerate(bases):  # only the first (dark) base layer starts visible
        folium.TileLayer(
            tiles=base.url,
            attr=base.attr,
            name=base.name,
            overlay=False,
            show=i == 0,
            max_zoom=19,
        ).add_to(m)

    if show_heat:
        _heat_layer(scored, m).add_to(m)
    if show_parks:
        _parks_layer(layers["parks"]).add_to(m)
    if show_zones:
        _zones_layer(scored, zones, layers["corridors"]).add_to(m)

    if labels is not None:
        # Street/place labels float above the choropleth but below pins and tooltips.
        folium.map.CustomPane("labels", z_index=450, pointer_events=False).add_to(m)
        folium.TileLayer(
            tiles=labels.url,
            attr=labels.attr,
            name=labels.name,
            overlay=True,
            pane="labels",
            max_zoom=19,
        ).add_to(m)

    minx, miny, maxx, maxy = scored.total_bounds
    m.fit_bounds([[miny, minx], [maxy, maxx]])
    folium.LayerControl(collapsed=True).add_to(m)
    return m


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------
APP_CSS = f"""
<style>
.block-container, [data-testid="stMainBlockContainer"] {{
  padding-top: 1.5rem; padding-bottom: 1.5rem; max-width: 100%; }}
#MainMenu, footer, [data-testid="stMainMenu"], [data-testid="stToolbarActions"],
[data-testid="stAppDeployButton"], [data-testid="stDecoration"] {{ display: none !important; }}
header[data-testid="stHeader"] {{ background: transparent; }}

/* KPI cards */
[data-testid="stMetric"] {{ background: rgba(17,24,39,.55); border: 1px solid {BORDER};
  border-radius: 14px; padding: .85rem 1.1rem; backdrop-filter: blur(6px);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.03); }}
[data-testid="stMetricLabel"] p {{ font-size: .7rem; letter-spacing: .09em;
  text-transform: uppercase; color: {MUTED}; font-weight: 600; }}
[data-testid="stMetricValue"] {{ font-size: 1.65rem; font-weight: 700; color: #F9FAFB; }}

/* Sidebar */
[data-testid="stSidebar"] {{ background: #0D1320; border-right: 1px solid {BORDER}; }}
[data-testid="stSidebar"] hr {{ margin: .9rem 0; border: 0; border-top: 1px solid {BORDER}; }}
.uhi-side-h {{ font-size: .68rem; font-weight: 700; letter-spacing: .14em;
  text-transform: uppercase; color: {ACCENT}; margin: .25rem 0 .55rem; }}

/* Map frame */
iframe[title*="st_folium"] {{ border: 1px solid {BORDER} !important; border-radius: 14px;
  background: {BG}; box-shadow: 0 18px 50px rgba(0,0,0,.45); }}

/* Data panels */
[data-testid="stDataFrame"] {{ border: 1px solid {BORDER}; border-radius: 12px; overflow: hidden; }}
[data-testid="stExpander"] details {{ border: 1px solid {BORDER}; border-radius: 12px;
  background: rgba(17,24,39,.4); }}
[data-testid="stTabs"] button p {{ font-size: .8rem; font-weight: 600; letter-spacing: .02em; }}

/* Hero */
.uhi-hero {{ display: flex; justify-content: space-between; align-items: flex-end; gap: 1rem;
  flex-wrap: wrap; margin-bottom: 1rem; }}
.uhi-eyebrow {{ font-size: .7rem; font-weight: 700; letter-spacing: .16em;
  text-transform: uppercase; color: {ACCENT}; display: flex; align-items: center; gap: .5rem; }}
.uhi-dot {{ width: 8px; height: 8px; border-radius: 50%; background: {ACCENT};
  box-shadow: 0 0 10px {ACCENT}; animation: uhiBlink 1.8s ease-in-out infinite; }}
@keyframes uhiBlink {{ 50% {{ opacity: .35; }} }}
.uhi-hero h1 {{ font-size: 2rem; font-weight: 800; letter-spacing: -.02em; margin: .2rem 0 .15rem;
  padding: 0; color: #F9FAFB; }}
.uhi-hero p {{ color: {MUTED}; margin: 0; font-size: .92rem; }}
.uhi-chips {{ display: flex; gap: .4rem; flex-wrap: wrap; }}
.uhi-chip {{ border: 1px solid {BORDER}; background: rgba(17,24,39,.7); border-radius: 999px;
  padding: .25rem .7rem; font-size: .75rem; color: #D1D5DB; white-space: nowrap; }}
.uhi-chip b {{ color: #F9FAFB; }}

/* Inspector card */
.uhi-insp {{ background: rgba(17,24,39,.55); border: 1px solid {BORDER}; border-radius: 14px;
  padding: 1rem 1.1rem; margin-bottom: .8rem; }}
.uhi-insp-top {{ display: flex; justify-content: space-between; gap: .8rem;
  align-items: flex-start; }}
.uhi-insp-k {{ font-size: .66rem; font-weight: 700; letter-spacing: .14em;
  text-transform: uppercase; color: {ACCENT}; }}
.uhi-insp-nb {{ font-size: 1.25rem; font-weight: 800; color: #F9FAFB; line-height: 1.2; }}
.uhi-insp-st {{ color: {MUTED}; font-size: .85rem; }}
.uhi-insp-score {{ text-align: center; border-radius: 12px; padding: .35rem .7rem; color: #fff;
  min-width: 64px; }}
.uhi-insp-score b {{ display: block; font-size: 1.6rem; line-height: 1.1; }}
.uhi-insp-score span {{ font-size: .62rem; letter-spacing: .1em; text-transform: uppercase;
  opacity: .85; }}
.uhi-insp-stats {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: .45rem;
  margin: .85rem 0; }}
.uhi-insp-stats div {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 10px;
  padding: .45rem .55rem; }}
.uhi-insp-stats span {{ display: block; font-size: .6rem; letter-spacing: .08em;
  text-transform: uppercase; color: #6B7280; }}
.uhi-insp-stats b {{ font-size: .98rem; color: #F9FAFB; }}
.uhi-bar {{ margin: .4rem 0; font-size: .75rem; color: #D1D5DB; }}
.uhi-bar-row {{ display: flex; justify-content: space-between; }}
.uhi-bar-row span:last-child {{ color: {MUTED}; }}
.uhi-bar-track {{ height: 6px; border-radius: 3px; background: {BORDER}; margin-top: 3px;
  overflow: hidden; }}
.uhi-bar-fill {{ height: 100%; border-radius: 3px; }}
.uhi-insp-rec {{ margin-top: .8rem; border-left: 3px solid {EMERALD};
  background: rgba(16,185,129,.08); border-radius: 8px; padding: .55rem .75rem; color: #D1FAE5;
  font-size: .85rem; }}
.uhi-insp-rec span {{ display: block; font-size: .6rem; letter-spacing: .1em;
  text-transform: uppercase; color: {EMERALD}; font-weight: 700; }}
.uhi-insp-rec.uhi-off {{ border-left-color: #4B5563; background: rgba(75,85,99,.15);
  color: {MUTED}; }}
.uhi-insp-rec.uhi-off span {{ color: {MUTED}; }}
.uhi-hint {{ color: #6B7280; font-size: .75rem; margin: -.3rem 0 .6rem; }}
</style>
"""


def inject_css() -> None:
    """Global dark command-center styling for Streamlit chrome, cards and panels."""
    st.markdown(APP_CSS, unsafe_allow_html=True)


def render_hero(meta: dict[str, Any], weights: Weights, k: int) -> None:
    """Title block with live model chips."""
    lst = meta.get("lst") or {}
    years = (lst.get("date_range") or [f"{config.LST_YEARS[0]}", f"{config.LST_YEARS[1]}"])[:2]
    chips = "".join(
        f'<span class="uhi-chip">{label} <b>{value}</b></span>'
        for label, value in (
            ("Heat", f"{weights.thermal:.2f}"),
            ("Park gap", f"{weights.distance:.2f}"),
            ("Sealed", f"{weights.impervious:.2f}"),
            ("Top-K", str(k)),
        )
    )
    st.markdown(
        '<div class="uhi-hero"><div>'
        '<div class="uhi-eyebrow"><span class="uhi-dot"></span>'
        "Edinburgh · Urban heat island command center</div>"
        "<h1>Where should Edinburgh plant its next trees?</h1>"
        f"<p>Summer surface heat (Landsat 8/9, {years[0][:4]}–{years[1][:4]}) × distance to "
        "greenspace × sealed ground · re-ranked live from the control panel ›</p>"
        f'</div><div class="uhi-chips">{chips}</div></div>',
        unsafe_allow_html=True,
    )


def render_kpis(meta: dict[str, Any], n_zones: int) -> None:
    """Four KPI cards, all values read from ``scoring_metadata.json``."""
    kpis, lst = meta["kpis"], meta.get("lst") or {}
    c1, c2, c3, c4 = st.columns(4)
    c1.metric(
        "Peak zone heat anomaly",
        f"+{kpis['peak_zone_anomaly_c']:.1f} °C",
        help="Median summer daytime surface-temperature anomaly of the hottest top-ranked cell, "
        "vs. a typical Edinburgh street.",
    )
    c2.metric(
        "Blocks beyond 200 m of a park",
        f"{kpis['pct_urban_cells_beyond_doorstep']:.0f} %",
        help="Urban blocks more than 200 m from a ≥ 0.5 ha greenspace (Natural England's "
        "doorstep standard).",
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
        "Clear-sky Landsat passes",
        "n/a" if scenes is None else str(scenes),
        help=help_txt,
    )


def zone_table(zones: pd.DataFrame) -> pd.DataFrame:
    """Ranked Top-K table with numeric columns left unformatted for ``column_config``."""
    return pd.DataFrame(
        {
            "Rank": zones["zone_rank"].to_numpy(),
            "Zone": zones["neighbourhood"].fillna("Edinburgh").astype(str).to_numpy(),
            "Street": zones["main_street"].fillna("—").astype(str).to_numpy(),
            "Score": zones["priority_score"].round(1).to_numpy(),
            "LST": zones["lst_anomaly_c"].round(1).to_numpy(),
            "Gap": zones["dist_park_m"].round().to_numpy(),
            "Sealed": (zones["impervious_frac"] * 100).round().to_numpy(),
            "Action": zones["intervention"].map(INTERVENTION_LABELS).to_numpy(),
        }
    )


def breakdown_chart(zones: pd.DataFrame) -> alt.LayerChart:
    """Dark horizontal stacked bar of score contributions per zone, with totals."""
    labels = "#" + zones["zone_rank"].astype(str) + "  " + zones["neighbourhood"].astype(str)
    long = (
        zones[["zone_rank", *COMPONENT_COLUMNS]]
        .assign(Zone=labels.to_numpy())
        .melt(id_vars=["zone_rank", "Zone"], var_name="Component", value_name="Points")
    )
    long["Component"] = long["Component"].map({col: label for label, col, _ in COMPONENTS})
    totals = pd.DataFrame(
        {
            "zone_rank": zones["zone_rank"].to_numpy(),
            "Zone": labels.to_numpy(),
            "Total": zones["priority_score"].to_numpy(),
        }
    )
    y = alt.Y("Zone:N", sort=labels.tolist(), title=None)  # explicit rank order
    bars = (
        alt.Chart(long)
        .mark_bar(cornerRadiusEnd=3, height={"band": 0.7})
        .encode(
            y=y,
            x=alt.X(
                "Points:Q",
                title="Greening priority (0–100)",
                scale=alt.Scale(domain=[0, 105]),
                stack="zero",
            ),
            color=alt.Color(
                "Component:N",
                scale=alt.Scale(
                    domain=[c[0] for c in COMPONENTS], range=[c[2] for c in COMPONENTS]
                ),
                legend=alt.Legend(orient="bottom", title=None),
            ),
            order=alt.Order("Component:N", sort="ascending"),
            tooltip=["Zone", "Component", alt.Tooltip("Points:Q", format=".1f")],
        )
    )
    text = (
        alt.Chart(totals)
        .mark_text(align="left", dx=4, color=TEXT, fontWeight="bold", fontSize=11)
        .encode(y=y, x="Total:Q", text=alt.Text("Total:Q", format=".0f"))
    )
    return (
        alt.layer(bars, text)
        .properties(height=26 * len(zones) + 20, background=BG)
        .configure_view(strokeWidth=0)
        .configure_axis(
            labelColor=MUTED,
            titleColor=MUTED,
            gridColor=BORDER,
            domainColor=BORDER,
            tickColor=BORDER,
            labelFontSize=11,
            titleFontSize=11,
            titleFontWeight="normal",
        )
        .configure_axisY(grid=False, labelLimit=170)
        .configure_legend(labelColor="#D1D5DB", labelFontSize=11, symbolType="square")
    )


def render_zone_table(zones: pd.DataFrame) -> None:
    """Ranked Top-K table plus a stacked score-breakdown chart."""
    tab_table, tab_chart = st.tabs(["🏆 Ranking", "📊 Score breakdown"])
    with tab_table:
        table = zone_table(zones)
        st.dataframe(
            table,
            hide_index=True,
            column_order=["Rank", "Zone", "Score", "LST", "Gap", "Sealed", "Street", "Action"],
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


def inspector_html(selected: pd.Series, rank: int | None, weights: Weights) -> str:
    """Dark HTML card describing one grid cell."""
    esc = html.escape
    score = float(selected["priority_score"])
    anomaly = selected["lst_anomaly_c"]
    anomaly_txt = "n/a" if pd.isna(anomaly) else f"{anomaly:+.1f} °C"
    street = selected["main_street"] or "No named main street"
    kicker = f"Zone #{rank} of top-K" if rank else "Selected cell"

    bars = []
    for (label, col, colour), w in zip(
        COMPONENTS, (weights.thermal, weights.distance, weights.impervious), strict=True
    ):
        cap, value = 100.0 * w, float(selected[col])
        pct = min(value / cap, 1.0) * 100 if cap > 0 else 0.0
        bars.append(
            f'<div class="uhi-bar"><div class="uhi-bar-row"><span>{label}</span>'
            f"<span>{value:.0f} / {cap:.0f}</span></div>"
            f'<div class="uhi-bar-track"><div class="uhi-bar-fill" '
            f'style="width:{pct:.0f}%;background:{colour}"></div></div></div>'
        )

    if bool(selected["eligible"]) and selected["recommendation"]:
        action = INTERVENTION_LABELS.get(selected["intervention"], "Recommended")
        rec = (
            f'<div class="uhi-insp-rec"><span>{esc(action)}</span>'
            f"{esc(selected['recommendation'])}</div>"
        )
    else:
        code = selected["exclusion_reason"]
        reason = EXCLUSION_LABELS.get(code, code) if code else "n/a"
        rec = f'<div class="uhi-insp-rec uhi-off"><span>Not eligible</span>{esc(reason)}</div>'

    return (
        '<div class="uhi-insp"><div class="uhi-insp-top"><div>'
        f'<div class="uhi-insp-k">{kicker}</div>'
        f'<div class="uhi-insp-nb">{esc(selected["neighbourhood"] or "Edinburgh")}</div>'
        f'<div class="uhi-insp-st">{esc(street)}</div></div>'
        f'<div class="uhi-insp-score" style="background:{score_colour(score)}">'
        f"<b>{score:.0f}</b><span>Priority</span></div></div>"
        '<div class="uhi-insp-stats">'
        f"<div><span>LST anomaly</span><b>{anomaly_txt}</b></div>"
        f"<div><span>Park gap</span><b>{selected['dist_park_m']:.0f} m</b></div>"
        f"<div><span>Impervious</span><b>{selected['impervious_frac'] * 100:.0f} %</b></div>"
        "</div>" + "".join(bars) + rec + "</div>"
    )


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


def _side_heading(text: str) -> None:
    st.markdown(f'<div class="uhi-side-h">{text}</div>', unsafe_allow_html=True)


def render_controls(
    meta: dict[str, Any], demo: bool
) -> tuple[tuple[bool, bool, bool], Weights, int]:
    """Sidebar control panel: layer toggles, model weights and K."""
    with st.sidebar:
        st.markdown("### 🛰️ Control panel")
        _side_heading("Map layers")
        show_heat = st.toggle("Surface heat anomaly", value=True, key="t_heat")
        show_parks = st.toggle("Cooling greenspace", value=True, key="t_parks")
        show_zones = st.toggle("Top greening zones", value=not demo, key="t_zones")

        st.divider()
        _side_heading("Model weights")
        raw = (
            st.slider("🔥 Heat", 0.0, 1.0, DEFAULT_WEIGHTS[0], 0.05, key="w_thermal"),
            st.slider("🌳 Park gap", 0.0, 1.0, DEFAULT_WEIGHTS[1], 0.05, key="w_distance"),
            st.slider("🧱 Sealed surface", 0.0, 1.0, DEFAULT_WEIGHTS[2], 0.05, key="w_impervious"),
        )
        weights = normalise_weights(raw)
        if sum(raw) <= 0:
            st.warning("All weights are zero; using defaults.")
        st.caption(
            f"Normalised: heat {weights.thermal:.2f} · park gap {weights.distance:.2f} · "
            f"sealed {weights.impervious:.2f}"
        )

        st.divider()
        _side_heading("Zone selection")
        k = st.slider("Zones (K)", 10, 15, config.TOPK_K, key="k_zones")
        st.button("↺ Restore defaults", on_click=_reset_model, width="stretch")

        st.divider()
        st.caption(
            "Live re-ranking. Robustness of the default top-15 to ±0.1 weight changes: "
            f"mean Jaccard **{meta['sensitivity']['jaccard_mean']:.2f}**."
        )
    return (show_heat, show_parks, show_zones), weights, k


def main() -> None:
    """Render the dashboard."""
    inject_css()
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

    toggles, weights, k = render_controls(meta, demo)
    render_hero(meta, weights, k)

    scored, zones = rerank(layers["grid"], weights, k, min_sep_m)
    render_kpis(meta, len(zones))
    st.write("")

    map_col, side_col = st.columns([0.7, 0.3], gap="medium")
    with map_col:
        fmap = build_map(scored, zones, layers, toggles)
        out = st_folium(
            fmap,
            height=MAP_HEIGHT_PX,
            use_container_width=True,
            returned_objects=["last_active_drawing"],
            key="map",
        )
    clicked = ((out or {}).get("last_active_drawing") or {}).get("properties", {}).get("cell_id")
    by_id = scored.set_index("cell_id", drop=False)
    selected_id = clicked if clicked in by_id.index else zones.iloc[0]["cell_id"]

    with side_col:
        render_inspector(by_id.loc[selected_id], zones, weights)
        render_zone_table(zones)

    render_footer()


main()
