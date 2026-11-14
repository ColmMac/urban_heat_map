"""Pure HTML and table builders for the control panel.

Nothing here calls Streamlit, so every builder can be unit-tested with plain pandas inputs.
All text that comes from data is HTML-escaped; colours come from ``theme`` tokens.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from typing import Any

import pandas as pd

from edinburgh_uhi import config
from edinburgh_uhi.config import Weights
from edinburgh_uhi.ui.theme import ACCENT, AMBER, COMPONENTS, CRIMSON, SLATE

INTERVENTION_LABELS = {
    "green_roofs": "Green roofs",
    "street_trees": "Street trees",
    "depave_pocket_park": "Depave & pocket park",
}
EXCLUSION_LABELS = {  # scoring.eligibility() reason codes -> hover-card / inspector text
    "edge_cell": "Mostly outside the city boundary",
    "water": "Mostly water",
    "non_plantable_landuse": "Non-plantable land use",
    "not_urban": "Not urban fabric",
    "already_green": "Already greenspace",
    "insufficient_lst": "Too few clear-sky Landsat passes",
}
CELL_AREA_HA = config.GRID_CELL_M**2 / 10_000
TITLE = "Where should Edinburgh plant its next trees?"

esc = html.escape


def _text(value: object, fallback: str) -> str:
    """``value`` as a string, or ``fallback`` when it is missing (None / NaN / empty)."""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return fallback
    return str(value) or fallback


def exclusion_label(reasons: pd.Series) -> pd.Series:
    """Human-readable exclusion reasons; unknown codes pass through, missing stays missing."""
    return reasons.astype(object).map(lambda r: EXCLUSION_LABELS.get(r, r) if pd.notna(r) else r)


def score_colour(score: float) -> str:
    """Badge colour for a 0–100 priority score."""
    if score >= 75:
        return CRIMSON
    if score >= 50:
        return ACCENT
    if score >= 25:
        return AMBER
    return SLATE


def zone_rank(zones: pd.DataFrame, cell_id: object) -> int | None:
    """Top-K rank of ``cell_id``, or None if it is not one of the selected zones."""
    match = zones.loc[zones["cell_id"] == cell_id, "zone_rank"]
    return int(match.iloc[0]) if len(match) else None


def _stack(parts: Sequence[tuple[float, str]], cls: str = "uhi-stack") -> str:
    """Horizontal stacked bar; each part is ``(width %, colour)``."""
    segs = "".join(
        f'<i style="width:{max(w, 0.0):.1f}%;background:{colour}"></i>' for w, colour in parts
    )
    return f'<div class="{cls}">{segs}</div>'


# --------------------------------------------------------------------------------------
# Header and KPIs
# --------------------------------------------------------------------------------------
def header_html(meta: dict[str, Any]) -> str:
    """Eyebrow, title and one-line method summary (the summary is hidden on phones)."""
    lst = meta.get("lst") or {}
    years = (lst.get("date_range") or [f"{config.LST_YEARS[0]}", f"{config.LST_YEARS[1]}"])[:2]
    return (
        '<div class="uhi-head-block">'
        '<div class="uhi-eyebrow"><span class="uhi-dot"></span>Edinburgh urban heat island</div>'
        f'<h1 class="uhi-title">{TITLE}</h1>'
        f'<p class="uhi-sub">Summer surface heat (Landsat 8/9, {years[0][:4]}–{years[1][:4]}) '
        "× distance to greenspace × sealed ground.</p></div>"
    )


def kpi_items(meta: dict[str, Any], n_zones: int) -> list[tuple[str, str]]:
    """``(value, label)`` pairs for the KPI strip, read from ``scoring_metadata.json``."""
    kpis, lst = meta["kpis"], meta.get("lst") or {}
    scenes = lst.get("n_scenes_used")
    return [
        (f"+{kpis['peak_zone_anomaly_c']:.1f}°C", "peak zone anomaly"),
        (f"{kpis['pct_urban_cells_beyond_doorstep']:.0f}%", "blocks >200 m from a park"),
        (str(n_zones), f"zones · {n_zones * CELL_AREA_HA:.0f} ha"),
        ("n/a" if scenes is None else str(scenes), "clear-sky passes"),
    ]


def kpi_strip_html(items: Sequence[tuple[str, str]]) -> str:
    """A row of compact KPI tiles (value over label); four fit side by side at 375 px."""
    cells = "".join(
        f'<div class="uhi-kpi"><b>{esc(v)}</b><span>{esc(lbl)}</span></div>' for v, lbl in items
    )
    return f'<div class="uhi-kpis">{cells}</div>'


def kpi_glossary_md(meta: dict[str, Any]) -> str:
    """Markdown definitions for the KPI strip (the old hover-only ``help=`` texts)."""
    lst = meta.get("lst") or {}
    dates, air = lst.get("date_range"), lst.get("mean_air_tmax_c")
    passes = "Clear-sky Landsat summer passes used"
    if dates:
        passes += f", {dates[0]} to {dates[1]}" + (f" (mean air Tmax {air:.1f} °C)" if air else "")
    return (
        "- **Peak zone anomaly:** median summer daytime surface-temperature anomaly of the "
        "hottest top-ranked cell, vs. a typical Edinburgh street.\n"
        "- **Blocks > 200 m from a park:** urban blocks more than 200 m from a ≥ 0.5 ha "
        "greenspace (Natural England's doorstep standard).\n"
        "- **Zones:** the top-K cells recommended for greening and their total area.\n"
        f"- **Clear-sky passes:** {passes}."
    )


# --------------------------------------------------------------------------------------
# Selected cell
# --------------------------------------------------------------------------------------
def _recommendation(selected: pd.Series) -> tuple[bool, str, str]:
    """``(eligible, label, text)`` for the selected cell's recommendation line."""
    if bool(selected["eligible"]) and _text(selected["recommendation"], ""):
        label = INTERVENTION_LABELS.get(selected["intervention"], "Recommended")
        return True, label, str(selected["recommendation"])
    code = selected["exclusion_reason"]
    reason = EXCLUSION_LABELS.get(code, code) if _text(code, "") else "n/a"
    return False, "Not eligible", str(reason)


