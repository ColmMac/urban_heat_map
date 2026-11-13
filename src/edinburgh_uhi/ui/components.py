"""Pure HTML, table and chart builders for the control panel.

Nothing here calls Streamlit, so every builder can be unit-tested with plain pandas inputs.
"""

from __future__ import annotations

import html
from typing import Any

import altair as alt
import pandas as pd

from edinburgh_uhi import config
from edinburgh_uhi.config import Weights
from edinburgh_uhi.ui.model import COMPONENT_COLUMNS
from edinburgh_uhi.ui.theme import (
    ACCENT,
    AMBER,
    BG,
    BORDER,
    COMPONENTS,
    CRIMSON,
    MUTED,
    SLATE,
    TEXT,
)

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


def hero_html(meta: dict[str, Any], weights: Weights, k: int) -> str:
    """Title block with live model chips."""
    lst = meta.get("lst") or {}
    years = (lst.get("date_range") or [f"{config.LST_YEARS[0]}", f"{config.LST_YEARS[1]}"])[:2]
    chips = "".join(
        f'<span class="uhi-chip">{label} <b>{value}</b></span>'
        for label, value in (
            ("Heat", f"{weights.thermal:.2f}"),
            ("Park gap", f"{weights.distance:.2f}"),
            ("Sealed", f"{weights.impervious:.2f}"),
            ("Top-K", str(k)),
        )
    )
    return (
        '<div class="uhi-hero"><div>'
        '<div class="uhi-eyebrow"><span class="uhi-dot"></span>'
        "Edinburgh · Urban heat island command center</div>"
        "<h1>Where should Edinburgh plant its next trees?</h1>"
        f"<p>Summer surface heat (Landsat 8/9, {years[0][:4]}–{years[1][:4]}) × distance to "
        "greenspace × sealed ground · re-ranked live from the control panel ›</p>"
        f'</div><div class="uhi-chips">{chips}</div></div>'
    )


def zone_table(zones: pd.DataFrame) -> pd.DataFrame:
    """Ranked Top-K table with numeric columns left unformatted for ``column_config``."""
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


def breakdown_chart(zones: pd.DataFrame) -> alt.LayerChart:
    """Dark horizontal stacked bar of score contributions per zone, with totals."""
    labels = "#" + zones["zone_rank"].astype(str) + "  " + zones["neighbourhood"].astype(str)
    long = (
        zones[["zone_rank", *COMPONENT_COLUMNS]]
        .assign(Zone=labels.to_numpy())
        .melt(id_vars=["zone_rank", "Zone"], var_name="Component", value_name="Points")
    )
    long["Component"] = long["Component"].map({col: label for label, col, _ in COMPONENTS})
    totals = pd.DataFrame(
        {
            "zone_rank": zones["zone_rank"].to_numpy(),
            "Zone": labels.to_numpy(),
            "Total": zones["priority_score"].to_numpy(),
        }
    )
    y = alt.Y("Zone:N", sort=labels.tolist(), title=None)  # explicit rank order
    bars = (
        alt.Chart(long)
        .mark_bar(cornerRadiusEnd=3, height={"band": 0.7})
        .encode(
            y=y,
            x=alt.X(
                "Points:Q",
                title="Greening priority (0–100)",
                scale=alt.Scale(domain=[0, 105]),
                stack="zero",
            ),
            color=alt.Color(
                "Component:N",
                scale=alt.Scale(
                    domain=[c[0] for c in COMPONENTS], range=[c[2] for c in COMPONENTS]
                ),
                legend=alt.Legend(orient="bottom", title=None),
            ),
            order=alt.Order("Component:N", sort="ascending"),
            tooltip=["Zone", "Component", alt.Tooltip("Points:Q", format=".1f")],
        )
    )
    text = (
        alt.Chart(totals)
        .mark_text(align="left", dx=4, color=TEXT, fontWeight="bold", fontSize=11)
        .encode(y=y, x="Total:Q", text=alt.Text("Total:Q", format=".0f"))
    )
    return (
        alt.layer(bars, text)
        .properties(height=26 * len(zones) + 20, background=BG)
        .configure_view(strokeWidth=0)
        .configure_axis(
            labelColor=MUTED,
            titleColor=MUTED,
            gridColor=BORDER,
            domainColor=BORDER,
            tickColor=BORDER,
            labelFontSize=11,
            titleFontSize=11,
            titleFontWeight="normal",
        )
        .configure_axisY(grid=False, labelLimit=170)
        .configure_legend(labelColor="#D1D5DB", labelFontSize=11, symbolType="square")
    )


def inspector_html(selected: pd.Series, rank: int | None, weights: Weights) -> str:
    """Dark HTML card describing one grid cell."""
    esc = html.escape
    score = float(selected["priority_score"])
    anomaly = selected["lst_anomaly_c"]
    anomaly_txt = "n/a" if pd.isna(anomaly) else f"{anomaly:+.1f} °C"
    street = selected["main_street"] or "No named main street"
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

    if bool(selected["eligible"]) and selected["recommendation"]:
        action = INTERVENTION_LABELS.get(selected["intervention"], "Recommended")
        rec = (
            f'<div class="uhi-insp-rec"><span>{esc(action)}</span>'
            f"{esc(selected['recommendation'])}</div>"
        )
    else:
        code = selected["exclusion_reason"]
        reason = EXCLUSION_LABELS.get(code, code) if code else "n/a"
        rec = f'<div class="uhi-insp-rec uhi-off"><span>Not eligible</span>{esc(reason)}</div>'

    return (
        '<div class="uhi-insp"><div class="uhi-insp-top"><div>'
        f'<div class="uhi-insp-k">{kicker}</div>'
        f'<div class="uhi-insp-nb">{esc(selected["neighbourhood"] or "Edinburgh")}</div>'
        f'<div class="uhi-insp-st">{esc(street)}</div></div>'
        f'<div class="uhi-insp-score" style="background:{score_colour(score)}">'
        f"<b>{score:.0f}</b><span>Priority</span></div></div>"
        '<div class="uhi-insp-stats">'
        f"<div><span>LST anomaly</span><b>{anomaly_txt}</b></div>"
        f"<div><span>Park gap</span><b>{selected['dist_park_m']:.0f} m</b></div>"
        f"<div><span>Impervious</span><b>{selected['impervious_frac'] * 100:.0f} %</b></div>"
        "</div>" + "".join(bars) + rec + "</div>"
    )
