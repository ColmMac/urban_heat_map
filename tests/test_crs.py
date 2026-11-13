"""CRS discipline: metric work in EPSG:27700, web layers in EPSG:4326 (ARCHITECTURE §4)."""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pyproj
import pytest
import shapely
import xarray as xr
from shapely.geometry import box

from edinburgh_uhi import config
from edinburgh_uhi.spatial_grid import (
    aggregate_raster_class_fraction,
    aggregate_raster_mean,
    area_fraction,
    dissolve_greenspace,
    distance_to_greenspace,
    make_grid,
    require_crs,
    to_web,
    validate_grid_features,
)

X0, Y0 = 320_000, 670_000  # south-west corner of the 1 km synthetic boundary (BNG metres)
CELL = config.GRID_CELL_M


# --------------------------------------------------------------------------------------
# Fixtures (tiny, synthetic, offline)
# --------------------------------------------------------------------------------------
@pytest.fixture
def boundary_bng() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(geometry=[box(X0, Y0, X0 + 1000, Y0 + 1000)], crs=config.CRS_BNG)


@pytest.fixture
def grid(boundary_bng: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    return make_grid(boundary_bng)


def _raster(blocks: np.ndarray, x_shift: float = 0.0) -> xr.DataArray:
    """25 m raster over the synthetic extent; each 250 m block (10x10 px) holds ``blocks[r, c]``."""
    data = np.kron(blocks, np.ones((10, 10)))
    n = data.shape[0]
    x = X0 + 12.5 + 25 * np.arange(n) + x_shift
    y = Y0 + 1000 - 12.5 - 25 * np.arange(n)  # y descending: row 0 = north
    da = xr.DataArray(data, dims=("y", "x"), coords={"y": y, "x": x})
    return da.rio.write_crs(config.CRS_BNG)


# --------------------------------------------------------------------------------------
# CRS constants and transformations
# --------------------------------------------------------------------------------------
def test_crs_constants() -> None:
    assert config.CRS_WGS84 == "EPSG:4326"
    assert config.CRS_BNG == "EPSG:27700"
    bng, wgs = pyproj.CRS(config.CRS_BNG), pyproj.CRS(config.CRS_WGS84)
    assert bng.is_projected and not bng.is_geographic
    assert wgs.is_geographic and not wgs.is_projected
    assert bng.axis_info[0].unit_name == "metre"


def test_edinburgh_projects_into_expected_bng_window() -> None:
    lat, lon = config.EDINBURGH_CENTER_LATLON
    e, n = pyproj.Transformer.from_crs(4326, 27700, always_xy=True).transform(lon, lat)
    assert 320_000 <= e <= 332_000
    assert 668_000 <= n <= 680_000


def test_round_trip_wgs84_bng_wgs84() -> None:
    pts = [(-3.1883, 55.9533), (-3.2, 55.94), (-3.05, 55.97), (-3.35, 55.95), (-3.12, 55.91)]
    fwd = pyproj.Transformer.from_crs(4326, 27700, always_xy=True)
    back = pyproj.Transformer.from_crs(27700, 4326, always_xy=True)
    for lon, lat in pts:
        lon2, lat2 = back.transform(*fwd.transform(lon, lat))
        assert lon2 == pytest.approx(lon, abs=1e-7)
        assert lat2 == pytest.approx(lat, abs=1e-7)


def test_bng_distance_matches_geodesic() -> None:
    """1,000 m in BNG near Edinburgh is ~1,000 m on the ellipsoid (scale factor ~0.9997)."""
    back = pyproj.Transformer.from_crs(27700, 4326, always_xy=True)
    lon1, lat1 = back.transform(325_000, 673_000)
    lon2, lat2 = back.transform(326_000, 673_000)
    _, _, dist = pyproj.Geod(ellps="WGS84").inv(lon1, lat1, lon2, lat2)
    assert abs(dist - 1000.0) / 1000.0 < 0.001


def test_snap_bounds_multiples() -> None:
    snapped = config.snap_bounds((320010.4, 670249.9, 320990, 671001), 250)
    assert snapped == (320000, 670000, 321000, 671250)


# --------------------------------------------------------------------------------------
# Grid cells are metric
# --------------------------------------------------------------------------------------
def test_make_grid_shape_and_ids(grid: gpd.GeoDataFrame) -> None:
    assert len(grid) == 16
    assert grid.crs is not None and pyproj.CRS(config.CRS_BNG).equals(grid.crs)
    assert grid["cell_id"].is_unique
    assert "E0320000_N0670000" in set(grid["cell_id"])
    assert "E0320750_N0670750" in set(grid["cell_id"])
    assert grid.geometry.area.to_numpy() == pytest.approx(CELL**2, abs=1e-6)
    north = grid.loc[grid["row"] == 0, "northing_ll"]
    assert (north == Y0 + 1000 - CELL).all()  # row 0 is the northernmost row


def test_grid_cell_dimensions_are_metres(grid: gpd.GeoDataFrame) -> None:
    minx, miny, maxx, maxy = (grid.geometry.bounds[c].to_numpy() for c in grid.geometry.bounds)
    assert (maxx - minx) == pytest.approx(CELL, abs=1e-9)
    assert (maxy - miny) == pytest.approx(CELL, abs=1e-9)
    assert (grid["easting_ll"] % CELL == 0).all()
    assert (grid["northing_ll"] % CELL == 0).all()
    # Stored centroids (cx_m, cy_m) are BNG metres and match the geometry.
    cents = grid.geometry.centroid
    assert grid["cx_m"].to_numpy() == pytest.approx(cents.x.to_numpy(), abs=1e-9)
    assert grid["cy_m"].to_numpy() == pytest.approx(cents.y.to_numpy(), abs=1e-9)


def test_neighbouring_cell_distances_in_metres(grid: gpd.GeoDataFrame) -> None:
    by_id = grid.set_index("cell_id")
    a, b, c = (
        by_id.loc["E0320000_N0670000"],
        by_id.loc["E0320250_N0670000"],
        by_id.loc["E0320250_N0670250"],
    )
    assert a.geometry.centroid.distance(b.geometry.centroid) == pytest.approx(250.0)
    # Diagonal neighbours: 250 * sqrt(2) ~ 353.55 m (why the top-K separation is 500 m).
    assert a.geometry.centroid.distance(c.geometry.centroid) == pytest.approx(250 * 2**0.5)


def test_make_grid_rejects_geographic_input(boundary_bng: gpd.GeoDataFrame) -> None:
    with pytest.raises(ValueError, match="EPSG:27700"):
        make_grid(boundary_bng.to_crs(config.CRS_WGS84))


def test_require_crs_guard(grid: gpd.GeoDataFrame) -> None:
    require_crs(grid, config.CRS_BNG)  # no raise
    with pytest.raises(ValueError):
        require_crs(grid, config.CRS_WGS84)
    with pytest.raises(ValueError, match="no CRS"):
        require_crs(grid.set_crs(None, allow_override=True), config.CRS_BNG)


# --------------------------------------------------------------------------------------
# Raster -> grid aggregation
# --------------------------------------------------------------------------------------
def test_raster_block_orientation(grid: gpd.GeoDataFrame) -> None:
    blocks = np.add.outer(np.arange(4) * 10, np.arange(4)).astype(float)  # blocks[r, c] = r*10+c
    out = aggregate_raster_mean(_raster(blocks), grid)
    expected = (grid["row"] * 10 + grid["col"]).to_numpy(dtype=float)
    np.testing.assert_allclose(out.reindex(grid["cell_id"]).to_numpy(), expected)


def test_raster_misaligned_extent_raises(grid: gpd.GeoDataFrame) -> None:
    with pytest.raises(ValueError, match="origin"):
        aggregate_raster_mean(_raster(np.zeros((4, 4)), x_shift=12.5), grid)


def test_class_fraction(grid: gpd.GeoDataFrame) -> None:
    da = _raster(np.zeros((4, 4)))
    arr = da.to_numpy().copy()
    arr[10:15, 20:25] = 50  # 25 of the 100 pixels in block (row 1, col 2)
    da = da.copy(data=arr)
    out = aggregate_raster_class_fraction(da, grid, [50]).reindex(grid["cell_id"])
    target = (grid["row"] == 1) & (grid["col"] == 2)
    assert out[target.to_numpy()].iloc[0] == 0.25
    assert (out[~target.to_numpy()] == 0).all()


# --------------------------------------------------------------------------------------
# Metric vector maths
# --------------------------------------------------------------------------------------
def test_distance_to_greenspace_known_values(grid: gpd.GeoDataFrame) -> None:
    # Cell E0320000_N0670000 has centroid (320125, 670125); park edge is 300 m east of it.
    park = gpd.GeoDataFrame(
        {"park_id": ["P0001"], "name": ["Test Park"]},
        geometry=[box(320_425, 670_075, 320_525, 670_175)],
        crs=config.CRS_BNG,
    )
    out = distance_to_greenspace(grid, park)
    assert out.loc["E0320000_N0670000", "dist_park_m"] == pytest.approx(300.0, abs=1e-6)
    assert out.loc["E0320000_N0670000", "nearest_park_name"] == "Test Park"

    inside = gpd.GeoDataFrame(
        {"park_id": ["P0001"], "name": ["Inside"]},
        geometry=[box(320_575, 670_075, 320_675, 670_175)],  # contains centroid (320625, 670125)
        crs=config.CRS_BNG,
    )
    assert distance_to_greenspace(grid, inside).loc["E0320500_N0670000", "dist_park_m"] == 0.0


def test_distance_to_greenspace_rejects_wgs84(grid: gpd.GeoDataFrame) -> None:
    park = gpd.GeoDataFrame(
        {"park_id": ["P0001"], "name": ["x"]}, geometry=[box(-3.2, 55.9, -3.19, 55.91)], crs=4326
    )
    with pytest.raises(ValueError):
        distance_to_greenspace(grid, park)


def test_dissolve_then_filter() -> None:
    # Two touching 0.3 ha polygons merge to 0.6 ha (kept); an isolated 0.4 ha polygon is dropped.
    src = gpd.GeoDataFrame(
        {"name": ["A", "B", "C"]},
        geometry=[
            box(325_000, 670_000, 325_050, 670_060),
            box(325_050, 670_000, 325_100, 670_060),
            box(326_000, 670_000, 326_040, 670_100),
        ],
        crs=config.CRS_BNG,
    )
    parks = dissolve_greenspace(src)
    assert len(parks) == 1
    assert parks["area_ha"].iloc[0] == pytest.approx(0.6, abs=0.01)


def test_area_fraction_unions_overlaps(grid: gpd.GeoDataFrame) -> None:
    footprint = box(X0, Y0, X0 + 200, Y0 + 250)  # 80 % of cell E0320000_N0670000
    buildings = gpd.GeoDataFrame(geometry=[footprint, footprint], crs=config.CRS_BNG)
    out = area_fraction(grid, buildings)
    assert out["E0320000_N0670000"] == pytest.approx(0.8)
    assert out.drop("E0320000_N0670000").eq(0).all()


# --------------------------------------------------------------------------------------
# Web boundary: the only 27700 -> 4326 conversion
# --------------------------------------------------------------------------------------
def test_to_web_outputs_wgs84_in_edinburgh(grid: gpd.GeoDataFrame) -> None:
    web = to_web(grid)
    assert web.crs is not None and pyproj.CRS(config.CRS_WGS84).equals(web.crs)
    minx, miny, maxx, maxy = web.total_bounds
    assert minx >= -3.6 and maxx <= -2.9
    assert miny >= 55.75 and maxy <= 56.05
    coords = shapely.get_coordinates(np.asarray(web.geometry))
    assert np.allclose(coords, np.round(coords, 5), atol=1e-9)  # <= 5 decimal places


def test_to_web_round_trip_stays_within_metres(grid: gpd.GeoDataFrame) -> None:
    back = to_web(grid).to_crs(config.CRS_BNG)
    # 5 dp of a degree is ~1 m, so cell areas stay within a percent of 62,500 m^2.
    assert back.geometry.area.to_numpy() == pytest.approx(CELL**2, rel=0.02)
    offsets = np.hypot(
        back.geometry.centroid.x.to_numpy() - grid["cx_m"].to_numpy(),
        back.geometry.centroid.y.to_numpy() - grid["cy_m"].to_numpy(),
    )
    assert offsets.max() < 2.0


def test_to_web_rejects_wgs84_input(grid: gpd.GeoDataFrame) -> None:
    with pytest.raises(ValueError):
        to_web(grid.to_crs(config.CRS_WGS84))


# --------------------------------------------------------------------------------------
# Cached pipeline artefacts (run with ``-m data`` once Tickets 1-3 have produced them)
# --------------------------------------------------------------------------------------
PROCESSED_BNG = ["grid_features", "grid_scored", "top_zones"]
WEB_LAYERS = ["grid_web", "parks_web", "corridors_web", "zones_web"]


def _read(path) -> gpd.GeoDataFrame:
    if not path.exists():
        pytest.skip(f"{path.name} not found; run the pipeline first")
    return gpd.read_parquet(path)


@pytest.mark.data
@pytest.mark.parametrize("name", PROCESSED_BNG)
def test_processed_layers_are_bng(name: str) -> None:
    gdf = _read(config.DATA_PROCESSED_DIR / f"{name}.parquet")
    require_crs(gdf, config.CRS_BNG)
    assert gdf.crs is not None and gdf.crs.to_epsg() == 27700
    # Cells are 250 m x 250 m squares in metres.
    b = gdf.geometry.bounds
    assert (b["maxx"] - b["minx"]).to_numpy() == pytest.approx(CELL, abs=1e-6)
    assert (b["maxy"] - b["miny"]).to_numpy() == pytest.approx(CELL, abs=1e-6)
    assert gdf.geometry.area.to_numpy() == pytest.approx(CELL**2, abs=1e-6)


@pytest.mark.data
def test_processed_grid_contract() -> None:
    gdf = _read(config.DATA_PROCESSED_DIR / "grid_features.parquet")
    assert validate_grid_features(gdf) == []
    assert (gdf["dist_park_m"] >= 0).all()


@pytest.mark.data
def test_processed_support_layers_are_bng() -> None:
    for name in ("greenspace_dissolved", "cell_main_streets"):
        require_crs(_read(config.DATA_PROCESSED_DIR / f"{name}.parquet"), config.CRS_BNG)


@pytest.mark.data
@pytest.mark.parametrize("name", WEB_LAYERS)
def test_web_layers_are_wgs84(name: str) -> None:
    gdf = _read(config.WEB_DIR / f"{name}.parquet")
    require_crs(gdf, config.CRS_WGS84)
    assert gdf.crs is not None and gdf.crs.to_epsg() == 4326
    minx, miny, maxx, maxy = gdf.total_bounds
    assert minx >= -3.6 and maxx <= -2.9
    assert miny >= 55.75 and maxy <= 56.05


@pytest.mark.data
def test_web_directory_contains_only_wgs84_layers() -> None:
    paths = sorted(config.WEB_DIR.glob("*.parquet"))
    if not paths:
        pytest.skip("no web layers found; run the scoring step first")
    for path in paths:
        require_crs(gpd.read_parquet(path), config.CRS_WGS84)
