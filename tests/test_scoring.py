"""Scoring maths, eligibility and top-K selection on lightweight synthetic data."""

from __future__ import annotations

import logging
from dataclasses import FrozenInstanceError

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import box

from edinburgh_uhi import config
from edinburgh_uhi.config import Weights
from edinburgh_uhi.scoring import (
    component_scores,
    derive_bounds,
    eligibility,
    priority,
    score_grid,
    select_top_zones,
)

CELL = config.GRID_CELL_M
X0, Y0 = 320_000, 670_000


# --------------------------------------------------------------------------------------
# Synthetic fixtures
# --------------------------------------------------------------------------------------
def make_frame(n_side: int = 8, seed: int = 0) -> pd.DataFrame:
    """``n_side`` x ``n_side`` eligible cells on the 250 m lattice (BNG metres)."""
    rng = np.random.default_rng(seed)
    rows, cols = np.meshgrid(np.arange(n_side), np.arange(n_side), indexing="ij")
    rows, cols = rows.ravel(), cols.ravel()
    east = X0 + cols * CELL
    north = Y0 + (n_side - 1 - rows) * CELL
    n = len(rows)
    return pd.DataFrame(
        {
            "cell_id": [f"E{e:07d}_N{m:07d}" for e, m in zip(east, north, strict=True)],
            "row": rows,
            "col": cols,
            "cx_m": east + CELL / 2.0,
            "cy_m": north + CELL / 2.0,
            "land_frac": 1.0,
            "water_frac": 0.0,
            "excl_frac": 0.0,
            "is_urban": True,
            "green_frac_osm": 0.0,
            "lst_valid_frac": 1.0,
            "n_obs_median": 5.0,
            "lst_anomaly_c": rng.uniform(0.0, 3.0, n),
            "dist_park_m": rng.uniform(0.0, 900.0, n),
            "impervious_frac": rng.uniform(0.0, 1.0, n),
            "building_frac": 0.1,
            "main_street": pd.Series([None] * n, dtype="object"),
            "main_street_len_m": 0.0,
            "neighbourhood": "Testville",
        }
    )


def as_geo(df: pd.DataFrame) -> gpd.GeoDataFrame:
    geom = [
        box(x - CELL / 2, y - CELL / 2, x + CELL / 2, y + CELL / 2)
        for x, y in zip(df["cx_m"], df["cy_m"], strict=True)
    ]
    return gpd.GeoDataFrame(df, geometry=geom, crs=config.CRS_BNG)


def pairwise_min_distance(zones: pd.DataFrame) -> float:
    xy = zones[["cx_m", "cy_m"]].to_numpy(dtype="float64")
    if len(xy) < 2:
        return float("inf")
    d = np.hypot(xy[:, None, 0] - xy[None, :, 0], xy[:, None, 1] - xy[None, :, 1])
    return float(d[np.triu_indices(len(xy), k=1)].min())


# --------------------------------------------------------------------------------------
# Weights validation
# --------------------------------------------------------------------------------------
def test_weights_default_sums_to_one() -> None:
    w = Weights()
    assert w.thermal + w.distance + w.impervious == pytest.approx(1.0, abs=1e-9)
    assert w == config.WEIGHTS


@pytest.mark.parametrize("bad", [(0.5, 0.3, 0.3), (0.4, 0.2, 0.2), (1.0, 1.0, 1.0)])
def test_weights_reject_wrong_sum(bad: tuple[float, float, float]) -> None:
    with pytest.raises(ValueError, match="sum to 1"):
        Weights(*bad)


@pytest.mark.parametrize("bad", [(-0.1, 0.6, 0.5), (0.5, -0.25, 0.75), (1.2, 0.0, -0.2)])
def test_weights_reject_negative(bad: tuple[float, float, float]) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        Weights(*bad)


def test_weights_accept_edge_cases_and_are_frozen() -> None:
    assert Weights(1.0, 0.0, 0.0).thermal == 1.0
    assert Weights(1 / 3, 1 / 3, 1 / 3).distance == pytest.approx(1 / 3)
    with pytest.raises(FrozenInstanceError):
        Weights().thermal = 0.9  # type: ignore[misc]


