"""Unit tests for the dashboard's presentation layer (``edinburgh_uhi.ui``).

Pure functions only: session state is a plain ``dict`` and inputs are hand-built pandas objects,
so this runs offline in the default suite with no Streamlit runtime and no cached data.
"""

from __future__ import annotations

import pandas as pd
import pytest

from edinburgh_uhi import config
from edinburgh_uhi.config import Weights
from edinburgh_uhi.ui.components import (
    exclusion_label,
    hero_html,
    inspector_html,
    score_colour,
    zone_table,
)
from edinburgh_uhi.ui.map_view import heat_domain, tooltip_props
from edinburgh_uhi.ui.model import DEFAULT_WEIGHTS, normalise_weights
from edinburgh_uhi.ui.state import (
    MAP_KEY,
    SELECTED_KEY,
    consume_map_click,
    init_state,
    model_from_state,
    reset_model,
    selected_cell_id,
)
from edinburgh_uhi.ui.theme import AMBER, CRIMSON, SLATE

DEFAULT = Weights(*DEFAULT_WEIGHTS)


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
    assert consume_map_click(state) is False  # same click reported again on a later run


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


def test_hero_html_shows_live_weights() -> None:
    meta = {"lst": {"date_range": ["2019-06-01", "2025-08-31"]}}
    out = hero_html(meta, Weights(thermal=0.6, distance=0.2, impervious=0.2), k=12)
    assert "Heat <b>0.60</b>" in out
    assert "Top-K <b>12</b>" in out
    assert "2019–2025" in out


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
