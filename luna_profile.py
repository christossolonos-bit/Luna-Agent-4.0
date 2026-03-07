"""
Luna user profile: permanent structured profile per user/scope.
Luna can ask the user for missing fields; answers are stored here and injected into the system prompt.
"""
import json
import re
from pathlib import Path

from luna_memory import MEMORY_DIR

PROFILE_FILE = MEMORY_DIR / "luna_profile.json"

# Standard profile fields (labels for prompt). "other" can hold free-form notes.
PROFILE_FIELDS = ["name", "location", "occupation", "interests", "birthday", "other"]


def _load_all() -> dict:
    """Load profiles: { scope: { field: value, ... } }."""
    if not PROFILE_FILE.is_file():
        return {}
    try:
        with open(PROFILE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_all(data: dict) -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    with open(PROFILE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_profile(scope: str) -> dict:
    """Return the profile dict for this scope. Keys are PROFILE_FIELDS; missing keys have empty string."""
    data = _load_all()
    raw = data.get(scope)
    if not isinstance(raw, dict):
        raw = {}
    return {f: (raw.get(f) or "").strip() for f in PROFILE_FIELDS}


def set_profile_field(scope: str, field: str, value: str) -> None:
    """Set one profile field. Field must be in PROFILE_FIELDS; value is trimmed (max 500 chars)."""
    field = (field or "").strip().lower()
    if field not in PROFILE_FIELDS:
        return
    value = (value or "").strip()[:500]
    data = _load_all()
    scope_data = data.setdefault(scope, {})
    if not isinstance(scope_data, dict):
        scope_data = {}
    scope_data[field] = value
    data[scope] = {f: (scope_data.get(f) or "").strip() for f in PROFILE_FIELDS}
    _save_all(data)


def get_profile_prompt(scope: str) -> str:
    """Format profile for system prompt. Asks Luna to fill missing fields by asking the user."""
    profile = get_profile(scope)
    filled = [(k, v) for k, v in profile.items() if v]
    is_discord = scope.startswith("discord")
    header = "Profile for this user (each Discord user has their own; ask for missing fields when relevant):" if is_discord else "User profile (permanent; ask the user to fill in when you don't know):"
    if not filled:
        return (
            f"{header}\n"
            "  name, location, occupation, interests, birthday, other — all empty. "
            "Politely ask the user for their name and other details when relevant."
        )
    lines = [f"  {k}: {v}" for k, v in profile.items() if v]
    missing = [k for k in PROFILE_FIELDS if not (profile.get(k) or "").strip()]
    hint = ""
    if missing:
        hint = " Ask the user for: " + ", ".join(missing) + "." if len(missing) <= 4 else ""
    return header + "\n" + "\n".join(lines) + hint


def clear_profile(scope: str) -> int:
    """Clear all profile fields for this scope. Returns number of fields cleared."""
    data = _load_all()
    if scope not in data:
        return 0
    n = len([v for v in (data[scope] or {}).values() if v]) if isinstance(data[scope], dict) else 0
    data[scope] = {f: "" for f in PROFILE_FIELDS}
    _save_all(data)
    return n


# Patterns: (field, regex that matches Luna's question)
# If the last assistant message matches and user reply is short, set profile[field] = user_reply
PROFILE_QUESTION_PATTERNS = [
    ("name", r"(?:what'?s|what is|may i have|can i get|tell me)\s+(?:your\s+)?name\??\s*$"),
    ("name", r"(?:who are you|who am i)\s+(?:talking to|speaking to)\??\s*$"),
    ("location", r"(?:where\s+do you live|where are you from|what'?s your (?:city|location))\??\s*$"),
    ("location", r"(?:where\s+(?:are you|do you)\s+(?:based|located))\??\s*$"),
    ("occupation", r"(?:what do you do|what'?s your (?:job|occupation|work)|do you work)\??\s*$"),
    ("occupation", r"(?:are you a|what kind of work)\s+"),
    ("interests", r"(?:what are your (?:interests|hobbies)|what do you like to do)\??\s*$"),
    ("birthday", r"(?:when is your birthday|when were you born|what'?s your (?:birth ?day|dob))\??\s*$"),
]


def try_capture_profile_from_reply(
    scope: str, last_assistant_message: str, user_reply: str
) -> bool:
    """
    If last assistant message looks like a profile question and user gave a short answer, set that profile field.
    Returns True if a field was set.
    """
    if not scope or not last_assistant_message or not user_reply:
        return False
    reply = (user_reply or "").strip()
    if len(reply) > 300 or len(reply) < 1:
        return False
    # Take last sentence or line of assistant message (the question)
    last_line = (last_assistant_message or "").strip().split("\n")[-1].strip()
    for field, pattern in PROFILE_QUESTION_PATTERNS:
        if re.search(pattern, last_line, re.IGNORECASE):
            set_profile_field(scope, field, reply)
            return True
    return False


def merge_profiles(target_scope: str, source_scopes: list[str]) -> int:
    """
    Merge profile fields from source scopes into target scope.
    Existing non-empty target fields are preserved.
    Returns number of fields updated on target.
    """
    if not target_scope or not source_scopes:
        return 0
    data = _load_all()
    target_raw = data.get(target_scope)
    if not isinstance(target_raw, dict):
        target_raw = {}
    current = {f: (target_raw.get(f) or "").strip() for f in PROFILE_FIELDS}
    before = dict(current)

    def _score(field: str, value: str) -> int:
        v = (value or "").strip()
        if not v:
            return -999
        t = v.lower()
        score = 0
        if len(v) <= 120:
            score += 2
        else:
            score -= 4
        # Common bad captures from free-form chats or questions
        bad_snippets = (
            "tell me what you know",
            "what do you know about me",
            "thinking of how to automate you",
            "something drastic like",
            "i don't know",
            "i dont know",
        )
        if any(s in t for s in bad_snippets):
            score -= 12
        if field == "name":
            if re.fullmatch(r"[A-Za-z][A-Za-z .'-]{0,40}", v) and len(v.split()) <= 4:
                score += 12
            if len(v.split()) > 4:
                score -= 8
        elif field == "birthday":
            if re.search(r"\d", t):
                score += 4
            if any(m in t for m in ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")):
                score += 2
        elif field in ("location", "occupation", "interests"):
            if "?" in v:
                score -= 4
        return score

    merged = dict(current)
    for f in PROFILE_FIELDS:
        best_val = merged.get(f, "")
        best_score = _score(f, best_val)
        for src in source_scopes:
            if not src or src == target_scope:
                continue
            src_raw = data.get(src)
            if not isinstance(src_raw, dict):
                continue
            candidate = (src_raw.get(f) or "").strip()[:500]
            s = _score(f, candidate)
            if s > best_score:
                best_val = candidate
                best_score = s
        merged[f] = best_val

    data[target_scope] = merged
    _save_all(data)
    return sum(1 for f in PROFILE_FIELDS if merged.get(f) != before.get(f))
