"""250 m British National Grid analysis grid and per-cell feature engineering.

Implements ``docs/ARCHITECTURE.md`` §5 / TASKS Ticket 2. Every metric operation (grid generation,
areas, lengths, distances) runs in ``EPSG:27700``; raw OSM layers are reprojected exactly once in
:func:`load_raw_vectors` and the only outbound conversion is :func:`to_web`.

Run with ``uv run python -m edinburgh_uhi.spatial_grid``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import time
import warnings
from collections.abc import Iterable, Iterator
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyproj
import rioxarray as rxr
import shapely
import xarray as xr
from shapely.geometry import MultiLineString

from edinburgh_uhi import config
from edinburgh_uhi.config import (
    CRS_BNG,
    CRS_WGS84,
    GREEN_MERGE_TOL_M,
    GREEN_MIN_AREA_M2,
    GRID_CELL_M,
    GRID_MIN_INSIDE_FRAC,
    NEIGHBOURHOOD_MAX_DIST_M,
    WEB_PRECISION_DP,
    snap_bounds,
)

logger = logging.getLogger("edinburgh_uhi.spatial_grid")

GRID_FEATURES_PARQUET: Path = config.DATA_PROCESSED_DIR / "grid_features.parquet"
GREENSPACE_DISSOLVED_PARQUET: Path = config.DATA_PROCESSED_DIR / "greenspace_dissolved.parquet"
CELL_MAIN_STREETS_PARQUET: Path = config.DATA_PROCESSED_DIR / "cell_main_streets.parquet"

WEB_LON_RANGE: tuple[float, float] = (-3.6, -2.9)
WEB_LAT_RANGE: tuple[float, float] = (55.75, 56.05)
UNNAMED_GREENSPACE = "Unnamed greenspace"
DEFAULT_NEIGHBOURHOOD = "Edinburgh"
ANOMALY_CENTER_MIN_VALID = 0.6

# Column -> expected dtype kind (ARCHITECTURE §5.4). "string" is checked with is_string_dtype.
GRID_SCHEMA: dict[str, str] = {
    "cell_id": "string",
    "row": "int32",
    "col": "int32",
    "easting_ll": "int32",
    "northing_ll": "int32",
    "cx_m": "float64",
    "cy_m": "float64",
    "land_frac": "float32",
    "lst_anomaly_raw_c": "float32",
    "lst_anomaly_c": "float32",
    "lst_c": "float32",
    "lst_valid_frac": "float32",
    "n_obs_median": "float32",
    "builtup_frac": "float32",
    "tree_frac": "float32",
    "water_frac": "float32",
    "green_frac_wc": "float32",
    "building_frac": "float32",
    "impervious_frac": "float32",
    "green_frac_osm": "float32",
    "excl_frac": "float32",
    "dist_park_m": "float32",
    "nearest_park_name": "string",
    "street_len_m": "float32",
    "street_density_m_per_ha": "float32",
    "main_street_len_m": "float32",
    "main_street": "string",
    "neighbourhood": "string",
    "is_urban": "bool",
    "geometry": "geometry",
}
FRAC_COLUMNS: tuple[str, ...] = (
    "land_frac",
    "lst_valid_frac",
    "builtup_frac",
    "tree_frac",
    "water_frac",
    "green_frac_wc",
    "building_frac",
    "impervious_frac",
    "green_frac_osm",
    "excl_frac",
)


# --------------------------------------------------------------------------------------
# Guards and small helpers
# --------------------------------------------------------------------------------------
def require_crs(gdf: gpd.GeoDataFrame | gpd.GeoSeries, expected: str) -> None:
    """Raise ``ValueError`` if ``gdf`` has no CRS or its CRS is not ``expected``."""
    if gdf.crs is None:
        raise ValueError(f"GeoDataFrame has no CRS; expected {expected}")
    if not pyproj.CRS(expected).equals(gdf.crs):
        raise ValueError(f"Expected CRS {expected}, got {gdf.crs.to_string()}")


@contextlib.contextmanager
def _timed(stage: str) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        logger.info("stage %-28s %6.1fs", stage, time.perf_counter() - start)


def _polygons_only(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Make geometries valid, explode and keep only non-empty polygonal parts (any CRS)."""
    out = gdf.copy()
    out["geometry"] = shapely.make_valid(np.asarray(out.geometry))
    out = out.explode(index_parts=False)
    out = out[out.geometry.geom_type.isin(["Polygon", "MultiPolygon"]) & ~out.geometry.is_empty]
    return out.reset_index(drop=True)


