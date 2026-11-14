"""Folium map: basemaps, heat / greenspace / top-zone layers, hover cards and legend.

All geometry arriving here is already EPSG:4326 (``data/processed/web/``); nothing is reprojected.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import branca.colormap as cm
import folium
import geopandas as gpd
import numpy as np
import pandas as pd
import streamlit as st

from edinburgh_uhi import config
from edinburgh_uhi.ui.components import exclusion_label
from edinburgh_uhi.ui.theme import (
    ACCENT,
    CORRIDOR,
    EMERALD,
    EMERALD_LINE,
    FONT_STACK,
    HEAT_RAMP,
    INTER_CSS_URL,
    NODATA,
    TEXT,
    css_vars,
)

CARTO_ATTR = "© OpenStreetMap contributors © CARTO"
CARTO_URL = "https://basemaps.cartocdn.com/rastertiles/{style}/{{z}}/{{x}}/{{y}}{{r}}.png?key={key}"
ESRI_ATTR = (
    "Tiles © Esri — Esri, HERE, Garmin, © OpenStreetMap contributors, and the GIS user community"
)
ESRI_URL = (
    "https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/{layer}/MapServer/tile/"
    "{{z}}/{{y}}/{{x}}"
)

MAP_HEIGHT_PX = 900  # nominal; CSS stretches the st_folium iframe to 100vh x 100vw


def _nullable(series: pd.Series) -> pd.Series:
    """Object series with missing values as ``None`` (serialises to JSON ``null``)."""
    return series.astype(object).where(series.notna(), None)


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
const tier = (s) => s >= 75 ? 'var(--heat-hot)' : s >= 50 ? 'var(--heat)'
  : s >= 25 ? 'var(--heat-warm)' : 'var(--sealed)';
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
    'style="color:var(--green)">Cooling greenspace</div><div class="uhi-nb">\' + esc(p.name) '
    "+ '</div><div class=\"uhi-st\">' + p.area_ha + '</div></div>', tipOpts);"
)
CORRIDOR_TOOLTIP_JS = _on_each_feature(
    'layer.bindTooltip(\'<div class="uhi-card uhi-mini"><div class="uhi-kicker" '
    'style="color:var(--corridor)">Proposed tree corridor</div><div class="uhi-nb">\' '
    "+ esc(p.main_street) + '</div></div>', tipOpts);"
)

# The st_folium iframe is its own document: it gets its own copy of the tokens and of Inter.
_MAP_STYLES = """
.leaflet-container { background: var(--bg); font-family: FONT_STACK; }
.leaflet-top.leaflet-left, .leaflet-bottom.leaflet-left { margin-left: 390px; }
html, body { margin: 0; height: 100%; overflow: hidden; }
.folium-map, .leaflet-container { width: 100% !important; height: 100vh !important; }
@media (max-width: 768px) {
  .leaflet-top.leaflet-left, .leaflet-bottom.leaflet-left { margin-left: 0; }
  .leaflet-bottom { bottom: 272px; }  /* clears the collapsed "peek" sheet (~264 px) */
  .uhi-legend { display: none; }
}
.leaflet-bar { border: 1px solid var(--border) !important; border-radius: 10px !important;
  overflow: hidden; box-shadow: var(--shadow-lg) !important; }
.leaflet-bar a, .leaflet-bar a:hover { background: var(--surface-2); color: var(--text);
  border-bottom: 1px solid var(--border); }
.leaflet-bar a:hover { background: var(--surface-3); }
.leaflet-control-layers {
  background: color-mix(in srgb, var(--surface) 94%, transparent) !important;
  color: var(--text); border: 1px solid var(--border) !important; border-radius: 10px !important;
  box-shadow: var(--shadow-lg) !important; }
.leaflet-control-layers-toggle { filter: invert(.92); }
.leaflet-control-layers-separator { border-top-color: var(--border) !important; }
.leaflet-control-layers label { font-size: 12px; }
.leaflet-control-attribution {
  background: color-mix(in srgb, var(--bg) 75%, transparent) !important;
  color: var(--faint) !important; font-size: 10px; }
.leaflet-control-attribution a { color: var(--muted) !important; }
.leaflet-control-scale-line { background: color-mix(in srgb, var(--surface) 80%, transparent);
  color: var(--text); border-color: var(--faint); }
.leaflet-tooltip.uhi-tip { background: transparent; border: 0; box-shadow: none; padding: 0;
  white-space: normal; }
