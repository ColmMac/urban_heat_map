"""Pure re-scoring logic behind the weight sliders (no Streamlit, no geometry operations)."""

from __future__ import annotations

import geopandas as gpd

from edinburgh_uhi import config
from edinburgh_uhi.config import Weights
from edinburgh_uhi.scoring import priority, select_top_zones

COMPONENT_COLUMNS = ["c_thermal", "c_distance", "c_impervious"]
SCORE_COLUMNS = ["s_thermal", "s_distance", "s_impervious"]
DEFAULT_WEIGHTS = (config.WEIGHTS.thermal, config.WEIGHTS.distance, config.WEIGHTS.impervious)


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
