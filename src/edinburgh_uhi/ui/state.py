"""Session-state model for the dashboard.

Session state is the single source of truth for the model settings. Widgets are drawn with
``key=`` and ``persist_state="page"`` only (never ``value=``), and the app reads the settings from
state, not from widget return values. The controls live in a panel section that is only drawn
when open, so the app must work on runs where no control widget exists at all: ``init_state``
seeds every key, and ``persist_state`` keeps a widget's value while its section is closed.
Every function takes the state mapping as an argument, so it can be tested with a plain ``dict``.
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
VIEW_KEY = "panel_view"  # open panel section, or None for the collapsed "peek" sheet
ZONE_VIEW = "zone"
SHEET_HIDDEN_KEY = "sheet_hidden"  # phones only: bottom sheet closed to a "Show panel" pill
PERSIST = "page"  # persist_state for every control widget (kept while its section is closed)

MODEL_DEFAULTS: dict[str, Any] = {
    **dict(zip(WEIGHT_KEYS, DEFAULT_WEIGHTS, strict=True)),
    "k_zones": config.TOPK_K,
}


@dataclass(frozen=True)
class ModelState:
    """Everything the re-ranking and the map need from the controls."""

    weights: Weights
    k: int
    toggles: tuple[bool, bool, bool]
    weights_all_zero: bool


def init_state(state: State, demo: bool) -> None:
    """Seed every missing key with its default (existing values are never overwritten)."""
    for key, value in MODEL_DEFAULTS.items():
        state.setdefault(key, value)
    state.setdefault("t_heat", True)
    state.setdefault("t_parks", True)
    state.setdefault("t_zones", not demo)  # ?demo=1 starts with zones hidden for the reveal
    state.setdefault(SELECTED_KEY, None)
    state.setdefault(VIEW_KEY, None)
    state.setdefault(SHEET_HIDDEN_KEY, False)


def hide_sheet(state: State) -> None:
    """``on_click``: close the phone bottom sheet so the map fills the screen."""
    state[SHEET_HIDDEN_KEY] = True


def show_sheet(state: State) -> None:
    """``on_click``: bring the phone bottom sheet back."""
    state[SHEET_HIDDEN_KEY] = False


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
    """Record a new map click as the selected cell and open the Zone section.

    Returns True if the selection changed. ``last_active_drawing`` keeps reporting the last
    click on every later run, so only a click on a different feature counts as new. Must run
    before the section nav is drawn (a widget's key can't be set after it is instantiated).
    """
    cell = clicked_cell(state)
    if cell is None or cell == state.get(SELECTED_KEY):
        return False
    state[SELECTED_KEY] = cell
    state[VIEW_KEY] = ZONE_VIEW  # tapping the map opens the inspector (half sheet on phones)
    state[SHEET_HIDDEN_KEY] = False  # ...and brings a closed sheet back to show it
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
