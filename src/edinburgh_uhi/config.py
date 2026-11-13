"""Project-wide constants, paths, thresholds and tiny pure helpers.

This module is the single home for every bound, weight, tag dictionary and path used by the
pipeline (``docs/ARCHITECTURE.md`` §14). It deliberately imports nothing heavy (no geopandas,
xarray, ...) so ``import edinburgh_uhi.config`` stays well under 50 ms.

CRS rule (ARCHITECTURE §4): analyse in ``CRS_BNG`` (EPSG:27700, metres); use ``CRS_WGS84``
(EPSG:4326) only at ingestion boundaries and for web export.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
DATA_RAW_DIR: Path = PROJECT_ROOT / "data" / "raw"
DATA_PROCESSED_DIR: Path = PROJECT_ROOT / "data" / "processed"
WEB_DIR: Path = DATA_PROCESSED_DIR / "web"
OSMNX_CACHE_DIR: Path = DATA_RAW_DIR / ".osmnx_cache"

# Raw artefacts written by ``ingest.py`` (ARCHITECTURE §2).
BOUNDARY_PARQUET: Path = DATA_RAW_DIR / "edinburgh_boundary.parquet"
OSM_BUILDINGS_PARQUET: Path = DATA_RAW_DIR / "osm_buildings.parquet"
OSM_GREENSPACE_PARQUET: Path = DATA_RAW_DIR / "osm_greenspace.parquet"
OSM_STREETS_PARQUET: Path = DATA_RAW_DIR / "osm_streets.parquet"
OSM_PLACES_PARQUET: Path = DATA_RAW_DIR / "osm_places.parquet"
OSM_EXCLUSIONS_PARQUET: Path = DATA_RAW_DIR / "osm_exclusions.parquet"
WORLDCOVER_TIF: Path = DATA_RAW_DIR / "worldcover_2021_27700_10m.tif"
LST_TIF: Path = DATA_RAW_DIR / "landsat_summer_lst_27700_25m.tif"
LST_MANIFEST_JSON: Path = DATA_RAW_DIR / "lst_scene_manifest.json"

# --------------------------------------------------------------------------------------
# CRS and place
# --------------------------------------------------------------------------------------
CRS_WGS84: str = "EPSG:4326"
CRS_BNG: str = "EPSG:27700"

PLACE_QUERY: str = "City of Edinburgh, Scotland, United Kingdom"
# Pin after the first successful boundary fetch (ingest logs the value), e.g. "R1234567".
BOUNDARY_OSM_ID: str | None = "R1920901"
EDINBURGH_CENTER_LATLON: tuple[float, float] = (55.9533, -3.1883)
# Sanity range for the council-area polygon, in km² (computed in EPSG:27700).
BOUNDARY_AREA_KM2_RANGE: tuple[float, float] = (200.0, 450.0)

# --------------------------------------------------------------------------------------
# Grid
# --------------------------------------------------------------------------------------
GRID_CELL_M: int = 250
GRID_MIN_INSIDE_FRAC: float = 0.25

# --------------------------------------------------------------------------------------
# Network behaviour (ARCHITECTURE §3.5)
# --------------------------------------------------------------------------------------
NETWORK_RETRY_ATTEMPTS: int = 3
NETWORK_RETRY_BASE_DELAY_S: float = 2.0  # waits 2 s, 4 s, ... between attempts

# --------------------------------------------------------------------------------------
# Landsat land surface temperature (ARCHITECTURE §3.1-3.2)
# --------------------------------------------------------------------------------------
STAC_URL: str = "https://planetarycomputer.microsoft.com/api/stac/v1"
LST_COLLECTION: str = "landsat-c2-l2"
LST_PLATFORMS: tuple[str, ...] = ("landsat-8", "landsat-9")
LST_YEARS: tuple[int, int] = (2019, 2025)  # inclusive
LST_MONTHS: tuple[int, ...] = (6, 7, 8)
LST_MAX_SCENE_CLOUD: float = 40
LST_MIN_SCENE_CLEAR_FRAC: float = 0.5
LST_MIN_SCENES: int = 3
LST_RES_M: int = 25
LST_SCALE: float = 0.00341802
LST_OFFSET_K: float = 149.0
KELVIN_TO_C: float = 273.15
# QA_PIXEL bits: fill, dilated cloud, cirrus, cloud, cloud shadow, snow, water.
LST_QA_MASK_BITS: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 7)
LST_ANNOTATE_AIR_TEMP: bool = True
LST_MIN_AIR_TMAX_C: float | None = None
OPEN_METEO_ARCHIVE_URL: str = "https://archive-api.open-meteo.com/v1/archive"

# --------------------------------------------------------------------------------------
# ESA WorldCover (ARCHITECTURE §3.3)
# --------------------------------------------------------------------------------------
WORLDCOVER_COLLECTION: str = "esa-worldcover"
WORLDCOVER_YEAR: int = 2021
WORLDCOVER_RES_M: int = 10
WC_TREE: int = 10
WC_SHRUB: int = 20
WC_GRASS: int = 30
WC_BUILT: int = 50
WC_WATER: int = 80
WC_WETLAND: int = 90

# --------------------------------------------------------------------------------------
# OSM tag dictionaries (ARCHITECTURE §3.4)
# --------------------------------------------------------------------------------------
BUILDING_TAGS: dict[str, bool | list[str]] = {"building": True}

GREENSPACE_TAGS: dict[str, bool | list[str]] = {
    "leisure": ["park", "garden", "nature_reserve", "common", "recreation_ground", "golf_course"],
    "landuse": [
        "grass",
        "forest",
        "meadow",
        "village_green",
        "recreation_ground",
        "cemetery",
        "allotments",
    ],
    "natural": ["wood", "scrub", "heath", "grassland"],
}

PLACE_TAGS: dict[str, bool | list[str]] = {"place": ["suburb", "neighbourhood", "quarter"]}

EXCLUSION_TAGS: dict[str, bool | list[str]] = {
    "aeroway": ["aerodrome"],
    "landuse": ["railway", "quarry", "military"],
}

# Motorway/trunk are excluded on purpose: not realistic street-tree corridors.
STREET_CUSTOM_FILTER: str = (
    '["highway"~"^(primary|secondary|tertiary|residential|unclassified|living_street|pedestrian)$"]'
)

# --------------------------------------------------------------------------------------
# Greenspace and neighbourhood features (ARCHITECTURE §5.3)
# --------------------------------------------------------------------------------------
GREEN_MIN_AREA_M2: float = 5_000
GREEN_MERGE_TOL_M: float = 1.0
NEIGHBOURHOOD_MAX_DIST_M: float = 1_500
URBAN_BUILTUP_MIN: float = 0.20

# --------------------------------------------------------------------------------------
# Eligibility and normalisation bounds (ARCHITECTURE §6.1-6.2)
# --------------------------------------------------------------------------------------
ELIG_MIN_LAND_FRAC: float = 0.5
ELIG_MAX_WATER_FRAC: float = 0.25
ELIG_MAX_EXCL_FRAC: float = 0.5
ELIG_MAX_GREEN_FRAC: float = 0.5
ELIG_MIN_LST_VALID: float = 0.6

THERMAL_LO_C: float = 0.0
THERMAL_HI_PERCENTILE: float = 95
THERMAL_MIN_HI_C: float = 0.5
DIST_LO_M: float = 200.0
DIST_HI_M: float = 600.0
IMPERV_LO: float = 0.20
IMPERV_HI: float = 0.90


@dataclass(frozen=True)
class Weights:
    """Greening Priority Score weights (ARCHITECTURE §6.3). Must be >= 0 and sum to 1."""

    thermal: float = 0.50
    distance: float = 0.25
    impervious: float = 0.25

    def __post_init__(self) -> None:
        values = (self.thermal, self.distance, self.impervious)
        if any(v < 0 for v in values):
            raise ValueError(f"Weights must be non-negative, got {values}")
        if abs(sum(values) - 1.0) >= 1e-9:
            raise ValueError(f"Weights must sum to 1, got {values} (sum={sum(values)})")


WEIGHTS: Weights = Weights(thermal=0.50, distance=0.25, impervious=0.25)

# --------------------------------------------------------------------------------------
# Intervention typing, top-K selection, sensitivity, web export
# --------------------------------------------------------------------------------------
INTERV_GREEN_ROOF_MIN_BUILDING: float = 0.35
INTERV_MIN_CORRIDOR_M: float = 150.0

TOPK_K: int = 15
TOPK_MIN_SEPARATION_M: float = 500.0
HOTSPOT_REL_THRESHOLD: float = 0.9
SENSITIVITY_DELTA: float = 0.10

WEB_PRECISION_DP: int = 5
PARKS_SIMPLIFY_M: float = 5.0


def snap_bounds(
    bounds: tuple[float, float, float, float], cell_m: float
) -> tuple[float, float, float, float]:
    """Expand ``(minx, miny, maxx, maxy)`` outwards to whole multiples of ``cell_m``.

    Pure helper, CRS-agnostic; used with EPSG:27700 bounds in metres so the analysis grid and
    every raster share one origin on a ``cell_m`` lattice (ARCHITECTURE §5.1).
    """
    if cell_m <= 0:
        raise ValueError(f"cell_m must be positive, got {cell_m}")
    minx, miny, maxx, maxy = bounds
    return (
        math.floor(minx / cell_m) * cell_m,
        math.floor(miny / cell_m) * cell_m,
        math.ceil(maxx / cell_m) * cell_m,
        math.ceil(maxy / cell_m) * cell_m,
    )
