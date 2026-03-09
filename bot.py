"""
Luna — Discord chatbot (Python) with Ollama (e.g. llama3.2, deepseek-r1).
Responds when mentioned or in DMs. Add your token to .env and run: python bot.py

Or pass token on command line: python bot.py YOUR_TOKEN

Also runs a web UI at http://127.0.0.1:5050 — Jarvis-style chat in the browser.
"""
import asyncio
import base64
import html
import io
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
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

from luna_files import (
    safe_path as luna_safe_path,
    read_file as luna_read_file,
    write_file as luna_write_file,
    list_dir as luna_list_dir,
    modify_file as luna_modify_file,
)


def _open_file_in_editor(relative_path: str) -> bool:
    """Open a file from Luna projects in the default app (e.g. Notepad for .txt). Returns True if opened."""
    full = luna_safe_path(relative_path)
    if full is None or not full.is_file():
        return False
    return _open_file_by_path(str(full))


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
from luna_profile import (
    get_profile_prompt,
    get_profile,
    set_profile_field,
    try_capture_profile_from_reply,
    clear_profile,
    PROFILE_FIELDS,
    merge_profiles,
)
from luna_conversation import get_recent_conversation, append_exchange, merge_conversations
from local_music import create_local_song_project

# Ollama defaults (override in .env: OLLAMA_BASE_URL, OLLAMA_MODEL)
OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:latest")

LUNA_SYSTEM_PROMPT = """You are Luna, a friendly AI companion. You're warm, helpful, and a bit playful. Keep replies concise (a few sentences). Speak in first person as Luna.
File access: Luna has access to the "Luna projects" folder. When the user asks you to create a website, game, or any file(s) and wants them saved there, you MUST save them via the system—do not only paste code in chat. For every file to save, add this exact block (one block per file). Put all blocks at the end of your reply with no other text after the last END_LUNA_WRITE.
LUNA_WRITE_FILE
path: <path>
---
<content>
END_LUNA_WRITE
Use path relative to Luna projects (e.g. index.html, css/styles.css, js/game.js). For multiple files (e.g. HTML + CSS + JS for a game), output multiple blocks back-to-back. In your reply text, briefly say what you prepared and that they can reply "yes" to create the files in Luna projects. Do not mention the block names. Never say you have already created or saved the file—creation happens only after the user replies "yes". Do not tell the user to use !write. For read/list/edit only, tell them to use !read, !list, !edit.
You have a permanent user profile (name, location, occupation, interests, birthday). When you don't know a profile field, politely ask the user; their answers are saved automatically."""

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
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:latest")
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
# WhatsApp Desktop (Windows): optional path to WhatsApp.exe; default %LOCALAPPDATA%\WhatsApp\WhatsApp.exe
if sys.platform == "win32":
    _whatsapp_default_path = os.path.join(os.environ.get("LOCALAPPDATA", ""), "WhatsApp", "WhatsApp.exe")
else:
    _whatsapp_default_path = ""
WHATSAPP_APP_PATH = (os.environ.get("WHATSAPP_APP_PATH") or _whatsapp_default_path).strip()
FACEBOOK_PROFILE_URL = (os.environ.get("FACEBOOK_PROFILE_URL") or "https://www.facebook.com/solonaras").strip()
FACEBOOK_HOME_URL = (os.environ.get("FACEBOOK_HOME_URL") or "https://www.facebook.com/").strip()
FACEBOOK_PROFILE_DIR = os.environ.get(
    "FACEBOOK_PROFILE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "facebook_profile"),
).strip()
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
_FFMPEG_BEFORE_OPTS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
_FFMPEG_OPTS = "-vn"


def _cleanup_track_temp_file(track: dict | None) -> None:
    """Delete downloaded temporary audio file for a track, if any."""
    if not isinstance(track, dict):
        return
    fp = (track.get("local_path") or "").strip()
    if not fp:
        return
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

    ydl_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch1",
        "extract_flat": False,
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(q, download=False)
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


def _download_youtube_audio_temp(web_url: str) -> tuple[bool, str]:
    """Download YouTube audio to a temp file for stable Discord playback."""
    src = (web_url or "").strip()
    if not src:
        return False, "Missing source URL for download fallback."
    try:
        import yt_dlp
    except Exception:
        return False, "yt-dlp is not installed."
    try:
        temp_dir = os.path.join(tempfile.gettempdir(), "luna_music_cache")
        os.makedirs(temp_dir, exist_ok=True)
        token = str(int(time.time() * 1000))
        outtmpl = os.path.join(temp_dir, f"luna_{token}_%(id)s.%(ext)s")
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
        source = discord.FFmpegPCMAudio(
            local_path,
            before_options="-nostdin",
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

        if (not manual_skip) and ended_too_soon and retry_count < 1:
            web_url = (current.get("web_url") or "").strip()
            if web_url:
                ok, refreshed = await asyncio.to_thread(_resolve_youtube_track, web_url)
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
        if (not manual_skip) and ended_too_soon and retry_count < 2 and not local_path:
            web_url = (current.get("web_url") or "").strip()
            if web_url:
                ok_dl, dl = await asyncio.to_thread(_download_youtube_audio_temp, web_url)
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
    """Append user profile and 4-layer memory to system prompt if scope given. Discord = per-user scope."""
    if not base:
        return None
    if not memory_scope:
        return base
    parts = [base.rstrip()]
    # Linked user (web + Discord): same person on both platforms
    if LINKED_SCOPE and memory_scope == LINKED_SCOPE:
        parts.append(
            "The current user is Chris (Solonaras). They use both the web UI and Discord—treat them as the same person. "
            "Remember them and continue conversations naturally on either platform. "
            "When they ask for a game, website, or any code to be saved in Luna projects, you must add LUNA_WRITE_FILE blocks (one per file) so the files are created there—do not only show code in the chat."
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
    if len(parts) <= 1:
        return base
    return "\n\n".join(parts)


def _try_capture_memory(scope: str, user_message: str) -> None:
    """If user said something worth remembering, store in the right layer (core vs long-term)."""
    text = (user_message or "").strip()
    if not text or not scope:
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


def ollama_chat(
    user_message: str,
    system_prompt: str | None = None,
    memory_scope: str | None = None,
    message_history: list[dict] | None = None,
) -> str:
    """Send user message to Ollama, return assistant reply. Blocking.
    message_history: optional list of {"role": "user"|"assistant", "content": "..."} for short-term context.
    """
    prompt = _build_system_prompt(system_prompt, memory_scope)
    messages = []
    if prompt:
        messages.append({"role": "system", "content": prompt})
    if message_history:
        for h in message_history[-20:]:  # last 20 turns
            role = (h.get("role") or "").lower()
            content = (h.get("content") or "").strip()
            if content and role in ("user", "assistant"):
                messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})
    body = json.dumps({"model": OLLAMA_MODEL, "messages": messages, "stream": False}).encode()
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


