# Luna Agent 4.0

Luna is a multi-platform AI companion with Discord + web chat, persistent memory, voice interaction, secure project file tools, social/browser automation, and media creation.

## Updates

**Web UI — Gyroscope Rings (v4.0.1)**

- **Center graphic** — Replaced the original spinning circles with a gyroscope-style 3D ring display
- **Dynamic ring count** — Slider control (2–12 rings) underneath the Luna title; rings are added/removed in real time
- **Degree-based orientation** — Each ring is placed at evenly spaced degree markers (360° ÷ N × index) and spins around its own axis
- **Hollow rings** — Rings rendered as hoops (radial mask) instead of solid discs
- **Seamless animation** — Continuous spin with no visible reset at loop boundaries
- **Compact size** — Center gyroscope scaled to 22.5vmin / 112.5px
- **3D depth** — Perspective, backface visibility, and per-ring spin for a gyroscope/gimbal effect

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
   - Click "New Application" → enable "Message Content Intent" under Privileged Gateway Intents
   - Go to Bot → "Add Bot" → Copy token

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   python -m playwright install chromium
   ```

3. **[AMD GPU Setup (ROCm)](#rocm-amd-gpu-setup)** — *Optional if you have an AMD GPU*
   - Recent Ollama versions auto-detect ROCm
   - See detailed guide below if manual setup needed

4. Create your env file:
   ```bash
   copy .env.example .env
   ```
   - Set `DISCORD_TOKEN=your_token_here`
   - (Optional) Configure ROCm if using AMD GPU

5. Install Ollama and pull a model:
   ```bash
   # Download and install from https://ollama.ai
   ollama pull llama3.2:latest
   ```

6. Run:
   ```bash
   python bot.py
   ```
   - Bot connects to Discord and starts web UI at `http://127.0.0.1:5050`

---

## ROCm (AMD GPU) Setup

Luna uses **Ollama**, which has native ROCm support for AMD GPUs. GPU acceleration makes inference **10-40x faster**.

### System Requirements

- **AMD GPU**: RX 5000 series or newer (with RDNA, RDNA2, RDNA3, or CDNA architecture)
  - Not compatible with older GCN-based cards (R9 Fury, RX 480/580, etc.)
- **Windows 11/10** (64-bit) or Linux
- **ROCm** runtime (usually bundled with Ollama)

### Installation Steps

#### Windows — Quick Path

1. **Download Ollama with ROCm**: https://ollama.ai/download
   - Windows installer automatically includes ROCm support
   - Verify: Run `ollama` in terminal → should initialize smoothly

2. **Open command prompt and pull a model**:
   ```bash
   ollama pull llama3.2:latest
   ```
   
3. **Verify GPU is detected**:
   - In your bot's terminal, you should see messages like:
     ```
     Loading model 'llama3.2:latest'
     [GPU] gfx90c (VRAM: 12345 MB)
     ```
   - If you see "CPU only" or no GPU line, see troubleshooting below

#### Windows — Manual GPU Detection

If Ollama doesn't auto-detect your AMD GPU:

1. **Find your GPU's architecture**:
   - Open Device Manager → GPU name
   - Use the [AMD GPU architecture table](#amd-gpu-architectures) below

2. **Set `HSA_OVERRIDE_GFX_VERSION` in `.env`**:
   ```ini
   HSA_OVERRIDE_GFX_VERSION=10.3.0
   ```
   - Replace with your GPU's GFX version from the table below

3. **Restart Ollama** and verify in terminal

### AMD GPU Architectures

Use this table to find your GPU's GFX version:

| Architecture | GPU Series | Example Cards | GFX Version |
|---|---|---|---|
| **RDNA** | RX 5000 | 5700 XT, 5600 XT | 10.1.0 |
| **RDNA2** | RX 6000 | 6900 XT, 6700 XT, 6600 | 10.3.0 |
| **RDNA3** | RX 7000 | 7900 XTX, 7800 XT, 7600 | 11.0.0 |
| **RDNA3** | RX 7900 GRE (OEM) | — | 11.0.1 |
| **RDNA4** | RX 9000/9070 | 9070 XT, 9070, 9050 | 12.0.0 |
| **CDNA** | MI100/MI210 (Data Center) | — | 11.0.0 |

#### Linux — ROCm Setup

1. **Install ROCm runtime**:
   ```bash
   wget -q -O - https://repo.radeon.com/rocm/rocm.gpg.key | sudo apt-key add -
   echo "deb [arch=amd64] https://repo.radeon.com/rocm/apt/debian focal main" | sudo tee /etc/apt/sources.list.d/rocm.list
   sudo apt update && sudo apt install -y rocm-hip-runtime rocm-opencl-runtime
   ```

2. **Add your user to `video` group**:
   ```bash
   sudo usermod -a -G video $USER
   sudo usermod -a -G render $USER
   # Log out and back in for changes to take effect
   ```

3. **Install Ollama**: https://ollama.ai/download/linux

4. **Verify GPU**:
   ```bash
   rocm-smi
   ```

### Troubleshooting

#### "failed to retrieve device list" or GPU not detected

1. **Verify GPU is present**:
   - Windows: Device Manager → Display adapters
   - Linux: `rocm-smi` or `lspci | grep AMD`

2. **Check Ollama version** (must be recent):
   ```bash
   ollama --version  # Should be 0.1.40+
   ```

3. **Try setting GFX version manually**:
   ```ini
   # In .env
   HSA_OVERRIDE_GFX_VERSION=10.3.0  # Use your GPU's version
   ```

4. **Force ROCm backend**:
   ```ini
   OLLAMA_LLM_LIBRARY=rocm
   ```

5. **Restart Ollama service**:
   ```bash
   # Windows
   Restart-Service OllamaService  # PowerShell
   ```

#### Inference is slow (not using GPU)

1. **Check bot terminal for GPU initialization**:
   - Look for messages containing `[GPU]` or the GPU name
   - Absence means CPU fallback (slow)

2. **Verify Ollama is using ROCm**:
   ```bash
   ollama serve  # Run manually to see detailed output
   ```

3. **Check available VRAM**:
   - Model must fit in GPU memory
   - `llama3.2:1b` ≈ 2 GB VRAM needed
   - `llama3.2:7b` ≈ 8 GB VRAM needed

#### Multi-GPU Setup

To use multiple AMD GPUs:

1. **List available devices**:
   ```bash
   rocm-smi
   ```

2. **Pin to specific GPU in `.env`**:
   ```ini
   HSA_VISIBLE_DEVICES=0,1  # Use GPUs 0 and 1
   ```
   Ollama will load-balance models across them.

---

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
