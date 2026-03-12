# Luna Agent 4.0

Luna is a personal AI companion and automation assistant: Discord bot, web chat UI, persistent memory, voice (TTS), sandboxed file tools, social and browser automation, scheduled posts, and editable identity (SOUL, TOOLS, OBJECTIVES) — all driven by a local Ollama model.

## Features

### Chat & memory
- **Discord + web UI** — Same conversational Luna on both; linked user shares memory and profile across platforms.
- **4-layer memory** — Core, long-term, short-term, and working memory; survives restarts.
- **Per-user profiles** — Luna gathers and remembers name, preferences, and facts; she can also ask what to put in SOUL.md, TOOLS.md, and OBJECTIVES.md and save your reply (like profile).
- **Context compaction** — Long conversations are summarized so the context window stays manageable (OpenClaw-style).
### Voice & media
- **TTS** — Server-side speech on the host and in Discord voice channels (gTTS); optional auto-join and speak in configured text channels.
- **Discord voice** — `!join` / `!leave`; Luna speaks replies in VC when invited. YouTube music: `!play`, `!pause`, `!resume`, `!skip`, `!queue`, `!stop`.

### Files & execution
- **Luna projects** — Read, write, edit, list only inside the project sandbox; file creation requires confirmation.
- **Run scripts** — `!run <path>` runs a `.py` script in Luna projects after you confirm.

### Identity & skills
- **SOUL.md, TOOLS.md, OBJECTIVES.md** — Loaded from `data/` and injected into the system prompt. Edit them directly or tell Luna to set them; she asks what to put in each and saves your next message to the file.
- **Skills** — Any `.md` in `data/skills/` is injected as a skill Luna follows when relevant (no code changes to add new behaviors).

### Automation (browser)
- **Suno** — Song creation via description; first login in browser, then automated.
- **X (Twitter) & Facebook** — Share a random song from a configured YouTube channel; **George** (Shadow's sub-agent) schedules X at 10:00 & 18:00 and Facebook at 11:00 & 19:00 (local time).
- **YouTube** — Comment on videos/Shorts (transcribe or use title/description, then post).
- **Instagram** — DM by username or direct thread URL.
- **WhatsApp** — Desktop call flow (e.g. `!call <contact>`) and Web messaging (`!msg <contact> [context]`).
- **Facebook Messenger** — Message by name/username.
- **News** — Fetch and show latest world news on request.
- **Search** — Open Google, fetch results, and recommend the best link (Ollama).

### Tools & control
- **Mistake analysis** — Ask “why did you make a mistake?” for an explanation; “retry and find the solution” triggers multi-strategy retry in one session. Successful fixes are stored and preferred next time.
- **Objectives** — `data/OBJECTIVES.md` defines rules Luna must follow; she can ask you what to put there and record it.

### Security & control
- **Sandbox** — File and run operations only under Luna projects (and allowed `data/` paths).
- **Confirmation** — File creation, “do” actions, and script runs require explicit yes.
- **Admin / pairing** — Discord admin and linked user for sensitive actions; configurable via `.env`.

## Quick start

1. **Ollama** — Install [Ollama](https://ollama.ai) and pull a model, e.g.:
   ```bash
   ollama pull qwen2.5-coder:7b-instruct
   ```
2. **Discord bot** — Create an app in the [Discord Developer Portal](https://discord.com/developers/applications), create a bot, enable **Message Content Intent**, and copy the token.
3. **Dependencies**:
   ```bash
   pip install -r requirements.txt
   python -m playwright install chromium
   ```
   FFmpeg in PATH is needed for TTS and music playback.
4. **Config** — Copy `.env.example` to `.env` and set at least `DISCORD_TOKEN`. Optionally set `OLLAMA_MODEL`, `GEORGE_SCHEDULE_X_TIMES` / `GEORGE_SCHEDULE_FACEBOOK_TIMES`, and other variables (see `.env.example`).
5. **Run**:
   ```bash
   python bot.py
   ```
   The web UI opens in the default browser; the Discord bot connects with the token.

## Project layout

- `bot.py` — Main entry: Discord bot, Flask web app, Ollama chat, TTS, automation, scheduler, memory, and identity injection.
- `data/` — SOUL.md, TOOLS.md, OBJECTIVES.md, skills (`.md`), memory and profile storage.
- `Luna projects/` — Sandbox for file read/write/edit and run scripts.
- `static/` — Web UI (Jarvis-style chat).
- `luna_conversation.py`, `luna_memory.py`, `luna_files.py` — Conversation history, memory layers, and sandboxed file access.

## Configuration

- **Discord** — `DISCORD_TOKEN`, optional `DISCORD_ADMIN_ID`, `LINKED_DISCORD_USER_ID`, `DISCORD_TTS_CHANNEL_IDS` (auto-join voice in those text channels).
- **Ollama** — `OLLAMA_BASE_URL`, `OLLAMA_MODEL` (default `qwen2.5-coder:7b-instruct`).
- **George (scheduler)** — `george.py` runs share-to-X and share-to-Facebook at set times (default X 10:00 & 18:00, Facebook 11:00 & 19:00); override with `GEORGE_SCHEDULE_X_TIMES` and `GEORGE_SCHEDULE_FACEBOOK_TIMES`.
- **Identity** — Edit `data/SOUL.md`, `data/TOOLS.md`, `data/OBJECTIVES.md` or ask Luna to set them via chat.

## License

See repository license file.
