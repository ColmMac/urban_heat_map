"""Unit tests for the dashboard's presentation layer (``edinburgh_uhi.ui``).

Pure functions only: session state is a plain ``dict`` and inputs are hand-built pandas objects,
so this runs offline in the default suite with no Streamlit runtime and no cached data.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import folium
import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString, box
from streamlit_folium import _get_map_string, generate_js_hash

from edinburgh_uhi import config
from edinburgh_uhi.config import Weights
from edinburgh_uhi.ui.components import (
    exclusion_label,
    header_html,
    inspector_html,
    kpi_glossary_md,
    kpi_items,
    kpi_strip_html,
    peek_html,
    ranked_list_html,
    score_colour,
    weight_mix_html,
    zone_rank,
    zone_table,
    zones_csv,
)
from edinburgh_uhi.ui.map_view import (
    _CARD_JS,
    _MAP_STYLES,
    CORRIDOR_TOOLTIP_JS,
    MAP_CSS,
    PARK_TOOLTIP_JS,
    build_base_map,
    dynamic_layers,
    heat_domain,
    tooltip_props,
)
from edinburgh_uhi.ui.model import DEFAULT_WEIGHTS, normalise_weights
from edinburgh_uhi.ui.state import (
    MAP_KEY,
    SELECTED_KEY,
    VIEW_KEY,
    ZONE_VIEW,
    consume_map_click,
    init_state,
    model_from_state,
    reset_model,
    selected_cell_id,
)
from edinburgh_uhi.ui.theme import (
    AMBER,
    COMPONENTS,
    CRIMSON,
    FELT_CSS,
    HEAT_RAMP,
    SLATE,
    TOKENS,
    css_vars,
    page_css,
)

DEFAULT = Weights(*DEFAULT_WEIGHTS)
CONFIG_TOML = Path(__file__).resolve().parents[1] / ".streamlit" / "config.toml"
HEX = re.compile(r"#[0-9A-Fa-f]{3,8}\b")


def _click(cell_id: str) -> dict:
    return {"last_active_drawing": {"properties": {"cell_id": cell_id}}}


def _cell(**overrides: object) -> pd.Series:
    base: dict[str, object] = {
        "cell_id": "C1",
        "neighbourhood": "Leith",
        "main_street": "Leith Walk",
        "priority_score": 82.4,
        "lst_anomaly_c": 2.34,
        "dist_park_m": 412.0,
        "impervious_frac": 0.78,
        "nearest_park_name": "Pilrig Park",
        "c_thermal": 40.0,
        "c_distance": 20.0,
        "c_impervious": 22.4,
        "eligible": True,
        "recommendation": "Street-tree corridor along Leith Walk",
        "intervention": "street_trees",
        "exclusion_reason": None,
        "zone_rank": 1,
    }
    return pd.Series({**base, **overrides})


# --------------------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------------------
def test_init_state_seeds_defaults() -> None:
    state: dict = {}
    init_state(state, demo=False)
    assert (state["w_thermal"], state["w_distance"], state["w_impervious"]) == DEFAULT_WEIGHTS
    assert state["k_zones"] == config.TOPK_K
    assert state["t_heat"] and state["t_parks"] and state["t_zones"]
    assert state[SELECTED_KEY] is None
    assert state[VIEW_KEY] is None  # collapsed "peek" sheet


def test_init_state_demo_hides_zones_only_on_first_run() -> None:
    state: dict = {}
    init_state(state, demo=True)
    assert state["t_zones"] is False
    state["t_zones"] = True  # user reveals the zones
    init_state(state, demo=True)
    assert state["t_zones"] is True


def test_init_state_keeps_existing_values() -> None:
    state: dict = {"w_thermal": 0.1, "k_zones": 11, "t_parks": False}
    init_state(state, demo=False)
    assert state["w_thermal"] == 0.1
    assert state["k_zones"] == 11
    assert state["t_parks"] is False


def test_reset_model_restores_weights_and_k_but_not_toggles() -> None:
    state: dict = {}
    init_state(state, demo=False)
    state.update(w_thermal=0.0, w_distance=1.0, k_zones=10, t_heat=False)
    reset_model(state)
    assert (state["w_thermal"], state["w_distance"]) == DEFAULT_WEIGHTS[:2]
    assert state["k_zones"] == config.TOPK_K
    assert state["t_heat"] is False


def test_consume_map_click_records_only_new_clicks() -> None:
    state: dict = {SELECTED_KEY: None}
    assert consume_map_click(state) is False  # no map output yet
    state[MAP_KEY] = _click("C7")
    assert consume_map_click(state) is True
    assert state[SELECTED_KEY] == "C7"
    assert state[VIEW_KEY] == ZONE_VIEW  # a map tap opens the inspector
    state[VIEW_KEY] = None  # user collapses the sheet
    assert consume_map_click(state) is False  # same click reported again on a later run
    assert state[VIEW_KEY] is None  # ...must not reopen it


@pytest.mark.parametrize(
    "map_out", [None, {}, {"last_active_drawing": None}, {"last_active_drawing": {}}]
)
def test_consume_map_click_ignores_empty_output(map_out: object) -> None:
    state: dict = {SELECTED_KEY: "C1", MAP_KEY: map_out}
    assert consume_map_click(state) is False
    assert state[SELECTED_KEY] == "C1"


def test_model_from_state_normalises_weights() -> None:
    state: dict = {}
    init_state(state, demo=False)
    state.update(w_thermal=1.0, w_distance=1.0, w_impervious=0.0, k_zones=12, t_parks=False)
    model = model_from_state(state)
    assert model.weights == Weights(thermal=0.5, distance=0.5, impervious=0.0)
    assert model.k == 12
    assert model.toggles == (True, False, True)
    assert model.weights_all_zero is False


def test_model_from_state_all_zero_falls_back_to_defaults() -> None:
    state: dict = {}
    init_state(state, demo=False)
    state.update(w_thermal=0.0, w_distance=0.0, w_impervious=0.0)
    model = model_from_state(state)
    assert model.weights_all_zero is True
    assert model.weights == config.WEIGHTS


def test_selected_cell_id_falls_back_when_missing() -> None:
    assert selected_cell_id({SELECTED_KEY: "C2"}, {"C1", "C2"}, "C1") == "C2"
    assert selected_cell_id({SELECTED_KEY: "gone"}, {"C1", "C2"}, "C1") == "C1"
    assert selected_cell_id({SELECTED_KEY: None}, {"C1"}, "C1") == "C1"


# --------------------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------------------
def test_normalise_weights_sums_to_one() -> None:
    w = normalise_weights((2.0, 1.0, 1.0))
    assert w.thermal + w.distance + w.impervious == pytest.approx(1.0)
    assert w.thermal == pytest.approx(0.5)


# --------------------------------------------------------------------------------------
# components
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("score", "colour"), [(90, CRIMSON), (75, CRIMSON), (30, AMBER), (24.9, SLATE)]
)
def test_score_colour_tiers(score: float, colour: str) -> None:
    assert score_colour(score) == colour


def test_exclusion_label_maps_known_codes_and_keeps_missing() -> None:
    out = exclusion_label(pd.Series(["water", "mystery_code", None]))
    assert out.iloc[0] == "Mostly water"
    assert out.iloc[1] == "mystery_code"
    assert pd.isna(out.iloc[2])


def test_inspector_html_escapes_text() -> None:
    cell = _cell(neighbourhood="<script>alert(1)</script>", main_street="A & B")
    out = inspector_html(cell, rank=3, weights=DEFAULT)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "A &amp; B" in out
    assert "Zone #3 of top-K" in out


def test_inspector_html_component_bars_use_weight_caps() -> None:
    out = inspector_html(_cell(), rank=None, weights=DEFAULT)
    assert "Selected cell" in out
    assert "40 / 50" in out  # c_thermal against 100 x 0.50
    assert "+2.3 °C" in out


def test_inspector_html_ineligible_shows_reason() -> None:
    cell = _cell(eligible=False, recommendation=None, exclusion_reason="already_green")
    out = inspector_html(cell, rank=None, weights=DEFAULT)
    assert "Not eligible" in out
    assert "Already greenspace" in out


def test_zone_table_columns_and_units() -> None:
    zones = pd.DataFrame([_cell(), _cell(cell_id="C2", zone_rank=2, neighbourhood=None)])
    table = zone_table(zones)
    assert list(table.columns) == [
        "Rank",
        "Zone",
        "Street",
        "Score",
        "LST",
        "Gap",
        "Sealed",
        "Action",
    ]
    assert table["Zone"].tolist() == ["Leith", "Edinburgh"]
    assert table["Sealed"].iloc[0] == 78
    assert table["Action"].iloc[0] == "Street trees"


META = {
    "kpis": {"peak_zone_anomaly_c": 4.04, "pct_urban_cells_beyond_doorstep": 6.2},
    "lst": {
        "date_range": ["2019-06-01", "2025-08-31"],
        "n_scenes_used": 12,
        "mean_air_tmax_c": 19.4,
    },
}


def _zones() -> pd.DataFrame:
    return pd.DataFrame(
        [
            _cell(),
            _cell(
                cell_id="C2",
                zone_rank=2,
                neighbourhood=None,
                main_street=None,
                priority_score=48.0,
                c_thermal=30.0,
                c_distance=10.0,
                c_impervious=8.0,
            ),
        ]
    )


def test_header_html_shows_years_and_title() -> None:
    out = header_html(META)
    assert "Where should Edinburgh plant its next trees?" in out
    assert "2019–2025" in out


def test_kpi_items_and_strip() -> None:
    items = kpi_items(META, n_zones=15)
    assert items[0] == ("+4.0°C", "peak zone anomaly")
    assert items[1][0] == "6%"
    assert items[2] == ("15", "zones · 94 ha")
    assert items[3][0] == "12"
    out = kpi_strip_html([("<b>", "a & b")])
    assert out.count('class="uhi-kpi"') == 1
    assert "&lt;b&gt;" in out and "a &amp; b" in out


def test_kpi_items_without_scene_count() -> None:
    assert kpi_items({"kpis": META["kpis"]}, 10)[3][0] == "n/a"


def test_kpi_glossary_mentions_dates_and_air_temperature() -> None:
    md = kpi_glossary_md(META)
    assert "2019-06-01 to 2025-08-31" in md and "19.4 °C" in md


def test_zone_rank() -> None:
    zones = _zones()
    assert zone_rank(zones, "C2") == 2
    assert zone_rank(zones, "nope") is None


def test_peek_html_for_zone_and_excluded_cell() -> None:
    out = peek_html(_cell(), rank=1)
    assert "Zone #1" in out and "Leith" in out and "Leith Walk" in out
    assert "Street trees" in out and "uhi-off" not in out
    off = peek_html(
        _cell(eligible=False, recommendation=None, exclusion_reason="water", main_street=None),
        rank=None,
    )
    assert "Selected cell" in off and "Not eligible" in off and "Mostly water" in off
    assert "uhi-off" in off and " · " not in off  # no street -> no separator


def test_peek_html_escapes() -> None:
    assert "<script>" not in peek_html(_cell(neighbourhood="<script>x</script>"), rank=None)


def test_weight_mix_html_shows_percentages() -> None:
    out = weight_mix_html(Weights(thermal=0.5, distance=0.3, impervious=0.2))
    assert "Heat <b>50%</b>" in out and "Park gap <b>30%</b>" in out
    assert "Sealed surface <b>20%</b>" in out
    assert out.count("<i style=") == 6  # 3 bar segments + 3 key swatches


def test_ranked_list_html_rows_order_and_selection() -> None:
    zones = _zones().iloc[::-1]  # out of order on purpose
    out = ranked_list_html(zones, selected_id="C2")
    assert out.count('class="uhi-rank-row') == 2
    assert out.index("Leith") < out.index("Edinburgh")  # sorted by rank
    assert out.count("is-sel") == 1
    assert "width:40.0%" in out  # c_thermal points -> bar width
    assert ">82<" in out and ">48<" in out


def test_zones_csv_has_machine_friendly_header() -> None:
    lines = zones_csv(_zones()).decode("utf-8").splitlines()
    assert lines[0].startswith("rank,neighbourhood,main_street,priority_score")
    assert len(lines) == 3


# --------------------------------------------------------------------------------------
# map_view
# --------------------------------------------------------------------------------------
def test_heat_domain_straddles_zero() -> None:
    lo, hi = heat_domain(pd.Series([0.5, 1.0, 2.0, 3.0]))
    assert lo < 0 < hi


def test_tooltip_props_hide_recommendation_when_ineligible() -> None:
    cells = pd.DataFrame(
        [
            _cell(),
            _cell(
                cell_id="C2",
                eligible=False,
                exclusion_reason="water",
                main_street=None,
                lst_anomaly_c=float("nan"),
            ),
        ]
    )
    props = tooltip_props(cells)
    assert props["rc"].iloc[0].startswith("Street-tree")
    assert props["ex"].iloc[0] is None
    assert props["rc"].iloc[1] is None
    assert props["ex"].iloc[1] == "Mostly water"
    assert props["an"].iloc[1] is None and props["st"].iloc[1] is None
    assert props["ps"].tolist() == [82, 82]


# --------------------------------------------------------------------------------------
# theme / design tokens
# --------------------------------------------------------------------------------------
def test_css_vars_declares_every_token() -> None:
    block = css_vars()
    assert block.startswith(":root{")
    for name, value in TOKENS.items():
        assert f"--{name}:{value};" in block


def test_token_values_are_colours_or_shadows() -> None:
    for name, value in TOKENS.items():
        if name.startswith("shadow"):
            assert "rgba(" in value, name
        else:
            assert HEX.fullmatch(value) or value.startswith("rgba("), name


@pytest.mark.parametrize(
    ("label", "source"),
    [
        ("page CSS", FELT_CSS),
        ("map CSS", _MAP_STYLES),
        ("hover-card JS", _CARD_JS),
        ("park tooltip JS", PARK_TOOLTIP_JS.js_code),
        ("corridor tooltip JS", CORRIDOR_TOOLTIP_JS.js_code),
    ],
)
def test_stylesheets_use_tokens_not_raw_hex(label: str, source: str) -> None:
    assert HEX.findall(source) == [], f"{label} hard-codes colours; use var(--token)"


def test_every_css_var_reference_is_a_token() -> None:
    used = set(re.findall(r"var\(--([a-z0-9-]+)\)", FELT_CSS + _MAP_STYLES + _CARD_JS))
    assert used - TOKENS.keys() == set()


def test_page_and_map_both_ship_the_tokens() -> None:
    assert css_vars() in page_css()
    assert css_vars() in MAP_CSS  # the st_folium iframe is a separate document


def test_python_palette_comes_from_tokens() -> None:
    token_values = set(TOKENS.values())
    assert {colour for _, _, colour in COMPONENTS} <= token_values
    assert set(HEAT_RAMP) <= token_values


def test_streamlit_config_mirrors_tokens() -> None:
    theme = tomllib.loads(CONFIG_TOML.read_text(encoding="utf-8"))["theme"]
    assert theme["base"] == "dark"
    assert theme["backgroundColor"] == TOKENS["bg"]
    assert theme["secondaryBackgroundColor"] == TOKENS["surface-2"]
    assert theme["textColor"] == TOKENS["text"]
    assert theme["primaryColor"] == TOKENS["text"]
    assert theme["borderColor"] == TOKENS["border"]
    assert theme["font"].startswith("Inter:https://fonts.googleapis.com/")


# --------------------------------------------------------------------------------------
# map_view: static base map + dynamic layers (no remount on reruns)
# --------------------------------------------------------------------------------------
def _web_layers() -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, dict[str, gpd.GeoDataFrame]]:
    """Tiny EPSG:4326 stand-ins for the web layers: two scored cells, one park, one corridor."""
    cells = pd.DataFrame([_cell(), _cell(cell_id="C2", zone_rank=2, lst_anomaly_c=-0.5)])
    geoms = [box(-3.20, 55.95, -3.19, 55.96), box(-3.19, 55.95, -3.18, 55.96)]
    scored = gpd.GeoDataFrame(cells, geometry=geoms, crs="EPSG:4326")
    zones = scored.copy()
    parks = gpd.GeoDataFrame(
        {"park_id": [1], "name": ["The Meadows"], "area_ha": [25.0]},
        geometry=[box(-3.195, 55.94, -3.185, 55.945)],
        crs="EPSG:4326",
    )
    corridors = gpd.GeoDataFrame(
        {"cell_id": ["C1"], "main_street": ["Leith Walk"]},
        geometry=[LineString([(-3.199, 55.951), (-3.191, 55.959)])],
        crs="EPSG:4326",
    )
    return scored, zones, {"parks": parks, "corridors": corridors}


def _map_key(m: folium.Map) -> str:
    return generate_js_hash(_get_map_string(m), "map", False)


def test_base_map_key_is_stable_across_builds() -> None:
    """st_folium keys the component on the base map's script: equal keys = no remount."""
    scored, zones, _ = _web_layers()
    assert _map_key(build_base_map(scored, zones)) == _map_key(build_base_map(scored, zones))


