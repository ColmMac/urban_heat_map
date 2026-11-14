# Edinburgh UHI & Green Space Optimizer — Build Tickets

> Five sequential, self-contained tickets. Hand **one ticket at a time** to the coding model along with [`docs/ARCHITECTURE.md`](ARCHITECTURE.md). ARCHITECTURE.md is authoritative for names, schemas, CRS rules and formulas; this file says *what to build in which order* and *how to prove it works*.

## How to use this file

**Handoff prompt template** (paste for each ticket):

```text
You are implementing Ticket <N> of the Edinburgh UHI project.
Read docs/ARCHITECTURE.md in full, then docs/TASKS.md → Ticket <N>.
Rules:
- Only create/modify the files listed under "Files" for this ticket.
- Use exactly the file paths, column names, constants and CRS rules from ARCHITECTURE.md. Do not rename anything.
- Use `uv add` for dependencies and `uv run` for every command. Never use pip.
- Do not implement later tickets. Do not "improve" the scoring maths.
- Finish by running every command under "Verification" and pasting the output.
- If something in the spec is impossible or contradictory, stop and explain instead of guessing.
```

**Definition of done (every ticket):**
- [ ] All acceptance criteria ticked.
- [ ] `uv run ruff check .` and `uv run ruff format --check .` pass with zero findings.
- [ ] `uv run pytest` passes. **Tickets 1–3 only:** pytest exit code 5 ("no tests collected") is acceptable, so verify with `uv run pytest; test $? -eq 0 -o $? -eq 5`. Ticket 4 adds the full suite.
- [ ] No `print()` in library code (use `logging`). No hard-coded absolute paths. No `to_crs` outside the three call sites in ARCHITECTURE §4.2.
- [ ] Public functions have type hints and a docstring stating the expected CRS.

> **Prefer test-first?** Move each block of Ticket 4's test list into the ticket that builds the code under test. The tickets are written so that works without changes.

---

## Ticket 1 — Project config & data ingestion

**Goal:** One command fetches every raw input for Edinburgh and caches it under `data/raw/`, reproducibly and idempotently.

**Depends on:** nothing (repo already initialised with uv, Python 3.12).

**Files:**
- `src/edinburgh_uhi/config.py` (create/replace)
- `src/edinburgh_uhi/ingest.py` (create/replace)
- `src/edinburgh_uhi/__init__.py` (ensure exists; expose `__version__ = "1.0.0"`)
- `pyproject.toml` (add the tool config from ARCHITECTURE §9; ensure a `[build-system]` exists)
- `.gitignore` (add the lines from ARCHITECTURE §2)
- `data/raw/.gitkeep`, `data/processed/.gitkeep`, `data/processed/web/.gitkeep`

**Dependencies:**

```bash
uv add geopandas shapely pyproj pyarrow numpy pandas xarray rioxarray rasterio \
       osmnx pystac-client planetary-computer odc-stac odc-geo requests
uv add --dev pytest ruff
```

Constraints to respect when the lock resolves: `osmnx>=2.0`, `shapely>=2.0`, `geopandas>=1.0`. If `uv add` picks older versions, add the lower bounds explicitly.

**Inputs:** network only (Nominatim, Overpass, Planetary Computer STAC, optional Open-Meteo).

**Outputs (exact paths, see ARCHITECTURE §2):**
- `data/raw/edinburgh_boundary.parquet`
- `data/raw/osm_buildings.parquet`, `osm_greenspace.parquet`, `osm_streets.parquet`, `osm_places.parquet`, `osm_exclusions.parquet`
- `data/raw/worldcover_2021_27700_10m.tif`
- `data/raw/landsat_summer_lst_27700_25m.tif`
- `data/raw/lst_scene_manifest.json`

**Implementation notes:**
1. `config.py`: implement every constant in ARCHITECTURE §14, the frozen `@dataclass Weights` with validation in `__post_init__`, and the pure helper `snap_bounds(bounds, cell_m)`. Paths derive from `PROJECT_ROOT = Path(__file__).resolve().parents[2]`. No heavy imports (no geopandas/xarray) in `config.py`.
2. `ingest.py` public API:

   ```python
   def fetch_boundary(force: bool = False) -> Path
   def fetch_osm_layers(force: bool = False) -> dict[str, Path]   # keys: buildings, greenspace, streets, places, exclusions
   def fetch_worldcover(force: bool = False) -> Path
   def fetch_lst(force: bool = False, refresh_manifest: bool = False) -> tuple[Path, Path]  # (tif, manifest)
   def main(argv: list[str] | None = None) -> int                 # argparse: --step, --force, --refresh-manifest
   ```

   Plus private helpers: `_configure_osmnx()`, `_retry(fn, attempts=3)`, `_atomic_write_parquet(gdf, path)`, `_atomic_write_raster(da, path)`, `_flatten_osm(gdf, keep_cols)`, `_analysis_geobox(boundary_bng, res_m)`, `_qa_valid_mask(qa, bits)`, `_annotate_air_temp(dates)`.