def _cell_area(grid: gpd.GeoDataFrame) -> float:
    return float(grid.geometry.iloc[0].area)


def _centroids(grid: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"cell_id": grid["cell_id"].to_numpy()},
        geometry=grid.geometry.centroid.to_numpy(),
        crs=grid.crs,
        index=grid.index,
    )


# --------------------------------------------------------------------------------------
# Grid generation
# --------------------------------------------------------------------------------------
def make_grid(
    boundary_bng: gpd.GeoDataFrame,
    cell_m: int = GRID_CELL_M,
    min_inside_frac: float = GRID_MIN_INSIDE_FRAC,
) -> gpd.GeoDataFrame:
    """Build the square grid over ``boundary_bng`` (EPSG:27700); row 0 is the northern edge."""
    require_crs(boundary_bng, CRS_BNG)
    poly = shapely.union_all(np.asarray(boundary_bng.geometry))
    xmin, ymin, xmax, ymax = (int(v) for v in snap_bounds(tuple(boundary_bng.total_bounds), cell_m))
    n_cols = (xmax - xmin) // cell_m
    n_rows = (ymax - ymin) // cell_m

    rows, cols = np.meshgrid(np.arange(n_rows), np.arange(n_cols), indexing="ij")
    rows, cols = rows.ravel(), cols.ravel()
    east = xmin + cols * cell_m
    north = ymax - (rows + 1) * cell_m
    boxes = shapely.box(east, north, east + cell_m, north + cell_m)

    shapely.prepare(poly)
    hit = shapely.intersects(boxes, poly)
    land = np.zeros(len(boxes))
    land[hit] = shapely.area(shapely.intersection(boxes[hit], poly)) / float(cell_m**2)
    keep = land >= min_inside_frac

    east, north, rows, cols = east[keep], north[keep], rows[keep], cols[keep]
    grid = gpd.GeoDataFrame(
        {
            "cell_id": pd.array(
                [f"E{e:07d}_N{n:07d}" for e, n in zip(east, north, strict=True)], dtype="string"
            ),
            "row": rows.astype("int32"),
            "col": cols.astype("int32"),
            "easting_ll": east.astype("int32"),
            "northing_ll": north.astype("int32"),
            "cx_m": east + cell_m / 2.0,
            "cy_m": north + cell_m / 2.0,
            "land_frac": land[keep].astype("float32"),
        },
        geometry=boxes[keep],
        crs=CRS_BNG,
    )
    return grid.sort_values("cell_id").reset_index(drop=True)


# --------------------------------------------------------------------------------------
# Raster -> grid block reduction
# --------------------------------------------------------------------------------------
def _as_2d(da: xr.DataArray) -> xr.DataArray:
    if "band" in da.dims:
        if da.sizes["band"] != 1:
            raise ValueError("Select a single band before aggregating (use da.isel(band=i))")
        da = da.isel(band=0, drop=True)
    return da


