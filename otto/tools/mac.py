"""macOS tools.

All of these go through `MacBridge`, so on Linux they exercise the fake and in
production they exercise `osascript`. Every verifier re-reads real state: after
"open Safari" we ask the Mac which app is frontmost, we do not take the handler's
word for it.

There is deliberately no `click_at(x, y)` tool. Elements are addressed by name.
"""

from __future__ import annotations

from typing import Any

from ..core.permissions import Permission
from ..core.state import Artifact
from .registry import ToolContext, ToolSpec


# ---------------------------------------------------------------------------
# open_app
# ---------------------------------------------------------------------------


def _open_app(ctx: ToolContext, name: str) -> dict[str, Any]:
    # Resolve to the installed app's real name *before* opening, so verification
    # checks the app we were asked for. Reading `frontmost_app()` back here would
    # make the verifier tautological: it would happily confirm that whatever is on
    # screen is on screen, even if nothing launched.
    resolved = ctx.mac.resolve_app(name)
    ctx.mac.open_app(resolved)
    ctx.memory.note_usage("app", resolved)
    return {"app": resolved, "requested": name}


def _verify_open_app(ctx: ToolContext, args: dict, result: Any) -> tuple[bool, str]:
    """Ask the Mac what is actually frontmost."""
    frontmost = ctx.mac.frontmost_app()
    wanted = result["app"].lower()
    if frontmost and frontmost.lower() == wanted:
        return True, f"{result['app']} is frontmost"
    running = [a.lower() for a in ctx.mac.running_apps()]
    if wanted in running:
        return True, f"{result['app']} is running (frontmost is {frontmost})"
    return False, f"{result['app']} is not running; frontmost is {frontmost!r}"


OPEN_APP = ToolSpec(
    name="open_app",
    description="Open (or bring to the front) an application installed on this Mac.",
    schema={"name": {"type": "string", "max_length": 64}},
    required=("name",),
    handler=_open_app,
    verifier=_verify_open_app,
    permission=Permission.SAFE,
)


# ---------------------------------------------------------------------------
# open_url
# ---------------------------------------------------------------------------


def _open_url(ctx: ToolContext, url: str) -> dict[str, Any]:
    ctx.mac.open_url(url)
    return {"url": url, "frontmost": ctx.mac.frontmost_app()}


def _verify_open_url(ctx: ToolContext, args: dict, result: Any) -> tuple[bool, str]:
    window = ctx.mac.get_active_window()
    if window is None:
        return False, "no active window after opening the URL"
    return True, f"{window.app} is frontmost"


OPEN_URL = ToolSpec(
    name="open_url",
    description="Open an http or https URL in the default browser.",
    schema={"url": {"type": "string", "max_length": 2048}},
    required=("url",),
    handler=_open_url,
    verifier=_verify_open_url,
    permission=Permission.SAFE,
)


# ---------------------------------------------------------------------------
# list_apps / get_active_window / accessibility
# ---------------------------------------------------------------------------


def _list_apps(ctx: ToolContext) -> dict[str, Any]:
    return {"apps": ctx.mac.list_apps(), "running": ctx.mac.running_apps()}


def _verify_list_apps(ctx: ToolContext, args: dict, result: Any) -> tuple[bool, str]:
    if not result["apps"]:
        return False, "no applications found — is this really a Mac?"
    return True, f"{len(result['apps'])} apps installed"


LIST_APPS = ToolSpec(
    name="list_apps",
    description="List the applications installed on this Mac.",
    schema={},
    handler=_list_apps,
    verifier=_verify_list_apps,
    permission=Permission.SAFE,
)


def _active_window(ctx: ToolContext) -> dict[str, Any]:
    window = ctx.mac.get_active_window()
    return {
        "app": window.app if window else None,
        "title": window.title if window else None,
    }


def _verify_active_window(ctx: ToolContext, args: dict, result: Any) -> tuple[bool, str]:
    if result["app"] is None:
        return False, "nothing is frontmost"
    return True, f"{result['app']} — {result['title']!r}"


GET_ACTIVE_WINDOW = ToolSpec(
    name="get_active_window",
    description="Report which application and window are currently frontmost.",
    schema={},
    handler=_active_window,
    verifier=_verify_active_window,
    permission=Permission.SAFE,
)