def peek_html(selected: pd.Series, rank: int | None) -> str:
    """One-glance summary of the selected cell, shown above the section nav."""
    score = float(selected["priority_score"])
    kicker = f"Zone #{rank}" if rank else "Selected cell"
    eligible, label, text = _recommendation(selected)
    street = _text(selected["main_street"], "")
    return (
        '<div class="uhi-peek">'
        f'<div class="uhi-peek-score" style="background:{score_colour(score)}">{score:.0f}</div>'
        '<div class="uhi-peek-body">'
        f'<div class="uhi-peek-k">{kicker}</div>'
        f'<div class="uhi-peek-nb">{esc(_text(selected["neighbourhood"], "Edinburgh"))}'
        + (f"<span> · {esc(street)}</span>" if street else "")
        + "</div>"
        f'<div class="uhi-peek-rec{"" if eligible else " uhi-off"}">'
        f"<b>{esc(label)}</b> {esc(text)}</div>"
        "</div></div>"
    )


def inspector_html(selected: pd.Series, rank: int | None, weights: Weights) -> str:
    """Dark HTML card describing one grid cell."""
    score = float(selected["priority_score"])
    anomaly = selected["lst_anomaly_c"]
    anomaly_txt = "n/a" if pd.isna(anomaly) else f"{anomaly:+.1f} °C"
    street = _text(selected["main_street"], "No named main street")
    kicker = f"Zone #{rank} of top-K" if rank else "Selected cell"

    bars = []
    for (label, col, colour), w in zip(
        COMPONENTS, (weights.thermal, weights.distance, weights.impervious), strict=True
    ):
        cap, value = 100.0 * w, float(selected[col])
        pct = min(value / cap, 1.0) * 100 if cap > 0 else 0.0
        bars.append(
            f'<div class="uhi-bar"><div class="uhi-bar-row"><span>{label}</span>'
            f"<span>{value:.0f} / {cap:.0f}</span></div>"
            f'<div class="uhi-bar-track"><div class="uhi-bar-fill" '
            f'style="width:{pct:.0f}%;background:{colour}"></div></div></div>'
        )

    eligible, label, text = _recommendation(selected)
    rec_cls = "uhi-insp-rec" if eligible else "uhi-insp-rec uhi-off"
    rec = f'<div class="{rec_cls}"><span>{esc(label)}</span>{esc(text)}</div>'

    return (
        '<div class="uhi-insp"><div class="uhi-insp-top"><div>'
        f'<div class="uhi-insp-k">{kicker}</div>'
        f'<div class="uhi-insp-nb">{esc(_text(selected["neighbourhood"], "Edinburgh"))}</div>'
        f'<div class="uhi-insp-st">{esc(street)}</div></div>'
        f'<div class="uhi-insp-score" style="background:{score_colour(score)}">'
        f"<b>{score:.0f}</b><span>Priority</span></div></div>"
        '<div class="uhi-insp-stats">'
        f"<div><span>LST anomaly</span><b>{anomaly_txt}</b></div>"
        f"<div><span>Park gap</span><b>{selected['dist_park_m']:.0f} m</b></div>"
        f"<div><span>Impervious</span><b>{selected['impervious_frac'] * 100:.0f} %</b></div>"
        "</div>" + "".join(bars) + rec + "</div>"
    )


