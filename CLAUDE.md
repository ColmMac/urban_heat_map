\# CLAUDE.md — Edinburgh UHI \& Green Space Optimizer



\## Environment \& Tooling

\- \*\*Package Manager:\*\* `uv` only (Python 3.12). Never use `pip` or `conda`.

\- \*\*Add packages:\*\* `uv add <package>` (or `uv add --dev <package>` for test/lint tools).

\- \*\*Run modules:\*\* `uv run python -m edinburgh\_uhi.<module>`

\- \*\*Run dashboard:\*\* `uv run streamlit run src/edinburgh\_uhi/app.py`

\- \*\*Verify code:\*\* Run `uv run ruff check .` and `uv run pytest` before marking any ticket complete.



\## Repository Layout

\- `docs/ARCHITECTURE.md`: Master technical blueprint (source of truth).

\- `docs/TASKS.md`: Sequential build tickets. Update checkboxes (`\[x]`) as tickets finish.

\- `src/edinburgh\_uhi/`: Core package (`config.py`, `ingest.py`, `spatial\_grid.py`, `scoring.py`, `app.py`).

\- `data/raw/` \& `data/processed/`: Git-ignored GeoParquet/GeoTIFF caches. Always cache intermediate spatial layers so downstream runs don't re-hit external APIs.

\- `tests/`: Pytest suite (`test\_crs.py`, `test\_scoring.py`).



\## Geospatial \& Coding Rules

\- \*\*CRS Discipline:\*\* Perform all area, buffering, grid generation, and distance-to-park math in \*\*British National Grid (`EPSG:27700`)\*\*. Reproject to \*\*WGS84 (`EPSG:4326`)\*\* only at ingestion boundaries or right before rendering in `Folium`.

\- \*\*Vector Storage:\*\* Use GeoParquet (`.parquet`) via `geopandas.to\_parquet()`, never ESRI Shapefiles (`.shp`).

\- \*\*Scope Discipline:\*\* Implement only the active ticket requested. Keep modules typed, modular, and clean.

