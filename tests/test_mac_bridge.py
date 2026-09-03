"""The macOS boundary: the fake's behaviour, and AppleScript-injection safety.

The injection tests are the important ones. `osascript` compiles its input as
source and AppleScript can `do shell script`, so any untrusted value that reaches
the script *text* is arbitrary code execution. Otto passes values as `on run argv`
process arguments instead — these tests assert that, on the real implementation,
using a fake `subprocess.run`.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from otto.platform.mac import (
    Element,
    FakeMac,
    MacError,
    OsascriptMac,
    PermissionMissing,
    build_mac_bridge,
    valid_app_name,
)

INJECTION = '" & (do shell script "id") & "'
INJECTIONS = [
    INJECTION,
    'x"; do shell script "rm -rf ~"; "',
    "'; do shell script 'id'; '",
    'end run\non run argv\ndo shell script "id"',
]


# -- picking an implementation ---------------------------------------------


def test_off_darwin_you_only_ever_get_the_fake():
    bridge = build_mac_bridge()
    if sys.platform != "darwin":
        assert isinstance(bridge, FakeMac)
        assert bridge.is_real is False


def test_the_real_bridge_refuses_to_construct_off_darwin():
    if sys.platform == "darwin":  # pragma: no cover - not this machine
        pytest.skip("we are on macOS")
    with pytest.raises(MacError, match="only runs on macOS"):
        OsascriptMac()


# -- injection safety -------------------------------------------------------


@pytest.fixture
def real_bridge(monkeypatch):
    """The real implementation with subprocess replaced, so we can inspect argv."""
    calls: list = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))

        class R:
            returncode = 0
            stdout = "Safari"
            stderr = ""

        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    bridge = OsascriptMac(allow_non_darwin=True)
    return bridge, calls


@pytest.mark.parametrize("payload", INJECTIONS)
def test_untrusted_values_are_runtime_arguments_not_script_source(real_bridge, payload):
    bridge, calls = real_bridge
    bridge.run_script("write_clipboard", payload)

    argv, kw = calls[-1]
    assert argv[0] == "osascript"
    assert argv[1] == "-e"
    script = argv[2]
    separator = argv[3]
    values = argv[4:]

    # The payload appears ONLY as a trailing process argument.
    assert payload not in script, "an untrusted value reached the script source"
    assert separator == "--"
    assert values == [payload]
    # And no shell is involved at any point.
    assert kw["shell"] is False
    assert isinstance(argv, list)


def test_the_script_reads_its_values_from_argv(real_bridge):
    bridge, calls = real_bridge
    bridge.run_script("notify", "title", "body")
    script = calls[-1][0][2]
    assert "on run argv" in script
    assert "item 1 of argv" in script
    assert "do shell script" not in script


def test_say_passes_text_as_an_argument_not_a_command(monkeypatch):
    captured: list = []

    class FakeProc:
        def poll(self):
            return None

        def terminate(self):
            pass

    def fake_popen(argv, **kw):
        captured.append((argv, kw))
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    bridge = OsascriptMac(allow_non_darwin=True)
    bridge.speak('hello"; do shell script "id"')

    argv, kw = captured[-1]
    assert argv[0] == "say"
    assert argv[-1] == 'hello"; do shell script "id"'
    assert kw["shell"] is False


def test_a_bad_voice_name_is_refused(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: None)
    bridge = OsascriptMac(allow_non_darwin=True)
    with pytest.raises(MacError, match="voice"):
        bridge.speak("hi", voice="Alex; id")


def test_a_permission_error_is_recognised(monkeypatch):
    def fake_run(argv, **kw):
        class R:
            returncode = 1
            stdout = ""
            stderr = "execution error: Not allowed assistive access (-1743)"

        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    bridge = OsascriptMac(allow_non_darwin=True)
    with pytest.raises(PermissionMissing, match="Accessibility"):
        bridge.run_script("frontmost")
    assert bridge.accessibility_trusted() is False


def test_script_arguments_must_be_strings(real_bridge):
    bridge, _ = real_bridge
    with pytest.raises(MacError, match="not a string"):
        bridge.run_script("write_clipboard", 42)  # type: ignore[arg-type]
    with pytest.raises(MacError, match="null byte"):
        bridge.run_script("write_clipboard", "a\x00b")


# -- app names and URLs -----------------------------------------------------


@pytest.mark.parametrize("name", ["Safari", "Visual Studio Code", "IINA", "1Password 7"])
def test_plausible_app_names_are_accepted(name):
    assert valid_app_name(name)


@pytest.mark.parametrize(
    "name",
    ["/Applications/Evil.app", "Safari; id", "$(id)", "", "a" * 200, "Safari\nid",
     "../../bin/sh"],
)
def test_path_shaped_or_metacharacter_names_are_refused(name):
    assert not valid_app_name(name)


def test_uninstalled_apps_are_refused():
    mac = FakeMac(installed=["Safari"])
    with pytest.raises(MacError, match="not installed"):
        mac.open_app("Photoshop")


def test_ambiguous_app_names_are_refused():
    mac = FakeMac(installed=["Chess Pro", "Chess Deluxe"])
    with pytest.raises(MacError, match="several"):
        mac.open_app("Chess")


def test_app_names_are_matched_case_insensitively():
    mac = FakeMac(installed=["Safari"])
    mac.open_app("safari")
    assert mac.frontmost_app() == "Safari"


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "javascript:alert(1)", "otto://x", "ftp://host/x", "", "data:text/html,x"],
)
def test_only_http_and_https_urls_are_opened(url):
    mac = FakeMac()
    with pytest.raises(MacError, match="only http"):
        mac.open_url(url)


def test_https_urls_are_opened():
    mac = FakeMac()
    mac.open_url("https://example.com")
    assert mac.frontmost_app() == "Safari"


# -- the fake's own behaviour ----------------------------------------------


def test_fake_tracks_frontmost_and_windows():
    mac = FakeMac()
    assert mac.frontmost_app() == "Finder"
    mac.open_app("Safari")
    assert mac.frontmost_app() == "Safari"
    window = mac.get_active_window()
    assert window is not None and window.app == "Safari"


def test_fake_accessibility_requires_permission():
    mac = FakeMac(trusted=False)
    with pytest.raises(PermissionMissing, match="Accessibility"):
        mac.accessibility_tree("Safari")
    with pytest.raises(PermissionMissing):
        mac.click_element("Safari", "OK")


def test_fake_element_tree_and_typing():
    mac = FakeMac()
    mac.set_tree(
        "Safari",
        Element(
            role="application",
            name="Safari",
            children=[
                Element(role="window", name="Start",
                        children=[Element(role="text field", name="Address")])
            ],
        ),
    )
    assert mac.find_element("Safari", "Address") is not None
    mac.type_into_element("Safari", "Address", "example.com")
    assert mac.find_element("Safari", "Address").value == "example.com"
    assert mac.typed == [("Safari", "Address", "example.com")]


def test_fake_click_needs_a_real_element():
    mac = FakeMac()
    with pytest.raises(MacError, match="no element named"):
        mac.click_element("Safari", "Nonexistent Button")


def test_fake_records_trash_moves_rather_than_deleting():
    mac = FakeMac()
    mac.move_to_trash("/Users/apple/Desktop/old.txt")
    assert mac.trashed == ["/Users/apple/Desktop/old.txt"]


def test_open_failures_are_injectable():
    mac = FakeMac()
    mac.open_failures["Safari"] = MacError("Safari refused to launch")
    with pytest.raises(MacError, match="refused to launch"):
        mac.open_app("Safari")


# -- structural checks on scripts we cannot compile here --------------------


def _blocks(script: str) -> dict[str, tuple[int, int]]:
    lines = [ln.strip() for ln in script.splitlines()]
    # A block `tell` occupies its own line; `tell X to <statement>` does not open
    # one. A block `if` ends with "then"; anything after "then" is a one-liner.
    opens = {
        "tell": sum(1 for ln in lines if ln.startswith("tell ") and " to " not in ln),
        "repeat": sum(1 for ln in lines if ln.startswith("repeat")),
        "if": sum(1 for ln in lines if ln.startswith("if ") and ln.endswith("then")),
        "try": sum(1 for ln in lines if ln == "try"),
    }
    closes = {
        "tell": lines.count("end tell"),
        "repeat": lines.count("end repeat"),
        "if": lines.count("end if"),
        "try": lines.count("end try"),
    }
    return {k: (opens[k], closes[k]) for k in opens}


@pytest.mark.parametrize("key", sorted(__import__(
    "otto.platform.mac", fromlist=["_SCRIPTS"]
)._SCRIPTS))
def test_every_script_is_structurally_sound(key):
    """These scripts are never compiled in this environment, so this is the only
    automated protection they have. It cannot prove they are correct AppleScript;
    it does catch an unbalanced block, which is the easiest way to break one."""
    from otto.platform.mac import _SCRIPTS

    script = _SCRIPTS[key]
    assert script.startswith("on run argv"), f"{key} must take its values from argv"
    assert script.rstrip().endswith("end run")
    for kind, (opened, closed) in _blocks(script).items():
        assert opened == closed, f"{key}: {opened} {kind} vs {closed} end {kind}"


def test_no_script_contains_a_formatting_placeholder():
    """A placeholder in a script would be a way back to injection.

    Note `{}` alone is not one — it is AppleScript's empty list, which the tree
    script legitimately uses. What must not appear is a *named or indexed* slot.
    """
    import re

    from otto.platform.mac import _SCRIPTS

    placeholder = re.compile(r"\{[A-Za-z_0-9]+\}|%[sdr]\b")
    for key, script in _SCRIPTS.items():
        assert not placeholder.search(script), f"{key} has a format placeholder"
        assert "do shell script" not in script, f"{key} shells out"


def test_run_script_has_no_way_to_interpolate_a_value():
    """The structural guarantee behind the injection tests.

    The one function that calls osascript looks its script up in a table and
    passes it along untouched. String literals are stripped before checking, so
    that the word "script" inside an error message is not mistaken for the
    variable — it is the *script value* that must never be built.
    """
    import inspect
    import io
    import re
    import tokenize

    from otto.platform.mac import OsascriptMac

    source = inspect.getsource(OsascriptMac.run_script)

    # Rebuild each line with string literals blanked out.
    blanked: dict[int, str] = {}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        row = token.start[0]
        text = "" if token.type == tokenize.STRING else token.string
        blanked[row] = blanked.get(row, "") + " " + text

    assignments = [
        line.strip() for line in blanked.values() if re.match(r"\s*script\s*=[^=]", line)
    ]
    assert len(assignments) == 1, f"script is assigned more than once: {assignments}"
    assert "_SCRIPTS [ key ]" in assignments[0].replace("  ", " "), assignments[0]

    for line in blanked.values():
        if not re.search(r"\bscript\b", line) or line.strip() == assignments[0].strip():
            continue
        # Every remaining mention of the script must pass it along whole.
        assert ".format" not in line, line
        assert "+" not in line, line
        assert "%" not in line, line


def test_every_script_key_is_reachable_through_run_script():
    """A script nobody calls is dead weight that still has to be maintained."""
    import inspect

    from otto.platform import mac

    source = inspect.getsource(mac.OsascriptMac)
    for key in mac._SCRIPTS:
        assert f'"{key}"' in source, f"{key} is never used"