def _block_reduce(
    da: xr.DataArray, grid: gpd.GeoDataFrame, cell_m: int, how: str, classes: Iterable[int] = ()
) -> pd.Series:
    """Reduce ``da`` (EPSG:27700, pixel grid aligned to the 250 m lattice) per grid cell."""
    require_crs(grid, CRS_BNG)
    da = _as_2d(da)
    if da.rio.crs is None or not pyproj.CRS(CRS_BNG).equals(da.rio.crs):
        raise ValueError(f"Raster CRS must be {CRS_BNG}, got {da.rio.crs}")
    if float(da["y"][0]) < float(da["y"][-1]):  # make row 0 the north edge
        da = da.isel(y=slice(None, None, -1))

    res_x, res_y = (abs(float(r)) for r in da.rio.resolution())
    if abs(res_x - res_y) > 1e-9 or cell_m % res_x > 1e-9:
        raise ValueError(f"Raster resolution ({res_x}, {res_y}) does not divide {cell_m} m cells")
    k = round(cell_m / res_x)

    left, _, _, top = da.rio.bounds()
    x0 = float((grid["easting_ll"] - grid["col"] * cell_m).iloc[0])
    y0 = float((grid["northing_ll"] + (grid["row"] + 1) * cell_m).iloc[0])
    if abs(left - x0) > 1e-6 or abs(top - y0) > 1e-6:
        raise ValueError(
            f"Raster origin (left={left}, top={top}) does not match grid origin "
            f"(xmin={x0}, ymax={y0}); raster bounds={da.rio.bounds()}"
        )
    n_rows, n_cols = da.sizes["y"] // k, da.sizes["x"] // k
    if da.sizes["y"] % k or da.sizes["x"] % k:
        raise ValueError(f"Raster shape {da.shape} is not a multiple of {k} px per cell")
    if int(grid["row"].max()) >= n_rows or int(grid["col"].max()) >= n_cols:
        raise ValueError(f"Raster covers {n_rows}x{n_cols} cells; grid needs more")

    coarse = {"y": k, "x": k}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        if how == "mean":
            block = da.coarsen(coarse, boundary="exact").mean()
        elif how == "median":
            block = da.coarsen(coarse, boundary="exact").reduce(np.nanmedian)
        elif how == "valid":
            block = da.notnull().coarsen(coarse, boundary="exact").mean()
        elif how == "classes":
            block = da.isin(list(classes)).coarsen(coarse, boundary="exact").mean()
        else:
            raise ValueError(f"Unknown reduction {how!r}")
    values = np.asarray(block.values, dtype="float64")[
        grid["row"].to_numpy(), grid["col"].to_numpy()
    ]
    return pd.Series(values, index=pd.Index(grid["cell_id"].to_numpy(), name="cell_id"))


def aggregate_raster_mean(
    da: xr.DataArray, grid: gpd.GeoDataFrame, cell_m: int = GRID_CELL_M
) -> pd.Series:
    """NaN-skipping block mean of a single-band raster (EPSG:27700); index = ``cell_id``."""
    return _block_reduce(da, grid, cell_m, "mean")


def aggregate_raster_median(
    da: xr.DataArray, grid: gpd.GeoDataFrame, cell_m: int = GRID_CELL_M
) -> pd.Series:
    """NaN-skipping block median of a single-band raster (EPSG:27700); index = ``cell_id``."""
    return _block_reduce(da, grid, cell_m, "median")


def aggregate_raster_valid_frac(
    da: xr.DataArray, grid: gpd.GeoDataFrame, cell_m: int = GRID_CELL_M
) -> pd.Series:
    """Fraction of non-NaN pixels per cell (EPSG:27700 raster); index = ``cell_id``."""
    return _block_reduce(da, grid, cell_m, "valid")


def aggregate_raster_class_fraction(
    da: xr.DataArray,
    grid: gpd.GeoDataFrame,
    classes: Iterable[int],
    cell_m: int = GRID_CELL_M,
) -> pd.Series:
    """Fraction of pixels whose value is in ``classes`` per cell; index = ``cell_id``."""
    return _block_reduce(da, grid, cell_m, "classes", classes)


# --------------------------------------------------------------------------------------
# Vector features
# --------------------------------------------------------------------------------------
def area_fraction(
    grid: gpd.GeoDataFrame, polygons_bng: gpd.GeoDataFrame | gpd.GeoSeries
) -> pd.Series:
    """Share of each cell covered by the *union* of ``polygons_bng`` (both EPSG:27700).

    Overlapping/duplicate polygons are not double-counted. Index = ``cell_id``, range [0, 1].
    """
    require_crs(grid, CRS_BNG)
    require_crs(polygons_bng, CRS_BNG)
    out = pd.Series(0.0, index=pd.Index(grid["cell_id"].to_numpy(), name="cell_id"))
    polys = gpd.GeoDataFrame(geometry=np.asarray(polygons_bng.geometry), crs=CRS_BNG)
    polys = _polygons_only(polys)
    if polys.empty:
        return out

    cells = grid[["cell_id", "geometry"]]
    joined = gpd.sjoin(cells, polys[["geometry"]], how="inner", predicate="intersects")
    if joined.empty:
        return out
    inter = shapely.intersection(
        np.asarray(joined.geometry), np.asarray(polys.geometry)[joined["index_right"].to_numpy()]
    )
    df = pd.DataFrame({"cell_id": joined["cell_id"].to_numpy(), "geom": inter})
    df = df[shapely.area(df["geom"].to_numpy()) > 0]
    union_area = df.groupby("cell_id")["geom"].apply(lambda s: shapely.union_all(s.to_numpy()).area)
    frac = (union_area / _cell_area(grid)).clip(0, 1)
    out.loc[frac.index] = frac.to_numpy()
    return out


