"""
Light and dark (2026-11-09): tokens follow the Streamlit theme, the stylesheet
is built from them, and nothing in the UI reads a light-only constant.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ais_fmd.ui import appearance, theme

UI_DIR = Path(theme.__file__).parent
VIEWS_DIR = UI_DIR.parent / "views"


def test_every_token_has_a_value_in_both_palettes():
    for name in theme.TOKEN_NAMES:
        light, dark = getattr(theme.LIGHT, name), getattr(theme.DARK, name)
        assert light and dark, name
    assert theme.LIGHT.INK != theme.DARK.INK and theme.LIGHT.CANVAS != theme.DARK.CANVAS


def test_outside_a_session_the_tokens_are_light():
    assert theme.tokens() is theme.LIGHT
    assert theme.active() is theme.PEBBLE and not theme.is_dark()


def test_dark_tokens_when_the_streamlit_theme_is_dark(monkeypatch):
    monkeypatch.setattr(theme, "is_dark", lambda: True)
    assert theme.tokens() is theme.DARK
    assert theme.active() is theme.PEBBLE_DARK
    assert theme.DARK.CANVAS in theme.app_css() and theme.LIGHT.CANVAS not in theme.app_css()
    assert "color-scheme: dark" in theme.app_css()


def test_the_streamlit_config_pairs_a_light_and_a_dark_theme():
    config = (UI_DIR.parent.parent / ".streamlit" / "config.toml").read_text()
    assert "[theme.light]" in config and "[theme.dark]" in config
    assert 'base = "light"' not in config, "a pinned base would stop Streamlit following the device"
    assert theme.DARK.CANVAS in config and theme.LIGHT.CANVAS in config


_CONSTANT = re.compile(r"\btheme\.(" + "|".join(sorted(theme.TOKEN_NAMES, key=len, reverse=True)) + r")\b")


@pytest.mark.parametrize("path", sorted(p for d in (UI_DIR, VIEWS_DIR) for p in d.glob("*.py") if p.name != "theme.py"))
def test_ui_code_reads_colours_at_draw_time_not_import_time(path):
    # `theme.INK` is the light value whatever the session's theme; `theme.tokens().INK` follows it.
    hits = [line.strip() for line in path.read_text().splitlines() if _CONSTANT.search(line)]
    assert not hits, f"{path.name} reads a light-only token: {hits[:3]}"


def test_the_appearance_helper_exists_and_speaks_streamlits_protocol():
    page = (UI_DIR / "appearance" / "index.html").read_text()
    assert "streamlit:componentReady" in page and "streamlit:setComponentValue" in page
    assert "stActiveTheme-" in page and "VERSION = 2;" in page, "Streamlit 1.66 stores the theme under stActiveTheme-<path>-v2"
    assert appearance.CHOICES == ("System", "Light", "Dark")
