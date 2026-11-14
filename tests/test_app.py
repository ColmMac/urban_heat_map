"""Integration tests that run the real dashboard script with Streamlit's ``AppTest``.

They need the processed web layers in ``data/processed/web/``, so they carry the ``data`` marker
and are skipped by the default suite. Run them with ``uv run python -m pytest -m data
tests/test_app.py``.
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


def _open(app: AppTest, section: str | None) -> AppTest:
    app.segmented_control(key="panel_view").set_value(section).run()
    assert not app.exception, app.exception
    return app


def test_default_is_collapsed_peek(at: AppTest) -> None:
    text = _markdown(at)
    assert 'class="uhi-kpis"' in text
    assert 'class="uhi-peek"' in text and "Zone #1" in text
    assert at.session_state["panel_view"] is None
    assert len(at.slider) == 0  # controls only exist while Tune is open
    assert not at.warning


def test_sections_render(at: AppTest) -> None:
    _open(at, "zone")
    assert "Zone #1 of top-K" in _markdown(at)
    assert 'class="uhi-peek"' not in _markdown(at)  # the inspector replaces the peek card

    _open(at, "rank")
    assert _markdown(at).count('class="uhi-rank-row') == config.TOPK_K

    _open(at, "about")
    assert "Clear-sky passes" in _markdown(at)

    _open(at, None)
    assert 'class="uhi-peek"' in _markdown(at)


def test_weights_survive_section_switch(at: AppTest) -> None:
    """Regression: controls are not drawn while Tune is closed; their values must persist."""
    _open(at, "tune")
    at.slider(key="w_thermal").set_value(0.2).run()
    at.toggle(key="t_parks").set_value(False).run()
    at.segmented_control(key="k_zones").set_value(12).run()
    assert "Heat <b>29%</b>" in _markdown(at)  # 0.2 / (0.2 + 0.25 + 0.25)

    _open(at, "about")
    assert len(at.slider) == 0
    _open(at, "rank")
    assert _markdown(at).count('class="uhi-rank-row') == 12  # K still applied while hidden
    _open(at, None)

    _open(at, "tune")
    assert at.slider(key="w_thermal").value == pytest.approx(0.2)
    assert at.toggle(key="t_parks").value is False
    assert at.segmented_control(key="k_zones").value == 12


def test_restore_defaults(at: AppTest) -> None:
    _open(at, "tune")
    at.slider(key="w_thermal").set_value(0.2).run()
    at.toggle(key="t_parks").set_value(False).run()
    next(b for b in at.button if b.label == "Restore defaults").click().run()
    assert at.slider(key="w_thermal").value == pytest.approx(0.5)
    assert at.segmented_control(key="k_zones").value == config.TOPK_K
    assert at.toggle(key="t_parks").value is False  # reset leaves layer toggles alone
    assert not at.exception


def test_all_zero_weights_warn_and_fall_back(at: AppTest) -> None:
    _open(at, "tune")
    for key in ("w_thermal", "w_distance", "w_impervious"):
        at.slider(key=key).set_value(0.0)
    at.run()
    assert any("All weights are zero" in w.value for w in at.warning)
    assert "Heat <b>50%</b>" in _markdown(at)


def test_map_click_opens_zone_and_survives_weight_change(at: AppTest) -> None:
    grid = gpd.read_parquet(config.WEB_DIR / "grid_web.parquet")
    cell = grid.loc[~grid["eligible"].astype(bool)].iloc[0]  # never a top-K zone

    at.session_state["map"] = {"last_active_drawing": {"properties": {"cell_id": cell["cell_id"]}}}
    at.run()
    assert at.session_state["sel_cell"] == cell["cell_id"]
    assert at.session_state["panel_view"] == "zone"
    text = _markdown(at)
    assert "Selected cell" in text and "Not eligible" in text

    _open(at, "tune")
    at.slider(key="w_distance").set_value(0.6).run()
    assert at.session_state["sel_cell"] == cell["cell_id"]
    assert at.session_state["panel_view"] == "tune"  # an old click doesn't reopen Zone
    _open(at, "zone")
    assert "Selected cell" in _markdown(at)
