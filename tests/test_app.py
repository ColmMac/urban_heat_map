"""Integration tests that run the real dashboard script with Streamlit's ``AppTest``.

They need the processed web layers in ``data/processed/web/``, so they carry the ``data`` marker
and are skipped by the default suite. Run them with ``uv run pytest -m data tests/test_app.py``.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pytest
from streamlit.testing.v1 import AppTest

from edinburgh_uhi import config

pytestmark = pytest.mark.data

APP = Path(__file__).resolve().parents[1] / "src" / "edinburgh_uhi" / "app.py"
TIMEOUT_S = 120


@pytest.fixture
def at() -> AppTest:
    if not (config.WEB_DIR / "grid_web.parquet").exists():
        pytest.skip("processed web layers not built")
    app = AppTest.from_file(str(APP), default_timeout=TIMEOUT_S)
    app.run()
    assert not app.exception, app.exception
    return app


def _markdown(app: AppTest) -> str:
    return "\n".join(m.value for m in app.markdown)


def _captions(app: AppTest) -> str:
    return "\n".join(c.value for c in app.caption)


def test_app_renders_with_defaults(at: AppTest) -> None:
    assert at.slider(key="w_thermal").value == pytest.approx(0.5)
    assert at.slider(key="k_zones").value == config.TOPK_K
    assert "Zone #1 of top-K" in _markdown(at)
    assert not at.warning


def test_weight_change_survives_reruns_and_reset_restores(at: AppTest) -> None:
    at.slider(key="w_thermal").set_value(0.2).run()
    assert "Normalised: heat 0.29" in _captions(at)  # 0.2 / (0.2 + 0.25 + 0.25)

    at.toggle(key="t_parks").set_value(False).run()  # an unrelated rerun
    assert at.slider(key="w_thermal").value == pytest.approx(0.2)

    next(b for b in at.button if "Restore defaults" in b.label).click().run()
    assert at.slider(key="w_thermal").value == pytest.approx(0.5)
    assert at.toggle(key="t_parks").value is False  # reset leaves layer toggles alone
    assert not at.exception


def test_all_zero_weights_warn_and_fall_back(at: AppTest) -> None:
    for key in ("w_thermal", "w_distance", "w_impervious"):
        at.slider(key=key).set_value(0.0)
    at.run()
    assert any("All weights are zero" in w.value for w in at.warning)
    assert "Normalised: heat 0.50" in _captions(at)


def test_map_click_selects_cell_and_survives_weight_change(at: AppTest) -> None:
    grid = gpd.read_parquet(config.WEB_DIR / "grid_web.parquet")
    cell = grid.loc[~grid["eligible"].astype(bool)].iloc[0]  # never a top-K zone

    at.session_state["map"] = {"last_active_drawing": {"properties": {"cell_id": cell["cell_id"]}}}
    at.run()
    assert at.session_state["sel_cell"] == cell["cell_id"]
    text = _markdown(at)
    assert "Selected cell" in text and "Not eligible" in text

    at.slider(key="w_distance").set_value(0.6).run()
    assert at.session_state["sel_cell"] == cell["cell_id"]
    assert "Selected cell" in _markdown(at)
