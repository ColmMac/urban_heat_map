"""Greening Priority Score, eligibility gate, top-K micro-zones and web export.

Implements ``docs/ARCHITECTURE.md`` §6. Everything except :func:`export_web_layers`,
:func:`run` and :func:`main` is pure and I/O-free. All metric maths expects EPSG:27700 inputs;
the only reprojection (to EPSG:4326) is delegated to ``spatial_grid.to_web`` which is imported
lazily so ``import edinburgh_uhi.scoring`` stays light for the Streamlit app.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
from numpy.typing import ArrayLike

from edinburgh_uhi import __version__, config
from edinburgh_uhi.config import (
    CRS_BNG,
    DIST_HI_M,
    DIST_LO_M,
    ELIG_MAX_EXCL_FRAC,
    ELIG_MAX_GREEN_FRAC,
    ELIG_MAX_WATER_FRAC,
    ELIG_MIN_LAND_FRAC,
    ELIG_MIN_LST_VALID,
    GRID_CELL_M,
    HOTSPOT_REL_THRESHOLD,
    IMPERV_HI,
    IMPERV_LO,
    INTERV_GREEN_ROOF_MIN_BUILDING,
    INTERV_MIN_CORRIDOR_M,
    LST_MIN_SCENES,
    PARKS_SIMPLIFY_M,
    SENSITIVITY_DELTA,
    THERMAL_HI_PERCENTILE,
    THERMAL_LO_C,
    THERMAL_MIN_HI_C,
    TOPK_K,
    TOPK_MIN_SEPARATION_M,
    WEIGHTS,
    Weights,
)

__all__ = [
    "NormBounds",
    "classify_intervention",
    "component_scores",
    "compute_kpis",
    "derive_bounds",
    "eligibility",
    "export_web_layers",
    "hotspot_extent",
    "linear_clip",
    "main",
    "priority",
    "run",
    "score_grid",
    "select_top_zones",
    "weight_sensitivity",
]

logger = logging.getLogger("edinburgh_uhi.scoring")

MIN_ELIGIBLE_CELLS = 30
WEB_BUDGET_MB = 5.0
CELL_AREA_HA = GRID_CELL_M**2 / 10_000

GRID_SCORED_PARQUET = config.DATA_PROCESSED_DIR / "grid_scored.parquet"
TOP_ZONES_PARQUET = config.DATA_PROCESSED_DIR / "top_zones.parquet"
METADATA_JSON = config.DATA_PROCESSED_DIR / "scoring_metadata.json"
GRID_FEATURES_PARQUET = config.DATA_PROCESSED_DIR / "grid_features.parquet"
GREENSPACE_PARQUET = config.DATA_PROCESSED_DIR / "greenspace_dissolved.parquet"
MAIN_STREETS_PARQUET = config.DATA_PROCESSED_DIR / "cell_main_streets.parquet"

GRID_WEB_COLUMNS = [
    "cell_id",
    "neighbourhood",
    "lst_c",
    "lst_anomaly_c",
    "dist_park_m",
    "nearest_park_name",
    "impervious_frac",
    "building_frac",
    "tree_frac",
    "s_thermal",
    "s_distance",
    "s_impervious",
    "c_thermal",
    "c_distance",
    "c_impervious",
    "priority_score",
    "eligible",
    "exclusion_reason",
    "intervention",
    "recommendation",
    "main_street",
    "cx_m",
    "cy_m",
    "geometry",
]
ZONE_COLUMNS = [
    "zone_rank",
    "cell_id",
    "neighbourhood",
    "main_street",
    "main_street_len_m",
    "intervention",
    "recommendation",
    "priority_score",
    "c_thermal",
    "c_distance",
    "c_impervious",
    "lst_anomaly_c",
    "lst_c",
    "dist_park_m",
    "nearest_park_name",
    "impervious_frac",
    "building_frac",
    "tree_frac",
    "hotspot_cells",
    "geometry",
]


# --------------------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class NormBounds:
    """Lower/upper anchors of the three clipped-linear normalisations (ARCHITECTURE §6.2)."""

    thermal_lo: float
    thermal_hi: float
    dist_lo_m: float
    dist_hi_m: float
    imperv_lo: float
    imperv_hi: float


def linear_clip(x: ArrayLike, lo: float, hi: float) -> np.ndarray:
    """Return ``min(1, max(0, (x - lo) / (hi - lo)))``; NaN in gives NaN out."""
    if hi <= lo:
        raise ValueError(f"hi must be greater than lo, got lo={lo}, hi={hi}")
    arr = np.asarray(x, dtype="float64")
    return np.clip((arr - lo) / (hi - lo), 0.0, 1.0)


# --------------------------------------------------------------------------------------
# Eligibility
# --------------------------------------------------------------------------------------
def eligibility(grid: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Return ``(eligible, exclusion_reason)``; the first failing rule wins (§6.1).

    Missing values fail the rule they feed (``NaN < x`` is False), so they are excluded.
    """
    lst_ok = (
        (grid["lst_valid_frac"] >= ELIG_MIN_LST_VALID)
        & (grid["n_obs_median"] >= LST_MIN_SCENES)
        & grid["lst_anomaly_c"].notna()
    )
    rules: list[tuple[str, pd.Series]] = [
        ("edge_cell", grid["land_frac"] >= ELIG_MIN_LAND_FRAC),
        ("water", grid["water_frac"] < ELIG_MAX_WATER_FRAC),
        ("non_plantable_landuse", grid["excl_frac"] < ELIG_MAX_EXCL_FRAC),
        ("not_urban", grid["is_urban"].fillna(False).astype(bool)),
        ("already_green", grid["green_frac_osm"] < ELIG_MAX_GREEN_FRAC),
        ("insufficient_lst", lst_ok),
    ]
    reason = pd.Series(pd.NA, index=grid.index, dtype="string")
    for name, passed in reversed(rules):  # reversed so the first rule overwrites last
        reason = reason.mask(~passed.fillna(False).astype(bool), name)
    eligible = reason.isna()
    return eligible, reason


