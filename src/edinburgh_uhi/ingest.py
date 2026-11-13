"""Network ingestion: OSM layers, ESA WorldCover and Landsat summer LST -> ``data/raw/``.

CRS discipline (ARCHITECTURE §4):

* Vector layers are cached in the source CRS, ``EPSG:4326`` (GeoParquet, never Shapefile).
* STAC ``bbox`` arguments are ``EPSG:4326``.
* Rasters are resampled exactly once, straight onto a pixel grid in ``EPSG:27700`` whose origin
  is snapped to a multiple of ``GRID_CELL_M`` so each 250 m cell is an exact block of pixels.
* The only ``to_crs`` call in this module is :func:`_to_bng` (4326 -> 27700), used to compute
  the snapped extent, the inside-boundary mask and the boundary-area sanity check.

Every step is idempotent (skipped when its outputs exist unless ``--force``), every write is
atomic (temp file + ``os.replace``) and every network call is retried with backoff.

Run::

    uv run python -m edinburgh_uhi.ingest --step all
    uv run python -m edinburgh_uhi.ingest --step validate
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import geopandas as gpd
import numpy as np
import pandas as pd
from affine import Affine
from shapely.geometry import box

from edinburgh_uhi import config

if TYPE_CHECKING:
    from odc.geo.geobox import GeoBox
    from pystac import Item
    from pystac_client import Client

__all__ = [
    "InsufficientScenesError",
    "NetworkError",
    "fetch_boundary",
    "fetch_lst",
    "fetch_osm_layers",
    "fetch_worldcover",
    "main",
    "validate_raw_outputs",
]

logger = logging.getLogger("edinburgh_uhi.ingest")

# Layer name -> (output path, kept non-geometry columns, allowed geometry types).
_OSM_LAYERS: dict[str, tuple[Path, tuple[str, ...], frozenset[str]]] = {
    "buildings": (
        config.OSM_BUILDINGS_PARQUET,
        ("osm_uid", "building"),
        frozenset({"Polygon", "MultiPolygon"}),
    ),
    "greenspace": (
        config.OSM_GREENSPACE_PARQUET,
        ("osm_uid", "green_type", "name", "access"),
        frozenset({"Polygon", "MultiPolygon"}),
    ),
    "streets": (
        config.OSM_STREETS_PARQUET,
        ("u", "v", "key", "osm_ids", "name", "highway"),
        frozenset({"LineString"}),
    ),
    "places": (
        config.OSM_PLACES_PARQUET,
        ("osm_uid", "name", "place"),
        frozenset({"Point"}),
    ),
    "exclusions": (
        config.OSM_EXCLUSIONS_PARQUET,
        ("osm_uid", "excl_type"),
        frozenset({"Polygon", "MultiPolygon"}),
    ),
}
_BOUNDARY_COLUMNS = ("name", "osm_id")
_LST_BAND_NAMES = ("lst_anomaly_c", "lst_c", "n_obs")
_SAS_MARKERS = ("sig=", "?st=", "&se=")
# Overpass rejects very large request bodies (HTTP 413), so queries use a simplified polygon
# (tolerance in degrees, ~50 m) and fall back to a bounding box if it is still too big.
_QUERY_SIMPLIFY_DEG = 0.0005
_QUERY_MAX_VERTICES = 400
_OVERPASS_MAX_QUERY_AREA_M2 = 2_500_000_000


class InsufficientScenesError(RuntimeError):
    """Raised when fewer than ``LST_MIN_SCENES`` clear Landsat scenes survive filtering."""


class NetworkError(RuntimeError):
    """Raised when a network call still fails after all retry attempts."""


# --------------------------------------------------------------------------------------
# Generic helpers: retry, atomic writes, CRS
# --------------------------------------------------------------------------------------
def _retry[T](
    fn: Callable[[], T],
    attempts: int = config.NETWORK_RETRY_ATTEMPTS,
    *,
    what: str = "network call",
    base_delay_s: float = config.NETWORK_RETRY_BASE_DELAY_S,
) -> T:
    """Call ``fn`` up to ``attempts`` times with exponential backoff (2 s, 4 s, ...).

    Raises :class:`NetworkError` (chained to the last error) if every attempt fails.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt == attempts:
                break
            delay = base_delay_s * 2 ** (attempt - 1)
            logger.warning(
                "%s failed (attempt %d/%d): %s - retrying in %.0f s",
                what,
                attempt,
                attempts,
                exc,
                delay,
            )
            time.sleep(delay)
    raise NetworkError(
        f"{what} failed after {attempts} attempts: {last_exc}. "
        "Re-run the same command to resume (the OSMnx cache keeps partial progress)."
    ) from last_exc


def _tmp_path(path: Path) -> Path:
    return path.with_name(path.name + ".tmp")