# --------------------------------------------------------------------------------------
# Eligibility filtering
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"land_frac": 0.3}, "edge_cell"),
        ({"water_frac": 0.4}, "water"),
        ({"excl_frac": 0.9}, "non_plantable_landuse"),
        ({"is_urban": False}, "not_urban"),
        ({"green_frac_osm": 0.8}, "already_green"),
        ({"lst_valid_frac": 0.2}, "insufficient_lst"),
        ({"n_obs_median": 2.0}, "insufficient_lst"),
        ({"lst_anomaly_c": np.nan}, "insufficient_lst"),
    ],
)
def test_eligibility_each_rule_has_its_reason(override: dict, reason: str) -> None:
    frame = make_frame(2)
    for col, val in override.items():
        frame.loc[0, col] = val
    eligible, why = eligibility(frame)
    assert not eligible.iloc[0] and why.iloc[0] == reason
    assert eligible.iloc[1:].all() and why.iloc[1:].isna().all()


def test_eligibility_first_failing_rule_wins() -> None:
    frame = make_frame(2)
    frame.loc[0, ["water_frac", "is_urban"]] = [0.9, False]  # fails water AND not_urban
    frame.loc[1, ["excl_frac", "green_frac_osm"]] = [0.9, 0.9]  # airport AND already green
    _, why = eligibility(frame)
    assert why.iloc[0] == "water"
    assert why.iloc[1] == "non_plantable_landuse"


# --------------------------------------------------------------------------------------
# Normalisation bounds (0-100) and exact additive decomposition
# --------------------------------------------------------------------------------------
def test_component_scores_bounded_for_extreme_inputs() -> None:
    frame = make_frame(2).iloc[:4].copy()
    frame["lst_anomaly_c"] = [-50.0, 0.0, 5.0, 500.0]
    frame["dist_park_m"] = [0.0, 200.0, 600.0, 1e6]
    frame["impervious_frac"] = [0.0, 0.2, 0.9, 1.0]
    bounds = derive_bounds(make_frame(), pd.Series(True, index=range(64)))
    comps = component_scores(frame, bounds)
    assert ((comps >= 0.0) & (comps <= 1.0)).all().all()
    assert comps["s_distance"].tolist() == [0.0, 0.0, 1.0, 1.0]
    assert comps["s_impervious"].tolist() == [0.0, 0.0, 1.0, 1.0]
    assert comps["s_thermal"].iloc[0] == 0.0 and comps["s_thermal"].iloc[3] == 1.0


def test_thermal_ceiling_is_p95_of_eligible_cells() -> None:
    frame = make_frame(8).iloc[:40].copy()  # 40 eligible cells
    frame["lst_anomaly_c"] = np.arange(40, dtype=float)
    bounds = derive_bounds(frame, pd.Series(True, index=frame.index))
    assert bounds.thermal_lo == 0.0
    assert bounds.thermal_hi == pytest.approx(37.05)
    assert (bounds.dist_lo_m, bounds.dist_hi_m) == (200.0, 600.0)
    assert (bounds.imperv_lo, bounds.imperv_hi) == (0.20, 0.90)


def test_derive_bounds_rejects_flat_composite_and_small_samples() -> None:
    frame = make_frame(8).iloc[:40].copy()
    frame["lst_anomaly_c"] = 0.1
    with pytest.raises(ValueError, match="broken"):
        derive_bounds(frame, pd.Series(True, index=frame.index))
    with pytest.raises(ValueError, match="at least 30"):
        derive_bounds(make_frame(8), pd.Series([True] * 20 + [False] * 44))


