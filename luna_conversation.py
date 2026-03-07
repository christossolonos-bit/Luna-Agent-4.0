"""
Luna conversation history: persistent per-scope so context survives app restarts.
Stored in data/luna_conversations.json; loaded when building context for Ollama.
"""
import json
from datetime import datetime
from pathlib import Path

from luna_memory import MEMORY_DIR

CONVERSATION_FILE = MEMORY_DIR / "luna_conversations.json"
MAX_MESSAGES_PER_SCOPE = 200  # keep last N messages (100 exchanges)


def _load_all() -> dict:
    """Load full store: { scope: [ { role, content, created_at }, ... ] }."""
    if not CONVERSATION_FILE.is_file():
        return {}
    try:
        with open(CONVERSATION_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_all(data: dict) -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONVERSATION_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_recent_conversation(scope: str, max_messages: int = 20) -> list[dict]:
    """
    Return the most recent messages for this scope (oldest first) for API context.
    Each item is {"role": "user"|"assistant", "content": "..."}. created_at is not included in output.
    """
    data = _load_all()
    messages = data.get(scope)
    if not isinstance(messages, list):
        return []
    # Trim to last max_messages, then return in chronological order
    recent = messages[-max_messages:]
    return [{"role": m.get("role", "user"), "content": (m.get("content") or "").strip()} for m in recent if (m.get("role") and (m.get("content") or "").strip())]


def append_exchange(scope: str, user_message: str, assistant_reply: str) -> None:
    """Append one user message and one assistant reply to the scope's conversation. Trims store to MAX_MESSAGES_PER_SCOPE."""
    if not scope:
        return
    user_message = (user_message or "").strip()
    assistant_reply = (assistant_reply or "").strip()
    now = datetime.utcnow().isoformat() + "Z"
    data = _load_all()
    messages = list(data.get(scope) or [])
    if not isinstance(messages, list):
        messages = []
    messages.append({"role": "user", "content": user_message, "created_at": now})
    messages.append({"role": "assistant", "content": assistant_reply, "created_at": now})
    data[scope] = messages[-MAX_MESSAGES_PER_SCOPE:]
    _save_all(data)


def merge_conversations(target_scope: str, source_scopes: list[str]) -> int:
    """
    Merge conversation messages from source scopes into target scope.
    De-duplicates by (role, content, created_at), keeps chronological order and trim.
    Returns number of messages added.
    """
    if not target_scope or not source_scopes:
        return 0
    data = _load_all()
    target = list(data.get(target_scope) or [])
    if not isinstance(target, list):
        target = []
    before_len = len(target)

    seen = {
        (
            (m.get("role") or "").strip(),
            (m.get("content") or "").strip(),
            (m.get("created_at") or "").strip(),
        )
        for m in target
        if isinstance(m, dict)
    }

    for src in source_scopes:
        if not src or src == target_scope:
            continue
        msgs = data.get(src)
        if not isinstance(msgs, list):
            continue
        for m in msgs:
            if not isinstance(m, dict):
                continue
            key = (
                (m.get("role") or "").strip(),
                (m.get("content") or "").strip(),
                (m.get("created_at") or "").strip(),
            )
            if not key[0] or not key[1] or key in seen:
                continue
            target.append(
                {
                    "role": key[0],
                    "content": key[1],
                    "created_at": key[2] or datetime.utcnow().isoformat() + "Z",
                }
            )
            seen.add(key)

    target = sorted(target, key=lambda m: (m.get("created_at") or ""))
    data[target_scope] = target[-MAX_MESSAGES_PER_SCOPE:]
    _save_all(data)
    return max(0, len(data[target_scope]) - before_len)