def derive_bounds(grid: pd.DataFrame, eligible: pd.Series) -> NormBounds:
    """Fix the normalisation anchors; thermal ceiling is P95 over eligible cells (§6.2)."""
    anomalies = grid.loc[eligible, "lst_anomaly_c"].dropna().to_numpy(dtype="float64")
    if len(anomalies) < MIN_ELIGIBLE_CELLS:
        raise ValueError(
            f"Only {len(anomalies)} eligible cells with LST; need at least {MIN_ELIGIBLE_CELLS}."
        )
    hi = float(np.percentile(anomalies, THERMAL_HI_PERCENTILE))
    if hi <= THERMAL_MIN_HI_C:
        raise ValueError(
            f"Thermal P{THERMAL_HI_PERCENTILE:g} is {hi:.3f} degC (<= {THERMAL_MIN_HI_C}); "
            "the LST composite looks broken."
        )
    return NormBounds(THERMAL_LO_C, hi, DIST_LO_M, DIST_HI_M, IMPERV_LO, IMPERV_HI)


def component_scores(grid: pd.DataFrame, b: NormBounds) -> pd.DataFrame:
    """Return ``s_thermal``, ``s_distance``, ``s_impervious`` in [0, 1] (float32)."""
    s_thermal = np.nan_to_num(linear_clip(grid["lst_anomaly_c"], b.thermal_lo, b.thermal_hi))
    out = pd.DataFrame(
        {
            "s_thermal": s_thermal,
            "s_distance": linear_clip(grid["dist_park_m"], b.dist_lo_m, b.dist_hi_m),
            "s_impervious": linear_clip(grid["impervious_frac"], b.imperv_lo, b.imperv_hi),
        },
        index=grid.index,
    )
    return out.astype("float32")