.leaflet-tooltip.uhi-tip:before { display: none; }
.uhi-card { width: 260px; background: color-mix(in srgb, var(--bg) 96%, transparent);
  color: var(--text); border: 1px solid var(--border); border-radius: 12px; padding: 12px 14px;
  box-shadow: var(--shadow-lg); font-size: 12px; line-height: 1.35; }
.uhi-card.uhi-mini { width: auto; min-width: 160px; max-width: 240px; padding: 9px 12px; }
.uhi-kicker { font-size: 10px; font-weight: 700; letter-spacing: .1em; text-transform: uppercase;
  margin-bottom: 2px; }
.uhi-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; }
.uhi-title { min-width: 0; }
.uhi-nb { font-size: 14px; font-weight: 700; color: var(--text-hi); }
.uhi-st { color: var(--muted); font-size: 11.5px; margin-top: 1px; }
.uhi-rank { color: var(--heat); margin-right: 6px; }
.uhi-badge { flex: none; min-width: 38px; height: 26px; border-radius: 8px; color: var(--text-hi);
  font-weight: 800; font-size: 14px; text-align: center; line-height: 26px; padding: 0 6px;
  font-variant-numeric: tabular-nums;
  box-shadow: 0 0 14px color-mix(in srgb, var(--heat) 35%, transparent); }
.uhi-stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin: 10px 0 6px; }
.uhi-stats div { background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px;
  padding: 5px 6px; }
.uhi-stats span, .uhi-rec span { display: block; font-size: 9.5px; letter-spacing: .06em;
  text-transform: uppercase; color: var(--faint); }
.uhi-rec span { color: var(--green); font-weight: 600; }
.uhi-rec.uhi-off span { color: var(--muted); }
.uhi-stats b { font-size: 12.5px; color: var(--text-hi); font-variant-numeric: tabular-nums; }
.uhi-park { color: var(--faint); font-size: 11px; margin-bottom: 8px; }
.uhi-rec { border-left: 3px solid var(--green);
  background: color-mix(in srgb, var(--green) 8%, transparent);
  padding: 6px 8px; border-radius: 6px; color: var(--green-pale); }
.uhi-rec.uhi-off { border-left-color: var(--faint);
  background: color-mix(in srgb, var(--faint) 18%, transparent); color: var(--muted); }
.uhi-pin-wrap { background: transparent; border: 0; }
.uhi-pin { position: relative; width: 30px; height: 30px; border-radius: 50%;
  background: var(--bg); border: 2px solid var(--heat); color: var(--heat-soft);
  font: 800 13px/26px FONT_STACK; text-align: center;
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--heat) 22%, transparent),
    0 0 16px 4px color-mix(in srgb, var(--heat) 60%, transparent); }
.uhi-pin-top { background: var(--heat); color: var(--bg); border-color: var(--heat-pale); }
.uhi-pin:after { content: ""; position: absolute; inset: -2px; border-radius: 50%;
  border: 2px solid var(--heat); animation: uhiPulse 2.4s ease-out infinite; }
@keyframes uhiPulse { 0% { transform: scale(1); opacity: .8; }
  100% { transform: scale(2.1); opacity: 0; } }
.uhi-legend { position: absolute; right: 12px; bottom: 34px; z-index: 1000; width: 250px;
  background: color-mix(in srgb, var(--bg) 90%, transparent); border: 1px solid var(--border);
  border-radius: 12px; padding: 10px 12px; color: var(--text); font: 12px/1.35 FONT_STACK;
  box-shadow: var(--shadow-lg); backdrop-filter: blur(6px); }
.uhi-legend .uhi-kicker { color: var(--muted); }
.uhi-ramp { height: 8px; border-radius: 4px; margin: 6px 0 3px; }
.uhi-ticks { display: flex; justify-content: space-between; color: var(--muted); font-size: 10.5px;
  font-variant-numeric: tabular-nums; }
.uhi-keys { display: flex; flex-wrap: wrap; gap: 6px 12px; margin-top: 8px; font-size: 11px;
  color: var(--text); }
.uhi-keys i { display: inline-block; width: 12px; height: 12px; border-radius: 3px;
  vertical-align: -2px; margin-right: 5px; }
""".replace("FONT_STACK", FONT_STACK)

MAP_CSS = (
    f'<link rel="stylesheet" href="{INTER_CSS_URL}">\n'
    f"<style>\n{css_vars()}\n{_MAP_STYLES}</style>\n"
)


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
    colours = clipped.map(lambda v: cmap(v) if pd.notna(v) else NODATA)
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
            "color": EMERALD_LINE,
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
