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

from crwallm.desktop.bridge import Bridge

UI = Path(__file__).resolve().parents[2] / "src" / "crwallm" / "ui"

HTML = (UI / "index.html").read_text(encoding="utf-8")
JS = (UI / "app.js").read_text(encoding="utf-8")
CSS = (UI / "style.css").read_text(encoding="utf-8")


def html_ids() -> set[str]:
    return set(re.findall(r'id="([\w-]+)"', HTML))


def js_ids() -> set[str]:
    """Ids the script reaches for.

    Two forms: ``$("busy")`` directly, and the step names held in ``STEPS``
    and passed to ``$`` as a variable.
    """
    return set(re.findall(r'\$\("([\w-]+)"\)', JS)) | set(re.findall(r'"(step-[\w-]+)"', JS))


def test_every_element_the_script_wants_exists() -> None:
    missing = sorted(js_ids() - html_ids())
    assert not missing, (
        f"app.js reads element(s) {missing} that index.html does not define. "
        "In a browser this is a null and then a TypeError on first use."
    )


def test_every_verb_the_script_calls_exists_on_the_bridge() -> None:
    """``window.pywebview.api`` is the Bridge, so a rename here is a crash there."""
    called = set(re.findall(r"api\(\)\.(\w+)\(", JS))
    assert called, "no api() calls found - the regex or the file changed shape"

    missing = sorted(verb for verb in called if not callable(getattr(Bridge, verb, None)))
    assert not missing, f"app.js calls api().{missing} - not a method on Bridge"


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


def test_the_page_loads_its_own_stylesheet_and_script() -> None:
    """A file:// page with a wrong href fails silently and looks unstyled."""
    for name in ("style.css", "app.js"):
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


def test_no_network_references() -> None:
    """The window has no internet: it is a local file with no server behind it.

    A CDN font or script would silently not load, and pulling one in would
    also mean the desktop app phoned somewhere on every launch.
    """
    for name, text in (("index.html", HTML), ("app.js", JS), ("style.css", CSS)):
        found = re.findall(r"https?://[^\s\"')]+", text)
        external = [u for u in found if "developer.microsoft.com" not in u]
        assert not external, f"{name} references {external}"