def priority(components: pd.DataFrame, w: Weights = WEIGHTS) -> pd.DataFrame:
    """Additive Greening Priority Score and its three contributions (§6.3, float64)."""
    c_t = 100.0 * w.thermal * components["s_thermal"].astype("float64")
    c_d = 100.0 * w.distance * components["s_distance"].astype("float64")
    c_i = 100.0 * w.impervious * components["s_impervious"].astype("float64")
    return pd.DataFrame(
        {
            "c_thermal": c_t,
            "c_distance": c_d,
            "c_impervious": c_i,
            "priority_score": c_t + c_d + c_i,
        },
        index=components.index,
    )


# --------------------------------------------------------------------------------------
# Intervention typing
# --------------------------------------------------------------------------------------
def classify_intervention(grid: pd.DataFrame) -> pd.DataFrame:
    """Rule-based intervention code and sentence per eligible cell (§6.4).

    Expects an ``eligible`` column; ineligible rows get ``None`` for both outputs.
    """
    street = grid["main_street"].astype("object")
    has_street = street.notna()
    area = street.where(has_street, grid["neighbourhood"].astype("object"))
    roofs = grid["building_frac"] >= INTERV_GREEN_ROOF_MIN_BUILDING
    trees = (grid["main_street_len_m"] >= INTERV_MIN_CORRIDOR_M) & has_street & ~roofs

    code = np.where(roofs, "green_roofs", np.where(trees, "street_trees", "depave_pocket_park"))
    text = np.where(
        roofs,
        "Green roofs & façade greening around " + area.astype(str),
        np.where(
            trees,
            "Street-tree corridor along " + street.astype(str),
            "Depave & pocket park in " + grid["neighbourhood"].astype(str),
        ),
    )
    elig = grid["eligible"].astype(bool).to_numpy()
    return pd.DataFrame(
        {
            "intervention": pd.Series(np.where(elig, code, None), index=grid.index, dtype="string"),
            "recommendation": pd.Series(
                np.where(elig, text, None), index=grid.index, dtype="string"
            ),
        },
        index=grid.index,
    )


# --------------------------------------------------------------------------------------
# Top-K selection
# --------------------------------------------------------------------------------------
def select_top_zones(
    scored: pd.DataFrame, k: int = TOPK_K, min_sep_m: float = TOPK_MIN_SEPARATION_M
) -> pd.DataFrame:
    """Greedy spatial non-maximum suppression over eligible cells (§6.5).

    Needs ``cell_id, cx_m, cy_m, priority_score, eligible``. Sorted by score desc then
    ``cell_id`` asc; returns the selected rows with ``zone_rank`` 1..n (never padded).
    """
    cand = scored.loc[scored["eligible"].astype(bool)]
    order = np.lexsort((cand["cell_id"].to_numpy(), -cand["priority_score"].to_numpy()))
    cand = cand.iloc[order]
    xy = cand[["cx_m", "cy_m"]].to_numpy(dtype="float64")

    chosen: list[int] = []
    for i in range(len(cand)):
        if chosen:
            d = np.hypot(xy[chosen, 0] - xy[i, 0], xy[chosen, 1] - xy[i, 1])
            if np.any(d < min_sep_m):
                continue
        chosen.append(i)
        if len(chosen) == k:
            break
    if len(chosen) < k:
        logger.warning("Only %d of %d requested zones could be selected", len(chosen), k)
    zones = cand.iloc[chosen].copy()
    zones.insert(0, "zone_rank", np.arange(1, len(zones) + 1))
    return zones


def hotspot_extent(
    scored: pd.DataFrame, zones: pd.DataFrame, rel: float = HOTSPOT_REL_THRESHOLD
) -> pd.Series:
    """``1 + `` count of eligible queen neighbours scoring >= ``rel`` x the seed (1..9)."""
    elig = scored.loc[scored["eligible"].astype(bool), ["row", "col", "priority_score"]]
    lookup = {(int(r), int(c)): s for r, c, s in elig.itertuples(index=False)}
    counts = []
    for z in zones.itertuples():
        threshold = rel * float(z.priority_score)
        n = 0
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if (dr, dc) == (0, 0):
                    continue
                s = lookup.get((int(z.row) + dr, int(z.col) + dc))
                if s is not None and s >= threshold:
                    n += 1
        counts.append(1 + n)
    return pd.Series(counts, index=zones.index, dtype="int32", name="hotspot_cells")