def _atomic_write_parquet(gdf: gpd.GeoDataFrame, path: Path) -> None:
    """Write GeoParquet to ``<path>.tmp`` then ``os.replace`` it into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    try:
        gdf.to_parquet(tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_write_json(obj: dict[str, Any], path: Path) -> None:
    """Write JSON atomically; refuses to persist anything that looks like a signed URL."""
    text = json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
    for marker in _SAS_MARKERS:
        if marker in text:
            raise ValueError(f"Refusing to write {path.name}: contains SAS marker {marker!r}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_write_raster(
    data: np.ndarray,
    path: Path,
    *,
    transform: Affine,
    nodata: float | int,
    descriptions: Sequence[str],
) -> None:
    """Write a ``(bands, rows, cols)`` array as a tiled DEFLATE GeoTIFF in EPSG:27700."""
    import rasterio
    from rasterio.crs import CRS

    if data.ndim != 3 or data.shape[0] != len(descriptions):
        raise ValueError(f"data shape {data.shape} does not match {len(descriptions)} bands")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    is_float = np.issubdtype(data.dtype, np.floating)
    try:
        with rasterio.open(
            tmp,
            "w",
            driver="GTiff",
            height=data.shape[1],
            width=data.shape[2],
            count=data.shape[0],
            dtype=data.dtype,
            crs=CRS.from_epsg(27700),
            transform=transform,
            nodata=nodata,
            compress="DEFLATE",
            predictor=3 if is_float else 2,
            tiled=True,
            blockxsize=256,
            blockysize=256,
        ) as dst:
            dst.write(data)
            dst.descriptions = tuple(descriptions)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _to_bng(gdf_4326: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Reproject an EPSG:4326 frame to EPSG:27700 (the only ``to_crs`` call in this module)."""
    if gdf_4326.crs is None or not gdf_4326.crs.equals(config.CRS_WGS84):
        raise ValueError(f"Expected {config.CRS_WGS84} input, got {gdf_4326.crs}")
    return gdf_4326.to_crs(config.CRS_BNG)


def _configure_osmnx() -> None:
    """Apply the global OSMnx settings from ARCHITECTURE §3.4 (idempotent)."""
    import osmnx as ox

    config.OSMNX_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ox.settings.use_cache = True
    ox.settings.cache_folder = str(config.OSMNX_CACHE_DIR)
    ox.settings.requests_timeout = 300
    ox.settings.log_console = False
    # Edinburgh (~260 km²) is far below this, so OSMnx sends one request with a compact polygon
    # instead of subdividing it into many large multi-polygon queries.
    ox.settings.max_query_area_size = _OVERPASS_MAX_QUERY_AREA_M2


def _log_cached(*paths: Path) -> None:
    for path in paths:
        logger.info("cached: %s", path)


# --------------------------------------------------------------------------------------
# Boundary
# --------------------------------------------------------------------------------------
def _check_boundary_area(boundary_4326: gpd.GeoDataFrame) -> float:
    """Return the boundary area in km² (computed in EPSG:27700); raise if implausible."""
    area_km2 = float(_to_bng(boundary_4326).area.iloc[0] / 1e6)
    lo, hi = config.BOUNDARY_AREA_KM2_RANGE
    if not lo <= area_km2 <= hi:
        raise ValueError(
            f"Boundary area {area_km2:.1f} km² is outside the expected {lo:.0f}-{hi:.0f} km² "
            "range - Nominatim may have returned the wrong polygon."
        )
    return area_km2


def fetch_boundary(force: bool = False) -> Path:
    """Fetch the City of Edinburgh boundary and cache it as EPSG:4326 GeoParquet.

    Output columns: ``name``, ``osm_id`` (``"R<id>"``), ``geometry``. Returns the path written.
    """
    path = config.BOUNDARY_PARQUET
    if path.exists() and not force:
        _log_cached(path)
        return path

    import osmnx as ox

    _configure_osmnx()
    pinned = config.BOUNDARY_OSM_ID
    query = pinned or config.PLACE_QUERY
    raw = _retry(
        lambda: ox.geocode_to_gdf(query, by_osmid=pinned is not None),
        what=f"Nominatim boundary lookup ({query})",
    )
    if len(raw) != 1:
        raise ValueError(f"Expected exactly one boundary row, got {len(raw)}")
    if raw.crs is None or not raw.crs.equals(config.CRS_WGS84):
        raise ValueError(f"Nominatim returned CRS {raw.crs}, expected {config.CRS_WGS84}")
    geom = raw.geometry.iloc[0]
    if geom.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError(f"Boundary geometry must be a (Multi)Polygon, got {geom.geom_type}")

    row = raw.iloc[0]
    osm_uid = f"{str(row['osm_type'])[:1].upper()}{row['osm_id']}"
    if pinned is not None and osm_uid != pinned:
        raise ValueError(f"Pinned BOUNDARY_OSM_ID {pinned} but Nominatim returned {osm_uid}")
    name = row.get("name") if "name" in raw.columns else None
    name = str(name) if isinstance(name, str) and name else str(row.get("display_name", query))

    out = gpd.GeoDataFrame(
        {"name": pd.array([name], dtype="string"), "osm_id": pd.array([osm_uid], dtype="string")},
        geometry=[geom],
        crs=config.CRS_WGS84,
    )
    area_km2 = _check_boundary_area(out)
    _atomic_write_parquet(out, path)
    logger.info("Boundary %s (%s): %.1f km² -> %s", name, osm_uid, area_km2, path)
    if pinned is None:
        logger.warning("Pin BOUNDARY_OSM_ID = '%s' in config.py", osm_uid)
    return path


def _load_boundary() -> gpd.GeoDataFrame:
    """Read the cached EPSG:4326 boundary, fetching it first if it is missing."""
    path = fetch_boundary()
    return gpd.read_parquet(path)


# --------------------------------------------------------------------------------------
# OSM layers
# --------------------------------------------------------------------------------------
def _derive_type(
    flat: pd.DataFrame, tags: dict[str, bool | list[str]], default: str | None = None
) -> pd.Series:
    """Build a ``"key=value"`` label from whichever tag matched first (dict order)."""
    out = pd.Series(pd.NA, index=flat.index, dtype="object")
    for key, allowed in tags.items():
        if key not in flat.columns:
            continue
        col = flat[key].astype("string")
        hit = col.notna() & out.isna()
        if allowed is not True:
            hit &= col.isin(list(allowed))
        out[hit] = key + "=" + col[hit]
    if default is not None:
        out = out.fillna(default)
    return out


