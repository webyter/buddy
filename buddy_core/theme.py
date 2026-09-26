"""buddy_core.theme — role-based color themes, OpenCode-style.

Themes are dicts of hex colors keyed by the SAME role names opencode uses
(primary, secondary, accent, error, warning, success, info, text, textMuted,
background*, border*). Buddy maps those roles onto its terminal palette:
the ANSI ui_* helpers and the curses chrome both read util._THEME, and
apply() rewrites it in place — every component recolors automatically.

Built-in themes are ported from opencode's own theme assets (MIT). Users can
drop extra JSON files (same shape) into ~/.buddy/themes/ and pick any of them
with "theme": "<name>" in config.json or /theme <name> in chat.
"""

from __future__ import annotations

import json
import re

from .config import HOME

# ---------------------------------------------------------------------------
# built-in themes — palettes ported from opencode (github.com/sst/opencode)
# ---------------------------------------------------------------------------

THEMES: dict[str, dict[str, str]] = {
    "opencode": {  # buddy's default — warm accent on near-black
        "primary": "#fab283", "secondary": "#5c9cf5", "accent": "#9d7cd8",
        "error": "#e06c75", "warning": "#f5a742", "success": "#7fd88f",
        "info": "#56b6c2", "text": "#eeeeee", "textMuted": "#808080",
        "background": "#0a0a0a", "backgroundPanel": "#141414",
        "backgroundElement": "#1e1e1e", "border": "#484848",
        "borderSubtle": "#3c3c3c",
    },
    "catppuccin": {
        "primary": "#89b4fa", "secondary": "#cba6f7", "accent": "#f5c2e7",
        "error": "#f38ba8", "warning": "#f9e2af", "success": "#a6e3a1",
        "info": "#94e2d5", "text": "#cdd6f4", "textMuted": "#9399b2",
        "background": "#1e1e2e", "backgroundPanel": "#181825",
        "backgroundElement": "#313244", "border": "#585b70",
        "borderSubtle": "#45475a",
    },
    "dracula": {
        "primary": "#bd93f9", "secondary": "#ff79c6", "accent": "#8be9fd",
        "error": "#ff5555", "warning": "#f1fa8c", "success": "#50fa7b",
        "info": "#ffb86c", "text": "#f8f8f2", "textMuted": "#6272a4",
        "background": "#282a36", "backgroundPanel": "#21222c",
        "backgroundElement": "#44475a", "border": "#6272a4",
        "borderSubtle": "#44475a",
    },
    "gruvbox": {
        "primary": "#83a598", "secondary": "#d3869b", "accent": "#fabd2f",
        "error": "#fb4934", "warning": "#fe8019", "success": "#b8bb26",
        "info": "#83a598", "text": "#ebdbb2", "textMuted": "#928374",
        "background": "#282828", "backgroundPanel": "#3c3836",
        "backgroundElement": "#504945", "border": "#665c54",
        "borderSubtle": "#504945",
    },
    "nord": {
        "primary": "#88c0d0", "secondary": "#81a1c1", "accent": "#b48ead",
        "error": "#bf616a", "warning": "#d08770", "success": "#a3be8c",
        "info": "#88c0d0", "text": "#eceff4", "textMuted": "#8b95a7",
        "background": "#2e3440", "backgroundPanel": "#3b4252",
        "backgroundElement": "#434c5e", "border": "#4c566a",
        "borderSubtle": "#434c5e",
    },
    "tokyonight": {
        "primary": "#82aaff", "secondary": "#c099ff", "accent": "#65b2c3",
        "error": "#ff757f", "warning": "#ff966c", "success": "#c3e88d",
        "info": "#82aaff", "text": "#c8d3f5", "textMuted": "#828bb8",
        "background": "#1a1b26", "backgroundPanel": "#1e2030",
        "backgroundElement": "#2f334d", "border": "#737aa2",
        "borderSubtle": "#545c7e",
    },
    "vesper": {
        "primary": "#ffc799", "secondary": "#99ffe4", "accent": "#ffb7c5",
        "error": "#ff8080", "warning": "#ffc799", "success": "#99ffe4",
        "info": "#ffc799", "text": "#ffffff", "textMuted": "#a0a0a0",
        "background": "#101010", "backgroundPanel": "#1c1c1c",
        "backgroundElement": "#282828", "border": "#505050",
        "borderSubtle": "#282828",
    },
    "aura": {
        "primary": "#a277ff", "secondary": "#f694ff", "accent": "#61ffca",
        "error": "#ff6767", "warning": "#ffca85", "success": "#61ffca",
        "info": "#a277ff", "text": "#edecee", "textMuted": "#6d6d6d",
        "background": "#0f0f0f", "backgroundPanel": "#15141b",
        "backgroundElement": "#211f26", "border": "#4a4a4a",
        "borderSubtle": "#2d2d2d",
    },
    "ocweb": {  # buddy "ember" — warm dark default (matches webui.py :root)
        "primary": "#e8a478", "secondary": "#d4764a", "accent": "#e8a478",
        "error": "#e06c5f", "warning": "#e0a44a", "success": "#7fbf7f",
        "info": "#56a8c2", "text": "#e6dfd6", "textMuted": "#a3968a",
        "background": "#100e0c", "backgroundPanel": "#181512",
        "backgroundElement": "#201c18", "border": "#3a332b",
        "borderSubtle": "#2b2620",
    },
    "ocweb-dark": {  # opencode.ai web / share-UI palette — dark
        "primary": "#f4efef", "secondary": "#b8b2b2", "accent": "#7f7a7a",
        "error": "#e06c75", "warning": "#f5a742", "success": "#7fd88f",
        "info": "#56b6c2", "text": "#b8b2b2", "textMuted": "#7f7a7a",
        "background": "#131010", "backgroundPanel": "#1b1818",
        "backgroundElement": "#292424", "border": "#4a4545",
        "borderSubtle": "#3d3838",
    },
    "light": {  # opencode's light steps — for pale terminal themes
        "primary": "#3b7dd8", "secondary": "#7b5bb6", "accent": "#d68c27",
        "error": "#d1383d", "warning": "#b0851f", "success": "#3d9a57",
        "info": "#318795", "text": "#1a1a1a", "textMuted": "#8a8a8a",
        "background": "#ffffff", "backgroundPanel": "#fafafa",
        "backgroundElement": "#ebebeb", "border": "#d4d4d4",
        "borderSubtle": "#e1e1e5",
    },
}