def dissolve_greenspace(
    greenspace_bng: gpd.GeoDataFrame,
    min_area_m2: float = GREEN_MIN_AREA_M2,
    merge_tol_m: float = GREEN_MERGE_TOL_M,
) -> gpd.GeoDataFrame:
    """Merge touching greenspace polygons (EPSG:27700) and keep merged parts >= ``min_area_m2``.

    Returns ``park_id, name, area_ha, geometry`` ordered by descending area.
    """
    require_crs(greenspace_bng, CRS_BNG)
    src = _polygons_only(greenspace_bng)
    cols = ["name"] if "name" in src.columns else []
    src = src[[*cols, "geometry"]]
    if "name" not in src.columns:
        src["name"] = pd.Series(pd.NA, index=src.index, dtype="string")

    buffered = shapely.buffer(np.asarray(src.geometry), merge_tol_m)
    merged = shapely.union_all(buffered)
    parts = shapely.get_parts(merged)
    shrunk = shapely.get_parts(shapely.buffer(parts, -merge_tol_m))
    shrunk = shrunk[~shapely.is_empty(shrunk)]
    shrunk = shrunk[shapely.area(shrunk) >= min_area_m2]
    areas = shapely.area(shrunk)
    order = np.argsort(-areas, kind="stable")
    shrunk, areas = shrunk[order], areas[order]

    parks = gpd.GeoDataFrame(
        {
            "park_id": [f"P{i + 1:04d}" for i in range(len(shrunk))],
            "area_ha": (areas / 10_000.0),
        },
        geometry=shrunk,
        crs=CRS_BNG,
    )

    # Name: the named source polygon with the largest overlap with each merged park.
    named = src[src["name"].notna() & (src["name"].astype("string").str.strip() != "")]
    names = pd.Series(UNNAMED_GREENSPACE, index=parks["park_id"], dtype="string")
    if not named.empty and not parks.empty:
        j = gpd.sjoin(
            parks[["park_id", "geometry"]], named[["name", "geometry"]], predicate="intersects"
        )
        overlap = shapely.area(
            shapely.intersection(
                np.asarray(j.geometry),
                np.asarray(named.geometry)[_positions(named, j["index_right"])],
            )
        )
        j = j.assign(overlap=overlap).sort_values(
            ["park_id", "overlap", "name"], ascending=[True, False, True]
        )
        best = j.drop_duplicates("park_id").set_index("park_id")["name"]
        names.loc[best.index] = best.astype("string").to_numpy()
    parks["name"] = names.to_numpy()
    return parks[["park_id", "name", "area_ha", "geometry"]]


def _positions(gdf: gpd.GeoDataFrame, labels: pd.Series) -> np.ndarray:
    """Positional indices of index ``labels`` within ``gdf`` (index labels are unique)."""
    return gdf.index.get_indexer(labels)


def distance_to_greenspace(grid: gpd.GeoDataFrame, parks_bng: gpd.GeoDataFrame) -> pd.DataFrame:
    """Distance (m) from each cell centroid to the nearest dissolved park, plus its name.

    Both inputs EPSG:27700. Index = ``cell_id``; columns ``dist_park_m``, ``nearest_park_name``.
    """
    require_crs(grid, CRS_BNG)
    require_crs(parks_bng, CRS_BNG)
    if parks_bng.empty:
        raise ValueError("No dissolved greenspace parks available for distance calculation")
    cents = _centroids(grid)
    j = gpd.sjoin_nearest(
        cents,
        parks_bng[["park_id", "name", "geometry"]],
        how="left",
        distance_col="dist_park_m",
    )
    j = j.sort_values(["cell_id", "park_id"])
    j = j.drop_duplicates("cell_id", keep="first").set_index("cell_id")
    j = j.reindex(grid["cell_id"].to_numpy())
    return pd.DataFrame(
        {
            "dist_park_m": j["dist_park_m"].astype("float64").to_numpy(),
            "nearest_park_name": j["name"].astype("string").fillna(UNNAMED_GREENSPACE).to_numpy(),
        },
        index=pd.Index(grid["cell_id"].to_numpy(), name="cell_id"),
    )


