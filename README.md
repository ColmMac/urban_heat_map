# Edinburgh Urban Heat Island (UHI) & Green Space Optimizer

An end-to-end geospatial data engineering and spatial optimization pipeline that identifies priority micro-zones across Edinburgh, Scotland for targeted urban greening interventions (street tree planting, green roofs, and pocket parks).

Built with Python 3.12, GeoPandas, Rasterio, OSMnx, Microsoft Planetary Computer STAC, and an interactive Streamlit + Folium dark-mode geospatial studio.

---

## Key Edinburgh Findings

Across **4,443 metric grid cells (250 m × 250 m)** covering the City of Edinburgh boundary (**1,444 eligible urban cells** after filtering water, airports, and protected sites):

* **Peak Surface Heat Anomaly:** **+4.43 °C** above the urban median (95th-percentile normalization ceiling at **+2.60 °C**).
* **Maximum Green Space Gap:** Up to **1,508 m** from the nearest contiguous green space ($\ge 0.5\text{ ha}$).
* **Top-Ranked Priority Zones:** Industrial/commercial corridors in **Bankhead** (*Bankhead Drive*, *Bankhead Crossway North*) and dense stone-built historic canyons (*Nicolson Street*, *Lothian Road*, *Cowgate*) consistently rank highest across weight sensitivity checks (mean Top-15 Jaccard stability of **0.87** under $\pm 0.10$ weight perturbations).

---

## Coordinate Reference System (CRS) Strategy

Strict CRS separation is enforced throughout the pipeline and verified by automated tests (`tests/test_crs.py`):

| Pipeline Stage | CRS | Purpose |
| :--- | :--- | :--- |
| **Raw Boundary & STAC Queries** | `EPSG:4326` (WGS84) | Standard geographic bounding boxes for OpenStreetMap (`OSMnx`) and Planetary Computer STAC catalog queries. |
| **Spatial Grid, Joins & Scoring** | **`EPSG:27700` (British National Grid)** | All metric 250 m × 250 m grid construction, polygon area fractions (buildings, impervious surfaces, exclusions), raster zonal statistics, Euclidean distances to cooling parks, and 500 m non-maximum suppression. |
| **Web Export & Interactive Map** | `EPSG:4326` (WGS84) | Precomputed, geometry-simplified GeoParquet layers (`data/processed/web/*.parquet`, ~1.7 MB total) for zero-latency Folium rendering and Streamlit Cloud deployment. |

---

## Greening Priority Scoring Methodology

Each eligible urban grid cell $i$ receives a composite **Greening Priority Score** $S_i \in [0, 100]$:

$$S_i = 100 \times \left( w_{\text{thermal}} \cdot \tilde{T}_i + w_{\text{distance}} \cdot \tilde{D}_i + w_{\text{impervious}} \cdot \tilde{I}_i \right)$$

1. **Eligibility Filtering:** Excludes non-urban cells, invalid/cloud-masked Land Surface Temperature (LST) cells, water bodies, aerodromes (Edinburgh Airport), and restricted/excluded land (`excl_frac`).
2. **Feature Normalization ($\tilde{T}_i, \tilde{D}_i, \tilde{I}_i \in [0, 1]$):**
   * **Thermal Anomaly ($\tilde{T}_i$, default weight `0.50`):** Summer Landsat 8/9 LST anomaly relative to the urban median, capped at the 95th percentile (`2.60 °C`).
   * **Park Access Gap ($\tilde{D}_i$, default weight `0.25`):** Euclidean distance in meters (`EPSG:27700`) to the nearest dissolved green space $\ge 5,000\text{ m}^2$.
   * **Impervious Surface Density ($\tilde{I}_i$, default weight `0.25`):** Combined building footprint and sealed surface fraction within the 250 m cell.
3. **Spatial De-Clustering (NMS):** Greedy **Non-Maximum Suppression** with a **500 m minimum centroid separation** selects the Top $K$ (default $K = 15$) distinct intervention zones and assigns context-aware recommendations (*Street trees*, *Green roofs*, or

Setup & Usage (uv)

This project uses uv for deterministic Python 3.12 dependency management.
1. Install Dependencies
Bash

git clone [https://github.com/ColmMac/urban_heat_map.git](https://github.com/ColmMac/urban_heat_map.git)
cd urban_heat_map
uv sync

2. Launch the Interactive Dashboard

Precomputed EPSG:4326 web layers are included in data/processed/web/, so the dashboard runs immediately without downloading raw satellite rasters:
Bash

uv run streamlit run src/edinburgh_uhi/app.py

(Tip: Append ?demo=1 to http://localhost:8501/?demo=1 to launch with the Top-K zones layer toggled off for presentation walkthroughs.)
3. Re-Run the Full Geospatial Pipeline (Optional)

To re-ingest raw OpenStreetMap vectors and Planetary Computer Landsat 8/9 + ESA WorldCover rasters from scratch:
Bash

uv run python -m edinburgh_uhi.ingest --step all
uv run python -m edinburgh_uhi.ingest --step validate
uv run python -m edinburgh_uhi.spatial_grid
uv run python -m edinburgh_uhi.scoring

4. Run Tests & Linting
Bash

# Run the fast offline unit test suite (54 tests)
uv run pytest

# Include cached-data integration tests against data/processed/ (64 tests total)
uv run pytest -m data

# Check code style and formatting
uv run ruff check .
uv run ruff format --check .

Data Attributions

    Vector Boundaries, Buildings, Parks & Streets: © OpenStreetMap contributors (via OSMnx).

    Land Surface Temperature (LST): USGS/NASA Landsat 8 & 9 Collection 2 Level-2 (via Microsoft Planetary Computer STAC).

    Land Cover: ESA WorldCover 10 m 2021 (via Microsoft Planetary Computer STAC).


<ElicitationsGroup message="Ready to wrap up your GitHub repo and LinkedIn launch?">
  <Elicitation label="Draft a punchy LinkedIn post for the Edinburgh UHI demo" query="Write an authentic, punchy LinkedIn post showcasing my Edinburgh Urban Heat Island & Green Space Optimizer project, highlighting the real Edinburgh findings (Bankhead, Nicolson Street, Cowgate) and the geospatial Python stack."/>
  <Elicitation label="Give me the git commands to commit README.md" query="Give me the exact git commands to add and commit README.md to main in C:\ip\urban_heat_map."/>
</ElicitationsGroup>