# buddy role <- opencode role. c_* tuples are (xterm-256 index, 8-color
# fallback) exactly like the hand-tuned values that shipped in _THEME.
_ROLE_MAP = {
    "accent": "primary",    # ui_accent — model name, prompt chevron, borders
    "faint":  "textMuted",  # ui_faint — chrome gray, rules, status bar
    "ok":     "success",    # ui_ok — tool success
    "error":  "error",      # ui_err — failures
    "amber":  "warning",    # ui_amber — tips
    "mark":   "secondary",  # ui_mark — block-letter wordmark
    "info":   "info",       # ui_info — code-block tint in the TUI
}

CURRENT: str = "opencode"


def list_themes() -> list[str]:
    """Built-in + user theme names (user themes shadow built-ins)."""
    names = dict.fromkeys(THEMES)
    for p in sorted((HOME / "themes").glob("*.json")):
        names[p.stem] = p.stem
    return list(names)


def load_theme(name: str) -> dict[str, str] | None:
    """Resolve a theme by name; user themes in ~/.buddy/themes win."""
    if not isinstance(name, str) or not re.fullmatch(r"[\w-]+", name):
        return None
    custom = HOME / "themes" / f"{name}.json"
    if custom.exists():
        try:
            data = json.loads(custom.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("primary"):
                return data
        except Exception:
            pass
    return THEMES.get(name)


def _hex_to_256(hexcolor: str) -> int:
    """Nearest xterm-256 palette index for a hex color (stdlib only)."""
    if not isinstance(hexcolor, str):
        return 7
    h = hexcolor.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    except (ValueError, IndexError):
        return 7  # default gray on garbage

    def cube_component(v: int) -> int:
        # 6 levels: 0, 95, 135, 175, 215, 255
        for step, edge in enumerate((48, 115, 155, 195, 235, 256)):
            if v < edge:
                return step
        return 5

    if r == g == b and r not in (0, 255):
        # grayscale ramp (232-255) often lands closer than the color cube
        gray = 232 + round((r - 8) / 247 * 23)
        return max(232, min(255, gray))
    if r <= 8 and g <= 8 and b <= 8:
        return 16
    if r >= 248 and g >= 248 and b >= 248:
        return 231
    return 16 + 36 * cube_component(r) + 6 * cube_component(g) + cube_component(b)


def apply(name: str) -> str:
    """Rewrite util._THEME in place from the named theme. Returns the theme
    actually applied (falls back to built-ins on a bad name)."""
    global CURRENT
    from . import util
    data = load_theme(name)
    if data is None:
        return CURRENT  # unknown: keep whatever is live
    for buddy_role, oc_role in _ROLE_MAP.items():
        hexcolor = data.get(oc_role) or THEMES["opencode"][oc_role]
        idx = _hex_to_256(hexcolor)
        sgr = f"38;5;{idx}"
        is_err = buddy_role == "error"
        util._THEME[buddy_role] = sgr + (";1" if is_err else "")
        # 8-color fallback: nearest of the 16 base ANSI colors
        util._THEME[f"{buddy_role}8"] = _SGR8.get(buddy_role, str(30 + idx % 8))
        # curses: (xterm-256 index, 8-color fallback index)
        fb = {"accent": 3, "faint": 8, "ok": 2, "error": 1,
              "amber": 3, "mark": 6, "info": 6}[buddy_role]
        util._THEME[f"c_{buddy_role}"] = (idx, fb)
    # Aliases for the keys readers actually use (see util._THEME / tui._role):
    # ui_dim reads "dim", fullscreen reads c_red/c_dim — keep them in sync.
    util._THEME["dim"] = util._THEME["faint"]
    util._THEME["c_red"] = util._THEME["c_error"]
    util._THEME["c_dim"] = util._THEME["c_faint"]
    CURRENT = name
    return name


_SGR8 = {"accent": "33;1", "faint": "90", "ok": "32",
         "error": "31;1", "amber": "33", "mark": "36"}