def _to_multiline(geoms: Iterable[shapely.Geometry]) -> MultiLineString:
    lines: list[shapely.Geometry] = []
    for g in geoms:
        lines.extend(p for p in shapely.get_parts(g) if p.geom_type == "LineString")
    merged = shapely.union_all(lines)
    parts = [p for p in shapely.get_parts(merged) if p.geom_type == "LineString"]
    return MultiLineString(parts)


def street_features(
    grid: gpd.GeoDataFrame, streets_bng: gpd.GeoDataFrame
) -> tuple[pd.DataFrame, gpd.GeoDataFrame]:
    """Per-cell street length/density and the longest named street (both inputs EPSG:27700).

    Returns ``(features indexed by cell_id, cell_main_streets GeoDataFrame)``.
    """
    require_crs(grid, CRS_BNG)
    require_crs(streets_bng, CRS_BNG)
    st = streets_bng[["name", "geometry"]].reset_index(drop=True)
    st = st[st.geometry.notna() & ~st.geometry.is_empty]
    st["name"] = st["name"].astype("string")

    ids = pd.Index(grid["cell_id"].to_numpy(), name="cell_id")
    feats = pd.DataFrame(
        {
            "street_len_m": 0.0,
            "street_density_m_per_ha": 0.0,
            "main_street": pd.Series(pd.NA, index=ids, dtype="string"),
            "main_street_len_m": 0.0,
        },
        index=ids,
    )
    empty = gpd.GeoDataFrame(
        {"cell_id": pd.array([], dtype="string"), "main_street": pd.array([], dtype="string")},
        geometry=[],
        crs=CRS_BNG,
    )
    if st.empty:
        return feats, empty

    joined = gpd.sjoin(grid[["cell_id", "geometry"]], st, how="inner", predicate="intersects")
    inter = shapely.intersection(
        np.asarray(joined.geometry), np.asarray(st.geometry)[joined["index_right"].to_numpy()]
    )
    df = pd.DataFrame(
        {
            "cell_id": joined["cell_id"].to_numpy(),
            "name": joined["name"].to_numpy(),
            "length": shapely.length(inter),
            "geom": inter,
        }
    )
    df = df[df["length"] > 0]
    total = df.groupby("cell_id")["length"].sum()
    feats.loc[total.index, "street_len_m"] = total.to_numpy()
    feats["street_density_m_per_ha"] = feats["street_len_m"] / (_cell_area(grid) / 10_000.0)

    named = df[df["name"].notna() & (df["name"].astype("string").str.strip() != "")]
    if named.empty:
        return feats, empty
    by = (
        named.groupby(["cell_id", "name"], as_index=False)["length"]
        .sum()
        .sort_values(["cell_id", "length", "name"], ascending=[True, False, True])
        .drop_duplicates("cell_id")
    )
    feats.loc[by["cell_id"].to_numpy(), "main_street"] = by["name"].astype("string").to_numpy()
    feats.loc[by["cell_id"].to_numpy(), "main_street_len_m"] = by["length"].to_numpy()

    chosen = named.merge(by[["cell_id", "name"]], on=["cell_id", "name"])
    lines = chosen.groupby("cell_id")["geom"].apply(lambda s: _to_multiline(s.to_numpy()))
    main_streets = gpd.GeoDataFrame(
        {
            "cell_id": pd.array(lines.index.to_numpy(), dtype="string"),
            "main_street": pd.array(
                by.set_index("cell_id").loc[lines.index, "name"].to_numpy(), dtype="string"
            ),
        },
        geometry=lines.to_numpy(),
        crs=CRS_BNG,
    )
    return feats, main_streets.sort_values("cell_id").reset_index(drop=True)


