"""Slash command catalogue: drives the completion menu and /help."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandSpec:
    name: str  # e.g. "/play" or "/queue add"
    args: str  # usage hint, "" if the command takes no arguments
    help: str


COMMAND_SPECS: list[CommandSpec] = [
    CommandSpec("/search", "<words>", "search the library"),
    CommandSpec("/recent", "[n]", "recently added tracks"),
    CommandSpec("/history", "[n]", "recently played tracks"),
    CommandSpec("/top", "[n]", "most played tracks"),
    CommandSpec("/play", "[n|words]", "play the listed tracks from #n, or search and play"),
    CommandSpec("/queue", "", "show the queue"),
    CommandSpec("/queue add", "<n…>", "add listed tracks to the queue (1 3 5-7)"),
    CommandSpec("/queue rm", "<n…>", "remove tracks from the queue"),
    CommandSpec("/queue clear", "", "empty the queue"),
    CommandSpec("/playlists", "", "list your playlists"),
    CommandSpec("/playlist new", "<name>", "create an empty playlist"),
    CommandSpec("/playlist save", "<name>", "save the current queue as a playlist"),
    CommandSpec("/playlist play", "<name|n> [shuffle]", "play a playlist"),
    CommandSpec("/playlist shuffle", "<name|n>", "play a playlist shuffled"),
    CommandSpec("/playlist show", "<name|n>", "list a playlist's tracks"),
    CommandSpec("/playlist add", "<name|n> <n…>", "add listed tracks to a playlist"),
    CommandSpec("/playlist rm", "<name|n> <pos…>", "remove tracks from a playlist"),
    CommandSpec("/playlist move", "<name|n> <from> <to>", "reorder a playlist"),
    CommandSpec("/playlist rename", "<name|n> -> <new>", "rename a playlist"),
    CommandSpec("/playlist delete", "<name|n>", "delete a playlist (tracks stay)"),
    CommandSpec("/next", "", "next track"),
    CommandSpec("/prev", "", "previous track (or restart this one)"),
    CommandSpec("/pause", "", "play / pause"),
    CommandSpec("/stop", "", "stop and clear the queue"),
    CommandSpec("/shuffle", "[on|off]", "random order on/off (Ctrl+S)"),
    CommandSpec("/repeat", "[off|all|one]", "repeat the queue/playlist or the song (Ctrl+R)"),
    CommandSpec("/seek", "<±sec|m:ss>", "seek, e.g. +30, -10, 1:20"),
    CommandSpec("/vol", "<0-100|±n>", "set the volume"),
    CommandSpec("/add", "<path|url>", "upload files or import a link"),
    CommandSpec("/jobs", "", "recent imports"),
    CommandSpec("/jobs retry", "<n>", "retry a failed import"),
    CommandSpec("/jobs cancel", "<n>", "cancel an import (or the rest of a playlist)"),
    CommandSpec("/edit", "<n>", "edit title / artist / album of a listed track"),
    CommandSpec("/delete", "<n>", "delete a listed track from the server"),
    CommandSpec("/library", "", "browse mode (same as Ctrl+B)"),
    CommandSpec("/settings", "", "theme, keys, playback options (F2)"),
    CommandSpec("/login", "", "log in / change server"),
    CommandSpec("/logout", "", "revoke and forget the token"),
    CommandSpec("/clear", "", "clear the screen"),
    CommandSpec("/help", "", "show help"),
    CommandSpec("/quit", "", "exit (Ctrl+Q)"),
]


def match_commands(value: str) -> list[CommandSpec]:
    """Commands to suggest for what has been typed so far ("" -> none)."""
    v = value.lower()
    if not v.startswith("/"):
        return []
    prefix = [c for c in COMMAND_SPECS if c.name.startswith(v)]
    if prefix:
        return prefix
    if " " in v:
        return []  # typing arguments: nothing to complete
    needle = v[1:]
    return [c for c in COMMAND_SPECS if needle and needle in c.name]
