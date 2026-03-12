"""
Shadow — Command executor. No memory, no chat. User says "Shadow, do X"; Luna routes here;
Shadow parses and runs the command, returns result. Keeps Luna focused on chat + context.
"""
from __future__ import annotations

from typing import Callable

SHADOW_WAKE = "shadow"


def strip_shadow_prefix(message: str) -> str | None:
    """
    If message starts with "Shadow" (case-insensitive) followed by comma/space, return the rest.
    Otherwise return None (not a Shadow invocation).
    """
    if not message or not isinstance(message, str):
        return None
    s = message.strip()
    low = s.lower()
    if not low.startswith(SHADOW_WAKE):
        return None
    rest = s[len(SHADOW_WAKE) :].lstrip(" ,\t")
    return rest  # can be "" if they only said "Shadow"


def run_shadow(
    message: str,
    scope: str | None,
    parse_fn: Callable[[str], tuple[str, dict] | None],
    run_fn: Callable[[str, dict, str | None], str],
    *,
    permission_fn: Callable[[str, int], bool] | None = None,
    author_id: int | None = None,
    log_fn: Callable[[str, dict, str], None] | None = None,
) -> str:
    """
    Parse the command from message (after "Shadow" is stripped) and execute it.
    parse_fn(msg) -> (cmd, params) | None
    run_fn(cmd, params, scope) -> reply str
    On Discord, pass permission_fn and author_id to enforce admin/linked checks.
    If log_fn is provided, call it with (cmd, params, reply) after running (for action log).
    """
    msg = (message or "").strip()
    if not msg:
        return "Say what you want Shadow to do (e.g. Shadow, share a song on X)."
    parsed = parse_fn(msg)
    if not parsed:
        return "Shadow didn't understand that. Try: share on X, message Marios, news, search for …, suno, run script.py, etc."
    cmd, params = parsed
    if permission_fn is not None and author_id is not None and not permission_fn(cmd, author_id):
        return "You don't have permission to use that command here."
    reply = run_fn(cmd, params, scope)
    if log_fn is not None:
        try:
            log_fn(cmd, params, reply)
        except Exception:
            pass
    return reply