def assign_neighbourhood(
    grid: gpd.GeoDataFrame,
    places_bng: gpd.GeoDataFrame,
    max_dist_m: float = NEIGHBOURHOOD_MAX_DIST_M,
) -> pd.Series:
    """Name of the nearest place point within ``max_dist_m`` of each cell centroid (EPSG:27700)."""
    require_crs(grid, CRS_BNG)
    require_crs(places_bng, CRS_BNG)
    ids = pd.Index(grid["cell_id"].to_numpy(), name="cell_id")
    out = pd.Series(DEFAULT_NEIGHBOURHOOD, index=ids, dtype="string")
    pl = places_bng[places_bng["name"].notna() & (places_bng["name"].astype("string") != "")]
    if pl.empty:
        return out
    pl = pl[["name", "geometry"]]
    j = gpd.sjoin_nearest(
        _centroids(grid), pl, how="inner", max_distance=max_dist_m, distance_col="_d"
    )
    j = j.sort_values(["cell_id", "_d", "name"]).drop_duplicates("cell_id", keep="first")
    out.loc[j["cell_id"].to_numpy()] = j["name"].astype("string").to_numpy()
    return out


def recenter_anomaly(grid: pd.DataFrame) -> pd.Series:
    """``lst_anomaly_raw_c`` minus its median over urban cells with enough valid LST pixels."""
    mask = grid["is_urban"] & (grid["lst_valid_frac"] >= ANOMALY_CENTER_MIN_VALID)
    raw = grid["lst_anomaly_raw_c"].astype("float64")
    center = raw[mask].median()
    return (raw - center).astype("float32")


# --------------------------------------------------------------------------------------
# Web export helper
# --------------------------------------------------------------------------------------
def to_web(gdf: gpd.GeoDataFrame, precision: int = WEB_PRECISION_DP) -> gpd.GeoDataFrame:
    """Reproject EPSG:27700 -> EPSG:4326 and round coordinates to ``precision`` decimals."""
    require_crs(gdf, CRS_BNG)
    out = gdf.to_crs(CRS_WGS84)
    rounded = shapely.set_precision(np.asarray(out.geometry), 10.0**-precision)
    out = out.set_geometry(gpd.GeoSeries(rounded, index=out.index, crs=CRS_WGS84))
    if len(out):
        minx, miny, maxx, maxy = out.total_bounds
        if (
            minx < WEB_LON_RANGE[0]
            or maxx > WEB_LON_RANGE[1]
            or miny < WEB_LAT_RANGE[0]
            or maxy > WEB_LAT_RANGE[1]
        ):
            logger.warning(
                "Web bounds (%.4f, %.4f, %.4f, %.4f) fall outside the Edinburgh sanity box",
                minx,
                miny,
                maxx,
                maxy,
            )
    return out


# --------------------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------------------
def load_raw_vectors() -> dict[str, gpd.GeoDataFrame]:
    """Read ``data/raw/*.parquet`` (EPSG:4326) and reproject each layer to EPSG:27700."""
    paths = {
        "boundary": config.BOUNDARY_PARQUET,
        "buildings": config.OSM_BUILDINGS_PARQUET,
        "greenspace": config.OSM_GREENSPACE_PARQUET,
        "streets": config.OSM_STREETS_PARQUET,
        "places": config.OSM_PLACES_PARQUET,
        "exclusions": config.OSM_EXCLUSIONS_PARQUET,
    }
    out: dict[str, gpd.GeoDataFrame] = {}
    for key, path in paths.items():
        if not path.exists():
            raise FileNotFoundError(
                f"Missing {path}. Run: uv run python -m edinburgh_uhi.ingest --step all"
            )
        out[key] = gpd.read_parquet(path).to_crs(CRS_BNG)
        logger.info("loaded %-10s %7d features", key, len(out[key]))
    return out


def _atomic_write_parquet(gdf: gpd.GeoDataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    gdf.to_parquet(tmp, index=False)
    os.replace(tmp, path)
    return path


def _open_raster(path: Path, masked: bool) -> xr.DataArray:
    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path}. Run: uv run python -m edinburgh_uhi.ingest --step all"
        )
    return rxr.open_rasterio(path, masked=masked)


