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
/* ===== Page: full-bleed canvas, the map owns the viewport ===== */
html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
  overflow: hidden !important; height: 100vh; height: 100dvh; margin: 0; padding: 0;
}
html, body, [data-testid="stAppViewContainer"], .stApp { background: var(--bg); }
[data-testid="stMainBlockContainer"], .block-container, [data-testid="block-container"] {
  padding: 0 !important; margin: 0 !important; max-width: 100% !important; width: 100% !important;
  height: 100vh; height: 100dvh;
}
[data-testid="stMainBlockContainer"] > [data-testid="stVerticalBlock"] { gap: 0; }
#MainMenu, footer, [data-testid="stToolbar"], [data-testid="stToolbarActions"],
[data-testid="stAppDeployButton"], [data-testid="stDecoration"], [data-testid="stHeader"],
[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"],
[data-testid="collapsedControl"] { display: none !important; }
[data-testid="stElementContainer"]:has(iframe[title*="st_folium"]), .stCustomComponentV1 {
  width: 100vw !important; height: 100vh !important; height: 100dvh !important;
  margin: 0 !important;
}
iframe[title*="st_folium"] {
  border: none !important; display: block; width: 100vw !important; height: 100vh !important;
  height: 100dvh !important;
}

/* ===== Panel (st.container(key="felt_panel")): floating card on desktop ===== */
.st-key-felt_panel {
  position: fixed; top: 16px; left: 16px; z-index: 1000; width: 360px;
  max-height: calc(100vh - 32px); max-height: calc(100dvh - 32px);
  overflow-y: auto; overscroll-behavior: contain; box-sizing: border-box;
  padding: 14px 14px 16px;
  background: color-mix(in srgb, var(--surface) 90%, transparent);
  backdrop-filter: blur(16px) saturate(1.2); -webkit-backdrop-filter: blur(16px) saturate(1.2);
  border: 1px solid var(--line); border-radius: 14px;
  box-shadow: inset 0 1px 0 rgba(255,255,255,.04), var(--shadow-lg);
  scrollbar-width: thin; scrollbar-color: var(--faint) transparent;
}
.st-key-felt_panel [data-testid="stVerticalBlock"] { gap: 10px; }
.st-key-felt_panel :focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.st-key-felt_panel [data-testid="stCaptionContainer"] { font-size: 11.5px; color: var(--muted); }
.st-key-felt_panel [data-testid="stMarkdownContainer"] li { font-size: 13px; color: var(--text); }
.uhi-side-h {
  font-size: 11px; font-weight: 600; letter-spacing: .1em; text-transform: uppercase;
  color: var(--muted); margin: 6px 0 0;
}
.uhi-hint { color: var(--faint); font-size: 12px; margin-top: -2px; }

/* ===== Header + KPI strip ===== */
.uhi-eyebrow {
  display: flex; align-items: center; gap: 8px; font-size: 11px; font-weight: 600;
  letter-spacing: .1em; text-transform: uppercase; color: var(--muted);
}
.uhi-dot {
  width: 7px; height: 7px; border-radius: 50%; background: var(--heat);
  box-shadow: 0 0 8px color-mix(in srgb, var(--heat) 70%, transparent);
}
.uhi-title {
  font-size: 20px !important; line-height: 1.2 !important; font-weight: 700 !important;
  letter-spacing: -.02em; color: var(--text-hi) !important; margin: 6px 0 4px !important;
  padding: 0 !important;
}
.uhi-sub { color: var(--muted); font-size: 12.5px; line-height: 1.4; margin: 0 0 12px; }
.uhi-kpis { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 6px; }
.uhi-kpi {
  min-width: 0; padding: 8px 9px; border-radius: 10px;
  background: var(--surface-2); border: 1px solid var(--line);
}
.uhi-kpi b {
  display: block; font-size: 15px; font-weight: 600; line-height: 1.15; letter-spacing: -.01em;
  color: var(--text-hi); white-space: nowrap; font-variant-numeric: tabular-nums;
}
.uhi-kpi span { display: block; margin-top: 3px; font-size: 10.5px; line-height: 1.25;
  color: var(--muted); }

/* ===== Peek card (selected zone, one glance) ===== */
.uhi-peek {
  display: flex; gap: 10px; align-items: center; padding: 9px 10px; border-radius: 12px;
  background: var(--surface-2); border: 1px solid var(--line);
}
.uhi-peek-score {
  flex: none; width: 40px; height: 40px; border-radius: 10px; display: grid; place-items: center;
  color: var(--text-hi); font-size: 17px; font-weight: 700; font-variant-numeric: tabular-nums;
}
.uhi-peek-body { min-width: 0; flex: 1; }
.uhi-peek-k { font-size: 10.5px; font-weight: 600; letter-spacing: .1em; text-transform: uppercase;
  color: var(--heat); }
.uhi-peek-nb, .uhi-peek-rec { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.uhi-peek-nb { font-size: 14px; font-weight: 600; color: var(--text-hi); }
.uhi-peek-nb span { font-weight: 400; color: var(--muted); }
.uhi-peek-rec { font-size: 12px; color: var(--green-pale); }
.uhi-peek-rec b { color: var(--green); font-weight: 600; }
.uhi-peek-rec.uhi-off, .uhi-peek-rec.uhi-off b { color: var(--muted); }

/* ===== Inspector card (Zone section) ===== */
.uhi-insp {
  background: var(--surface-2); border: 1px solid var(--line); border-radius: 12px;
  padding: 12px 13px; color: var(--text-hi); box-shadow: var(--shadow-sm);
}
.uhi-insp-top { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
.uhi-insp-k { font-size: 10.5px; font-weight: 600; letter-spacing: .12em; text-transform: uppercase;
  color: var(--heat); }
.uhi-insp-nb { font-size: 19px; font-weight: 700; color: var(--text-hi); line-height: 1.2; }
.uhi-insp-st { color: var(--muted); font-size: 13px; }
.uhi-insp-score {
  text-align: center; border-radius: 10px; padding: 5px 10px; color: var(--text-hi);
  min-width: 60px;
}
.uhi-insp-score b { display: block; font-size: 24px; line-height: 1.1;
  font-variant-numeric: tabular-nums; }
.uhi-insp-score span { font-size: 10px; letter-spacing: .1em; text-transform: uppercase;
  opacity: .85; }
.uhi-insp-stats { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; margin: 12px 0; }
.uhi-insp-stats div {
  background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 7px 8px;
}
.uhi-insp-stats span { display: block; font-size: 9.5px; letter-spacing: .08em;
  text-transform: uppercase; color: var(--faint); }
.uhi-insp-stats b { font-size: 15px; color: var(--text-hi); font-variant-numeric: tabular-nums; }
.uhi-bar { margin: 6px 0; font-size: 12px; color: var(--text); }
.uhi-bar-row { display: flex; justify-content: space-between; }
.uhi-bar-row span:last-child { color: var(--muted); font-variant-numeric: tabular-nums; }
.uhi-bar-track { height: 6px; border-radius: 3px; background: var(--surface-3); margin-top: 3px;
  overflow: hidden; }
.uhi-bar-fill { height: 100%; border-radius: 3px; }
.uhi-insp-rec {
  margin-top: 12px; border-left: 3px solid var(--green);
  background: color-mix(in srgb, var(--green) 8%, transparent);
  border-radius: 8px; padding: 8px 11px; color: var(--green-pale); font-size: 13px;
}
.uhi-insp-rec span { display: block; font-size: 10px; letter-spacing: .1em;
  text-transform: uppercase; color: var(--green); font-weight: 600; }
.uhi-insp-rec.uhi-off { border-left-color: var(--faint);
  background: color-mix(in srgb, var(--faint) 18%, transparent); color: var(--muted); }
.uhi-insp-rec.uhi-off span { color: var(--muted); }

/* ===== Stacked bars, weight mix, ranked list ===== */
.uhi-stack { display: flex; height: 6px; border-radius: 3px; overflow: hidden;
  background: var(--surface-3); }
.uhi-stack i { display: block; height: 100%; }
.uhi-mix-bar { height: 8px; border-radius: 4px; }
.uhi-keys { display: flex; flex-wrap: wrap; gap: 4px 12px; margin-top: 6px; font-size: 12px;
  color: var(--muted); }
.uhi-keys i { display: inline-block; width: 8px; height: 8px; border-radius: 2px;
  margin-right: 5px; vertical-align: 0; }
.uhi-keys b { color: var(--text-hi); font-weight: 600; font-variant-numeric: tabular-nums; }
.uhi-rank-keys { margin: 0 0 6px; }
.uhi-rank-list { list-style: none; margin: 0; padding: 0; }
.uhi-rank-row {
  display: grid; grid-template-columns: 24px minmax(0, 1fr) 30px; gap: 8px; align-items: center;
  padding: 7px 8px; margin: 0 !important; border-radius: 8px;
  border-bottom: 1px solid var(--line);
}
.uhi-rank-row.is-sel { background: var(--surface-2); border-bottom-color: transparent; }
.uhi-rk { font-size: 12px; font-weight: 700; color: var(--heat); text-align: center;
  font-variant-numeric: tabular-nums; }
.uhi-rank-top { display: flex; gap: 6px; align-items: baseline; margin-bottom: 5px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.uhi-rank-top b { font-size: 13px; font-weight: 600; color: var(--text-hi); }
.uhi-rank-top small { font-size: 11.5px; color: var(--muted); overflow: hidden;
  text-overflow: ellipsis; }
.uhi-rank-score { font-size: 14px; font-weight: 700; text-align: right;
  font-variant-numeric: tabular-nums; }

/* ===== Streamlit 1.67 widget internals (react-aria). Re-check after any upgrade. ===== */
/* Section nav and K selector: equal-width segments; the active one is a light chip. */
.st-key-felt_panel [role="radiogroup"]:has(> button[data-variant="segmented_control"]) {
  display: flex; width: 100%;
}
.st-key-felt_panel button[data-variant="segmented_control"] {
  flex: 1 1 0; min-width: 0; min-height: 38px; padding: 0 6px;
}
.st-key-felt_panel button[data-variant="segmented_control"] p { font-size: 13px; font-weight: 500; }
.st-key-felt_panel button[data-variant="segmented_control"][aria-checked="true"],
.st-key-felt_panel button[data-variant="segmented_control"][aria-checked="true"]:hover {
  background: var(--text); border-color: var(--text); color: var(--bg);
}
.st-key-felt_panel button[data-variant="segmented_control"][aria-checked="true"] p {
  color: var(--bg); font-weight: 600;
}
/* Monochrome primary: an "on" toggle has a light track, so its knob switches to ink. */
.st-key-felt_panel label[data-selected="true"]:has(input[role="switch"]) > div:first-of-type > div {
  background: var(--bg);
}
/* Slider thumbs: larger touch target, coloured by the score component they weight. */
.st-key-felt_panel [data-testid="stSlider"] [style*="translate(-50%, -50%)"] {
  width: 18px; height: 18px; border: 2px solid var(--bg);
  box-shadow: 0 0 0 1px var(--line-strong), var(--shadow-sm);
}
.st-key-w_thermal [style*="translate(-50%, -50%)"] { background: var(--heat) !important; }
.st-key-w_distance [style*="translate(-50%, -50%)"] { background: var(--green) !important; }
.st-key-w_impervious [style*="translate(-50%, -50%)"] { background: var(--sealed) !important; }
.st-key-felt_panel [data-testid="stSlider"] { padding-inline: 6px; }
/* Colour swatches before the toggle / slider labels double as the map legend. */
.st-key-t_heat [data-testid="stWidgetLabel"] p::before,
.st-key-t_parks [data-testid="stWidgetLabel"] p::before,
.st-key-t_zones [data-testid="stWidgetLabel"] p::before,
.st-key-w_thermal [data-testid="stWidgetLabel"] p::before,
.st-key-w_distance [data-testid="stWidgetLabel"] p::before,
.st-key-w_impervious [data-testid="stWidgetLabel"] p::before {
  content: ""; display: inline-block; width: 10px; height: 10px; border-radius: 3px;
  margin-right: 8px; vertical-align: -1px;
}
.st-key-t_heat [data-testid="stWidgetLabel"] p::before {
  background: linear-gradient(90deg, var(--cool), var(--heat-warm), var(--heat-hot));
}
.st-key-t_parks [data-testid="stWidgetLabel"] p::before { background: var(--green); }
.st-key-t_zones [data-testid="stWidgetLabel"] p::before {
  background: var(--heat); border-radius: 50%;
  box-shadow: 0 0 6px color-mix(in srgb, var(--heat) 70%, transparent);
}
.st-key-w_thermal [data-testid="stWidgetLabel"] p::before { background: var(--heat); }
.st-key-w_distance [data-testid="stWidgetLabel"] p::before { background: var(--green); }
.st-key-w_impervious [data-testid="stWidgetLabel"] p::before { background: var(--sealed); }
/* stMarkdownContainer has margin-bottom: -1rem to cancel a trailing <p> margin; our raw-HTML
   blocks have no <p>, so without this each one is overlapped by the next element. */
.st-key-felt_panel [data-testid="stMarkdownContainer"]:has(> [class^="uhi-"]) {
  margin-bottom: 0 !important;
}
/* ===== end widget internals ===== */

/* ===== Phones: the panel becomes a bottom sheet (kept last so it wins the cascade) ===== */
@media (max-width: 768px) {
  .st-key-felt_panel, div.st-key-felt_panel[data-testid="stVerticalBlock"] {
    top: auto !important; left: 0 !important; right: 0 !important; bottom: 0 !important;
    width: 100% !important; max-width: 100% !important;
    height: auto !important; max-height: 85vh !important; max-height: 85dvh !important;
    z-index: 9999 !important; border-radius: 18px 18px 0 0 !important; border-bottom: 0 !important;
    padding: 6px 14px calc(12px + env(safe-area-inset-bottom)) !important;
  }
  /* Grab handle */
  .st-key-felt_panel::before {
    content: ""; display: block; flex: none; width: 36px; height: 4px; border-radius: 2px;
    background: var(--faint); margin: 2px auto 2px;
  }
  /* A section is open: half-height sheet that scrolls internally. */
  /* (Same selector shape as the sheet rule above, so it wins on specificity.) */
  .st-key-felt_panel:has(.st-key-panel_body),
  div.st-key-felt_panel[data-testid="stVerticalBlock"]:has(.st-key-panel_body) {
    height: 62vh !important; height: 62dvh !important;
  }
  /* ...and gives its room to the section: title and KPI strip collapse while it is open. */
  .st-key-felt_panel:has(.st-key-panel_body)
    [data-testid="stElementContainer"]:has(.uhi-kpis) {
    display: none;
  }
  .st-key-felt_panel [data-testid="stVerticalBlock"] { gap: 8px; }
  .uhi-eyebrow, .uhi-sub { display: none; }
  .uhi-title { font-size: 16px !important; margin: 0 0 8px !important; }
  .uhi-kpi { padding: 7px 7px; }
  .uhi-kpi b { font-size: 14px; }
  .uhi-kpi span { font-size: 10px; }
}
"""


def page_css() -> str:
    """Full ``<style>`` block for the page: tokens first, then the panel stylesheet."""
    return f"<style>\n{css_vars()}\n{FELT_CSS}</style>"


def inject_felt_css() -> None:
    """Inject the page stylesheet (design tokens + floating-panel styles)."""
    st.markdown(page_css(), unsafe_allow_html=True)