3. Follow ARCHITECTURE §3.2 exactly for Landsat: per-year summer search → T1 filter → manifest → pinned reload via `ids=` → `odc.stac.load` on the snapped 25 m BNG GeoBox (bilinear for `lwir11`, nearest for `qa_pixel`, `groupby="solar_day"`, `patch_url=planetary_computer.sign`) → QA mask → scale to °C → per-scene clear-fraction filter → per-scene anomaly → median composite → 3-band GeoTIFF with band descriptions.
4. Raise a custom `InsufficientScenesError(RuntimeError)` when fewer than `LST_MIN_SCENES` scenes survive.
5. After the first successful boundary fetch, log `"Pin BOUNDARY_OSM_ID = 'R<id>' in config.py"` at WARNING level if `BOUNDARY_OSM_ID is None`.
6. Add a `validate_raw_outputs() -> list[str]` function that returns a list of human-readable problems (empty = OK): files exist, CRS as specified, boundary area range, non-empty layers, geometry types, raster CRS = 27700, raster origin on a 250 m multiple, raster resolution exact, `n_obs` max ≥ `LST_MIN_SCENES`. Wire it to `--step validate`.

**Acceptance criteria:**
- [ ] `uv run python -m edinburgh_uhi.ingest --step all` completes from a clean `data/raw/` and writes all 9 outputs.
- [ ] A second run with no flags finishes in seconds and logs `cached:` for every output (no network calls for cached steps).
- [ ] `--force` re-downloads; `--refresh-manifest` re-runs the STAC search; without it, an existing manifest's IDs are reused.
- [ ] Every vector output is EPSG:4326 and contains only the columns listed in ARCHITECTURE §3.4.2/§3.4.3, all string-typed except `geometry`.
- [ ] `osm_buildings` / `osm_greenspace` / `osm_exclusions` contain only Polygon/MultiPolygon. `osm_streets` contains only LineString. `osm_places` contains only Point.
- [ ] Both GeoTIFFs: CRS EPSG:27700; `transform.c % 250 == 0` and `transform.f % 250 == 0`; pixel size exactly 25 m (LST) / 10 m (WorldCover); width and height are multiples of 10 / 25 respectively.
- [ ] LST band 2 (`lst_c`) values inside the boundary are physically plausible for an Edinburgh summer daytime surface (sanity range 5–50 °C; log P1/P50/P99).
- [ ] The manifest contains no signed URLs or SAS tokens (no `?st=`/`sig=` substrings).
- [ ] `uv run python -m edinburgh_uhi.ingest --step validate` prints `OK` and exits 0.
- [ ] `import edinburgh_uhi.config` takes < 0.2 s (`uv run python -X importtime -c "import edinburgh_uhi.config"`).

**Verification:**

```bash
uv sync
uv run python -m edinburgh_uhi.ingest --step all
uv run python -m edinburgh_uhi.ingest --step all            # should be all "cached:"
uv run python -m edinburgh_uhi.ingest --step validate
uv run python -c "import geopandas as gpd; b=gpd.read_parquet('data/raw/edinburgh_boundary.parquet'); print(b.crs, round(b.to_crs(27700).area.iloc[0]/1e6,1),'km2')"
uv run python -c "import rioxarray as rxr; d=rxr.open_rasterio('data/raw/landsat_summer_lst_27700_25m.tif'); print(d.rio.crs, d.rio.resolution(), d.rio.bounds(), d.attrs.get('long_name'))"
uv run ruff check . && uv run ruff format --check .
uv run pytest; test $? -eq 0 -o $? -eq 5
```

**Out of scope:** grid building, scoring, tests beyond what's needed to import, the app.

---

## Ticket 2 — 250 m BNG grid & feature engineering

**Goal:** Turn the raw layers into one analysis-ready GeoParquet with one row per 250 m cell and every feature from ARCHITECTURE §5.3.

**Depends on:** Ticket 1 outputs in `data/raw/`.

**Files:**
- `src/edinburgh_uhi/spatial_grid.py` (create/replace)

**Dependencies:** none new (already added in Ticket 1).

**Inputs:** all files in `data/raw/` listed in Ticket 1.

**Outputs (EPSG:27700):**
- `data/processed/grid_features.parquet` (schema ARCHITECTURE §5.4)
- `data/processed/greenspace_dissolved.parquet` (`park_id, name, area_ha, geometry`)
- `data/processed/cell_main_streets.parquet` (`cell_id, main_street, geometry`)

**Implementation notes — public API (pure functions take and return GeoDataFrames; only `build_grid_features` and `main` do I/O):**