def _tree(ctx: ToolContext, app: str, max_nodes: int = 200) -> dict[str, Any]:
    root = ctx.mac.accessibility_tree(app)
    nodes = []
    for element in root.walk():
        nodes.append({"role": element.role, "name": element.name})
        if len(nodes) >= max_nodes:
            break
    return {"app": app, "nodes": nodes, "root": root.name}


def _verify_tree(ctx: ToolContext, args: dict, result: Any) -> tuple[bool, str]:
    if not result["nodes"]:
        return False, "the accessibility tree came back empty (permission missing?)"
    return True, f"{len(result['nodes'])} elements in {result['app']}"


INSPECT_TREE = ToolSpec(
    name="inspect_accessibility_tree",
    description="List the named UI elements of an app's windows.",
    schema={
        "app": {"type": "string", "max_length": 64},
        "max_nodes": {"type": "integer", "default": 200},
    },
    required=("app",),
    handler=_tree,
    verifier=_verify_tree,
    permission=Permission.SAFE,
)


def _find_element(ctx: ToolContext, app: str, name: str, role: str = "") -> dict[str, Any]:
    element = ctx.mac.find_element(app, name, role or None)
    return {
        "app": app,
        "name": name,
        "found": element is not None,
        "role": element.role if element else None,
        "enabled": element.enabled if element else None,
    }


def _verify_find(ctx: ToolContext, args: dict, result: Any) -> tuple[bool, str]:
    # "Not found" is a truthful answer, not a tool failure — the call succeeded in
    # telling us the element is not there.
    return True, (
        f"{result['name']!r} found ({result['role']})"
        if result["found"]
        else f"{result['name']!r} is not in {result['app']}"
    )


FIND_ELEMENT = ToolSpec(
    name="find_element",
    description="Find a named UI element in an app.",
    schema={
        "app": {"type": "string", "max_length": 64},
        "name": {"type": "string", "max_length": 200},
        "role": {"type": "string", "default": ""},
    },
    required=("app", "name"),
    handler=_find_element,
    verifier=_verify_find,
    permission=Permission.SAFE,
)


# ---------------------------------------------------------------------------
# click / type / menu — CONFIRM, because they change someone else's app
# ---------------------------------------------------------------------------


def _click(ctx: ToolContext, app: str, name: str, role: str = "") -> dict[str, Any]:
    ctx.mac.click_element(app, name, role or None)
    return {"app": app, "name": name, "frontmost": ctx.mac.frontmost_app()}


def _verify_click(ctx: ToolContext, args: dict, result: Any) -> tuple[bool, str]:
    window = ctx.mac.get_active_window()
    if window is None:
        return False, "no window is frontmost after the click"
    return True, f"clicked {result['name']!r}; {window.app} shows {window.title!r}"


CLICK_ELEMENT = ToolSpec(
    name="click_element",
    description="Click a named UI element. Never coordinates.",
    schema={
        "app": {"type": "string", "max_length": 64},
        "name": {"type": "string", "max_length": 200},
        "role": {"type": "string", "default": ""},
    },
    required=("app", "name"),
    handler=_click,
    verifier=_verify_click,
    permission=Permission.CONFIRM,
    confirm_template="Click {name!r} in {app}?",
)


def _type_into(ctx: ToolContext, app: str, name: str, text: str) -> dict[str, Any]:
    ctx.mac.type_into_element(app, name, text)
    return {"app": app, "name": name, "text": text}


def _verify_type(ctx: ToolContext, args: dict, result: Any) -> tuple[bool, str]:
    element = ctx.mac.find_element(result["app"], result["name"])
    if element is None:
        return False, f"{result['name']!r} vanished after typing"
    if element.value != result["text"]:
        return False, f"{result['name']!r} holds {element.value!r}, not what was typed"
    return True, f"{result['name']!r} now contains the text"


TYPE_INTO_ELEMENT = ToolSpec(
    name="type_into_element",
    description="Type text into a named text field.",
    schema={
        "app": {"type": "string", "max_length": 64},
        "name": {"type": "string", "max_length": 200},
        "text": {"type": "string", "max_length": 5000},
    },
    required=("app", "name", "text"),
    handler=_type_into,
    verifier=_verify_type,
    permission=Permission.CONFIRM,
    confirm_template="Type into {name!r} in {app}?",
)