def weight_sensitivity(
    components: pd.DataFrame,
    scored: pd.DataFrame,
    base: Weights,
    delta: float = SENSITIVITY_DELTA,
    k: int = TOPK_K,
    min_sep_m: float = TOPK_MIN_SEPARATION_M,
) -> dict[str, Any]:
    """Jaccard overlap of the top-K under +/-``delta`` perturbations of each weight (§6.6)."""
    base_ids = set(select_top_zones(scored, k, min_sep_m)["cell_id"])
    names = ("thermal", "distance", "impervious")
    variants: list[dict[str, Any]] = []
    for name in names:
        for sign in (-1, 1):
            vals = {n: getattr(base, n) for n in names}
            vals[name] = max(0.0, vals[name] + sign * delta)
            total = sum(vals.values())
            vals = {n: v / total for n, v in vals.items()}
            alt = scored.drop(columns=["priority_score"]).join(
                priority(components, Weights(**vals))["priority_score"]
            )
            ids = set(select_top_zones(alt, k, min_sep_m)["cell_id"])
            union = base_ids | ids
            jac = len(base_ids & ids) / len(union) if union else 1.0
            variants.append(
                {"weight": name, "delta": sign * delta, "weights": vals, "jaccard": float(jac)}
            )
    jac_all = [v["jaccard"] for v in variants]
    return {
        "variants": variants,
        "jaccard_min": float(min(jac_all)),
        "jaccard_mean": float(np.mean(jac_all)),
    }