```python
def require_crs(gdf: gpd.GeoDataFrame, expected: str) -> None
def make_grid(boundary_bng: gpd.GeoDataFrame, cell_m: int = GRID_CELL_M,
              min_inside_frac: float = GRID_MIN_INSIDE_FRAC) -> gpd.GeoDataFrame
def aggregate_raster_mean(da: xr.DataArray, grid: gpd.GeoDataFrame, cell_m: int = GRID_CELL_M) -> pd.Series   # index = cell_id
def aggregate_raster_valid_frac(da, grid, cell_m=GRID_CELL_M) -> pd.Series
def aggregate_raster_class_fraction(da, grid, classes: Iterable[int], cell_m=GRID_CELL_M) -> pd.Series
def area_fraction(grid, polygons_bng) -> pd.Series                       # building_frac, green_frac_osm, excl_frac
def dissolve_greenspace(greenspace_bng, min_area_m2=GREEN_MIN_AREA_M2, merge_tol_m=GREEN_MERGE_TOL_M) -> gpd.GeoDataFrame
def distance_to_greenspace(grid, parks_bng) -> pd.DataFrame              # dist_park_m, nearest_park_name
def street_features(grid, streets_bng) -> tuple[pd.DataFrame, gpd.GeoDataFrame]  # (per-cell features, cell_main_streets)
def assign_neighbourhood(grid, places_bng, max_dist_m=NEIGHBOURHOOD_MAX_DIST_M) -> pd.Series
def recenter_anomaly(grid) -> pd.Series                                   # lst_anomaly_c
def to_web(gdf: gpd.GeoDataFrame, precision: int = WEB_PRECISION_DP) -> gpd.GeoDataFrame
def load_raw_vectors() -> dict[str, gpd.GeoDataFrame]                    # reads data/raw/*.parquet and .to_crs(CRS_BNG)
def build_grid_features() -> dict[str, Path]
def main(argv: list[str] | None = None) -> int
```

