# Luna Agent 4.0

Luna is a conversational AI companion with Discord + web chat, persistent memory, voice replies, secure local file access, and browser automation workflows.

## Highlights

- Multi-platform chat on Discord and a local web interface
- Persistent 4-layer memory with user profile awareness
- Text-to-speech replies on desktop and Discord voice
- Safe file operations inside `Luna projects`
- Automation flows for Suno, X, and Facebook using Playwright
- Local song generation with lyrics, sync timing, and final mix output

## Quick Start

1. Create a Discord bot in the [Discord Developer Portal](https://discord.com/developers/applications)
2. Enable required bot intents (including Message Content Intent)
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Create your env file:
   - Copy `.env.example` to `.env`
   - Set `DISCORD_TOKEN=your_token_here`
5. Run:
   ```bash
   python bot.py
   ```

## Memory and Profile

Luna keeps long-lived context across sessions:

- Core memory (important facts)
- Long-term memory
- Short-term memory (recent focus)
- Working memory (recent conversation history)

Profile data is stored per user scope and reused in future conversations for more personalized responses.

## Voice and Media

- TTS playback is handled by the runtime machine
- Discord voice replies are supported when Luna is connected to a voice channel
- FFmpeg should be installed and available in PATH for media operations

## Automation

Luna supports guided automation tasks such as:

- Creating songs in Suno from prompts
- Sharing channel songs to X
- Sharing channel songs to Facebook
- Generating fully local songs with synced lyrics and mixed output files

## Security

Luna is designed to keep file operations restricted to the project sandbox and confirmation-based for sensitive actions.
