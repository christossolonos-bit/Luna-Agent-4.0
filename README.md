# Luna Agent 4.0

Luna is a personal AI companion with **two modes**: **Luna** (chat) and **Shadow** (commands and code). She runs as a Discord bot and a web chat UI, with persistent memory, voice (TTS), browser automation, reminders (Discord DM + voice note), and code generation into `Luna projects/agents/`. All powered by local Ollama models.

## Two modes

| Mode   | Model (config)              | Use |
|--------|-----------------------------|-----|
| **Luna**  | `OLLAMA_CHAT_MODEL` (default `llama3.2:latest`) | Normal conversation. Chat with her; she remembers you (4-layer memory, profile, goals). |
| **Shadow** | `OLLAMA_MODEL` (default `qwen2.5-coder:7b-instruct`) | Commands and code. Say **Shadow, &lt;what to do&gt;** — news, search, share to X/Facebook, create a script, remind me at 7pm, etc. |

- **Chat:** Talk to Luna normally; she replies with the chat model (Llama).
- **Commands:** Say **Shadow, news**, **Shadow, create a script that fetches the weather**, **Shadow, remind me at 7pm to take my medicine**, or use **!help** for the full list.

## Features

### Chat & memory (Luna)
- **Discord + web UI** — Same Luna on both; linked user shares memory and profile.
- **4-layer memory** — Core, long-term, short-term, working; survives restarts.
- **Luna brain** — Digital neuron layer gates what gets stored; she can learn from conversation without explicit “remember” phrases.
- **Per-user profile** — Name, preferences, goals; she can also save your SOUL/TOOLS/OBJECTIVES via chat.

### Commands & code (Shadow)
- **Create code** — “Shadow, create a script that …” → Qwen generates Python, saved to `Luna projects/agents/*.py` and opened in Notepad.
- **Reminders** — “Remind me at 7pm to take my medicine” → at that time you get a **Discord DM** with text + **voice note** (TTS). One-shot or daily (e.g. `Luna projects/agents/dailymedreminder.py` registers a daily 7pm medicine reminder).
- **Retry** — If a command failed, say **retry**; Luna retries with different strategies (e.g. longer waits) up to twice.
- **News, search, Suno, X, Facebook, YouTube comment, Instagram DM, Messenger, WhatsApp** — Same automation as before; no scheduled George.

### Voice & media
- **TTS** — Server-side speech (gTTS) and in Discord voice channels.
- **Discord voice** — `!join` / `!leave`; `!play` / `!pause` / `!skip` / `!stop` / `!queue` for music.

### Identity
- **SOUL.md, TOOLS.md, OBJECTIVES.md** — In `data/`; you can edit them or tell Luna to set them (she saves your next message to the file).
- **Skills** — `.md` files in `data/skills/` are injected when relevant.

### Security
- **Linked user** — `LINKED_DISCORD_USER_ID` in `.env`; that user gets reminders, create-code, and automation on Discord. Admin can be set via `DISCORD_ADMIN_ID`.

## Quick start

1. **Ollama** — Install [Ollama](https://ollama.ai) and pull both models:
   ```bash
   ollama pull llama3.2:latest
   ollama pull qwen2.5-coder:7b-instruct
   ```
2. **Discord bot** — [Discord Developer Portal](https://discord.com/developers/applications) → create app → Bot → enable **Message Content Intent** → copy token.
3. **Dependencies**:
   ```bash
   pip install -r requirements.txt
   python -m playwright install chromium
   ```
   FFmpeg in PATH for TTS and music.
4. **Config** — Copy `.env.example` to `.env`. Set at least:
   - `DISCORD_TOKEN`
   - `LINKED_DISCORD_USER_ID` (your Discord user ID) for reminders and DMs.
   Optional: `OLLAMA_CHAT_MODEL`, `OLLAMA_MODEL`, `OLLAMA_BASE_URL` (see `.env.example`).
5. **Run**:
   ```bash
   python bot.py
   ```
   Web UI opens in the browser; Discord bot connects.

## Project layout

- `bot.py` — Main entry: Discord bot, Flask web app, Luna chat (Llama), Shadow commands (Qwen), TTS, reminders, automation, memory.
- `data/` — SOUL.md, TOOLS.md, OBJECTIVES.md, skills, memory, profile, reminders (e.g. `reminders.json`).
- `Luna projects/agents/` — Generated and helper scripts (e.g. `dailymedreminder.py`). “Shadow, create a script that …” writes `.py` here.
- `static/` — Web chat UI.
- `luna_memory.py`, `luna_brain.py`, `luna_conversation.py`, `luna_profile.py`, `luna_files.py` — Memory, brain, conversation, profile, and agents file writes.

## Configuration

- **Discord** — `DISCORD_TOKEN`, `LINKED_DISCORD_USER_ID` (for reminders and automation), optional `DISCORD_ADMIN_ID`, `DISCORD_TTS_CHANNEL_IDS`.
- **Ollama** — `OLLAMA_BASE_URL`, `OLLAMA_CHAT_MODEL` (Luna chat, default `llama3.2:latest`), `OLLAMA_MODEL` (Shadow, default `qwen2.5-coder:7b-instruct`).
- **Identity** — Edit `data/SOUL.md`, `data/TOOLS.md`, `data/OBJECTIVES.md` or ask Luna to set them.

## License

See the repository license file.