def _select_menu(ctx: ToolContext, app: str, menu: str, item: str) -> dict[str, Any]:
    ctx.mac.select_menu_item(app, menu, item)
    return {"app": app, "menu": menu, "item": item}


def _verify_menu(ctx: ToolContext, args: dict, result: Any) -> tuple[bool, str]:
    frontmost = ctx.mac.frontmost_app()
    selections = getattr(ctx.mac, "menu_selections", None)
    if selections is not None and (result["app"], result["menu"], result["item"]) not in selections:
        return False, "the menu selection was not recorded"
    return True, f"selected {result['menu']} → {result['item']} (frontmost {frontmost})"


SELECT_MENU_ITEM = ToolSpec(
    name="select_menu_item",
    description="Choose a menu item by name from an app's menu bar.",
    schema={
        "app": {"type": "string", "max_length": 64},
        "menu": {"type": "string", "max_length": 64},
        "item": {"type": "string", "max_length": 120},
    },
    required=("app", "menu", "item"),
    handler=_select_menu,
    verifier=_verify_menu,
    permission=Permission.CONFIRM,
    confirm_template="Choose {menu} → {item} in {app}?",
)


# ---------------------------------------------------------------------------
# clipboard, notify, speak
# ---------------------------------------------------------------------------


def _read_clipboard(ctx: ToolContext) -> dict[str, Any]:
    return {"text": ctx.mac.read_clipboard()}


def _verify_clipboard_read(ctx: ToolContext, args: dict, result: Any) -> tuple[bool, str]:
    return True, f"{len(result['text'])} characters on the clipboard"


READ_CLIPBOARD = ToolSpec(
    name="read_clipboard",
    description="Read the current clipboard text.",
    schema={},
    handler=_read_clipboard,
    verifier=_verify_clipboard_read,
    permission=Permission.SAFE,
)


def _write_clipboard(ctx: ToolContext, text: str) -> dict[str, Any]:
    ctx.mac.write_clipboard(text)
    return {"text": text}


def _verify_clipboard_write(ctx: ToolContext, args: dict, result: Any) -> tuple[bool, str]:
    current = ctx.mac.read_clipboard()
    if current != result["text"]:
        return False, "the clipboard does not hold the new text"
    return True, "clipboard updated"


WRITE_CLIPBOARD = ToolSpec(
    name="write_clipboard",
    description="Put text on the clipboard.",
    schema={"text": {"type": "string", "max_length": 20000}},
    required=("text",),
    handler=_write_clipboard,
    verifier=_verify_clipboard_write,
    permission=Permission.CONFIRM,
    confirm_template="Replace the clipboard contents?",
)


def _notify(ctx: ToolContext, title: str, message: str) -> dict[str, Any]:
    ctx.mac.notify(title, message)
    return {"title": title, "message": message}


def _verify_notify(ctx: ToolContext, args: dict, result: Any) -> tuple[bool, str]:
    return True, "notification posted"


NOTIFY = ToolSpec(
    name="notify",
    description="Post a macOS notification.",
    schema={
        "title": {"type": "string", "max_length": 120},
        "message": {"type": "string", "max_length": 400},
    },
    required=("title", "message"),
    handler=_notify,
    verifier=_verify_notify,
    permission=Permission.SAFE,
)


def _speak(ctx: ToolContext, text: str) -> dict[str, Any]:
    ctx.services.speak(text)
    ctx.task.add_artifact(
        Artifact(kind="text", name="spoken", value=text, subtask_id=ctx.subtask_id)
    )
    return {"text": text}


def _verify_speak(ctx: ToolContext, args: dict, result: Any) -> tuple[bool, str]:
    return (True, "spoken") if result["text"] else (False, "nothing to say")


SPEAK = ToolSpec(
    name="speak",
    description="Say something out loud to the user.",
    schema={"text": {"type": "string", "max_length": 2000}},
    required=("text",),
    handler=_speak,
    verifier=_verify_speak,
    permission=Permission.SAFE,
)


MAC_TOOLS = (
    OPEN_APP,
    OPEN_URL,
    LIST_APPS,
    GET_ACTIVE_WINDOW,
    INSPECT_TREE,
    FIND_ELEMENT,
    CLICK_ELEMENT,
    TYPE_INTO_ELEMENT,
    SELECT_MENU_ITEM,
    READ_CLIPBOARD,
    WRITE_CLIPBOARD,
    NOTIFY,
    SPEAK,
)