# --------------------------------------------------------------------------------------
# Model weights and ranking
# --------------------------------------------------------------------------------------
def weight_mix_html(weights: Weights) -> str:
    """Normalised weight mix as one stacked bar plus a percentage key, in component colours."""
    shares = (weights.thermal, weights.distance, weights.impervious)
    keys = "".join(
        f'<span><i style="background:{colour}"></i>{label} <b>{100 * share:.0f}%</b></span>'
        for (label, _, colour), share in zip(COMPONENTS, shares, strict=True)
    )
    bar = _stack(
        [(100 * share, colour) for (_, _, colour), share in zip(COMPONENTS, shares, strict=True)],
        "uhi-stack uhi-mix-bar",
    )
    return f'<div class="uhi-mix">{bar}<div class="uhi-keys">{keys}</div></div>'


def ranked_list_html(zones: pd.DataFrame, selected_id: object = None) -> str:
    """Top-K zones as a list: rank, place, score and a stacked bar of score contributions.

    Contributions (``c_*``) are points out of 100, so the bar length *is* the score.
    """
    keys = "".join(
        f'<span><i style="background:{colour}"></i>{label}</span>'
        for label, _, colour in COMPONENTS
    )
    rows = []
    for _, z in zones.sort_values("zone_rank").iterrows():
        score = float(z["priority_score"])
        bar = _stack([(float(z[col]), colour) for _, col, colour in COMPONENTS])
        street = _text(z["main_street"], "")
        sel = " is-sel" if z["cell_id"] == selected_id else ""
        rows.append(
            f'<li class="uhi-rank-row{sel}"><span class="uhi-rk">{int(z["zone_rank"])}</span>'
            '<div class="uhi-rank-main"><div class="uhi-rank-top">'
            f"<b>{esc(_text(z['neighbourhood'], 'Edinburgh'))}</b>"
            + (f"<small>{esc(street)}</small>" if street else "")
            + f"</div>{bar}</div>"
            f'<span class="uhi-rank-score" style="color:{score_colour(score)}">{score:.0f}</span>'
            "</li>"
        )
    return (
        f'<div class="uhi-keys uhi-rank-keys">{keys}</div>'
        f'<ol class="uhi-rank-list">{"".join(rows)}</ol>'
    )


def zone_table(zones: pd.DataFrame) -> pd.DataFrame:
    """Ranked Top-K table (numeric columns unformatted) for the CSV download."""
    return pd.DataFrame(
        {
            "Rank": zones["zone_rank"].to_numpy(),
            "Zone": zones["neighbourhood"].fillna("Edinburgh").astype(str).to_numpy(),
            "Street": zones["main_street"].fillna("—").astype(str).to_numpy(),
            "Score": zones["priority_score"].round(1).to_numpy(),
            "LST": zones["lst_anomaly_c"].round(1).to_numpy(),
            "Gap": zones["dist_park_m"].round().to_numpy(),
            "Sealed": (zones["impervious_frac"] * 100).round().to_numpy(),
            "Action": zones["intervention"].map(INTERVENTION_LABELS).to_numpy(),
        }
    )


CSV_COLUMNS = {
    "Rank": "rank",
    "Zone": "neighbourhood",
    "Street": "main_street",
    "Score": "priority_score",
    "LST": "lst_anomaly_c",
    "Gap": "dist_park_m",
    "Sealed": "impervious_pct",
    "Action": "intervention",
}


def zones_csv(zones: pd.DataFrame) -> bytes:
    """UTF-8 CSV of the ranked zones with machine-friendly column names."""
    table = zone_table(zones).rename(columns=CSV_COLUMNS)
    return table.to_csv(index=False).encode("utf-8")
