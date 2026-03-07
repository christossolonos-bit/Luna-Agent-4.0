# Luna Agent 4.0

Luna is a multi-platform AI companion with Discord + web chat, persistent memory, voice interaction, secure project file tools, social/browser automation, and media creation.

## Current Features

- Discord bot + web chat UI with shared conversational behavior
- Persistent 4-layer memory and per-user profile memory
- Server-side TTS replies and Discord voice-channel speech
- Sandboxed file read/write/edit/list operations inside `Luna projects`
- Confirmation-based file creation flow
- Suno automation for song creation
- X (Twitter) sharing automation from YouTube channel picks
- Facebook sharing automation with composer flow support
- YouTube video/Shorts transcript-aware commenting automation (with fallback when captions are limited)
- Instagram DM automation by username or direct thread link
- Local music generation pipeline (instrumental + lyrics + synced output mix)
- Discord YouTube music player with queue controls

## Quick Start

1. Create a Discord bot in the [Discord Developer Portal](https://discord.com/developers/applications).
2. Enable required intents (including Message Content Intent).
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   python -m playwright install chromium
   ```
4. Create your env file:
   - Copy `.env.example` to `.env`
   - Set `DISCORD_TOKEN=your_token_here`
5. Run:
   ```bash
   python bot.py
   ```

## Memory System

Luna maintains long-lived context with four layers:

- Core memory (critical facts)
- Long-term memory
- Short-term memory (recent focus)
- Working memory (conversation context)

Profile data is persisted by user scope and reused across future sessions for personalized conversations.

## Voice, Media, and Playback

- TTS plays on the runtime machine and in Discord voice channels
- FFmpeg/ffplay should be installed and available in PATH
- Discord music player supports:
  - `!join`, `!leave`
  - `!play <url or search>`
  - `!pause`, `!resume`, `!skip`, `!queue`, `!stop`

## Automation Commands (Examples)

- `!suno <description>` - create a Suno song
- `!share_song` - share a random channel song to X
- `!share_facebook` - share a random channel song to Facebook
- `!yt_comment <youtube_url>` - generate and post a YouTube comment
- `!ig_dm <username|instagram_direct_thread_url> [message]` - send Instagram DM
- `!local_song <description>` - generate local song package

## Security Model

- File operations are restricted to the local project sandbox.
- Sensitive actions use explicit confirmation and permission checks.
- Browser automations use persistent profiles with first-login bootstrap flow for safer account reuse.
