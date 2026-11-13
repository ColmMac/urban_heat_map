"""Design tokens and page stylesheet for the dashboard.

``TOKENS`` is the single source of colour. It reaches every surface in one of two ways:

* as CSS custom properties (``css_vars()``) injected into the page *and* into the Folium iframe
  (a separate document), so stylesheets and the hover-card JS use ``var(--name)``;
* as the Python constants below, for places that cannot resolve CSS variables: Leaflet's canvas
  renderer (``prefer_canvas=True`` paints fills with raw colour strings), branca colormaps,
  Altair specs and inline styles built in Python.

Principle: the chrome is monochrome ink; saturated colour is reserved for data meaning (heat,
greenspace, corridors, sealed ground), so a colour on screen always says something.
``.streamlit/config.toml`` mirrors the surface and text tokens; ``tests/test_ui.py`` checks it.
"""

from __future__ import annotations

import streamlit as st

TOKENS: dict[str, str] = {
    # Surfaces: elevation gets lighter (bg < surface < surface-2 < surface-3).
    "bg": "#0B0F14",
    "surface": "#121820",
    "surface-2": "#1A222C",
    "surface-3": "#28323E",
    "border": "#232C37",
    "line": "rgba(255,255,255,.08)",
    "line-strong": "rgba(255,255,255,.14)",
    # Text
    "text-hi": "#F8FAFC",
    "text": "#E7EBF0",
    "muted": "#8A94A3",
    "faint": "#5A6472",
    "focus": "#93C5FD",
    # Data semantics: the only saturated colours on screen.
    "heat": "#F97316",
    "heat-warm": "#F59E0B",
    "heat-deep": "#EA580C",
    "heat-hot": "#DC2626",
    "heat-soft": "#FDBA74",
    "heat-pale": "#FED7AA",
    "green": "#10B981",
    "green-line": "#34D399",
    "green-pale": "#D1FAE5",
    "corridor": "#22D3EE",
    "sealed": "#94A3B8",
    "cool": "#3B82F6",
    "neutral": "#1E293B",  # heat-ramp midpoint (0 °C)
    "nodata": "#374151",
    # Elevation
    "shadow-sm": "0 4px 6px -1px rgba(0,0,0,.3), 0 2px 4px -1px rgba(0,0,0,.2)",
    "shadow-lg": "0 16px 48px rgba(0,0,0,.5), 0 2px 8px rgba(0,0,0,.3)",
}

INTER_CSS_URL = "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap"
FONT_STACK = 'Inter, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'

# Python-side aliases (canvas fills, colormaps, Altair, inline styles).
BG = TOKENS["bg"]
PANEL = TOKENS["surface-2"]
BORDER = TOKENS["border"]
TEXT = TOKENS["text"]
MUTED = TOKENS["muted"]
ACCENT = TOKENS["heat"]
AMBER = TOKENS["heat-warm"]
CRIMSON = TOKENS["heat-hot"]
EMERALD = TOKENS["green"]
EMERALD_LINE = TOKENS["green-line"]
CORRIDOR = TOKENS["corridor"]
SLATE = TOKENS["sealed"]
COOL = TOKENS["cool"]
NODATA = TOKENS["nodata"]
HEAT_RAMP = [COOL, TOKENS["neutral"], AMBER, TOKENS["heat-deep"], CRIMSON]
COMPONENTS = (  # (label, contribution column, colour)
    ("Heat", "c_thermal", ACCENT),
    ("Park gap", "c_distance", EMERALD),
    ("Sealed surface", "c_impervious", SLATE),
)


def css_vars() -> str:
    """``:root`` block declaring every token as a CSS custom property (``--name``)."""
    return ":root{" + "".join(f"--{name}:{value};" for name, value in TOKENS.items()) + "}"