def _extract_local_song_description(text: str) -> str:
    """Extract prompt for local song/lyrics generation."""
    raw = (text or "").strip()
    if not raw:
        return ""
    low = raw.lower()
    if low.startswith("!local_song "):
        return raw[len("!local_song ") :].strip()
    if low.startswith("!local song "):
        return raw[len("!local song ") :].strip()
    patterns = [
        r"^\s*(?:luna[\s,:-]*)?(?:create|make|generate)\s+(?:a\s+)?local\s+(?:song|music)(?:\s+(?:about|for|with))?\s*[:,-]?\s*(.+)$",
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

    # Strong direct intents we should execute immediately.
    strong_yes_patterns = (
        r"\bshare\s+my\s+song\b",
        r"\bshare\b.*\b(?:x|twitter|facebook)\b",
        r"\bpost\b.*\b(?:x|twitter|facebook)\b",
        r"\bcomment\b.*\b(?:youtube|yt)\b",
        r"\b(?:instagram|insta|ig)\b.*\b(?:message|dm|send)\b",
        r"\b(?:create|make)\b.*\b(?:song)\b",
        r"\b(?:open|use)\b.*\bsuno\b",
        r"\b(?:local\s+song|local\s+music)\b",
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

            clicked = False
            for bsel in (
                "button:has-text('Create')",
                "button:has-text('Generate')",
                "[role='button']:has-text('Create')",
                "[role='button']:has-text('Generate')",
            ):
                btn = page.locator(bsel).first
                try:
                    if btn.count() and btn.is_visible():
                        btn.click()
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                return False, "I entered your prompt on Suno, but couldn't find the Create button."

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

                post_btn = page.locator("div[role='dialog'] button[data-testid='tweetButtonInline']").first
                if not (post_btn.count() and post_btn.is_visible()):
                    post_btn = page.locator("div[role='dialog'] button[data-testid='tweetButton']").first
                if not (post_btn.count() and post_btn.is_visible()):
                    post_btn = page.locator("div[role='dialog'] button:has-text('Post')").first
                if not (post_btn.count() and post_btn.is_visible()):
                    post_btn = page.locator("button:has-text('Post')").first
                if not (post_btn.count() and post_btn.is_visible()):
                    return False, "I typed the message, but couldn't find the Post button."

                def _is_enabled(btn):
                    try:
                        if not btn.count() or not btn.is_visible():
                            return False
                        if btn.is_disabled():
                            return False
                        aria_disabled = (btn.get_attribute("aria-disabled") or "").lower().strip()
                        return aria_disabled not in ("true", "1")
                    except Exception:
                        return False

                # Wait for X to enable Post after typing.
                deadline = time.time() + 8
                while time.time() < deadline and not _is_enabled(post_btn):
                    page.wait_for_timeout(250)

                if not _is_enabled(post_btn):
                    # Fallback: retype a shorter plain message in case rich text input failed to register.
                    short_message = f"{song_title[:60].strip()} {song_url}".strip()[:220]
                    if not _set_x_text(textbox, short_message, delay_ms=14):
                        return False, "I couldn't re-focus and retype into the X post box for retry."
                    _resolve_x_profile_popup()
                    deadline2 = time.time() + 6
                    while time.time() < deadline2 and not _is_enabled(post_btn):
                        page.wait_for_timeout(250)

                if not _is_enabled(post_btn):
                    return False, "I typed the post, but X kept the Post button disabled. Please try once manually in that same compose box, then run again."

                try:
                    post_btn.click(timeout=5000)
                except Exception:
                    post_btn.click(timeout=5000, force=True)
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
                    page.wait_for_timeout(900)

                post_btn = _find_visible_button(
                    (
                        "div[role='dialog'] div[aria-label='Post']",
                        "div[role='dialog'] [role='button']:has-text('Post')",
                        "[role='button'][aria-label='Post']",
                        "[data-testid='react-composer-post-button']",
                        "button:has-text('Post')",
                    )
                )
                if post_btn is None:
                    return False, "I typed the Facebook post, but couldn't find the Post button."

                post_clicked = False
                post_deadline = time.time() + 8
                while time.time() < post_deadline:
                    try:
                        disabled = False
                        if hasattr(post_btn, "is_disabled") and post_btn.is_disabled():
                            disabled = True
                        aria_dis = (post_btn.get_attribute("aria-disabled") or "").lower().strip()
                        if aria_dis in ("true", "1"):
                            disabled = True
                        if not disabled:
                            post_btn.click()
                            post_clicked = True
                            break
                    except Exception:
                        pass
                    page.wait_for_timeout(250)
                if not post_clicked:
                    return False, "I found Facebook Post, but it stayed disabled."

                page.wait_for_timeout(2800)
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
            # Shorts UI may not expose the standard comment composer reliably.
            # Fallback to normal watch page for robust comment posting.
            if not found and is_shorts:
                try:
                    page.goto(watch_url, wait_until="domcontentloaded", timeout=90000)
                    page.wait_for_timeout(800)
                except Exception:
                    pass
                found = _locate_comment_placeholder()

            if not found:
                return False, "I couldn't open the YouTube comment box (including Shorts fallback)."

            placeholder = page.locator("ytd-comment-simplebox-renderer #simplebox-placeholder").first
            try:
                placeholder.click(timeout=3000, force=True)
            except Exception:
                return False, "I found comments but couldn't activate the comment editor."

            editor = page.locator("ytd-comment-simplebox-renderer #contenteditable-root[contenteditable='true']").first
            if not (editor.count() and editor.is_visible()):
                return False, "I couldn't find the editable YouTube comment field."
            try:
                editor.click(timeout=2500, force=True)
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
                page.keyboard.type(comment_text, delay=14)
                page.wait_for_timeout(500)
            except Exception as e:
                return False, f"I couldn't type the YouTube comment: {e}"

            submit_btn = page.locator("ytd-commentbox #submit-button button").first
            if not (submit_btn.count() and submit_btn.is_visible()):
                submit_btn = page.locator("ytd-commentbox #submit-button").first
            if not (submit_btn.count() and submit_btn.is_visible()):
                return False, "I typed the comment but couldn't find the YouTube Post button."

            def _is_enabled(btn):
                try:
                    if not btn.count() or not btn.is_visible():
                        return False
                    if hasattr(btn, "is_disabled") and btn.is_disabled():
                        return False
                    aria = (btn.get_attribute("aria-disabled") or "").lower().strip()
                    return aria not in ("true", "1")
                except Exception:
                    return False

            deadline = time.time() + 8
            while time.time() < deadline and not _is_enabled(submit_btn):
                page.wait_for_timeout(250)

            if not _is_enabled(submit_btn):
                return False, "I typed the YouTube comment, but Post stayed disabled."

            try:
                submit_btn.click(timeout=5000)
            except Exception:
                submit_btn.click(timeout=5000, force=True)
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
    wait_deadline = time.time() + 15
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
    if editor is None:
        return False, "I couldn't find the Instagram DM text box at the bottom."

    try:
        editor.scroll_into_view_if_needed(timeout=2500)
    except Exception:
        pass
    try:
        editor.click(timeout=4000, force=True)
        # Type slower so the user can visibly watch the message being written.
        page.keyboard.type(dm_text, delay=26)
        page.wait_for_timeout(1100)
    except Exception as e:
        return False, f"I couldn't type the Instagram DM: {e}"

    sent = False
    send_selectors = (
        "button[type='submit']",
        "button:has-text('Send')",
        "div[role='button']:has-text('Send')",
    )
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

    if not sent:
        return False, "I typed the Instagram DM but couldn't press Send on the right side."
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

                # 2) Use inbox search exactly like your screenshot flow.
                search_box = None
                for sel in (
                    "aside input[placeholder='Search']",
                    "aside input[aria-label='Search input']",
                    "input[placeholder='Search']",
                    "input[aria-label='Search input']",
                ):
                    loc = page.locator(sel).first
                    try:
                        if loc.count() and loc.is_visible():
                            search_box = loc
                            break
                    except Exception:
                        continue
                if search_box is None:
                    return False, "I couldn't find the Instagram inbox search box."

                try:
                    search_box.click(timeout=3000, force=True)
                    page.keyboard.press("Control+A")
                    page.keyboard.press("Backspace")
                    page.keyboard.type(target, delay=12)
                    page.wait_for_timeout(1200)
                except Exception as e:
                    return False, f"I couldn't type in Instagram inbox search: {e}"

                for sel in (
                    f"aside a[href*='/direct/t/']:has-text('{target}')",
                    f"aside div[role='button']:has-text('{target}')",
                    f"div[role='dialog'] [role='button']:has-text('{target}')",
                    f"main a[href*='/direct/t/']:has-text('{target}')",
                ):
                    loc = page.locator(sel).first
                    try:
                        if loc.count() and loc.is_visible():
                            loc.click(timeout=4000, force=True)
                            page.wait_for_timeout(900)
                            return True, "thread-opened-search"
                    except Exception:
                        continue
                return False, f"I couldn't find @{target} in Instagram inbox results."

            ok_thread, thread_msg = _open_thread_in_inbox()
            if not ok_thread:
                return False, thread_msg

            editor = None
            for sel in (
                "textarea[placeholder='Message...']",
                "textarea[aria-label='Message']",
                "div[role='textbox'][contenteditable='true']",
                "div[contenteditable='true'][aria-label='Message']",
                "div[aria-label='Message'][contenteditable='true']",
            ):
                loc = page.locator(sel).first
                try:
                    if loc.count() and loc.is_visible():
                        editor = loc
                        break
                except Exception:
                    continue
            if editor is None:
                return False, "I couldn't find the Instagram DM text box."

            try:
                editor.click(timeout=4000, force=True)
                # Type slower so the user can visibly watch the message being written.
                page.keyboard.type(dm_text, delay=26)
                page.wait_for_timeout(1100)
            except Exception as e:
                return False, f"I couldn't type the Instagram DM: {e}"

            sent = False
            for sel in (
                "button:has-text('Send')",
                "div[role='button']:has-text('Send')",
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

            if not sent:
                return False, "I typed the Instagram DM but couldn't send it."

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


def _can_use_whatsapp_discord(author_id: int) -> bool:
    """Allow WhatsApp call/msg automation on Discord only for linked user or admin."""
    if _discord_admin_id_int is not None and author_id == _discord_admin_id_int:
        return True
    if _linked_discord_id_int is not None and author_id == _linked_discord_id_int:
        return True
    return False


def _get_whatsapp_exe_path() -> str | None:
    """Return path to WhatsApp.exe (Windows). Tries WHATSAPP_APP_PATH then common install locations."""
    if sys.platform != "win32":
        return None
    if WHATSAPP_APP_PATH and os.path.isfile(WHATSAPP_APP_PATH):
        return WHATSAPP_APP_PATH
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        default = os.path.join(local_app_data, "WhatsApp", "WhatsApp.exe")
        if os.path.isfile(default):
            return default
    return WHATSAPP_APP_PATH or (local_app_data and os.path.join(local_app_data, "WhatsApp", "WhatsApp.exe")) or None


def _launch_whatsapp_via_start_menu() -> bool:
    """Open Start menu, search for WhatsApp, and click to launch (Windows). Works even when exe path is unknown."""
    if sys.platform != "win32":
        return False
    try:
        import pyautogui
    except ImportError:
        return False
    try:
        # Win key opens Start; on Win10/11 focus is in search, so we can type
        pyautogui.press("win")
        time.sleep(0.6)
        pyautogui.write("whatsapp", interval=0.06)
        time.sleep(1.2)
        pyautogui.press("enter")
        time.sleep(5)
        return True
    except Exception:
        return False


def _launch_whatsapp_desktop_if_needed() -> bool:
    """Start WhatsApp Desktop if not running (Windows). Tries existing window, exe path, then Start menu search."""
    if sys.platform != "win32":
        return False
    # 1) Already running on any monitor?
    try:
        from pywinauto import Application
        app = Application(backend="uia").connect(title_re=r".*WhatsApp.*", timeout=2)
        if app.windows():
            return True
    except Exception:
        pass
    # 2) Launch via exe path if we have it
    path = _get_whatsapp_exe_path()
    if path and os.path.isfile(path):
        try:
            subprocess.Popen([path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(5)
            try:
                Application(backend="uia").connect(title_re=r".*WhatsApp.*", timeout=5)
                return True
            except Exception:
                pass
        except Exception:
            pass
    # 3) Fallback: Start menu — open Start, search "whatsapp", press Enter
    if _launch_whatsapp_via_start_menu():
        try:
            Application(backend="uia").connect(title_re=r".*WhatsApp.*", timeout=12)
            return True
        except Exception:
            return True  # we launched something; user may need to wait a bit
    return False


def _run_whatsapp_desktop_open_contact(contact_name: str) -> tuple[bool, str]:
    """Open WhatsApp Desktop, search for contact, select from list (opens chat). Windows only."""
    if sys.platform != "win32":
        return False, "WhatsApp automation is only supported on Windows."
    contact_name = (contact_name or "").strip()
    if not contact_name:
        return False, "Please provide a contact name (e.g. !msg Marios)."
    try:
        from pywinauto import Application
    except ImportError:
        return False, "pywinauto is not installed. Run: pip install pywinauto"
    if not _launch_whatsapp_desktop_if_needed():
        return False, (
            "Could not start or find WhatsApp. "
            "Luna tried: (1) existing window, (2) launching via exe path, (3) Start menu → search 'whatsapp' → Enter. "
            "Install WhatsApp Desktop, or set WHATSAPP_APP_PATH in .env to your WhatsApp.exe path."
        )
    win = None
    try:
        app = Application(backend="uia").connect(title_re=r".*WhatsApp.*", timeout=12)
        win = app.window(title_re=r".*WhatsApp.*")
    except Exception:
        try:
            from pywinauto import Desktop
            desktop = Desktop(backend="uia")
            for w in desktop.windows():
                try:
                    t = (w.window_text() or "")
                    if "WhatsApp" in t:
                        win = w
                        break
                except Exception:
                    continue
        except Exception:
            pass
    if not win:
        return False, "WhatsApp is open but Luna could not attach to it. Focus the WhatsApp window and try !call again."
    try:
        win.set_focus()
        time.sleep(0.5)
        win.type_keys("^f")
        time.sleep(0.4)
        win.type_keys(contact_name, with_spaces=True)
        time.sleep(1.2)
        win.type_keys("{ENTER}")
        time.sleep(0.8)
        return True, f"Opened chat with **{contact_name}**."
    except Exception as e:
        err = (str(e) or "unknown")[:180]
        return False, f"WhatsApp automation error: {err}"


def _run_whatsapp_desktop_call(contact_name: str) -> tuple[bool, str]:
    """Open contact in WhatsApp Desktop, click Call (top right), then click Voice in the dropdown."""
    ok, msg = _run_whatsapp_desktop_open_contact(contact_name)
    if not ok:
        return ok, msg
    if sys.platform != "win32":
        return True, msg
    try:
        from pywinauto import Application
        from pywinauto import Desktop
        import pyautogui
        win = None
        try:
            app = Application(backend="uia").connect(title_re=r".*WhatsApp.*", timeout=8)
            win = app.window(title_re=r".*WhatsApp.*")
        except Exception:
            try:
                desktop = Desktop(backend="uia")
                for w in desktop.windows():
                    try:
                        if "WhatsApp" in (w.window_text() or ""):
                            win = w
                            break
                    except Exception:
                        continue
            except Exception:
                pass
        if not win:
            return True, f"Opened **{contact_name}**. Luna could not find the WhatsApp window for the call button. Tap Call then Voice manually."
        win.set_focus()
        time.sleep(0.5)
        rect = win.rectangle()
        call_clicked = False
        # Step 1: Click the Call button (top right, next to search) to open the dropdown
        for name in ("Call", "Voice call", "Phone"):
            pattern = f".*{re.escape(name)}.*"
            for ctrl_type in ("Button", "Hyperlink", "MenuItem", "Text", None):
                try:
                    if ctrl_type:
                        btn = win.child_window(title_re=pattern, control_type=ctrl_type)
                    else:
                        btn = win.child_window(title_re=pattern)
                    if btn.exists(timeout=0.8) and btn.is_enabled():
                        btn.click()
                        call_clicked = True
                        break
                except Exception:
                    continue
            if call_clicked:
                break
        if not call_clicked and rect.width() > 100 and rect.height() > 100:
            pyautogui.click(rect.right - 180, rect.top + 45)
            call_clicked = True
        if not call_clicked:
            return True, f"Opened **{contact_name}**. Could not find Call button; tap Call then Voice."
        time.sleep(0.7)
        # Step 2: Click the "Voice" option in the dropdown
        voice_clicked = False
        for name in ("Voice", "Voice call"):
            pattern = f".*{re.escape(name)}.*"
            for ctrl_type in ("Button", "MenuItem", "Hyperlink", "Text", None):
                try:
                    if ctrl_type:
                        btn = win.child_window(title_re=pattern, control_type=ctrl_type)
                    else:
                        btn = win.child_window(title_re=pattern)
                    if btn.exists(timeout=0.8) and btn.is_enabled():
                        btn.click()
                        voice_clicked = True
                        break
                except Exception:
                    continue
            if voice_clicked:
                break
        if not voice_clicked and rect.width() > 100 and rect.height() > 100:
            # Voice is the first item in the dropdown, below the Call button
            pyautogui.click(rect.right - 180, rect.top + 95)
            voice_clicked = True
        time.sleep(0.3)
        if voice_clicked:
            return True, f"Started a voice call with **{contact_name}**."
        return True, f"Opened Call menu for **{contact_name}**. Tap Voice to start the call."
    except Exception as e:
        return True, f"Opened **{contact_name}**. Tap Call then Voice to start the call. ({e})"


def _run_whatsapp_desktop_msg(contact_name: str) -> tuple[bool, str]:
    """Open WhatsApp Desktop and open chat with contact (ready to type message)."""
    return _run_whatsapp_desktop_open_contact(contact_name)


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


# Pending file creation: scope -> {path, content}. User must reply "yes" to confirm (admin only on Discord).
_pending_writes: dict[str, dict] = {}

# Phrases that count as "yes, create the file" (for web and Discord)
_CONFIRM_PHRASES = frozenset({
    "yes", "y", "confirm", "confirmed", "ok", "okay", "do it", "go ahead",
    "create it", "yes please", "sure", "please do", "go", "create",
})


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


def _pending_write_scope_for_web() -> str | None:
    """Return the scope key that has a pending write for the web UI (check both possible keys)."""
    if LINKED_SCOPE and LINKED_SCOPE in _pending_writes:
        return LINKED_SCOPE
    if "web" in _pending_writes:
        return "web"
    return None


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
    "**Luna file access** (only inside Luna projects):\n"
    "• !read <path> — read file\n"
    "• !write <path> <content> — write file\n"
    "• !list [path] — list directory\n"
    "• !edit <path> <old> -> <new> — replace text in file\n"
    "• !news — latest world news headlines\n"
    "• !suno <description> — open Suno and create a song\n"
    "• !local_song <description> — generate local instrumental + lyrics files\n"
    "• !share_song (or !share song) — share a random YouTube channel song to X\n"
    "• !share_facebook (or !share facebook) — share to Facebook\n"
    "• !yt_comment <youtube_url> — transcribe and post a YouTube comment\n"
    "• !ig_dm <username|thread_url> [message] — send an Instagram DM\n"
    "• !remember / !always_remember — store memories\n"
    "• !profile — view or set your profile\n"
    "• !join / !leave — voice; !play / !pause / !skip / !stop / !queue — music\n"
    "• !call <contact> — WhatsApp: open contact and start call (Windows)\n"
    "• !msg <contact> — WhatsApp: open chat with contact (Windows)"
)

# Trigger phrases for command/help intent (natural language → template reply)
_COMMAND_INTENT_TRIGGERS = (
    "what can you do",
    "what do you do",
    "commands",
    "list commands",
    "help",
    "what are your commands",
    "what can luna do",
    "luna commands",
    "what can luna",
    "show commands",
)


def _get_command_intent_reply(msg: str) -> str | None:
    """If the user is asking for commands/help, return the commands template. Else None (no Ollama skip)."""
    if not (msg or msg.strip()):
        return None
    low = msg.strip().lower()
    if any(trigger in low for trigger in _COMMAND_INTENT_TRIGGERS):
        return LUNA_COMMANDS_REPLY
    return None


def _pending_write_entries(pending: dict | None) -> list[dict]:
    """Backward-compatible read for pending single-write or multi-write payloads."""
    if not isinstance(pending, dict):
        return []
    entries = pending.get("writes")
    if isinstance(entries, list):
        out = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            p = (e.get("path") or "").strip()
            c = e.get("content") or ""
            if p:
                out.append({"path": p, "content": c})
        if out:
            return out
    # Legacy shape: {"path": "...", "content": "..."}
    p = (pending.get("path") or "").strip()
    if p:
        return [{"path": p, "content": pending.get("content") or ""}]
    return []


def _handle_web_file_command(msg: str) -> str | None:
    """If msg is a local command, run it and return reply. Else return None."""
    msg = (msg or "").strip()
    if not msg.startswith("!"):
        return None
    parts = msg.split(None, 1)
    cmd = (parts[0] or "").lower()
    args = (parts[1] or "").strip()
    if cmd == "!read":
        if not args:
            return "Usage: !read <path> (e.g. !read snippet.html)"
        ok, result = luna_read_file(args)
        if ok:
            return f"```\n{result}\n```" if len(result) <= 1900 else result[:1897] + "..."
        return f"❌ {result}"
    if cmd == "!write":
        if " " not in args:
            return "Usage: !write <path> <content> (e.g. !write test.txt Hello world)"
        path, content = args.split(None, 1)
        ok, result = luna_write_file(path, content)
        return f"✅ {result}" if ok else f"❌ {result}"
    if cmd == "!list":
        path = args  # can be empty
        ok, result = luna_list_dir(path)
        if ok:
            return f"Luna projects / {path or '.'}\n```\n{result}\n```"
        return f"❌ {result}"
    if cmd == "!edit":
        if " -> " not in args:
            return "Usage: !edit <path> <old_text> -> <new_text>"
        path_and_old, new_text = args.split(" -> ", 1)
        parts_edit = path_and_old.strip().split(None, 1)
        path = parts_edit[0] if parts_edit else ""
        old_text = parts_edit[1] if len(parts_edit) > 1 else ""
        new_text = new_text.strip()
        if not path:
            return "Usage: !edit <path> <old_text> -> <new_text>"
        ok, result = luna_modify_file(path, old_text, new_text)
        return f"✅ {result}" if ok else f"❌ {result}"
    if cmd in ("!files", "!commands"):
        return LUNA_COMMANDS_REPLY
    if cmd == "!news":
        ok, result = _fetch_world_news()
        return result if ok else f"❌ {result}"
    if cmd == "!suno":
        if not args:
            return "Usage: !suno <song description>"
        ok, result = _run_suno_create(args)
        return f"✅ {result}" if ok else f"❌ {result}"
    if cmd in ("!local_song", "!local-song"):
        if not args:
            return "Usage: !local_song <song description>"
        ok, result = create_local_song_project(args)
        return f"✅ Local song package created:\n{result}" if ok else f"❌ {result}"
    if cmd == "!local" and args.lower().startswith("song"):
        prompt = args[4:].strip()
        if not prompt:
            return "Usage: !local song <song description>"
        ok, result = create_local_song_project(prompt)
        return f"✅ Local song package created:\n{result}" if ok else f"❌ {result}"
    if cmd in ("!share_song", "!share-song", "!xshare"):
        ok, result = _run_x_share_random_song()
        return f"✅ {result}" if ok else f"❌ {result}"
    if cmd in ("!share_facebook", "!share-facebook", "!fbshare"):
        ok, result = _run_facebook_share_random_song()
        return f"✅ {result}" if ok else f"❌ {result}"
    if cmd == "!share" and args.lower().startswith("song"):
        ok, result = _run_x_share_random_song()
        return f"✅ {result}" if ok else f"❌ {result}"
    if cmd == "!share" and args.lower().startswith("facebook"):
        ok, result = _run_facebook_share_random_song()
        return f"✅ {result}" if ok else f"❌ {result}"
    if cmd in ("!yt_comment", "!youtube_comment", "!comment_youtube"):
        if not args:
            return "Usage: !yt_comment <youtube_video_url>"
        video_url = _extract_youtube_video_url(args) or args.strip()
        ok, result = _run_youtube_comment(video_url)
        return f"✅ {result}" if ok else f"❌ {result}"
    if cmd in ("!ig_dm", "!instagram_dm", "!igdm"):
        if not args:
            return "Usage: !ig_dm <username|instagram_direct_thread_url> [message]"
        parts = args.split(None, 1)
        target = (parts[0] or "").strip()
        custom_message = (parts[1] or "").strip() if len(parts) > 1 else ""
        ok, result = _run_instagram_dm(target, custom_message)
        return f"✅ {result}" if ok else f"❌ {result}"
    if cmd == "!call":
        if not args:
            return "Usage: !call <contact name or number> (e.g. !call Marios or !call +357 96 724268)"
        ok, result = _run_whatsapp_desktop_call(args)
        return f"✅ {result}" if ok else f"❌ {result}"
    if cmd == "!msg":
        if not args:
            return "Usage: !msg <contact name or number>"
        ok, result = _run_whatsapp_desktop_msg(args)
        return f"✅ {result}" if ok else f"❌ {result}"
    return None


@web_app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json(force=True, silent=True) or {}
    msg = (data.get("message") or "").strip()
    if not msg:
        return jsonify({"error": "Missing message"}), 400
    # Web UI uses linked scope (Chris/Solonaras) so same memory, profile, conversation as Discord
    scope = LINKED_SCOPE if LINKED_SCOPE else "web"
    # Confirmation: user said yes -> execute pending write, then open file in Notepad/default app
    pending_scope = scope if scope in _pending_writes else _pending_write_scope_for_web()
    if _is_confirm_message(msg) and pending_scope is not None:
        pending = _pending_writes.pop(pending_scope)
        entries = _pending_write_entries(pending)
        created_paths = []
        errors = []
        for e in entries:
            ok, result = luna_write_file(e["path"], e["content"])
            if ok:
                created_paths.append(result)
                print(f"[Luna] File written to: {result}", flush=True)
                _open_file_by_path(result)
            else:
                errors.append(f"{e['path']}: {result}")
        if created_paths and not errors:
            if len(created_paths) == 1:
                reply = f"✅ File created at:\n{created_paths[0]}\n(Opened in your default editor.)"
            else:
                preview = "\n".join(created_paths[:6])
                more = f"\n...and {len(created_paths) - 6} more." if len(created_paths) > 6 else ""
                reply = f"✅ Created {len(created_paths)} files:\n{preview}{more}\n(Opened in your default editor.)"
        elif created_paths and errors:
            reply = f"⚠️ Created {len(created_paths)} file(s), but some failed:\n" + "\n".join(errors[:6])
        else:
            reply = "❌ Could not create files:\n" + ("\n".join(errors[:6]) if errors else "No valid file payload found.")
        append_exchange(pending_scope, msg, reply)
        _play_reply_tts_on_pc(reply)
        return jsonify({"reply": reply})
    msg_lower = (msg or "").strip().lower()
    if msg_lower in ("no", "cancel") and (scope in _pending_writes or _pending_write_scope_for_web() is not None):
        pop_scope = scope if scope in _pending_writes else _pending_write_scope_for_web()
        if pop_scope:
            _pending_writes.pop(pop_scope, None)
        reply = "Cancelled."
        append_exchange(scope, msg, reply)
        return jsonify({"reply": reply})
    # Explicit local commands in browser always execute directly.
    file_reply = _handle_web_file_command(msg)
    if file_reply is not None:
        append_exchange(scope, msg, file_reply)
        return jsonify({"reply": file_reply})
    if _extract_news_request(msg):
        ok, result = _fetch_world_news()
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
    # Local song/lyrics generation (fully local files, no Suno)
    local_desc = _extract_local_song_description(msg) if tool_intent else ""
    if tool_intent and local_desc:
        announce = f"Generating local song and lyrics package for: {_suno_preview_text(local_desc)}"
        _play_reply_tts_on_pc(announce)
        ok, result = create_local_song_project(local_desc)
        reply = f"✅ Local song package created:\n{result}" if ok else f"❌ {result}"
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
    command_reply = _get_command_intent_reply(msg)
    if command_reply is not None:
        append_exchange(scope, msg, command_reply)
        _try_capture_memory(scope, msg)
        _try_capture_profile(scope, msg)
        _play_reply_tts_on_pc(command_reply)
        return jsonify({"reply": command_reply})
    # Use persisted conversation history so context survives restarts and page refresh
    history = get_recent_conversation(scope, 20)
    last_assistant = ""
    for h in reversed(history):
        if (h.get("role") or "").lower() == "assistant":
            last_assistant = (h.get("content") or "").strip()
            break
    try_capture_profile_from_reply(scope, last_assistant, msg)
    reply = ollama_chat(msg, LUNA_SYSTEM_PROMPT, scope, history)
    cleaned, writes = _parse_luna_writes(reply)
    if not writes and _user_wants_file_creation(msg):
        writes = _extract_code_blocks_from_reply(reply)
    if not writes and _user_wants_file_creation(msg) and cleaned and len(cleaned.strip()) > 20:
        path = _relatable_note_filename(msg, cleaned)
        writes = [{"path": _normalize_luna_path(path), "content": cleaned.strip()}]
    if writes:
        _pending_writes[scope] = {"writes": writes}
        count = len(writes)
        noun = "file" if count == 1 else "files"
        reply = cleaned + f"\n\nReply **yes** to create {count} {noun} in Luna projects, or **no** to cancel."
    else:
        reply = cleaned
    append_exchange(scope, msg, reply)
    _try_capture_memory(scope, msg)
    _try_capture_profile(scope, msg)
    _play_reply_tts_on_pc(reply)
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


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Ollama: {OLLAMA_BASE} — model: {OLLAMA_MODEL}")
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.listening, name=f"{OLLAMA_MODEL}")
    )


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Reply when mentioned or in DM — use Ollama (same memory, profile, conversation as web; unique per Discord user)
    if isinstance(message.channel, discord.DMChannel) or is_mentioning_luna(message):
        text = (message.content or "").strip()
        # Remove bot mention from text for the prompt
        if bot.user and f"<@{bot.user.id}>" in text:
            text = text.replace(f"<@{bot.user.id}>", "").strip()
        mention = f"<@{message.author.id}>"
        if not text:
            await message.reply(f"Hey {mention}! I'm **Luna**. Say something and I'll answer with my AI brain ({OLLAMA_MODEL}).")
            return
        # Linked user (Chris/Solonaras) shares one scope with web; others get per-server or per-DM scope
        memory_scope = _scope_for_discord_user(message.author.id, message.guild.id if message.guild else None)
        is_discord_admin = message.author.id == _discord_admin_id_int
        text_lower = text.lower().strip()
        # Pending file creation: only admin can confirm on Discord; then we write and open file
        is_confirm = _is_confirm_message(text)
        if is_confirm and memory_scope in _pending_writes:
            if is_discord_admin and _discord_admin_id_int is not None:
                pending = _pending_writes.pop(memory_scope)
                entries = _pending_write_entries(pending)
                created_paths = []
                errors = []
                for e in entries:
                    ok, result = await asyncio.to_thread(luna_write_file, e["path"], e["content"])
                    if ok:
                        created_paths.append(result)
                        print(f"[Luna] File written to: {result}", flush=True)
                        await asyncio.to_thread(_open_file_by_path, result)
                    else:
                        errors.append(f"{e['path']}: {result}")
                if created_paths and not errors:
                    if len(created_paths) == 1:
                        reply = f"✅ File created at:\n{created_paths[0]}\n(Opened in your default editor.)"
                    else:
                        preview = "\n".join(created_paths[:6])
                        more = f"\n...and {len(created_paths) - 6} more." if len(created_paths) > 6 else ""
                        reply = f"✅ Created {len(created_paths)} files:\n{preview}{more}\n(Opened in your default editor.)"
                elif created_paths and errors:
                    reply = f"⚠️ Created {len(created_paths)} file(s), but some failed:\n" + "\n".join(errors[:6])
                else:
                    reply = "❌ Could not create files:\n" + ("\n".join(errors[:6]) if errors else "No valid file payload found.")
            else:
                _pending_writes.pop(memory_scope, None)
                reply = "Only the server admin can create files in Luna projects."
            await asyncio.to_thread(append_exchange, memory_scope, text, reply)
            await message.reply(f"{mention} {reply}")
            return
        if text_lower in ("no", "cancel") and memory_scope in _pending_writes:
            _pending_writes.pop(memory_scope, None)
            reply = "Cancelled."
            await asyncio.to_thread(append_exchange, memory_scope, text, reply)
            await message.reply(f"{mention} {reply}")
            return
        if _extract_news_request(text):
            ok, result = await asyncio.to_thread(_fetch_world_news)
            reply = result if ok else f"❌ {result}"
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
        # Local song/lyrics generation from Discord
        local_desc = _extract_local_song_description(text) if tool_intent else ""
        if tool_intent and local_desc:
            await message.reply(f"{mention} Generating local song package for: {_suno_preview_text(local_desc)}")
            ok, result = await asyncio.to_thread(create_local_song_project, local_desc)
            reply = f"✅ Local song package created:\n{result}" if ok else f"❌ {result}"
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
        command_reply = _get_command_intent_reply(text)
        if command_reply is not None:
            await asyncio.to_thread(append_exchange, memory_scope, text, command_reply)
            await message.reply(f"{mention} {command_reply}")
            return
        # If Luna's last message was a profile question, treat this reply as the answer
        await asyncio.to_thread(try_capture_profile_from_reply, memory_scope, _last_assistant_by_scope.get(memory_scope, ""), text)
        # Load persisted conversation so Luna has context across restarts
        history = await asyncio.to_thread(get_recent_conversation, memory_scope, 20)
        try:
            async with message.channel.typing():
                reply = await asyncio.to_thread(ollama_chat, text, LUNA_SYSTEM_PROMPT, memory_scope, history)
            cleaned, writes = _parse_luna_writes(reply)
            if not writes and _user_wants_file_creation(text):
                writes = _extract_code_blocks_from_reply(reply)
            if not writes and _user_wants_file_creation(text) and cleaned and len(cleaned.strip()) > 20:
                path = _relatable_note_filename(text, cleaned)
                writes = [{"path": _normalize_luna_path(path), "content": cleaned.strip()}]
            if writes:
                if is_discord_admin and _discord_admin_id_int is not None:
                    _pending_writes[memory_scope] = {"writes": writes}
                    count = len(writes)
                    noun = "file" if count == 1 else "files"
                    reply = cleaned + f"\n\nReply **yes** to create {count} {noun} in Luna projects, or **no** to cancel."
                else:
                    reply = cleaned + "\n\nOnly the server admin can create files in Luna projects."
            else:
                reply = cleaned
            await asyncio.to_thread(append_exchange, memory_scope, text, reply)
            _last_assistant_by_scope[memory_scope] = reply
            await asyncio.to_thread(_try_capture_memory, memory_scope, text)
            await asyncio.to_thread(_try_capture_profile, memory_scope, text)
            if len(reply) > 1900:
                reply = reply[:1897] + "..."
            await message.reply(f"{mention} {reply}")
            # If Luna is in a voice channel in this guild, speak the reply with gTTS (no code read aloud).
            if message.guild:
                vc = next((c for c in bot.voice_clients if c.guild == message.guild and c.is_connected()), None)
                if vc and not vc.is_playing():
                    reply_clean = _reply_text_for_tts(reply)
                    if reply_clean and len(reply_clean) <= 500:
                        mp3_bytes = await _get_tts_for_discord(reply_clean)
                        if mp3_bytes:
                            try:
                                source = discord.FFmpegPCMAudio(io.BytesIO(mp3_bytes), pipe=True)
                                vc.play(source)
                            except Exception as e:
                                print(f"Discord voice play: {e}")
                        elif reply_clean:
                            print("Discord voice TTS: gTTS produced no audio.")
        except discord.HTTPException as e:
            print(f"Failed to reply: {e}")
        except Exception as e:
            await message.reply(f"{mention} Error: {e}")

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


@bot.command(name="read")
async def cmd_read(ctx: commands.Context, *, path: str):
    """Read a file from Luna projects. Example: !read snippet.html (admin only)"""
    if not _is_discord_file_admin(ctx):
        await ctx.reply("Only the server admin can use file commands.")
        return
    path = path.strip()
    ok, result = await asyncio.to_thread(luna_read_file, path)
    if ok:
        if len(result) > 1900:
            result = result[:1897] + "..."
        await ctx.reply(f"```\n{result}\n```")
    else:
        await ctx.reply(f"❌ {result}")


@bot.command(name="write")
async def cmd_write(ctx: commands.Context, *, args: str):
    """Write content to a file in Luna projects. Example: !write test.txt Hello world (admin only)"""
    if not _is_discord_file_admin(ctx):
        await ctx.reply("Only the server admin can use file commands.")
        return
    if " " not in args:
        await ctx.reply("Usage: `!write <path> <content>` (path is relative to Luna projects)")
        return
    path, content = args.strip().split(None, 1)
    ok, result = await asyncio.to_thread(luna_write_file, path, content)
    if ok:
        await ctx.reply(f"✅ {result}")
    else:
        await ctx.reply(f"❌ {result}")


@bot.command(name="list")
async def cmd_list(ctx: commands.Context, path: str = ""):
    """List files in Luna projects. Example: !list or !list subfolder (admin only)"""
    if not _is_discord_file_admin(ctx):
        await ctx.reply("Only the server admin can use file commands.")
        return
    ok, result = await asyncio.to_thread(luna_list_dir, path)
    if ok:
        await ctx.reply(f"Luna projects / {path or '.'}\n```\n{result}\n```")
    else:
        await ctx.reply(f"❌ {result}")


@bot.command(name="edit")
async def cmd_edit(ctx: commands.Context, *, args: str):
    """Replace old with new in a file. Example: !edit snippet.html old text -> new text (admin only)"""
    if not _is_discord_file_admin(ctx):
        await ctx.reply("Only the server admin can use file commands.")
        return
    if " -> " not in args:
        await ctx.reply("Usage: `!edit <path> <old_text> -> <new_text>` (path relative to Luna projects)")
        return
    path_and_old, new_text = args.strip().split(" -> ", 1)
    parts = path_and_old.strip().split(None, 1)
    path = parts[0] if parts else ""
    old_text = parts[1] if len(parts) > 1 else ""
    new_text = new_text.strip()
    if not path:
        await ctx.reply("Usage: `!edit <path> <old_text> -> <new_text>`")
        return
    ok, result = await asyncio.to_thread(luna_modify_file, path, old_text, new_text)
    if ok:
        await ctx.reply(f"✅ {result}")
    else:
        await ctx.reply(f"❌ {result}")


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
    ok, result = await asyncio.to_thread(_fetch_world_news)
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


@bot.command(name="local_song")
async def cmd_local_song(ctx: commands.Context, *, description: str = ""):
    """Generate local instrumental + lyrics package in Luna projects/music_local."""
    prompt = (description or "").strip()
    if not prompt:
        await ctx.reply("Usage: `!local_song <song description>`")
        return
    await ctx.reply("Generating local song package...")
    ok, result = await asyncio.to_thread(create_local_song_project, prompt)
    await ctx.reply(f"{'✅ Local song package created:' if ok else '❌'}\n{result}")


@bot.command(name="share_song")
async def cmd_share_song(ctx: commands.Context):
    """Share a random song from your configured YouTube channel to X (linked/admin only)."""
    if not _can_use_x_share_discord(ctx.author.id):
        await ctx.reply("Only the linked user/admin can use X sharing automation.")
        return
    await ctx.reply("Picking a random song from your YouTube channel and sharing it to X...")
    ok, result = await asyncio.to_thread(_run_x_share_random_song)
    await ctx.reply(f"{'✅' if ok else '❌'} {result}")


@bot.command(name="share_facebook")
async def cmd_share_facebook(ctx: commands.Context):
    """Share a random song from your configured YouTube channel to Facebook (linked/admin only)."""
    if not _can_use_x_share_discord(ctx.author.id):
        await ctx.reply("Only the linked user/admin can use Facebook sharing automation.")
        return
    await ctx.reply("Picking a random song from your YouTube channel and sharing it to Facebook...")
    ok, result = await asyncio.to_thread(_run_facebook_share_random_song)
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
    """Play YouTube audio in voice channel. Usage: !play <url or search>"""
    if not ctx.guild:
        await ctx.reply("Music playback is only available in a server voice channel.")
        return
    q = (query or "").strip()
    if not q:
        await ctx.reply("Usage: `!play <youtube url or search terms>`")
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

    await ctx.reply(f"Searching YouTube for: `{q}` ...")
    ok, result = await asyncio.to_thread(_resolve_youtube_track, q)
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
    """Open WhatsApp Desktop, select contact, and start a call (phone icon). Usage: !call <contact name>"""
    if not _can_use_whatsapp_discord(ctx.author.id):
        await ctx.reply("Only the linked user or admin can use WhatsApp automation.")
        return
    contact = (contact or "").strip()
    if not contact:
        await ctx.reply("Usage: `!call <contact name>` (e.g. !call Marios)")
        return
    await ctx.reply(f"Opening WhatsApp and starting a call with **{contact}**…")
    ok, result = await asyncio.to_thread(_run_whatsapp_desktop_call, contact)
    await ctx.reply(result if ok else f"❌ {result}")


@bot.command(name="msg")
async def cmd_whatsapp_msg(ctx: commands.Context, *, contact: str = ""):
    """Open WhatsApp Desktop and open chat with contact. Usage: !msg <contact name>"""
    if not _can_use_whatsapp_discord(ctx.author.id):
        await ctx.reply("Only the linked user or admin can use WhatsApp automation.")
        return
    contact = (contact or "").strip()
    if not contact:
        await ctx.reply("Usage: `!msg <contact name>` (e.g. !msg Marios)")
        return
    await ctx.reply(f"Opening WhatsApp and chat with **{contact}**…")
    ok, result = await asyncio.to_thread(_run_whatsapp_desktop_msg, contact)
    await ctx.reply(result if ok else f"❌ {result}")


def main():
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