def test_base_map_holds_no_data_layers_and_frames_responsively() -> None:
    scored, zones, _ = _web_layers()
    script = _get_map_string(build_base_map(scored, zones))
    assert "geo_json" not in script  # data layers travel separately (feature_group_to_add)
    assert "max-width: 768px" in script and "animate: false" in script
    assert "[[55.95, -3.2], [55.96, -3.18]]" in script  # bounds rounded, lat/lng order


@pytest.mark.parametrize(
    ("toggles", "names"),
    [
        ((True, True, True), ["Surface heat anomaly", "Cooling greenspace ≥ 0.5 ha", "Top 2"]),
        ((False, True, False), ["Cooling greenspace ≥ 0.5 ha"]),
        ((False, False, False), []),
    ],
)
def test_dynamic_layers_follow_toggles(toggles: tuple[bool, bool, bool], names: list[str]) -> None:
    scored, zones, layers = _web_layers()
    groups = dynamic_layers(scored, zones, layers, toggles)
    assert len(groups) == len(names)
    for group, name in zip(groups, names, strict=True):
        assert group.layer_name.startswith(name)


def test_map_css_adapts_to_touch_and_motion() -> None:
    assert "@media (hover: none)" in _MAP_STYLES
    assert "prefers-reduced-motion" in _MAP_STYLES
    assert "100dvh" in _MAP_STYLES
    # the phone block comes after the base legend rule so it wins the cascade
    assert _MAP_STYLES.index(".uhi-legend { top: 10px") > _MAP_STYLES.index(".uhi-legend {")