FELT_CSS = """
/* Full-bleed canvas: the map owns the whole viewport, nothing scrolls. */
html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
  overflow: hidden !important; height: 100vh; margin: 0; padding: 0;
}
html, body, [data-testid="stAppViewContainer"], .stApp { background: var(--bg); }
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
  background: color-mix(in srgb, var(--surface) 88%, transparent);
  backdrop-filter: blur(16px) saturate(1.2); -webkit-backdrop-filter: blur(16px) saturate(1.2);
  border: 1px solid var(--line); border-radius: 14px;
  box-shadow: inset 0 1px 0 rgba(255,255,255,.04), var(--shadow-lg);
  scrollbar-width: thin; scrollbar-color: var(--faint) transparent;
}
/* --- Streamlit 1.67 widget internals (react-aria). Re-check these after any upgrade. --- */
/* Monochrome primary: an "on" toggle has a light track, so its knob switches to ink. */
.st-key-felt_panel label[data-selected="true"]:has(input[role="switch"]) > div:first-of-type > div {
  background: var(--bg);
}
/* --- end widget internals --- */

.st-key-felt_panel [data-testid="stVerticalBlock"] { gap: .6rem; }
.st-key-felt_panel hr { margin: .5rem 0; border: 0; border-top: 1px solid var(--line); }
.st-key-felt_panel [data-testid="stMetric"] {
  background: var(--surface-2); border-radius: 10px; padding: .55rem .7rem;
  border: 1px solid var(--line);
}
.st-key-felt_panel [data-testid="stMetricLabel"] p {
  font-size: .64rem; font-weight: 500; color: var(--muted); letter-spacing: .02em;
  line-height: 1.2;
}
.st-key-felt_panel [data-testid="stMetricValue"] {
  color: var(--text-hi); font-weight: 600; letter-spacing: -.02em; font-size: 1.05rem;
  font-variant-numeric: tabular-nums;
}
.st-key-felt_panel [data-testid="stDataFrame"] {
  border: 1px solid var(--line); border-radius: 10px; overflow: hidden;
}
.st-key-felt_panel [data-testid="stExpander"] {
  border: 1px solid var(--line-strong); border-radius: 10px; background: var(--surface-2);
}
.st-key-felt_panel [data-testid="stTabs"] button p { font-size: .75rem; font-weight: 600; }
.st-key-felt_panel [data-testid="stCaptionContainer"] { font-size: .7rem; color: var(--muted); }
.st-key-felt_panel :focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
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
  color: var(--muted); margin: .25rem 0 .55rem;
}

/* Hero */
.uhi-hero { padding: 0; margin: 0; }
.uhi-eyebrow {
  font-size: .7rem; font-weight: 600; letter-spacing: .14em; text-transform: uppercase;
  color: var(--muted); display: flex; align-items: center; gap: .5rem;
}
.uhi-dot {
  width: 8px; height: 8px; border-radius: 50%; background: var(--heat);
  box-shadow: 0 0 8px color-mix(in srgb, var(--heat) 70%, transparent);
}
.uhi-hero h1 {
  font-size: 1.25rem; line-height: 1.25; font-weight: 700; letter-spacing: -.02em;
  margin: .3rem 0 .3rem; padding: 0; color: var(--text-hi);
}
.uhi-hero p { color: var(--muted); margin: 0; font-size: .74rem; line-height: 1.4; }
.uhi-chips { display: flex; gap: .3rem; flex-wrap: wrap; margin-top: .6rem; }
.uhi-chip {
  border: 1px solid var(--line-strong); background: var(--surface-2); border-radius: 999px;
  padding: .15rem .55rem; font-size: .68rem; color: var(--text); white-space: nowrap;
}
.uhi-chip b { color: var(--text-hi); font-variant-numeric: tabular-nums; }

/* Inspector card */
.uhi-insp {
  background: var(--surface-2); border: 1px solid var(--line); border-radius: 12px;
  padding: .85rem .95rem; margin-bottom: .3rem; color: var(--text-hi);
  box-shadow: var(--shadow-sm);
}
.uhi-insp-top {
  display: flex; justify-content: space-between; gap: .8rem; align-items: flex-start;
}
.uhi-insp-k {
  font-size: .66rem; font-weight: 600; letter-spacing: .12em; text-transform: uppercase;
  color: var(--heat);
}
.uhi-insp-nb { font-size: 1.25rem; font-weight: 700; color: var(--text-hi); line-height: 1.2; }
.uhi-insp-st { color: var(--muted); font-size: .85rem; }
.uhi-insp-score {
  text-align: center; border-radius: 10px; padding: .35rem .7rem; color: var(--text-hi);
  min-width: 64px;
}
.uhi-insp-score b { display: block; font-size: 1.6rem; line-height: 1.1;
  font-variant-numeric: tabular-nums; }
.uhi-insp-score span {
  font-size: .62rem; letter-spacing: .1em; text-transform: uppercase; opacity: .85;
}
.uhi-insp-stats {
  display: grid; grid-template-columns: repeat(3, 1fr); gap: .45rem; margin: .85rem 0;
}
.uhi-insp-stats div {
  background: var(--surface); border: 1px solid var(--line); border-radius: 10px;
  padding: .45rem .55rem;
}
.uhi-insp-stats span {
  display: block; font-size: .6rem; letter-spacing: .08em; text-transform: uppercase;
  color: var(--faint);
}
.uhi-insp-stats b { font-size: .98rem; color: var(--text-hi); font-variant-numeric: tabular-nums; }
.uhi-bar { margin: .4rem 0; font-size: .75rem; color: var(--text); }
.uhi-bar-row { display: flex; justify-content: space-between; }
.uhi-bar-row span:last-child { color: var(--muted); font-variant-numeric: tabular-nums; }
.uhi-bar-track {
  height: 6px; border-radius: 3px; background: var(--surface-3); margin-top: 3px;
  overflow: hidden;
}
.uhi-bar-fill { height: 100%; border-radius: 3px; }
.uhi-insp-rec {
  margin-top: .8rem; border-left: 3px solid var(--green);
  background: color-mix(in srgb, var(--green) 8%, transparent);
  border-radius: 8px; padding: .55rem .75rem; color: var(--green-pale); font-size: .85rem;
}
.uhi-insp-rec span {
  display: block; font-size: .6rem; letter-spacing: .1em; text-transform: uppercase;
  color: var(--green); font-weight: 600;
}
.uhi-insp-rec.uhi-off {
  border-left-color: var(--faint); background: color-mix(in srgb, var(--faint) 18%, transparent);
  color: var(--muted);
}
.uhi-insp-rec.uhi-off span { color: var(--muted); }
.uhi-hint { color: var(--faint); font-size: .75rem; margin: -.3rem 0 .6rem; }

/* Links: monochrome, underlined on hover */
.uhi-insp a { color: var(--text); text-decoration: none;
  border-bottom: 1px solid var(--line-strong); }
.uhi-insp a:hover { border-bottom-color: var(--text); }
"""


def page_css() -> str:
    """Full ``<style>`` block for the page: tokens first, then the panel stylesheet."""
    return f"<style>\n{css_vars()}\n{FELT_CSS}</style>"


def inject_felt_css() -> None:
    """Inject the page stylesheet (design tokens + floating-panel styles)."""
    st.markdown(page_css(), unsafe_allow_html=True)