def validate_grid_features(
    gdf: gpd.GeoDataFrame, n_scenes_used: int | None = None, cell_m: int = GRID_CELL_M
) -> list[str]:
    """Return human-readable schema/dtype/range problems for ``grid_features`` (empty = OK)."""
    problems: list[str] = []
    expected = list(GRID_SCHEMA)
    if list(gdf.columns) != expected:
        problems.append(
            f"columns differ: missing={set(expected) - set(gdf.columns)}, "
            f"extra={set(gdf.columns) - set(expected)}"
        )
        return problems
    if gdf.crs is None or not pyproj.CRS(CRS_BNG).equals(gdf.crs):
        problems.append(f"CRS is {gdf.crs}, expected {CRS_BNG}")
    for col, dtype in GRID_SCHEMA.items():
        if dtype == "geometry":
            continue
        if dtype == "string":
            if not pd.api.types.is_string_dtype(gdf[col]):
                problems.append(f"{col}: dtype {gdf[col].dtype}, expected string")
        elif str(gdf[col].dtype) != dtype:
            problems.append(f"{col}: dtype {gdf[col].dtype}, expected {dtype}")
    if not gdf["cell_id"].is_unique:
        problems.append("cell_id is not unique")
    for col in FRAC_COLUMNS:
        s = gdf[col].dropna()
        if len(s) and (s.min() < -1e-6 or s.max() > 1 + 1e-6):
            problems.append(f"{col} outside [0, 1]: [{s.min()}, {s.max()}]")
    for col in ("dist_park_m", "street_len_m", "street_density_m_per_ha", "main_street_len_m"):
        if gdf[col].isna().any() or (gdf[col] < 0).any():
            problems.append(f"{col} has NaN or negative values")
    if gdf["neighbourhood"].isna().any() or gdf["nearest_park_name"].isna().any():
        problems.append("neighbourhood / nearest_park_name must not be null")
    if n_scenes_used is not None and gdf["n_obs_median"].max() > n_scenes_used:
        problems.append(
            f"n_obs_median max {gdf['n_obs_median'].max()} > scenes used {n_scenes_used}"
        )
    area = float(cell_m**2)
    if not np.allclose(shapely.area(np.asarray(gdf.geometry)), area, atol=1e-6):
        problems.append(f"geometries are not all {cell_m} m squares (area {area})")
    if (gdf["easting_ll"] % cell_m).any() or (gdf["northing_ll"] % cell_m).any():
        problems.append(f"lower-left corners are not multiples of {cell_m} m")
    return problems


def _scenes_used() -> int | None:
    if not config.LST_MANIFEST_JSON.exists():
        return None
    manifest = json.loads(config.LST_MANIFEST_JSON.read_text(encoding="utf-8"))
    return sum(1 for s in manifest.get("scenes", []) if s.get("used"))


