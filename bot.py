"""
Luna — Discord chatbot (Python) with Ollama (e.g. qwen2.5-coder:7b-instruct).
Responds when mentioned or in DMs. Add your token to .env and run: python bot.py

Or pass token on command line: python bot.py YOUR_TOKEN

Also runs a web UI at http://127.0.0.1:5050 — Jarvis-style chat in the browser.
"""
import asyncio
import base64
import ctypes
import html
import io
import json
import os
import random
import re
import subprocess
import uuid
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import discord
from discord.ext import commands
from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context

from luna_files import REPO_ROOT as LUNA_REPO_ROOT, write_file as luna_write_file  # agents .py writes go under Luna projects/agents
from shadow_agent import strip_shadow_prefix, run_shadow as shadow_run
import celine  # Sub-agent for voice clips: transcribe and decide Shadow vs Luna


def _open_file_by_path(absolute_path: str) -> bool:
    """Open a file by absolute path. On Windows uses Notepad so code opens in Notepad and can be saved as HTML in Luna projects."""
    if not absolute_path or not os.path.isfile(absolute_path):
        return False
    try:
        if sys.platform == "win32":
            subprocess.Popen(["notepad", absolute_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", absolute_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


from luna_memory import (
    get_memory_prompt,
    get_memories,
    get_core_memories,
    get_short_term_memories,
    get_long_term_memories,
    add_memory,
    add_core_memory,
    clear_memories,
    clear_core_memories,
    clear_all_memories,
    merge_memories,
)
from luna_brain import brain_step, brain_should_remember
from luna_profile import (
    get_profile_prompt,
    get_profile,
    set_profile_field,
    try_capture_profile_from_reply,
    clear_profile,
    PROFILE_FIELDS,
    merge_profiles,
)
from luna_conversation import get_recent_conversation, append_exchange, merge_conversations, get_recent_user_messages, count_user_messages

# Ollama: two modes — Luna (chat) uses OLLAMA_CHAT_MODEL (e.g. Llama 3.2); Shadow (commands, code) uses OLLAMA_MODEL (Qwen 2.5 Coder).
OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b-instruct")  # Shadow: commands, create code, agents
OLLAMA_CHAT_MODEL = os.environ.get("OLLAMA_CHAT_MODEL", "llama3.2:latest").strip() or "llama3.2:latest"  # Luna: normal chatting

LUNA_SYSTEM_PROMPT = """You are Luna, a friendly AI companion. You're warm, helpful, and a bit playful. Keep replies concise (a few sentences). Speak in first person as Luna.
You have a permanent user profile (name, location, occupation, interests, birthday). When you don't know a profile field, politely ask the user; their answers are saved automatically. You remember things the user tells you using your 4-layer memory."""

# SOUL.md + TOOLS.md + skills (OpenClaw-style): loaded from data/ and injected into the system prompt.
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_REMINDERS_FILE = os.path.join(_DATA_DIR, "reminders.json")
_reminders_lock = threading.Lock()
_SOUL_PATH = os.path.join(_DATA_DIR, "SOUL.md")
_TOOLS_PATH = os.path.join(_DATA_DIR, "TOOLS.md")
_OBJECTIVES_PATH = os.path.join(_DATA_DIR, "OBJECTIVES.md")
_SKILLS_DIR = os.path.join(_DATA_DIR, "skills")
# Cache identity file contents (SOUL, TOOLS, OBJECTIVES, skills) to reduce disk I/O and speed up replies.
_IDENTITY_CACHE_TTL = 30  # seconds
_identity_cache: dict = {}
_identity_cache_lock = threading.Lock()


def _load_soul_content() -> str:
    """Load SOUL.md (identity/personality). Edit this file to change who Luna is."""
    try:
        if os.path.isfile(_SOUL_PATH):
            with open(_SOUL_PATH, encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return ""


def _load_tools_content() -> str:
    """Load TOOLS.md (list of commands and when to use them). Edit to document Luna's tools."""
    try:
        if os.path.isfile(_TOOLS_PATH):
            with open(_TOOLS_PATH, encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return ""


def _load_objectives_content() -> str:
    """Load OBJECTIVES.md (objectives and constraints Luna must follow). Edit to add rules."""
    try:
        if os.path.isfile(_OBJECTIVES_PATH):
            with open(_OBJECTIVES_PATH, encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return ""


def _get_identity_file_hint() -> str:
    """Return a hint for Luna when SOUL/TOOLS/OBJECTIVES are missing or short: she can ask the user and record with LUNA_RECORD_*."""
    missing = []
    if len(_load_soul_content()) < 50:
        missing.append("SOUL.md (who you are / personality)")
    if len(_load_tools_content()) < 50:
        missing.append("TOOLS.md (what you can do)")
    if len(_load_objectives_content()) < 50:
        missing.append("OBJECTIVES.md (rules to follow)")
    if not missing:
        return ""
    return (
        "The following identity files are empty or very short: " + ", ".join(missing) + ". "
        "You can ask the user what to put in each. When you want to save their *next* reply into a file, end your message with exactly one of these on its own line: LUNA_RECORD_SOUL, LUNA_RECORD_TOOLS, or LUNA_RECORD_OBJECTIVES. "
        "Their next message will then be saved to that file (like when you gather profile info)."
    )


def _load_skills_content() -> str:
    """Load all .md files from data/skills/ and return a single block. Each file is a skill Luna can follow when relevant."""
    out = []
    try:
        if not os.path.isdir(_SKILLS_DIR):
            return ""
        for name in sorted(os.listdir(_SKILLS_DIR)):
            if not name.endswith(".md"):
                continue
            path = os.path.join(_SKILLS_DIR, name)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read().strip()
                if text:
                    skill_name = name[:-3].replace("_", " ").replace("-", " ").title()
                    out.append(f"### Skill: {skill_name}\n{text}")
            except Exception:
                continue
        if not out:
            return ""
        return "\n\n---\n\n".join(out)
    except Exception:
        return ""


def _get_effective_system_prompt(base: str) -> str:
    """Inject SOUL (identity), TOOLS (capabilities), objectives (constraints), and skills (markdown) into the base system prompt. Caches file contents for 30s to reduce I/O."""
    now = time.time()
    with _identity_cache_lock:
        if _identity_cache and (now - _identity_cache.get("ts", 0)) < _IDENTITY_CACHE_TTL:
            soul = _identity_cache.get("soul", "")
            tools = _identity_cache.get("tools", "")
            objectives = _identity_cache.get("objectives", "")
            skills = _identity_cache.get("skills", "")
        else:
            soul = _load_soul_content()
            tools = _load_tools_content()
            objectives = _load_objectives_content()
            skills = _load_skills_content()
            _identity_cache.update({"soul": soul, "tools": tools, "objectives": objectives, "skills": skills, "ts": now})
    parts = []
    if soul:
        parts.append(soul)
    parts.append(base.strip())
    if tools:
        parts.append("---\nTools and commands (use these when the user asks):\n" + tools)
    if objectives:
        parts.append("---\nObjectives and constraints (follow these):\n" + objectives)
    if skills:
        parts.append("---\nSkills (follow when relevant to the user's request):\n" + skills)
    return "\n\n".join(parts)


def _invalidate_identity_cache() -> None:
    """Call after SOUL/TOOLS/OBJECTIVES are updated so the next reply uses fresh content."""
    with _identity_cache_lock:
        _identity_cache.clear()


_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _read_token_from_env_file(path: str) -> str:
    """Read DISCORD_TOKEN directly from .env, strip BOM and hidden chars."""
    try:
        with open(path, encoding="utf-8-sig") as f:  # utf-8-sig strips BOM
            for line in f:
                line = line.strip()
                if line.startswith("DISCORD_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    return token.split("\n")[0].split("\r")[0].strip()
    except Exception:
        pass
    return ""


# Load .env so OLLAMA_* etc. are set (use utf-8-sig to avoid BOM issues)
try:
    with open(_env_path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")
except Exception:
    pass
load_dotenv(_env_path)

# If token passed on command line (python bot.py TOKEN), use only that
if len(sys.argv) > 1:
    DISCORD_TOKEN = sys.argv[1].strip().strip('"').strip("'")
    DISCORD_TOKEN = DISCORD_TOKEN.split("\n")[0].split("\r")[0].strip()
    print("Using token from command line (ignoring .env)")
else:
    DISCORD_TOKEN = _read_token_from_env_file(_env_path)
    if not DISCORD_TOKEN:
        DISCORD_TOKEN = (os.environ.get("DISCORD_TOKEN") or "").strip().strip('"').strip("'")
        DISCORD_TOKEN = DISCORD_TOKEN.split("\n")[0].split("\r")[0].strip()

OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b-instruct")
# Small/fast model for "employee" tasks (NL parse, summarization). If unset, use main model.
OLLAMA_MODEL_SMALL = os.environ.get("OLLAMA_MODEL_SMALL", "").strip() or OLLAMA_MODEL

# --- Luna as Boss + Employees (hierarchy) ---
# Boss: Luna. She routes each message and delegates to employees for small tasks; she does the main chat herself.
# Employee names (who does what, who uses them):
#
#   COMMANDER  — Parses natural language into command + params (e.g. "message Marios" -> {command: "msg", contact: "Marios"}).
#                Model: OLLAMA_MODEL_SMALL. Used by: Boss in api_chat and on_message when message looks like a command.
#
#   SCRIBE     — Summarizes long conversation into 2–4 sentences (for compaction).
#                Model: OLLAMA_MODEL_SMALL. Used by: Boss inside _compact_conversation_history.
#
#   LUNA       — Main chat reply (Boss herself). Model: OLLAMA_MODEL. Streams on web.
#                Used by: Boss in api_chat and on_message for the actual reply.
#
#   SEARCH_PICKER — Pick best search result + short reason. Model: OLLAMA_MODEL_SMALL. Used by: Boss in _open_google_search.
#   COPYWRITER   — Short copy: WhatsApp message from context, YouTube comment from transcript/context. Model: OLLAMA_MODEL. Used by: msg flow, YT comment flow.
#   RECEPTIONIST — Return "what can you do" / commands help (no Ollama). Used by: Boss when user asks for help.
#   NEWSROOM     — Fetch and format world news (no Ollama). Used by: Boss when user says news.
#
EMPLOYEE_COMMANDER = "Commander"
EMPLOYEE_SCRIBE = "Scribe"
EMPLOYEE_LUNA = "Luna"
EMPLOYEE_SEARCH_PICKER = "SearchPicker"
EMPLOYEE_COPYWRITER = "Copywriter"
EMPLOYEE_RECEPTIONIST = "Receptionist"
EMPLOYEE_NEWSROOM = "Newsroom"

# Discord: only this user ID can create/read/write/list/edit files. Others can chat only (no file execution).
DISCORD_ADMIN_ID = (os.environ.get("DISCORD_ADMIN_ID") or "").strip()
try:
    _discord_admin_id_int = int(DISCORD_ADMIN_ID) if DISCORD_ADMIN_ID else None
except ValueError:
    _discord_admin_id_int = None
# Web UI linked to this Discord user (Chris/Solonaras): same memory, profile, and conversation on both platforms.
LINKED_DISCORD_USER_ID = (os.environ.get("LINKED_DISCORD_USER_ID") or "1414944231222411378").strip()
try:
    _linked_discord_id_int = int(LINKED_DISCORD_USER_ID) if LINKED_DISCORD_USER_ID else None
except ValueError:
    _linked_discord_id_int = None
LINKED_SCOPE = f"discord:user:{LINKED_DISCORD_USER_ID}" if LINKED_DISCORD_USER_ID else ""
# Discord-only scope sync (DM + server chat) for specific users.
# This does NOT link to web scope; it only unifies Discord DM/guild history for those user IDs.
DISCORD_DM_SYNC_USER_IDS = (os.environ.get("DISCORD_DM_SYNC_USER_IDS") or "550782786013757442").strip()
_discord_dm_sync_ids_int: set[int] = set()
for _part in DISCORD_DM_SYNC_USER_IDS.split(","):
    _id_txt = (_part or "").strip()
    if not _id_txt:
        continue
    try:
        _discord_dm_sync_ids_int.add(int(_id_txt))
    except ValueError:
        continue
# Text channel IDs where Luna auto-joins voice and speaks (TTS) when she replies. Empty = no auto-join; Luna only speaks in VC if someone used !join. Comma-separated.
DISCORD_TTS_CHANNEL_IDS = (os.environ.get("DISCORD_TTS_CHANNEL_IDS") or "").strip()
_discord_tts_channel_ids: set[int] = set()
for _part in DISCORD_TTS_CHANNEL_IDS.split(","):
    _id_txt = (_part or "").strip()
    if not _id_txt:
        continue
    try:
        _discord_tts_channel_ids.add(int(_id_txt))
    except ValueError:
        continue
SUNO_CREATE_URL = os.environ.get("SUNO_CREATE_URL", "https://suno.com/create").strip() or "https://suno.com/create"
SUNO_PROFILE_DIR = os.environ.get(
    "SUNO_PROFILE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "suno_profile"),
).strip()
# Prefer a real installed browser for OAuth pages (Google often blocks automated bundled Chromium).
# Optional .env:
#   SUNO_BROWSER_CHANNEL=chrome   (or msedge)
#   SUNO_BROWSER_PATH=C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe
SUNO_BROWSER_CHANNEL = (os.environ.get("SUNO_BROWSER_CHANNEL") or "chrome").strip().lower()
SUNO_BROWSER_PATH = (os.environ.get("SUNO_BROWSER_PATH") or "").strip()
YOUTUBE_CHANNEL_URL = (
    os.environ.get("YOUTUBE_CHANNEL_URL") or "https://www.youtube.com/channel/UCqIjEHOABb8fwbKbjDhVRuA"
).strip()
YOUTUBE_CHANNEL_ID = (os.environ.get("YOUTUBE_CHANNEL_ID") or "UCqIjEHOABb8fwbKbjDhVRuA").strip()
YOUTUBE_FEED_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={YOUTUBE_CHANNEL_ID}"
WORLD_NEWS_FEEDS = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
]
X_COMPOSE_URL = (os.environ.get("X_COMPOSE_URL") or "https://x.com/compose/post").strip()
X_PROFILE_URL = (os.environ.get("X_PROFILE_URL") or "https://x.com/ChrisSolonos").strip()
X_HANDLE = (os.environ.get("X_HANDLE") or "@ChrisSolonos").strip()
X_PROFILE_DIR = os.environ.get(
    "X_PROFILE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "x_profile"),
).strip()
YOUTUBE_PROFILE_DIR = os.environ.get(
    "YOUTUBE_PROFILE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "youtube_profile"),
).strip()
YOUTUBE_BROWSER_CHANNEL = (os.environ.get("YOUTUBE_BROWSER_CHANNEL") or "chrome").strip().lower()
YOUTUBE_BROWSER_PATH = (os.environ.get("YOUTUBE_BROWSER_PATH") or "").strip()
INSTAGRAM_PROFILE_DIR = os.environ.get(
    "INSTAGRAM_PROFILE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "instagram_profile"),
).strip()
INSTAGRAM_BROWSER_CHANNEL = (os.environ.get("INSTAGRAM_BROWSER_CHANNEL") or "chrome").strip().lower()
INSTAGRAM_BROWSER_PATH = (os.environ.get("INSTAGRAM_BROWSER_PATH") or "").strip()
INSTAGRAM_BASE_URL = (os.environ.get("INSTAGRAM_BASE_URL") or "https://www.instagram.com").strip().rstrip("/")
# WhatsApp Web (Playwright): profile dir and browser; same first-login flow as Suno/X/Instagram
WHATSAPP_WEB_URL = (os.environ.get("WHATSAPP_WEB_URL") or "https://web.whatsapp.com").strip().rstrip("/")
WHATSAPP_WEB_PROFILE_DIR = os.environ.get(
    "WHATSAPP_WEB_PROFILE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "whatsapp_web_profile"),
).strip()
WHATSAPP_WEB_BROWSER_CHANNEL = (os.environ.get("WHATSAPP_WEB_BROWSER_CHANNEL") or "chrome").strip().lower()
WHATSAPP_WEB_BROWSER_PATH = (os.environ.get("WHATSAPP_WEB_BROWSER_PATH") or "").strip()
FACEBOOK_PROFILE_URL = (os.environ.get("FACEBOOK_PROFILE_URL") or "https://www.facebook.com/solonaras").strip()
FACEBOOK_HOME_URL = (os.environ.get("FACEBOOK_HOME_URL") or "https://www.facebook.com/").strip()
FACEBOOK_PROFILE_DIR = os.environ.get(
    "FACEBOOK_PROFILE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "facebook_profile"),
).strip()
# Facebook Messenger: open the chat window on Facebook.com (right-side panel) or use messenger.com
MESSENGER_OPEN_ON_FACEBOOK = os.environ.get("MESSENGER_OPEN_ON_FACEBOOK", "true").strip().lower() in ("1", "true", "yes")
MESSENGER_URL = (os.environ.get("MESSENGER_URL") or (FACEBOOK_HOME_URL if MESSENGER_OPEN_ON_FACEBOOK else "https://www.messenger.com")).strip().rstrip("/")
MESSENGER_PROFILE_DIR = os.environ.get(
    "MESSENGER_PROFILE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "messenger_profile"),
).strip()
MESSENGER_BROWSER_CHANNEL = (os.environ.get("MESSENGER_BROWSER_CHANNEL") or "chrome").strip().lower()
MESSENGER_BROWSER_PATH = (os.environ.get("MESSENGER_BROWSER_PATH") or "").strip()
_suno_bootstrap_lock = threading.Lock()
_suno_bootstrap_running = False
_suno_run_lock = threading.Lock()
_x_share_lock = threading.Lock()
_x_bootstrap_lock = threading.Lock()
_x_bootstrap_running = False
_fb_share_lock = threading.Lock()
_fb_bootstrap_lock = threading.Lock()
_fb_bootstrap_running = False
_yt_comment_lock = threading.Lock()
_yt_bootstrap_lock = threading.Lock()
_yt_bootstrap_running = False
_ig_dm_lock = threading.Lock()
_ig_bootstrap_lock = threading.Lock()
_ig_bootstrap_running = False
_wa_web_lock = threading.Lock()
_wa_web_bootstrap_lock = threading.Lock()
_wa_web_bootstrap_running = False
# Persist WhatsApp Web browser so the window stays open after !call / !msg
_wa_web_context = None
_wa_web_playwright = None


def _clear_wa_web_context() -> None:
    """Clear cached WhatsApp Web context so next use creates a fresh one (fixes 'cannot switch to a different thread' etc.)."""
    global _wa_web_context, _wa_web_playwright
    ctx, pw = _wa_web_context, _wa_web_playwright
    _wa_web_context = None
    _wa_web_playwright = None
    try:
        if ctx is not None:
            ctx.close()
    except Exception:
        pass
    try:
        if pw is not None:
            pw.stop()
    except Exception:
        pass
_messenger_lock = threading.Lock()
_messenger_bootstrap_lock = threading.Lock()
_messenger_bootstrap_running = False
_messenger_context = None
_messenger_playwright = None


def _collect_legacy_scopes_for_linked_user() -> list[str]:
    """Find legacy scopes for this linked user (web + old discord scope formats)."""
    if not LINKED_DISCORD_USER_ID:
        return []
    candidates = {f"discord:dm:{LINKED_DISCORD_USER_ID}", "web"}
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    files = [
        os.path.join(data_dir, "luna_profile.json"),
        os.path.join(data_dir, "luna_memory.json"),
        os.path.join(data_dir, "luna_conversations.json"),
    ]
    suffix = f":{LINKED_DISCORD_USER_ID}"
    for fp in files:
        try:
            if not os.path.isfile(fp):
                continue
            with open(fp, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                continue
            for scope in data.keys():
                if not isinstance(scope, str):
                    continue
                if scope == LINKED_SCOPE:
                    continue
                if scope == "web" or (scope.startswith("discord:") and scope.endswith(suffix)):
                    candidates.add(scope)
        except Exception:
            continue
    scopes = [s for s in candidates if s and s != LINKED_SCOPE]
    suffix = f":{LINKED_DISCORD_USER_ID}"
    # Prefer old Discord per-guild scope first, then DM, then web.
    def _priority(s: str) -> tuple[int, str]:
        if s.startswith("discord:") and s.endswith(suffix) and not s.startswith("discord:dm:"):
            return (0, s)
        if s.startswith("discord:dm:"):
            return (1, s)
        if s == "web":
            return (2, s)
        return (3, s)
    scopes.sort(key=_priority)
    return scopes


def _restore_linked_user_data_if_needed() -> None:
    """Merge legacy web/discord scopes into LINKED_SCOPE so profile/memory persist across migrations."""
    if not LINKED_SCOPE:
        return
    legacy_scopes = _collect_legacy_scopes_for_linked_user()
    if not legacy_scopes:
        return
    try:
        p = merge_profiles(LINKED_SCOPE, legacy_scopes)
        core_added, long_added = merge_memories(LINKED_SCOPE, legacy_scopes)
        conv_added = merge_conversations(LINKED_SCOPE, legacy_scopes)
        if p or core_added or long_added or conv_added:
            print(
                f"[Luna] Restored linked user data into {LINKED_SCOPE}: "
                f"profile_fields={p}, core_added={core_added}, long_added={long_added}, conv_added={conv_added}",
                flush=True,
            )
    except Exception:
        pass


_restore_linked_user_data_if_needed()


def _collect_legacy_scopes_for_discord_synced_user(discord_user_id: int) -> list[str]:
    """Find old DM/guild scopes for a Discord-only synced user."""
    if discord_user_id <= 0:
        return []
    target_scope = f"discord:user:{discord_user_id}"
    suffix = f":{discord_user_id}"
    candidates = {f"discord:dm:{discord_user_id}"}
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    files = [
        os.path.join(data_dir, "luna_profile.json"),
        os.path.join(data_dir, "luna_memory.json"),
        os.path.join(data_dir, "luna_conversations.json"),
    ]
    for fp in files:
        try:
            if not os.path.isfile(fp):
                continue
            with open(fp, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                continue
            for scope in data.keys():
                if not isinstance(scope, str):
                    continue
                if scope == target_scope:
                    continue
                if scope.startswith("discord:") and scope.endswith(suffix):
                    candidates.add(scope)
        except Exception:
            continue
    scopes = [s for s in candidates if s and s != target_scope]
    scopes.sort(key=lambda s: (0 if (s.startswith("discord:") and not s.startswith("discord:dm:")) else 1, s))
    return scopes


def _restore_discord_synced_user_data_if_needed() -> None:
    """Merge legacy Discord DM/guild scopes into unified Discord-only scope."""
    for user_id in _discord_dm_sync_ids_int:
        # Skip linked web+discord user; that one already has LINKED_SCOPE migration.
        if _linked_discord_id_int is not None and user_id == _linked_discord_id_int:
            continue
        target_scope = f"discord:user:{user_id}"
        legacy_scopes = _collect_legacy_scopes_for_discord_synced_user(user_id)
        if not legacy_scopes:
            continue
        try:
            p = merge_profiles(target_scope, legacy_scopes)
            core_added, long_added = merge_memories(target_scope, legacy_scopes)
            conv_added = merge_conversations(target_scope, legacy_scopes)
            if p or core_added or long_added or conv_added:
                print(
                    f"[Luna] Restored Discord-only sync data into {target_scope}: "
                    f"profile_fields={p}, core_added={core_added}, long_added={long_added}, conv_added={conv_added}",
                    flush=True,
                )
        except Exception:
            continue


_restore_discord_synced_user_data_if_needed()

if not DISCORD_TOKEN or DISCORD_TOKEN == "your_bot_token_here":
    print("Missing or placeholder DISCORD_TOKEN.")
    print("1. Open https://discord.com/developers/applications → Your App → Bot → Reset Token")
    print("2. Copy the token and put it in .env: DISCORD_TOKEN=paste_here")
    raise SystemExit(1)

# Bot tokens have 3 parts (xxx.yyy.zzz). Client Secret has 1 part — wrong one causes 401.
if DISCORD_TOKEN.count(".") != 2:
    print("Token format looks wrong (expected: three parts separated by dots).")
    print("Use the BOT token from the 'Bot' tab → Reset Token.")
    print("Do NOT use the 'Client Secret' from the OAuth2 tab.")
    print(f"Loading .env from: {_env_path}")

# Need Message Content intent for reading message text (enable in Developer Portal → Bot)
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(
    command_prefix="!",  # optional for prefix commands
    intents=intents,
    description="Luna — your AI companion",
)


# Last assistant reply per scope (Discord) for profile-from-question capture
_last_assistant_by_scope: dict[str, str] = {}

# Discord music state (per guild) for YouTube playback queue.
_music_states: dict[int, dict] = {}
_music_state_lock = threading.Lock()
# Conversational play: when search returns multiple results, wait for user to pick (guild_id -> {results, channel_id, author_id}).
_pending_play_choice: dict[int, dict] = {}
_pending_play_choice_lock = threading.Lock()
_FFMPEG_BEFORE_OPTS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
_FFMPEG_OPTS = "-vn"
# Suno only: when stream playback fails, download here and play from file. Leave empty to use temp dir for Suno too.
LUNA_MUSIC_DOWNLOAD_DIR = (os.environ.get("LUNA_MUSIC_DOWNLOAD_DIR") or r"D:\Downloads\youtube shorts quotes chris\youtube quotes excel list_files").strip()


def _cleanup_track_temp_file(track: dict | None) -> None:
    """Delete downloaded temporary audio file for a track, if any. Keeps files in LUNA_MUSIC_DOWNLOAD_DIR."""
    if not isinstance(track, dict):
        return
    fp = (track.get("local_path") or "").strip()
    if not fp:
        return
    if LUNA_MUSIC_DOWNLOAD_DIR and os.path.normpath(fp).startswith(os.path.normpath(LUNA_MUSIC_DOWNLOAD_DIR)):
        return  # keep user's download folder files
    try:
        if os.path.isfile(fp):
            os.unlink(fp)
    except Exception:
        pass


def _music_state_for_guild(guild_id: int) -> dict:
    with _music_state_lock:
        state = _music_states.get(guild_id)
        if state is None:
            state = {"queue": deque(), "current": None}
            _music_states[guild_id] = state
        return state


def _clear_music_state_for_guild(guild_id: int) -> None:
    with _music_state_lock:
        old = _music_states.get(guild_id) or {"queue": deque(), "current": None}
        try:
            _cleanup_track_temp_file(old.get("current"))
            for t in list(old.get("queue") or []):
                _cleanup_track_temp_file(t)
        except Exception:
            pass
        _music_states[guild_id] = {"queue": deque(), "current": None}


def _resolve_youtube_track(query: str) -> tuple[bool, dict | str]:
    """Resolve a YouTube URL/search query into direct audio stream info using yt-dlp."""
    q = (query or "").strip()
    if not q:
        return False, "Please provide a YouTube URL or search terms."
    try:
        import yt_dlp
    except Exception:
        return False, "yt-dlp is not installed. Run: pip install yt-dlp"

    base_opts = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch1",
        "extract_flat": False,
        "skip_download": True,
    }
    # First try with android client (often avoids 403) and flexible format.
    ydl_opts = {
        **base_opts,
        "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
        "extractor_args": {"youtube": {"player_client": "android,web"}},
    }
    info = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(q, download=False)
    except Exception:
        # Fallback: no extractor_args, simpler format (sometimes works when first attempt 403s).
        ydl_opts_fallback = {**base_opts, "format": "bestaudio/best"}
        try:
            with yt_dlp.YoutubeDL(ydl_opts_fallback) as ydl:
                info = ydl.extract_info(q, download=False)
        except Exception:
            pass
    try:
        if info is None:
            return False, "No results found."
        if "entries" in info and info.get("entries"):
            info = next((e for e in info["entries"] if e), None)
        if not info:
            return False, "No playable result found."
        stream_url = (info.get("url") or "").strip()
        # Prefer direct audio formats if top-level url is missing.
        if not stream_url:
            for f in (info.get("formats") or []):
                if not isinstance(f, dict):
                    continue
                acodec = (f.get("acodec") or "").lower()
                vcodec = (f.get("vcodec") or "").lower()
                u = (f.get("url") or "").strip()
                if u and acodec not in ("", "none") and vcodec in ("none", ""):
                    stream_url = u
                    break
        if not stream_url:
            return False, "Could not resolve audio stream URL."
        title = (info.get("title") or "Unknown title").strip()
        web_url = (info.get("webpage_url") or info.get("original_url") or q).strip()
        duration = int(info.get("duration") or 0)
        headers = info.get("http_headers") or {}
        return True, {
            "title": title,
            "web_url": web_url,
            "stream_url": stream_url,
            "duration": duration,
            "http_headers": headers if isinstance(headers, dict) else {},
        }
    except Exception as e:
        return False, f"Could not resolve YouTube audio: {e}"


def _youtube_search_multiple(query: str, max_results: int = 5) -> tuple[bool, list[dict] | str]:
    """Search YouTube and return up to max_results as list of {title, url}. Used for conversational 'play X' -> 'Which song?' flow."""
    q = (query or "").strip()
    if not q:
        return False, "No search query."
    try:
        import yt_dlp
    except Exception:
        return False, "yt-dlp is not installed."
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "default_search": f"ytsearch{max_results}",
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(q, download=False)
        if not info or not info.get("entries"):
            return True, []
        results = []
        for e in (info.get("entries") or [])[:max_results]:
            if not isinstance(e, dict):
                continue
            vid_id = (e.get("id") or "").strip()
            title = (e.get("title") or e.get("url") or "Unknown").strip()
            url = (e.get("url") or "").strip()
            if not url and vid_id:
                url = f"https://www.youtube.com/watch?v={vid_id}"
            if url:
                results.append({"title": title, "url": url})
        return True, results
    except Exception as e:
        return False, str(e)


def _is_suno_url(query: str) -> bool:
    """True if query looks like a Suno song or share URL."""
    q = (query or "").strip().lower()
    return "suno.com/song/" in q or "suno.com/s/" in q


def _resolve_suno_track(url: str) -> tuple[bool, dict | str]:
    """Resolve a Suno song URL to track info (stream_url, title). Fetches page and extracts og:audio or cdn MP3 URL."""
    url = (url or "").strip()
    if not url:
        return False, "Missing Suno URL."
    if not _is_suno_url(url):
        return False, "Not a Suno song URL."
    # Normalize: ensure https
    if url.startswith("//"):
        url = "https:" + url
    elif not url.startswith("http"):
        url = "https://" + url
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return False, f"Could not fetch Suno page: {e}"

    stream_url = ""
    title = "Suno track"

    # og:audio (e.g. <meta property="og:audio" content="https://...">)
    m = re.search(r'<meta[^>]+property=["\']og:audio["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if not m:
        m = re.search(r'content=["\']([^"\']+)["\'][^>]+property=["\']og:audio["\']', html, re.IGNORECASE)
    if m:
        stream_url = m.group(1).strip()
    if not stream_url:
        # Fallback: look for cdn*.suno.ai or suno.ai MP3 URLs in page
        for m in re.finditer(r'https?://[^\s"\'<>]+\.suno\.ai[^\s"\'<>]*\.mp3[^\s"\'<>]*', html, re.IGNORECASE):
            cand = m.group(0).rstrip("\\")
            if "cdn" in cand.lower() or "storage" in cand.lower() or "media" in cand.lower():
                stream_url = cand
                break
        if not stream_url:
            for m in re.finditer(r'https?://[^\s"\'<>]+\.mp3', html):
                cand = m.group(0).rstrip("\\")
                if "suno" in cand.lower():
                    stream_url = cand
                    break

    if not stream_url or not stream_url.startswith("http"):
        return False, "Could not find audio URL on Suno page. The song may be private or the page format changed."

    # og:title for track name
    t = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if not t:
        t = re.search(r'content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']', html, re.IGNORECASE)
    if t:
        title = (t.group(1) or "").strip()[:200] or title

    return True, {
        "title": title,
        "web_url": url,
        "stream_url": stream_url,
        "duration": 0,
        "http_headers": {},
    }


def _resolve_local_file_track(query: str) -> tuple[bool, dict | str]:
    """Resolve a local MP3/M4A path to a track. Query can be absolute path, or filename in LUNA_MUSIC_DOWNLOAD_DIR."""
    q = (query or "").strip().strip('"').strip("'")
    if not q:
        return False, "Missing path or filename."
    q_lower = q.lower()
    if not (q_lower.endswith(".mp3") or q_lower.endswith(".m4a") or q_lower.endswith(".wav")):
        return False, "Not a local audio path."
    path = None
    if os.path.isabs(q) and os.path.isfile(q):
        path = os.path.normpath(q)
    elif LUNA_MUSIC_DOWNLOAD_DIR:
        dir_norm = os.path.normpath(LUNA_MUSIC_DOWNLOAD_DIR)
        # Try as path under the folder
        under = os.path.normpath(os.path.join(LUNA_MUSIC_DOWNLOAD_DIR, q))
        if os.path.isfile(under):
            path = under
        # Try as bare filename in folder
        if not path and os.path.dirname(q) == "":
            under = os.path.normpath(os.path.join(LUNA_MUSIC_DOWNLOAD_DIR, os.path.basename(q)))
            if os.path.isfile(under):
                path = under
    if not path or not os.path.isfile(path):
        return False, "File not found in your music folder or path."
    title = os.path.splitext(os.path.basename(path))[0].replace("_", " ").strip()
    return True, {
        "title": title,
        "web_url": path,
        "stream_url": "",
        "duration": 0,
        "http_headers": {},
        "local_path": path,
    }


def _resolve_play_track(query: str) -> tuple[bool, dict | str]:
    """Resolve a query to a playable track. Supports local MP3 (path or filename in folder), YouTube, and Suno."""
    q = (query or "").strip()
    if not q:
        return False, "Please provide a YouTube URL, search terms, a Suno song URL, or a path/filename to an MP3 in your music folder."
    # Local file: path to .mp3/.m4a or filename in LUNA_MUSIC_DOWNLOAD_DIR
    if ".mp3" in q.lower() or ".m4a" in q.lower() or ".wav" in q.lower():
        ok, result = _resolve_local_file_track(q)
        if ok:
            return True, result
    if _is_suno_url(q):
        ok, result = _resolve_suno_track(q)
        if not ok or not isinstance(result, dict):
            return ok, result
        # When Suno download folder is set, download to it first and play from file so we always find it there.
        download_dir = (LUNA_MUSIC_DOWNLOAD_DIR or "").strip()
        if download_dir:
            try:
                os.makedirs(download_dir, exist_ok=True)
            except Exception:
                pass
            if os.path.isdir(download_dir):
                ok_dl, local_path = _download_suno_audio_to_dir(
                    result.get("stream_url") or "",
                    download_dir,
                    (result.get("title") or "suno").strip(),
                )
                if ok_dl and local_path and os.path.isfile(local_path):
                    result["local_path"] = os.path.normpath(local_path)
        return True, result
    return _resolve_youtube_track(q)


def _download_suno_audio_temp(stream_url: str) -> tuple[bool, str]:
    """Download Suno audio from direct MP3 URL to a temp file for stable Discord playback."""
    url = (stream_url or "").strip()
    if not url or not url.startswith("http"):
        return False, "Missing or invalid Suno stream URL."
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        if not data or len(data) < 1000:
            return False, "Suno audio download too small or empty."
        ext = ".mp3" if b"ID3" in data[:20] or url.lower().endswith(".mp3") else ".m4a"
        fd, path = tempfile.mkstemp(suffix=ext, prefix="luna_suno_")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(data)
        return True, path
    except Exception as e:
        return False, f"Suno download failed: {e}"


def _download_youtube_audio_temp(web_url: str) -> tuple[bool, str]:
    """Download YouTube audio to a temp file for stable Discord playback."""
    return _download_youtube_audio_to_dir(web_url, tempfile.gettempdir())


def _download_youtube_audio_to_dir(web_url: str, target_dir: str) -> tuple[bool, str]:
    """Download YouTube audio to target_dir for stable Discord playback. Uses yt-dlp."""
    src = (web_url or "").strip()
    if not src:
        return False, "Missing source URL for download fallback."
    target_dir = (target_dir or "").strip()
    if not target_dir:
        target_dir = tempfile.gettempdir()
    try:
        import yt_dlp
    except Exception:
        return False, "yt-dlp is not installed."
    try:
        os.makedirs(target_dir, exist_ok=True)
        outtmpl = os.path.join(target_dir, "%(title)s-%(id)s.%(ext)s")
        ydl_opts = {
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "outtmpl": outtmpl,
            "restrictfilenames": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(src, download=True)
            path = ""
            requested = info.get("requested_downloads") if isinstance(info, dict) else None
            if isinstance(requested, list) and requested:
                path = (requested[0].get("filepath") or "").strip()
            if not path:
                path = ydl.prepare_filename(info)
        if not path or not os.path.isfile(path):
            return False, "Download fallback failed to produce a local file."
        return True, path
    except Exception as e:
        return False, f"Download fallback failed: {e}"


def _download_suno_audio_to_dir(stream_url: str, target_dir: str, title: str = "suno") -> tuple[bool, str]:
    """Download Suno audio to target_dir. Returns (ok, path)."""
    url = (stream_url or "").strip()
    if not url or not url.startswith("http"):
        return False, "Missing or invalid Suno stream URL."
    target_dir = (target_dir or "").strip()
    if not target_dir:
        target_dir = tempfile.gettempdir()
    try:
        os.makedirs(target_dir, exist_ok=True)
        safe = re.sub(r'[<>:"/\\|?*]', "_", (title or "suno")[:80]).strip() or "suno"
        safe = safe[:60]
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = os.path.join(target_dir, f"{safe}_{ts}.mp3")
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        if not data or len(data) < 1000:
            return False, "Suno audio download too small or empty."
        with open(path, "wb") as f:
            f.write(data)
        return True, path
    except Exception as e:
        return False, f"Suno download failed: {e}"


def _fmt_seconds(sec: int) -> str:
    s = max(int(sec or 0), 0)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


async def _announce_music_now_playing(guild_id: int, track: dict) -> None:
    channel_id = int(track.get("request_channel_id") or 0)
    if not channel_id:
        return
    ch = bot.get_channel(channel_id)
    if not ch:
        return
    try:
        dur = _fmt_seconds(int(track.get("duration") or 0))
        dur_part = f" ({dur})" if int(track.get("duration") or 0) > 0 else ""
        await ch.send(f"🎵 Now playing: **{track.get('title','Unknown')}**{dur_part}\n{track.get('web_url','')}")
    except Exception:
        pass


def _start_next_music_track(guild_id: int, voice_client: discord.VoiceClient) -> bool:
    state = _music_state_for_guild(guild_id)
    if voice_client.is_playing() or voice_client.is_paused():
        return True
    if not state["queue"]:
        state["current"] = None
        return False
    track = state["queue"].popleft()
    track["_retry_count"] = int(track.get("_retry_count") or 0)
    track["_started_monotonic"] = time.monotonic()
    state["current"] = track
    local_path = (track.get("local_path") or "").strip()
    if local_path and os.path.isfile(local_path):
        # Short delay so VC pipeline is ready before ffmpeg starts (reduces "stopped before it played").
        time.sleep(1.5)
        # Use absolute path with forward slashes so ffmpeg and Discord get a valid path (handles spaces).
        abs_path = os.path.abspath(local_path).replace("\\", "/")
        # -re: read input at native frame rate (real-time); needed so local file streams to VC properly instead of finishing too fast.
        source = discord.FFmpegPCMAudio(
            abs_path,
            before_options="-re -nostdin",
            options=_FFMPEG_OPTS,
        )
    else:
        headers = track.get("http_headers") or {}
        header_lines = []
        if isinstance(headers, dict):
            # Pass essential headers to avoid YouTube 403/empty stream in ffmpeg.
            for k in ("User-Agent", "Referer", "Accept-Language", "Cookie"):
                v = (headers.get(k) or "").strip()
                if v:
                    clean_v = v.replace("\r", " ").replace("\n", " ").strip()
                    header_lines.append(f"{k}: {clean_v}")
        header_opt = ""
        if header_lines:
            header_opt = f' -headers "{ "\\r\\n".join(header_lines) }\\r\\n"'
        before_opts = f"{_FFMPEG_BEFORE_OPTS}{header_opt} -nostdin"

        source = discord.FFmpegPCMAudio(
            track["stream_url"],
            before_options=before_opts,
            options=_FFMPEG_OPTS,
        )

    def _after_play(err):
        if err:
            print(f"[Luna music] Playback error: {err}", flush=True)
        try:
            asyncio.run_coroutine_threadsafe(_on_music_track_end(guild_id, err), bot.loop)
        except Exception:
            pass

    voice_client.play(source, after=_after_play)
    try:
        asyncio.run_coroutine_threadsafe(_announce_music_now_playing(guild_id, track), bot.loop)
    except Exception:
        pass
    return True


async def _on_music_track_end(guild_id: int, err=None) -> None:
    guild = bot.get_guild(guild_id)
    if not guild or not guild.voice_client or not guild.voice_client.is_connected():
        _clear_music_state_for_guild(guild_id)
        return
    state = _music_state_for_guild(guild_id)
    manual_skip = bool(state.pop("manual_skip", False))
    current = state.get("current")
    if current:
        started = float(current.get("_started_monotonic") or 0.0)
        elapsed = max(0.0, time.monotonic() - started) if started > 0 else 0.0
        duration = int(current.get("duration") or 0)
        retry_count = int(current.get("_retry_count") or 0)
        local_path = (current.get("local_path") or "").strip()

        # If ffmpeg dies early, don't treat it as completed playback.
        if duration > 0:
            ended_too_soon = elapsed < max(20.0, duration * 0.60)
        else:
            ended_too_soon = elapsed < 8.0

        # Don't retry "ended too soon" for local files from the user's folder (they're already on disk; retry can cause stop/restart).
        is_local_file_track = bool((current.get("local_path") or "").strip() and LUNA_MUSIC_DOWNLOAD_DIR and os.path.normpath((current.get("local_path") or "")).startswith(os.path.normpath(LUNA_MUSIC_DOWNLOAD_DIR)))
        if (not manual_skip) and ended_too_soon and retry_count < 1 and not is_local_file_track:
            web_url = (current.get("web_url") or "").strip()
            if web_url:
                ok, refreshed = await asyncio.to_thread(_resolve_play_track, web_url)
                if ok and isinstance(refreshed, dict) and refreshed.get("stream_url"):
                    refreshed["request_channel_id"] = current.get("request_channel_id")
                    refreshed["_retry_count"] = retry_count + 1
                    state["current"] = None
                    state["queue"].appendleft(refreshed)
                    ch = bot.get_channel(int(current.get("request_channel_id") or 0))
                    if ch:
                        try:
                            await ch.send("🔁 Stream dropped early, retrying this track once...")
                        except Exception:
                            pass
                    _start_next_music_track(guild_id, guild.voice_client)
                    return
        if (not manual_skip) and ended_too_soon and retry_count < 2 and not local_path and not is_local_file_track:
            web_url = (current.get("web_url") or "").strip()
            if web_url and _is_suno_url(web_url):
                # Suno only: download to folder or temp and retry. YouTube is stream-only (no download).
                download_dir = (LUNA_MUSIC_DOWNLOAD_DIR or "").strip()
                if download_dir and not os.path.isdir(download_dir):
                    try:
                        os.makedirs(download_dir, exist_ok=True)
                    except Exception:
                        download_dir = ""
                if download_dir:
                    ok_dl, dl = await asyncio.to_thread(
                        _download_suno_audio_to_dir,
                        current.get("stream_url") or "",
                        download_dir,
                        (current.get("title") or "suno").strip(),
                    )
                    if not ok_dl:
                        ok_dl, dl = await asyncio.to_thread(_download_suno_audio_temp, current.get("stream_url") or "")
                else:
                    ok_dl, dl = await asyncio.to_thread(_download_suno_audio_temp, current.get("stream_url") or "")
                if ok_dl:
                    current["local_path"] = dl
                    current["_retry_count"] = retry_count + 1
                    current["_started_monotonic"] = 0.0
                    state["current"] = None
                    state["queue"].appendleft(current)
                    ch = bot.get_channel(int(current.get("request_channel_id") or 0))
                    if ch:
                        try:
                            await ch.send("📥 Stream is unstable. Downloading this track for stable playback and retrying...")
                        except Exception:
                            pass
                    _start_next_music_track(guild_id, guild.voice_client)
                    return

        # Track finished (or skipped) -> clean temp file.
        _cleanup_track_temp_file(current)

    state["current"] = None
    _start_next_music_track(guild_id, guild.voice_client)


def _build_system_prompt(base: str | None, memory_scope: str | None) -> str | None:
    """Append user profile and 4-layer memory to system prompt if scope given. SOUL.md and TOOLS.md are injected into base."""
    if not base:
        return None
    base = _get_effective_system_prompt(base)
    if not memory_scope:
        return base
    parts = [base.rstrip()]
    # Linked user (web + Discord): same person on both platforms
    if LINKED_SCOPE and memory_scope == LINKED_SCOPE:
        parts.append(
            "The current user is Chris (Solonaras). They use both the web UI and Discord—treat them as the same person. "
            "Remember them and continue conversations naturally on either platform. "
            "When they ask for a game, website, or any code to be saved in Luna projects, you must add LUNA_WRITE_FILE blocks (one per file) so the files are created there—do not only show code in the chat. "
            "When they ask you to *run* a script, you must trigger the 'run' command (output the JSON command)—do NOT create a file that only lists instructions. Execute via the tool."
        )
    elif memory_scope.startswith("discord"):
        parts.append(
            "On Discord you talk to many different users. The profile and memories below are for the *current* user only. "
            "Each user has their own profile and memory; ask for their name and other details when you don't have them."
        )
    profile_block = get_profile_prompt(memory_scope)
    if profile_block:
        parts.append(profile_block)
    memory_block = get_memory_prompt(memory_scope)
    if memory_block:
        parts.append(memory_block)
    style_block = get_user_style_prompt(memory_scope)
    if style_block:
        parts.append(style_block)
    goals_block = get_goals_prompt(memory_scope)
    if goals_block:
        parts.append(goals_block)
    parts.append("Using the user's profile and goals, you may suggest next steps or offer to help when relevant. If this seems like the start of a conversation, you may open with a brief nudge (e.g. 'You might want to…' or 'Last time you were…') when relevant.")
    identity_hint = _get_identity_file_hint()
    if identity_hint and memory_scope and (memory_scope == LINKED_SCOPE or memory_scope == "web"):
        parts.append("---\n" + identity_hint)
    if len(parts) <= 1:
        return base
    return "\n\n".join(parts)


def _try_capture_memory(scope: str, user_message: str) -> None:
    """If user said something worth remembering, store in the right layer (core vs long-term). Luna's brain (neuron layer) gates learning and can add from conversation."""
    text = (user_message or "").strip()
    if not text or not scope:
        return
    brain = brain_step(scope, text, context={})
    # "my goal is X" / "remember my goal: X" -> goals list (Luna references in prompt)
    m = re.search(r"\b(?:my goal is|remember my goal[:\s]+)\s*(.+?)(?:\.|$)", text, re.I | re.S)
    if m:
        add_goal(scope, m.group(1).strip()[:500])
        return
    m = re.search(r"\b(?:my goals? (?:are|is)\s+)(.+?)(?:\.|$)", text, re.I | re.S)
    if m:
        add_goal(scope, m.group(1).strip()[:500])
        return
    # "always remember that X" -> Layer 1 (core)
    m = re.search(r"\balways\s+remember\s+that\s+(.+?)(?:\.|$)", text, re.I | re.S)
    if m:
        add_core_memory(scope, m.group(1).strip()[:1500])
        return
    m = re.search(r"\balways\s+remember[:\s]+(.+?)(?:\.|$)", text, re.I | re.S)
    if m:
        add_core_memory(scope, m.group(1).strip()[:1500])
        return
    # "remember that ..." / "remember: ..." -> Layer 2/3 (long-term)
    m = re.search(r"\bremember\s+that\s+(.+?)(?:\.|$)", text, re.I | re.S)
    if m:
        add_memory(scope, m.group(1).strip()[:1500])
        return
    m = re.search(r"\bremember[:\s]+(.+?)(?:\.|$)", text, re.I | re.S)
    if m:
        add_memory(scope, m.group(1).strip()[:1500])
        return
    # Name capture: keep strict to avoid false positives like "I'm thinking..."
    m = re.search(r"\b(?:my name is|call me|i am called|this is)\s+([a-zA-Z][a-zA-Z\s\-']{0,50}?)(?:\.|,|\s+and|\s*$)", text, re.I)
    if m:
        name = m.group(1).strip()
        if 1 <= len(name) <= 80:
            add_core_memory(scope, f"The user's name is {name}.")
        return
    # "I like X" / "I love X" / "I prefer X" -> Layer 2/3 (long-term)
    m = re.search(r"\b(?:i like|i love|i prefer|i enjoy)\s+(.+?)(?:\.|$)", text, re.I | re.S)
    if m:
        pref = m.group(1).strip()[:300]
        if len(pref) >= 2:
            add_memory(scope, f"The user likes/prefers: {pref}.")
        return
    # Brain: learn from conversation when the neuron layer says "should remember" (no explicit phrase)
    if brain.get("should_remember") and not brain.get("should_add_core"):
        if 10 <= len(text) <= 800:
            add_memory(scope, text[:500])
    elif brain.get("should_add_core") and 10 <= len(text) <= 800:
        add_core_memory(scope, text[:500])


def _try_capture_profile(scope: str, user_message: str) -> None:
    """Update permanent user profile from explicit phrases (name, location, occupation, etc.)."""
    text = (user_message or "").strip()
    if not text or not scope:
        return
    # Name (strict pattern to avoid accidental captures from normal sentences)
    m = re.search(r"\b(?:my name is|call me|i am called|this is)\s+([a-zA-Z][a-zA-Z\s\-']{0,50}?)(?:\.|,|\s+and|\s*$)", text, re.I)
    if m:
        name = m.group(1).strip()
        if 1 <= len(name) <= 80:
            set_profile_field(scope, "name", name)
        return
    # Location
    m = re.search(r"\b(?:i live in|i'?m from|i live at|based in)\s+(.+?)(?:\.|$)", text, re.I | re.S)
    if m:
        loc = m.group(1).strip()[:200]
        if len(loc) >= 2:
            set_profile_field(scope, "location", loc)
        return
    # Occupation
    m = re.search(r"\b(?:i work as|i'?m a|i'?m an|i do)\s+(.+?)(?:\.|$)", text, re.I | re.S)
    if m:
        job = m.group(1).strip()[:200]
        if len(job) >= 2:
            set_profile_field(scope, "occupation", job)
        return
    # Interests
    m = re.search(r"\b(?:my (?:main )?interests? (?:are|is)|i (?:am )?interested in)\s+(.+?)(?:\.|$)", text, re.I | re.S)
    if m:
        val = m.group(1).strip()[:300]
        if len(val) >= 2:
            set_profile_field(scope, "interests", val)
        return
    # Birthday
    m = re.search(r"\b(?:my birthday is|i was born on|born on)\s+(.+?)(?:\.|$)", text, re.I | re.S)
    if m:
        val = m.group(1).strip()[:100]
        if len(val) >= 2:
            set_profile_field(scope, "birthday", val)


# Context compaction: when conversation history exceeds this many messages, older part is summarized (OpenClaw-style).
# Fewer messages = faster Ollama replies; keep 12 recent for good context without large prompts.
_COMPACT_THRESHOLD = 20
_KEEP_RECENT_MESSAGES = 12  # keep this many most recent messages in full; older ones become a summary
_OLLAMA_HISTORY_MAX = 12  # max conversation turns sent to Ollama per request (fewer = faster)


def _summarize_conversation(messages: list[dict]) -> str:
    """Ask Ollama for a 2-4 sentence summary of the conversation. Returns summary or empty on failure."""
    if not messages:
        return ""
    lines = []
    for m in messages:
        role = (m.get("role") or "user").strip().lower()
        content = (m.get("content") or "").strip()[:500]
        if content:
            lines.append(f"{'User' if role == 'user' else 'Luna'}: {content}")
    if not lines:
        return ""
    block = "\n".join(lines[-30:])  # at most last 30 message contents
    prompt = (
        "Summarize this conversation in 2-4 short sentences. Keep only main topics, decisions, and outcomes. "
        "Output only the summary, no preamble.\n\n" + block
    )
    try:
        out = ollama_chat(
            prompt,
            system_prompt="You are a summarizer. Output only the summary text, 2-4 sentences.",
            memory_scope=None,
            message_history=None,
            model=OLLAMA_MODEL_SMALL,
        )
        return (out or "").strip()[:600] or ""
    except Exception:
        return ""


def _compact_conversation_history(messages: list[dict]) -> list[dict]:
    """If history is long, Boss asks Scribe to summarize the oldest part, then keeps recent messages."""
    if not messages or len(messages) <= _COMPACT_THRESHOLD:
        return list(messages)
    old = messages[: -_KEEP_RECENT_MESSAGES]
    recent = messages[-_KEEP_RECENT_MESSAGES:]
    summary = employee_scribe(old)
    if not summary:
        return recent[-_COMPACT_THRESHOLD:]  # fallback: just trim
    return [
        {"role": "user", "content": f"[Previous conversation summary]: {summary}"},
        {"role": "assistant", "content": "Understood."},
    ] + recent


def ollama_chat(
    user_message: str,
    system_prompt: str | None = None,
    memory_scope: str | None = None,
    message_history: list[dict] | None = None,
    model: str | None = None,
) -> str:
    """Send user message to Ollama, return assistant reply. Blocking.
    Used by Boss (Luna) for main chat reply when model is default; employees use model=OLLAMA_MODEL_SMALL.
    message_history: optional list of {"role": "user"|"assistant", "content": "..."} for short-term context.
    model: optional model name; employees (Commander, Scribe) use OLLAMA_MODEL_SMALL.
    """
    use_model = (model or OLLAMA_MODEL).strip() or OLLAMA_MODEL
    prompt = _build_system_prompt(system_prompt, memory_scope)
    messages = []
    if prompt:
        messages.append({"role": "system", "content": prompt})
    if message_history:
        for h in message_history[-_OLLAMA_HISTORY_MAX:]:  # limit turns for faster inference
            role = (h.get("role") or "").lower()
            content = (h.get("content") or "").strip()
            if content and role in ("user", "assistant"):
                messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})
    body = json.dumps({"model": use_model, "messages": messages, "stream": False}).encode()
    req = urllib.request.Request(
        f"{OLLAMA_BASE}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
        return (data.get("message") or {}).get("content", "").strip() or "I didn't get a reply."
    except urllib.error.URLError as e:
        return f"Ollama isn't responding: {e.reason}. Is it running? Try: ollama run {OLLAMA_MODEL}"
    except Exception as e:
        return f"Something went wrong: {e}"


def _run_create_agent_code(user_request: str, open_in_notepad: bool = True) -> tuple[bool, str]:
    """Generate Python code with Qwen 2.5 Coder, save to Luna projects/agents/<name>.py, optionally open in Notepad. Returns (ok, message)."""
    request = (user_request or "").strip()
    if not request or len(request) > 2000:
        return False, "Give a short description of what the script should do (e.g. 'a script that fetches the weather' or 'code to list files in a folder')."
    system = (
        "You are a Python code generator. The user will describe what they want. "
        "Output only valid Python code. Use a single markdown code block with language python, e.g. ```python\\n...\\n```. "
        "No explanation outside the block. Keep the code concise and runnable."
    )
    try:
        reply = ollama_chat(
            f"Generate Python code for the following request. Output only the code in a ```python code block.\n\nRequest: {request}",
            system_prompt=system,
            memory_scope=None,
            message_history=None,
            model=OLLAMA_MODEL,
        )
    except Exception as e:
        return False, f"Could not generate code: {e}"
    if not reply or not reply.strip():
        return False, "No code was generated. Try a clearer description."
    # Extract code from ```python ... ``` or ``` ... ```
    code = reply.strip()
    for pattern in (r"```python\s*\n(.*?)```", r"```\s*\n(.*?)```"):
        m = re.search(pattern, code, re.DOTALL | re.IGNORECASE)
        if m and m.group(1).strip():
            code = m.group(1).strip()
            break
    if not code or len(code) > 100_000:
        return False, "No valid code block found in the reply or output too large."
    # Safe filename under agents/: only alphanumeric, underscore, hyphen; default new_script.py
    slug = re.sub(r"[^\w\s-]", "", request.lower())[:30].strip().replace(" ", "_") or "new_script"
    slug = re.sub(r"_+", "_", slug).strip("_") or "new_script"
    base = slug if slug.endswith(".py") else f"{slug}.py"
    if not base.endswith(".py"):
        base += ".py"
    rel_path = f"agents/{base}"
    ok, result = luna_write_file(rel_path, code)
    if not ok:
        return False, result
    if open_in_notepad and result:
        _open_file_by_path(result)
    return True, f"Created **{rel_path}** and opened in Notepad." if open_in_notepad else f"Created **{rel_path}**."


def ollama_chat_stream(
    user_message: str,
    system_prompt: str | None = None,
    memory_scope: str | None = None,
    message_history: list[dict] | None = None,
    model: str | None = None,
):
    """Stream Ollama reply: yields content deltas (str). For Luna chat use model=OLLAMA_CHAT_MODEL; default OLLAMA_MODEL."""
    use_model = (model or OLLAMA_MODEL).strip() or OLLAMA_MODEL
    prompt = _build_system_prompt(system_prompt, memory_scope)
    messages = []
    if prompt:
        messages.append({"role": "system", "content": prompt})
    if message_history:
        for h in message_history[-_OLLAMA_HISTORY_MAX:]:
            role = (h.get("role") or "").lower()
            content = (h.get("content") or "").strip()
            if content and role in ("user", "assistant"):
                messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})
    body = json.dumps({"model": use_model, "messages": messages, "stream": True}).encode()
    req = urllib.request.Request(
        f"{OLLAMA_BASE}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            buffer = b""
            for chunk in iter(lambda: resp.read(4096), b""):
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        content = (data.get("message") or {}).get("content") or ""
                        if content:
                            yield content
                    except (json.JSONDecodeError, TypeError):
                        pass
            # process any remaining line in buffer
            if buffer.strip():
                try:
                    data = json.loads(buffer.decode("utf-8", errors="replace").strip())
                    content = (data.get("message") or {}).get("content") or ""
                    if content:
                        yield content
                except (json.JSONDecodeError, TypeError):
                    pass
    except Exception:
        yield "Something went wrong."


# Web UI (Jarvis-style chat in browser)
_base_dir = os.path.dirname(os.path.abspath(__file__))
web_app = Flask(__name__, static_folder=os.path.join(_base_dir, "static"))


@web_app.route("/")
def serve_index():
    return send_from_directory(web_app.static_folder, "index.html")


# gTTS voice settings used across web + Discord.
GTTS_LANG = "en"


def _split_into_chunks(text: str, max_chunk_chars: int = 80) -> list[str]:
    """Split text into small chunks for streaming TTS — first chunk plays as soon as it's ready."""
    text = (text or "").strip()
    if not text:
        return []
    # Split on sentence or clause boundaries so chunks are natural
    parts = re.split(r"(?<=[.!?,;:])\s+", text)
    chunks = []
    current = ""
    for p in parts:
        if not p.strip():
            continue
        if current and len(current) + len(p) + 1 <= max_chunk_chars:
            current = (current + " " + p).strip()
        else:
            if current:
                chunks.append(current)
            current = p.strip()
    if current:
        chunks.append(current)
    # If no breaks, split by length so we still stream
    if not chunks:
        for i in range(0, len(text), max_chunk_chars):
            chunks.append(text[i : i + max_chunk_chars].strip())
    return [c for c in chunks if c]


def _generate_tts(text: str) -> bytes:
    """Generate TTS with gTTS. Used for all platforms. Returns MP3 bytes."""
    clean = (text or "").strip()
    if not clean:
        return b""
    try:
        from gtts import gTTS
        buf = io.BytesIO()
        tts = gTTS(text=clean[:500], lang=GTTS_LANG, slow=False)
        tts.write_to_fp(buf)
        buf.seek(0)
        return buf.read()
    except Exception:
        return b""


async def _get_tts_for_discord(text: str) -> bytes | None:
    """Generate Discord TTS with gTTS."""
    return await asyncio.to_thread(_generate_tts, text)


# TTS stop: set by /api/tts-stop or before next chunk; current ffplay process for web PC playback
_tts_stop_requested = False
_tts_current_process: subprocess.Popen | None = None
_tts_lock = threading.Lock()


def _stop_tts_on_pc() -> None:
    """Stop any currently playing TTS on the PC (web). Idempotent."""
    global _tts_stop_requested, _tts_current_process
    with _tts_lock:
        _tts_stop_requested = True
        p = _tts_current_process
        _tts_current_process = None
    if p is not None and p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=2)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


def _play_tts_on_pc(audio_mp3_bytes: bytes) -> None:
    """Play TTS audio on this PC (server) using ffplay. Respects _tts_stop_requested."""
    global _tts_current_process
    if not audio_mp3_bytes:
        return
    fd, path = tempfile.mkstemp(suffix=".mp3")
    proc = None
    try:
        os.write(fd, audio_mp3_bytes)
        os.close(fd)
        fd = None
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        proc = subprocess.Popen(
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-i", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        with _tts_lock:
            _tts_current_process = proc
        while proc.poll() is None:
            if _tts_stop_requested:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                break
            time.sleep(0.2)
    except FileNotFoundError:
        pass  # ffplay not in PATH
    except Exception:
        pass
    finally:
        with _tts_lock:
            if _tts_current_process is proc:
                _tts_current_process = None
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        try:
            os.unlink(path)
        except Exception:
            pass


def _reply_text_for_tts(reply: str) -> str:
    """Strip code, HTML, and write-block lines so TTS speaks only context, not code."""
    if not (reply or reply.strip()):
        return ""
    text = reply
    # Remove full LUNA_WRITE_FILE ... END_LUNA_WRITE blocks
    text = re.sub(r"LUNA_WRITE_FILE.*?END_LUNA_WRITE", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Remove markdown code blocks (```lang\n...\n``` or ```\n...\n```)
    text = re.sub(r"```[\w]*\n.*?```", "", text, flags=re.DOTALL)
    # Remove standalone LUNA_WRITE_FILE / path: lines (artifact when block is partial)
    text = re.sub(r"(?m)^.*LUNA_WRITE_FILE.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?m)^\s*path:\s*\S+.*$", "", text)
    # Remove inline code (single backticks)
    text = re.sub(r"`[^`]*`", " ", text)
    # Remove HTML tags and their content (optional: only strip tags, keep text inside)
    text = re.sub(r"<[^>]+>", " ", text)
    # Strip Discord mentions and bold
    text = re.sub(r"<@!?\d+>", "", text)
    text = re.sub(r"\*\*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _play_reply_tts_on_pc(reply: str) -> None:
    """Generate TTS for reply and play on this PC in chunks (streaming feel). Runs in background."""
    global _tts_stop_requested
    tts_text = _reply_text_for_tts(reply)
    if not tts_text.strip():
        return  # Nothing to speak (e.g. only code)
    def _run():
        global _tts_stop_requested
        try:
            _tts_stop_requested = False
            for chunk_text in _split_into_chunks(tts_text):
                if _tts_stop_requested:
                    break
                if not chunk_text.strip():
                    continue
                audio = _generate_tts(chunk_text)
                if _tts_stop_requested:
                    break
                if audio:
                    _play_tts_on_pc(audio)
        except Exception:
            pass
    t = threading.Thread(target=_run, daemon=True)
    t.start()


# --- Reminders: store in data/reminders.json; background task sends Discord DM + TTS at set time ---

def _load_reminders() -> list[dict]:
    with _reminders_lock:
        try:
            if os.path.isfile(_REMINDERS_FILE):
                with open(_REMINDERS_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            pass
    return []


def _save_reminders(reminders: list[dict]) -> None:
    with _reminders_lock:
        try:
            os.makedirs(os.path.dirname(_REMINDERS_FILE), exist_ok=True)
            with open(_REMINDERS_FILE, "w", encoding="utf-8") as f:
                json.dump(reminders, f, indent=2, ensure_ascii=False)
        except Exception:
            pass


def add_reminder(time_str: str, message: str, discord_user_id: str, recurring: str | None = None) -> str:
    """Add a reminder. time_str like '19:00'. recurring 'daily' or None. Returns reminder id."""
    tid = str(uuid.uuid4())[:8]
    entry = {
        "id": tid,
        "time": time_str,
        "message": (message or "").strip() or "do something",
        "discord_user_id": str(discord_user_id),
        "recurring": recurring,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    reminders = _load_reminders()
    reminders.append(entry)
    _save_reminders(reminders)
    return tid


def get_due_reminders() -> list[dict]:
    """Return reminders that are due right now (current local time HH:MM). Recurring daily only once per day."""
    now = datetime.now()
    current_time = now.strftime("%H:%M")
    today = now.strftime("%Y-%m-%d")
    reminders = _load_reminders()
    due = []
    for r in reminders:
        if (r.get("time") or "").strip() != current_time:
            continue
        if r.get("recurring") == "daily":
            if (r.get("last_sent") or "") == today:
                continue
        due.append(r)
    return due


def remove_reminder_by_id(rid: str) -> None:
    reminders = [r for r in _load_reminders() if r.get("id") != rid]
    _save_reminders(reminders)


async def _send_reminder_dm(reminder: dict) -> None:
    """Send Discord DM to user with text + TTS voice note (Hey, remember you need to <message>)."""
    user_id_str = (reminder.get("discord_user_id") or "").strip()
    if not user_id_str:
        return
    try:
        user_id = int(user_id_str)
    except ValueError:
        return
    msg_text = (reminder.get("message") or "do something").strip()
    full_text = f"Hey, remember you need to {msg_text}"
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        if user is None:
            return
        channel = user.dm_channel or await user.create_dm()
        await channel.send(full_text)
        mp3_bytes = await _get_tts_for_discord(full_text)
        if mp3_bytes:
            await channel.send(file=discord.File(io.BytesIO(mp3_bytes), filename="reminder.mp3"))
    except Exception:
        pass
    rid = reminder.get("id")
    if rid:
        if reminder.get("recurring") != "daily":
            remove_reminder_by_id(rid)
        else:
            today = datetime.now().strftime("%Y-%m-%d")
            reminders = _load_reminders()
            for r in reminders:
                if r.get("id") == rid:
                    r["last_sent"] = today
                    break
            _save_reminders(reminders)


def _parse_reminder_time(s: str) -> str | None:
    """Parse '7pm', '7:00 pm', '19:00', '9am' -> '19:00' or '09:00'. Returns None if invalid."""
    s = (s or "").strip().lower()
    if not s:
        return None
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", s.replace(" ", ""))
    if not m:
        return None
    h, mi, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").strip()
    if ampm == "pm" and h != 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0
    elif not ampm and h >= 24:
        return None
    if h < 0 or h > 23 or mi < 0 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


async def _reminder_loop() -> None:
    """Background: every minute check for due reminders and send DM + TTS to user."""
    await bot.wait_until_ready()
    last_minute = ""
    while True:
        try:
            now = datetime.now()
            this_minute = now.strftime("%H:%M")
            if this_minute != last_minute:
                last_minute = this_minute
                for r in get_due_reminders():
                    await _send_reminder_dm(r)
        except Exception:
            pass
        await asyncio.sleep(30)


def _can_use_suno_discord(author_id: int) -> bool:
    """Allow Suno automation on Discord only for linked user or configured admin."""
    if _discord_admin_id_int is not None and author_id == _discord_admin_id_int:
        return True
    if _linked_discord_id_int is not None and author_id == _linked_discord_id_int:
        return True
    return False


def _can_use_discord_dm_action(author_id: int) -> bool:
    """Allow outbound DM actions only for linked user or configured admin."""
    if _discord_admin_id_int is not None and author_id == _discord_admin_id_int:
        return True
    if _linked_discord_id_int is not None and author_id == _linked_discord_id_int:
        return True
    return False


def _can_use_x_share_discord(author_id: int) -> bool:
    """Allow X sharing automation on Discord only for linked user or configured admin."""
    if _discord_admin_id_int is not None and author_id == _discord_admin_id_int:
        return True
    if _linked_discord_id_int is not None and author_id == _linked_discord_id_int:
        return True
    return False


def _can_use_youtube_comment_discord(author_id: int) -> bool:
    """Allow YouTube comment automation on Discord only for linked user or configured admin."""
    if _discord_admin_id_int is not None and author_id == _discord_admin_id_int:
        return True
    if _linked_discord_id_int is not None and author_id == _linked_discord_id_int:
        return True
    return False


def _can_use_instagram_dm_discord(author_id: int) -> bool:
    """Allow Instagram DM automation on Discord only for linked user or configured admin."""
    if _discord_admin_id_int is not None and author_id == _discord_admin_id_int:
        return True
    if _linked_discord_id_int is not None and author_id == _linked_discord_id_int:
        return True
    return False


def _extract_discord_user_id(target: str) -> int | None:
    """Parse a Discord user id from mention (<@123> / <@!123>) or raw id text."""
    t = (target or "").strip()
    if not t:
        return None
    m = re.search(r"<@!?(\d+)>", t)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    digits = re.sub(r"\D", "", t)
    if 16 <= len(digits) <= 22:
        try:
            return int(digits)
        except Exception:
            return None
    return None


def _extract_share_song_request(text: str) -> bool:
    """Return True if user asks Luna to share a random channel song to X."""
    raw = (text or "").strip()
    if not raw:
        return False
    low = raw.lower()
    if low.startswith("!share_song") or low.startswith("!share-song"):
        return True
    if low.startswith("!share song"):
        return True
    patterns = (
        r"\bshare\s+my\s+song\b",
        r"\bshare\s+a\s+song\b",
        r"\bshare\b.*\bsong\b.*\b(?:x|twitter)\b",
        r"\bpost\b.*\bsong\b.*\b(?:x|twitter)\b",
    )
    return any(re.search(p, low, re.IGNORECASE) for p in patterns)


def _extract_share_facebook_request(text: str) -> bool:
    """Return True if user asks Luna to share a random channel song to Facebook."""
    raw = (text or "").strip()
    if not raw:
        return False
    low = raw.lower()
    if low.startswith("!share_facebook") or low.startswith("!share-facebook"):
        return True
    if low.startswith("!share facebook"):
        return True
    if low in ("share facebook", "i share facebook", "share on facebook", "post on facebook"):
        return True
    patterns = (
        r"\bshare\s+my\s+song\b.*\bfacebook\b",
        r"\bshare\b.*\bsong\b.*\bfacebook\b",
        r"\bpost\b.*\bsong\b.*\bfacebook\b",
        r"\bshare\b.*\bfacebook\b",
        r"\bpost\b.*\bfacebook\b",
    )
    return any(re.search(p, low, re.IGNORECASE) for p in patterns)


def _extract_suno_description(text: str) -> str:
    """Extract song description from !suno or conversational song-create requests."""
    raw = (text or "").strip()
    if not raw:
        return ""
    low = raw.lower()
    if low.startswith("!suno "):
        return raw[6:].strip()
    patterns = [
        r"^\s*(?:luna[\s,:-]*)?(?:create|make)\s+(?:me\s+)?(?:a\s+)?song(?:\s+(?:about|for|with))?\s*[:,-]?\s*(.+)$",
        r"^\s*(?:luna[\s,:-]*)?song\s*[:,-]\s*(.+)$",
    ]
    for p in patterns:
        m = re.match(p, raw, re.IGNORECASE | re.DOTALL)
        if m:
            return (m.group(1) or "").strip()
    return ""


def _extract_youtube_video_url(text: str) -> str:
    """Extract the first YouTube video URL from text/command."""
    raw = (text or "").strip()
    if not raw:
        return ""
    low = raw.lower()
    if low.startswith("!yt_comment "):
        raw = raw[len("!yt_comment ") :].strip()
    elif low.startswith("!youtube_comment "):
        raw = raw[len("!youtube_comment ") :].strip()
    elif low.startswith("!comment_youtube "):
        raw = raw[len("!comment_youtube ") :].strip()
    # Keep URL extraction permissive for conversational requests.
    m = re.search(r"(https?://(?:www\.)?(?:youtube\.com|youtu\.be)/[^\s)]+)", raw, re.IGNORECASE)
    if not m:
        return ""
    return (m.group(1) or "").strip().rstrip(".,!?")


def _extract_youtube_comment_request(text: str) -> str:
    """Return YouTube URL if message asks Luna to comment on that video."""
    raw = (text or "").strip()
    if not raw:
        return ""
    low = raw.lower()
    url = _extract_youtube_video_url(raw)
    if not url:
        return ""
    if low.startswith(("!yt_comment", "!youtube_comment", "!comment_youtube")):
        return url
    if re.search(r"\b(comment|reply)\b", low, re.IGNORECASE) and re.search(
        r"\b(youtube|yt|video)\b", low, re.IGNORECASE
    ):
        return url
    return ""


def _extract_instagram_dm_request(text: str) -> tuple[str, str]:
    """
    Extract Instagram DM target + optional message.
    Returns (target, message). Target can be username or direct thread URL.
    """
    raw = (text or "").strip()
    if not raw:
        return "", ""
    low = raw.lower()
    if low.startswith(("!ig_dm ", "!instagram_dm ", "!igdm ")):
        parts = raw.split(None, 2)
        if len(parts) < 2:
            return "", ""
        first = (parts[1] or "").strip()
        direct = _extract_instagram_thread_url(first)
        username = re.sub(r"^@", "", first) if not direct else direct
        msg = (parts[2] or "").strip() if len(parts) > 2 else ""
        return username, msg

    patterns = [
        r"\b(?:message|dm|send(?:\s+a)?\s+message(?:\s+to)?)\s+@?([a-z0-9._]{2,30})\b.*\binstagram\b",
        r"\binstagram\b.*\b(?:message|dm|send(?:\s+a)?\s+message(?:\s+to)?)\s+@?([a-z0-9._]{2,30})\b",
    ]
    for p in patterns:
        m = re.search(p, low, re.IGNORECASE)
        if m:
            username = (m.group(1) or "").strip()
            # Optional quoted message in conversational prompt.
            m_msg = re.search(r'"([^"]{3,280})"', raw)
            msg = (m_msg.group(1) or "").strip() if m_msg else ""
            return username, msg
    direct = _extract_instagram_thread_url(raw)
    if direct and re.search(r"\b(?:instagram|insta|ig)\b", low, re.IGNORECASE):
        m_msg = re.search(r'"([^"]{3,280})"', raw)
        msg = (m_msg.group(1) or "").strip() if m_msg else ""
        return direct, msg
    return "", ""


def _extract_instagram_thread_url(text: str) -> str:
    """Extract instagram direct thread URL (https://www.instagram.com/direct/t/<id>)."""
    raw = (text or "").strip()
    if not raw:
        return ""
    m = re.search(r"(https?://(?:www\.)?instagram\.com/direct/t/\d+/?[^\s]*)", raw, re.IGNORECASE)
    if not m:
        return ""
    return (m.group(1) or "").strip().rstrip(".,!?")


def _extract_news_request(text: str) -> bool:
    """Return True when user asks for latest/today/world news headlines."""
    raw = (text or "").strip()
    if not raw:
        return False
    low = raw.lower()
    if low.startswith("!news"):
        return True
    patterns = (
        r"\bwhat(?:'s| is)\s+the\s+news\b",
        r"\bnews\s+for\s+today\b",
        r"\btoday(?:'s)?\s+news\b",
        r"\blatest\s+(?:world\s+)?news\b",
        r"\bworld\s+news\b",
        r"\bheadlines\b",
    )
    return any(re.search(p, low, re.IGNORECASE) for p in patterns)


def _parse_world_news_feed(xml_text: str) -> list[dict]:
    """Parse RSS/Atom feed into normalized news items."""
    out: list[dict] = []
    if not xml_text:
        return out
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return out

    def _txt(el):
        return (el.text or "").strip() if el is not None and el.text else ""

    # RSS items
    for item in root.findall(".//item"):
        title = _txt(item.find("title"))
        link = _txt(item.find("link"))
        pub = _txt(item.find("pubDate")) or _txt(item.find("published")) or _txt(item.find("updated"))
        if title and link:
            out.append({"title": title, "link": link, "published": pub})

    # Atom entries
    atom_ns = {"atom": "http://www.w3.org/2005/Atom"}
    for entry in root.findall(".//atom:entry", atom_ns):
        title = _txt(entry.find("atom:title", atom_ns))
        link = ""
        link_el = entry.find("atom:link[@rel='alternate']", atom_ns) or entry.find("atom:link", atom_ns)
        if link_el is not None:
            link = (link_el.get("href") or "").strip()
        pub = _txt(entry.find("atom:updated", atom_ns)) or _txt(entry.find("atom:published", atom_ns))
        if title and link:
            out.append({"title": title, "link": link, "published": pub})
    return out


def _news_time_sort_key(published: str) -> float:
    s = (published or "").strip()
    if not s:
        return 0.0
    dt = _news_datetime(published)
    if dt is None:
        return 0.0
    return dt.timestamp()


def _news_datetime(published: str) -> datetime | None:
    s = (published or "").strip()
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass
    try:
        # ISO-like fallback
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _fetch_world_news(limit: int = 8) -> tuple[bool, str]:
    """Fetch latest world headlines from RSS feeds, prioritizing today's news."""
    items: list[dict] = []
    for url in WORLD_NEWS_FEEDS:
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                xml_text = resp.read().decode("utf-8", errors="replace")
            items.extend(_parse_world_news_feed(xml_text))
        except Exception:
            continue

    if not items:
        return False, "I couldn't fetch world news right now. Please try again in a moment."

    # Deduplicate by title/link.
    seen = set()
    uniq = []
    for it in items:
        key = ((it.get("title") or "").strip().lower(), (it.get("link") or "").strip())
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)

    uniq.sort(key=lambda x: _news_time_sort_key(x.get("published") or ""), reverse=True)

    # Prefer items published "today" in local time.
    today_local = datetime.now().date()
    todays = []
    for it in uniq:
        dt = _news_datetime(it.get("published") or "")
        if dt is None:
            continue
        try:
            local_date = dt.astimezone().date()
        except Exception:
            local_date = dt.date()
        if local_date == today_local:
            todays.append(it)

    pick_from = todays if todays else uniq
    top = pick_from[: max(1, min(limit, 12))]
    lines = ["📰 **Today's world news headlines:**" if todays else "📰 **Latest world news headlines (no fresh items dated today in feeds):**"]
    for i, it in enumerate(top, 1):
        title = (it.get("title") or "Untitled").strip()
        link = (it.get("link") or "").strip()
        lines.append(f"{i}. {title}\n{link}")
    return True, "\n\n".join(lines)


def _fetch_search_results(query: str, max_results: int = 10) -> list[dict]:
    """Fetch search results from DuckDuckGo HTML. Returns list of {title, url, snippet}."""
    query = (query or "").strip()
    if not query:
        return []
    try:
        url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query, safe="")
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return []
    html = html.replace("&amp;", "&")
    results = []
    # result__a: title + url
    title_url_pat = re.compile(
        r'class="result__a"[^>]*href="//duckduckgo.com/l/\?uddg=([^&"]+)[^"]*"[^>]*>([^<]+)</a>',
        re.I,
    )
    for m in title_url_pat.finditer(html):
        if len(results) >= max_results:
            break
        enc = m.group(1).strip()
        try:
            real_url = urllib.parse.unquote(enc)
        except Exception:
            continue
        title = (m.group(2) or "").strip()
        if not real_url or not title:
            continue
        results.append({"title": title[:200], "url": real_url[:500], "snippet": ""})
    # result__snippet: text between > and </a> (may contain <b> etc.)
    snippet_pat = re.compile(r'class="result__snippet"[^>]*>([^<]*(?:<[^>]+>[^<]*)*?)</a>', re.I | re.S)
    snippets = []
    for m in snippet_pat.finditer(html):
        raw = (m.group(1) or "").strip()
        raw = re.sub(r"<[^>]+>", "", raw).strip()[:300]
        snippets.append(raw)
    for i, s in enumerate(snippets):
        if i < len(results):
            results[i]["snippet"] = s
    return results[:max_results]


def _recommend_best_search_result(query: str, results: list[dict]) -> str:
    """Return the first result as recommendation (command-only mode: no Ollama)."""
    if not results:
        return ""
    r = results[0]
    title = (r.get("title") or "").strip()
    url = (r.get("url") or "").strip()
    snippet = (r.get("snippet") or "").strip()[:180]
    return f"Top result: **{title}** — {url}\n{snippet}"


def _open_google_search(query: str) -> tuple[bool, str]:
    """Fetch results, analyze with LLM for best link, open Google in browser, return recommendation."""
    query = (query or "").strip()
    if not query:
        return False, "No search query given."
    try:
        results = _fetch_search_results(query, max_results=10)
        recommendation = ""
        if results:
            recommendation = employee_search_picker(query, results)
        url = "https://www.google.com/search?q=" + urllib.parse.quote(query, safe="")
        webbrowser.open(url)
        base = f"Opened Google search for **{query[:80]}{'…' if len(query) > 80 else ''}** in your browser."
        if recommendation:
            base = base + "\n\n" + recommendation
        return True, base
    except Exception as e:
        try:
            url = "https://www.google.com/search?q=" + urllib.parse.quote(query, safe="")
            webbrowser.open(url)
            return True, f"Opened Google search for **{query[:80]}{'…' if len(query) > 80 else ''}** in your browser. (Could not analyze results this time.)"
        except Exception:
            pass
        return False, str(e)[:200]


# Conversation starters: skip Ollama intent check to speed up plain chat.
_INTENT_CONVERSATION_START = re.compile(
    r"^(how\s|what\s|why\s|when\s|who\s|where\s|is\s|are\s|can you|could you|would you|tell me|i\s|we\s|my\s|hey\s|hi\s|hello\s)",
    re.IGNORECASE,
)


def _intent_requires_tool_call(text: str) -> bool:
    """
    Binary intent gate for direct tool automation.
    Returns True (YES) if user asks to execute tools now, else False (NO).
    """
    raw = (text or "").strip()
    if not raw:
        return False
    low = raw.lower()
    if low.startswith("!"):
        return True

    # Clearly conversational start -> no Ollama intent call (faster chat).
    if _INTENT_CONVERSATION_START.match(low):
        return False

    # Strong direct intents we should execute immediately.
    strong_yes_patterns = (
        r"\bshare\s+my\s+song\b",
        r"\bshare\b.*\b(?:x|twitter|facebook)\b",
        r"\bpost\b.*\b(?:x|twitter|facebook)\b",
        r"\bcomment\b.*\b(?:youtube|yt)\b",
        r"\b(?:instagram|insta|ig)\b.*\b(?:message|dm|send)\b",
        r"\b(?:create|make)\b.*\b(?:song)\b",
        r"\b(?:open|use)\b.*\bsuno\b",
    )
    if any(re.search(p, low, re.IGNORECASE) for p in strong_yes_patterns):
        return True

    # If no action language appears, avoid extra model latency and treat as conversation.
    if not re.search(
        r"\b(create|make|generate|open|run|start|share|post|send|read|write|edit|list|use|publish)\b",
        low,
        re.IGNORECASE,
    ):
        return False

    # Ambiguous requests -> binary YES/NO classifier via Ollama.
    prompt = (
        "You are a strict binary intent classifier for Luna.\n"
        "Task: decide whether the user is asking to execute an external tool action RIGHT NOW.\n"
        "Tool action examples: file operations, browser automation (Suno/X/Facebook), social sharing, local song generation.\n"
        "Conversation examples: chatting, questions, opinions, planning, storytelling.\n"
        "Reply with exactly one token: YES or NO.\n\n"
        f"User message: {raw}"
    )
    payload = json.dumps(
        {
            "model": OLLAMA_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_BASE}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        out = ((data.get("message") or {}).get("content") or "").strip().upper()
        first = (out.split() or [""])[0]
        if first == "YES":
            return True
        if first == "NO":
            return False
    except Exception:
        pass
    # Safe fallback: don't run tools unless classifier is clearly YES.
    return False


def _suno_preview_text(desc: str, limit: int = 220) -> str:
    """Short, safe preview for chat/TTS announcements."""
    t = (desc or "").strip().replace("\n", " ")
    if len(t) <= limit:
        return t
    return t[: limit - 3].rstrip() + "..."


def _suno_bootstrap_marker_path() -> str:
    return os.path.join(SUNO_PROFILE_DIR, ".login_ready")


def _is_suno_bootstrap_done() -> bool:
    return os.path.isfile(_suno_bootstrap_marker_path())


def _mark_suno_bootstrap_done() -> None:
    try:
        os.makedirs(SUNO_PROFILE_DIR, exist_ok=True)
        with open(_suno_bootstrap_marker_path(), "w", encoding="utf-8") as f:
            f.write("ready")
    except Exception:
        pass


def _clear_suno_bootstrap_done() -> None:
    try:
        marker = _suno_bootstrap_marker_path()
        if os.path.isfile(marker):
            os.unlink(marker)
    except Exception:
        pass


def _sanitize_suno_profile_state() -> None:
    """
    Clean stale Chrome profile state so persistent launches don't show
    'didn't shut down correctly' or attach to stale singleton locks.
    """
    try:
        os.makedirs(SUNO_PROFILE_DIR, exist_ok=True)
    except Exception:
        return

    # Remove stale singleton files created by previous Chrome runs.
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        fp = os.path.join(SUNO_PROFILE_DIR, name)
        try:
            if os.path.exists(fp):
                os.unlink(fp)
        except Exception:
            pass

    # Reset crash markers in profile preferences.
    pref_path = os.path.join(SUNO_PROFILE_DIR, "Default", "Preferences")
    try:
        if os.path.isfile(pref_path):
            with open(pref_path, encoding="utf-8") as f:
                prefs = json.load(f)
            if isinstance(prefs, dict):
                prefs["exited_cleanly"] = True
                prof = prefs.get("profile")
                if isinstance(prof, dict):
                    prof["exit_type"] = "Normal"
                    prefs["profile"] = prof
                with open(pref_path, "w", encoding="utf-8") as f:
                    json.dump(prefs, f, ensure_ascii=False)
    except Exception:
        pass


def _launch_suno_context(playwright):
    """Launch Playwright persistent context with settings that are friendlier for OAuth login."""
    _sanitize_suno_profile_state()
    opts = {
        "user_data_dir": SUNO_PROFILE_DIR,
        "headless": False,
        "viewport": {"width": 1280, "height": 900},
        # Reduce automation fingerprinting that can trigger Google "not secure browser".
        "ignore_default_args": ["--enable-automation"],
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    }
    if SUNO_BROWSER_PATH and os.path.isfile(SUNO_BROWSER_PATH):
        opts["executable_path"] = SUNO_BROWSER_PATH
    elif SUNO_BROWSER_CHANNEL in ("chrome", "msedge", "chrome-beta", "msedge-beta", "msedge-dev", "chromium"):
        opts["channel"] = SUNO_BROWSER_CHANNEL
    return playwright.chromium.launch_persistent_context(**opts)


def _start_suno_first_login_window() -> tuple[bool, str]:
    """
    First-run login flow: open Suno and keep browser window open for manual login.
    When user closes the window, mark login bootstrap as done.
    """
    global _suno_bootstrap_running
    with _suno_bootstrap_lock:
        if _suno_bootstrap_running:
            return False, "Suno login window is already open. Finish login there, then close it and run your song request again."
        _suno_bootstrap_running = True

    def _run():
        global _suno_bootstrap_running
        context = None
        opened = False
        try:
            from playwright.sync_api import sync_playwright

            os.makedirs(SUNO_PROFILE_DIR, exist_ok=True)
            with sync_playwright() as p:
                context = _launch_suno_context(p)
                page = None
                for pg in list(context.pages):
                    try:
                        if not pg.is_closed():
                            page = pg
                            break
                    except Exception:
                        continue
                if page is None:
                    page = context.new_page()
                page.goto(SUNO_CREATE_URL, wait_until="domcontentloaded", timeout=90000)
                opened = True
                # Keep this first-login browser session open until the user closes it.
                while True:
                    try:
                        open_pages = [pg for pg in context.pages if not pg.is_closed()]
                        if not open_pages:
                            break
                        time.sleep(1.0)
                    except Exception:
                        break
        except Exception as e:
            print(f"Suno first-login bootstrap error: {e}", flush=True)
        finally:
            try:
                if context:
                    context.close()
            except Exception:
                pass
            if opened:
                _mark_suno_bootstrap_done()
            with _suno_bootstrap_lock:
                _suno_bootstrap_running = False

    threading.Thread(target=_run, daemon=True).start()
    return True, "First-time Suno login: I opened the browser and left it open. Log in with your account, then close that browser window. After that, ask again and Luna will create songs automatically and close the browser."


def _run_suno_create(description: str) -> tuple[bool, str]:
    """
    Open Suno create page via Playwright using persistent profile and submit prompt.
    Returns (success, message). Uses headed browser so user can see it.
    """
    desc = (description or "").strip()
    if not desc:
        return False, "Please include a song description. Example: !suno cinematic cyberpunk anthem."
    if len(desc) > 1200:
        desc = desc[:1200]
    if _suno_bootstrap_running:
        return False, "Suno login window is still open. Finish login there, then close that window and try again."
    if not _suno_run_lock.acquire(blocking=False):
        return False, "Suno automation is already running. Please wait a few seconds and try again."

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        _suno_run_lock.release()
        return False, "Playwright is not installed. Run: pip install playwright && python -m playwright install chromium"

    os.makedirs(SUNO_PROFILE_DIR, exist_ok=True)

    def _find_prompt_target(page):
        # Prefer specific editable fields, fallback to first visible textarea/contenteditable.
        selectors = [
            "[data-testid*='prompt'] textarea",
            "[data-testid*='lyrics'] textarea",
            "textarea[placeholder*='Describe']",
            "textarea[placeholder*='song']",
            "textarea[aria-label*='Describe']",
            "textarea[aria-label*='prompt']",
            "textarea",
            "[contenteditable='true']",
        ]
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if loc.count() and loc.is_visible():
                    return sel, loc
            except Exception:
                continue
        return "", None

    def _looks_like_login_required(page) -> bool:
        """Return True only when page clearly indicates login is required."""
        try:
            url = (page.url or "").lower()
            if any(k in url for k in ("/sign-in", "/login", "accounts.google.com")):
                return True
        except Exception:
            pass
        # Explicit login buttons/text.
        indicators = (
            "button:has-text('Sign in')",
            "button:has-text('Log in')",
            "a:has-text('Sign in')",
            "a:has-text('Log in')",
            "text=Continue with Google",
            "text=Continue with Discord",
        )
        for sel in indicators:
            loc = page.locator(sel).first
            try:
                if loc.count() and loc.is_visible():
                    return True
            except Exception:
                continue
        return False

    context = None
    try:
        with sync_playwright() as p:
            context = _launch_suno_context(p)
            page = None
            for pg in list(context.pages):
                try:
                    if not pg.is_closed():
                        page = pg
                        break
                except Exception:
                    continue
            if page is None:
                page = context.new_page()
            page.goto(SUNO_CREATE_URL, wait_until="domcontentloaded", timeout=90000)
            try:
                page.bring_to_front()
                page.wait_for_timeout(900)
            except Exception:
                pass

            # Detect prompt visibility with generous wait (Suno UI can take time to hydrate).
            sel = ""
            target = None
            quick_deadline = time.time() + 30
            while time.time() < quick_deadline:
                sel, target = _find_prompt_target(page)
                if target is not None:
                    break
                page.wait_for_timeout(800)

            if target is None:
                # Only trigger login flow when sign-in indicators are actually present.
                if _looks_like_login_required(page):
                    _clear_suno_bootstrap_done()
                    ok, msg = _start_suno_first_login_window()
                    if ok:
                        return (
                            False,
                            "Suno needs login again. I opened a browser window and kept it open. "
                            "Please log in there, then close that window and ask again.",
                        )
                    return False, msg
                return False, "Suno opened and you seem logged in, but I couldn't find the prompt box to type the description."

            # Logged in and prompt visible: mark setup complete so next runs skip bootstrap.
            if not _is_suno_bootstrap_done():
                _mark_suno_bootstrap_done()

            # For visible/fun automation, always type with keyboard so user can watch it.
            target.click()
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            page.keyboard.type(desc, delay=22)

            # Wait for UI to enable the Create button after typing (Suno often enables it after prompt is entered).
            page.wait_for_timeout(2000)

            # Try multiple strategies: different selectors, scroll into view, force click.
            _waits = (800, 1500, 2500) if _should_prefer_longer_waits("suno") else (800, 1500, 2500)
            clicked = False
            button_selectors = [
                "button:has-text('Create')",
                "button:has-text('Generate')",
                "button:has-text('Create song')",
                "button:has-text('Generate song')",
                "[role='button']:has-text('Create')",
                "[role='button']:has-text('Generate')",
                "[data-testid*='create']",
                "[data-testid*='generate']",
                "button[type='submit']",
                "a:has-text('Create')",
                "a:has-text('Generate')",
            ]
            for extra_wait in _waits:
                page.wait_for_timeout(extra_wait)
                for bsel in button_selectors:
                    try:
                        btn = page.locator(bsel).first
                        if btn.count() > 0:
                            btn.scroll_into_view_if_needed(timeout=3000)
                            if btn.is_visible():
                                btn.click(force=True, timeout=5000)
                                clicked = True
                                break
                    except Exception:
                        continue
                if clicked:
                    break
                try:
                    combined = page.locator("button:has-text('Create'), button:has-text('Generate'), [role='button']:has-text('Create'), [role='button']:has-text('Generate')").first
                    if combined.count() > 0:
                        combined.scroll_into_view_if_needed(timeout=3000)
                        combined.click(force=True, timeout=5000)
                        clicked = True
                        break
                except Exception:
                    pass

            if not clicked:
                return False, "I entered your prompt on Suno, but couldn't find or click the Create button (Suno's UI may have changed). Try clicking Create yourself in the open window."

            page.wait_for_timeout(2500)
            return True, f"Opened Suno, typed: \"{_suno_preview_text(desc, 260)}\", then clicked Create."
    except Exception as e:
        err = str(e)
        low = err.lower()
        if "opening in existing browser session" in low or "target page, context or browser has been closed" in low:
            return (
                False,
                "Suno browser profile is already in use by another Chrome window. "
                "Please close any Suno/automation Chrome window opened by Luna, then try again.",
            )
        return False, f"Suno automation error: {e}"
    finally:
        try:
            if context:
                context.close()
        except Exception:
            pass
        try:
            _suno_run_lock.release()
        except Exception:
            pass


def _get_random_channel_song() -> tuple[bool, dict | str]:
    """Pick a random recent video from the YouTube channel feed."""
    try:
        req = urllib.request.Request(
            YOUTUBE_FEED_URL,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            xml_text = resp.read().decode("utf-8", errors="replace")
        root = ET.fromstring(xml_text)
        ns = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
        entries = root.findall("atom:entry", ns)
        songs = []
        for e in entries:
            title = (e.findtext("atom:title", default="", namespaces=ns) or "").strip()
            vid = (e.findtext("yt:videoId", default="", namespaces=ns) or "").strip()
            link = ""
            link_el = e.find("atom:link[@rel='alternate']", ns)
            if link_el is not None:
                link = (link_el.attrib.get("href") or "").strip()
            if not link and vid:
                link = f"https://www.youtube.com/watch?v={vid}"
            if title and link:
                songs.append({"title": title, "url": link})
        if not songs:
            return False, "No songs found in the YouTube channel feed."
        return True, random.choice(songs)
    except Exception as e:
        return False, f"Could not load YouTube channel feed: {e}"


def _build_x_invite_message(song_title: str, song_url: str) -> str:
    """Create a friendly invite message for X post."""
    clean_title = (song_title or "").strip().replace("\n", " ")
    if len(clean_title) > 80:
        clean_title = clean_title[:77].rstrip() + "..."
    templates = [
        "Hey guys, it's a great time to listen to this one: \"{title}\" 🎶\n\nGive it a play and tell me what you think!\n{url}",
        "Hey everyone, if you need a fresh vibe right now, try this track: \"{title}\" ✨\n\nPress play and drop your feedback!\n{url}",
        "Quick music drop for today: \"{title}\" 🎧\n\nListen now, and if you like it, share it with a friend!\n{url}",
    ]
    msg = random.choice(templates).format(title=clean_title, url=song_url)
    # Keep some margin under X limit so URL normalization and emojis won't disable Post.
    if len(msg) > 260:
        msg = f"Hey guys, it's a great time to listen to this song.\n{clean_title}\n{song_url}"
    return msg


def _looks_like_x_login_required(page) -> bool:
    """Detect X login-required state on the page."""
    try:
        url = (page.url or "").lower()
        if any(k in url for k in ("/login", "/i/flow/login", "x.com/i/flow")):
            return True
    except Exception:
        pass
    for sel in ("text=Sign in", "text=Log in", "text=Create account"):
        loc = page.locator(sel).first
        try:
            if loc.count() and loc.is_visible():
                return True
        except Exception:
            continue
    return False


def _x_bootstrap_marker_path() -> str:
    return os.path.join(X_PROFILE_DIR, ".login_ready")


def _is_x_bootstrap_done() -> bool:
    return os.path.isfile(_x_bootstrap_marker_path())


def _mark_x_bootstrap_done() -> None:
    try:
        os.makedirs(X_PROFILE_DIR, exist_ok=True)
        with open(_x_bootstrap_marker_path(), "w", encoding="utf-8") as f:
            f.write("ready")
    except Exception:
        pass


def _clear_x_bootstrap_done() -> None:
    try:
        marker = _x_bootstrap_marker_path()
        if os.path.isfile(marker):
            os.unlink(marker)
    except Exception:
        pass


def _launch_x_context(playwright):
    """Launch Playwright persistent context for X with OAuth-friendly settings."""
    opts = {
        "user_data_dir": X_PROFILE_DIR,
        "headless": False,
        "viewport": {"width": 1280, "height": 900},
        "ignore_default_args": ["--enable-automation"],
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    }
    if SUNO_BROWSER_PATH and os.path.isfile(SUNO_BROWSER_PATH):
        opts["executable_path"] = SUNO_BROWSER_PATH
    elif SUNO_BROWSER_CHANNEL in ("chrome", "msedge", "chrome-beta", "msedge-beta", "msedge-dev", "chromium"):
        opts["channel"] = SUNO_BROWSER_CHANNEL
    return playwright.chromium.launch_persistent_context(**opts)


def _start_x_first_login_window() -> tuple[bool, str]:
    """
    First-run X login flow: open X compose and keep browser window open for manual login.
    When user closes window, mark bootstrap as done.
    """
    global _x_bootstrap_running
    with _x_bootstrap_lock:
        if _x_bootstrap_running:
            return False, "X login window is already open. Finish login there, then close it and run share again."
        _x_bootstrap_running = True

    def _run():
        global _x_bootstrap_running
        context = None
        opened = False
        try:
            from playwright.sync_api import sync_playwright

            os.makedirs(X_PROFILE_DIR, exist_ok=True)
            with sync_playwright() as p:
                context = _launch_x_context(p)
                page = None
                for pg in list(context.pages):
                    try:
                        if not pg.is_closed():
                            page = pg
                            break
                    except Exception:
                        continue
                if page is None:
                    page = context.new_page()
                page.goto(X_COMPOSE_URL, wait_until="domcontentloaded", timeout=90000)
                opened = True
                while True:
                    try:
                        open_pages = [pg for pg in context.pages if not pg.is_closed()]
                        if not open_pages:
                            break
                        time.sleep(1.0)
                    except Exception:
                        break
        except Exception as e:
            print(f"X first-login bootstrap error: {e}", flush=True)
        finally:
            try:
                if context:
                    context.close()
            except Exception:
                pass
            if opened:
                _mark_x_bootstrap_done()
            with _x_bootstrap_lock:
                _x_bootstrap_running = False

    threading.Thread(target=_run, daemon=True).start()
    return True, "First-time X login: I opened the browser and left it open. Log in with your account, then close that window. After that, run share again."


def _run_x_share_random_song() -> tuple[bool, str]:
    """
    Pick a random song from the configured YouTube channel and post it to X.
    Uses Playwright persistent profile so your login/session can be reused.
    """
    if _x_bootstrap_running:
        return False, "X login window is still open. Finish login there, then close that window and try again."
    if not _x_share_lock.acquire(blocking=False):
        return False, "X share automation is already running. Please wait and try again."

    try:
        ok_song, song = _get_random_channel_song()
        if not ok_song:
            return False, f"{song}"
        song_title = (song.get("title") or "").strip()
        song_url = (song.get("url") or "").strip()
        message = _build_x_invite_message(song_title, song_url)

        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            return False, "Playwright is not installed. Run: pip install playwright && python -m playwright install chromium"

        context = None
        needs_relogin = False
        try:
            with sync_playwright() as p:
                os.makedirs(X_PROFILE_DIR, exist_ok=True)
                context = _launch_x_context(p)
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(X_COMPOSE_URL, wait_until="domcontentloaded", timeout=90000)
                try:
                    page.bring_to_front()
                except Exception:
                    pass

                if _looks_like_x_login_required(page):
                    # Session expired; bootstrap after this context is closed.
                    _clear_x_bootstrap_done()
                    needs_relogin = True
                    return False, "X needs login again. Opening login window..."

                # Cookie banner can block compose controls
                for cookie_sel in (
                    "button:has-text('Accept all cookies')",
                    "button:has-text('Refuse non-essential cookies')",
                ):
                    btn = page.locator(cookie_sel).first
                    try:
                        if btn.count() and btn.is_visible():
                            btn.click()
                            page.wait_for_timeout(250)
                    except Exception:
                        pass

                def _find_x_textbox():
                    for sel in (
                        "div[role='dialog'] div[data-testid='tweetTextarea_0'][role='textbox']",
                        "div[role='dialog'] div[aria-label='Post text'][role='textbox']",
                        "div[role='dialog'] div[aria-label=\"What's happening?\"][role='textbox']",
                        "div[data-testid='tweetTextarea_0'][role='textbox']",
                        "div[data-testid='tweetTextarea_0']",
                        "div[role='textbox'][contenteditable='true']",
                        "[aria-label='Post text']",
                        "[aria-label=\"What's happening?\"]",
                    ):
                        loc = page.locator(sel).first
                        try:
                            if loc.count() and loc.is_visible():
                                return loc
                        except Exception:
                            continue
                    return None

                textbox = _find_x_textbox()
                if textbox is None:
                    # Try opening compose explicitly.
                    for compose_sel in (
                        "a[data-testid='SideNav_NewTweet_Button']",
                        "button[data-testid='SideNav_NewTweet_Button']",
                        "a[href='/compose/post']",
                    ):
                        c = page.locator(compose_sel).first
                        try:
                            if c.count() and c.is_visible():
                                c.click()
                                page.wait_for_timeout(900)
                                break
                        except Exception:
                            continue
                    deadline = time.time() + 12
                    while time.time() < deadline and textbox is None:
                        textbox = _find_x_textbox()
                        if textbox is not None:
                            break
                        page.wait_for_timeout(400)
                if textbox is None:
                    return False, "I couldn't find the X post text box."

                # Logged in and compose UI visible: mark setup complete.
                if not _is_x_bootstrap_done():
                    _mark_x_bootstrap_done()

                def _focus_x_textbox(tb) -> bool:
                    try:
                        tb.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                    for use_force in (False, True):
                        try:
                            tb.click(timeout=2500, force=use_force)
                            return True
                        except Exception:
                            continue
                    # Last fallback: focus via DOM API (no pointer event needed).
                    try:
                        handle = tb.element_handle(timeout=2500)
                        if handle:
                            page.evaluate("(el) => el && el.focus()", handle)
                            return True
                    except Exception:
                        pass
                    return False

                def _textbox_has_content(tb) -> bool:
                    try:
                        txt = (tb.inner_text(timeout=1200) or "").strip()
                        return len(txt) >= 6
                    except Exception:
                        return False

                def _set_x_text(tb, text_value: str, delay_ms: int) -> bool:
                    if not _focus_x_textbox(tb):
                        return False
                    # Method 1: keyboard typing (most reliable for X compose state).
                    try:
                        page.keyboard.press("Control+A")
                        page.keyboard.press("Backspace")
                        page.keyboard.type(text_value, delay=delay_ms)
                        page.wait_for_timeout(900)
                        if _textbox_has_content(tb):
                            return True
                    except Exception:
                        pass
                    # Method 2: direct DOM contenteditable write + input/change events.
                    try:
                        handle = tb.element_handle(timeout=2000)
                        if handle:
                            handle.evaluate(
                                """
                                (el, value) => {
                                    if (!el) return false;
                                    el.focus();
                                    el.textContent = value || "";
                                    el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value || "" }));
                                    el.dispatchEvent(new Event("change", { bubbles: true }));
                                    return true;
                                }
                                """,
                                text_value,
                            )
                            page.wait_for_timeout(650)
                            if _textbox_has_content(tb):
                                return True
                    except Exception:
                        pass
                    return False

                def _resolve_x_profile_popup() -> None:
                    """
                    If X mention/typeahead popup is open, select the profile suggestion
                    (or dismiss popup) so the Post button can be clicked.
                    """
                    try:
                        popup_visible = False
                        for popup_sel in (
                            "div[role='dialog'] div[role='listbox']",
                            "div[role='dialog'] [id^='typeaheadDropdown']",
                            "div[data-testid='typeaheadResults']",
                        ):
                            p = page.locator(popup_sel).first
                            try:
                                if p.count() and p.is_visible():
                                    popup_visible = True
                                    break
                            except Exception:
                                continue
                        if not popup_visible:
                            return

                        handle_txt = (X_HANDLE or "").lstrip("@").strip()
                        selectors = []
                        if handle_txt:
                            selectors.extend(
                                [
                                    f"div[role='dialog'] [role='option']:has-text('{handle_txt}')",
                                    f"div[role='dialog'] div[data-testid='typeaheadResult']:has-text('{handle_txt}')",
                                ]
                            )
                        selectors.extend(
                            [
                                "div[role='dialog'] [role='option']",
                                "div[role='dialog'] div[data-testid='typeaheadResult']",
                            ]
                        )

                        for sel in selectors:
                            opt = page.locator(sel).first
                            try:
                                if opt.count() and opt.is_visible():
                                    opt.click(timeout=2500, force=True)
                                    page.wait_for_timeout(220)
                                    return
                            except Exception:
                                continue

                        # Keyboard fallback: accept first suggestion.
                        try:
                            page.keyboard.press("ArrowDown")
                            page.wait_for_timeout(100)
                            page.keyboard.press("Enter")
                            page.wait_for_timeout(220)
                            return
                        except Exception:
                            pass

                        # Last fallback: close popup so it doesn't block Post.
                        try:
                            page.keyboard.press("Escape")
                            page.wait_for_timeout(180)
                        except Exception:
                            pass
                    except Exception:
                        pass

                if not _focus_x_textbox(textbox):
                    return False, "I found the X post box but couldn't focus it (an overlay is intercepting clicks)."

                # Keep browser visibly open while typing so user can watch the full post text.
                if not _set_x_text(textbox, message, delay_ms=26):
                    return False, "I couldn't type into the X post box. The compose overlay appears to be blocking input."
                _resolve_x_profile_popup()

                def _find_x_post_btn():
                    for sel in (
                        "div[role='dialog'] button[data-testid='tweetButtonInline']",
                        "div[role='dialog'] button[data-testid='tweetButton']",
                        "div[role='dialog'] button:has-text('Post')",
                        "button:has-text('Post')",
                        "button[data-testid='tweetButton']",
                    ):
                        loc = page.locator(sel).first
                        try:
                            if loc.count() and loc.is_visible():
                                return loc
                        except Exception:
                            continue
                    return None

                def _is_enabled(btn):
                    try:
                        if btn is None or not btn.count() or not btn.is_visible():
                            return False
                        if btn.is_disabled():
                            return False
                        aria_disabled = (btn.get_attribute("aria-disabled") or "").lower().strip()
                        return aria_disabled not in ("true", "1")
                    except Exception:
                        return False

                x_posted = False
                _x_waits = (1500, 2000, 0) if _should_prefer_longer_waits("share_x") else (0, 1500, 2000)
                for extra_wait in _x_waits:
                    page.wait_for_timeout(extra_wait)
                    post_btn = _find_x_post_btn()
                    if post_btn is None:
                        continue
                    deadline = time.time() + 8
                    while time.time() < deadline and not _is_enabled(post_btn):
                        page.wait_for_timeout(250)
                    if not _is_enabled(post_btn) and extra_wait == 0:
                        short_message = f"{song_title[:60].strip()} {song_url}".strip()[:220]
                        if _set_x_text(textbox, short_message, delay_ms=14):
                            _resolve_x_profile_popup()
                            for _ in range(24):
                                page.wait_for_timeout(250)
                                if _is_enabled(post_btn):
                                    break
                    if not _is_enabled(post_btn):
                        continue
                    try:
                        post_btn.click(timeout=5000)
                        x_posted = True
                        break
                    except Exception:
                        try:
                            post_btn.click(timeout=5000, force=True)
                            x_posted = True
                            break
                        except Exception:
                            pass
                if not x_posted:
                    return False, "I typed the message, but couldn't find or click the X Post button (tried several strategies)."
                page.wait_for_timeout(3000)
                return True, f"Shared to X ({X_PROFILE_URL}): \"{song_title}\" — {song_url}"
        except Exception as e:
            return False, f"X share automation error: {e}"
        finally:
            try:
                if context:
                    context.close()
            except Exception:
                pass
            if needs_relogin:
                ok_boot, msg_boot = _start_x_first_login_window()
                if not ok_boot:
                    print(f"X relogin bootstrap could not start: {msg_boot}", flush=True)
    finally:
        try:
            _x_share_lock.release()
        except Exception:
            pass


def _build_facebook_invite_message(song_title: str, song_url: str) -> str:
    """Create a friendly invite message for Facebook post."""
    clean_title = (song_title or "").strip().replace("\n", " ")
    if len(clean_title) > 80:
        clean_title = clean_title[:77].rstrip() + "..."
    templates = [
        "Hey friends, here is a random pick from my channel: \"{title}\" 🎶\n\nGive it a listen and tell me what you think:\n{url}",
        "Sharing one of my tracks: \"{title}\" ✨\n\nHope you enjoy it:\n{url}",
        "Music share time 🎧 \"{title}\"\n\nIf you like it, drop a comment:\n{url}",
    ]
    msg = random.choice(templates).format(title=clean_title, url=song_url)
    return msg[:420]


def _looks_like_facebook_login_required(page) -> bool:
    """Detect Facebook login-required state."""
    try:
        url = (page.url or "").lower()
        if "facebook.com/login" in url:
            return True
    except Exception:
        pass
    for sel in ("input[name='email']", "input[name='pass']", "button[name='login']"):
        loc = page.locator(sel).first
        try:
            if loc.count() and loc.is_visible():
                return True
        except Exception:
            continue
    return False


def _fb_bootstrap_marker_path() -> str:
    return os.path.join(FACEBOOK_PROFILE_DIR, ".login_ready")


def _is_fb_bootstrap_done() -> bool:
    return os.path.isfile(_fb_bootstrap_marker_path())


def _mark_fb_bootstrap_done() -> None:
    try:
        os.makedirs(FACEBOOK_PROFILE_DIR, exist_ok=True)
        with open(_fb_bootstrap_marker_path(), "w", encoding="utf-8") as f:
            f.write("ready")
    except Exception:
        pass


def _clear_fb_bootstrap_done() -> None:
    try:
        marker = _fb_bootstrap_marker_path()
        if os.path.isfile(marker):
            os.unlink(marker)
    except Exception:
        pass


def _launch_facebook_context(playwright):
    """Launch Playwright persistent context for Facebook."""
    opts = {
        "user_data_dir": FACEBOOK_PROFILE_DIR,
        "headless": False,
        "viewport": {"width": 1280, "height": 900},
        "ignore_default_args": ["--enable-automation"],
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    }
    if SUNO_BROWSER_PATH and os.path.isfile(SUNO_BROWSER_PATH):
        opts["executable_path"] = SUNO_BROWSER_PATH
    elif SUNO_BROWSER_CHANNEL in ("chrome", "msedge", "chrome-beta", "msedge-beta", "msedge-dev", "chromium"):
        opts["channel"] = SUNO_BROWSER_CHANNEL
    return playwright.chromium.launch_persistent_context(**opts)


def _start_facebook_first_login_window() -> tuple[bool, str]:
    """First-run Facebook login flow: keep browser window open until user closes it."""
    global _fb_bootstrap_running
    with _fb_bootstrap_lock:
        if _fb_bootstrap_running:
            return False, "Facebook login window is already open. Finish login there, then close it and run share again."
        _fb_bootstrap_running = True

    def _run():
        global _fb_bootstrap_running
        context = None
        opened = False
        login_confirmed = False
        try:
            from playwright.sync_api import sync_playwright
            os.makedirs(FACEBOOK_PROFILE_DIR, exist_ok=True)
            with sync_playwright() as p:
                context = _launch_facebook_context(p)
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(FACEBOOK_PROFILE_URL or FACEBOOK_HOME_URL, wait_until="domcontentloaded", timeout=90000)
                opened = True
                while True:
                    try:
                        # Track whether user appears logged in during the bootstrap window lifetime.
                        if not _looks_like_facebook_login_required(page):
                            login_confirmed = True
                        open_pages = [pg for pg in context.pages if not pg.is_closed()]
                        if not open_pages:
                            break
                        time.sleep(1.0)
                    except Exception:
                        break
        except Exception as e:
            print(f"Facebook first-login bootstrap error: {e}", flush=True)
        finally:
            try:
                if context:
                    context.close()
            except Exception:
                pass
            if opened and login_confirmed:
                _mark_fb_bootstrap_done()
            elif opened:
                _clear_fb_bootstrap_done()
            with _fb_bootstrap_lock:
                _fb_bootstrap_running = False

    threading.Thread(target=_run, daemon=True).start()
    return True, "First-time Facebook login: I opened the browser and left it open. Log in with your account, then close that window. After that, run share again."


def _run_facebook_share_random_song() -> tuple[bool, str]:
    """Pick random channel song and post it to Facebook."""
    if _fb_bootstrap_running:
        return False, "Facebook login window is still open. Finish login there, then close that window and try again."
    if not _fb_share_lock.acquire(blocking=False):
        return False, "Facebook share automation is already running. Please wait and try again."

    try:
        ok_song, song = _get_random_channel_song()
        if not ok_song:
            return False, f"{song}"
        song_title = (song.get("title") or "").strip()
        song_url = (song.get("url") or "").strip()
        message = _build_facebook_invite_message(song_title, song_url)

        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            return False, "Playwright is not installed. Run: pip install playwright && python -m playwright install chromium"

        context = None
        needs_relogin = False
        try:
            with sync_playwright() as p:
                os.makedirs(FACEBOOK_PROFILE_DIR, exist_ok=True)
                context = _launch_facebook_context(p)
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(FACEBOOK_PROFILE_URL or FACEBOOK_HOME_URL, wait_until="domcontentloaded", timeout=90000)
                try:
                    page.bring_to_front()
                except Exception:
                    pass

                if _looks_like_facebook_login_required(page):
                    _clear_fb_bootstrap_done()
                    needs_relogin = True
                    return False, "Facebook needs login again. Opening login window..."

                # Cookie banners can block composer interactions.
                for cookie_sel in (
                    "button:has-text('Accept all cookies')",
                    "button:has-text('Allow all cookies')",
                    "button:has-text('Refuse non-essential cookies')",
                ):
                    cbtn = page.locator(cookie_sel).first
                    try:
                        if cbtn.count() and cbtn.is_visible():
                            cbtn.click()
                            page.wait_for_timeout(250)
                    except Exception:
                        pass

                def _find_fb_textbox():
                    for sel in (
                        # Strongly prefer the active composer modal textbox.
                        "div[role='dialog'] div[role='textbox'][contenteditable='true']:not([aria-label^='Comment'])",
                        "div[role='dialog'] div[aria-label*=\"What's on your mind\"][role='textbox']",
                        "div[role='dialog'] div[aria-placeholder*=\"What's on your mind\"][role='textbox']",
                        # Fallbacks still exclude comment boxes.
                        "div[aria-label*='Write something'][role='textbox']:not([aria-label^='Comment'])",
                        "div[aria-label*='What'][role='textbox']:not([aria-label^='Comment'])",
                    ):
                        t = page.locator(sel).first
                        try:
                            if t.count() and t.is_visible():
                                return t
                        except Exception:
                            continue
                    return None

                # In some layouts, composer textbox is already present without opening modal.
                textbox = _find_fb_textbox()

                # Open create-post composer if textbox isn't present yet.
                if textbox is None:
                    opened_composer = False
                    for sel in (
                        "div[aria-label*='mind']",
                        "div[aria-label*='Create post']",
                        "div[role='button']:has-text('What')",
                        "div[role='button']:has-text('mind')",
                        "div[role='button']:has-text('Create post')",
                        "a[aria-label='Create a post']",
                        "a[role='link']:has-text('Create post')",
                    ):
                        btn = page.locator(sel).first
                        try:
                            if btn.count() and btn.is_visible():
                                btn.click()
                                opened_composer = True
                                break
                        except Exception:
                            continue
                    if not opened_composer:
                        # Last fallback: open home feed and try again there.
                        page.goto(FACEBOOK_HOME_URL, wait_until="domcontentloaded", timeout=90000)
                        page.wait_for_timeout(600)
                        for sel in (
                            "div[aria-label*='mind']",
                            "div[role='button']:has-text('What')",
                            "div[role='button']:has-text('Create post')",
                        ):
                            btn = page.locator(sel).first
                            try:
                                if btn.count() and btn.is_visible():
                                    btn.click()
                                    opened_composer = True
                                    break
                            except Exception:
                                continue
                    if not opened_composer:
                        return False, "I couldn't open the Facebook post composer."

                    # Wait for editor textbox after opening composer.
                    deadline = time.time() + 14
                    while time.time() < deadline and textbox is None:
                        textbox = _find_fb_textbox()
                        if textbox is not None:
                            break
                        page.wait_for_timeout(350)
                if textbox is None:
                    return False, "I couldn't find the Facebook post text box."

                if not _is_fb_bootstrap_done():
                    _mark_fb_bootstrap_done()

                # Avoid pointer interception overlays by forcing focus/click in the active composer.
                try:
                    textbox.scroll_into_view_if_needed()
                except Exception:
                    pass
                try:
                    textbox.click(force=True)
                except Exception:
                    try:
                        textbox.focus()
                    except Exception:
                        pass
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                page.keyboard.type(message, delay=24)
                page.wait_for_timeout(1000)

                def _find_visible_button(selectors: tuple[str, ...]):
                    for sel in selectors:
                        b = page.locator(sel).first
                        try:
                            if b.count() and b.is_visible():
                                return b
                        except Exception:
                            continue
                    return None

                # Some Facebook composer flows require clicking "Next" before "Post".
                next_btn = _find_visible_button(
                    (
                        "div[role='dialog'] [role='button']:has-text('Next')",
                        "div[role='dialog'] [aria-label='Next']",
                        "[role='button'][aria-label='Next']",
                        "[data-testid='react-composer-post-button-next']",
                        "button:has-text('Next')",
                    )
                )
                if next_btn is not None:
                    next_clicked = False
                    next_deadline = time.time() + 8
                    while time.time() < next_deadline:
                        try:
                            disabled = False
                            if hasattr(next_btn, "is_disabled") and next_btn.is_disabled():
                                disabled = True
                            aria_dis = (next_btn.get_attribute("aria-disabled") or "").lower().strip()
                            if aria_dis in ("true", "1"):
                                disabled = True
                            if not disabled:
                                next_btn.click()
                                next_clicked = True
                                break
                        except Exception:
                            pass
                        page.wait_for_timeout(250)
                    if not next_clicked:
                        return False, "I typed the Facebook post, but the Next button stayed disabled."
                    page.wait_for_timeout(2500)

                # Post settings dialog: try multiple strategies in one session until one works (no extra browser opens).
                def _post_dialog_still_visible() -> bool:
                    try:
                        d = page.locator("div[role='dialog']").first
                        return d.count() > 0 and d.is_visible()
                    except Exception:
                        return True

                def _find_post_button(use_last: bool = True):
                    for sel in (
                        "div[role='dialog']:has-text('Post settings') button:has-text('Post')",
                        "div[role='dialog']:has-text('Save') button:has-text('Post')",
                        "div[role='dialog'] button:has-text('Post')",
                        "div[role='dialog'] [role='button']:has-text('Post')",
                        "[role='dialog'] button:has-text('Post')",
                    ):
                        loc = page.locator(sel).last if use_last else page.locator(sel).first
                        try:
                            if loc.count() and loc.is_visible():
                                return loc
                        except Exception:
                            continue
                    try:
                        r = page.get_by_role("button", name="Post").last if use_last else page.get_by_role("button", name="Post").first
                        if r.count() and r.is_visible():
                            return r
                    except Exception:
                        pass
                    return _find_visible_button(
                        (
                            "div[role='dialog'] div[aria-label='Post']",
                            "[role='button'][aria-label='Post']",
                            "[data-testid='react-composer-post-button']",
                            "button:has-text('Post')",
                        )
                    )

                post_success = False
                # Strategies: (extra_wait_ms, use_last_for_selectors, try_any_post_button)
                _strategies = (
                    (0, True, False),
                    (2000, True, False),
                    (2000, False, False),
                    (1500, True, True),
                    (1000, False, True),
                )
                if _should_prefer_longer_waits("share_facebook"):
                    _strategies = ((2000, True, False), (2000, False, False), (1500, True, True), (1000, False, True), (0, True, False))
                for extra_wait, use_last, try_any in _strategies:
                    page.wait_for_timeout(extra_wait)
                    post_btn = None
                    for _ in range(10):
                        if try_any:
                            try:
                                all_post = page.locator('button:has-text("Post"), [role="button"]:has-text("Post")')
                                for i in range(min(all_post.count(), 5)):
                                    b = all_post.nth(i)
                                    if b.is_visible():
                                        post_btn = b
                                        break
                            except Exception:
                                pass
                        else:
                            post_btn = _find_post_button(use_last=use_last)
                        if post_btn is not None:
                            break
                        page.wait_for_timeout(400)
                    if post_btn is None:
                        continue
                    try:
                        post_btn.scroll_into_view_if_needed()
                    except Exception:
                        pass
                    page.wait_for_timeout(300)
                    clicked = False
                    for _ in range(25):
                        try:
                            disabled = False
                            if hasattr(post_btn, "is_disabled") and post_btn.is_disabled():
                                disabled = True
                            aria_dis = (post_btn.get_attribute("aria-disabled") or "").lower().strip()
                            if aria_dis in ("true", "1"):
                                disabled = True
                            if not disabled:
                                post_btn.click(force=True)
                                clicked = True
                                break
                        except Exception:
                            pass
                        page.wait_for_timeout(350)
                    if not clicked:
                        continue
                    page.wait_for_timeout(2500)
                    if not _post_dialog_still_visible():
                        post_success = True
                        break
                if not post_success:
                    return False, "I typed the Facebook post, but couldn't find or click the Post button in the Post settings dialog (tried several strategies)."
                page.wait_for_timeout(500)
                return True, f"Shared to Facebook ({FACEBOOK_PROFILE_URL}): \"{song_title}\" — {song_url}"
        except Exception as e:
            return False, f"Facebook share automation error: {e}"
        finally:
            try:
                if context:
                    context.close()
            except Exception:
                pass
            if needs_relogin:
                ok_boot, msg_boot = _start_facebook_first_login_window()
                if not ok_boot:
                    print(f"Facebook relogin bootstrap could not start: {msg_boot}", flush=True)
    finally:
        try:
            _fb_share_lock.release()
        except Exception:
            pass


def _youtube_bootstrap_marker_path() -> str:
    return os.path.join(YOUTUBE_PROFILE_DIR, ".login_ready")


def _is_youtube_bootstrap_done() -> bool:
    return os.path.isfile(_youtube_bootstrap_marker_path())


def _mark_youtube_bootstrap_done() -> None:
    try:
        os.makedirs(YOUTUBE_PROFILE_DIR, exist_ok=True)
        with open(_youtube_bootstrap_marker_path(), "w", encoding="utf-8") as f:
            f.write("ready")
    except Exception:
        pass


def _clear_youtube_bootstrap_done() -> None:
    try:
        marker = _youtube_bootstrap_marker_path()
        if os.path.isfile(marker):
            os.unlink(marker)
    except Exception:
        pass


def _extract_youtube_video_id(video_url: str) -> str:
    """Extract YouTube video id from standard/youtu.be/shorts URLs."""
    u = (video_url or "").strip()
    if not u:
        return ""
    try:
        p = urllib.parse.urlparse(u)
        host = (p.netloc or "").lower()
        path = (p.path or "").strip("/")
        if "youtu.be" in host:
            return path.split("/")[0].strip()
        if "youtube.com" in host:
            if path == "watch":
                return (urllib.parse.parse_qs(p.query).get("v") or [""])[0].strip()
            if path.startswith("shorts/") or path.startswith("live/"):
                return path.split("/", 1)[1].split("/")[0].strip()
            if path.startswith("embed/"):
                return path.split("/", 1)[1].split("/")[0].strip()
    except Exception:
        return ""
    return ""


def _is_youtube_shorts_url(video_url: str) -> bool:
    """True when URL points to youtube.com/shorts/..."""
    u = (video_url or "").strip()
    if not u:
        return False
    try:
        p = urllib.parse.urlparse(u)
        host = (p.netloc or "").lower()
        path = (p.path or "").lower()
        return ("youtube.com" in host) and path.startswith("/shorts/")
    except Exception:
        return False


def _fetch_youtube_watch_html(video_id: str) -> str:
    url = f"https://www.youtube.com/watch?v={video_id}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(req, timeout=35) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _extract_caption_base_url_from_watch_html(html_text: str) -> str:
    if not html_text:
        return ""
    # Locate first caption track object and parse baseUrl.
    m = re.search(r'"captionTracks"\s*:\s*(\[[^\]]+\])', html_text, re.DOTALL)
    if not m:
        return ""
    raw = m.group(1)
    try:
        tracks = json.loads(raw)
    except Exception:
        return ""
    if not isinstance(tracks, list) or not tracks:
        return ""
    preferred = None
    for t in tracks:
        try:
            code = (((t.get("languageCode") or "") if isinstance(t, dict) else "") or "").lower()
            if code.startswith("en"):
                preferred = t
                break
        except Exception:
            continue
    chosen = preferred or tracks[0]
    try:
        base = (chosen.get("baseUrl") or "").strip()
    except Exception:
        base = ""
    return base


def _vtt_to_plain_text(vtt_text: str) -> str:
    if not vtt_text:
        return ""
    lines = []
    for raw in vtt_text.splitlines():
        s = (raw or "").strip()
        if not s:
            continue
        if s.startswith("WEBVTT") or s.startswith("Kind:") or s.startswith("Language:") or s.startswith("NOTE"):
            continue
        if "-->" in s:
            continue
        if re.fullmatch(r"\d+", s):
            continue
        s = re.sub(r"<[^>]+>", "", s)
        s = html.unescape(s).strip()
        if s:
            lines.append(s)
    # Deduplicate adjacent duplicate lines.
    out = []
    prev = ""
    for s in lines:
        if s != prev:
            out.append(s)
        prev = s
    return "\n".join(out)


def _extract_meta_description_from_watch_html(html_text: str) -> str:
    """Best-effort extraction of video description snippet from watch HTML."""
    if not html_text:
        return ""
    m = re.search(
        r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']',
        html_text,
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return ""
    desc = html.unescape((m.group(1) or "").strip())
    desc = re.sub(r"\s+", " ", desc).strip()
    return desc[:600]


def _fetch_youtube_transcript(video_url: str) -> tuple[bool, str, str]:
    """
    Return (ok, video_title, transcript_text).
    Uses YouTube captions endpoint where available.
    """
    video_id = _extract_youtube_video_id(video_url)
    if not video_id:
        return False, "", "Could not parse the YouTube video ID."
    try:
        watch_html = _fetch_youtube_watch_html(video_id)
    except Exception as e:
        return False, "", f"Could not open YouTube watch page: {e}"

    title = ""
    m_title = re.search(r"<title>(.*?)</title>", watch_html, re.IGNORECASE | re.DOTALL)
    if m_title:
        title = html.unescape((m_title.group(1) or "").replace("- YouTube", "").strip())

    base_url = _extract_caption_base_url_from_watch_html(watch_html)
    if not base_url:
        return False, title, "No captions were found for this video."

    # Request VTT format for cleaner parsing.
    parsed = urllib.parse.urlparse(base_url)
    qs = urllib.parse.parse_qs(parsed.query)
    qs["fmt"] = ["vtt"]
    new_query = urllib.parse.urlencode(qs, doseq=True)
    caption_url = urllib.parse.urlunparse(parsed._replace(query=new_query))
    try:
        req = urllib.request.Request(
            caption_url,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=35) as resp:
            vtt = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return False, title, f"Could not download captions: {e}"

    transcript = _vtt_to_plain_text(vtt)
    # Shorts often have very short caption payloads; keep threshold low.
    if len(transcript.strip()) < 40:
        return False, title, "Transcript is too short or unavailable for meaningful commenting."
    return True, title, transcript


def _build_youtube_comment_from_transcript(video_title: str, transcript_text: str) -> tuple[bool, str]:
    """Generate a concise, human-sounding YouTube comment from transcript."""
    title = (video_title or "this video").strip()
    sample = (transcript_text or "").strip()
    if len(sample) > 5000:
        sample = sample[:5000]
    prompt = (
        "You write one authentic YouTube comment.\n"
        "Use the transcript to mention one specific insight/value point.\n"
        "Style: warm, supportive, human, not robotic.\n"
        "Constraints: 1-2 sentences, max 220 chars, no hashtags, no emojis spam, no quotes.\n"
        "Return ONLY the final comment text.\n\n"
        f"Video title: {title}\n\nTranscript:\n{sample}"
    )
    payload = json.dumps(
        {
            "model": OLLAMA_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_BASE}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=70) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        txt = ((data.get("message") or {}).get("content") or "").strip()
        txt = re.sub(r"\s+", " ", txt).strip().strip('"').strip("'")
        if not txt:
            return False, "Comment generation returned empty text."
        if len(txt) > 230:
            txt = txt[:227].rstrip() + "..."
        return True, txt
    except Exception as e:
        return False, f"Could not generate comment: {e}"


def _build_youtube_comment_from_video_context(video_title: str, context_text: str) -> tuple[bool, str]:
    """Fallback comment generation when transcript is unavailable (common for Shorts)."""
    title = (video_title or "this short").strip()
    ctx = (context_text or "").strip()
    if len(ctx) > 900:
        ctx = ctx[:900]
    prompt = (
        "Write one friendly YouTube comment for this video.\n"
        "Style: warm, human, engaging call-to-action.\n"
        "Constraints: 1-2 sentences, max 180 chars, no hashtags, no spammy emojis.\n"
        "Return ONLY the final comment text.\n\n"
        f"Video title: {title}\n"
        f"Video context: {ctx or 'Short-form video with limited transcript available.'}"
    )
    payload = json.dumps(
        {
            "model": OLLAMA_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_BASE}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=50) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        txt = ((data.get("message") or {}).get("content") or "").strip()
        txt = re.sub(r"\s+", " ", txt).strip().strip('"').strip("'")
        if not txt:
            return False, "Comment generation returned empty text."
        if len(txt) > 190:
            txt = txt[:187].rstrip() + "..."
        return True, txt
    except Exception as e:
        return False, f"Could not generate context fallback comment: {e}"


def _looks_like_youtube_login_required(page) -> bool:
    try:
        url = (page.url or "").lower()
        if any(k in url for k in ("accounts.google.com", "ServiceLogin".lower(), "/signin")):
            return True
    except Exception:
        pass
    for sel in (
        "a[href*='ServiceLogin']",
        "tp-yt-paper-button:has-text('Sign in')",
        "ytd-button-renderer:has-text('Sign in')",
        "text=Sign in",
    ):
        loc = page.locator(sel).first
        try:
            if loc.count() and loc.is_visible():
                return True
        except Exception:
            continue
    return False


def _launch_youtube_context(playwright):
    """Launch Playwright persistent context for YouTube commenting."""
    opts = {
        "user_data_dir": YOUTUBE_PROFILE_DIR,
        "headless": False,
        "viewport": {"width": 1360, "height": 900},
        "ignore_default_args": ["--enable-automation"],
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    }
    if YOUTUBE_BROWSER_PATH and os.path.isfile(YOUTUBE_BROWSER_PATH):
        opts["executable_path"] = YOUTUBE_BROWSER_PATH
    elif YOUTUBE_BROWSER_CHANNEL in ("chrome", "msedge", "chrome-beta", "msedge-beta", "msedge-dev", "chromium"):
        opts["channel"] = YOUTUBE_BROWSER_CHANNEL
    return playwright.chromium.launch_persistent_context(**opts)


def _start_youtube_first_login_window() -> tuple[bool, str]:
    """
    First-run YouTube login flow: keep browser open for manual login.
    User should log in with the desired account, then close the window.
    """
    global _yt_bootstrap_running
    with _yt_bootstrap_lock:
        if _yt_bootstrap_running:
            return False, "YouTube login window is already open. Finish login there, then close it and try again."
        _yt_bootstrap_running = True

    def _run():
        global _yt_bootstrap_running
        context = None
        opened = False
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                os.makedirs(YOUTUBE_PROFILE_DIR, exist_ok=True)
                context = _launch_youtube_context(p)
                page = context.pages[0] if context.pages else context.new_page()
                page.goto("https://www.youtube.com/", wait_until="domcontentloaded", timeout=90000)
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                opened = True
                while True:
                    try:
                        if not context.pages:
                            break
                        page.wait_for_timeout(1000)
                    except Exception:
                        break
        except Exception:
            pass
        finally:
            try:
                if context:
                    context.close()
            except Exception:
                pass
            if opened:
                _mark_youtube_bootstrap_done()
            with _yt_bootstrap_lock:
                _yt_bootstrap_running = False

    threading.Thread(target=_run, daemon=True).start()
    return True, (
        "First-time YouTube login: I opened the browser and left it open. "
        "Log in with solonaras3@gmail.com, then close that window. After that, run the comment request again."
    )


def _run_youtube_comment(video_url: str) -> tuple[bool, str]:
    """Transcribe a YouTube video and post an AI-written comment."""
    if _yt_bootstrap_running:
        return False, "YouTube login window is still open. Finish login there, then close it and try again."
    if not _yt_comment_lock.acquire(blocking=False):
        return False, "YouTube comment automation is already running. Please wait and try again."

    needs_relogin = False
    context = None
    try:
        video_id = _extract_youtube_video_id(video_url)
        if not video_id:
            return False, "Please provide a valid YouTube video URL."

        ok_tr, title, transcript = _fetch_youtube_transcript(video_url)
        comment_text = ""
        if ok_tr:
            ok_cmt, comment_text = _build_youtube_comment_from_transcript(title, transcript)
            if not ok_cmt:
                comment_text = ""
        if not comment_text:
            # Shorts and some videos have no usable captions; fallback to title/description context.
            context_hint = ""
            try:
                watch_html = _fetch_youtube_watch_html(video_id)
                if not title:
                    m_title = re.search(r"<title>(.*?)</title>", watch_html, re.IGNORECASE | re.DOTALL)
                    if m_title:
                        title = html.unescape((m_title.group(1) or "").replace("- YouTube", "").strip())
                context_hint = _extract_meta_description_from_watch_html(watch_html)
            except Exception:
                pass
            ok_fallback, comment_text = _build_youtube_comment_from_video_context(title, context_hint)
            if not ok_fallback or not comment_text:
                comment_text = "Loved this short. Great energy and clear message - keep these coming!"

        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            return False, "Playwright is not installed. Run: pip install playwright && python -m playwright install chromium"

        with sync_playwright() as p:
            os.makedirs(YOUTUBE_PROFILE_DIR, exist_ok=True)
            context = _launch_youtube_context(p)
            page = context.pages[0] if context.pages else context.new_page()
            watch_url = f"https://www.youtube.com/watch?v={video_id}"
            source_url = (video_url or "").strip() or watch_url
            is_shorts = _is_youtube_shorts_url(source_url)
            # Open the user-provided URL first (including Shorts) so Luna "sees" that content.
            page.goto(source_url, wait_until="domcontentloaded", timeout=90000)
            try:
                page.bring_to_front()
            except Exception:
                pass

            if _looks_like_youtube_login_required(page):
                _clear_youtube_bootstrap_done()
                needs_relogin = True
                return False, "YouTube needs login again. Opening login window..."

            # Cookie banners can block interactions.
            for sel in (
                "button:has-text('Accept all')",
                "button:has-text('Reject all')",
                "tp-yt-paper-button:has-text('Accept all')",
            ):
                b = page.locator(sel).first
                try:
                    if b.count() and b.is_visible():
                        b.click(timeout=1500)
                        page.wait_for_timeout(200)
                except Exception:
                    pass

            if not _is_youtube_bootstrap_done():
                _mark_youtube_bootstrap_done()

            # Scroll comments into view.
            def _locate_comment_placeholder() -> bool:
                for _ in range(18):
                    box = page.locator("ytd-comment-simplebox-renderer #simplebox-placeholder").first
                    try:
                        if box.count() and box.is_visible():
                            return True
                    except Exception:
                        pass
                    try:
                        page.mouse.wheel(0, 1400)
                    except Exception:
                        pass
                    page.wait_for_timeout(320)
                return False

            found = _locate_comment_placeholder()
            if not found and is_shorts:
                try:
                    page.goto(watch_url, wait_until="domcontentloaded", timeout=90000)
                    page.wait_for_timeout(800)
                except Exception:
                    pass
                found = _locate_comment_placeholder()
            if not found:
                for _ in range(3):
                    page.wait_for_timeout(1500)
                    try:
                        page.mouse.wheel(0, 800)
                    except Exception:
                        pass
                    found = _locate_comment_placeholder()
                    if found:
                        break

            if not found:
                return False, "I couldn't open the YouTube comment box (including Shorts fallback and retries)."

            placeholder = page.locator("ytd-comment-simplebox-renderer #simplebox-placeholder").first
            placeholder_clicked = False
            for _ in range(3):
                try:
                    placeholder.click(timeout=3000, force=True)
                    placeholder_clicked = True
                    break
                except Exception:
                    page.wait_for_timeout(600)
            if not placeholder_clicked:
                return False, "I found comments but couldn't activate the comment editor (tried several times)."

            editor = None
            for sel in (
                "ytd-comment-simplebox-renderer #contenteditable-root[contenteditable='true']",
                "ytd-comment-simplebox-renderer [contenteditable='true']",
                "#contenteditable-root[contenteditable='true']",
            ):
                loc = page.locator(sel).first
                try:
                    if loc.count() and loc.is_visible():
                        editor = loc
                        break
                except Exception:
                    continue
            if editor is None:
                return False, "I couldn't find the editable YouTube comment field (tried several selectors)."
            try:
                editor.click(timeout=2500, force=True)
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                page.keyboard.type(comment_text, delay=14)
                page.wait_for_timeout(500)
            except Exception as e:
                return False, f"I couldn't type the YouTube comment: {e}"

            def _yt_submit_enabled(btn):
                try:
                    if btn is None or not btn.count() or not btn.is_visible():
                        return False
                    if hasattr(btn, "is_disabled") and btn.is_disabled():
                        return False
                    aria = (btn.get_attribute("aria-disabled") or "").lower().strip()
                    return aria not in ("true", "1")
                except Exception:
                    return False

            submit_btn = None
            for sel in (
                "ytd-commentbox #submit-button button",
                "ytd-commentbox #submit-button",
                "#submit-button button",
                "ytd-commentbox button[aria-label='Comment']",
            ):
                loc = page.locator(sel).first
                try:
                    if loc.count() and loc.is_visible():
                        submit_btn = loc
                        break
                except Exception:
                    continue
            if submit_btn is None:
                return False, "I typed the comment but couldn't find the YouTube Post button (tried several strategies)."

            _yt_waits = (2000, 1500, 0) if _should_prefer_longer_waits("yt_comment") else (0, 2000, 1500)
            for extra_wait in _yt_waits:
                page.wait_for_timeout(extra_wait)
                deadline = time.time() + 8
                while time.time() < deadline and not _yt_submit_enabled(submit_btn):
                    page.wait_for_timeout(250)
                if not _yt_submit_enabled(submit_btn):
                    continue
                try:
                    submit_btn.click(timeout=5000)
                    break
                except Exception:
                    try:
                        submit_btn.click(timeout=5000, force=True)
                        break
                    except Exception:
                        pass
            else:
                return False, "I typed the YouTube comment, but Post stayed disabled (tried several waits)."

            page.wait_for_timeout(2000)
            short_title = (title or video_id).strip()
            if len(short_title) > 90:
                short_title = short_title[:87].rstrip() + "..."
            return True, f"Comment posted on YouTube video: \"{short_title}\""
    except Exception as e:
        return False, f"YouTube comment automation error: {e}"
    finally:
        try:
            if context:
                context.close()
        except Exception:
            pass
        if needs_relogin:
            ok_boot, msg_boot = _start_youtube_first_login_window()
            if not ok_boot:
                print(f"YouTube relogin bootstrap could not start: {msg_boot}", flush=True)
        try:
            _yt_comment_lock.release()
        except Exception:
            pass


def _instagram_bootstrap_marker_path() -> str:
    return os.path.join(INSTAGRAM_PROFILE_DIR, ".login_ready")


def _is_instagram_bootstrap_done() -> bool:
    return os.path.isfile(_instagram_bootstrap_marker_path())


def _mark_instagram_bootstrap_done() -> None:
    try:
        os.makedirs(INSTAGRAM_PROFILE_DIR, exist_ok=True)
        with open(_instagram_bootstrap_marker_path(), "w", encoding="utf-8") as f:
            f.write("ready")
    except Exception:
        pass


def _clear_instagram_bootstrap_done() -> None:
    try:
        marker = _instagram_bootstrap_marker_path()
        if os.path.isfile(marker):
            os.unlink(marker)
    except Exception:
        pass


def _looks_like_instagram_login_required(page) -> bool:
    try:
        url = (page.url or "").lower()
        if any(k in url for k in ("/accounts/login", "login", "challenge")):
            return True
    except Exception:
        pass
    for sel in ("text=Log in", "text=Login", "input[name='username']"):
        loc = page.locator(sel).first
        try:
            if loc.count() and loc.is_visible():
                return True
        except Exception:
            continue
    return False


def _launch_instagram_context(playwright):
    """Launch Playwright persistent context for Instagram DM automation."""
    opts = {
        "user_data_dir": INSTAGRAM_PROFILE_DIR,
        "headless": False,
        "viewport": {"width": 1360, "height": 900},
        "ignore_default_args": ["--enable-automation"],
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    }
    if INSTAGRAM_BROWSER_PATH and os.path.isfile(INSTAGRAM_BROWSER_PATH):
        opts["executable_path"] = INSTAGRAM_BROWSER_PATH
    elif INSTAGRAM_BROWSER_CHANNEL in ("chrome", "msedge", "chrome-beta", "msedge-beta", "msedge-dev", "chromium"):
        opts["channel"] = INSTAGRAM_BROWSER_CHANNEL
    return playwright.chromium.launch_persistent_context(**opts)


def _start_instagram_first_login_window() -> tuple[bool, str]:
    """
    First-run Instagram login flow: open and keep browser window for manual login.
    """
    global _ig_bootstrap_running
    with _ig_bootstrap_lock:
        if _ig_bootstrap_running:
            return False, "Instagram login window is already open. Finish login there, then close it and try again."
        _ig_bootstrap_running = True

    def _run():
        global _ig_bootstrap_running
        context = None
        opened = False
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                os.makedirs(INSTAGRAM_PROFILE_DIR, exist_ok=True)
                context = _launch_instagram_context(p)
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(f"{INSTAGRAM_BASE_URL}/", wait_until="domcontentloaded", timeout=90000)
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                opened = True
                while True:
                    try:
                        if not context.pages:
                            break
                        page.wait_for_timeout(1000)
                    except Exception:
                        break
        except Exception:
            pass
        finally:
            try:
                if context:
                    context.close()
            except Exception:
                pass
            if opened:
                _mark_instagram_bootstrap_done()
            with _ig_bootstrap_lock:
                _ig_bootstrap_running = False

    threading.Thread(target=_run, daemon=True).start()
    return True, (
        "First-time Instagram login: I opened the browser and left it open. "
        "Log in with solonaras3@gmail.com, then close that window. After that, run the DM request again."
    )


def _build_instagram_dm_message(username: str, custom_message: str = "") -> str:
    msg = (custom_message or "").strip()
    if msg:
        return msg[:500]
    templates = [
        "Hey, I really enjoy your content. Keep it up!",
        "Hi, great content and great energy - wanted to show some support!",
        "Hey! I like what you're posting. Keep creating, you're doing great.",
    ]
    return random.choice(templates)


def _send_instagram_message_in_open_thread(page, dm_text: str) -> tuple[bool, str]:
    """Send message in currently open Instagram thread."""
    composer = None
    composer_selectors = (
        "main form",
        "div[role='main'] form",
        "footer form",
        "form",
    )
    # Let thread UI settle before searching the bottom composer.
    wait_deadline = time.time() + 15
    while time.time() < wait_deadline and composer is None:
        for sel in composer_selectors:
            loc = page.locator(sel).last
            try:
                if loc.count() and loc.is_visible():
                    composer = loc
                    break
            except Exception:
                continue
        if composer is None:
            page.wait_for_timeout(350)

    editor = None
    editor_selectors = (
        "textarea[placeholder='Message...']",
        "textarea[placeholder='Message']",
        "textarea[aria-label='Message']",
        "div[role='textbox'][aria-label='Message'][contenteditable='true']",
        "div[contenteditable='true'][aria-label='Message']",
        "div[role='textbox'][contenteditable='true']",
        "p[contenteditable='true']",
    )
    # Multi-strategy in one session: retry finding editor with extra waits.
    if _should_prefer_longer_waits("ig_dm"):
        page.wait_for_timeout(1500)
    for attempt in range(3):
        wait_deadline = time.time() + (12 if attempt == 0 else 6)
        while time.time() < wait_deadline and editor is None:
            if composer is not None:
                for sel in editor_selectors:
                    loc = composer.locator(sel).first
                    try:
                        if loc.count() and loc.is_visible():
                            editor = loc
                            break
                    except Exception:
                        continue
            if editor is None:
                for sel in editor_selectors:
                    loc = page.locator(sel).last
                    try:
                        if loc.count() and loc.is_visible():
                            editor = loc
                            break
                    except Exception:
                        continue
            if editor is None:
                page.wait_for_timeout(350)
        if editor is not None:
            break
        page.wait_for_timeout(1500)
    if editor is None:
        return False, "I couldn't find the Instagram DM text box at the bottom (tried several times)."

    try:
        editor.scroll_into_view_if_needed(timeout=2500)
    except Exception:
        pass
    try:
        editor.click(timeout=4000, force=True)
        page.keyboard.type(dm_text, delay=26)
        page.wait_for_timeout(1100)
    except Exception as e:
        return False, f"I couldn't type the Instagram DM: {e}"

    sent = False
    send_selectors = (
        "button[type='submit']",
        "button:has-text('Send')",
        "div[role='button']:has-text('Send')",
        "[aria-label='Send']",
        "button[type='submit']",
    )
    for _ in range(2):
        if composer is not None:
            for sel in send_selectors:
                b = composer.locator(sel).last
                try:
                    if b.count() and b.is_visible():
                        b.click(timeout=3500, force=True)
                        sent = True
                        break
                except Exception:
                    continue
        if not sent:
            for sel in send_selectors:
                b = page.locator(sel).last
                try:
                    if b.count() and b.is_visible():
                        b.click(timeout=3500, force=True)
                        sent = True
                        break
                except Exception:
                    continue
        if not sent:
            try:
                page.keyboard.press("Enter")
                sent = True
            except Exception:
                pass
        if sent:
            break
        page.wait_for_timeout(1000)

    if not sent:
        return False, "I typed the Instagram DM but couldn't press Send (tried several strategies)."
    page.wait_for_timeout(1500)
    return True, "sent"


def _run_instagram_dm_by_thread_url(thread_url: str, custom_message: str = "") -> tuple[bool, str]:
    """Open a direct Instagram thread URL and send a DM."""
    target_url = _extract_instagram_thread_url(thread_url)
    if not target_url:
        return False, "Invalid Instagram thread URL."
    if _ig_bootstrap_running:
        return False, "Instagram login window is still open. Finish login there, then close it and try again."
    if not _ig_dm_lock.acquire(blocking=False):
        return False, "Instagram DM automation is already running. Please wait and try again."

    needs_relogin = False
    context = None
    try:
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            return False, "Playwright is not installed. Run: pip install playwright && python -m playwright install chromium"

        dm_text = _build_instagram_dm_message("friend", custom_message)
        with sync_playwright() as p:
            os.makedirs(INSTAGRAM_PROFILE_DIR, exist_ok=True)
            context = _launch_instagram_context(p)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(target_url, wait_until="domcontentloaded", timeout=90000)
            try:
                page.bring_to_front()
            except Exception:
                pass

            if _looks_like_instagram_login_required(page):
                _clear_instagram_bootstrap_done()
                needs_relogin = True
                return False, "Instagram needs login again. Opening login window..."

            for sel in (
                "button:has-text('Allow all cookies')",
                "button:has-text('Only allow essential cookies')",
                "button:has-text('Not Now')",
            ):
                b = page.locator(sel).first
                try:
                    if b.count() and b.is_visible():
                        b.click(timeout=1200)
                        page.wait_for_timeout(220)
                except Exception:
                    pass

            if not _is_instagram_bootstrap_done():
                _mark_instagram_bootstrap_done()

            ok_send, send_msg = _send_instagram_message_in_open_thread(page, dm_text)
            if not ok_send:
                return False, send_msg
            return True, "Instagram message sent in the provided thread."
    except Exception as e:
        return False, f"Instagram DM automation error: {e}"
    finally:
        try:
            if context:
                context.close()
        except Exception:
            pass
        if needs_relogin:
            ok_boot, msg_boot = _start_instagram_first_login_window()
            if not ok_boot:
                print(f"Instagram relogin bootstrap could not start: {msg_boot}", flush=True)
        try:
            _ig_dm_lock.release()
        except Exception:
            pass


def _run_instagram_dm(target: str, custom_message: str = "") -> tuple[bool, str]:
    """Dispatch Instagram DM by username or direct thread URL."""
    target_text = (target or "").strip()
    if not target_text:
        return False, "Missing Instagram target."
    direct = _extract_instagram_thread_url(target_text)
    if direct:
        return _run_instagram_dm_by_thread_url(direct, custom_message)
    return _run_instagram_dm_by_username(target_text, custom_message)


def _run_instagram_dm_by_username(username: str, custom_message: str = "") -> tuple[bool, str]:
    """Open Instagram profile and send a DM to the provided username."""
    target = re.sub(r"^@", "", (username or "").strip().lower())
    if not re.fullmatch(r"[a-z0-9._]{2,30}", target or ""):
        return False, "Invalid Instagram username. Use only letters, numbers, dots, or underscores."
    if _ig_bootstrap_running:
        return False, "Instagram login window is still open. Finish login there, then close it and try again."
    if not _ig_dm_lock.acquire(blocking=False):
        return False, "Instagram DM automation is already running. Please wait and try again."

    needs_relogin = False
    context = None
    try:
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            return False, "Playwright is not installed. Run: pip install playwright && python -m playwright install chromium"

        dm_text = _build_instagram_dm_message(target, custom_message)
        inbox_url = f"{INSTAGRAM_BASE_URL}/direct/inbox/"

        with sync_playwright() as p:
            os.makedirs(INSTAGRAM_PROFILE_DIR, exist_ok=True)
            context = _launch_instagram_context(p)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(inbox_url, wait_until="domcontentloaded", timeout=90000)
            try:
                page.bring_to_front()
            except Exception:
                pass

            if _looks_like_instagram_login_required(page):
                _clear_instagram_bootstrap_done()
                needs_relogin = True
                return False, "Instagram needs login again. Opening login window..."

            # Cookies/notification dialogs can block the Message button.
            for sel in (
                "button:has-text('Allow all cookies')",
                "button:has-text('Only allow essential cookies')",
                "button:has-text('Not Now')",
            ):
                b = page.locator(sel).first
                try:
                    if b.count() and b.is_visible():
                        b.click(timeout=1200)
                        page.wait_for_timeout(220)
                except Exception:
                    pass

            if not _is_instagram_bootstrap_done():
                _mark_instagram_bootstrap_done()

            def _open_thread_in_inbox() -> tuple[bool, str]:
                # 1) Try existing thread in left conversation list.
                for sel in (
                    f"main a[href*='/direct/t/']:has-text('{target}')",
                    f"main div[role='button']:has-text('{target}')",
                    f"main span:has-text('{target}')",
                ):
                    loc = page.locator(sel).first
                    try:
                        if loc.count() and loc.is_visible():
                            loc.click(timeout=3500, force=True)
                            page.wait_for_timeout(900)
                            return True, "thread-opened"
                    except Exception:
                        continue

                # 2) Use inbox search — multi-strategy: retry with extra wait and alternate selectors.
                search_box = None
                _ig_search_waits = (1500, 1000, 0) if _should_prefer_longer_waits("ig_dm") else (0, 1500, 1000)
                for wait_ms in _ig_search_waits:
                    page.wait_for_timeout(wait_ms)
                    for sel in (
                        "aside input[placeholder='Search']",
                        "aside input[aria-label='Search input']",
                        "input[placeholder='Search']",
                        "input[aria-label='Search input']",
                        "input[placeholder*='Search']",
                    ):
                        loc = page.locator(sel).first
                        try:
                            if loc.count() and loc.is_visible():
                                search_box = loc
                                break
                        except Exception:
                            continue
                    if search_box is not None:
                        break
                if search_box is None:
                    return False, "I couldn't find the Instagram inbox search box (tried several strategies)."

                try:
                    search_box.click(timeout=3000, force=True)
                    page.keyboard.press("Control+A")
                    page.keyboard.press("Backspace")
                    page.keyboard.type(target, delay=12)
                    page.wait_for_timeout(1200)
                except Exception as e:
                    return False, f"I couldn't type in Instagram inbox search: {e}"

                user_found = False
                for wait_after in (900, 1500, 1200):
                    page.wait_for_timeout(wait_after)
                    for sel in (
                        f"aside a[href*='/direct/t/']:has-text('{target}')",
                        f"aside div[role='button']:has-text('{target}')",
                        f"div[role='dialog'] [role='button']:has-text('{target}')",
                        f"main a[href*='/direct/t/']:has-text('{target}')",
                        f"a[href*='/direct/']:has-text('{target}')",
                        f"[role='button']:has-text('{target}')",
                    ):
                        loc = page.locator(sel).first
                        try:
                            if loc.count() and loc.is_visible():
                                loc.click(timeout=4000, force=True)
                                page.wait_for_timeout(900)
                                user_found = True
                                break
                        except Exception:
                            continue
                    if user_found:
                        break
                if not user_found:
                    return False, f"I couldn't find @{target} in Instagram inbox results (tried several strategies)."
                return True, "thread-opened-search"

            ok_thread, thread_msg = _open_thread_in_inbox()
            if not ok_thread:
                return False, thread_msg

            editor = None
            _ig_editor_waits = (1200, 1000, 0) if _should_prefer_longer_waits("ig_dm") else (0, 1200, 1000)
            for wait_ms in _ig_editor_waits:
                page.wait_for_timeout(wait_ms)
                for sel in (
                    "textarea[placeholder='Message...']",
                    "textarea[aria-label='Message']",
                    "div[role='textbox'][contenteditable='true']",
                    "div[contenteditable='true'][aria-label='Message']",
                    "div[aria-label='Message'][contenteditable='true']",
                    "textarea[placeholder*='Message']",
                ):
                    loc = page.locator(sel).first
                    try:
                        if loc.count() and loc.is_visible():
                            editor = loc
                            break
                    except Exception:
                        continue
                if editor is not None:
                    break
            if editor is None:
                return False, "I couldn't find the Instagram DM text box (tried several strategies)."

            try:
                editor.click(timeout=4000, force=True)
                page.keyboard.type(dm_text, delay=26)
                page.wait_for_timeout(1100)
            except Exception as e:
                return False, f"I couldn't type the Instagram DM: {e}"

            sent = False
            for _ in range(2):
                for sel in (
                    "button:has-text('Send')",
                    "div[role='button']:has-text('Send')",
                    "button[type='submit']",
                    "[aria-label='Send']",
                ):
                    b = page.locator(sel).first
                    try:
                        if b.count() and b.is_visible():
                            b.click(timeout=3000, force=True)
                            sent = True
                            break
                    except Exception:
                        continue
                if not sent:
                    try:
                        page.keyboard.press("Enter")
                        sent = True
                    except Exception:
                        pass
                if sent:
                    break
                page.wait_for_timeout(800)

            if not sent:
                return False, "I typed the Instagram DM but couldn't send it (tried several strategies)."

            page.wait_for_timeout(1500)
            return True, f"Instagram message sent to @{target}."
    except Exception as e:
        return False, f"Instagram DM automation error: {e}"
    finally:
        try:
            if context:
                context.close()
        except Exception:
            pass
        if needs_relogin:
            ok_boot, msg_boot = _start_instagram_first_login_window()
            if not ok_boot:
                print(f"Instagram relogin bootstrap could not start: {msg_boot}", flush=True)
        try:
            _ig_dm_lock.release()
        except Exception:
            pass


def _whatsapp_web_bootstrap_marker_path() -> str:
    return os.path.join(WHATSAPP_WEB_PROFILE_DIR, ".login_ready")


def _is_whatsapp_web_bootstrap_done() -> bool:
    return os.path.isfile(_whatsapp_web_bootstrap_marker_path())


def _mark_whatsapp_web_bootstrap_done() -> None:
    try:
        os.makedirs(WHATSAPP_WEB_PROFILE_DIR, exist_ok=True)
        with open(_whatsapp_web_bootstrap_marker_path(), "w", encoding="utf-8") as f:
            f.write("ready")
    except Exception:
        pass


def _launch_whatsapp_web_context(playwright):
    """Launch Playwright persistent context for WhatsApp Web."""
    opts = {
        "user_data_dir": WHATSAPP_WEB_PROFILE_DIR,
        "headless": False,
        "viewport": {"width": 1280, "height": 900},
        "ignore_default_args": ["--enable-automation"],
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    }
    if WHATSAPP_WEB_BROWSER_PATH and os.path.isfile(WHATSAPP_WEB_BROWSER_PATH):
        opts["executable_path"] = WHATSAPP_WEB_BROWSER_PATH
    elif WHATSAPP_WEB_BROWSER_CHANNEL in ("chrome", "msedge", "chrome-beta", "msedge-beta", "msedge-dev", "chromium"):
        opts["channel"] = WHATSAPP_WEB_BROWSER_CHANNEL
    return playwright.chromium.launch_persistent_context(**opts)


def _start_whatsapp_web_first_login_window() -> tuple[bool, str]:
    """First-run: open WhatsApp Web and keep browser open until user scans QR (or is already logged in)."""
    global _wa_web_bootstrap_running
    with _wa_web_bootstrap_lock:
        if _wa_web_bootstrap_running:
            return False, "WhatsApp Web login window is already open. Scan the QR code or close the window, then try again."
        _wa_web_bootstrap_running = True

    def _run():
        global _wa_web_bootstrap_running
        context = None
        opened = False
        try:
            from playwright.sync_api import sync_playwright
            os.makedirs(WHATSAPP_WEB_PROFILE_DIR, exist_ok=True)
            with sync_playwright() as p:
                context = _launch_whatsapp_web_context(p)
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(WHATSAPP_WEB_URL, wait_until="domcontentloaded", timeout=90000)
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                opened = True
                while True:
                    try:
                        if not context.pages:
                            break
                        page.wait_for_timeout(1000)
                    except Exception:
                        break
        except Exception as e:
            print(f"WhatsApp Web first-login error: {e}", flush=True)
        finally:
            try:
                if context:
                    context.close()
            except Exception:
                pass
            if opened:
                _mark_whatsapp_web_bootstrap_done()
            with _wa_web_bootstrap_lock:
                _wa_web_bootstrap_running = False

    threading.Thread(target=_run, daemon=True).start()
    return True, (
        "Opening WhatsApp Web. Scan the QR code with your phone if needed. "
        "When you're logged in, close the browser window. Next time Luna will use this session for !call / !msg."
    )


def _get_or_create_wa_web_context():
    """Get existing WhatsApp Web browser context (window stays open) or create a new one. Returns (context, page, is_new)."""
    global _wa_web_context, _wa_web_playwright
    try:
        if _wa_web_context is not None:
            try:
                pages = _wa_web_context.pages
                if pages and not pages[0].is_closed():
                    return _wa_web_context, pages[0], False
            except Exception:
                pass
            _wa_web_context = None
            _wa_web_playwright = None
    except Exception:
        pass
    from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    _wa_web_playwright = p
    os.makedirs(WHATSAPP_WEB_PROFILE_DIR, exist_ok=True)
    _wa_web_context = _launch_whatsapp_web_context(p)
    page = _wa_web_context.pages[0] if _wa_web_context.pages else _wa_web_context.new_page()
    return _wa_web_context, page, True


def _generate_whatsapp_message_from_context(description: str) -> str:
    """Use the description as context and generate a short WhatsApp message (e.g. friendly, signed from Luna)."""
    system = (
        "You are Luna, a friendly assistant. The user will give you a brief context or topic. "
        "Reply with exactly one short WhatsApp-style message (1-2 sentences, friendly and natural). "
        "The message should be inspired by the context, not a literal repeat. End with 'from Luna' or '- from Luna'. "
        "Output only the message text, no quotes, no explanation."
    )
    try:
        out = ollama_chat(description.strip(), system_prompt=system, memory_scope=None, message_history=None)
        out = (out or "").strip()
        if out and len(out) < 500 and ("luna" in out.lower() or "from" in out.lower()):
            return out
        if out and len(out) < 500:
            return out + " - from Luna"
    except Exception:
        pass
    return (description.strip() + " - from Luna").strip()


def _parse_whatsapp_msg_args(args: str) -> tuple[str, str | None]:
    """Split '!msg' arguments into contact name and optional description. E.g. 'Marios goodnight' -> ('Marios', 'goodnight')."""
    s = (args or "").strip()
    if not s:
        return "", None
    parts = s.split(None, 1)
    contact = parts[0] or ""
    description = (parts[1].strip() or None) if len(parts) > 1 else None
    return contact, description


def _default_whatsapp_message() -> str:
    """Return a default message when user doesn't provide a description (time-based or friendly)."""
    hour = time.localtime().tm_hour
    if 5 <= hour < 12:
        return "Have a wonderful day from Luna"
    if 12 <= hour < 17:
        return "Hope you're having a great day - from Luna"
    if 17 <= hour < 21:
        return "Have a lovely evening from Luna"
    if hour >= 21 or hour < 5:
        return "Goodnight from Luna"
    return "Have a wonderful day from Luna"


def _run_whatsapp_web_open_contact(contact_name: str, message_to_send: str | None = None) -> tuple[bool, str]:
    """Open WhatsApp Web, ensure logged in, search for contact, open chat. Optionally type and send a message. Leaves browser window open."""
    contact_name = (contact_name or "").strip()
    if not contact_name:
        return False, "Please provide a contact name (e.g. !msg Marios)."
    with _wa_web_lock:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return False, "Playwright is not installed. Run: pip install playwright && playwright install chromium"
        if not _is_whatsapp_web_bootstrap_done():
            ok, msg = _start_whatsapp_web_first_login_window()
            return False, msg + " Then run !call or !msg again."
        last_error = "unknown"
        for attempt in range(2):
            try:
                context, page, is_new = _get_or_create_wa_web_context()
                if is_new:
                    page.goto(WHATSAPP_WEB_URL, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(3000)
                else:
                    page.goto(WHATSAPP_WEB_URL, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(1500)
                qr = page.locator('canvas[aria-label*="QR"]').first
                try:
                    if qr.count() and qr.is_visible():
                        _start_whatsapp_web_first_login_window()
                        return False, "WhatsApp Web session expired. Luna opened the login window — scan the QR code, then try !call again."
                except Exception:
                    pass
                search_selectors = [
                    '[data-tab="3"]',
                    '[aria-label="Search input textbox"]',
                    '[aria-label="Search name or number"]',
                    'div[contenteditable="true"][data-tab="3"]',
                    '[contenteditable="true"][data-tab="3"]',
                    'footer + div [contenteditable="true"]',
                ]
                search_box = None
                _wa_prefer = _should_prefer_longer_waits("msg") or _should_prefer_longer_waits("call")
                _wa_search_waits = (1500, 1000, 0) if _wa_prefer else (0, 1500, 1000)
                for wait_ms in _wa_search_waits:
                    page.wait_for_timeout(wait_ms)
                    for sel in search_selectors:
                        loc = page.locator(sel).first
                        try:
                            if loc.count() and loc.is_visible():
                                search_box = loc
                                break
                        except Exception:
                            continue
                    if search_box:
                        break
                if not search_box:
                    return False, "Could not find the search box on WhatsApp Web (tried several strategies). Try logging in again."
                search_box.click()
                page.wait_for_timeout(400)
                search_box.fill("")
                page.wait_for_timeout(200)
                search_box.press_sequentially(contact_name, delay=50)
                contact_clicked = False
                for wait_ms in (2000, 1500, 1000):
                    page.wait_for_timeout(wait_ms)
                    for sel in [
                        f'[role="listitem"]:has-text("{contact_name}")',
                        f'div[data-testid="cell-frame-container"]:has-text("{contact_name}")',
                        f'span[dir="auto"]:has-text("{contact_name}")',
                        f'[data-testid="cell-frame-container"]:has-text("{contact_name}")',
                    ]:
                        try:
                            row = page.locator(sel).first
                            if row.count() and row.is_visible():
                                row.click()
                                contact_clicked = True
                                break
                        except Exception:
                            continue
                    if contact_clicked:
                        break
                    try:
                        page.get_by_text(contact_name, exact=False).first.click()
                        contact_clicked = True
                        break
                    except Exception:
                        pass
                page.wait_for_timeout(800)
                if message_to_send:
                    # Prefer the chat message box (placeholder "Type a message"), NOT the left-pane search bar
                    msg_input_selectors = [
                        '[data-placeholder="Type a message"]',
                        'div[contenteditable="true"][data-placeholder="Type a message"]',
                        'footer div[contenteditable="true"][data-placeholder="Type a message"]',
                        'footer [contenteditable="true"]',  # chat footer (only one with contenteditable in footer)
                        '[contenteditable="true"][data-tab="10"]',
                        'div[contenteditable="true"].selectable-text',
                        'footer div[contenteditable="true"]',
                        '[data-tab="10"]',
                        'div[contenteditable="true"][role="textbox"]',
                    ]
                    sent_ok = False
                    _wa_msg_waits = (1200, 800, 0) if _wa_prefer else (0, 1200, 800)
                    for wait_ms in _wa_msg_waits:
                        page.wait_for_timeout(wait_ms)
                        msg_input = None
                        # First try: explicit "Type a message" box (chat input, not search)
                        try:
                            pl = page.get_by_placeholder("Type a message")
                            if pl.count() and pl.first.is_visible():
                                msg_input = pl.first
                        except Exception:
                            pass
                        if not msg_input:
                            for sel in msg_input_selectors:
                                loc = page.locator(sel).first
                                try:
                                    if loc.count() and loc.is_visible():
                                        # Skip if this is the search box (left pane)
                                        aria = loc.get_attribute("aria-label") or ""
                                        if "search" in aria.lower() or "search" in (loc.get_attribute("data-tab") or ""):
                                            continue
                                        msg_input = loc
                                        break
                                except Exception:
                                    continue
                        if not msg_input:
                            continue
                        try:
                            msg_input.click()
                            page.wait_for_timeout(300)
                            msg_input.fill("")
                            page.wait_for_timeout(100)
                            msg_input.press_sequentially(message_to_send, delay=30)
                            page.wait_for_timeout(400)
                            for send_sel in ('[data-testid="send"]', '[data-icon="send"]', '[aria-label="Send"]'):
                                send_btn = page.locator(send_sel).first
                                try:
                                    if send_btn.count() and send_btn.is_visible():
                                        send_btn.click()
                                        sent_ok = True
                                        break
                                except Exception:
                                    continue
                            if not sent_ok:
                                page.keyboard.press("Enter")
                                sent_ok = True
                            if sent_ok:
                                page.wait_for_timeout(500)
                                return True, f"Sent to **{contact_name}**: \"{message_to_send[:50]}{'…' if len(message_to_send) > 50 else ''}\". Window left open."
                        except Exception:
                            continue
                    if not sent_ok:
                        return True, f"Opened chat with **{contact_name}** but couldn't send the message (tried several strategies). Window left open."
                return True, f"Opened chat with **{contact_name}**. Window left open."
            except Exception as e:
                last_error = str(e) or "unknown"
                err_lower = last_error.lower()
                if attempt == 0 and ("cannot switch to a different thread" in last_error or "exited" in err_lower):
                    _clear_wa_web_context()
                    continue
                return False, f"WhatsApp Web error: {last_error[:200]}"
        return False, f"WhatsApp Web error: {last_error[:200] if last_error else 'unknown'}"


def _get_whatsapp_desktop_exe() -> str | None:
    """Return path to WhatsApp Desktop exe on Windows, or None if not found (fallback when Start menu launch fails)."""
    if sys.platform != "win32":
        return None
    localappdata = os.environ.get("LOCALAPPDATA", "").strip()
    if not localappdata:
        return None
    candidates = [
        os.path.join(localappdata, "WhatsApp", "WhatsApp.exe"),
        os.path.join(localappdata, "Programs", "WhatsApp", "WhatsApp.exe"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _launch_whatsapp_via_start_search_ui() -> bool:
    """Launch WhatsApp by opening Start search, typing 'whatsApp', and pressing Enter (same place user finds the app)."""
    if sys.platform != "win32":
        return False
    try:
        from pywinauto import Application
    except ImportError:
        return False
    try:
        VK_LWIN = 0x5B
        KEYEVENTF_KEYUP = 0x0002
        INPUT_KEYBOARD = 1
        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", ctypes.c_ushort),
                ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]
        class INPUT(ctypes.Structure):
            _fields_ = [("type", ctypes.c_ulong), ("ki", KEYBDINPUT)]
        down = INPUT(INPUT_KEYBOARD, KEYBDINPUT(VK_LWIN, 0, 0, 0, None))
        up = INPUT(INPUT_KEYBOARD, KEYBDINPUT(VK_LWIN, 0, KEYEVENTF_KEYUP, 0, None))
        ctypes.windll.user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
        time.sleep(0.08)
        ctypes.windll.user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))
    except Exception:
        return False
    time.sleep(0.9)
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return False
        app = Application(backend="uia").connect(handle=hwnd)
        win = app.window(handle=hwnd)
        win.set_focus()
        win.type_keys("whatsapp", with_spaces=False)
        time.sleep(1.4)
        win.type_keys("{ENTER}")
        return True
    except Exception:
        return False


def _launch_whatsapp_via_start_menu() -> bool:
    """Launch WhatsApp via Windows Start: open Start search, type 'whatsApp', Enter (same UI as user). Fallback: Start-Process then exe."""
    if sys.platform != "win32":
        return False
    if _launch_whatsapp_via_start_search_ui():
        return True
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Start-Process 'WhatsApp'"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        return True
    except Exception:
        pass
    exe = _get_whatsapp_desktop_exe()
    if exe:
        try:
            subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=os.path.dirname(exe))
            return True
        except Exception:
            pass
    return False


def _run_whatsapp_desktop_call(contact_name: str) -> tuple[bool, str]:
    """Open WhatsApp Desktop (via Start menu if needed), search contact, then find and click Call then Voice. Uses pywinauto (Windows)."""
    if sys.platform != "win32":
        return False, "WhatsApp Desktop call is only supported on Windows."
    try:
        from pywinauto import Application
        from pywinauto.findwindows import ElementNotFoundError
    except ImportError:
        return False, "Install pywinauto for desktop call: pip install pywinauto"
    contact_name = (contact_name or "").strip()
    if not contact_name:
        return False, "Please provide a contact name (e.g. Marios)."
    try:
        # If already open: connect to existing window by title
        app = None
        try:
            app = Application(backend="uia").connect(title_re=".*WhatsApp.*", timeout=3)
        except Exception:
            pass
        if app is None:
            # Not running: launch via Start menu (or exe fallback)
            if not _launch_whatsapp_via_start_menu():
                return False, "Could not start WhatsApp (Start menu or exe). Install WhatsApp Desktop and try again."
            time.sleep(4)
            try:
                app = Application(backend="uia").connect(title_re=".*WhatsApp.*", timeout=15)
            except Exception as e:
                return False, f"WhatsApp did not open in time: {e}"
        win = app.window(title_re=".*WhatsApp.*")
        win.wait("ready", timeout=5)
        win.restore()
        win.set_focus()
        time.sleep(0.5)

        # Search for contact: find search box and type name
        try:
            search = win.child_window(title_re=".*Search.*", control_type="Edit")
            search.wait("ready", timeout=3)
            search.set_focus()
            search.set_edit_text("")
            time.sleep(0.2)
            search.type_keys(contact_name, with_spaces=True)
            time.sleep(1.2)
            win.type_keys("{ENTER}")
            time.sleep(1.0)
        except (ElementNotFoundError, Exception):
            for c in win.descendants(control_type="Edit"):
                try:
                    c.set_focus()
                    c.set_edit_text("")
                    time.sleep(0.2)
                    c.type_keys(contact_name, with_spaces=True)
                    time.sleep(1.2)
                    win.type_keys("{ENTER}")
                    time.sleep(1.0)
                    break
                except Exception:
                    continue

        # Search for Call button until found (then click)
        call_clicked = False
        call_names = ("Call", "Phone", "call", "phone", "Voice call", "Video call")
        for _ in range(20):
            time.sleep(0.5)
            for name in call_names:
                try:
                    btn = win.child_window(title_re=f".*{name}.*", control_type="Button")
                    if btn.exists(timeout=0):
                        btn.click_input()
                        call_clicked = True
                        break
                except Exception:
                    continue
            if call_clicked:
                break
            for desc in win.descendants(control_type="Button"):
                try:
                    t = (desc.window_text() or "").lower()
                    if "call" in t or "phone" in t:
                        desc.click_input()
                        call_clicked = True
                        break
                except Exception:
                    continue
            if call_clicked:
                break
        if not call_clicked:
            return False, "Could not find the Call button in WhatsApp Desktop. Open the chat and try the call from the app."
        time.sleep(1.0)

        # Search for Voice button until found (then click)
        voice_clicked = False
        voice_names = ("Voice", "Voice call", "voice", "Voice Call")
        for _ in range(20):
            time.sleep(0.5)
            for name in voice_names:
                try:
                    btn = win.child_window(title_re=f".*{name}.*", control_type="Button")
                    if btn.exists(timeout=0):
                        btn.click_input()
                        voice_clicked = True
                        break
                except Exception:
                    continue
            if voice_clicked:
                break
            for desc in win.descendants(control_type="Button"):
                try:
                    if "voice" in (desc.window_text() or "").lower():
                        desc.click_input()
                        voice_clicked = True
                        break
                except Exception:
                    continue
            if voice_clicked:
                break
        if not voice_clicked:
            return True, f"Opened call menu for **{contact_name}** but could not find Voice button — please click Voice call yourself."
        return True, f"Started voice call with **{contact_name}** in WhatsApp Desktop."
    except Exception as e:
        return False, f"WhatsApp Desktop error: {e}"


def _run_whatsapp_web_call(contact_name: str) -> tuple[bool, str]:
    """Try WhatsApp Desktop first (open app, find Call then Voice). Fall back to opening chat on Web if desktop fails."""
    ok, msg = _run_whatsapp_desktop_call(contact_name)
    if ok:
        return ok, msg
    # Fallback: open chat on Web so user can call manually
    ok_web, msg_web = _run_whatsapp_web_open_contact(contact_name)
    if not ok_web:
        return ok_web, msg_web
    return True, f"{msg} Opened chat with **{contact_name}** on WhatsApp Web so you can call from there."


def _run_whatsapp_web_msg(contact_name: str, description: str | None = None) -> tuple[bool, str]:
    """Open WhatsApp Web, open chat with contact, and send a message. If no description, send a default (e.g. 'Have a wonderful day from Luna'). If description is given, use it as context to generate the message (not word-for-word)."""
    if description is not None and description.strip():
        message = employee_copywriter_whatsapp(description)
    else:
        message = _default_whatsapp_message()
    return _run_whatsapp_web_open_contact(contact_name, message_to_send=message)


def _messenger_bootstrap_marker_path() -> str:
    return os.path.join(MESSENGER_PROFILE_DIR, ".login_ready")


def _is_messenger_bootstrap_done() -> bool:
    return os.path.isfile(_messenger_bootstrap_marker_path())


def _mark_messenger_bootstrap_done() -> None:
    try:
        os.makedirs(MESSENGER_PROFILE_DIR, exist_ok=True)
        with open(_messenger_bootstrap_marker_path(), "w", encoding="utf-8") as f:
            f.write("ready")
    except Exception:
        pass


def _launch_messenger_context(playwright):
    """Launch Playwright persistent context for Facebook Messenger (messenger.com)."""
    opts = {
        "user_data_dir": MESSENGER_PROFILE_DIR,
        "headless": False,
        "viewport": {"width": 1280, "height": 900},
        "ignore_default_args": ["--enable-automation"],
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    }
    if MESSENGER_BROWSER_PATH and os.path.isfile(MESSENGER_BROWSER_PATH):
        opts["executable_path"] = MESSENGER_BROWSER_PATH
    elif MESSENGER_BROWSER_CHANNEL in ("chrome", "msedge", "chrome-beta", "msedge-beta", "msedge-dev", "chromium"):
        opts["channel"] = MESSENGER_BROWSER_CHANNEL
    return playwright.chromium.launch_persistent_context(**opts)


def _start_messenger_first_login_window() -> tuple[bool, str]:
    """First-run: open Facebook (or Messenger) and keep browser open until user logs in."""
    global _messenger_bootstrap_running
    with _messenger_bootstrap_lock:
        if _messenger_bootstrap_running:
            return False, "Messenger login window is already open. Log in with Facebook, then close it and try again."
        _messenger_bootstrap_running = True

    url = FACEBOOK_HOME_URL if MESSENGER_OPEN_ON_FACEBOOK else MESSENGER_URL
    def _run():
        global _messenger_bootstrap_running
        context = None
        opened = False
        try:
            from playwright.sync_api import sync_playwright
            os.makedirs(MESSENGER_PROFILE_DIR, exist_ok=True)
            with sync_playwright() as p:
                context = _launch_messenger_context(p)
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                opened = True
                while True:
                    try:
                        if not context.pages:
                            break
                        page.wait_for_timeout(1000)
                    except Exception:
                        break
        except Exception as e:
            print(f"Messenger first-login error: {e}", flush=True)
        finally:
            try:
                if context:
                    context.close()
            except Exception:
                pass
            if opened:
                _mark_messenger_bootstrap_done()
            with _messenger_bootstrap_lock:
                _messenger_bootstrap_running = False

    threading.Thread(target=_run, daemon=True).start()
    return True, (
        "Opening Messenger. Log in with your Facebook account if prompted. "
        "When you're in, close the browser window. Next time Luna will use this session for !fb_msg."
    )


def _default_messenger_message() -> str:
    """Default message when user doesn't provide one (same style as WhatsApp)."""
    return _default_whatsapp_message()


def _get_or_create_messenger_context():
    """Get existing Messenger browser context or create one. Window is left open (not closed)."""
    global _messenger_context, _messenger_playwright
    try:
        if _messenger_context is not None:
            try:
                if _messenger_context.pages and not _messenger_context.pages[0].is_closed():
                    return _messenger_context, _messenger_context.pages[0], False
            except Exception:
                pass
            _messenger_context = None
            _messenger_playwright = None
    except Exception:
        pass
    from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    _messenger_playwright = p
    os.makedirs(MESSENGER_PROFILE_DIR, exist_ok=True)
    _messenger_context = _launch_messenger_context(p)
    page = _messenger_context.pages[0] if _messenger_context.pages else _messenger_context.new_page()
    return _messenger_context, page, True


def _run_messenger_msg(username: str, message: str = "") -> tuple[bool, str]:
    """Open Facebook (or Messenger), open the Messenger chat window for the user, send message. Keeps browser open."""
    username = (username or "").strip()
    if not username:
        return False, "Please provide a Messenger contact (name or username)."
    msg_text = (message or "").strip() or _default_messenger_message()
    if not _messenger_lock.acquire(blocking=False):
        return False, "Messenger automation is already running. Please wait and try again."
    try:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return False, "Playwright is not installed. Run: pip install playwright && playwright install chromium"
        if not _is_messenger_bootstrap_done():
            _messenger_lock.release()
            ok, out = _start_messenger_first_login_window()
            return False, out + " Then run !fb_msg again."
        context, page, is_new = _get_or_create_messenger_context()
        if is_new:
            page.goto(MESSENGER_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
        else:
            page.goto(MESSENGER_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)
        # Login required?
        if "facebook.com" in page.url and ("login" in page.url.lower() or "checkpoint" in page.url.lower()):
            _messenger_lock.release()
            _start_messenger_first_login_window()
            return False, "Facebook/Messenger session expired. Luna opened the login window — log in, then try !fb_msg again."
        # On Facebook.com: go to the user's profile and click the "Message" button to open the chat (not search)
        if "facebook.com" in page.url and MESSENGER_OPEN_ON_FACEBOOK:
            # Username for URL: allow "antonia.constandinou" or "Antonia Constandinou" -> antonia.constandinou
            profile_slug = username.replace(" ", ".").strip().lower()
            if not re.match(r"^[a-z0-9._-]+$", profile_slug):
                profile_slug = re.sub(r"[^a-zA-Z0-9._-]", "", username).replace(" ", ".")
            profile_url = f"{FACEBOOK_HOME_URL.rstrip('/')}/{profile_slug}"
            page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3500)
            # Click the blue "Message" button on the profile (opens the right-side chat window)
            message_btn = None
            for sel in [
                'a[href*="/messages/t/"]',
                'span:has-text("Message")',
                'div[aria-label="Message"]',
                'button:has-text("Message")',
                'a:has-text("Message")',
                '[role="button"]:has-text("Message")',
            ]:
                try:
                    btn = page.locator(sel).first
                    if btn.count() and btn.is_visible():
                        message_btn = btn
                        break
                except Exception:
                    continue
            if not message_btn or not message_btn.count():
                return False, "Could not find the Message button on the profile. Check the username (e.g. antonia.constandinou)."
            message_btn.click()
            page.wait_for_timeout(3500)
            if _should_prefer_longer_waits("fb_msg"):
                page.wait_for_timeout(1500)
        else:
            # messenger.com or fallback: search then open conversation
            search_selectors = [
                'input[placeholder*="Search"]',
                'input[aria-label*="Search"]',
                'div[role="search"] input',
                'input[type="search"]',
            ]
            search_box = None
            for sel in search_selectors:
                loc = page.locator(sel).first
                try:
                    if loc.count() and loc.is_visible():
                        search_box = loc
                        break
                except Exception:
                    continue
            if not search_box:
                return False, "Could not find the search box. Try logging in again (close and run !fb_msg again)."
            search_box.click()
            page.wait_for_timeout(400)
            search_box.fill("")
            page.wait_for_timeout(200)
            search_box.press_sequentially(username, delay=40)
            page.wait_for_timeout(2800)
            for sel in [
                f'[role="listitem"]:has-text("{username}")',
                f'a[href*="/messages"]:has-text("{username}")',
                f'div[role="button"]:has-text("{username}")',
                f'span:has-text("{username}")',
            ]:
                try:
                    row = page.locator(sel).first
                    if row.count() and row.is_visible():
                        row.click()
                        break
                except Exception:
                    continue
            else:
                try:
                    page.get_by_text(username, exact=False).first.click()
                except Exception:
                    pass
            page.wait_for_timeout(1500)
        # Message input: the chat has a row of 4 icons; in the middle is the text box with "Aa" — type only there (never in profile post comments).
        page.wait_for_timeout(1500)
        msg_input = None
        vw = page.viewport_size.get("width", 1000) if page.viewport_size else 1000
        # 0) The chat message box is the one with "Aa" (middle of the input bar) — not profile comments
        for aa_sel in [
            '[data-placeholder*="Aa"]',
            '[placeholder*="Aa" i]',
            '[aria-placeholder*="Aa" i]',
            'div[contenteditable="true"][data-lexical-editor="true"]',
        ]:
            try:
                loc = page.locator(aa_sel).first
                if loc.count() and loc.is_visible():
                    box = loc.bounding_box()
                    if box and box["x"] >= vw * 0.35:
                        msg_input = loc
                        break
            except Exception:
                continue
        if not msg_input:
            try:
                aa_label = page.get_by_text("Aa", exact=True).first
                if aa_label.count() and aa_label.is_visible():
                    for candidate in [
                        aa_label.locator('xpath=preceding-sibling::*[@contenteditable="true"][1]'),
                        aa_label.locator('xpath=preceding-sibling::*[1]//*[@contenteditable="true"]'),
                        aa_label.locator('xpath=preceding-sibling::*[1]'),
                        aa_label.locator('xpath=following-sibling::*[@contenteditable="true"][1]'),
                        aa_label.locator('..').locator('[contenteditable="true"]').first,
                    ]:
                        if not candidate.count() or not candidate.is_visible():
                            continue
                        el = candidate.first
                        if el.get_attribute("contenteditable") == "true":
                            box = el.bounding_box()
                            if box and box["x"] >= vw * 0.35:
                                msg_input = el
                                break
                    if not msg_input and aa_label.locator('..').locator('[contenteditable="true"]').count():
                        msg_input = aa_label.locator('..').locator('[contenteditable="true"]').first
            except Exception:
                pass
        # 1) Find by emoji icon (message box is left of "Choose an emoji")
        for emoji_sel in ['[aria-label*="emoji" i]', '[aria-label*="Choose an emoji" i]', '[title*="emoji" i]']:
            try:
                parent = page.locator(emoji_sel).first
                if parent.count() and parent.is_visible():
                    candidate = parent.locator('xpath=preceding-sibling::*[1]').first
                    if candidate.count() and candidate.is_visible() and candidate.get_attribute("contenteditable") == "true":
                        msg_input = candidate
                        break
                    candidate = parent.locator('..').locator('div[contenteditable="true"]').first
                    if candidate.count() and candidate.is_visible():
                        msg_input = candidate
                        break
            except Exception:
                continue
        # 2) Role and common selectors (prefer right side of screen = chat panel)
        if not msg_input:
            for sel in [
                'div[role="dialog"] div[contenteditable="true"][role="textbox"]',
                'div[role="dialog"] div[contenteditable="true"]',
                '[data-pagelet*="Messenger"] div[contenteditable="true"]',
                'div[contenteditable="true"][role="textbox"]',
                'div[contenteditable="true"][data-placeholder*="message"]',
                'div[contenteditable="true"][data-placeholder*="Message"]',
                'div[contenteditable="true"]',
            ]:
                try:
                    loc = page.locator(sel).first
                    if not loc.count() or not loc.is_visible():
                        continue
                    box = loc.bounding_box()
                    if box and box["x"] < vw * 0.45:
                        continue
                    msg_input = loc
                    break
                except Exception:
                    continue
        # 3) get_by_role("textbox") in case the message box has that role
        if not msg_input:
            try:
                for role_loc in [page.get_by_role("textbox"), page.locator('[role="textbox"]')]:
                    for i in range(role_loc.count()):
                        loc = role_loc.nth(i)
                        if loc.is_visible():
                            box = loc.bounding_box()
                            if box and box["x"] >= vw * 0.45:
                                msg_input = loc
                                break
                    if msg_input:
                        break
            except Exception:
                pass
        # 4) JS fallback: mark the rightmost contenteditable in the right half of the viewport, then select it
        if not msg_input:
            try:
                marked = page.evaluate("""() => {
                    const vw = window.innerWidth;
                    const edits = document.querySelectorAll('[contenteditable="true"]');
                    let best = null;
                    let maxX = vw * 0.35;
                    for (const el of edits) {
                        const r = el.getBoundingClientRect();
                        if (r.width < 20 || r.height < 15) continue;
                        if (r.x >= maxX && r.x < vw - 40 && r.bottom > window.innerHeight * 0.5) {
                            maxX = r.x;
                            best = el;
                        }
                    }
                    if (best) { best.setAttribute('data-luna-msgbox', '1'); return true; }
                    return false;
                }""")
                if marked:
                    msg_input = page.locator('[data-luna-msgbox="1"]').first
            except Exception:
                pass
        # 5) Last resort: any contenteditable in the right 60% of the page
        if not msg_input:
            try:
                all_edits = page.locator('div[contenteditable="true"]')
                for i in range(min(all_edits.count(), 15)):
                    loc = all_edits.nth(i)
                    if not loc.is_visible():
                        continue
                    box = loc.bounding_box()
                    if box and box["x"] >= vw * 0.35 and box["width"] > 50:
                        msg_input = loc
                        break
            except Exception:
                pass
        # If still not found, retry detection after extra wait (same session — no new browser open)
        for retry_attempt in range(2):
            if msg_input:
                break
            page.wait_for_timeout(2000)
            for aa_sel in ['[data-placeholder*="Aa"]', 'div[contenteditable="true"][data-lexical-editor="true"]']:
                try:
                    loc = page.locator(aa_sel).first
                    if loc.count() and loc.is_visible() and loc.bounding_box() and loc.bounding_box().get("x", 0) >= vw * 0.35:
                        msg_input = loc
                        break
                except Exception:
                    continue
            if not msg_input:
                try:
                    for sel in ['div[role="dialog"] div[contenteditable="true"]', 'div[contenteditable="true"]']:
                        loc = page.locator(sel)
                        for i in range(min(loc.count(), 10)):
                            L = loc.nth(i)
                            if L.is_visible():
                                box = L.bounding_box()
                                if box and box.get("x", 0) >= vw * 0.35:
                                    msg_input = L
                                    break
                        if msg_input:
                            break
                except Exception:
                    pass
        if not msg_input:
            return False, "Could not find the message box in the chat window (contenteditable). Make sure the Message button opened the chat."
        # Focus the chat message box, clear it, type the message (no .fill() — use only contenteditable-friendly input)
        msg_input.click()
        page.wait_for_timeout(400)
        # Clear any existing text: Ctrl+A then Backspace (works in contenteditable)
        page.keyboard.press("Control+a")
        page.wait_for_timeout(50)
        page.keyboard.press("Backspace")
        page.wait_for_timeout(150)
        # Type the message so the user sees it being written; keep window open until sent
        msg_input.press_sequentially(msg_text, delay=35)
        page.wait_for_timeout(500)
        # Send with Enter (as requested)
        page.keyboard.press("Enter")
        page.wait_for_timeout(800)
        # Leave the browser window open (do not close context in finally)
        return True, f"Opened the Messenger chat with **{username}** and sent: \"{msg_text[:50]}{'…' if len(msg_text) > 50 else ''}\". Window left open."
    except Exception as e:
        err = (str(e) or "unknown")[:200]
        return False, f"Messenger error: {err}"
    finally:
        try:
            _messenger_lock.release()
        except Exception:
            pass
        # Do not close the browser — keep the window open so the user sees the message written and sent


def _can_use_whatsapp_discord(author_id: int) -> bool:
    """Allow WhatsApp call/msg automation on Discord only for linked user or admin."""
    if _discord_admin_id_int is not None and author_id == _discord_admin_id_int:
        return True
    if _linked_discord_id_int is not None and author_id == _linked_discord_id_int:
        return True
    return False


def _can_use_messenger_discord(author_id: int) -> bool:
    """Allow Messenger automation on Discord only for linked user or admin."""
    return _can_use_whatsapp_discord(author_id)


def _stream_tts_chunks(text: str):
    """Stream TTS with gTTS: yield SSE events as each chunk is ready."""
    for chunk_text in _split_into_chunks(text):
        if not chunk_text.strip():
            continue
        try:
            audio_bytes = _generate_tts(chunk_text)
            if audio_bytes:
                b64 = base64.b64encode(audio_bytes).decode("ascii")
                yield f"event: audio\ndata: {b64}\n\n"
        except Exception:
            continue
    yield "event: done\ndata: {}\n\n"


# Last automation failure (for "retry" — analyze and retry with different strategies)
_last_automation_command: str | None = None
_last_automation_error: str | None = None
_last_automation_params: dict = {}

# Shadow action log (audit trail): one JSONL line per Shadow command run.
_ACTION_LOG_PATH = os.path.join(_DATA_DIR, "action_log.jsonl")
_action_log_lock = threading.Lock()


def _log_shadow_action(cmd: str, params: dict, reply: str) -> None:
    """Append one line to Shadow action log (cmd, params, reply preview). Called only when Shadow runs a command."""
    try:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "cmd": cmd,
            "params": {k: v for k, v in (params or {}).items() if isinstance(v, (str, int, float, bool)) or v is None},
            "reply_preview": (reply or "")[:500],
        }
        with _action_log_lock:
            with open(_ACTION_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

# Pending "record to identity file": scope -> "SOUL"|"TOOLS"|"OBJECTIVES". Next user message is saved to that file (like profile).
_pending_file_update: dict[str, str] = {}

# User style (Luna): per-scope short summary of reply length/tone; injected into prompt. Updated in background.
_USER_STYLE_FILE = os.path.join(_DATA_DIR, "user_style.json")
_user_style_lock = threading.Lock()
_USER_STYLE_UPDATE_EVERY = 5  # update style at most every N user messages (background)

# Goals (Luna): per-scope list of goal strings; injected into prompt. "My goal is X" / "remember my goal: X" adds here.
_GOALS_FILE = os.path.join(_DATA_DIR, "goals.json")
_goals_lock = threading.Lock()

# Phrases that count as "yes, create the file" (for web and Discord)
_CONFIRM_PHRASES = frozenset({
    "yes", "y", "confirm", "confirmed", "ok", "okay", "do it", "go ahead",
    "create it", "yes please", "sure", "please do", "go", "create",
})


def _load_user_style_data() -> dict:
    """Load { scope: { "summary": str } } from disk."""
    with _user_style_lock:
        try:
            if os.path.isfile(_USER_STYLE_FILE):
                with open(_USER_STYLE_FILE, encoding="utf-8") as f:
                    out = json.load(f)
                return out if isinstance(out, dict) else {}
        except Exception:
            pass
    return {}


def _save_user_style(scope: str, summary: str) -> None:
    with _user_style_lock:
        data = _load_user_style_data()
        data[scope] = {"summary": (summary or "").strip()[:500], "updated_at": datetime.now(timezone.utc).isoformat()}
        try:
            os.makedirs(os.path.dirname(_USER_STYLE_FILE), exist_ok=True)
            with open(_USER_STYLE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass


def get_user_style_prompt(scope: str) -> str:
    """Style block for system prompt (reply length/tone)."""
    data = _load_user_style_data()
    rec = data.get(scope) if scope else None
    if not rec or not isinstance(rec, dict) or not (rec.get("summary") or "").strip():
        return ""
    return "User style (adapt when relevant): " + (rec.get("summary") or "").strip()


def _maybe_update_user_style(scope: str) -> None:
    """Background: Ollama summarizes user's style from recent messages; save. Runs in thread."""
    if not scope:
        return
    history = get_recent_conversation(scope, 15)
    user_msgs = [h.get("content", "").strip() for h in history if (h.get("role") or "").lower() == "user" and (h.get("content") or "").strip()]
    if len(user_msgs) < 2:
        return
    text = "\n".join(user_msgs[-8:])
    try:
        out = ollama_chat(
            f"Based on these user messages, summarize in 1-2 sentences: preferred reply length (short/medium/long), tone (casual/formal). Output only the summary.\n\n{text[:2000]}",
            system_prompt="You are a style summarizer. One or two sentences only.",
            memory_scope=None,
            message_history=None,
            model=OLLAMA_MODEL_SMALL,
        )
        if out and len(out.strip()) > 10:
            _save_user_style(scope, out.strip())
    except Exception:
        pass


def _load_goals(scope: str) -> list[str]:
    with _goals_lock:
        try:
            if os.path.isfile(_GOALS_FILE):
                with open(_GOALS_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get(scope), list):
                    return [str(g).strip() for g in data[scope] if str(g).strip()]
        except Exception:
            pass
    return []


def _save_goals(scope: str, goals: list[str]) -> None:
    with _goals_lock:
        try:
            data = {}
            if os.path.isfile(_GOALS_FILE):
                with open(_GOALS_FILE, encoding="utf-8") as f:
                    data = json.load(f)
            if not isinstance(data, dict):
                data = {}
            data[scope] = [g.strip() for g in goals if g.strip()][:20]
            os.makedirs(os.path.dirname(_GOALS_FILE), exist_ok=True)
            with open(_GOALS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass


def add_goal(scope: str, content: str) -> None:
    content = (content or "").strip()[:500]
    if not content or not scope:
        return
    goals = _load_goals(scope)
    if content in goals:
        return
    goals = [content] + [g for g in goals if g != content][:19]
    _save_goals(scope, goals)


def get_goals_prompt(scope: str) -> str:
    goals = _load_goals(scope)
    if not goals:
        return ""
    return "User's current goals (reference when relevant): " + "; ".join(goals[:5])


def _is_confirm_message(msg: str) -> bool:
    """True if the message is a short affirmative (user confirming file creation)."""
    t = (msg or "").strip().lower()
    if not t or len(t) > 50:
        return False
    if t in _CONFIRM_PHRASES:
        return True
    if t.startswith("yes ") or t.startswith("yes,") or t.startswith("yes."):
        return True
    return False


def _normalize_luna_path(path: str) -> str:
    """Strip 'Luna projects/' or similar so path is relative to Luna projects root. Avoids nested folder."""
    if not path or not path.strip():
        return path or ""
    p = path.strip().replace("\\", "/")
    # If Luna put an absolute path, keep only the part under "Luna projects"
    if "/" in p or "\\" in p:
        lower = p.lower()
        marker = "luna projects"
        if marker in lower:
            idx = lower.rfind(marker) + len(marker)
            rest = p[idx:].lstrip("/\\")
            if rest:
                p = rest
    for prefix in ("Luna projects/", "Luna projects\\", "./", ".\\"):
        if p.lower().startswith(prefix.lower()):
            p = p[len(prefix) :].strip()
            break
    return p.strip() or path.strip()


def _parse_luna_writes(reply: str) -> tuple[str, list[dict]]:
    """
    Parse one or more LUNA_WRITE_FILE blocks.
    Returns (cleaned_reply_without_blocks, writes[]), where writes contains
    dict entries: {"path": <relative_path>, "content": <text>}.
    """
    if not reply or "LUNA_WRITE_FILE" not in reply or "END_LUNA_WRITE" not in reply:
        return reply, []

    pattern = re.compile(r"LUNA_WRITE_FILE(.*?)END_LUNA_WRITE", re.DOTALL | re.IGNORECASE)
    matches = list(pattern.finditer(reply))
    if not matches:
        return reply, []

    writes: list[dict] = []
    for m in matches:
        block = m.group(0)
        path = ""
        for line in block.split("\n"):
            if line.strip().lower().startswith("path:"):
                path = line.split(":", 1)[1].strip().strip('"\'')
                break
        if not path:
            continue
        if "---" in block and "END_LUNA_WRITE" in block:
            content = block[block.index("---") + 4 : block.upper().rfind("END_LUNA_WRITE")].strip()
        else:
            content = ""
        writes.append({"path": _normalize_luna_path(path), "content": content})

    cleaned = pattern.sub("", reply).strip()
    return cleaned, writes


def _parse_luna_repo_writes(reply: str) -> list[dict]:
    """
    Parse one or more LUNA_WRITE_REPO blocks (path relative to Luna repo root).
    Returns list of {"path": <repo_relative_path>, "content": <text>}.
    """
    if not reply or "LUNA_WRITE_REPO" not in reply or "END_LUNA_WRITE_REPO" not in reply:
        return []
    pattern = re.compile(r"LUNA_WRITE_REPO(.*?)END_LUNA_WRITE_REPO", re.DOTALL | re.IGNORECASE)
    matches = list(pattern.finditer(reply))
    writes: list[dict] = []
    for m in matches:
        block = m.group(0)
        path = ""
        for line in block.split("\n"):
            if line.strip().lower().startswith("path:"):
                path = line.split(":", 1)[1].strip().strip('"\'')
                break
        if not path:
            continue
        path = path.replace("\\", "/").lstrip("/")
        if "---" in block and "END_LUNA_WRITE_REPO" in block:
            content = block[block.index("---") + 4 : block.upper().rfind("END_LUNA_WRITE_REPO")].strip()
        else:
            content = ""
        writes.append({"path": path, "content": content})
    return writes


def _user_wants_file_creation(msg: str) -> bool:
    """True if the user message suggests they want to create/save files (e.g. game, website, save that, note, txt)."""
    if not (msg or msg.strip()):
        return False
    lower = msg.strip().lower()
    triggers = (
        "create a game", "make a game", "website game", "create a website", "make a website",
        "save that", "save it", "save the", "save these", "save to", "save in",
        "write that", "write it", "write the", "write this", "put that in", "put it in",
        "create that", "create those", "create the file", "create these files",
        "luna projects", "project folder", "in the folder", "to the folder",
        "create a file", "make a file", "write a file",
        "note", "txt document", "text file", "save as txt", "write a note", "create a note",
        "create a text file with", "make a text file with",
    )
    return any(t in lower for t in triggers)


def _extract_text_file_description(msg: str) -> str | None:
    """If msg is 'create a text file with (description)', return the description part; else None."""
    if not (msg or msg.strip()):
        return None
    lower = msg.strip().lower()
    for prefix in ("create a text file with ", "make a text file with "):
        if lower.startswith(prefix):
            return msg.strip()[len(prefix):].strip() or None
        if prefix in lower:
            idx = lower.find(prefix)
            return msg[idx + len(prefix):].strip() or None
    return None


def _relatable_note_filename(user_msg: str, content_preview: str) -> str:
    """Generate a safe .txt filename from the user message or first line of content, for saving to Luna projects."""
    # Prefer "create a text file with (description)" -> use description for slug
    desc = _extract_text_file_description(user_msg)
    if desc:
        stop = {"a", "the", "to", "in", "as", "me", "my", "for"}
        slug = ""
        for word in re.findall(r"[a-zA-Z0-9]+", desc):
            w = word.lower()
            if w in stop or len(w) < 2:
                continue
            slug = (slug + "_" + w) if slug else w
            if len(slug) >= 28:
                break
        slug = re.sub(r"_+", "_", slug).strip("_") or "note"
        date_part = datetime.now().strftime("%Y%m%d")
        return f"note_{date_part}_{slug}.txt" if slug != "note" else f"note_{date_part}.txt"
    # Fallback: slug from full message or first line of content
    stop = {"luna", "create", "write", "save", "file", "a", "the", "to", "in", "as", "txt", "me", "this", "that", "it"}
    slug = ""
    for word in re.findall(r"[a-zA-Z0-9]+", (user_msg or "").strip()):
        w = word.lower()
        if w in stop or len(w) < 2:
            continue
        slug = (slug + "_" + w) if slug else w
        if len(slug) >= 28:
            break
    if not slug:
        first_line = (content_preview or "").strip().split("\n")[0][:40]
        slug = re.sub(r"[^\w\s-]", "", first_line).strip().replace(" ", "_")[:28] or "note"
    slug = re.sub(r"_+", "_", slug).strip("_") or "note"
    date_part = datetime.now().strftime("%Y%m%d")
    return f"note_{date_part}_{slug}.txt" if slug != "note" else f"note_{date_part}.txt"


def _extract_code_blocks_from_reply(reply: str) -> list[dict]:
    """
    Parse markdown code blocks from Luna's reply into writes for Luna projects.
    Returns list of {"path": "<relative path>", "content": "..."} with default filenames by language.
    """
    if not reply or "```" not in reply:
        return []
    # Match ```lang\n...\n``` or ```\n...\n```
    pattern = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
    matches = list(pattern.finditer(reply))
    if not matches:
        return []
    counts: dict[str, int] = {}
    default_names: dict[str, tuple[str, str]] = {
        "html": ("index", ".html"),
        "css": ("styles", ".css"),
        "javascript": ("game", ".js"),
        "js": ("game", ".js"),
        "json": ("data", ".json"),
        "text": ("note", ".txt"),
        "": ("code", ".txt"),
    }
    writes: list[dict] = []
    for m in matches:
        lang = (m.group(1) or "").strip().lower()
        content = (m.group(2) or "").strip()
        if not content or len(content) > 500_000:
            continue
        base, ext = default_names.get(lang) or ("code", ".txt")
        if lang == "html":
            counts["html"] = counts.get("html", 0) + 1
            n = counts["html"]
            path = "index.html" if n == 1 else f"index{n}.html"
        elif lang == "css":
            n = counts.get("css", 0) + 1
            counts["css"] = n
            path = "css/styles.css" if n == 1 else f"css/styles{n}.css"
        elif lang in ("javascript", "js"):
            n = counts.get("js", 0) + 1
            counts["js"] = n
            path = "js/game.js" if n == 1 else f"js/game{n}.js"
        else:
            key = f"{base}{ext}"
            n = counts.get(key, 0) + 1
            counts[key] = n
            path = f"{base}{ext}" if n == 1 else f"{base}{n}{ext}"
        writes.append({"path": _normalize_luna_path(path), "content": content})
    return writes


# Intent template for "commands" / "help" — reply without calling Ollama
LUNA_COMMANDS_REPLY = (
    "**Luna** = chat with me (Llama). **Shadow** = commands & code (Qwen). Chat normally with Luna; for actions say **Shadow, [what to do]** (e.g. Shadow, news; Shadow, create a script that…). If something failed, say **retry**. **!help** for the full list.\n\n"
    "• !news — latest world news headlines\n"
    "• !search <query> / \"search for …\" / \"google …\" — open Google and search\n"
    "• !suno <description> — open Suno and create a song\n"
    "• !share_song (or !share song) — share a random YouTube channel song to X\n"
    "• !share_facebook (or !share facebook) — share to Facebook\n"
    "• !yt_comment <youtube_url> — transcribe and post a YouTube comment\n"
    "• !ig_dm <username|thread_url> [message] — send an Instagram DM\n"
    "• !fb_msg <username or name> [message] — send a Facebook Messenger message\n"
    "• !remember / !always_remember — store memories\n"
    "• !profile — view or set your profile\n"
    "• !join / !leave — voice; !play or say **play** <artist/song> (e.g. play AC/DC); if multiple: **Which song?** — reply with number or name; !pause / !skip / !stop / !queue — music\n"
    "• !call <contact> — WhatsApp Desktop: open app, find Call then Voice to start voice call (Windows)\n"
    "• !msg <contact> [description] — WhatsApp Web: open chat and send message (default or your text, from Luna)\n"
    "• **create code** — say e.g. \"Shadow, create a script that fetches the weather\" → Qwen generates Python, saved to Luna projects/agents/*.py and opened in Notepad\n"
    "• **remind me at 7pm to …** — I'll DM you on Discord at that time with a text + voice note (e.g. remind me at 7pm to take my medicine)\n"
    "• **retry** (or \"try again\") — if the last action failed, I'll analyze and retry with different strategies (up to 2 attempts)\n"
)

# Natural language command parsing: system prompt for Ollama to map user message → command + params
_NL_COMMAND_SYSTEM = """You are a strict command classifier for Luna (an AI assistant). The user will say something in natural language. Decide if they are asking Luna to perform ONE of these actions. If yes, reply with ONLY a single JSON object, no other text. If no, reply with exactly: {"command": null}

Commands and their JSON shape (use these keys only):
- Send a WhatsApp message to someone: {"command": "msg", "contact": "<name or number>", "description": "<optional context for message content>"}
- Open WhatsApp chat / call someone: {"command": "call", "contact": "<name or number>"}
- Get latest news / headlines: {"command": "news"}
- Play music on Discord (e.g. play X, play a song): {"command": "play", "query": "<song name or empty>"}
- Create a song on Suno: {"command": "suno", "description": "<song idea>"}
- Share a song to X/Twitter: {"command": "share_x"}
- Share a song to Facebook: {"command": "share_facebook"}
- Comment on a YouTube video: {"command": "yt_comment", "video_url": "<url>"}
- Send an Instagram DM: {"command": "ig_dm", "target": "<username or thread url>", "message": "<optional text>"}
- Send a Facebook Messenger message: {"command": "fb_msg", "target": "<name or username>", "message": "<optional text>"}
- Open Google and search for something (e.g. search for X, google X, look up X): {"command": "search", "query": "<search terms>"}
- Show commands / help / what can you do: {"command": "files"}
- Join voice channel: {"command": "join"}
- Leave voice: {"command": "leave"}
- Pause / resume / skip / stop music, show queue: {"command": "pause"}, {"command": "resume"}, {"command": "skip"}, {"command": "stop"}, {"command": "queue"}

Rules: Output ONLY valid JSON. Omit optional params if not mentioned. For "msg", description is optional (context for the message). For "play", query can be empty string if they just say "play" or "resume". If the user is just chatting, asking a question, or not clearly requesting one action above, reply {"command": null}."""

# Only run NL parse (Commander) when message *starts with* a command verb — avoids extra Ollama call for plain chat.
_NL_COMMAND_STARTS = (
    "play ", "search ", "send ", "create ", "open ", "call ", "msg ", "message ",
    "remind ", "share ", "post ", "run ", "google ", "news ", "suno ", "comment ",
    "whatsapp ", "facebook ", "instagram ", "youtube ", "add ",
    "make ", "change ", "update ", "implement ", "build ", "fix ", "improve ",
    "i want ", "i'd like ", "can you ", "could you ", "do ", "research ", "why ", "explain ",
    "learn ", "learn to ",
)
_NL_COMMAND_START_EXACT = ("play", "search", "send", "create", "open", "call", "msg", "run", "google", "news")


def _message_likely_command(msg: str) -> bool:
    """Boss routing: True only when message clearly starts with a command. Plain chat skips Commander = faster reply."""
    msg = (msg or "").strip()
    if not msg or len(msg) > 500:
        return False
    low = msg.lower()
    # Starts with a command verb (e.g. "play something", "search for X") — not "I want to play" or "your message"
    for start in _NL_COMMAND_STARTS:
        if low.startswith(start):
            return True
    for exact in _NL_COMMAND_START_EXACT:
        if low == exact or (low.startswith(exact + " ") and len(low) > len(exact) + 1):
            return True
    return False


def employee_commander(msg: str) -> tuple[str, dict] | None:
    """Employee: Commander. Parses natural language into (command_key, params). Uses OLLAMA_MODEL_SMALL. Used by Boss when message looks like a command."""
    return _parse_natural_language_command(msg)


def _shadow_parse_with_fallback(msg: str) -> tuple[str, dict] | None:
    """Try Suno/create-a-song heuristics first so 'Shadow, suno create...', 'Shadow, create a song about...' and 'Shadow, Suno, ...' work; else Commander."""
    m = (msg or "").strip()
    if not m:
        return None
    low = m.lower()
    # "suno create ..." or "suno create a song about ..." -> description is everything after "suno create"
    if low.startswith("suno create"):
        desc = m[11:].lstrip(" ,\t")
        if desc:
            return ("suno", {"description": desc})
    # "Suno, a woman that is flying..." or "suno a woman..."
    if low.startswith("suno"):
        desc = m[4:].lstrip(" ,\t")
        if desc:
            return ("suno", {"description": desc})
    # "create a song about a woman that is flying..."
    if low.startswith("create a song about"):
        desc = m[18:].strip()
        if desc:
            return ("suno", {"description": desc})
    # "create a song ..." or "create a song on suno ..."
    if low.startswith("create a song"):
        desc = m[14:].strip()
        if desc:
            return ("suno", {"description": desc})
    return employee_commander(msg)


def employee_scribe(messages: list[dict]) -> str:
    """Employee: Scribe. Summarizes conversation into 2–4 sentences. Uses OLLAMA_MODEL_SMALL. Used by Boss when compacting history."""
    return _summarize_conversation(messages)


def employee_search_picker(query: str, results: list[dict]) -> str:
    """Employee: SearchPicker. Picks best result from search + short reason. Uses OLLAMA_MODEL_SMALL. Used by Boss in _open_google_search."""
    return _recommend_best_search_result(query, results)


def employee_copywriter_whatsapp(description: str) -> str:
    """Employee: Copywriter. Short WhatsApp-style message from context. Used by Boss when user provides description for !msg."""
    return _generate_whatsapp_message_from_context(description)


def employee_receptionist(msg: str) -> str | None:
    """Employee: Receptionist. Returns commands/help template if user asks for help. No Ollama. Used by Boss via _get_command_intent_reply."""
    return _get_command_intent_reply(msg)


def employee_newsroom(limit: int = 8) -> tuple[bool, str]:
    """Employee: Newsroom. Fetches and formats world news. No Ollama. Used by Boss when user says news."""
    return _fetch_world_news(limit)


# Command-only mode: no Ollama/chatbot; Luna responds only to commands. This reply is used when user sends plain chat.
COMMAND_ONLY_REPLY = (
    "I'm **Luna** (chat) and **Shadow** (commands). Chat with me normally, or say **Shadow, <command>** (e.g. Shadow, news; Shadow, create a script that…). Use **!help** for the full list. If you were just chatting, my chat model may be unavailable — try `ollama run llama3.2:latest`."
)


def _parse_natural_language_command_heuristic(msg: str) -> tuple[str, dict] | None:
    """Parse natural-language command using regex/keywords only (no Ollama). Returns (cmd, params) or None."""
    raw = (msg or "").strip()
    if not raw or raw.startswith("!"):
        return None
    low = raw.lower()

    # news
    if re.search(r"\b(?:news|headlines|latest\s+news|world\s+news|today'?s?\s+news)\b", low):
        return ("news", {})

    # share on X / Twitter
    if re.search(r"\b(?:share\s+(?:a\s+)?song\s+on\s+(?:x|twitter)|share\s+on\s+x|share\s+on\s+twitter|post\s+to\s+x|tweet\s+my\s+song)\b", low):
        return ("share_x", {})
    # share on Facebook
    if re.search(r"\b(?:share\s+(?:a\s+)?song\s+on\s+facebook|share\s+on\s+facebook|post\s+to\s+facebook)\b", low):
        return ("share_facebook", {})

    # YouTube comment: need URL in message
    yt_url = _extract_youtube_video_url(raw)
    if yt_url and re.search(r"\b(?:comment|reply)\b", low) and re.search(r"\b(?:youtube|yt|video)\b", low):
        return ("yt_comment", {"video_url": yt_url})
    if low.startswith(("comment on", "comment on this", "reply to this video")) and yt_url:
        return ("yt_comment", {"video_url": yt_url})

    # search / google
    for prefix in ("search for ", "search ", "google ", "look up ", "find "):
        if low.startswith(prefix):
            q = raw[len(prefix):].strip()
            if q:
                return ("search", {"query": q})
    if low.startswith("search") and len(raw) > 6:
        return ("search", {"query": raw[6:].strip()})

    # message/call (WhatsApp) — "message X" or "call X"
    if re.match(r"^(?:send\s+)?(?:a\s+)?message\s+to\s+(.+)$", low):
        contact = re.match(r"^(?:send\s+)?(?:a\s+)?message\s+to\s+(.+)$", low, re.IGNORECASE)
        if contact:
            rest = contact.group(1).strip()
            desc = ""
            if " saying " in rest or " with " in rest:
                parts = re.split(r"\s+(?:saying|with)\s+", rest, 1, flags=re.IGNORECASE)
                rest = (parts[0] or "").strip()
                desc = (parts[1] or "").strip().strip('"') if len(parts) > 1 else ""
            if rest:
                return ("msg", {"contact": rest, "description": desc or ""})
    if re.match(r"^call\s+(.+)$", low):
        contact = re.match(r"^call\s+(.+)$", low, re.IGNORECASE)
        if contact and contact.group(1).strip():
            return ("call", {"contact": contact.group(1).strip()})

    # Instagram DM
    ig_target, ig_msg = _extract_instagram_dm_request(raw)
    if ig_target:
        return ("ig_dm", {"target": ig_target, "message": ig_msg or ""})

    # Facebook Messenger
    if re.search(r"\b(?:message|dm|send)\s+.*\b(?:messenger|facebook\s+message)\b", low) or re.search(r"\bmessenger\s+(?:message\s+)?(?:to\s+)?@?([a-z0-9._]+)", low):
        m = re.search(r"(?:message|dm|send(?:\s+a)?\s+message(?:\s+to)?)\s+@?([a-z0-9._]{2,30})", low)
        if m:
            target = (m.group(1) or "").strip()
            if target:
                return ("fb_msg", {"target": target, "message": ""})
        m = re.search(r"messenger\s+(?:message\s+)?(?:to\s+)?@?([a-z0-9._]{2,30})", low)
        if m:
            return ("fb_msg", {"target": (m.group(1) or "").strip(), "message": ""})

    # create code / write script → Luna projects/agents/*.py (Qwen 2.5 Coder)
    if re.search(r"\b(?:create|write|generate|make)\s+(?:a\s+)?(?:python\s+)?(?:script|code|\.py\s+file)\b", low):
        return ("create_code", {"request": raw})
    if re.search(r"\b(?:create|write|generate)\s+(?:some\s+)?code\s+(?:that|to)\b", low):
        return ("create_code", {"request": raw})
    if re.search(r"\bcode\s+(?:that|to)\s+", low) and re.search(r"\b(?:that|to)\s+(?:fetches|does|lists|sends|reads|writes)\b", low):
        return ("create_code", {"request": raw})

    # remind me at <time> to <message> → Discord DM + TTS at that time
    m_remind = re.search(r"\bremind\s+me\s+at\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s+to\s+(.+)", low, re.IGNORECASE | re.DOTALL)
    if m_remind:
        raw_time = (m_remind.group(1) or "").strip().replace(" ", "")
        msg_part = (m_remind.group(2) or "").strip()[:500]
        if msg_part and _parse_reminder_time(raw_time):
            return ("remind", {"time": raw_time, "message": msg_part})

    # help / commands
    if re.search(r"\b(?:help|commands|what\s+can\s+you\s+do|list\s+commands)\b", low):
        return ("files", {})

    # play (Discord music)
    if low.startswith("play ") or low == "play":
        return ("play", {"query": raw[5:].strip() if low.startswith("play ") else ""})

    return None


def _parse_natural_language_command(msg: str) -> tuple[str, dict] | None:
    """If the user message is a natural-language request for a Luna command, return (command_key, params). Else return None. Uses heuristic parser only (no Ollama)."""
    return _parse_natural_language_command_heuristic(msg)


def _run_parsed_command(cmd: str, params: dict, scope: str | None = None) -> str:
    """Execute a parsed command (from NL or internal) and return the reply text."""
    cmd = (cmd or "").strip().lower()
    p = params or {}
    if cmd == "msg":
        contact = (p.get("contact") or "").strip()
        desc = (p.get("description") or "").strip() or None
        if not contact:
            return "Who should I message? Say the contact name (e.g. message Marios)."
        ok, result = _run_whatsapp_web_msg(contact, desc)
        if not ok:
            _record_automation_failure(cmd, result, p)
            return f"❌ {result}"
        return f"✅ {result}"
    if cmd == "call":
        contact = (p.get("contact") or "").strip()
        if not contact:
            return "Who should I call? Say the contact name."
        ok, result = _run_whatsapp_web_call(contact)
        if not ok:
            _record_automation_failure(cmd, result, p)
            return f"❌ {result}"
        return f"✅ {result}"
    if cmd == "news":
        ok, result = employee_newsroom()
        if not ok:
            _record_automation_failure(cmd, result, p)
            return f"❌ {result}"
        return result
    if cmd == "search":
        query = (p.get("query") or "").strip()
        if not query:
            return "What should I search for? (e.g. search for best pizza near me, or google how to fix a bike)"
        ok, result = _open_google_search(query)
        if not ok:
            return f"❌ {result}"
        return f"✅ {result}"
    if cmd == "play":
        return "Use !play <song or url> in Discord when I'm in a voice channel to play music."
    if cmd == "suno":
        desc = (p.get("description") or "").strip()
        if not desc:
            return "What kind of song? Give a short description."
        ok, result = _run_suno_create(desc)
        if not ok:
            _record_automation_failure(cmd, result, p)
            return f"❌ {result}"
        return f"✅ {result}"
    if cmd == "share_x":
        ok, result = _run_x_share_random_song()
        if not ok:
            _record_automation_failure(cmd, result, p)
            return f"❌ {result}"
        return f"✅ {result}"
    if cmd == "share_facebook":
        ok, result = _run_facebook_share_random_song()
        if not ok:
            _record_automation_failure(cmd, result, p)
            return f"❌ {result}"
        return f"✅ {result}"
    if cmd == "yt_comment":
        url = (p.get("video_url") or "").strip()
        if not url:
            return "Which YouTube video? Paste the link."
        url = _extract_youtube_video_url(url) or url
        ok, result = _run_youtube_comment(url)
        if not ok:
            _record_automation_failure(cmd, result, p)
            return f"❌ {result}"
        return f"✅ {result}"
    if cmd == "ig_dm":
        target = (p.get("target") or "").strip()
        msg_text = (p.get("message") or "").strip() or ""
        if not target:
            return "Who should I message on Instagram? Give a username or thread link."
        ok, result = _run_instagram_dm(target, msg_text)
        if not ok:
            _record_automation_failure(cmd, result, p)
            return f"❌ {result}"
        return f"✅ {result}"
    if cmd == "fb_msg":
        target = (p.get("target") or "").strip()
        msg_text = (p.get("message") or "").strip() or ""
        if not target:
            return "Who should I message on Messenger? Give a name or username."
        ok, result = _run_messenger_msg(target, msg_text)
        if not ok:
            _record_automation_failure(cmd, result, p)
            return f"❌ {result}"
        return f"✅ {result}"
    if cmd == "create_code":
        req = (p.get("request") or "").strip()
        req = re.sub(r"^(?:shadow[,:\s]+|luna[,:\s]+)", "", req, flags=re.IGNORECASE).strip() or req
        if not req:
            return "What should the script do? (e.g. Shadow, create a script that fetches the weather)"
        ok, result = _run_create_agent_code(req, open_in_notepad=True)
        if not ok:
            _record_automation_failure(cmd, result, p)
            return f"❌ {result}"
        return f"✅ {result}"
    if cmd == "remind":
        time_raw = (p.get("time") or "").strip().replace(" ", "")
        msg_part = (p.get("message") or "").strip()[:500]
        if not msg_part:
            return "What should I remind you to do? (e.g. remind me at 7pm to take my medicine)"
        time_str = _parse_reminder_time(time_raw)
        if not time_str:
            return "I didn't get a valid time. Say something like 7pm, 7:00 pm, or 19:00."
        if not LINKED_DISCORD_USER_ID:
            return "Reminders need a linked Discord user (set LINKED_DISCORD_USER_ID in .env). I'll DM you at that time."
        add_reminder(time_str, msg_part, LINKED_DISCORD_USER_ID, recurring=None)
        return f"✅ I'll remind you at **{time_str}** to {msg_part}. You'll get a Discord DM with a voice note."
    if cmd == "files":
        return "Type **!help** to see my full command list."
    if cmd == "join":
        return "Use !join in a Discord server where I'm in the voice channel list."
    if cmd in ("leave", "pause", "resume", "skip", "stop", "queue"):
        return f"Use !{cmd} in Discord when I'm in a voice channel."
    return ""


def _record_automation_failure(command_id: str, error_message: str, params: dict | None = None) -> None:
    """Store last failed automation so the user can say 'retry' to analyze and retry with different strategies."""
    global _last_automation_command, _last_automation_error, _last_automation_params
    _last_automation_command = (command_id or "").strip() or None
    _last_automation_error = (error_message or "").strip() or None
    _last_automation_params = dict(params) if params else {}


# Learned automation solutions: remember what fixed a failure so we prefer it next time (all commands).
_AUTOMATION_SOLUTIONS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "automation_solutions.json"
)
_automation_solutions_lock = threading.Lock()


def _load_automation_solutions() -> dict:
    """Load { command_id: [ {"error_contains": str, "hint": str}, ... ] } from disk."""
    with _automation_solutions_lock:
        try:
            if os.path.isfile(_AUTOMATION_SOLUTIONS_PATH):
                with open(_AUTOMATION_SOLUTIONS_PATH, encoding="utf-8") as f:
                    out = json.load(f)
                return out if isinstance(out, dict) else {}
        except Exception:
            pass
    return {}


def _save_automation_solution(command_id: str, error_message: str, hint: str = "extra_wait_and_alternate_selectors") -> None:
    """Remember that this fix worked for this command (so we prefer it next time)."""
    command_id = (command_id or "").strip()
    if not command_id:
        return
    error_snippet = (error_message or "").strip()[:120]
    with _automation_solutions_lock:
        data = _load_automation_solutions()
        entries = data.setdefault(command_id, [])
        entries.append({"error_contains": error_snippet, "hint": hint})
        if len(entries) > 20:
            data[command_id] = entries[-15:]
        try:
            os.makedirs(os.path.dirname(_AUTOMATION_SOLUTIONS_PATH), exist_ok=True)
            with open(_AUTOMATION_SOLUTIONS_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=0)
        except Exception:
            pass


def _get_learned_solutions(command_id: str) -> list:
    """Return list of learned solution hints for this command (so we can prefer longer waits / alternate selectors)."""
    data = _load_automation_solutions()
    entries = data.get((command_id or "").strip(), [])
    return [e.get("hint", "extra_wait_and_alternate_selectors") for e in entries if isinstance(e, dict)]


def _should_prefer_longer_waits(command_id: str) -> bool:
    """True if we have any learned solution for this command (use longer waits / alternate strategies first)."""
    return len(_get_learned_solutions(command_id)) > 0


def _is_retry_solution_request(msg: str) -> bool:
    """True if the user wants Luna to analyze the situation and retry with different strategies."""
    if not (msg or msg.strip()):
        return False
    low = msg.strip().lower()
    phrases = (
        "retry",
        "retry and find the solution",
        "retry with a solution",
        "find the solution and retry",
        "try again and fix it",
        "retry and fix it",
        "find the solution",
        "retry and fix",
        "retry that",
        "try again",
    )
    return any(p in low for p in phrases)


def _handle_retry_solution() -> str:
    """Analyze the situation and retry the last failed command with different strategies (longer waits, alternate selectors). Tries up to two attempts so Luna can find what works."""
    if not _last_automation_command or not _last_automation_error:
        return "I don't have a recent failure to retry. Run an action first; if it fails, say **retry** and I'll try again with different strategies."
    cmd = _last_automation_command
    params = _last_automation_params
    # First attempt: re-run the command (automation code uses multiple strategies in one run)
    retry_reply = _run_parsed_command(cmd, params)
    if (retry_reply or "").strip().startswith("✅"):
        _save_automation_solution(cmd, _last_automation_error)
        return f"**Retry (different strategies):**\n\n{retry_reply}"
    # Second attempt: save that we're retrying so next run prefers longer waits / alternate paths, then try once more
    _save_automation_solution(cmd, _last_automation_error)
    retry_reply2 = _run_parsed_command(cmd, params)
    if (retry_reply2 or "").strip().startswith("✅"):
        return f"**First attempt didn't complete; second attempt (longer waits / alternate strategy):**\n\n{retry_reply2}"
    return f"**Analyzed and retried twice** with different strategies; still didn't complete:\n\n{retry_reply2}"


def _is_nl_command_allowed_on_discord(cmd: str, author_id: int) -> bool:
    """True if this Discord user can run this command (admin/linked where required)."""
    if cmd in ("msg", "call"):
        return _can_use_whatsapp_discord(author_id)
    if cmd in ("suno", "create_code"):
        return _can_use_suno_discord(author_id)
    if cmd == "remind":
        return _linked_discord_id_int is not None and author_id == _linked_discord_id_int
    if cmd in ("share_x", "share_facebook"):
        return _can_use_x_share_discord(author_id)
    if cmd == "yt_comment":
        return _can_use_youtube_comment_discord(author_id)
    if cmd == "ig_dm":
        return _can_use_instagram_dm_discord(author_id)
    if cmd == "fb_msg":
        return _can_use_messenger_discord(author_id)
    return True


def _get_command_intent_reply(msg: str) -> str | None:
    """Receptionist: command list only via explicit !help / !files / !commands (handled elsewhere). Never from natural language."""
    return None


def _handle_web_file_command(msg: str) -> str | None:
    """If msg is a local command, run it and return reply. Else return None."""
    msg = (msg or "").strip()
    if not msg.startswith("!"):
        return None
    parts = msg.split(None, 1)
    cmd = (parts[0] or "").lower()
    args = (parts[1] or "").strip()
    if cmd in ("!help", "!files", "!commands"):
        return LUNA_COMMANDS_REPLY
    if cmd == "!news":
        ok, result = employee_newsroom()
        return result if ok else f"❌ {result}"
    if cmd == "!search":
        if not args:
            return "Usage: !search <query> (e.g. !search best pizza near me)"
        ok, result = _open_google_search(args)
        return f"✅ {result}" if ok else f"❌ {result}"
    if cmd == "!suno":
        if not args:
            return "Usage: !suno <song description>"
        ok, result = _run_suno_create(args)
        return f"✅ {result}" if ok else f"❌ {result}"
    if cmd in ("!share_song", "!share-song", "!xshare"):
        ok, result = _run_x_share_random_song()
        if not ok:
            _record_automation_failure("share_x", result, {})
            return f"❌ {result}"
        return f"✅ {result}"
    if cmd in ("!share_facebook", "!share-facebook", "!fbshare"):
        ok, result = _run_facebook_share_random_song()
        if not ok:
            _record_automation_failure("share_facebook", result, {})
            return f"❌ {result}"
        return f"✅ {result}"
    if cmd == "!share" and args.lower().startswith("song"):
        ok, result = _run_x_share_random_song()
        if not ok:
            _record_automation_failure("share_x", result, {})
            return f"❌ {result}"
        return f"✅ {result}"
    if cmd == "!share" and args.lower().startswith("facebook"):
        ok, result = _run_facebook_share_random_song()
        if not ok:
            _record_automation_failure("share_facebook", result, {})
            return f"❌ {result}"
        return f"✅ {result}"
    if cmd in ("!yt_comment", "!youtube_comment", "!comment_youtube"):
        if not args:
            return "Usage: !yt_comment <youtube_video_url>"
        video_url = _extract_youtube_video_url(args) or args.strip()
        ok, result = _run_youtube_comment(video_url)
        if not ok:
            _record_automation_failure("yt_comment", result, {"video_url": video_url})
            return f"❌ {result}"
        return f"✅ {result}"
    if cmd in ("!ig_dm", "!instagram_dm", "!igdm"):
        if not args:
            return "Usage: !ig_dm <username|instagram_direct_thread_url> [message]"
        parts = args.split(None, 1)
        target = (parts[0] or "").strip()
        custom_message = (parts[1] or "").strip() if len(parts) > 1 else ""
        ok, result = _run_instagram_dm(target, custom_message)
        if not ok:
            _record_automation_failure("ig_dm", result, {"target": target, "message": custom_message})
            return f"❌ {result}"
        return f"✅ {result}"
    if cmd in ("!fb_msg", "!messenger", "!fbmsg"):
        if not args:
            return "Usage: !fb_msg <username or name> [message] (e.g. !fb_msg John or !fb_msg John have a great day)"
        parts = args.split(None, 1)
        target = (parts[0] or "").strip()
        custom_message = (parts[1] or "").strip() if len(parts) > 1 else ""
        ok, result = _run_messenger_msg(target, custom_message)
        if not ok:
            _record_automation_failure("fb_msg", result, {"target": target, "message": custom_message})
            return f"❌ {result}"
        return f"✅ {result}"
    if cmd == "!call":
        if not args:
            return "Usage: !call <contact name or number> (e.g. !call Marios or !call +357 96 724268)"
        ok, result = _run_whatsapp_web_call(args)
        if not ok:
            _record_automation_failure("call", result, {"contact": args})
            return f"❌ {result}"
        return f"✅ {result}"
    if cmd == "!msg":
        if not args:
            return "Usage: !msg <contact> [description] (e.g. !msg Marios or !msg Marios goodnight)"
        contact_name, description = _parse_whatsapp_msg_args(args)
        if not contact_name:
            return "Usage: !msg <contact> [description]"
        ok, result = _run_whatsapp_web_msg(contact_name, description)
        if not ok:
            _record_automation_failure("msg", result, {"contact": contact_name, "description": description or ""})
            return f"❌ {result}"
        return f"✅ {result}"
    return None


def _path_for_identity_file(key: str) -> str | None:
    """Return the data/ path for SOUL, TOOLS, or OBJECTIVES."""
    if key == "SOUL":
        return _SOUL_PATH
    if key == "TOOLS":
        return _TOOLS_PATH
    if key == "OBJECTIVES":
        return _OBJECTIVES_PATH
    return None


def _save_identity_file_and_clear_pending(scope: str) -> tuple[str, str] | None:
    """If scope has a pending file update, return (file_key, path); caller writes content and then clears."""
    key = _pending_file_update.get(scope)
    if not key:
        return None
    path = _path_for_identity_file(key)
    if not path:
        _pending_file_update.pop(scope, None)
        return None
    return key, path


@web_app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json(force=True, silent=True) or {}
    msg = (data.get("message") or "").strip()
    stream_response = data.get("stream") is True
    if not msg:
        return jsonify({"error": "Missing message"}), 400
    # Web UI uses linked scope (Chris/Solonaras) so same memory, profile, conversation as Discord
    scope = LINKED_SCOPE if LINKED_SCOPE else "web"
    # Pending "record to SOUL/TOOLS/OBJECTIVES": user's message is the content to save (like profile)
    pending = _save_identity_file_and_clear_pending(scope)
    if pending:
        key, path = pending
        _pending_file_update.pop(scope, None)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(msg)
            _invalidate_identity_cache()
            name = {"SOUL": "SOUL.md", "TOOLS": "TOOLS.md", "OBJECTIVES": "OBJECTIVES.md"}[key]
            reply = f"Saved to **{name}**. I'll use that from now on."
        except Exception as e:
            reply = f"Couldn't save to {key}: {e}"
        append_exchange(scope, msg, reply)
        _play_reply_tts_on_pc(reply)
        return jsonify({"reply": reply})
    # Confirmation: user said yes -> run pending script, execute pending "do" action, or pending write
    # Shadow: "Shadow, do X" -> run command only, no Luna LLM (lighter + faster)
    rest = strip_shadow_prefix(msg)
    if rest is not None:
        reply = shadow_run(rest, scope, _shadow_parse_with_fallback, _run_parsed_command, log_fn=_log_shadow_action)
        append_exchange(scope, msg, reply)
        _play_reply_tts_on_pc(reply)
        return jsonify({"reply": reply})
    # Explicit ! commands in browser always execute directly.
    file_reply = _handle_web_file_command(msg)
    if file_reply is not None:
        append_exchange(scope, msg, file_reply)
        return jsonify({"reply": file_reply})
    if _is_retry_solution_request(msg):
        reply = _handle_retry_solution()
        append_exchange(scope, msg, reply)
        _play_reply_tts_on_pc(reply)
        return jsonify({"reply": reply})
    # Boss: if message looks like a command, Commander parses it and Shadow executes (all commands done by Shadow).
    if not msg.strip().startswith("!") and _message_likely_command(msg):
        parsed = employee_commander(msg)
        if parsed:
            cmd_key, cmd_params = parsed
            reply = _run_parsed_command(cmd_key, cmd_params, scope)
            if reply:
                _log_shadow_action(cmd_key, cmd_params, reply)
                append_exchange(scope, msg, reply)
                _play_reply_tts_on_pc(reply)
                return jsonify({"reply": reply})
    if _extract_news_request(msg):
        ok, result = employee_newsroom()
        reply = result if ok else f"❌ {result}"
        append_exchange(scope, msg, reply)
        _play_reply_tts_on_pc(reply)
        return jsonify({"reply": reply})
    tool_intent = _intent_requires_tool_call(msg)
    ig_username, ig_custom_msg = _extract_instagram_dm_request(msg) if tool_intent else ("", "")
    if tool_intent and ig_username:
        announce_target = ig_username if _extract_instagram_thread_url(ig_username) else f"@{ig_username}"
        announce = f"Opening Instagram and sending a message to {announce_target}."
        _play_reply_tts_on_pc(announce)
        ok, result = _run_instagram_dm(ig_username, ig_custom_msg)
        reply = f"✅ {result}" if ok else f"❌ {result}"
        append_exchange(scope, msg, reply)
        _play_reply_tts_on_pc(reply)
        return jsonify({"reply": reply})
    yt_comment_url = _extract_youtube_comment_request(msg) if tool_intent else ""
    if tool_intent and yt_comment_url:
        announce = "Opening YouTube, transcribing the video, and posting a thoughtful comment."
        _play_reply_tts_on_pc(announce)
        ok, result = _run_youtube_comment(yt_comment_url)
        reply = f"✅ {result}" if ok else f"❌ {result}"
        append_exchange(scope, msg, reply)
        _play_reply_tts_on_pc(reply)
        return jsonify({"reply": reply})
    # Suno song creation from web UI (direct command or conversational phrase)
    suno_desc = _extract_suno_description(msg) if tool_intent else ""
    if tool_intent and suno_desc:
        announce = f"Opening Suno now. I will type: {_suno_preview_text(suno_desc)}"
        _play_reply_tts_on_pc(announce)
        ok, result = _run_suno_create(suno_desc)
        reply = f"✅ {result}" if ok else f"❌ {result}"
        append_exchange(scope, msg, reply)
        _play_reply_tts_on_pc(reply)
        return jsonify({"reply": reply})
    # X share flow from web UI (command or conversational request)
    if tool_intent and _extract_share_song_request(msg):
        announce = "Sharing a random song from your YouTube channel to X with an inviting message."
        _play_reply_tts_on_pc(announce)
        ok, result = _run_x_share_random_song()
        reply = f"✅ {result}" if ok else f"❌ {result}"
        append_exchange(scope, msg, reply)
        _play_reply_tts_on_pc(reply)
        return jsonify({"reply": reply})
    # Facebook share flow from web UI
    if tool_intent and _extract_share_facebook_request(msg):
        announce = "Sharing a random song from your YouTube channel to Facebook with an inviting message."
        _play_reply_tts_on_pc(announce)
        ok, result = _run_facebook_share_random_song()
        reply = f"✅ {result}" if ok else f"❌ {result}"
        append_exchange(scope, msg, reply)
        _play_reply_tts_on_pc(reply)
        return jsonify({"reply": reply})
    # Intent template: commands/help → reply without Ollama
    command_reply = employee_receptionist(msg)
    if command_reply is not None:
        append_exchange(scope, msg, command_reply)
        _try_capture_memory(scope, msg)
        _try_capture_profile(scope, msg)
        _play_reply_tts_on_pc(command_reply)
        return jsonify({"reply": command_reply})
    # Luna mode (chat): use OLLAMA_CHAT_MODEL (e.g. Llama 3.2) for normal conversation. Shadow stays Qwen for commands.
    history = get_recent_conversation(scope, 15)
    try:
        if stream_response:
            chunks = []
            for chunk in ollama_chat_stream(
                msg, system_prompt=LUNA_SYSTEM_PROMPT, memory_scope=scope, message_history=history, model=OLLAMA_CHAT_MODEL
            ):
                chunks.append(chunk)
            reply = "".join(chunks).strip() if chunks else ""
        else:
            reply = ollama_chat(
                msg, system_prompt=LUNA_SYSTEM_PROMPT, memory_scope=scope, message_history=history, model=OLLAMA_CHAT_MODEL
            )
        if not reply or reply.startswith("Ollama isn't responding") or reply.startswith("Something went wrong"):
            reply = COMMAND_ONLY_REPLY
    except Exception:
        reply = COMMAND_ONLY_REPLY
    append_exchange(scope, msg, reply)
    _try_capture_memory(scope, msg)
    _try_capture_profile(scope, msg)
    _play_reply_tts_on_pc(reply)
    if stream_response:
        def _stream_gen():
            yield f"data: {json.dumps({'chunk': reply}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'done': True, 'final': reply}, ensure_ascii=False)}\n\n"
        return Response(
            stream_with_context(_stream_gen()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )
    return jsonify({"reply": reply})


@web_app.route("/api/tts", methods=["POST"])
def api_tts():
    """Generate speech with gTTS. Returns MP3."""
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Missing text"}), 400
    try:
        audio_bytes = _generate_tts(text)
        if not audio_bytes:
            return jsonify({"error": "gTTS produced no audio"}), 500
        return Response(audio_bytes, mimetype="audio/mpeg")
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@web_app.route("/api/tts-stream", methods=["POST"])
def api_tts_stream():
    """Stream gTTS as SSE: each event is an MP3 chunk (base64)."""
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Missing text"}), 400
    return Response(
        stream_with_context(_stream_tts_chunks(text)),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            "Transfer-Encoding": "chunked",
        },
    )


@web_app.route("/api/tts-stop", methods=["POST"])
def api_tts_stop():
    """Stop any TTS currently playing on the server (web PC playback)."""
    _stop_tts_on_pc()
    return jsonify({"ok": True})


def _transcribe_audio_with_whisper(audio_path: str) -> str | None:
    """Transcribe audio file (e.g. .webm, .wav) using Whisper. Returns None if Whisper not available or error."""
    try:
        import whisper
        model = getattr(_transcribe_audio_with_whisper, "_whisper_model", None)
        if model is None:
            model = whisper.load_model("base")
            _transcribe_audio_with_whisper._whisper_model = model
        result = model.transcribe(audio_path, fp16=False, language=None)
        return (result.get("text") or "").strip()
    except Exception as e:
        print(f"[Luna] Whisper transcribe error: {e}", flush=True)
        return None


@web_app.route("/api/transcribe", methods=["POST"])
def api_transcribe():
    """Accept recorded audio (e.g. audio/webm), transcribe with Whisper, return { \"text\": \"...\" }. Browser keeps recording until user clicks stop."""
    raw = request.get_data()
    if not raw or len(raw) < 100:
        return jsonify({"error": "No audio data or too short."}), 400
    suffix = ".webm"
    content_type = (request.content_type or "").lower()
    if "wav" in content_type or request.headers.get("X-Audio-Format") == "wav":
        suffix = ".wav"
    fd = path = None
    try:
        fd, path = tempfile.mkstemp(suffix=suffix)
        os.write(fd, raw)
        os.close(fd)
        fd = None
        text = _transcribe_audio_with_whisper(path)
        if text is not None:
            return jsonify({"text": text})
        return (
            jsonify({
                "error": "Server-side transcription not available. Install: pip install openai-whisper and have ffmpeg in PATH."
            }),
            503,
        )
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500
    finally:
        try:
            if fd is not None:
                os.close(fd)
            if path and os.path.isfile(path):
                os.unlink(path)
        except Exception:
            pass


def run_web_ui():
    web_app.run(host="127.0.0.1", port=5050, use_reloader=False, threaded=True)


def open_web_ui_in_browser():
    """Open the Luna web UI in the default browser (Brave if set as default) after a short delay."""
    time.sleep(1.5)
    import webbrowser
    webbrowser.open("http://127.0.0.1:5050")


def is_mentioning_luna(message: discord.Message) -> bool:
    """True if the message mentions this bot."""
    return bot.user and (bot.user.mentioned_in(message) or message.author == bot.user)


async def _ensure_voice_and_speak(guild: discord.Guild, reply: str) -> None:
    """If Luna is in a voice channel on this guild, speak reply via TTS. If channel is in DISCORD_TTS_CHANNEL_IDS and we're not in voice, try to join a voice channel then speak."""
    vc = next((c for c in bot.voice_clients if c.guild == guild and c.is_connected()), None)
    if not vc:
        return
    reply_clean = _reply_text_for_tts(reply)
    if not reply_clean or len(reply_clean) > 500:
        return
    mp3_bytes = await _get_tts_for_discord(reply_clean)
    if not mp3_bytes:
        if reply_clean:
            print("Discord voice TTS: gTTS produced no audio.")
        return
    try:
        source = discord.FFmpegPCMAudio(io.BytesIO(mp3_bytes), pipe=True)
        vc.play(source)
    except Exception as e:
        print(f"Discord voice play: {e}")


async def _auto_join_voice_for_guild(guild: discord.Guild) -> discord.VoiceClient | None:
    """Join a voice channel on this guild. Prefer author's channel, then 'General' (or similar), then first with members. Returns voice client or None."""
    vc = next((c for c in bot.voice_clients if c.guild == guild and c.is_connected()), None)
    if vc:
        return vc
    channel = None
    for ch in guild.voice_channels:
        name_lower = (ch.name or "").lower()
        if "general" in name_lower:
            channel = ch
            break
    if not channel:
        for ch in guild.voice_channels:
            members = [m for m in ch.members if not m.bot]
            if members:
                channel = ch
                break
    if not channel and guild.voice_channels:
        channel = guild.voice_channels[0]
    if not channel:
        return None
    try:
        return await channel.connect()
    except (discord.ClientException, Exception):
        return None


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Ollama: {OLLAMA_BASE} — model: {OLLAMA_MODEL}")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.listening, name=f"{OLLAMA_MODEL}")
    )
    bot.loop.create_task(_reminder_loop())


def _parse_play_choice(user_message: str, results: list[dict]) -> int | None:
    """Parse user reply to 'Which song?' — number 1..n or partial title match. Returns 0-based index or None."""
    msg = (user_message or "").strip().lower()
    if not msg or not results:
        return None
    # Number: "1", "2", "3" or "the first one", "second", "2nd"
    if msg.isdigit():
        i = int(msg)
        if 1 <= i <= len(results):
            return i - 1
        return None
    for word in ("first", "second", "third", "fourth", "fifth"):
        if word in msg:
            idx = ("first", "second", "third", "fourth", "fifth").index(word)
            if idx < len(results):
                return idx
            return None
    # Partial title match
    for i, r in enumerate(results):
        title = (r.get("title") or "").lower()
        if msg in title or title in msg:
            return i
    return None


def _celine_route_decider(text: str) -> str | None:
    """Used by Celine: return 'shadow' if transcript is a command, 'luna' if chat."""
    if not (text or "").strip():
        return None
    if strip_shadow_prefix(text) is not None:
        return "shadow"
    if _message_likely_command(text) and employee_commander(text) is not None:
        return "shadow"
    return "luna"


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # ——— Celine: voice clip / MP3 from phone — transcribe and decide Shadow (command) vs Luna (chat)
    effective_content = (message.content or "").strip()
    celine_route: str | None = None
    voice_text, celine_route = await celine.process_voice_message(
        message,
        transcribe_fn=_transcribe_audio_with_whisper,
        route_decider=_celine_route_decider,
        run_in_thread=asyncio.to_thread,
    )
    if voice_text:
        effective_content = voice_text
        if celine_route:
            print(f"[Celine] Voice → {celine_route}: {effective_content[:80]}…", flush=True)

    # ——— Conversational music: "Which song?" follow-up (user picked from list)
    if message.guild:
        with _pending_play_choice_lock:
            pending = _pending_play_choice.get(message.guild.id)
        if pending and message.channel.id == pending.get("channel_id") and message.author.id == pending.get("author_id"):
            results = pending.get("results") or []
            idx = _parse_play_choice(effective_content, results)
            if idx is not None:
                with _pending_play_choice_lock:
                    _pending_play_choice.pop(message.guild.id, None)
                url = (results[idx].get("url") or "").strip()
                if not url:
                    await message.reply("❌ Could not get that song's URL.")
                    return
                ok, track_result = await asyncio.to_thread(_resolve_play_track, url)
                if not ok:
                    await message.reply(f"❌ {track_result}")
                    return
                track = track_result
                track["request_channel_id"] = message.channel.id
                if not message.author.voice or not message.author.voice.channel:
                    await message.reply("You're not in a voice channel anymore.")
                    return
                target_channel = message.author.voice.channel
                vc = message.guild.voice_client
                try:
                    if vc and vc.is_connected():
                        if vc.channel.id != target_channel.id:
                            await vc.move_to(target_channel)
                    else:
                        vc = await target_channel.connect()
                except Exception as e:
                    await message.reply(f"Couldn't join voice channel: {e}")
                    return
                state = _music_state_for_guild(message.guild.id)
                state["queue"].append(track)
                started = await asyncio.to_thread(_start_next_music_track, message.guild.id, vc)
                title = (track.get("title") or "Unknown").strip()
                if started:
                    await message.reply(f"▶️ Playing: **{title}**")
                else:
                    await message.reply(f"➕ Queued: **{title}**")
                return
            # Not a valid choice — leave pending so they can try again or say "cancel"
            if effective_content.lower() in ("cancel", "nevermind", "never mind"):
                with _pending_play_choice_lock:
                    _pending_play_choice.pop(message.guild.id, None)
                await message.reply("Cancelled. Say `play <something>` to search again.")
                return

    # ——— Conversational music: "play AC/DC" (no !play) — search YouTube, then "Which song?" if multiple
    if message.guild and message.author.voice and message.author.voice.channel:
        raw = effective_content
        low = raw.lower()
        if low.startswith("play ") and len(raw) > 5:
            query = raw[5:].strip()
            if query and not raw.startswith("!"):
                ok, search_result = await asyncio.to_thread(_youtube_search_multiple, query, 5)
                if not ok:
                    await message.reply(f"❌ Search failed: {search_result}")
                    return
                results = search_result if isinstance(search_result, list) else []
                if not results:
                    await message.reply(f"No YouTube results for **{query}**. Try different words or use `!play <url>`.")
                    return
                if len(results) == 1:
                    url = (results[0].get("url") or "").strip()
                    if url:
                        ok, track_result = await asyncio.to_thread(_resolve_play_track, url)
                        if not ok:
                            await message.reply(f"❌ {track_result}")
                            return
                        track = track_result
                        track["request_channel_id"] = message.channel.id
                        target_channel = message.author.voice.channel
                        vc = message.guild.voice_client
                        try:
                            if vc and vc.is_connected():
                                if vc.channel.id != target_channel.id:
                                    await vc.move_to(target_channel)
                            else:
                                vc = await target_channel.connect()
                        except Exception as e:
                            await message.reply(f"Couldn't join voice channel: {e}")
                            return
                        state = _music_state_for_guild(message.guild.id)
                        state["queue"].append(track)
                        started = await asyncio.to_thread(_start_next_music_track, message.guild.id, vc)
                        title = (track.get("title") or "Unknown").strip()
                        if started:
                            await message.reply(f"▶️ Playing: **{title}**")
                        else:
                            await message.reply(f"➕ Queued: **{title}**")
                        return
                # Multiple results: ask which song
                with _pending_play_choice_lock:
                    _pending_play_choice[message.guild.id] = {
                        "results": results,
                        "channel_id": message.channel.id,
                        "author_id": message.author.id,
                    }
                lines = [f"**Which song?** (reply with a number or the song name)"]
                for i, r in enumerate(results[:5], 1):
                    title = (r.get("title") or "Unknown")[:80]
                    lines.append(f"{i}. {title}")
                await message.reply("\n".join(lines))
                return

    # Reply when mentioned or in DM — use Ollama (same memory, profile, conversation as web; unique per Discord user)
    if isinstance(message.channel, discord.DMChannel) or is_mentioning_luna(message):
        text = effective_content
        # Remove bot mention from text for the prompt
        if bot.user and f"<@{bot.user.id}>" in text:
            text = text.replace(f"<@{bot.user.id}>", "").strip()
        mention = f"<@{message.author.id}>"
        if not text:
            await message.reply(f"Hey {mention}! I'm **Luna** — chat with me normally, or say **Shadow, <command>** for actions. **!help** for the list.")
            return
        # Linked user (Chris/Solonaras) shares one scope with web; others get per-server or per-DM scope
        memory_scope = _scope_for_discord_user(message.author.id, message.guild.id if message.guild else None)
        is_discord_admin = message.author.id == _discord_admin_id_int
        text_lower = text.lower().strip()
        # Pending "record to SOUL/TOOLS/OBJECTIVES": user's message is the content to save (like profile)
        pending = _save_identity_file_and_clear_pending(memory_scope)
        if pending:
            key, path = pending
            _pending_file_update.pop(memory_scope, None)
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                _invalidate_identity_cache()
                name = {"SOUL": "SOUL.md", "TOOLS": "TOOLS.md", "OBJECTIVES": "OBJECTIVES.md"}[key]
                reply = f"Saved to **{name}**. I'll use that from now on."
            except Exception as e:
                reply = f"Couldn't save to {key}: {e}"
            await asyncio.to_thread(append_exchange, memory_scope, text, reply)
            await message.reply(f"{mention} {reply}")
            return
        if _is_retry_solution_request(text):
            reply = await asyncio.to_thread(_handle_retry_solution)
            await asyncio.to_thread(append_exchange, memory_scope, text, reply)
            await message.reply(f"{mention} {reply}")
            return
        # Command list only for explicit !help / !files / !commands
        first_word = (text.strip().split() or [""])[0].lower()
        if first_word in ("!help", "!files", "!commands"):
            await asyncio.to_thread(append_exchange, memory_scope, text, LUNA_COMMANDS_REPLY)
            await message.reply(f"{mention} {LUNA_COMMANDS_REPLY}")
            return
        # Shadow: run when typed "Shadow, …" or when Celine (voice) decided it's a command (skip when Celine said luna)
        if celine_route == "luna":
            pass  # Celine said chat → skip Shadow, continue to Luna
        elif celine_route == "shadow":
            # Voice command: run Shadow with full transcript (e.g. "share on X")
            reply = await asyncio.to_thread(
                shadow_run,
                text,
                memory_scope,
                _shadow_parse_with_fallback,
                _run_parsed_command,
                permission_fn=_is_nl_command_allowed_on_discord,
                author_id=message.author.id,
                log_fn=_log_shadow_action,
            )
            await asyncio.to_thread(append_exchange, memory_scope, text, reply)
            await message.reply(f"{mention} {reply}")
            return
        else:
            rest = strip_shadow_prefix(text)
            if rest is not None:
                reply = await asyncio.to_thread(
                    shadow_run,
                    rest,
                    memory_scope,
                    _shadow_parse_with_fallback,
                    _run_parsed_command,
                    permission_fn=_is_nl_command_allowed_on_discord,
                    author_id=message.author.id,
                    log_fn=_log_shadow_action,
                )
                await asyncio.to_thread(append_exchange, memory_scope, text, reply)
                await message.reply(f"{mention} {reply}")
                return
        # Boss: if message looks like a command, Commander parses it and Shadow executes (all commands done by Shadow).
        if not text.strip().startswith("!") and _message_likely_command(text):
            parsed = await asyncio.to_thread(employee_commander, text)
            if parsed:
                cmd_key, cmd_params = parsed
                if _is_nl_command_allowed_on_discord(cmd_key, message.author.id):
                    reply = await asyncio.to_thread(_run_parsed_command, cmd_key, cmd_params, memory_scope)
                    if reply:
                        await asyncio.to_thread(_log_shadow_action, cmd_key, cmd_params, reply)
                        await asyncio.to_thread(append_exchange, memory_scope, text, reply)
                        await message.reply(f"{mention} {reply}")
                        return
                else:
                    reply = "You don't have permission to use that command here."
                    await asyncio.to_thread(append_exchange, memory_scope, text, reply)
                    await message.reply(f"{mention} {reply}")
                    return
        if _extract_news_request(text):
            ok, result = await asyncio.to_thread(employee_newsroom)
            reply = result if ok else f"❌ {result}"
            await asyncio.to_thread(append_exchange, memory_scope, text, reply)
            await message.reply(f"{mention} {reply}")
            return
        if text.strip().lower().startswith("!search"):
            query = text.strip()[7:].strip()
            if not query:
                reply = "Usage: `!search <query>` (e.g. !search best pizza near me)"
            else:
                ok, result = await asyncio.to_thread(_open_google_search, query)
                reply = f"✅ {result}" if ok else f"❌ {result}"
            await asyncio.to_thread(append_exchange, memory_scope, text, reply)
            await message.reply(f"{mention} {reply}")
            return
        tool_intent = await asyncio.to_thread(_intent_requires_tool_call, text)
        ig_username, ig_custom_msg = _extract_instagram_dm_request(text) if tool_intent else ("", "")
        if tool_intent and ig_username:
            if not _can_use_instagram_dm_discord(message.author.id):
                reply = "Only the linked user/admin can use Instagram DM automation on Discord."
            else:
                announce_target = ig_username if _extract_instagram_thread_url(ig_username) else f"@{ig_username}"
                await message.reply(f"{mention} Opening Instagram and messaging {announce_target} now...")
                ok, result = await asyncio.to_thread(_run_instagram_dm, ig_username, ig_custom_msg)
                reply = f"✅ {result}" if ok else f"❌ {result}"
            await asyncio.to_thread(append_exchange, memory_scope, text, reply)
            await message.reply(f"{mention} {reply}")
            return
        yt_comment_url = _extract_youtube_comment_request(text) if tool_intent else ""
        if tool_intent and yt_comment_url:
            if not _can_use_youtube_comment_discord(message.author.id):
                reply = "Only the linked user/admin can use YouTube comment automation on Discord."
            else:
                await message.reply(f"{mention} Transcribing this YouTube video and posting a comment now...")
                ok, result = await asyncio.to_thread(_run_youtube_comment, yt_comment_url)
                reply = f"✅ {result}" if ok else f"❌ {result}"
            await asyncio.to_thread(append_exchange, memory_scope, text, reply)
            await message.reply(f"{mention} {reply}")
            return
        # Suno song creation from Discord (linked/admin only): command or conversational phrase
        suno_desc = _extract_suno_description(text) if tool_intent else ""
        if tool_intent and suno_desc:
            if not _can_use_suno_discord(message.author.id):
                reply = "Only the linked user/admin can use Suno automation on Discord."
            else:
                announce = f"Opening Suno now. I will type: {_suno_preview_text(suno_desc)}"
                await message.reply(f"{mention} {announce}")
                ok, result = await asyncio.to_thread(_run_suno_create, suno_desc)
                reply = f"✅ {result}" if ok else f"❌ {result}"
            await asyncio.to_thread(append_exchange, memory_scope, text, reply)
            await message.reply(f"{mention} {reply}")
            return
        # X share flow from Discord (linked/admin only)
        if tool_intent and _extract_share_song_request(text):
            if not _can_use_x_share_discord(message.author.id):
                reply = "Only the linked user/admin can use X sharing automation on Discord."
            else:
                await message.reply(f"{mention} Sharing a random channel song to X now...")
                ok, result = await asyncio.to_thread(_run_x_share_random_song)
                reply = f"✅ {result}" if ok else f"❌ {result}"
            await asyncio.to_thread(append_exchange, memory_scope, text, reply)
            await message.reply(f"{mention} {reply}")
            return
        # Facebook share flow from Discord (linked/admin only)
        if tool_intent and _extract_share_facebook_request(text):
            if not _can_use_x_share_discord(message.author.id):
                reply = "Only the linked user/admin can use Facebook sharing automation on Discord."
            else:
                await message.reply(f"{mention} Sharing a random channel song to Facebook now...")
                ok, result = await asyncio.to_thread(_run_facebook_share_random_song)
                reply = f"✅ {result}" if ok else f"❌ {result}"
            await asyncio.to_thread(append_exchange, memory_scope, text, reply)
            await message.reply(f"{mention} {reply}")
            return
        # Intent template: commands/help → reply without Ollama
        command_reply = employee_receptionist(text)
        if command_reply is not None:
            await asyncio.to_thread(append_exchange, memory_scope, text, command_reply)
            await message.reply(f"{mention} {command_reply}")
            return
        # Luna mode (chat): use OLLAMA_CHAT_MODEL (e.g. Llama 3.2) for normal conversation.
        history = await asyncio.to_thread(get_recent_conversation, memory_scope, 15)
        try:
            reply = await asyncio.to_thread(
                ollama_chat,
                text,
                system_prompt=LUNA_SYSTEM_PROMPT,
                memory_scope=memory_scope,
                message_history=history,
                model=OLLAMA_CHAT_MODEL,
            )
            if not reply or reply.startswith("Ollama isn't responding") or reply.startswith("Something went wrong"):
                reply = COMMAND_ONLY_REPLY
        except Exception:
            reply = COMMAND_ONLY_REPLY
        await asyncio.to_thread(append_exchange, memory_scope, text, reply)
        await asyncio.to_thread(_try_capture_memory, memory_scope, text)
        await asyncio.to_thread(_try_capture_profile, memory_scope, text)
        await message.reply(f"{mention} {reply}")

    await bot.process_commands(message)


def _is_discord_file_admin(ctx: commands.Context) -> bool:
    """Only the user set as DISCORD_ADMIN_ID can use file commands on Discord."""
    return _discord_admin_id_int is not None and ctx.author.id == _discord_admin_id_int


def _scope_for_discord_user(author_id: int, guild_id: int | None) -> str:
    """Resolve Discord memory scope by user rules."""
    if _linked_discord_id_int is not None and author_id == _linked_discord_id_int:
        return LINKED_SCOPE
    if author_id in _discord_dm_sync_ids_int:
        return f"discord:user:{author_id}"
    return f"discord:{guild_id}:{author_id}" if guild_id else f"discord:dm:{author_id}"


@bot.command(name="files")
async def cmd_files_help(ctx: commands.Context):
    """Show Luna's commands (file access, automation, memory, voice)."""
    await ctx.reply(LUNA_COMMANDS_REPLY)


@bot.command(name="commands")
async def cmd_commands(ctx: commands.Context):
    """Show Luna's commands — same as !files."""
    await ctx.reply(LUNA_COMMANDS_REPLY)


@bot.command(name="news")
async def cmd_news(ctx: commands.Context):
    """Show latest world news headlines."""
    ok, result = await asyncio.to_thread(employee_newsroom)
    await ctx.reply(result if ok else f"❌ {result}")


@bot.command(name="suno")
async def cmd_suno(ctx: commands.Context, *, description: str = ""):
    """Create a Suno song from prompt using Playwright + your browser profile (linked/admin only)."""
    if not _can_use_suno_discord(ctx.author.id):
        await ctx.reply("Only the linked user/admin can use Suno automation.")
        return
    prompt = (description or "").strip()
    if not prompt:
        await ctx.reply("Usage: `!suno <song description>`")
        return
    await ctx.reply("Opening Suno and creating your song...")
    ok, result = await asyncio.to_thread(_run_suno_create, prompt)
    await ctx.reply(f"{'✅' if ok else '❌'} {result}")


@bot.command(name="share_song")
async def cmd_share_song(ctx: commands.Context):
    """Share a random song from your configured YouTube channel to X (linked/admin only)."""
    if not _can_use_x_share_discord(ctx.author.id):
        await ctx.reply("Only the linked user/admin can use X sharing automation.")
        return
    await ctx.reply("Picking a random song from your YouTube channel and sharing it to X...")
    ok, result = await asyncio.to_thread(_run_x_share_random_song)
    if not ok:
        _record_automation_failure("share_x", result, {})
    await ctx.reply(f"{'✅' if ok else '❌'} {result}")


@bot.command(name="share_facebook")
async def cmd_share_facebook(ctx: commands.Context):
    """Share a random song from your configured YouTube channel to Facebook (linked/admin only)."""
    if not _can_use_x_share_discord(ctx.author.id):
        await ctx.reply("Only the linked user/admin can use Facebook sharing automation.")
        return
    await ctx.reply("Picking a random song from your YouTube channel and sharing it to Facebook...")
    ok, result = await asyncio.to_thread(_run_facebook_share_random_song)
    if not ok:
        _record_automation_failure("share_facebook", result, {})
    await ctx.reply(f"{'✅' if ok else '❌'} {result}")


@bot.command(name="yt_comment")
async def cmd_yt_comment(ctx: commands.Context, *, video_url: str = ""):
    """Transcribe a YouTube video and post a thoughtful comment (linked/admin only)."""
    if not _can_use_youtube_comment_discord(ctx.author.id):
        await ctx.reply("Only the linked user/admin can use YouTube comment automation.")
        return
    raw = (video_url or "").strip()
    target = _extract_youtube_video_url(raw) or raw
    if not target:
        await ctx.reply("Usage: `!yt_comment <youtube_video_url>`")
        return
    await ctx.reply("Opening YouTube, transcribing the video, and posting a comment...")
    ok, result = await asyncio.to_thread(_run_youtube_comment, target)
    if not ok:
        _record_automation_failure("yt_comment", result, {"video_url": target})
    await ctx.reply(f"{'✅' if ok else '❌'} {result}")


@bot.command(name="ig_dm")
async def cmd_ig_dm(ctx: commands.Context, *, args: str = ""):
    """Send an Instagram DM by username or direct thread URL (linked/admin only)."""
    if not _can_use_instagram_dm_discord(ctx.author.id):
        await ctx.reply("Only the linked user/admin can use Instagram DM automation.")
        return
    text = (args or "").strip()
    if not text:
        await ctx.reply("Usage: `!ig_dm <username|instagram_direct_thread_url> [message]`")
        return
    parts = text.split(None, 1)
    target = (parts[0] or "").strip()
    custom_message = (parts[1] or "").strip() if len(parts) > 1 else ""
    announce_target = target if _extract_instagram_thread_url(target) else f"@{re.sub(r'^@', '', target)}"
    await ctx.reply(f"Opening Instagram and messaging {announce_target}...")
    ok, result = await asyncio.to_thread(_run_instagram_dm, target, custom_message)
    if not ok:
        _record_automation_failure("ig_dm", result, {"target": target, "message": custom_message})
    await ctx.reply(f"{'✅' if ok else '❌'} {result}")


@bot.command(name="fb_msg", aliases=["messenger", "fbmsg"])
async def cmd_fb_msg(ctx: commands.Context, *, args: str = ""):
    """Send a Facebook Messenger message by name or username (linked/admin only). Usage: !fb_msg <username> [message]"""
    if not _can_use_messenger_discord(ctx.author.id):
        await ctx.reply("Only the linked user/admin can use Messenger automation.")
        return
    text = (args or "").strip()
    if not text:
        await ctx.reply("Usage: `!fb_msg <username or name> [message]` (e.g. !fb_msg John or !fb_msg John have a great day)")
        return
    parts = text.split(None, 1)
    target = (parts[0] or "").strip()
    custom_message = (parts[1] or "").strip() if len(parts) > 1 else ""
    await ctx.reply(f"Opening Messenger and sending a message to **{target}**...")
    ok, result = await asyncio.to_thread(_run_messenger_msg, target, custom_message)
    if not ok:
        _record_automation_failure("fb_msg", result, {"target": target, "message": custom_message})
    await ctx.reply(f"{'✅' if ok else '❌'} {result}")


@bot.command(name="share")
async def cmd_share(ctx: commands.Context, *, args: str = ""):
    """Alias: !share song -> share a random channel song to X (linked/admin only)."""
    arg_low = (args or "").strip().lower()
    if arg_low.startswith("song"):
        await cmd_share_song(ctx)
        return
    if arg_low.startswith("facebook"):
        await cmd_share_facebook(ctx)
        return
    await ctx.reply("Usage: `!share song` or `!share facebook`")


@bot.command(name="dm")
async def cmd_dm(ctx: commands.Context, *, args: str = ""):
    """Send a DM through Luna. Usage: !dm <@user|user_id> <message> (linked/admin only)."""
    if not _can_use_discord_dm_action(ctx.author.id):
        await ctx.reply("Only the linked user/admin can make me DM someone.")
        return
    text = (args or "").strip()
    if not text or " " not in text:
        await ctx.reply("Usage: `!dm <@user|user_id> <message>`")
        return
    target_token, message_text = text.split(None, 1)
    target_id = _extract_discord_user_id(target_token)
    if target_id is None:
        await ctx.reply("I couldn't parse the user. Use a mention like `@User` or a numeric user ID.")
        return
    message_text = message_text.strip()
    if not message_text:
        await ctx.reply("Usage: `!dm <@user|user_id> <message>`")
        return
    try:
        user = bot.get_user(target_id) or await bot.fetch_user(target_id)
        if user is None:
            await ctx.reply("I couldn't find that user.")
            return
        channel = user.dm_channel or await user.create_dm()
        await channel.send(message_text)
        await ctx.reply(f"✅ Sent DM to **{user}** (`{target_id}`).")
    except discord.Forbidden:
        await ctx.reply("❌ I can't DM that user (privacy settings or no shared server).")
    except discord.NotFound:
        await ctx.reply("❌ User not found.")
    except Exception as e:
        await ctx.reply(f"❌ DM failed: {e}")


@bot.command(name="remember")
async def cmd_remember(ctx: commands.Context, *, fact: str):
    """Layer 2/3: Tell Luna to remember something. Example: !remember I prefer dark mode."""
    fact = (fact or "").strip()
    if not fact:
        await ctx.reply("Usage: `!remember <something to remember>`")
        return
    scope = _scope_for_discord_user(ctx.author.id, ctx.guild.id if ctx.guild else None)
    await asyncio.to_thread(add_memory, scope, fact)
    await ctx.reply("Got it, I'll remember that.")


@bot.command(name="always_remember")
async def cmd_always_remember(ctx: commands.Context, *, fact: str):
    """Layer 1 (Core): Always remember this—e.g. name, essential preference. Example: !always_remember My name is Alex."""
    fact = (fact or "").strip()
    if not fact:
        await ctx.reply("Usage: `!always_remember <essential fact>`")
        return
    scope = _scope_for_discord_user(ctx.author.id, ctx.guild.id if ctx.guild else None)
    await asyncio.to_thread(add_core_memory, scope, fact)
    await ctx.reply("Got it, I'll always keep that in mind.")


@bot.command(name="memories")
async def cmd_memories(ctx: commands.Context):
    """Show Luna's 4 layers of memory about you (core, long-term, short-term, working = current chat)."""
    scope = _scope_for_discord_user(ctx.author.id, ctx.guild.id if ctx.guild else None)
    core = await asyncio.to_thread(get_core_memories, scope)
    short = await asyncio.to_thread(get_short_term_memories, scope)
    long_term = await asyncio.to_thread(get_long_term_memories, scope, 15)
    if not core and not short and not long_term:
        await ctx.reply(
            "I don't have any memories for you yet. Use `!remember <fact>` or `!always_remember <fact>`, "
            "or tell me things like your name or preferences in chat."
        )
        return
    parts = []
    if core:
        parts.append("**Layer 1 – Core:**\n" + "\n".join(f"• {m}" for m in core))
    if long_term:
        parts.append("**Layer 2 – Long-term:**\n" + "\n".join(f"• {m}" for m in long_term))
    if short:
        parts.append("**Layer 3 – Short-term (recent):**\n" + "\n".join(f"• {m}" for m in short))
    parts.append("**Layer 4 – Working:** Current conversation (not stored).")
    text = "\n\n".join(parts)
    if len(text) > 1900:
        text = text[:1897] + "..."
    await ctx.reply(text)


@bot.command(name="forget")
async def cmd_forget(ctx: commands.Context):
    """Clear Layer 2/3 (long-term and short-term) memories. Core (Layer 1) is kept."""
    scope = _scope_for_discord_user(ctx.author.id, ctx.guild.id if ctx.guild else None)
    n = await asyncio.to_thread(clear_memories, scope)
    await ctx.reply(f"Done. Cleared {n} long-term/short-term memory(ies). Use `!forget_all` to clear everything including core.")


@bot.command(name="forget_all")
async def cmd_forget_all(ctx: commands.Context):
    """Clear all of Luna's memories about you (core + long-term + short-term)."""
    scope = _scope_for_discord_user(ctx.author.id, ctx.guild.id if ctx.guild else None)
    nc, nl = await asyncio.to_thread(clear_all_memories, scope)
    await ctx.reply(f"Done. Cleared {nc} core and {nl} long-term memory(ies).")


@bot.command(name="profile")
async def cmd_profile(ctx: commands.Context, *, args: str = ""):
    """Show or set your permanent profile (name, location, occupation, interests, birthday, other). Example: !profile set name Alex"""
    scope = _scope_for_discord_user(ctx.author.id, ctx.guild.id if ctx.guild else None)
    args = (args or "").strip()
    if args.lower().startswith("set "):
        rest = args[4:].strip()
        if " " not in rest:
            await ctx.reply("Usage: `!profile set <field> <value>`. Fields: " + ", ".join(PROFILE_FIELDS))
            return
        field, value = rest.split(None, 1)
        field = field.lower()
        if field not in PROFILE_FIELDS:
            await ctx.reply(f"Unknown field. Use one of: {', '.join(PROFILE_FIELDS)}")
            return
        await asyncio.to_thread(set_profile_field, scope, field, value)
        await ctx.reply(f"Updated profile **{field}**.")
        return
    if args.lower() == "clear":
        n = await asyncio.to_thread(clear_profile, scope)
        await ctx.reply(f"Cleared profile ({n} field(s) had values).")
        return
    profile = await asyncio.to_thread(get_profile, scope)
    filled = [(k, v) for k, v in profile.items() if v]
    if not filled:
        await ctx.reply("Your profile is empty. Tell me your name, where you live, etc., or use `!profile set <field> <value>`.")
        return
    text = "**Your profile:**\n" + "\n".join(f"• **{k}**: {v}" for k, v in profile.items() if v)
    if len(text) > 1900:
        text = text[:1897] + "..."
    await ctx.reply(text)


@bot.command(name="play")
async def cmd_play(ctx: commands.Context, *, query: str = ""):
    """Play YouTube or Suno audio in voice channel. Usage: !play <url or search>"""
    if not ctx.guild:
        await ctx.reply("Music playback is only available in a server voice channel.")
        return
    q = (query or "").strip()
    if not q:
        await ctx.reply("Usage: `!play <youtube url or search>`, `!play <suno url>`, or `!play <filename.mp3>` (from your music folder)")
        return
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.reply("Join a voice channel first, then use `!play`.")
        return

    vc = ctx.voice_client
    target_channel = ctx.author.voice.channel
    try:
        if vc and vc.is_connected():
            if vc.channel.id != target_channel.id:
                await vc.move_to(target_channel)
        else:
            vc = await target_channel.connect()
    except Exception as e:
        await ctx.reply(f"Couldn't join voice channel: {e}")
        return

    if ".mp3" in q.lower() or ".m4a" in q.lower() or (q.lower().endswith(".wav")):
        await ctx.reply(f"Looking for local file: `{q[:60]}{'…' if len(q) > 60 else ''}` ...")
    elif _is_suno_url(q):
        await ctx.reply(f"Downloading Suno track to your folder and queuing: `{q[:50]}{'…' if len(q) > 50 else ''}` ...")
    else:
        await ctx.reply(f"Searching YouTube for: `{q}` ...")
    ok, result = await asyncio.to_thread(_resolve_play_track, q)
    if not ok:
        await ctx.reply(f"❌ {result}")
        return

    track = result
    track["request_channel_id"] = ctx.channel.id
    state = _music_state_for_guild(ctx.guild.id)
    state["queue"].append(track)
    queued_pos = len(state["queue"])

    started = False
    if vc and vc.is_connected() and not vc.is_playing() and not vc.is_paused() and state.get("current") is None:
        started = _start_next_music_track(ctx.guild.id, vc)

    if started:
        await ctx.reply(f"▶️ Starting playback: **{track.get('title','Unknown')}**")
    else:
        await ctx.reply(f"➕ Added to queue (#{queued_pos}): **{track.get('title','Unknown')}**")


@bot.command(name="pause")
async def cmd_pause(ctx: commands.Context):
    """Pause current music playback."""
    vc = ctx.voice_client
    if not vc or not vc.is_connected():
        await ctx.reply("I'm not in a voice channel.")
        return
    if vc.is_playing():
        vc.pause()
        await ctx.reply("⏸️ Paused.")
    else:
        await ctx.reply("Nothing is playing right now.")


@bot.command(name="resume")
async def cmd_resume(ctx: commands.Context):
    """Resume paused music playback."""
    vc = ctx.voice_client
    if not vc or not vc.is_connected():
        await ctx.reply("I'm not in a voice channel.")
        return
    if vc.is_paused():
        vc.resume()
        await ctx.reply("▶️ Resumed.")
    else:
        await ctx.reply("Playback is not paused.")


@bot.command(name="skip")
async def cmd_skip(ctx: commands.Context):
    """Skip current track and play the next queued one."""
    vc = ctx.voice_client
    if not vc or not vc.is_connected():
        await ctx.reply("I'm not in a voice channel.")
        return
    if vc.is_playing() or vc.is_paused():
        if ctx.guild:
            _music_state_for_guild(ctx.guild.id)["manual_skip"] = True
        vc.stop()
        await ctx.reply("⏭️ Skipped.")
    else:
        await ctx.reply("Nothing is playing right now.")


@bot.command(name="stop")
async def cmd_stop(ctx: commands.Context):
    """Stop playback and clear queue."""
    if not ctx.guild:
        await ctx.reply("This command is only available in a server.")
        return
    vc = ctx.voice_client
    state = _music_state_for_guild(ctx.guild.id)
    state["manual_skip"] = True
    state["queue"].clear()
    _cleanup_track_temp_file(state.get("current"))
    state["current"] = None
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
    await ctx.reply("⏹️ Stopped playback and cleared the queue.")


@bot.command(name="queue")
async def cmd_queue(ctx: commands.Context):
    """Show now playing and upcoming tracks."""
    if not ctx.guild:
        await ctx.reply("This command is only available in a server.")
        return
    state = _music_state_for_guild(ctx.guild.id)
    current = state.get("current")
    queued = list(state.get("queue") or [])
    if not current and not queued:
        await ctx.reply("Queue is empty. Use `!play <url or search>`.")
        return

    lines = []
    if current:
        cur_dur = _fmt_seconds(int(current.get("duration") or 0))
        cur_dur = f" ({cur_dur})" if int(current.get("duration") or 0) > 0 else ""
        lines.append(f"🎵 **Now playing:** {current.get('title','Unknown')}{cur_dur}")
    if queued:
        lines.append("📜 **Up next:**")
        for i, t in enumerate(queued[:10], 1):
            dur = _fmt_seconds(int(t.get("duration") or 0))
            dur = f" ({dur})" if int(t.get("duration") or 0) > 0 else ""
            lines.append(f"{i}. {t.get('title','Unknown')}{dur}")
        if len(queued) > 10:
            lines.append(f"... and {len(queued) - 10} more")
    out = "\n".join(lines)
    if len(out) > 1900:
        out = out[:1897] + "..."
    await ctx.reply(out)


@bot.command(name="join")
async def cmd_join(ctx: commands.Context):
    """Find a voice channel with someone in it and join. Stays until you use !leave."""
    if not ctx.guild:
        await ctx.reply("Voice channels are only available in a server.")
        return
    # Already in a voice channel
    if ctx.voice_client and ctx.voice_client.is_connected():
        await ctx.reply(f"I'm already in **{ctx.voice_client.channel.name}**. Use `!leave` to make me disconnect.")
        return
    # Prefer the channel the author is in
    channel = None
    if ctx.author.voice and ctx.author.voice.channel:
        channel = ctx.author.voice.channel
    else:
        # Search for any voice channel in the guild that has at least one member (not just bots)
        for vc in ctx.guild.voice_channels:
            members = [m for m in vc.members if not m.bot]
            if members:
                channel = vc
                break
    if not channel:
        await ctx.reply("No voice channel with anyone in it. Join a voice channel and use `!join`, or I'll join your channel.")
        return
    try:
        await channel.connect()
        await ctx.reply(f"Joined **{channel.name}**. I'll stay here until you say `!leave`.")
    except discord.ClientException as e:
        await ctx.reply(f"Couldn't join: {e}")
    except Exception as e:
        await ctx.reply(f"Error: {e}")


@bot.command(name="leave")
async def cmd_leave(ctx: commands.Context):
    """Disconnect Luna from the current voice channel."""
    if not ctx.voice_client or not ctx.voice_client.is_connected():
        await ctx.reply("I'm not in a voice channel.")
        return
    if ctx.guild:
        _music_state_for_guild(ctx.guild.id)["manual_skip"] = True
        _clear_music_state_for_guild(ctx.guild.id)
    try:
        if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
            ctx.voice_client.stop()
    except Exception:
        pass
    channel_name = ctx.voice_client.channel.name
    await ctx.voice_client.disconnect()
    await ctx.reply(f"Left **{channel_name}**.")


@bot.command(name="stoptts")
async def cmd_stoptts(ctx: commands.Context):
    """Stop Luna's TTS (or any audio) playing in the current voice channel."""
    if not ctx.voice_client or not ctx.voice_client.is_connected():
        await ctx.reply("I'm not in a voice channel, so there's nothing to stop.")
        return
    if not (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
        await ctx.reply("I'm not playing anything right now.")
        return
    try:
        ctx.voice_client.stop()
        await ctx.reply("Stopped.")
    except Exception as e:
        await ctx.reply(f"Couldn't stop: {e}")


@bot.command(name="call")
async def cmd_whatsapp_call(ctx: commands.Context, *, contact: str = ""):
    """Open WhatsApp Desktop, search contact, find Call then Voice to start voice call. Usage: !call <contact name>"""
    if not _can_use_whatsapp_discord(ctx.author.id):
        await ctx.reply("Only the linked user or admin can use WhatsApp automation.")
        return
    contact = (contact or "").strip()
    if not contact:
        await ctx.reply("Usage: `!call <contact name>` (e.g. !call Marios)")
        return
    await ctx.reply(f"Opening WhatsApp and starting a call with **{contact}**…")
    ok, result = await asyncio.to_thread(_run_whatsapp_web_call, contact)
    if not ok:
        _record_automation_failure("call", result, {"contact": contact})
    await ctx.reply(result if ok else f"❌ {result}")


@bot.command(name="msg")
async def cmd_whatsapp_msg(ctx: commands.Context, *, args: str = ""):
    """Open WhatsApp Web, open chat with contact, and send a message. Usage: !msg <contact> [description]"""
    if not _can_use_whatsapp_discord(ctx.author.id):
        await ctx.reply("Only the linked user or admin can use WhatsApp automation.")
        return
    args = (args or "").strip()
    if not args:
        await ctx.reply("Usage: `!msg <contact> [description]` (e.g. !msg Marios or !msg Marios goodnight)")
        return
    contact_name, description = _parse_whatsapp_msg_args(args)
    if not contact_name:
        await ctx.reply("Usage: `!msg <contact> [description]`")
        return
    await ctx.reply(f"Opening WhatsApp and sending message to **{contact_name}**…")
    ok, result = await asyncio.to_thread(_run_whatsapp_web_msg, contact_name, description)
    if not ok:
        _record_automation_failure("msg", result, {"contact": contact_name, "description": description or ""})
    await ctx.reply(result if ok else f"❌ {result}")


def _warmup_ollama():
    """Send a minimal request to Ollama after a short delay so the model stays loaded; first user reply is faster."""
    time.sleep(5)
    try:
        body = json.dumps({
            "model": OLLAMA_MODEL,
            "messages": [{"role": "user", "content": "."}],
            "stream": False,
        }).encode()
        req = urllib.request.Request(
            f"{OLLAMA_BASE}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        print("[Luna] Ollama model warmup done.", flush=True)
    except Exception:
        pass


def _start_share_scheduler():
    """No-op: scheduled X/Facebook shares (George) removed."""
    pass


def main():
    _start_share_scheduler()
    threading.Thread(target=_warmup_ollama, daemon=True).start()
    web_thread = threading.Thread(target=run_web_ui, daemon=True)
    web_thread.start()
    threading.Thread(target=open_web_ui_in_browser, daemon=True).start()
    print("Web UI: http://127.0.0.1:5050")
    try:
        bot.run(DISCORD_TOKEN)
    except discord.LoginFailure:
        print("Invalid token (401).")
        print("  → Use the BOT token: Developer Portal → Your App → BOT (left menu) → Reset Token.")
        print("  → Do NOT use the 'Client Secret' from OAuth2 — that is not the bot token.")
        print(f"  → .env used: {_env_path}")
        print("  → In .env use one line: DISCORD_TOKEN=paste_token_here   (no quotes, no spaces)")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