def compute_kpis(scored: pd.DataFrame, zones: pd.DataFrame) -> dict[str, float]:
    """Headline numbers for the dashboard (§6.8)."""
    urban = scored.loc[scored["is_urban"].astype(bool)]
    pct = 100.0 * float((urban["dist_park_m"] > DIST_LO_M).mean()) if len(urban) else 0.0
    anomalies = zones["lst_anomaly_c"] if len(zones) else pd.Series(dtype="float64")
    return {
        "peak_zone_anomaly_c": float(anomalies.max()) if len(zones) else 0.0,
        "mean_top_zone_anomaly_c": float(anomalies.mean()) if len(zones) else 0.0,
        "pct_urban_cells_beyond_doorstep": pct,
        "top_zone_area_ha": float(len(zones) * CELL_AREA_HA),
    }


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------
def score_grid(grid: gpd.GeoDataFrame, w: Weights = WEIGHTS) -> tuple[gpd.GeoDataFrame, NormBounds]:
    """Score every cell of ``grid`` (EPSG:27700) and return the scored grid plus bounds."""
    from edinburgh_uhi.spatial_grid import require_crs  # lazy: keeps app imports light

    require_crs(grid, CRS_BNG)
    g = grid.reset_index(drop=True).copy()
    eligible, reason = eligibility(g)
    bounds = derive_bounds(g, eligible)
    comps = component_scores(g, bounds)
    prio = priority(comps, w)
    g["eligible"] = eligible
    interv = classify_intervention(g)

    ordered = (
        g.loc[eligible, ["cell_id"]]
        .assign(score=prio.loc[eligible, "priority_score"])
        .sort_values(["score", "cell_id"], ascending=[False, True])
    )
    rank = pd.Series(np.arange(1, len(ordered) + 1), index=ordered.index)

    g = pd.concat([g, comps, prio, interv], axis=1)
    g["exclusion_reason"] = reason
    g["rank_overall"] = rank.reindex(g.index).astype("Int32")
    return gpd.GeoDataFrame(g, geometry="geometry", crs=grid.crs), bounds


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _clean(value: Any) -> Any:
    """Make numpy/NaN values JSON-safe."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def _lst_metadata() -> dict[str, Any]:
    empty: dict[str, Any] = {"n_scenes_used": None, "date_range": None, "mean_air_tmax_c": None}
    path = config.LST_MANIFEST_JSON
    if not path.exists():
        logger.warning("LST manifest %s not found; recording nulls", path)
        return empty
    try:
        scenes = [s for s in json.loads(path.read_text("utf-8"))["scenes"] if s.get("used")]
    except (ValueError, KeyError):
        logger.warning("Could not parse LST manifest %s; recording nulls", path)
        return empty
    dates = sorted(s["datetime"][:10] for s in scenes)
    air = [s["air_tmax_c"] for s in scenes if s.get("air_tmax_c") is not None]
    return {
        "n_scenes_used": len(scenes),
        "date_range": [dates[0], dates[-1]] if dates else None,
        "mean_air_tmax_c": float(np.mean(air)) if air else None,
    }


def _atomic_write_parquet(gdf: gpd.GeoDataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    gdf.to_parquet(tmp, index=False)
    os.replace(tmp, path)
    return path


def _atomic_write_text(text: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path


def _web_dir_mb() -> float:
    return sum(p.stat().st_size for p in config.WEB_DIR.glob("*.parquet")) / 1e6


def export_web_layers(
    scored: gpd.GeoDataFrame,
    zones: gpd.GeoDataFrame,
    parks: gpd.GeoDataFrame,
    main_streets: gpd.GeoDataFrame,
) -> dict[str, Path]:
    """Write the four EPSG:4326 layers in ``data/processed/web`` (§6.7). Inputs are EPSG:27700."""
    from edinburgh_uhi.spatial_grid import to_web  # lazy; the only 27700 -> 4326 conversion

    web = config.WEB_DIR
    outputs: dict[str, Path] = {}
    for precision in (config.WEB_PRECISION_DP, config.WEB_PRECISION_DP - 1):
        grid_w = to_web(scored.loc[scored["is_urban"].astype(bool), GRID_WEB_COLUMNS], precision)
        parks_src = parks[["park_id", "name", "area_ha", "geometry"]].copy()
        parks_src["geometry"] = parks_src.geometry.simplify(PARKS_SIMPLIFY_M)
        parks_w = to_web(parks_src, precision)

        elig_ids = set(scored.loc[scored["eligible"].astype(bool), "cell_id"])
        corr = main_streets.loc[main_streets["cell_id"].isin(elig_ids)]
        corr_w = to_web(corr[["cell_id", "main_street", "geometry"]], precision)

        zones_w = to_web(zones[ZONE_COLUMNS], precision)
        bx = zones_w.geometry.bounds
        zones_w["lon"] = ((bx["minx"] + bx["maxx"]) / 2).round(precision)
        zones_w["lat"] = ((bx["miny"] + bx["maxy"]) / 2).round(precision)

        for name, layer in (
            ("grid_web", grid_w),
            ("parks_web", parks_w),
            ("corridors_web", corr_w),
            ("zones_web", zones_w),
        ):
            outputs[name] = _atomic_write_parquet(layer, web / f"{name}.parquet")
        size = _web_dir_mb()
        if size <= WEB_BUDGET_MB:
            break
        logger.warning("web/ is %.2f MB (> %.0f MB); retrying at lower precision", size, 5)
    logger.info("web layers total %.2f MB", size)
    if size > WEB_BUDGET_MB:
        logger.warning("web/ is still %.2f MB, over the %.0f MB budget", size, WEB_BUDGET_MB)
    return outputs


def _log_top_table(zones: pd.DataFrame) -> None:
    cols = [
        "zone_rank",
        "neighbourhood",
        "main_street",
        "intervention",
        "priority_score",
        "lst_anomaly_c",
        "dist_park_m",
    ]
    table = zones[cols].to_string(index=False, float_format=lambda v: f"{v:.1f}")
    logger.info("Top %d greening zones:\n%s", len(zones), table)


def run(k: int = TOPK_K, min_sep_m: float = TOPK_MIN_SEPARATION_M) -> dict[str, Path]:
    """Read processed inputs, score, rank, and write every artefact of ARCHITECTURE §6.7-6.8."""
    inputs = [GRID_FEATURES_PARQUET, GREENSPACE_PARQUET, MAIN_STREETS_PARQUET]
    for path in inputs:
        if not path.exists():
            raise FileNotFoundError(
                f"Missing {path}. Run: uv run python -m edinburgh_uhi.spatial_grid"
            )
    grid = gpd.read_parquet(GRID_FEATURES_PARQUET)
    parks = gpd.read_parquet(GREENSPACE_PARQUET)
    streets = gpd.read_parquet(MAIN_STREETS_PARQUET)

    scored, bounds = score_grid(grid, WEIGHTS)
    zones = select_top_zones(scored, k, min_sep_m)
    zones = zones.assign(hotspot_cells=hotspot_extent(scored, zones))
    zones = gpd.GeoDataFrame(zones[ZONE_COLUMNS], geometry="geometry", crs=scored.crs)

    comps = scored[["s_thermal", "s_distance", "s_impervious"]]
    sensitivity = weight_sensitivity(comps, scored, WEIGHTS, SENSITIVITY_DELTA, k, min_sep_m)
    kpis = compute_kpis(scored, zones)

    reasons = scored.loc[~scored["eligible"], "exclusion_reason"].value_counts()
    metadata = {
        "pipeline_version": __version__,
        "created_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "crs_analysis": CRS_BNG,
        "grid_cell_m": GRID_CELL_M,
        "weights": {
            "thermal": WEIGHTS.thermal,
            "distance": WEIGHTS.distance,
            "impervious": WEIGHTS.impervious,
        },
        "bounds": {
            "thermal": [bounds.thermal_lo, bounds.thermal_hi],
            "distance_m": [bounds.dist_lo_m, bounds.dist_hi_m],
            "impervious": [bounds.imperv_lo, bounds.imperv_hi],
        },
        "eligibility": {
            "n_cells": len(scored),
            "n_urban": int(scored["is_urban"].sum()),
            "n_eligible": int(scored["eligible"].sum()),
            "excluded_by_reason": {str(r): int(n) for r, n in reasons.items()},
        },
        "topk": {"k": k, "min_separation_m": min_sep_m},
        "sensitivity": {
            "jaccard_min": sensitivity["jaccard_min"],
            "jaccard_mean": sensitivity["jaccard_mean"],
            "variants": sensitivity["variants"],
        },
        "lst": _lst_metadata(),
        "kpis": kpis,
        "inputs_sha256": {
            p.name: _sha256(p) for p in [*inputs, config.LST_MANIFEST_JSON] if p.exists()
        },
    }

    out = {
        "grid_scored": _atomic_write_parquet(scored, GRID_SCORED_PARQUET),
        "top_zones": _atomic_write_parquet(zones, TOP_ZONES_PARQUET),
        "scoring_metadata": _atomic_write_text(
            json.dumps(_clean(metadata), indent=2) + "\n", METADATA_JSON
        ),
    }
    out.update(export_web_layers(scored, zones, parks, streets))
    _log_top_table(zones)
    logger.info(
        "eligible %d / urban %d / total %d; thermal P95 = %.2f degC",
        metadata["eligibility"]["n_eligible"],
        metadata["eligibility"]["n_urban"],
        metadata["eligibility"]["n_cells"],
        bounds.thermal_hi,
    )
    return out


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m edinburgh_uhi.scoring [--k 15] [--min-sep 500]``."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(description="Score the grid and extract top greening zones.")
    parser.add_argument("--k", type=int, default=TOPK_K, help="number of zones (default 15)")
    parser.add_argument(
        "--min-sep",
        type=float,
        default=TOPK_MIN_SEPARATION_M,
        help="minimum centroid separation in metres (default 500)",
    )
    args = parser.parse_args(argv)
    for name, path in run(args.k, args.min_sep).items():
        logger.info("wrote %-16s %s", name, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
