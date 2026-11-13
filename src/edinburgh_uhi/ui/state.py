"""Session-state model for the dashboard.

Session state is the single source of truth for the model settings. Widgets are drawn with
``key=`` only (never ``value=``) and the app reads the settings from state, not from widget return
values, so it can draw controls anywhere in the page (or not at all on a given run) without
losing them. Every function takes the state mapping as an argument, so it can be tested with a
plain ``dict``.
"""

from __future__ import annotations

from collections.abc import Collection, MutableMapping
from dataclasses import dataclass
from typing import Any

from edinburgh_uhi import config
from edinburgh_uhi.config import Weights
from edinburgh_uhi.ui.model import DEFAULT_WEIGHTS, normalise_weights

State = MutableMapping[str, Any]

WEIGHT_KEYS = ("w_thermal", "w_distance", "w_impervious")
TOGGLE_KEYS = ("t_heat", "t_parks", "t_zones")
MAP_KEY = "map"  # st_folium copies each click into this plain (non-widget) key
SELECTED_KEY = "sel_cell"

MODEL_DEFAULTS: dict[str, Any] = {
    **dict(zip(WEIGHT_KEYS, DEFAULT_WEIGHTS, strict=True)),
    "k_zones": config.TOPK_K,
}
# Widget-backed keys that must survive runs on which their widget isn't drawn.
PERSISTENT_KEYS = (*WEIGHT_KEYS, "k_zones", *TOGGLE_KEYS)


@dataclass(frozen=True)
class ModelState:
    """Everything the re-ranking and the map need from the controls."""

    weights: Weights
    k: int
    toggles: tuple[bool, bool, bool]
    weights_all_zero: bool


def init_state(state: State, demo: bool) -> None:
    """Seed missing keys with defaults and detach widget keys from widget clean-up.

    Streamlit deletes a widget's value on any run where that widget isn't drawn. Re-assigning
    each key to itself before the widgets are created keeps the value across such runs.
    """
    for key, value in MODEL_DEFAULTS.items():
        state.setdefault(key, value)
    state.setdefault("t_heat", True)
    state.setdefault("t_parks", True)
    state.setdefault("t_zones", not demo)  # ?demo=1 starts with zones hidden for the reveal
    state.setdefault(SELECTED_KEY, None)
    for key in PERSISTENT_KEYS:
        state[key] = state[key]


def reset_model(state: State) -> None:
    """``on_click`` callback: restore default weights and K (layer toggles are left alone)."""
    state.update(MODEL_DEFAULTS)


def clicked_cell(state: State) -> str | None:
    """``cell_id`` of the feature last clicked on the map, if any."""
    out = state.get(MAP_KEY) or {}
    props = (out.get("last_active_drawing") or {}).get("properties") or {}
    cell = props.get("cell_id")
    return str(cell) if cell is not None else None


def consume_map_click(state: State) -> bool:
    """Record a new map click as the selected cell; return True if the selection changed.

    ``last_active_drawing`` keeps reporting the last click on every later run, so only a click
    on a different feature counts as new.
    """
    cell = clicked_cell(state)
    if cell is None or cell == state.get(SELECTED_KEY):
        return False
    state[SELECTED_KEY] = cell
    return True


def model_from_state(state: State) -> ModelState:
    """Normalised weights, K and layer toggles from session state."""
    thermal, distance, impervious = (float(state[key]) for key in WEIGHT_KEYS)
    heat, parks, zones = (bool(state[key]) for key in TOGGLE_KEYS)
    raw = (thermal, distance, impervious)
    return ModelState(
        weights=normalise_weights(raw),
        k=int(state["k_zones"] or config.TOPK_K),
        toggles=(heat, parks, zones),
        weights_all_zero=sum(raw) <= 0,
    )


def selected_cell_id(state: State, valid_ids: Collection[str], fallback: str) -> str:
    """The selected cell if it still exists in the grid, else ``fallback`` (zone #1)."""
    cell = state.get(SELECTED_KEY)
    return cell if cell in valid_ids else fallback
