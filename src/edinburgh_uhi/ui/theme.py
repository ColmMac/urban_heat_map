"""Colour palette and page stylesheet for the dashboard.

The palette constants are shared by the page CSS, the Folium map CSS, the hover-card JS and the
Altair chart, so every surface draws from one place.
"""

from __future__ import annotations

import streamlit as st

# Palette (kept in sync with .streamlit/config.toml)
BG = "#171717"
PANEL = "#262626"
BORDER = "#333333"
TEXT = "#F3F4F6"
MUTED = "#9CA3AF"
ACCENT = "#F97316"
AMBER = "#F59E0B"
CRIMSON = "#DC2626"
EMERALD = "#10B981"
CORRIDOR = "#22D3EE"
SLATE = "#64748B"
COOL = "#3B82F6"
HEAT_RAMP = [COOL, "#1E293B", AMBER, "#EA580C", CRIMSON]
COMPONENTS = (  # (label, contribution column, colour)
    ("Heat", "c_thermal", ACCENT),
    ("Park gap", "c_distance", EMERALD),
    ("Sealed surface", "c_impervious", SLATE),
)

FELT_CSS = """
<style>
@import url("https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap");

html, body, [data-testid="stAppViewContainer"], .stApp {
  font-family: Inter, system-ui, -apple-system, "Segoe UI", sans-serif;
}
/* Full-bleed canvas: the map owns the whole viewport, nothing scrolls. */
html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
  overflow: hidden !important; height: 100vh; margin: 0; padding: 0;
}
[data-testid="stMainBlockContainer"], .block-container, [data-testid="block-container"] {
  padding: 0 !important; margin: 0 !important; max-width: 100% !important; width: 100% !important;
  height: 100vh;
}
[data-testid="stMainBlockContainer"] > [data-testid="stVerticalBlock"] { gap: 0; }
#MainMenu, footer, [data-testid="stToolbar"], [data-testid="stToolbarActions"],
[data-testid="stAppDeployButton"], [data-testid="stDecoration"], [data-testid="stHeader"],
[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"],
[data-testid="collapsedControl"] { display: none !important; }
[data-testid="stElementContainer"]:has(iframe[title*="st_folium"]), .stCustomComponentV1 {
  width: 100vw !important; height: 100vh !important; margin: 0 !important;
}
iframe[title*="st_folium"] {
  border: none !important; display: block; width: 100vw !important; height: 100vh !important;
}

/* Floating master panel (st.container(key="felt_panel") -> .st-key-felt_panel) */
.st-key-felt_panel {
  position: fixed; top: 20px; left: 20px; z-index: 1000; width: 350px; max-height: 90vh;
  overflow-y: auto; box-sizing: border-box; padding: 1rem 1.1rem 1.1rem;
  background: rgba(38,38,38,.86); backdrop-filter: blur(14px) saturate(1.1);
  -webkit-backdrop-filter: blur(14px) saturate(1.1);
  border: 1px solid rgba(255,255,255,.08); border-radius: 12px;
  box-shadow: 0 12px 40px rgba(0,0,0,.55), 0 2px 8px rgba(0,0,0,.35);
  scrollbar-width: thin; scrollbar-color: #525252 transparent;
}
.st-key-felt_panel [data-testid="stVerticalBlock"] { gap: .6rem; }
.st-key-felt_panel hr { margin: .5rem 0; border: 0; border-top: 1px solid rgba(255,255,255,.1); }
.st-key-felt_panel [data-testid="stMetric"] {
  background: rgba(23,23,23,.75); border-radius: 10px; padding: .55rem .7rem;
  border: 1px solid rgba(255,255,255,.06);
}
.st-key-felt_panel [data-testid="stMetricLabel"] p {
  font-size: .64rem; font-weight: 500; color: #A3A3A3; letter-spacing: .02em; line-height: 1.2;
}
.st-key-felt_panel [data-testid="stMetricValue"] {
  color: #FFFFFF; font-weight: 600; letter-spacing: -.02em; font-size: 1.05rem;
}
.st-key-felt_panel [data-testid="stDataFrame"] {
  border: 1px solid rgba(255,255,255,.08); border-radius: 10px; overflow: hidden;
}
.st-key-felt_panel [data-testid="stExpander"] {
  border: 1px solid rgba(255,255,255,.1); border-radius: 10px; background: rgba(23,23,23,.6);
}
.st-key-felt_panel [data-testid="stTabs"] button p { font-size: .75rem; font-weight: 600; }
.st-key-felt_panel [data-testid="stCaptionContainer"] { font-size: .7rem; }
/* Phones: the panel becomes a short bottom sheet so the map stays visible above it. */
@media (max-width: 768px) {
  .st-key-felt_panel, div.st-key-felt_panel[data-testid="stVerticalBlock"] {
    position: fixed !important; top: auto !important; bottom: 0 !important;
    left: 0 !important; right: auto !important; width: 100vw !important;
    max-width: 100vw !important; height: auto !important;
    max-height: 40vh !important; max-height: 40dvh !important;
    border-radius: 20px 20px 0 0 !important; z-index: 9999 !important;
    overflow-y: auto !important; padding: .75rem .9rem 2rem !important;
    border-bottom: 0 !important; overscroll-behavior: contain;
  }
  .uhi-hero p, .uhi-eyebrow { display: none; }
  .uhi-hero h1 { font-size: 1.05rem; margin: 0 0 .2rem; }
  .uhi-chips { margin-top: .3rem; }
  /* keep the 2 x 2 KPI grid side by side instead of Streamlit's stacked mobile columns */
  .st-key-felt_panel [data-testid="stHorizontalBlock"] { flex-direction: row !important;
    flex-wrap: nowrap !important; }
  .st-key-felt_panel [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
    min-width: 0 !important; flex: 1 1 0% !important; width: 0 !important; }
}
.uhi-side-h {
  font-size: .68rem; font-weight: 600; letter-spacing: .12em; text-transform: uppercase;
  color: #A3A3A3; margin: .25rem 0 .55rem;
}

/* Hero */
.uhi-hero { padding: 0; margin: 0; }
.uhi-eyebrow {
  font-size: .7rem; font-weight: 600; letter-spacing: .14em; text-transform: uppercase;
  color: #3B82F6; display: flex; align-items: center; gap: .5rem;
}
.uhi-dot { width: 8px; height: 8px; border-radius: 50%; background: #3B82F6; }
.uhi-hero h1 {
  font-size: 1.25rem; line-height: 1.25; font-weight: 700; letter-spacing: -.02em;
  margin: .3rem 0 .3rem; padding: 0; color: #FFFFFF;
}
.uhi-hero p { color: #A3A3A3; margin: 0; font-size: .74rem; line-height: 1.4; }
.uhi-chips { display: flex; gap: .3rem; flex-wrap: wrap; margin-top: .6rem; }
.uhi-chip {
  border: 1px solid rgba(255,255,255,.1); background: #262626; border-radius: 999px;
  padding: .15rem .55rem; font-size: .68rem; color: #D4D4D4; white-space: nowrap;
}
.uhi-chip b { color: #FFFFFF; }

/* Inspector card */
.uhi-insp {
  background: #262626; border: 1px solid rgba(255,255,255,.05); border-radius: 12px;
  padding: .85rem .95rem; margin-bottom: .3rem; color: #FFFFFF;
  box-shadow: 0 4px 6px -1px rgba(0,0,0,.3), 0 2px 4px -1px rgba(0,0,0,.2);
}
.uhi-insp-top {
  display: flex; justify-content: space-between; gap: .8rem; align-items: flex-start;
}
.uhi-insp-k {
  font-size: .66rem; font-weight: 600; letter-spacing: .12em; text-transform: uppercase;
  color: #3B82F6;
}
.uhi-insp-nb { font-size: 1.25rem; font-weight: 700; color: #FFFFFF; line-height: 1.2; }
.uhi-insp-st { color: #A3A3A3; font-size: .85rem; }
.uhi-insp-score {
  text-align: center; border-radius: 10px; padding: .35rem .7rem; color: #fff; min-width: 64px;
}
.uhi-insp-score b { display: block; font-size: 1.6rem; line-height: 1.1; }
.uhi-insp-score span {
  font-size: .62rem; letter-spacing: .1em; text-transform: uppercase; opacity: .85;
}
.uhi-insp-stats {
  display: grid; grid-template-columns: repeat(3, 1fr); gap: .45rem; margin: .85rem 0;
}
.uhi-insp-stats div {
  background: #171717; border: 1px solid rgba(255,255,255,.06); border-radius: 10px;
  padding: .45rem .55rem;
}
.uhi-insp-stats span {
  display: block; font-size: .6rem; letter-spacing: .08em; text-transform: uppercase;
  color: #737373;
}
.uhi-insp-stats b { font-size: .98rem; color: #FFFFFF; }
.uhi-bar { margin: .4rem 0; font-size: .75rem; color: #D4D4D4; }
.uhi-bar-row { display: flex; justify-content: space-between; }
.uhi-bar-row span:last-child { color: #A3A3A3; }
.uhi-bar-track {
  height: 6px; border-radius: 3px; background: #404040; margin-top: 3px; overflow: hidden;
}
.uhi-bar-fill { height: 100%; border-radius: 3px; }
.uhi-insp-rec {
  margin-top: .8rem; border-left: 3px solid #10B981; background: rgba(16,185,129,.08);
  border-radius: 8px; padding: .55rem .75rem; color: #D1FAE5; font-size: .85rem;
}
.uhi-insp-rec span {
  display: block; font-size: .6rem; letter-spacing: .1em; text-transform: uppercase;
  color: #10B981; font-weight: 600;
}
.uhi-insp-rec.uhi-off {
  border-left-color: #525252; background: rgba(82,82,82,.18); color: #A3A3A3;
}
.uhi-insp-rec.uhi-off span { color: #A3A3A3; }
.uhi-hint { color: #737373; font-size: .75rem; margin: -.3rem 0 .6rem; }

/* Generic custom cards */
.uhi-kpi-card, .uhi-inspector, .uhi-inspector-card {
  background: #262626; border-radius: 12px; padding: 1.2rem; color: #FFFFFF;
  border: 1px solid rgba(255,255,255,.05);
  box-shadow: 0 4px 6px -1px rgba(0,0,0,.3), 0 2px 4px -1px rgba(0,0,0,.2);
}
.uhi-insp a, .uhi-inspector a { color: #3B82F6; text-decoration: none; }
.uhi-insp a:hover, .uhi-inspector a:hover { text-decoration: underline; }
</style>
"""


def inject_felt_css() -> None:
    """Inject the Felt-inspired page stylesheet (neutral dark panels, blue accent)."""
    st.markdown(FELT_CSS, unsafe_allow_html=True)