def _flatten_osm(
    gdf: gpd.GeoDataFrame,
    keep_cols: Sequence[str],
    geom_types: frozenset[str],
    *,
    derive: tuple[str, dict[str, bool | list[str]]] | None = None,
) -> gpd.GeoDataFrame:
    """Flatten an OSMnx 2.x feature frame (MultiIndex of element type and id).

    Builds ``osm_uid = "<element>/<id>"``, optionally derives a ``"key=value"`` column,
    keeps only ``keep_cols`` (cast to ``string``) plus geometry, and drops duplicate,
    empty and wrong-type geometries. Index level names are detected at runtime.
    """
    flat = gdf.reset_index()
    elem_col = next((c for c in ("element", "element_type") if c in flat.columns), None)
    id_col = next((c for c in ("id", "osmid") if c in flat.columns), None)
    if elem_col is None or id_col is None:
        raise ValueError(f"Cannot find element/id columns in OSM frame: {list(flat.columns)[:10]}")
    flat["osm_uid"] = flat[elem_col].astype(str) + "/" + flat[id_col].astype(str)
    if derive is not None:
        flat[derive[0]] = _derive_type(flat, derive[1])

    flat = flat[flat.geometry.notna() & ~flat.geometry.is_empty]
    flat = flat[flat.geom_type.isin(geom_types)].drop_duplicates("osm_uid")
    for col in keep_cols:
        if col not in flat.columns:
            flat[col] = pd.NA
    out = flat[[*keep_cols, "geometry"]].copy()
    for col in keep_cols:
        out[col] = out[col].astype("string")
    return gpd.GeoDataFrame(out, geometry="geometry", crs=gdf.crs)


def _as_list(value: Any) -> list[Any]:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return []
    if isinstance(value, list | tuple | set):
        return list(value)
    return [value]


def _join_unique(value: Any, sep: str) -> Any:
    items = list(dict.fromkeys(str(v) for v in _as_list(value)))
    return sep.join(items) if items else pd.NA


def _first(value: Any) -> Any:
    items = _as_list(value)
    return str(items[0]) if items else pd.NA