def build_grid_features() -> dict[str, Path]:
    """Build and cache grid features, dissolved greenspace and per-cell main streets.

    Reads ``data/raw`` (vectors reprojected to EPSG:27700, rasters already EPSG:27700) and writes
    three GeoParquet files (EPSG:27700) to ``data/processed``.
    """
    with _timed("load vectors"):
        vec = load_raw_vectors()
    boundary = vec["boundary"]

    with _timed("make grid"):
        grid = make_grid(boundary)
        extent = snap_bounds(tuple(boundary.total_bounds), GRID_CELL_M)
        logger.info("grid extent %s, %d cells", extent, len(grid))

    with _timed("raster aggregation"):
        lst = _open_raster(config.LST_TIF, masked=True)
        wc = _open_raster(config.WORLDCOVER_TIF, masked=False)
        for name, da in (("LST", lst), ("WorldCover", wc)):
            bounds = tuple(float(b) for b in da.rio.bounds())
            if any(abs(a - b) > 1e-6 for a, b in zip(bounds, extent, strict=True)):
                raise AssertionError(f"{name} raster extent {bounds} != grid extent {extent}")
        anomaly, lst_c, n_obs = (lst.isel(band=i) for i in range(3))
        grid["lst_anomaly_raw_c"] = aggregate_raster_mean(anomaly, grid).to_numpy()
        grid["lst_c"] = aggregate_raster_mean(lst_c, grid).to_numpy()
        grid["n_obs_median"] = aggregate_raster_median(n_obs, grid).to_numpy()
        grid["lst_valid_frac"] = aggregate_raster_valid_frac(anomaly, grid).to_numpy()
        wc2d = wc.isel(band=0)
        grid["builtup_frac"] = aggregate_raster_class_fraction(
            wc2d, grid, [config.WC_BUILT]
        ).to_numpy()
        grid["tree_frac"] = aggregate_raster_class_fraction(wc2d, grid, [config.WC_TREE]).to_numpy()
        grid["water_frac"] = aggregate_raster_class_fraction(
            wc2d, grid, [config.WC_WATER]
        ).to_numpy()
        grid["green_frac_wc"] = aggregate_raster_class_fraction(
            wc2d, grid, [config.WC_TREE, config.WC_SHRUB, config.WC_GRASS, config.WC_WETLAND]
        ).to_numpy()

    with _timed("greenspace dissolve"):
        parks = dissolve_greenspace(vec["greenspace"])
        logger.info("%d dissolved parks >= %.0f m2", len(parks), GREEN_MIN_AREA_M2)

    with _timed("area fractions"):
        grid["building_frac"] = area_fraction(grid, vec["buildings"]).to_numpy()
        grid["impervious_frac"] = np.maximum(grid["builtup_frac"], grid["building_frac"])
        grid["green_frac_osm"] = area_fraction(grid, parks).to_numpy()
        grid["excl_frac"] = area_fraction(grid, vec["exclusions"]).to_numpy()

    with _timed("distance to greenspace"):
        dist = distance_to_greenspace(grid, parks)
        grid["dist_park_m"] = dist["dist_park_m"].to_numpy()
        grid["nearest_park_name"] = dist["nearest_park_name"].to_numpy()

    with _timed("streets"):
        sfeat, main_streets = street_features(grid, vec["streets"])
        for col in ("street_len_m", "street_density_m_per_ha", "main_street", "main_street_len_m"):
            grid[col] = sfeat[col].to_numpy()

    with _timed("neighbourhood"):
        grid["neighbourhood"] = assign_neighbourhood(grid, vec["places"]).to_numpy()

    grid["is_urban"] = (grid["builtup_frac"] >= config.URBAN_BUILTUP_MIN).to_numpy()
    grid["lst_anomaly_c"] = recenter_anomaly(grid)

    float32_cols = [c for c, t in GRID_SCHEMA.items() if t == "float32"]
    grid[float32_cols] = grid[float32_cols].astype("float32")
    for col in ("cell_id", "nearest_park_name", "main_street", "neighbourhood"):
        grid[col] = grid[col].astype("string")
    grid = grid[list(GRID_SCHEMA)].sort_values("cell_id").reset_index(drop=True)

    problems = validate_grid_features(grid, n_scenes_used=_scenes_used())
    if problems:
        raise ValueError("grid_features validation failed:\n- " + "\n- ".join(problems))

    urban = grid[grid["is_urban"]]
    valid_urban = urban[urban["lst_valid_frac"] >= ANOMALY_CENTER_MIN_VALID]
    logger.info(
        "cells=%d urban=%d eligible-looking(urban & valid LST)=%d median urban anomaly=%.4f",
        len(grid),
        len(urban),
        len(valid_urban),
        float(valid_urban["lst_anomaly_c"].median()),
    )
    if len(parks):
        big = parks.geometry.iloc[0]
        hit = grid[grid.geometry.contains(big.representative_point())]
        inside = grid[grid.geometry.centroid.within(big)]
        if (len(hit) and not hit["green_frac_osm"].iloc[0] > 0.5) or (
            len(inside) and (inside["dist_park_m"] > 0).any()
        ):
            logger.warning("Largest-park spot check failed (park %s)", parks["park_id"].iloc[0])

    with _timed("write parquet"):
        written = {
            "grid_features": _atomic_write_parquet(grid, GRID_FEATURES_PARQUET),
            "greenspace_dissolved": _atomic_write_parquet(parks, GREENSPACE_DISSOLVED_PARQUET),
            "cell_main_streets": _atomic_write_parquet(main_streets, CELL_MAIN_STREETS_PARQUET),
        }
    for path in written.values():
        logger.info("wrote %s", path)
    return written


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``uv run python -m edinburgh_uhi.spatial_grid``."""
    parser = argparse.ArgumentParser(description="Build the 250 m BNG grid and cell features.")
    parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    build_grid_features()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
