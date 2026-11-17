"""Rebindable key bindings.

Every configurable binding has an id; defaults live here and user overrides in
`Config.keymap` ({id: "key[,key2]"}), applied with Textual's App.set_keymap. Hints and the
settings screen read the effective keys from here, so they always match.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.binding import Binding


@dataclass(frozen=True)
class KeyAction:
    id: str
    label: str
    default: str
    scope: str  # "global" | "listen" | "browse"


KEY_ACTIONS: list[KeyAction] = [
    KeyAction("app.browse", "Toggle browse mode", "ctrl+b", "global"),
    KeyAction("app.shuffle", "Shuffle on/off", "ctrl+s", "global"),
    KeyAction("app.repeat", "Repeat off/all/one", "ctrl+r", "global"),
    KeyAction("app.settings", "Open settings", "f2", "global"),
    KeyAction("app.quit", "Quit", "ctrl+q", "global"),
    KeyAction("listen.prev", "Previous track", "left", "listen"),
    KeyAction("listen.next", "Next track", "right", "listen"),
    KeyAction("listen.vol_up", "Volume up", "up", "listen"),
    KeyAction("listen.vol_down", "Volume down", "down", "listen"),
    KeyAction("listen.pause", "Play / pause", "space", "listen"),
    KeyAction("listen.back", "Seek backward", "shift+left,comma", "listen"),
    KeyAction("listen.fwd", "Seek forward", "shift+right,full_stop", "listen"),
    KeyAction("listen.shuffle", "Shuffle on/off", "s", "listen"),
    KeyAction("listen.repeat", "Repeat off/all/one", "r", "listen"),
    KeyAction("browse.enqueue", "Add to queue", "a", "browse"),
    KeyAction("browse.add_to_playlist", "Add to playlist", "p", "browse"),
    KeyAction("browse.new_playlist", "New playlist", "n", "browse"),
    KeyAction("browse.edit", "Edit track / rename playlist", "e", "browse"),
    KeyAction("browse.delete", "Delete / remove", "d,delete", "browse"),
    KeyAction("browse.move_up", "Move up in playlist", "shift+up", "browse"),
    KeyAction("browse.move_down", "Move down in playlist", "shift+down", "browse"),
    KeyAction("browse.refresh", "Refresh", "r", "browse"),
    KeyAction("browse.filter", "Filter library", "slash", "browse"),
    KeyAction("browse.pause", "Play / pause", "space", "browse"),
]
BY_ID = {a.id: a for a in KEY_ACTIONS}

# Keys the UI relies on structurally; they can't be assigned to actions.
RESERVED = frozenset({"escape", "tab", "shift+tab", "enter", "ctrl+c", "backspace"})

_overrides: dict[str, str] = {}


def set_overrides(keymap: dict[str, str]) -> None:
    _overrides.clear()
    _overrides.update({k: v for k, v in keymap.items() if k in BY_ID and v})


def keys_for(action_id: str) -> str:
    return _overrides.get(action_id) or BY_ID[action_id].default


def effective_keymap() -> dict[str, str]:
    return {a.id: keys_for(a.id) for a in KEY_ACTIONS}


def binding(action_id: str, action: str, *, priority: bool = False) -> Binding:
    """A Binding with its id and default key taken from the registry."""
    a = BY_ID[action_id]
    return Binding(a.default, action, a.label, show=False, id=a.id, priority=priority)


def scope_keys(scope: str) -> set[str]:
    out: set[str] = set()
    for a in KEY_ACTIONS:
        if a.scope == scope:
            out.update(k.strip() for k in keys_for(a.id).split(","))
    return out


_NAMES = {
    "left": "←",
    "right": "→",
    "up": "↑",
    "down": "↓",
    "space": "space",
    "comma": ",",
    "full_stop": ".",
    "slash": "/",
    "delete": "Del",
    "escape": "Esc",
}


def display_key(key: str) -> str:
    """'ctrl+b' -> 'Ctrl+B', 'shift+left' -> 'Shift+←', 'f2' -> 'F2'."""
    parts = key.split("+")
    base = parts[-1]
    name = _NAMES.get(base) or (base.upper() if len(base) <= 3 else base.capitalize())
    mods = [m.capitalize() for m in parts[:-1]]
    return "+".join([*mods, name])


def show(action_id: str) -> str:
    """Display form of an action's keys, e.g. 'Shift+← / ,'."""
    return " / ".join(display_key(k.strip()) for k in keys_for(action_id).split(","))


def conflict(action_id: str, key: str) -> KeyAction | None:
    """Another action already using `key` where it would clash (same scope, or global)."""
    me = BY_ID[action_id]
    for a in KEY_ACTIONS:
        if a.id == action_id:
            continue
        clashes = a.scope == me.scope or "global" in (a.scope, me.scope)
        if clashes and key in (k.strip() for k in keys_for(a.id).split(",")):
            return a
    return None


def validate(action_id: str, key: str) -> str | None:
    """Error message if `key` can't be bound to the action, else None."""
    if key in RESERVED:
        return f"{display_key(key)} is reserved by the interface."
    me = BY_ID[action_id]
    has_modifier = "+" in key or (key.startswith("f") and key[1:].isdigit())
    if me.scope == "global" and not has_modifier:
        return (
            "Global shortcuts need a modifier (Ctrl/Alt) or a function key, otherwise "
            "they would fire while you type."
        )
    if other := conflict(action_id, key):
        return f"{display_key(key)} is already used by '{other.label}'."
    return None