Key rules:
1. `make_grid` uses `config.snap_bounds` and vectorised `shapely.box`; `cell_id = f"E{e:07d}_N{n:07d}"` of the lower-left corner; `row` 0 = north.
2. Raster aggregation is **block reduction** with `coarsen(boundary="exact")` plus strict extent/resolution asserts (ARCHITECTURE §5.2). Map block `[r, c]` → the cell with that `row`/`col`. **Do not** use `rasterstats` or point sampling.
3. `area_fraction`: `sjoin(predicate="intersects")` → vectorised `shapely.intersection(cell_geom, poly_geom)` → per-cell `shapely.union_all` (so overlapping/duplicate OSM polygons aren't double-counted) → `.area / cell_area`, clipped to [0, 1], 0 for cells with no hits.
4. `dissolve_greenspace`: `make_valid` → buffer(+tol) → `unary_union` → explode → buffer(−tol) → area filter → `park_id` by descending area → name via largest-overlap source polygon with a non-null name.
5. `distance_to_greenspace`: `sjoin_nearest` from cell **centroids** to parks with `distance_col="dist_park_m"`. A centroid inside a park → 0. Drop duplicate matches (ties) keeping the first by `park_id`.
6. `street_features`: clip streets to cells via `sjoin` + `shapely.intersection`, recompute lengths in BNG, choose `main_street` by max total named length (tie-break alphabetical), and build `cell_main_streets` as a MultiLineString per cell.
7. Anomaly re-centring and `is_urban` exactly as ARCHITECTURE §5.3.
8. `to_web` is the **only** 27700 → 4326 conversion in the codebase. It reprojects, then rounds coordinates with `shapely.set_precision(geom, 10**-precision)`. Validate the result's bounds fall within lon ∈ [−3.6, −2.9], lat ∈ [55.75, 56.05] for this project (log a warning otherwise).
9. Add `validate_grid_features(gdf) -> list[str]` (schema/dtypes/ranges/uniqueness) and run it at the end of `build_grid_features`; non-empty → raise.

**Acceptance criteria:**
- [ ] `uv run python -m edinburgh_uhi.spatial_grid` writes the 3 outputs; all have CRS EPSG:27700.
- [ ] `grid_features.parquet` has exactly the columns and dtypes in ARCHITECTURE §5.4; `cell_id` unique; every geometry is a 250 × 250 m square (area 62,500 ± 1e-6 m²) with corners on multiples of 250.
- [ ] All `*_frac` columns ∈ [0, 1]; `dist_park_m ≥ 0`; `n_obs_median ≤ number of used scenes in the manifest`.
- [ ] Log line reporting total cells, urban cells, eligible-looking cells (urban & valid LST), median `lst_anomaly_c` over urban cells (≈ 0 by construction, |x| < 0.05).
- [ ] `greenspace_dissolved` has no polygon < 5,000 m²; `area_ha = area/10,000`; no two parks intersect.
- [ ] Spot-check sanity: the cell containing the centroid of the largest park has `dist_park_m == 0` and `green_frac_osm > 0.5`.
- [ ] Re-running produces byte-identical `grid_features.parquet` content (compare with `gdf.equals` on a reload; no randomness, stable sort by `cell_id`).
- [ ] Runtime is a few minutes at most on a laptop. Log per-stage timings.

**Verification:**

```bash
uv run python -m edinburgh_uhi.spatial_grid
uv run python -c "
import geopandas as gpd
g = gpd.read_parquet('data/processed/grid_features.parquet')
print(g.crs, len(g), g.is_urban.sum(), g.cell_id.is_unique)
print(g[['lst_anomaly_c','impervious_frac','dist_park_m','building_frac']].describe().T)
"
uv run ruff check . && uv run ruff format --check .
uv run pytest; test $? -eq 0 -o $? -eq 5
```

**Out of scope:** scoring, eligibility, top-K, the web export of scored layers (only `to_web` itself is built here).

---

## Ticket 3 — Greening Priority Score & top-K micro-zones

**Goal:** Score every cell, gate by eligibility, pick 15 spatially distinct zones with a named corridor and intervention, and write analysis + web artifacts with full provenance.

**Depends on:** Ticket 2 outputs.

**Files:**
- `src/edinburgh_uhi/scoring.py` (create/replace)

**Dependencies:** none new.

**Inputs:**
- `data/processed/grid_features.parquet`, `greenspace_dissolved.parquet`, `cell_main_streets.parquet`
- `data/raw/lst_scene_manifest.json` (for metadata only; must not fail if absent. Record `null`s.)

**Outputs:**
- `data/processed/grid_scored.parquet`, `top_zones.parquet` (27700)
- `data/processed/scoring_metadata.json`
- `data/processed/web/grid_web.parquet`, `parks_web.parquet`, `corridors_web.parquet`, `zones_web.parquet` (4326)

**Implementation notes — public API (everything except `run`/`main`/`export_web_layers` is pure and I/O-free):**

```python
@dataclass(frozen=True)
class NormBounds:
    thermal_lo: float; thermal_hi: float
    dist_lo_m: float; dist_hi_m: float
    imperv_lo: float; imperv_hi: float

def linear_clip(x: ArrayLike, lo: float, hi: float) -> np.ndarray            # ValueError if hi <= lo; NaN in → NaN out
def eligibility(grid: pd.DataFrame) -> tuple[pd.Series, pd.Series]          # (eligible: bool, exclusion_reason: str|None), first failing rule wins
def derive_bounds(grid: pd.DataFrame, eligible: pd.Series) -> NormBounds    # thermal_hi = P95 over eligible; raises if <= THERMAL_MIN_HI_C or < 30 eligible cells
def component_scores(grid: pd.DataFrame, b: NormBounds) -> pd.DataFrame     # s_thermal, s_distance, s_impervious (NaN thermal → 0, cell is ineligible anyway)
def priority(components: pd.DataFrame, w: Weights) -> pd.DataFrame          # c_thermal, c_distance, c_impervious, priority_score
def classify_intervention(grid: pd.DataFrame) -> pd.DataFrame               # intervention, recommendation (None for ineligible)
def select_top_zones(scored: pd.DataFrame, k: int = TOPK_K,
                     min_sep_m: float = TOPK_MIN_SEPARATION_M) -> pd.DataFrame  # needs cell_id, cx_m, cy_m, priority_score, eligible; adds zone_rank
def hotspot_extent(scored: gpd.GeoDataFrame, zones: pd.DataFrame,
                   rel: float = HOTSPOT_REL_THRESHOLD) -> pd.Series         # 1..9
def weight_sensitivity(components: pd.DataFrame, scored: pd.DataFrame, base: Weights,
                       delta: float = SENSITIVITY_DELTA, k: int = TOPK_K) -> dict
def compute_kpis(scored: pd.DataFrame, zones: pd.DataFrame) -> dict
def score_grid(grid: gpd.GeoDataFrame, w: Weights = WEIGHTS) -> tuple[gpd.GeoDataFrame, NormBounds]
def export_web_layers(scored, zones, parks, main_streets) -> dict[str, Path]  # uses spatial_grid.to_web
def run(k: int = TOPK_K, min_sep_m: float = TOPK_MIN_SEPARATION_M) -> dict[str, Path]
def main(argv: list[str] | None = None) -> int                              # --k, --min-sep
```

Key rules (formulas are in ARCHITECTURE §6. Implement them literally):
0. Import `spatial_grid` **inside** `score_grid` / `export_web_layers` only (lazy import), so `import edinburgh_uhi.scoring` doesn't pull in xarray/rioxarray. The app imports this module. `classify_intervention` expects an `eligible` column to be present.
1. Eligibility rule order and reason strings exactly as ARCHITECTURE §6.1.
2. Bounds: thermal `[0, P95]`, distance `[200, 600]` m, impervious `[0.20, 0.90]`.
3. `P = 100·(0.5·S_T + 0.25·S_D + 0.25·S_I)`; contributions sum to `priority_score` within 1e-6.
4. Top-K: greedy NMS sorted by `(priority_score desc, cell_id asc)`, 500 m centroid separation, stop at K, warn if fewer.
5. `rank_overall`: dense 1..N over eligible cells by the same sort; `<NA>` for ineligible.
6. KPIs (ARCHITECTURE §6.8):
   - `peak_zone_anomaly_c` = max `lst_anomaly_c` among zones
   - `mean_top_zone_anomaly_c` = mean over zones
   - `pct_urban_cells_beyond_doorstep` = 100 × share of `is_urban` cells with `dist_park_m > DIST_LO_M`
   - `top_zone_area_ha` = (number of zones actually selected) × 6.25
7. `inputs_sha256`: SHA-256 of each input file's bytes.
8. Web export per ARCHITECTURE §6.7, including `lat`/`lon` columns on `zones_web` and the size budget check (log total MB; warn > 5 MB).

**Acceptance criteria:**
- [ ] `uv run python -m edinburgh_uhi.scoring` writes all 7 outputs.
- [ ] `priority_score ∈ [0, 100]` for all cells; `|c_thermal + c_distance + c_impervious − priority_score| < 1e-6`.
- [ ] `top_zones.parquet` has 10–15 rows (15 expected), `zone_rank` = 1..n, all `eligible`, pairwise centroid distances ≥ 500 m, sorted by rank, and no zone has `excl_frac ≥ 0.5` (Edinburgh Airport is **not** in the list).
- [ ] Every zone has a non-empty `recommendation`; `street_trees` zones have a non-null `main_street`.
- [ ] `scoring_metadata.json` contains every key shown in ARCHITECTURE §6.8 with real values; `bounds.thermal[1] > 0.5`.
- [ ] Web layers are EPSG:4326 and within the Edinburgh lon/lat sanity box; `web/` total < 5 MB.
- [ ] Running twice gives identical `top_zones` (`cell_id` order) and identical `priority_score` values.
- [ ] Log a human-readable top-15 table (rank, neighbourhood, main_street, intervention, score, ΔT, dist).

**Verification:**

```bash
uv run python -m edinburgh_uhi.scoring
uv run python -c "
import geopandas as gpd, json
z = gpd.read_parquet('data/processed/top_zones.parquet')
print(z[['zone_rank','neighbourhood','main_street','intervention','priority_score','lst_anomaly_c','dist_park_m']].to_string(index=False))
m = json.load(open('data/processed/scoring_metadata.json'))
print(m['bounds'], m['kpis'], m['sensitivity'])
"
du -sh data/processed/web
uv run ruff check . && uv run ruff format --check .
uv run pytest; test $? -eq 0 -o $? -eq 5
```

**Out of scope:** UI. Changing weights, bounds or rules from what ARCHITECTURE specifies (raise a question instead).

---

## Ticket 4 — Test suite: `tests/test_crs.py` & `tests/test_scoring.py` (+ CI)

**Goal:** A fast, offline, deterministic suite that pins the CRS behaviour and the scoring maths, so any regression in the pipeline's core assumptions fails CI.

**Depends on:** Tickets 1–3 code (tests use synthetic data; cached data only for `@pytest.mark.data` tests).

**Files:**
- `tests/conftest.py` (create: shared synthetic fixtures)
- `tests/test_crs.py` (create/replace)
- `tests/test_scoring.py` (create/replace)
- `.github/workflows/ci.yml` (create)

**Dependencies:** none new (`pytest`, `ruff` already dev deps). Optionally `uv add --dev pytest-cov` to add a coverage report.

**Fixtures (`conftest.py`):**
- `square_boundary_bng` → 1 km × 1 km polygon at `(320000, 670000)–(321000, 671000)` in EPSG:27700 (gives exactly 16 cells).
- `synthetic_grid` → `make_grid(square_boundary_bng)`.
- `block_raster_25m` → `xarray.DataArray` 40×40 px at 25 m over the same extent with `rio.write_crs(27700)`, where each 250 m block's value = `row*10 + col` (y descending).
- `scoring_frame` → a hand-built `pandas.DataFrame` of ~12 cells with known `lst_anomaly_c`, `dist_park_m`, `impervious_frac`, eligibility fields, `cx_m`, `cy_m`.

**`tests/test_crs.py` — required tests:**

| # | Test | Assertion |
|---|------|-----------|
| C1 | `test_crs_constants` | `config.CRS_WGS84 == "EPSG:4326"`, `config.CRS_BNG == "EPSG:27700"`; `pyproj.CRS(CRS_BNG).axis_info[0].unit_name == "metre"`; `CRS_BNG` is projected, `CRS_WGS84` geographic. |
| C2 | `test_edinburgh_projects_into_expected_bng_window` | `Transformer.from_crs(4326, 27700, always_xy=True).transform(-3.1883, 55.9533)` → E ∈ [320000, 332000], N ∈ [668000, 680000]. |
| C3 | `test_round_trip_wgs84_bng_wgs84` | Round trip of 5 Edinburgh points recovers lon/lat within 1e-7°. |
| C4 | `test_bng_distance_matches_geodesic` | Two points 1,000 m apart in BNG near Edinburgh → `pyproj.Geod(ellps="WGS84").inv` distance after transform; relative error < 0.1% (BNG point scale ≈ 0.9997 there). |
| C5 | `test_snap_bounds_multiples` | `snap_bounds((320010.4, 670249.9, 320990, 671001), 250) == (320000, 670000, 321000, 671250)`. |
| C6 | `test_make_grid_shape_and_ids` | 16 cells; areas = 62,500 (abs tol 1e-6); `cell_id` unique and equal to `E0320000_N0670000` etc.; `row` 0 is the northernmost; CRS 27700. |
| C7 | `test_make_grid_rejects_geographic_input` | Boundary in 4326 → `ValueError` (via `require_crs`). |
| C8 | `test_raster_block_orientation` | `aggregate_raster_mean(block_raster_25m, synthetic_grid)` returns `row*10+col` for every cell. This catches a north/south flip. |
| C9 | `test_raster_misaligned_extent_raises` | Shift the raster by 12.5 m → `AssertionError`/`ValueError` naming both extents. |
| C10 | `test_class_fraction` | Raster with 25% pixels == 50 in one block → `builtup_frac == 0.25` exactly for that cell. |
| C11 | `test_distance_to_greenspace_known_values` | Park = 100 m square whose edge is exactly 300 m east of a cell centroid → `dist_park_m == 300 ± 1e-6`; centroid inside park → 0. |
| C12 | `test_dissolve_then_filter` | Two touching 0.3 ha polygons are kept (merged 0.6 ha); an isolated 0.4 ha polygon is dropped. |
| C13 | `test_area_fraction_unions_overlaps` | Two identical overlapping building polygons covering 80% of a cell → `building_frac == 0.8` (not 1.0 from clipping, not 1.6 from summing). |
| C14 | `test_to_web_outputs_wgs84_in_edinburgh` | `to_web(synthetic_grid)` → CRS 4326, all bounds within lon [−3.6, −2.9], lat [55.75, 56.05], coordinate precision ≤ 5 dp. |
| C15 | `@pytest.mark.data test_processed_grid_contract` | If `data/processed/grid_features.parquet` exists: `validate_grid_features(gdf) == []`. |

> **Production-code changes in Ticket 4:** only to fix a bug a required test exposes, kept minimal and noted in the PR description. Never change a formula, bound or threshold to make a test pass.

**`tests/test_scoring.py` — required tests:**

| # | Test | Assertion |
|---|------|-----------|
| S1 | `test_linear_clip_anchors` | `linear_clip([lo, (lo+hi)/2, hi], lo, hi) == [0, 0.5, 1]`; values below lo → 0; above hi → 1. |
| S2 | `test_linear_clip_invalid_bounds` | `hi <= lo` → `ValueError`. |
| S3 | `test_linear_clip_nan_passthrough` | NaN in → NaN out. |
| S4 | `test_distance_component_anchors` | 150 m → 0, 200 m → 0, 400 m → 0.5, 600 m → 1, 2,000 m → 1. |
| S5 | `test_impervious_component_anchors` | 0.10 → 0, 0.20 → 0, 0.55 → 0.5, 0.90 → 1, 1.0 → 1. |
| S6 | `test_thermal_component_uses_p95_ceiling` | 40 eligible cells with anomalies `0, 1, …, 39` → `derive_bounds(...).thermal_hi == 37.05` (NumPy default linear percentile); a cell at 37.05 → `s_thermal == 1.0`, at 39 → 1.0 (clipped), at ≤ 0 → 0. |
| S7 | `test_derive_bounds_rejects_flat_composite` | 40 eligible cells all at 0.1 °C → `ValueError` (thermal_hi ≤ 0.5). Separately, 20 eligible cells → `ValueError` (fewer than 30). |
| S8 | `test_weights_validation` | `Weights(0.5, 0.3, 0.3)` → `ValueError`; negative weight → `ValueError`; default sums to 1. |
| S9 | `test_priority_range_and_additivity` | Random components in [0, 1] (seeded) → score ∈ [0, 100]; contributions sum to score (1e-6). |
| S10 | `test_priority_monotonic` | Raising any one component with the others fixed never lowers the score (parametrised over the 3 components). |
| S11 | `test_eligibility_reason_order` | A cell failing both `water` and `not_urban` reports `"water"` (first rule wins); each rule triggers its exact reason string. |
| S12 | `test_select_top_zones_enforces_separation` | 3×3 block of top-scoring adjacent cells + far cells → selected centroids pairwise ≥ 500 m; the best cell is rank 1. |
| S13 | `test_select_top_zones_deterministic_tiebreak` | Equal scores → lower `cell_id` wins; repeated calls give an identical result. |
| S14 | `test_select_top_zones_returns_fewer_when_exhausted` | Only 4 eligible cells → 4 rows, a warning logged (`caplog`), no padding. |
| S15 | `test_select_top_zones_ignores_ineligible` | The highest-score ineligible cell is never selected. |
| S16 | `test_intervention_rules` | `building_frac=0.4` → `green_roofs`; `building_frac=0.2, main_street_len_m=200` → `street_trees` with the street name in `recommendation`; otherwise `depave_pocket_park`; ineligible → `None`. |
| S17 | `test_weight_sensitivity_structure` | Returns 6 variants; all Jaccard values ∈ [0, 1]; perturbed weights sum to 1. |
| S18 | `test_kpis` | Known frame → `pct_urban_cells_beyond_doorstep` and `top_zone_area_ha` equal hand-computed values. |
| S19 | `@pytest.mark.data test_processed_outputs_contract` | If processed files exist: zones pairwise ≥ 500 m, `zone_rank` contiguous, metadata keys present, web layers EPSG:4326. |

**CI (`.github/workflows/ci.yml`):** on push/PR to `main`: checkout → `astral-sh/setup-uv` (latest major, with caching enabled) → `uv sync --locked` → `uv run ruff check .` → `uv run ruff format --check .` → `uv run pytest`. Python version comes from `.python-version` / `requires-python`.

**Acceptance criteria:**
- [ ] All C1–C14 and S1–S18 implemented and passing; C15/S19 pass with `-m data` once processed files exist.
- [ ] Default `uv run pytest` makes **zero** network calls (prove it by running with networking disabled, or by asserting no `requests`/`osmnx` calls via a `monkeypatch` autouse fixture that raises on socket connect).
- [ ] Full default suite runs in < 20 s.
- [ ] No test depends on another's side effects; random data uses fixed seeds.
- [ ] CI workflow is valid YAML and mirrors the local commands.

**Verification:**

```bash
uv run pytest -q
uv run pytest -q -m data          # after running Tickets 1–3
uv run pytest -q --durations=5
uv run ruff check . && uv run ruff format --check .
```

**Out of scope:** UI tests; network integration tests beyond marker scaffolding.

---

## Ticket 5 — Streamlit + Folium dashboard (`app.py`)

**Goal:** A polished, fast, single-page dashboard that tells the story in 15 seconds and deploys unchanged to Streamlit Community Cloud.

**Depends on:** Ticket 3 outputs in `data/processed/web/` and `scoring_metadata.json`. Uses `scoring.select_top_zones` for live re-weighting.

**Files:**
- `src/edinburgh_uhi/app.py` (create/replace)
- `.streamlit/config.toml` (create; content in ARCHITECTURE §7.7)

**Dependencies:**

```bash
uv add streamlit folium streamlit-folium branca
```

**Inputs:** `data/processed/web/grid_web.parquet`, `parks_web.parquet`, `corridors_web.parquet`, `zones_web.parquet`, `data/processed/scoring_metadata.json`. **Nothing else.**

**Outputs:** none on disk. A running app.

**Implementation notes (layout, layers, tooltips, KPIs and interactions are specified in ARCHITECTURE §7. Build that literally):**
1. Top of file: `sys.path` import guard (ARCHITECTURE §7.6), then `st.set_page_config(page_title="Edinburgh Heat & Greening Optimizer", page_icon="🌳", layout="wide", initial_sidebar_state="collapsed")`.
2. Allowed project imports: `edinburgh_uhi.config` (incl. `Weights`), `edinburgh_uhi.scoring` (only `select_top_zones`). Never import `ingest`, `spatial_grid`, `osmnx`, `odc`, `pystac_client` directly.
3. Structure the file as small functions: `load_layers()` (`@st.cache_data`), `load_metadata()` (`@st.cache_data`), `rerank(grid, weights, k)` (pure), `build_map(toggles, weights, k)` (`@st.cache_resource`), `render_kpis(meta)`, `render_zone_table(zones)`, `render_inspector(selected, zones, weights)`, `render_footer()`, `main()`.
4. Heat layer is a `GeoJson` choropleth (not `plugins.HeatMap`) with `LinearColormap` over `[p2, p98]` of `lst_anomaly_c`, legend added to the map.
5. `st_folium(..., returned_objects=["last_active_drawing"], key="map")`. Inspector reads `cell_id` from the returned feature's properties; default to zone #1.
6. `?demo=1` starts with the zones layer off; sidebar toggles for the three layers; "Tune the model" expander with weight sliders (auto-normalised) and K slider (10–15).
7. Missing processed files → `st.error` with the exact pipeline commands to run, then `st.stop()`. Don't crash with a traceback.
8. Footer: attributions from ARCHITECTURE §12 and a method expander summarising §6 in ≤ 8 lines (formula rendered with `st.latex`).

**Acceptance criteria:**
- [ ] `uv run streamlit run src/edinburgh_uhi/app.py` renders with no warnings in the terminal; cold load < 5 s locally after the first run.
- [ ] 4 KPI cards show values from `scoring_metadata.json` (no hard-coded numbers).
- [ ] The three layers toggle independently; basemap switch works via LayerControl.
- [ ] Hovering a heat cell shows the 6-field tooltip with formatted units (°C, m, %, /100).
- [ ] Top-K zones show numbered badges 1..K and amber corridor lines; the right-hand table matches the map numbering.
- [ ] Clicking any zone or eligible cell updates the inspector; the three bars sum to the displayed score.
- [ ] Changing weights re-ranks zones within ~1 s; restoring defaults reproduces `zones_web.parquet` exactly.
- [ ] Panning and zooming do **not** trigger a Streamlit rerun (verify: no "Running…" indicator while panning).
- [ ] `?demo=1` starts with zones hidden.
- [ ] Deleting `data/processed/web/` shows the friendly `st.error` instead of a traceback.
- [ ] The forbidden-import grep under Verification prints `imports clean`.
- [ ] `uv run python -X importtime -c "import edinburgh_uhi.scoring" 2>&1 | grep -E "rioxarray|xarray|osmnx"` returns nothing (scoring stays light for the app).
- [ ] Deployed to Streamlit Community Cloud (main file `src/edinburgh_uhi/app.py`, Python 3.12) and loads from a fresh browser session.

**Verification:**

```bash
uv run streamlit run src/edinburgh_uhi/app.py            # manual check, then:
uv run python -c "import ast,sys; src=open('src/edinburgh_uhi/app.py').read(); ast.parse(src); print('ok')"
grep -nE "^\s*(import|from)\s+.*(osmnx|odc|pystac_client|edinburgh_uhi(\.| import )(ingest|spatial_grid))" src/edinburgh_uhi/app.py || echo "imports clean"
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

**Recording checklist (LinkedIn):** follow the 15-second script in ARCHITECTURE §7.8. Open `http://localhost:8501/?demo=1`, collapse the sidebar, set browser zoom to 100%, record at 1080p/30 fps, and trim to 15 s. Put the GIF/MP4 at the top of the README.

**Out of scope:** authentication, persistence, new analysis.

---

## Ticket 6 — Mobile-first UI overhaul (`ui/` package)

**Goal:** Turn the full-bleed map dashboard into a polished product on phones and desktops (bottom sheet with peek/half states, unified palette, touch-first map) without breaking session state. Design and rules: ARCHITECTURE §7.9.

**Phases** (each ends with `uv run ruff check src tests`, `uv run pytest`, `uv run pytest -m data tests/test_app.py` and screenshots at 375 / 768 / 1440 px; widget keys `w_*`, `k_zones`, `t_*`, `map` never change):

- [x] **Phase 0 — Safety net.** Split `app.py` into `ui/theme.py`, `ui/components.py` (pure HTML/chart builders), `ui/map_view.py`, `ui/model.py`, `ui/panel.py`, `ui/state.py`. Add `tests/test_ui.py` (offline) and `tests/test_app.py` (`AppTest`, `-m data`). No visual change.
- [x] **Phase 1 — State-first.** `init_state` / `consume_map_click` / `model_from_state`; widgets use `key=` only; the panel renders top to bottom in one pass (no deferred slots). No visual change, except that a selected cell now stays selected after a weight change instead of falling back to zone #1.
- [x] **Phase 2 — Design tokens.** One `TOKENS` dict injected into the page and the map iframe; richer `config.toml` theme (Inter, radius, border, dataframe colours); hard-coded hex removed from CSS, hover-card JS and Altair; token tests in `test_ui.py`. Monochrome primary needed one widget-internals rule (ink knob on an "on" toggle).
- [x] **Phase 3 — Panel layout.** Header + HTML KPI strip + peek card + `st.segmented_control` sections (Zone / Tune / Ranking / About); `:has()`-driven bottom sheet (peek ≈ 265 px, open = 62dvh with header/KPIs collapsed); weight-mix bar, component-coloured slider thumbs, required K segmented control; HTML ranked list + CSV download; map click opens Zone. Controls use `persist_state="page"` (Streamlit 1.67) instead of the key re-assignment workaround; `test_app.py` covers weights/toggles/K surviving a section switch. Mobile eyebrow bug fixed (media query moved to the end of the stylesheet). Removed: `st.metric` KPIs, `st.dataframe` table, Altair breakdown chart, expander footer.
- [ ] **Phase 4 — Map.** Touch media queries (`hover: none`), `dvh`, `ResponsiveFit` fit-bounds macro, compact mobile legend, reduced motion; spike `feature_group_to_add` so weight changes stop reloading the map. From Phase 3: the map's bottom controls use a fixed 272 px offset for the peek sheet; with a section open (62dvh) the sheet still covers the city, which `ResponsiveFit` should frame around.
- [ ] **Phase 5 — Polish & QA.** Focus rings, contrast, iOS Safari / Android Chrome checks, Streamlit Cloud redeploy.

**Out of scope:** new analysis, changes to scoring maths or data contracts.

---

## Progress tracker

| Ticket | Module(s) | Status | Notes |
|--------|-----------|--------|-------|
| 1 | `config.py`, `ingest.py` | [x] | code written; verification commands not yet run. Pin `BOUNDARY_OSM_ID` and commit `lst_scene_manifest.json` after the first run |
| 2 | `spatial_grid.py` | [x] | 4,443 cells (1,748 urban); outputs cached in `data/processed/` |
| 3 | `scoring.py` | [x] | 1,444 eligible cells; 15 zones; web/ 1.7 MB; commit `data/processed/` |
| 4 | `tests/*`, CI | [x] | 54 offline tests + 10 `-m data` tests pass; `.github/workflows/ci.yml` created |
| 5 | `app.py`, `.streamlit/config.toml` | [x] | dashboard built and smoke-tested; Streamlit Cloud deploy + recording still manual |
| 6 | `app.py`, `ui/*`, `tests/test_ui.py`, `tests/test_app.py` | [ ] | Phases 0–3 done (module split, state-first, design tokens, bottom-sheet panel); Phases 4–5 open |
