"""Static checks that the no-build frontend wires difficulty in (Req 1.1-1.5).

The frontend is plain HTML/CSS/JS served as-is — there is no bundler, no module
system, and (per the dev-environment steering) no frontend toolchain to pin. So
rather than introduce a JS test runner, these checks read the static source
files and assert the difficulty control and its wiring are present: a
three-option ``<select>`` with ``medium`` preselected, difficulty travelling in
the newGame body, and the returned difficulty being displayed. They run offline
with the rest of the pytest suite and add no dependency.
"""

from __future__ import annotations

import re
from pathlib import Path

_FRONTEND = Path(__file__).resolve().parent.parent.parent / "frontend"


def _read(name: str) -> str:
    return (_FRONTEND / name).read_text(encoding="utf-8")


# --- index.html: the difficulty <select> (Requirements 1.1, 1.2) -------------


def test_index_html_has_a_difficulty_select_with_the_three_levels() -> None:
    html = _read("index.html")
    assert 'id="difficulty"' in html
    for level in ("easy", "medium", "hard"):
        assert f'value="{level}"' in html


def test_index_html_preselects_medium() -> None:
    html = _read("index.html")
    # The medium option carries `selected`; easy/hard do not.
    assert re.search(r'<option value="medium"[^>]*\bselected\b', html)
    assert not re.search(r'<option value="easy"[^>]*\bselected\b', html)
    assert not re.search(r'<option value="hard"[^>]*\bselected\b', html)


def test_index_html_has_a_place_to_display_the_games_difficulty() -> None:
    html = _read("index.html")
    assert 'id="game-difficulty"' in html


# --- app.js: difficulty is sent and displayed (Requirements 1.3, 1.4, 1.5) ---


def test_app_js_includes_difficulty_in_the_new_game_body() -> None:
    js = _read("app.js")
    # newGame() builds a body carrying both playerId and difficulty.
    assert "getSelectedDifficulty()" in js
    assert re.search(r"difficulty:\s*getSelectedDifficulty\(\)", js)


def test_app_js_reads_the_selected_difficulty_from_the_select() -> None:
    js = _read("app.js")
    assert "function getSelectedDifficulty" in js
    assert "'medium'" in js  # default when the control is absent


def test_app_js_displays_the_returned_difficulty() -> None:
    js = _read("app.js")
    assert "function showGameDifficulty" in js
    # The display is driven by the server-returned difficulty on new game.
    assert "showGameDifficulty(response.data.difficulty)" in js