def _normalise_streets(edges: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Flatten OSMnx edges: list-valued attributes -> strings; keep only the contract columns."""
    e = edges.reset_index()
    e["name"] = e["name"].map(lambda v: _join_unique(v, " / ")) if "name" in e else pd.NA
    e["highway"] = e["highway"].map(_first) if "highway" in e else pd.NA
    e["osm_ids"] = e["osmid"].map(lambda v: _join_unique(v, ",")) if "osmid" in e else pd.NA
    e = e[e.geometry.notna() & ~e.geometry.is_empty & (e.geom_type == "LineString")]
    cols = _OSM_LAYERS["streets"][1]
    out = e[[*cols, "geometry"]].copy()
    for col in cols:
        out[col] = out[col].astype("string")
    return gpd.GeoDataFrame(out, geometry="geometry", crs=edges.crs)


def _query_geometry(boundary_poly: Any) -> Any:
    """Compact geometry for Overpass: simplified polygon that still covers the boundary.

    Simplification can shave slivers off the boundary, so the result is buffered outwards by
    the same tolerance (mitre joins keep the vertex count low). If it is still too large, the
    bounding box is returned instead. The exact boundary is applied afterwards by
    :func:`_clip_to_boundary`.
    """
    tol = _QUERY_SIMPLIFY_DEG
    simple = boundary_poly.simplify(tol, preserve_topology=True)
    cover = simple.buffer(tol * 1.5, join_style="mitre").simplify(tol, preserve_topology=True)
    n_vertices = sum(len(p.exterior.coords) for p in getattr(cover, "geoms", [cover]))
    if n_vertices > _QUERY_MAX_VERTICES:
        logger.info("Query polygon has %d vertices; using bounding box instead", n_vertices)
        return box(*boundary_poly.bounds)
    return cover


def _overpass_features(query_geom: Any, tags: dict[str, bool | list[str]]) -> gpd.GeoDataFrame:
    import osmnx as ox

    if query_geom.geom_type == "Polygon" and query_geom.equals(box(*query_geom.bounds)):
        return ox.features_from_bbox(query_geom.bounds, tags)
    return ox.features_from_polygon(query_geom, tags)


def _clip_to_boundary(gdf: gpd.GeoDataFrame, boundary_poly: Any) -> gpd.GeoDataFrame:
    """Clip a query result to the exact (unsimplified) boundary, in EPSG:4326."""
    gdf = gdf.copy()
    gdf["geometry"] = gdf.geometry.make_valid()
    clipped = gdf.clip(boundary_poly, keep_geom_type=True)
    return clipped[~clipped.geometry.is_empty]


def _fetch_osm_layer(name: str, boundary_poly: Any) -> gpd.GeoDataFrame:
    """Download and normalise one OSM layer inside the exact EPSG:4326 boundary polygon.

    Overpass is queried with a compact geometry (see :func:`_query_geometry`) and the result
    is clipped locally to ``boundary_poly``.
    """
    import osmnx as ox

    _, cols, geom_types = _OSM_LAYERS[name]
    query_geom = _query_geometry(boundary_poly)

    if name == "streets":
        if query_geom.equals(box(*query_geom.bounds)):
            graph = _retry(
                lambda: ox.graph_from_bbox(
                    query_geom.bounds,
                    custom_filter=config.STREET_CUSTOM_FILTER,
                    simplify=True,
                    retain_all=True,
                    truncate_by_edge=True,
                ),
                what="Overpass street network",
            )
        else:
            graph = _retry(
                lambda: ox.graph_from_polygon(
                    query_geom,
                    custom_filter=config.STREET_CUSTOM_FILTER,
                    simplify=True,
                    retain_all=True,
                    truncate_by_edge=True,
                ),
                what="Overpass street network",
            )
        edges = _clip_to_boundary(ox.graph_to_gdfs(graph, nodes=False), boundary_poly)
        edges = edges.explode(index_parts=False)  # clipping can yield MultiLineStrings
        return _normalise_streets(edges)

    tags = {
        "buildings": config.BUILDING_TAGS,
        "greenspace": config.GREENSPACE_TAGS,
        "places": config.PLACE_TAGS,
        "exclusions": config.EXCLUSION_TAGS,
    }[name]
    raw = _retry(lambda: _overpass_features(query_geom, tags), what=f"Overpass {name}")
    raw = _clip_to_boundary(raw, boundary_poly)

    if name == "places":
        # Polygonal places become points; everything else non-point is dropped.
        raw = raw.copy()
        is_poly = raw.geom_type.isin({"Polygon", "MultiPolygon"})
        raw.loc[is_poly, "geometry"] = raw.loc[is_poly].geometry.representative_point()
        out = _flatten_osm(raw, cols, geom_types)
        named = out["name"].notna()
        if not named.all():
            logger.info("places: dropping %d unnamed places", int((~named).sum()))
        return out[named].reset_index(drop=True)

    derive = {
        "greenspace": ("green_type", config.GREENSPACE_TAGS),
        "exclusions": ("excl_type", config.EXCLUSION_TAGS),
    }.get(name)
    return _flatten_osm(raw, cols, geom_types, derive=derive)


def fetch_osm_layers(force: bool = False) -> dict[str, Path]:
    """Fetch buildings, greenspace, streets, places and exclusions as EPSG:4326 GeoParquet.

    Each layer is cached independently. Returns ``{layer_name: path}``.
    """
    paths = {name: spec[0] for name, spec in _OSM_LAYERS.items()}
    todo = [name for name, path in paths.items() if force or not path.exists()]
    for name in paths.keys() - set(todo):
        _log_cached(paths[name])
    if not todo:
        return paths

    _configure_osmnx()
    boundary = _load_boundary()
    poly = boundary.geometry.iloc[0]
    for name in todo:
        started = time.monotonic()
        layer = _fetch_osm_layer(name, poly)
        if layer.empty:
            raise ValueError(f"OSM layer '{name}' came back empty")
        _atomic_write_parquet(layer, paths[name])
        logger.info(
            "OSM %s: %d features in %.0f s -> %s",
            name,
            len(layer),
            time.monotonic() - started,
            paths[name],
        )
    return paths


# --------------------------------------------------------------------------------------
# Raster grid and STAC helpers
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class _AnalysisGrid:
    """Pixel grid in EPSG:27700 whose origin and extent are multiples of ``GRID_CELL_M``."""

    res_m: float
    xmin: float
    ymax: float
    width: int
    height: int
    transform: Affine
    geobox: GeoBox


def _analysis_geobox(boundary_bng: gpd.GeoDataFrame, res_m: float) -> _AnalysisGrid:
    """Build the snapped EPSG:27700 pixel grid covering the boundary at ``res_m`` resolution.

    ``boundary_bng`` must be EPSG:27700. Asserts each 250 m cell holds an integer pixel block.
    """
    from odc.geo.geobox import GeoBox

    cell = config.GRID_CELL_M
    block = cell / res_m
    if abs(block - round(block)) > 1e-9:
        raise ValueError(f"Cell size {cell} m is not a whole number of {res_m} m pixels")
    xmin, ymin, xmax, ymax = config.snap_bounds(tuple(boundary_bng.total_bounds), cell)
    width = round((xmax - xmin) / res_m)
    height = round((ymax - ymin) / res_m)
    if xmin % cell or ymin % cell or width % round(block) or height % round(block):
        raise AssertionError(
            f"Grid origin/extent not aligned to {cell} m: ({xmin}, {ymin}, {xmax}, {ymax})"
        )
    transform = Affine(res_m, 0.0, xmin, 0.0, -res_m, ymax)
    geobox = GeoBox((height, width), transform, config.CRS_BNG)
    return _AnalysisGrid(res_m, xmin, ymax, width, height, transform, geobox)


def _inside_mask(boundary_bng: gpd.GeoDataFrame, grid: _AnalysisGrid) -> np.ndarray:
    """Boolean ``(height, width)`` mask, True for pixels inside the EPSG:27700 boundary."""
    from rasterio.features import geometry_mask

    return geometry_mask(
        [boundary_bng.geometry.iloc[0]],
        out_shape=(grid.height, grid.width),
        transform=grid.transform,
        invert=True,
    )


def _open_stac_client() -> Client:
    import planetary_computer
    from pystac_client import Client

    return _retry(
        lambda: Client.open(config.STAC_URL, modifier=planetary_computer.sign_inplace),
        what="Planetary Computer STAC connection",
    )


def _qa_valid_mask(qa: np.ndarray, bits: Sequence[int]) -> np.ndarray:
    """True where none of the QA_PIXEL ``bits`` is set. NaN is treated as invalid (fill)."""
    mask_bits = sum(1 << b for b in bits)
    qa_int = np.nan_to_num(np.asarray(qa), nan=1).astype(np.uint32)
    return (qa_int & mask_bits) == 0


def _check_grid_shape(array_shape: tuple[int, ...], grid: _AnalysisGrid, what: str) -> None:
    if tuple(array_shape[-2:]) != (grid.height, grid.width):
        raise AssertionError(
            f"{what} loaded with shape {array_shape[-2:]}, expected {(grid.height, grid.width)}"
        )


# --------------------------------------------------------------------------------------
# WorldCover
# --------------------------------------------------------------------------------------
def fetch_worldcover(force: bool = False) -> Path:
    """Fetch ESA WorldCover 2021 (v200) as a uint8 GeoTIFF in EPSG:27700 at 10 m.

    The raster is loaded with nearest-neighbour resampling onto the snapped analysis grid, so
    each 250 m cell is an exact 25 x 25 pixel block.
    """
    path = config.WORLDCOVER_TIF
    if path.exists() and not force:
        _log_cached(path)
        return path

    import odc.stac
    import planetary_computer

    boundary = _load_boundary()
    grid = _analysis_geobox(_to_bng(boundary), config.WORLDCOVER_RES_M)
    bbox = [float(v) for v in boundary.total_bounds]
    client = _open_stac_client()
    year = config.WORLDCOVER_YEAR
    items = _retry(
        lambda: list(
            client.search(
                collections=[config.WORLDCOVER_COLLECTION],
                bbox=bbox,
                datetime=f"{year}-01-01/{year}-12-31",
            ).item_collection()
        ),
        what="WorldCover STAC search",
    )
    if not items:
        raise NetworkError(f"No WorldCover {year} items found for bbox {bbox}")
    logger.info("WorldCover: %d tile(s): %s", len(items), [i.id for i in items])

    ds = _retry(
        lambda: odc.stac.load(
            items,
            bands=["map"],
            geobox=grid.geobox,
            resampling="nearest",
            patch_url=planetary_computer.sign,
        ),
        what="WorldCover raster load",
    )
    da = ds["map"]
    if "time" in da.dims:
        da = da.max("time")  # all tiles share one nominal date; 0 is nodata so max mosaics
    _check_grid_shape(da.shape, grid, "WorldCover")
    data = np.nan_to_num(da.values, nan=0).astype(np.uint8)[np.newaxis]

    _atomic_write_raster(
        data, path, transform=grid.transform, nodata=0, descriptions=("worldcover_class",)
    )
    classes, counts = np.unique(data, return_counts=True)
    logger.info(
        "WorldCover: %dx%d px, class counts %s -> %s",
        grid.width,
        grid.height,
        dict(zip(classes.tolist(), counts.tolist(), strict=True)),
        path,
    )
    return path


# --------------------------------------------------------------------------------------
# Landsat LST
# --------------------------------------------------------------------------------------
def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _search_landsat_year(client: Client, bbox: list[float], year: int) -> list[Item]:
    """Search one meteorological summer; falls back to a looser query if the API rejects it."""
    from pystac_client.exceptions import APIError

    window = f"{year}-06-01/{year}-08-31"
    full_query = {
        "eo:cloud_cover": {"lt": config.LST_MAX_SCENE_CLOUD},
        "platform": {"in": list(config.LST_PLATFORMS)},
    }
    try:
        search = client.search(
            collections=[config.LST_COLLECTION], bbox=bbox, datetime=window, query=full_query
        )
        return list(search.item_collection())
    except APIError:
        logger.warning("STAC server rejected the platform query; filtering client-side")
        search = client.search(
            collections=[config.LST_COLLECTION],
            bbox=bbox,
            datetime=window,
            query={"eo:cloud_cover": {"lt": config.LST_MAX_SCENE_CLOUD}},
        )
        return list(search.item_collection())


def _discover_landsat_items(client: Client, bbox: list[float]) -> list[Item]:
    """Per-year summer search -> client-side T1/platform/cloud filter. ``bbox`` is EPSG:4326."""
    first, last = config.LST_YEARS
    found: dict[str, Item] = {}
    for year in range(first, last + 1):
        items = _retry(
            lambda y=year: _search_landsat_year(client, bbox, y),
            what=f"Landsat STAC search {year}",
        )
        kept = 0
        for item in items:
            props = item.properties
            if (
                props.get("landsat:collection_category") == "T1"
                and props.get("platform") in config.LST_PLATFORMS
                and props.get("eo:cloud_cover", 100.0) < config.LST_MAX_SCENE_CLOUD
                and item.datetime is not None
                and item.datetime.month in config.LST_MONTHS
            ):
                found[item.id] = item
                kept += 1
        logger.info("Landsat %d: %d candidate scene(s) (%d returned)", year, kept, len(items))
    return sorted(found.values(), key=lambda i: (i.datetime, i.id))


def _load_pinned_items(client: Client, manifest: dict[str, Any]) -> list[Item]:
    """Re-fetch exactly the manifest's scenes by id (reproducible; no discovery search)."""
    ids = [s["id"] for s in manifest["scenes"]]
    items: list[Item] = []
    for start in range(0, len(ids), 50):
        batch = ids[start : start + 50]
        items += _retry(
            lambda b=batch: list(
                client.search(collections=[config.LST_COLLECTION], ids=b).item_collection()
            ),
            what="Landsat pinned scene lookup",
        )
    missing = set(ids) - {i.id for i in items}
    if missing:
        logger.warning(
            "%d manifest scene(s) no longer in the catalog: %s", len(missing), sorted(missing)
        )
    return items


def _new_manifest(items: Sequence[Item]) -> dict[str, Any]:
    first, last = config.LST_YEARS
    return {
        "created_utc": _utc_stamp(),
        "stac_url": config.STAC_URL,
        "collection": config.LST_COLLECTION,
        "query": {
            "years": [first, last],
            "months": list(config.LST_MONTHS),
            "max_scene_cloud": config.LST_MAX_SCENE_CLOUD,
            "min_scene_clear_frac": config.LST_MIN_SCENE_CLEAR_FRAC,
        },
        "scenes": [
            {
                "id": i.id,
                "datetime": i.datetime.isoformat() if i.datetime else None,
                "platform": i.properties.get("platform"),
                "eo_cloud_cover": i.properties.get("eo:cloud_cover"),
                "boundary_clear_frac": None,
                "used": False,
            }
            for i in items
        ],
    }


def _read_manifest() -> dict[str, Any] | None:
    path = config.LST_MANIFEST_JSON
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(manifest.get("scenes"), list):
            raise ValueError("missing 'scenes' list")
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(
            f"Cannot read {path}: {exc}. Delete it or use --refresh-manifest."
        ) from exc
    return manifest


def _scene_date(scene: dict[str, Any]) -> date:
    return date.fromisoformat(str(scene["datetime"])[:10])


def _annotate_air_temp(dates: Sequence[date]) -> dict[str, float | None]:
    """Daily max air temperature (°C) at Edinburgh's centroid from Open-Meteo, by ISO date.

    Optional context only: any failure logs a warning and returns ``{}`` - it never fails the
    pipeline.
    """
    if not dates:
        return {}
    lat, lon = config.EDINBURGH_CENTER_LATLON
    params: dict[str, str | float] = {
        "latitude": lat,
        "longitude": lon,
        "start_date": min(dates).isoformat(),
        "end_date": max(dates).isoformat(),
        "daily": "temperature_2m_max",
        "timezone": "Europe/London",
    }
    try:
        import requests

        def _get() -> dict[str, Any]:
            resp = requests.get(config.OPEN_METEO_ARCHIVE_URL, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()

        daily = _retry(_get, what="Open-Meteo archive request")["daily"]
        lookup = dict(zip(daily["time"], daily["temperature_2m_max"], strict=True))
    except Exception as exc:
        logger.warning("Open-Meteo air-temperature annotation skipped: %s", exc)
        return {}
    return {
        d.isoformat(): (None if lookup.get(d.isoformat()) is None else float(lookup[d.isoformat()]))
        for d in dates
    }


def _fill_air_temp(scenes: list[dict[str, Any]]) -> bool:
    """Fill missing ``air_tmax_c`` entries in place. Returns True if the manifest changed."""
    pending = [s for s in scenes if s.get("air_tmax_c") is None]
    if not pending:
        return False
    temps = _annotate_air_temp(sorted({_scene_date(s) for s in pending}))
    changed = False
    for scene in pending:
        value = temps.get(_scene_date(scene).isoformat())
        if "air_tmax_c" not in scene or value is not None:
            scene["air_tmax_c"] = value
            changed = True
    return changed


def _build_composite(
    items: Sequence[Item], grid: _AnalysisGrid, inside: np.ndarray
) -> tuple[np.ndarray, dict[str, float]]:
    """Load scenes, mask, convert to °C and build the 3-band anomaly composite.

    Returns ``(composite[3, rows, cols] float32, clear_frac_by_solar_day)``. Bands are
    ``lst_anomaly_c``, ``lst_c`` and ``n_obs`` (ARCHITECTURE §3.2.5-3.2.6). Each solar-day
    mosaic is one observation. Raises :class:`InsufficientScenesError` if too few survive.
    """
    import odc.stac
    import planetary_computer

    ds = _retry(
        lambda: odc.stac.load(
            list(items),
            bands=["lwir11", "qa_pixel"],
            geobox=grid.geobox,
            resampling={"lwir11": "bilinear", "qa_pixel": "nearest"},
            groupby="solar_day",
            patch_url=planetary_computer.sign,
        ),
        what="Landsat raster load",
    )
    _check_grid_shape(ds["lwir11"].shape, grid, "Landsat")

    n_inside = int(inside.sum())
    if n_inside == 0:
        raise ValueError("Boundary mask is empty - check the analysis grid")
    clear_by_day: dict[str, float] = {}
    lst_layers: list[np.ndarray] = []
    anomaly_layers: list[np.ndarray] = []

    for i, stamp in enumerate(pd.to_datetime(ds["time"].values)):
        dn = np.asarray(ds["lwir11"].isel(time=i).values, dtype=np.float64)
        qa = ds["qa_pixel"].isel(time=i).values
        valid = np.isfinite(dn) & (dn != 0) & _qa_valid_mask(qa, config.LST_QA_MASK_BITS) & inside
        clear = float(valid.sum()) / n_inside
        clear_by_day[stamp.date().isoformat()] = clear
        if clear < config.LST_MIN_SCENE_CLEAR_FRAC:
            continue
        lst = np.full(dn.shape, np.nan, dtype=np.float32)
        lst[valid] = dn[valid] * config.LST_SCALE + config.LST_OFFSET_K - config.KELVIN_TO_C
        city_median = np.float32(np.median(lst[valid]))
        lst_layers.append(lst)
        anomaly_layers.append(lst - city_median)

    if len(lst_layers) < config.LST_MIN_SCENES:
        listing = ", ".join(f"{d}: {f:.2f}" for d, f in sorted(clear_by_day.items()))
        raise InsufficientScenesError(
            f"Only {len(lst_layers)} Landsat solar-day scene(s) have >= "
            f"{config.LST_MIN_SCENE_CLEAR_FRAC:.0%} clear boundary pixels "
            f"(need {config.LST_MIN_SCENES}). Clear fractions - {listing or 'none loaded'}. "
            "Widen LST_YEARS or raise LST_MAX_SCENE_CLOUD / lower LST_MIN_SCENE_CLEAR_FRAC "
            "in config.py, then re-run with --refresh-manifest."
        )

    lst_stack = np.stack(lst_layers)
    anomaly_stack = np.stack(anomaly_layers)
    del lst_layers, anomaly_layers
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)  # all-NaN slices -> NaN
        lst_median = np.nanmedian(lst_stack, axis=0)
        anomaly_median = np.nanmedian(anomaly_stack, axis=0)
    n_obs = np.isfinite(lst_stack).sum(axis=0)
    composite = np.stack([anomaly_median, lst_median, n_obs]).astype(np.float32)
    return composite, clear_by_day


def _check_lst_plausible(composite: np.ndarray, inside: np.ndarray) -> None:
    """Log P1/P50/P99 of ``lst_c`` inside the boundary; raise if outside 5-50 °C."""
    values = composite[1][inside & np.isfinite(composite[1])]
    if values.size == 0:
        raise ValueError("LST composite has no finite pixels inside the boundary")
    p1, p50, p99 = np.percentile(values, [1, 50, 99])
    logger.info("LST lst_c inside boundary: P1=%.1f P50=%.1f P99=%.1f °C", p1, p50, p99)
    if p1 < 5 or p99 > 50:
        raise ValueError(
            f"Implausible summer LST (P1={p1:.1f}, P99={p99:.1f} °C) - check scaling/masking"
        )


def fetch_lst(force: bool = False, refresh_manifest: bool = False) -> tuple[Path, Path]:
    """Build the Landsat summer LST anomaly composite and its scene manifest.

    Without ``refresh_manifest`` an existing manifest's scene IDs are reused (pinned reload),
    which keeps the composite reproducible. Returns ``(tif_path, manifest_path)``.
    """
    tif, manifest_path = config.LST_TIF, config.LST_MANIFEST_JSON
    if tif.exists() and manifest_path.exists() and not force and not refresh_manifest:
        _log_cached(tif, manifest_path)
        return tif, manifest_path

    boundary = _load_boundary()
    boundary_bng = _to_bng(boundary)
    grid = _analysis_geobox(boundary_bng, config.LST_RES_M)
    inside = _inside_mask(boundary_bng, grid)
    client = _open_stac_client()

    manifest = None if refresh_manifest else _read_manifest()
    pinned = manifest is not None
    if manifest is None:
        items = _discover_landsat_items(client, [float(v) for v in boundary.total_bounds])
        manifest = _new_manifest(items)
    else:
        items = _load_pinned_items(client, manifest)
    scenes: list[dict[str, Any]] = manifest["scenes"]
    logger.info(
        "Landsat: %d candidate scene(s) (%s manifest)", len(items), "pinned" if pinned else "new"
    )
    dirty = not pinned

    if config.LST_ANNOTATE_AIR_TEMP:
        dirty |= _fill_air_temp(scenes)

    if config.LST_MIN_AIR_TMAX_C is not None:
        warm = {
            s["id"]
            for s in scenes
            if s.get("air_tmax_c") is None or s["air_tmax_c"] >= config.LST_MIN_AIR_TMAX_C
        }
        items = [i for i in items if i.id in warm]
    if len(items) < config.LST_MIN_SCENES:
        raise InsufficientScenesError(
            f"Only {len(items)} candidate Landsat scene(s) (need {config.LST_MIN_SCENES}). "
            "Widen LST_YEARS or raise LST_MAX_SCENE_CLOUD in config.py and use "
            "--refresh-manifest."
        )

    started = time.monotonic()
    composite, clear_by_day = _build_composite(items, grid, inside)
    _check_lst_plausible(composite, inside)
    logger.info("Landsat composite built in %.0f s", time.monotonic() - started)

    loaded_ids = {i.id for i in items}
    new_flags: dict[str, tuple[float | None, bool]] = {}
    for scene in scenes:
        clear = None
        if scene["id"] in loaded_ids:
            clear = clear_by_day.get(_scene_date(scene).isoformat())
        used = clear is not None and clear >= config.LST_MIN_SCENE_CLEAR_FRAC
        new_flags[scene["id"]] = (None if clear is None else round(clear, 4), used)
    if pinned:
        changed = [s["id"] for s in scenes if bool(s.get("used")) != new_flags[s["id"]][1]]
        if changed:
            logger.warning(
                "%d scene(s) differ from the pinned 'used' flags: %s", len(changed), changed
            )
    else:
        for scene in scenes:
            scene["boundary_clear_frac"], scene["used"] = new_flags[scene["id"]]
        manifest["n_solar_days_used"] = sum(
            1 for frac in clear_by_day.values() if frac >= config.LST_MIN_SCENE_CLEAR_FRAC
        )
    if dirty:
        _atomic_write_json(manifest, manifest_path)
        logger.info("Wrote manifest %s", manifest_path)

    _atomic_write_raster(
        composite,
        tif,
        transform=grid.transform,
        nodata=float("nan"),
        descriptions=_LST_BAND_NAMES,
    )
    logger.info(
        "Wrote %s (%dx%d px, max n_obs=%d)",
        tif,
        grid.width,
        grid.height,
        int(np.nanmax(composite[2])),
    )
    return tif, manifest_path


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------
def _is_multiple(value: float, step: float, tol: float = 1e-6) -> bool:
    rem = value % step
    return min(rem, step - rem) <= tol


def _validate_vectors() -> list[str]:
    problems: list[str] = []
    specs: dict[str, tuple[Path, tuple[str, ...], frozenset[str]]] = {
        "boundary": (
            config.BOUNDARY_PARQUET,
            _BOUNDARY_COLUMNS,
            frozenset({"Polygon", "MultiPolygon"}),
        ),
        **_OSM_LAYERS,
    }
    for name, (path, cols, geom_types) in specs.items():
        if not path.exists():
            continue
        try:
            gdf = gpd.read_parquet(path)
        except Exception as exc:
            problems.append(f"{name}: cannot read {path.name}: {exc}")
            continue
        if gdf.empty:
            problems.append(f"{name}: layer is empty")
            continue
        if gdf.crs is None or not gdf.crs.equals(config.CRS_WGS84):
            problems.append(f"{name}: CRS is {gdf.crs}, expected {config.CRS_WGS84}")
        if set(gdf.columns) != {*cols, "geometry"}:
            expected = sorted([*cols, "geometry"])
            problems.append(f"{name}: columns {sorted(gdf.columns)} != {expected}")
        bad_dtype = [c for c in cols if c in gdf and not pd.api.types.is_string_dtype(gdf[c])]
        if bad_dtype:
            problems.append(f"{name}: non-string columns {bad_dtype}")
        found_types = set(gdf.geom_type.unique())
        if not found_types <= geom_types:
            problems.append(
                f"{name}: geometry types {sorted(found_types)} not in {sorted(geom_types)}"
            )
        if name == "boundary":
            if len(gdf) != 1:
                problems.append(f"boundary: expected 1 row, got {len(gdf)}")
            else:
                try:
                    _check_boundary_area(gdf)
                except ValueError as exc:
                    problems.append(f"boundary: {exc}")
    return problems


def _validate_raster(
    path: Path, res_m: float, n_bands: int, dtype: str, band_names: Sequence[str]
) -> tuple[list[str], tuple[float, ...] | None]:
    import rasterio

    problems: list[str] = []
    name = path.name
    cell = config.GRID_CELL_M
    block = round(cell / res_m)
    with rasterio.open(path) as src:
        if src.crs is None or src.crs.to_epsg() != 27700:
            problems.append(f"{name}: CRS is {src.crs}, expected EPSG:27700")
        t = src.transform
        if not _is_multiple(t.c, cell) or not _is_multiple(t.f, cell):
            problems.append(f"{name}: origin ({t.c}, {t.f}) is not a multiple of {cell} m")
        if abs(t.a - res_m) > 1e-9 or abs(t.e + res_m) > 1e-9:
            problems.append(f"{name}: pixel size ({t.a}, {t.e}) != ({res_m}, {-res_m})")
        if src.width % block or src.height % block:
            problems.append(f"{name}: size {src.width}x{src.height} is not a multiple of {block}")
        if src.count != n_bands or src.dtypes[0] != dtype:
            problems.append(f"{name}: {src.count} x {src.dtypes[0]}, expected {n_bands} x {dtype}")
        if list(src.descriptions) != list(band_names):
            problems.append(f"{name}: band descriptions {src.descriptions} != {tuple(band_names)}")
        if name == config.LST_TIF.name and src.count == 3:
            n_obs = src.read(3)
            if not np.isfinite(n_obs).any() or np.nanmax(n_obs) < config.LST_MIN_SCENES:
                problems.append(f"{name}: max n_obs < {config.LST_MIN_SCENES}")
            lst = src.read(2)
            lst = lst[np.isfinite(lst)]
            if lst.size:
                p1, p99 = np.percentile(lst, [1, 99])
                if p1 < 5 or p99 > 50:
                    problems.append(f"{name}: implausible lst_c P1={p1:.1f} P99={p99:.1f} °C")
        bounds = tuple(src.bounds)
    return problems, bounds


def _validate_manifest() -> list[str]:
    path = config.LST_MANIFEST_JSON
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    problems = [f"{path.name}: contains signed-URL marker {m!r}" for m in _SAS_MARKERS if m in text]
    try:
        manifest = json.loads(text)
        used = sum(1 for s in manifest["scenes"] if s.get("used"))
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        return [*problems, f"{path.name}: invalid manifest: {exc}"]
    if used < config.LST_MIN_SCENES:
        problems.append(f"{path.name}: only {used} scene(s) marked used")
    return problems


def validate_raw_outputs() -> list[str]:
    """Check every ``data/raw`` output against ARCHITECTURE §2/§3. Empty list means OK."""
    required = [
        config.BOUNDARY_PARQUET,
        *(spec[0] for spec in _OSM_LAYERS.values()),
        config.WORLDCOVER_TIF,
        config.LST_TIF,
        config.LST_MANIFEST_JSON,
    ]
    problems = [f"missing file: {p}" for p in required if not p.exists()]
    problems += _validate_vectors()

    bounds: dict[str, tuple[float, ...]] = {}
    rasters = (
        (config.WORLDCOVER_TIF, config.WORLDCOVER_RES_M, 1, "uint8", ("worldcover_class",)),
        (config.LST_TIF, config.LST_RES_M, 3, "float32", _LST_BAND_NAMES),
    )
    for path, res, n_bands, dtype, names in rasters:
        if not path.exists():
            continue
        try:
            found, extent = _validate_raster(path, res, n_bands, dtype, names)
        except Exception as exc:
            problems.append(f"{path.name}: cannot read raster: {exc}")
            continue
        problems += found
        if extent is not None:
            bounds[path.name] = extent
    if len(bounds) == 2:
        a, b = bounds.values()
        if not np.allclose(a, b, atol=1e-6):
            problems.append(f"WorldCover and LST extents differ: {a} vs {b}")

    problems += _validate_manifest()
    return problems


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m edinburgh_uhi.ingest",
        description="Fetch and cache all raw inputs for the Edinburgh UHI pipeline.",
    )
    parser.add_argument(
        "--step",
        choices=["boundary", "osm", "worldcover", "lst", "all", "validate"],
        default="all",
        help="which ingest step to run (default: all)",
    )
    parser.add_argument("--force", action="store_true", help="re-download even if cached")
    parser.add_argument(
        "--refresh-manifest",
        action="store_true",
        help="re-run the Landsat STAC search instead of reusing the pinned manifest",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        if args.step == "validate":
            problems = validate_raw_outputs()
            if problems:
                for problem in problems:
                    logger.error("%s", problem)
                return 1
            sys.stdout.write("OK\n")
            return 0
        if args.step in ("boundary", "all"):
            fetch_boundary(force=args.force)
        if args.step in ("osm", "all"):
            fetch_osm_layers(force=args.force)
        if args.step in ("worldcover", "all"):
            fetch_worldcover(force=args.force)
        if args.step in ("lst", "all"):
            fetch_lst(force=args.force, refresh_manifest=args.refresh_manifest)
    except (InsufficientScenesError, NetworkError) as exc:
        logger.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
