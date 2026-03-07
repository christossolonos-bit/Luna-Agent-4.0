"""
Luna memory: 4-layer persistent memory per user/scope.
Stored in a JSON file; injected into the system prompt so Luna "remembers".

Layers:
  1. Core      — Essential facts (name, always-remember). Max 10, always in prompt first.
  2. Long-term — Stored facts over time. Many, up to 20 in prompt (excluding recent 5).
  3. Short-term — Last 5 memories added (recent). Subset of long-term by recency.
  4. Working   — Current conversation (message_history). Not stored; passed at call time.
"""
import json
from datetime import datetime
from pathlib import Path

_base = Path(__file__).resolve().parent
MEMORY_DIR = _base / "data"
MEMORY_FILE = MEMORY_DIR / "luna_memory.json"

MAX_CORE_PER_SCOPE = 10
MAX_LONG_TERM_PER_SCOPE = 100
SHORT_TERM_COUNT = 5
LONG_TERM_IN_PROMPT = 20


def _load_all() -> dict:
    """Load full memory store. Per-scope: { core: [], long_term: [] }."""
    if not MEMORY_FILE.is_file():
        return {}
    try:
        with open(MEMORY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        # Migrate old format: scope -> list  =>  scope -> { long_term: list, core: [] }
        for scope, val in list(data.items()):
            if isinstance(val, list):
                data[scope] = {"core": [], "long_term": val}
            elif not isinstance(val, dict):
                data[scope] = {"core": [], "long_term": []}
            else:
                data[scope] = {"core": val.get("core") or [], "long_term": val.get("long_term") or []}
        return data
    except Exception:
        return {}


def _save_all(data: dict) -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _entries_to_texts(entries: list, limit: int, newest_first: bool = True) -> list[str]:
    """Convert list of {content, created_at} to list of content strings."""
    if not isinstance(entries, list):
        return []
    sorted_entries = sorted(entries, key=lambda x: x.get("created_at", ""), reverse=newest_first)
    texts = []
    for e in sorted_entries[:limit]:
        if isinstance(e, dict) and e.get("content"):
            texts.append(e["content"].strip())
        elif isinstance(e, str):
            texts.append(e.strip())
    return [t for t in texts if t]


# --- Layer 1: Core ---

def get_core_memories(scope: str) -> list[str]:
    """Layer 1: Core memories (essential facts). Max 10."""
    data = _load_all()
    scope_data = data.get(scope)
    if not scope_data or not isinstance(scope_data, dict):
        return []
    return _entries_to_texts(scope_data.get("core") or [], MAX_CORE_PER_SCOPE)


def add_core_memory(scope: str, content: str) -> None:
    """Add to Layer 1 (core). Trims; avoids duplicate; caps at MAX_CORE_PER_SCOPE."""
    content = (content or "").strip()
    if not content or len(content) > 2000:
        return
    data = _load_all()
    scope_data = data.setdefault(scope, {"core": [], "long_term": []})
    core = list(scope_data.get("core") or [])
    if not isinstance(core, list):
        core = []
    existing = {e.get("content", "").strip() if isinstance(e, dict) else str(e).strip() for e in core}
    if content in existing:
        return
    core.append({"content": content, "created_at": datetime.utcnow().isoformat() + "Z"})
    scope_data["core"] = core[-MAX_CORE_PER_SCOPE:]
    data[scope] = scope_data
    _save_all(data)


def clear_core_memories(scope: str) -> int:
    """Clear Layer 1 (core) for this scope. Returns count removed."""
    data = _load_all()
    scope_data = data.get(scope)
    if not scope_data or not isinstance(scope_data, dict):
        return 0
    core = scope_data.get("core") or []
    n = len(core) if isinstance(core, list) else 0
    scope_data["core"] = []
    data[scope] = scope_data
    _save_all(data)
    return n


# --- Layers 2 & 3: Long-term and Short-term (same store, different recall) ---

def get_long_term_memories(scope: str, limit: int = LONG_TERM_IN_PROMPT) -> list[str]:
    """Layer 2: Long-term memories. Returns most recent entries, excluding the last SHORT_TERM_COUNT."""
    data = _load_all()
    scope_data = data.get(scope)
    if not scope_data or not isinstance(scope_data, dict):
        return []
    entries = scope_data.get("long_term") or []
    if not isinstance(entries, list):
        return []
    sorted_entries = sorted(entries, key=lambda x: x.get("created_at", ""), reverse=True)
    # Skip the first SHORT_TERM_COUNT (those are short-term); take next `limit`
    long_term_only = sorted_entries[SHORT_TERM_COUNT : SHORT_TERM_COUNT + limit]
    return _entries_to_texts(long_term_only, limit)


def get_short_term_memories(scope: str, limit: int = SHORT_TERM_COUNT) -> list[str]:
    """Layer 3: Short-term (recent) memories. Last `limit` added to long_term."""
    data = _load_all()
    scope_data = data.get(scope)
    if not scope_data or not isinstance(scope_data, dict):
        return []
    entries = scope_data.get("long_term") or []
    if not isinstance(entries, list):
        return []
    sorted_entries = sorted(entries, key=lambda x: x.get("created_at", ""), reverse=True)
    return _entries_to_texts(sorted_entries[:limit], limit)


def get_memories(scope: str, limit: int = 25) -> list[str]:
    """All recallable memories (core + short-term + long-term) as one list, for !memories display.
    Order: core first, then short-term (recent 5), then long-term.
    """
    core = get_core_memories(scope)
    short = get_short_term_memories(scope)
    long_term = get_long_term_memories(scope, limit=limit)
    seen = set()
    out = []
    for t in core + short + long_term:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out[: limit + MAX_CORE_PER_SCOPE + SHORT_TERM_COUNT]


def get_memory_prompt(scope: str) -> str:
    """Format all 4 layers for system prompt. Layer 4 (working) is noted; actual working memory is message_history at call time."""
    core = get_core_memories(scope)
    short = get_short_term_memories(scope)
    long_term = get_long_term_memories(scope)

    parts = []
    if core:
        parts.append("Layer 1 – Core (essential, always remember):\n" + "\n".join("- " + c for c in core))
    if long_term:
        parts.append("Layer 2 – Long-term:\n" + "\n".join("- " + c for c in long_term))
    if short:
        parts.append("Layer 3 – Short-term (recently mentioned):\n" + "\n".join("- " + c for c in short))
    parts.append("Layer 4 – Working: Use the current conversation (the messages above) as immediate context.")
    return "\n\n".join(parts)


def add_memory(scope: str, content: str) -> None:
    """Append to long-term (Layer 2/3). Trims and dedupes; caps total per scope."""
    content = (content or "").strip()
    if not content or len(content) > 2000:
        return
    data = _load_all()
    scope_data = data.setdefault(scope, {"core": [], "long_term": []})
    long_term = list(scope_data.get("long_term") or [])
    if not isinstance(long_term, list):
        long_term = []
    existing = {e.get("content", "").strip() if isinstance(e, dict) else str(e).strip() for e in long_term}
    if content in existing:
        return
    long_term.append({"content": content, "created_at": datetime.utcnow().isoformat() + "Z"})
    scope_data["long_term"] = long_term[-MAX_LONG_TERM_PER_SCOPE:]
    data[scope] = scope_data
    _save_all(data)


def clear_memories(scope: str) -> int:
    """Clear long-term (and thus short-term) for this scope. Returns count removed. Does not clear core."""
    data = _load_all()
    scope_data = data.get(scope)
    if not scope_data or not isinstance(scope_data, dict):
        return 0
    long_term = scope_data.get("long_term") or []
    n = len(long_term) if isinstance(long_term, list) else 0
    scope_data["long_term"] = []
    data[scope] = scope_data
    _save_all(data)
    return n


def clear_all_memories(scope: str) -> tuple[int, int]:
    """Clear both core and long-term for this scope. Returns (core_count, long_term_count)."""
    nc = clear_core_memories(scope)
    nl = clear_memories(scope)
    return nc, nl


def merge_memories(target_scope: str, source_scopes: list[str]) -> tuple[int, int]:
    """
    Merge core + long-term memories from source scopes into target scope.
    De-duplicates by memory text content.
    Returns (core_added, long_term_added).
    """
    if not target_scope or not source_scopes:
        return 0, 0

    data = _load_all()
    target_data = data.get(target_scope)
    if not isinstance(target_data, dict):
        target_data = {"core": [], "long_term": []}

    target_core = list(target_data.get("core") or [])
    target_long = list(target_data.get("long_term") or [])
    core_seen = {
        (e.get("content", "").strip() if isinstance(e, dict) else str(e).strip())
        for e in target_core
        if (e.get("content", "").strip() if isinstance(e, dict) else str(e).strip())
    }
    long_seen = {
        (e.get("content", "").strip() if isinstance(e, dict) else str(e).strip())
        for e in target_long
        if (e.get("content", "").strip() if isinstance(e, dict) else str(e).strip())
    }
    core_added = 0
    long_added = 0

    for src in source_scopes:
        if not src or src == target_scope:
            continue
        src_data = data.get(src)
        if not isinstance(src_data, dict):
            continue

        for e in list(src_data.get("core") or []):
            text = (e.get("content", "").strip() if isinstance(e, dict) else str(e).strip())
            if not text or text in core_seen:
                continue
            target_core.append(e if isinstance(e, dict) else {"content": text, "created_at": datetime.utcnow().isoformat() + "Z"})
            core_seen.add(text)
            core_added += 1

        for e in list(src_data.get("long_term") or []):
            text = (e.get("content", "").strip() if isinstance(e, dict) else str(e).strip())
            if not text or text in long_seen:
                continue
            target_long.append(e if isinstance(e, dict) else {"content": text, "created_at": datetime.utcnow().isoformat() + "Z"})
            long_seen.add(text)
            long_added += 1

    target_data["core"] = sorted(target_core, key=lambda x: x.get("created_at", "") if isinstance(x, dict) else "")[-MAX_CORE_PER_SCOPE:]
    target_data["long_term"] = sorted(target_long, key=lambda x: x.get("created_at", "") if isinstance(x, dict) else "")[-MAX_LONG_TERM_PER_SCOPE:]
    data[target_scope] = target_data
    _save_all(data)
    return core_added, long_added
