"""The desktop window's two halves have to agree about names.

Nothing in Python fails when JavaScript asks for an element that is not there.
``document.getElementById("colums")`` returns ``null``, the page loads, looks
finished, and throws the first time somebody presses the button - which in a
desktop app means a window that does nothing with no error anywhere a person
would find it.

This is the same failure this project keeps producing at seams (see
``test_extraction_plan``): a name that exists on one side and not the other.
The checks here are static and cheap, and each one corresponds to a way the
window has actually broken or could break silently.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from crwallm.desktop.bridge import Bridge

UI = Path(__file__).resolve().parents[2] / "src" / "crwallm" / "ui"

HTML = (UI / "index.html").read_text(encoding="utf-8")
CSS = (UI / "style.css").read_text(encoding="utf-8")

SCRIPTS = ("api.js", "app.js", "jobs.js", "recipes.js", "chat.js", "nav.js")
"""Every script the page loads.

Derived from the page rather than listed, below, so a new file that is added
to index.html and forgotten here fails instead of going unchecked - which is
the failure this whole file exists to prevent."""

JS = "\n".join((UI / name).read_text(encoding="utf-8") for name in SCRIPTS)


def html_ids() -> set[str]:
    return set(re.findall(r'id="([\w-]+)"', HTML))


def literal(pattern: str) -> list[str]:
    """The strings inside one named literal in the scripts.

    Scoped to the literal rather than scanned across the file. Matching
    ``"step-*"`` anywhere also matched ``querySelector(".step-detail")`` - a
    CSS class, not an id - and the guard failed on code that was correct,
    which teaches people to edit the guard instead of the bug.
    """
    block = re.search(pattern, JS, re.DOTALL)
    return re.findall(r'"([\w-]+)"', block.group(1)) if block else []


def js_ids() -> set[str]:
    """Ids the scripts reach for.

    Three forms: ``$("busy")`` directly, the step names held in ``STEPS``, and
    the view names in ``VIEWS`` - both of the latter reach ``$`` as variables,
    where a typo is invisible until someone clicks the tab.
    """
    return (
        set(re.findall(r'\$\("([\w-]+)"\)', JS))
        | set(literal(r"STEPS = \{(.*?)\}"))
        | {f"view-{name}" for name in literal(r"VIEWS = \[(.*?)\]")}
    )


def test_the_guard_covers_every_script_the_page_loads() -> None:
    """A script added to the page and not to SCRIPTS is unguarded, and the
    guard still passes - which is worse than not having one."""
    loaded = set(re.findall(r'<script src="([^"]+)"', HTML))
    assert loaded == set(SCRIPTS), (
        f"index.html loads {sorted(loaded)}; SCRIPTS lists {sorted(SCRIPTS)}"
    )


def test_every_element_the_script_wants_exists() -> None:
    missing = sorted(js_ids() - html_ids())
    assert not missing, (
        f"the scripts read element(s) {missing} that index.html does not define. "
        "In a browser this is a null and then a TypeError on first use."
    )


def test_every_verb_the_script_calls_exists_on_the_bridge() -> None:
    """``window.pywebview.api`` is the Bridge, so a rename here is a crash there."""
    called = set(re.findall(r"api\(\)\.(\w+)\(", JS))
    assert called, "no api() calls found - the regex or the file changed shape"

    missing = sorted(verb for verb in called if not callable(getattr(Bridge, verb, None)))
    assert not missing, f"the scripts call api().{missing} - not a method on Bridge"


def test_the_page_can_call_exactly_the_four_verbs() -> None:
    """pywebview hands the page every public member of the bridge.

    It walks ``dir(obj)`` and skips only names starting with ``_`` - so a
    public helper is a button the JavaScript can press. ``close`` was one: it
    stops the engine loop, and every call after it would hang the window with
    nothing on screen to say why.
    """
    exposed = {name for name in dir(Bridge) if not name.startswith("_")}
    assert exposed == {"look", "collect", "save", "stop"}, (
        f"the page can call {sorted(exposed)}. Anything that is not one of the "
        "four verbs belongs behind a leading underscore."
    )


def test_progress_is_pushed_to_the_channel_the_page_listens_on() -> None:
    """``_emit`` writes one exact path into the page, and only one thing reads it."""
    emit = Path(Bridge.__module__.replace(".", "/") + ".py")
    source = (Path(__file__).resolve().parents[2] / "src" / emit).read_text(encoding="utf-8")
    assert "window.crwallm && window.crwallm.on(" in source
    assert "window.crwallm = {" in JS
    assert re.search(r"\bon\(event, payload\)", JS)


def test_the_page_loads_its_own_stylesheet_and_scripts() -> None:
    """A file:// page with a wrong href fails silently and looks unstyled."""
    for name in ("style.css", *SCRIPTS):
        assert f'"{name}"' in HTML, f"index.html does not reference {name}"
        assert (UI / name).exists()


def test_hidden_beats_the_layout_rules() -> None:
    """The overlay covered the app the moment it loaded.

    ``.busy`` sets ``display: flex``, which outranks the browser's own
    ``[hidden] { display: none }`` - so every section the page hides with the
    ``hidden`` property stayed on screen. Nothing in Python can see that, and
    the app is unusable without it.
    """
    assert re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", CSS), (
        "style.css must force [hidden] to display:none, or .busy's display:flex wins"
    )


def without_comments(source: str) -> str:
    """Explanations are not instructions to the browser."""
    return re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)


def test_the_page_loads_nothing_from_the_network() -> None:
    """In the window there is no internet: a local file with no server behind
    it. A CDN font or script would silently not load, and pulling one in would
    also mean the app phoned somewhere on every launch.

    Only things the browser *fetches* count. An example address printed for
    someone to read - "말해보세요: https://..." - is prose, and an earlier
    version of this test failed on exactly that, which is a guard describing
    something other than what it is named after.
    """
    fetched = re.findall(
        r'\b(?:src|href|action|poster|srcset|data)\s*=\s*"([^"]*)"', HTML
    ) + re.findall(r'url\(\s*["\']?([^"\')]+)', CSS)
    external = [ref for ref in fetched if re.match(r"https?://|//", ref)]
    assert not external, f"the page loads {external} from the network"


@pytest.mark.parametrize("name", SCRIPTS)
def test_the_scripts_name_no_host_but_this_one(name: str) -> None:
    """Every request the page makes goes to the server that served it - or, in
    the window, to pywebview. A hard-coded host here would be a call to someone
    else's machine from a tool whose whole claim is that data stays on this one.

    A *literal* host, specifically. ``https://${url}`` completes an address the
    person typed and names nobody; an earlier version of this test read the
    scheme alone and failed on exactly that, which teaches the next person to
    edit the guard rather than the code.
    """
    source = without_comments((UI / name).read_text(encoding="utf-8"))
    called = re.findall(r"""["'`](https?://(?=[A-Za-z0-9])[^"'`]*)""", source)
    assert not called, f"{name} names {called}"
