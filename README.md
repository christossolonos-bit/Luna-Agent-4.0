# Luna — Discord chatbot (Python)

A simple Discord chatbot named Luna. She responds when you mention her or DM her.

## Setup

1. **Create a bot in the [Discord Developer Portal](https://discord.com/developers/applications)**
   - New Application → name it (e.g. Luna)
   - Go to **Bot** → **Reset Token** and copy the token
   - Enable **Message Content Intent** under Privileged Gateway Intents (required for reading messages)
   - Under **OAuth2 → URL Generator**, select scopes `bot` and permissions you need (Send Messages, Read Message History, etc.), then invite the bot to your server

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Add your token and (optional) Discord admin**
   - Copy `.env.example` to `.env`
   - Put your token in `.env`: `DISCORD_TOKEN=your_token_here`
   - To allow only yourself to create/edit files on Discord, add `DISCORD_ADMIN_ID=your_discord_user_id` (right‑click your username → Copy ID when Developer Mode is on)
   - To link the web UI to your Discord identity (same memory, profile, conversation on both), set `LINKED_DISCORD_USER_ID=your_discord_user_id`. Default is `1414944231222411378` (Chris/Solonaras).

4. **Run Luna**
   ```bash
   python bot.py
   ```

You should see `Logged in as Luna#1234`. Mention the bot in a channel or DM her to get a reply.

## Web UI and voice

- **Web UI**: After starting the bot, open http://127.0.0.1:5050 in your browser to chat with Luna (Jarvis-style UI). The bot opens the UI in your default browser automatically (e.g. Brave if it’s set as default).
- **Voice (TTS)**: Luna uses **Edge TTS (Ava)** on your PC. **Web (Brave, etc.)**: TTS is played on the PC where the bot runs (via ffplay), not in the browser—you hear Luna from the machine running `bot.py`. **Discord**: Use `!join` to have Luna join a voice channel; when you mention her and she replies, she speaks the reply in the channel using the same Edge TTS. FFmpeg (and ffplay) must be on PATH for PC playback.

### Discord voice (FFmpeg)

For Luna to speak in a Discord voice channel, **FFmpeg** must be installed and on your system PATH. If it's missing, voice in Discord will fail (web TTS is unaffected).

- Windows: Download from https://ffmpeg.org/download.html (e.g. the "Windows builds" gyan.dev release), extract, and add the `bin` folder to your PATH.

## Memory (4 layers)

Luna uses **4 layers of memory** per user (and per Discord server). Stored in `data/luna_memory.json`.

- **Layer 1 – Core**: Essential facts (name, “always remember”). Max 10, always in prompt first. Auto: *"my name is …"* → core. Say *"always remember that …"* or use `!always_remember <fact>`.
- **Layer 2 – Long-term**: Stored facts over time. Auto: *"remember that …"*, *"I like …"*. Use `!remember <fact>`.
- **Layer 3 – Short-term**: Last 5 memories added (recent). Same store as long-term; shown as “recently mentioned.”
- **Layer 4 – Working**: Current conversation (last messages). **Persisted** in `data/luna_conversations.json` so Luna keeps context across app restarts and page refreshes (last 20 messages per scope).

**Discord**: Same 4-layer memory as web; **each Discord user has their own** (per server and per DM). Commands: `!remember`, `!always_remember`, `!memories`, `!forget`, `!forget_all`. **Web**: Single scope `web`; same auto-capture.

### User profile (permanent)

Luna keeps a **permanent user profile** (name, location, occupation, interests, birthday, other) in `data/luna_profile.json`. She is instructed to ask for missing fields; answers are stored when the user replies to her questions or says things like *"my name is …"*, *"I live in …"*, *"I work as …"*.

- **Discord**: **Each user has their own profile** (per server and per DM). Luna is told to treat each user separately and ask for name/details when missing. `!profile` to show, `!profile set <field> <value>` to set, `!profile clear` to clear.
- **Web**: Same auto-capture and question→answer capture; profile scope is `web`.

### File creation (confirm first; Discord admin only)

When you ask Luna to create or save a file, she repeats the request and asks for permission. Reply **yes** to create the file in Luna projects, or **no** to cancel. On **Discord**, only the user whose ID is set as `DISCORD_ADMIN_ID` in `.env` can create or edit files (they can use `!read`, `!write`, `!list`, `!edit` and confirm Luna’s file creation). Other users can chat and use Luna normally but get no file execution.

### Persistent conversation (context across restarts)

Conversation history is saved per user/scope in `data/luna_conversations.json` (last 200 messages per scope). When you restart the app or refresh the web UI, Luna still has the last 20 messages as context so she doesn’t forget the recent conversation.