@pytest.mark.parametrize(
    "w", [Weights(), Weights(1.0, 0.0, 0.0), Weights(0.0, 0.0, 1.0), Weights(0.2, 0.3, 0.5)]
)
def test_priority_range_and_exact_additive_decomposition(w: Weights) -> None:
    rng = np.random.default_rng(42)
    s = rng.uniform(0.0, 1.0, size=(500, 3))
    s[:2] = [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]]  # include both extremes
    comps = pd.DataFrame(s, columns=["s_thermal", "s_distance", "s_impervious"])
    comps = comps.astype("float32")  # as stored by component_scores
    out = priority(comps, w)

    for col in ("c_thermal", "c_distance", "c_impervious", "priority_score"):
        assert out[col].dtype == np.float64
    assert out["priority_score"].between(0.0, 100.0).all()
    assert out["priority_score"].iloc[0] == 0.0
    assert out["priority_score"].iloc[1] == pytest.approx(100.0, abs=1e-9)
    total = out["c_thermal"] + out["c_distance"] + out["c_impervious"]
    assert (total - out["priority_score"]).abs().max() < 1e-6
    # Each contribution is capped by 100 x its weight.
    assert (out["c_thermal"] <= 100.0 * w.thermal + 1e-9).all()
    assert (out["c_distance"] <= 100.0 * w.distance + 1e-9).all()
    assert (out["c_impervious"] <= 100.0 * w.impervious + 1e-9).all()


@pytest.mark.parametrize("component", ["s_thermal", "s_distance", "s_impervious"])
def test_priority_monotonic_in_each_component(component: str) -> None:
    base = pd.DataFrame({"s_thermal": [0.3], "s_distance": [0.3], "s_impervious": [0.3]})
    scores = []
    for v in np.linspace(0.0, 1.0, 11):
        comps = base.copy()
        comps[component] = v
        scores.append(float(priority(comps)["priority_score"].iloc[0]))
    assert all(b >= a for a, b in zip(scores, scores[1:], strict=False))
    assert scores[-1] > scores[0]


# --------------------------------------------------------------------------------------
# Top-K non-maximum suppression (>= 500 m)
# --------------------------------------------------------------------------------------
def _scored(frame: pd.DataFrame, scores: np.ndarray | float) -> pd.DataFrame:
    out = frame.copy()
    out["priority_score"] = scores
    out["eligible"] = True
    return out


def test_select_top_zones_enforces_500m_separation() -> None:
    frame = make_frame(8)
    # A 3x3 block of top scores plus a gradient elsewhere: naive top-K would pick the clump.
    scores = np.linspace(10.0, 50.0, len(frame))
    clump = (frame["row"].between(2, 4)) & (frame["col"].between(2, 4))
    scores[clump.to_numpy()] = 90.0 + frame.index[clump.to_numpy()].to_numpy() * 0.01  # unique
    scored = _scored(frame, scores)

    zones = select_top_zones(scored, k=15, min_sep_m=500.0)
    assert pairwise_min_distance(zones) >= 500.0
    assert zones["zone_rank"].tolist() == list(range(1, len(zones) + 1))
    assert zones["priority_score"].is_monotonic_decreasing
    assert zones.iloc[0]["cell_id"] == scored.loc[scored["priority_score"].idxmax(), "cell_id"]
    assert clump.sum() == 9 and zones["cell_id"].isin(frame.loc[clump, "cell_id"]).sum() < 9


def test_separation_threshold_rejects_diagonals_but_allows_exactly_500m() -> None:
    frame = make_frame(3)
    scores = np.zeros(len(frame))
    centre = (frame["row"] == 1) & (frame["col"] == 1)
    scores[centre.to_numpy()] = 100.0
    scored = _scored(frame, scores)
    zones = select_top_zones(scored, k=15, min_sep_m=500.0)
    # Only the 4 corners are >= 500 m from the centre? No: they are 354 m away -> rejected.
    assert zones["cell_id"].tolist() == [scored.loc[centre.to_numpy(), "cell_id"].iloc[0]]

    two = _scored(make_frame(8).iloc[[0, 2]], 50.0)  # same row, two cells apart = 500 m
    assert len(select_top_zones(two, k=15, min_sep_m=500.0)) == 2


def test_select_top_zones_deterministic_tiebreak() -> None:
    scored = _scored(make_frame(8), 42.0)
    first = select_top_zones(scored, k=5)
    second = select_top_zones(scored.sample(frac=1.0, random_state=1), k=5)
    assert first["cell_id"].tolist() == second["cell_id"].tolist()
    assert first.iloc[0]["cell_id"] == scored["cell_id"].min()


def test_select_top_zones_returns_fewer_when_exhausted(caplog: pytest.LogCaptureFixture) -> None:
    scored = _scored(make_frame(8).iloc[[0, 2, 4, 6]], np.array([4.0, 3.0, 2.0, 1.0]))
    with caplog.at_level(logging.WARNING, logger="edinburgh_uhi.scoring"):
        zones = select_top_zones(scored, k=15)
    assert len(zones) == 4
    assert "Only 4 of 15" in caplog.text


# --------------------------------------------------------------------------------------
# Airport / restricted cells never reach the top zones
# --------------------------------------------------------------------------------------
def test_ineligible_cells_never_selected() -> None:
    scored = _scored(make_frame(8), np.linspace(0.0, 50.0, 64))
    scored.loc[scored["priority_score"].idxmax(), "eligible"] = False
    top_ineligible = scored.loc[~scored["eligible"], "cell_id"].iloc[0]
    zones = select_top_zones(scored, k=15)
    assert top_ineligible not in set(zones["cell_id"])
    assert zones["eligible"].all()


def test_airport_and_restricted_cells_excluded_end_to_end() -> None:
    frame = make_frame(8, seed=3)
    airport = (frame["row"] <= 1) & (frame["col"] <= 1)  # 2x2 aerodrome block
    water = (frame["row"] == 7) & (frame["col"] == 7)
    edge = (frame["row"] == 7) & (frame["col"] == 0)
    # Make the restricted cells irresistible on every metric so only eligibility can stop them.
    for mask in (airport, water, edge):
        frame.loc[mask, ["lst_anomaly_c", "dist_park_m", "impervious_frac"]] = [20.0, 5000.0, 1.0]
    frame.loc[airport, "excl_frac"] = 0.95
    frame.loc[water, "water_frac"] = 0.6
    frame.loc[edge, "land_frac"] = 0.2

    scored, bounds = score_grid(as_geo(frame))
    restricted = airport | water | edge
    assert bounds.thermal_hi > config.THERMAL_MIN_HI_C
    assert not scored.loc[restricted.to_numpy(), "eligible"].any()
    assert (scored.loc[airport.to_numpy(), "exclusion_reason"] == "non_plantable_landuse").all()
    assert scored.loc[water.to_numpy(), "exclusion_reason"].eq("water").all()
    assert scored.loc[edge.to_numpy(), "exclusion_reason"].eq("edge_cell").all()
    assert scored.loc[restricted.to_numpy(), "rank_overall"].isna().all()
    # Without the gate these cells would win: their score is the grid maximum.
    assert scored["priority_score"].idxmax() in set(scored.index[restricted.to_numpy()])

    zones = select_top_zones(scored, k=6, min_sep_m=500.0)
    assert len(zones) == 6
    assert not zones["cell_id"].isin(scored.loc[restricted.to_numpy(), "cell_id"]).any()
    assert (zones["excl_frac"] < config.ELIG_MAX_EXCL_FRAC).all()
    assert pairwise_min_distance(zones) >= 500.0
    assert zones["intervention"].notna().all() and zones["recommendation"].notna().all()


def test_score_grid_outputs_bounded_and_additive() -> None:
    scored, _ = score_grid(as_geo(make_frame(8, seed=7)))
    assert scored["priority_score"].between(0.0, 100.0).all()
    total = scored["c_thermal"] + scored["c_distance"] + scored["c_impervious"]
    assert (total.astype("float64") - scored["priority_score"].astype("float64")).abs().max() < 1e-6
    assert scored.crs is not None and scored.crs.to_epsg() == 27700
    assert scored["rank_overall"].dropna().min() == 1
