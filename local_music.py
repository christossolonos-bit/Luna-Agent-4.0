"""
Local Suno-like prototype for Luna.

Generates:
- 3-minute arranged instrumental WAV
- lyrics text (Ollama-generated with fallback template)
- spoken vocal track from lyrics
- final mixed song WAV for easy playback
- metadata JSON
inside Luna projects/music_local/<timestamp>_<slug>/
"""
from __future__ import annotations

import json
import math
import os
import random
import struct
import subprocess
import tempfile
import urllib.error
import urllib.request
import wave
from datetime import datetime
from pathlib import Path

from luna_files import ALLOWED_BASE


def _slug(text: str, max_len: int = 36) -> str:
    s = "".join(c for c in (text or "") if c.isalnum() or c in (" ", "-", "_")).strip().lower()
    s = "_".join(s.split())
    return (s[:max_len] or "track").strip("_")


def _pick_style(desc: str) -> tuple[str, int, bool]:
    t = (desc or "").lower()
    if any(k in t for k in ("trap", "drill", "club", "dance", "edm")):
        return "electronic", random.randint(118, 145), True
    if any(k in t for k in ("rock", "metal", "punk")):
        return "rock", random.randint(118, 168), True
    if any(k in t for k in ("cinematic", "ambient", "lofi", "chill", "sad", "dark")):
        return "cinematic", random.randint(72, 102), True
    return "pop", random.randint(90, 124), False


def _midi_to_hz(midi: float) -> float:
    return 440.0 * (2.0 ** ((midi - 69.0) / 12.0))


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _adsr(pos: float, length: float, a: float, d: float, s: float, r: float) -> float:
    if pos < 0.0 or pos > length:
        return 0.0
    if pos < a:
        return pos / max(a, 1e-9)
    if pos < a + d:
        k = (pos - a) / max(d, 1e-9)
        return 1.0 + (s - 1.0) * k
    if pos < max(length - r, 0.0):
        return s
    k = (pos - max(length - r, 0.0)) / max(r, 1e-9)
    return s * (1.0 - _clamp(k, 0.0, 1.0))


def _generate_lyrics_fallback(description: str, style: str) -> str:
    seed_words = [w.strip(".,!?") for w in (description or "").split() if len(w.strip(".,!?")) >= 4][:5]
    core = ", ".join(seed_words) if seed_words else "light, motion, memory"
    return (
        "[Verse 1]\n"
        f"Neon thoughts in a {style} sky,\n"
        f"We draw our names where signals fly.\n"
        f"Echoes of {core} in the air,\n"
        "A quiet fire everywhere.\n\n"
        "[Chorus]\n"
        "Hold this moment, don't let go,\n"
        "Turn the silence into glow.\n"
        "If you hear me, sing it strong,\n"
        "We are sparks inside this song.\n\n"
        "[Verse 2]\n"
        "Footsteps sync with midnight drums,\n"
        "Every heartbeat softly hums.\n"
        "Write tomorrow in one line,\n"
        "Your voice and mine, your voice and mine.\n"
    )


def _generate_lyrics_with_ollama(description: str, style: str, bpm: int) -> tuple[bool, str]:
    """
    Generate lyrics with local Ollama chat API.
    Returns (ok, lyrics_text).
    """
    base = (os.environ.get("OLLAMA_BASE_URL") or "http://127.0.0.1:11434").rstrip("/")
    model = (os.environ.get("OLLAMA_MODEL") or "llama3.2:latest").strip()
    prompt = (
        "Write full song lyrics in English for this prompt:\n"
        f"Prompt: {description}\n"
        f"Style: {style}\n"
        f"BPM: {bpm}\n\n"
        "Requirements:\n"
        "- Structure: [Verse 1], [Chorus], [Verse 2], [Chorus], [Bridge], [Final Chorus]\n"
        "- 4 lines per section\n"
        "- Memorable hooks, clean language\n"
        "- Return ONLY the lyrics text with section headers."
    )
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        text = ((data.get("message") or {}).get("content") or "").strip()
        if not text:
            return False, "Empty lyrics from Ollama."
        t_low = text.lower()
        bad_markers = (
            "i cannot",
            "i can't",
            "can i help you with something else",
            "as an ai",
            "i'm unable",
            "i am unable",
            "cannot write",
            "can't write",
        )
        line_count = len([ln for ln in text.splitlines() if ln.strip()])
        has_sections = ("[verse" in t_low) or ("[chorus" in t_low)
        if any(m in t_low for m in bad_markers):
            return False, "Ollama refused lyrics generation."
        if line_count < 8:
            return False, "Ollama returned too few lyric lines."
        if not has_sections:
            return False, "Ollama returned unstructured lyrics."
        return True, text
    except urllib.error.URLError as e:
        return False, f"Ollama unavailable: {e.reason}"
    except Exception as e:
        return False, str(e)


def _section_profile(progress: float) -> tuple[str, float, float, float, float]:
    """
    Return section + instrument multipliers by timeline progress [0..1].
    Outputs: (name, pad_mul, bass_mul, drums_mul, lead_mul)
    """
    p = _clamp(progress, 0.0, 1.0)
    if p < 0.10:
        return "intro", 0.55, 0.0, 0.0, 0.10
    if p < 0.26:
        return "verse1", 0.70, 0.55, 0.45, 0.25
    if p < 0.39:
        return "chorus1", 0.90, 0.85, 0.90, 0.85
    if p < 0.55:
        return "verse2", 0.72, 0.58, 0.52, 0.30
    if p < 0.68:
        return "chorus2", 0.95, 0.90, 1.00, 0.95
    if p < 0.80:
        return "bridge", 0.62, 0.35, 0.22, 0.70
    if p < 0.93:
        return "final_chorus", 1.00, 0.92, 1.00, 1.00
    return "outro", 0.45, 0.20, 0.15, 0.25


def _render_instrumental_wav(path: Path, bpm: int, minor: bool, duration_sec: int = 180) -> None:
    sr = 22050
    beat_sec = 60.0 / float(bpm)
    bar_sec = beat_sec * 4.0
    total_sec = float(duration_sec)
    n = int(total_sec * sr)
    out = [0.0] * n

    # Major/minor scale intervals and progression.
    scale = [0, 2, 3, 5, 7, 8, 10] if minor else [0, 2, 4, 5, 7, 9, 11]
    prog = [0, 5, 3, 4] if minor else [0, 4, 5, 3]
    root = random.choice([45, 47, 48, 50])  # A2..D3
    rng = random.Random(1337)
    lead_pattern = [0, 2, 4, 2, 5, 4, 2, 0]

    for i in range(n):
        t = i / sr
        progress = t / max(total_sec, 1e-9)
        _, pad_mul, bass_mul, drum_mul, lead_mul = _section_profile(progress)

        bar_i = int(t / bar_sec)
        bar_pos = t - bar_i * bar_sec
        beat_i = int(bar_pos / beat_sec)
        beat_pos = bar_pos - beat_i * beat_sec
        deg = prog[bar_i % len(prog)]

        chord_root = root + scale[deg]
        third = chord_root + (3 if minor else 4)
        fifth = chord_root + 7

        pad_env = _adsr(beat_pos, beat_sec, 0.01, 0.12, 0.68, 0.08)
        pad = (
            math.sin(2 * math.pi * _midi_to_hz(chord_root + 12) * t)
            + 0.82 * math.sin(2 * math.pi * _midi_to_hz(third + 12) * t)
            + 0.75 * math.sin(2 * math.pi * _midi_to_hz(fifth + 12) * t)
        ) * 0.12 * pad_env * pad_mul

        bass_env = 0.0
        if beat_i in (0, 2) and beat_pos < beat_sec * 0.8:
            bass_env = _adsr(beat_pos, beat_sec * 0.8, 0.002, 0.10, 0.65, 0.14)
        bass = math.sin(2 * math.pi * _midi_to_hz(chord_root - 12) * t) * 0.22 * bass_env * bass_mul

        kick = 0.0
        if beat_pos < 0.11:
            k_env = _adsr(beat_pos, 0.11, 0.001, 0.04, 0.22, 0.06)
            k_freq = 120.0 - 70.0 * _clamp(beat_pos / 0.11, 0.0, 1.0)
            kick = math.sin(2 * math.pi * k_freq * t) * 0.42 * k_env * drum_mul

        snare = 0.0
        if beat_i in (1, 3) and beat_pos < 0.12:
            s_env = _adsr(beat_pos, 0.12, 0.001, 0.03, 0.18, 0.08)
            noise = (rng.random() * 2.0 - 1.0) * 0.55
            tone = math.sin(2 * math.pi * 190.0 * t) * 0.15
            snare = (noise + tone) * 0.30 * s_env * drum_mul

        hat = 0.0
        half = beat_sec / 2.0
        sub_pos = (bar_pos % half)
        if sub_pos < 0.05:
            h_env = 1.0 - _clamp(sub_pos / 0.05, 0.0, 1.0)
            hat = (rng.random() * 2.0 - 1.0) * 0.08 * h_env * drum_mul

        # Simple lead/arpeggio for chorus/bridge impact.
        step = int((bar_pos / (beat_sec / 2.0))) % len(lead_pattern)
        lead_deg = lead_pattern[step] % len(scale)
        lead_note = root + scale[(deg + lead_deg) % len(scale)] + 12
        lead_env = _adsr((bar_pos % (beat_sec / 2.0)), beat_sec / 2.0, 0.002, 0.05, 0.55, 0.05)
        lead = math.sin(2 * math.pi * _midi_to_hz(lead_note) * t) * 0.16 * lead_env * lead_mul

        out[i] = pad + bass + kick + snare + hat + lead

    peak = max(abs(x) for x in out) or 1.0
    gain = 0.92 / peak
    pcm = bytearray()
    for x in out:
        s = int(_clamp(x * gain, -1.0, 1.0) * 32767.0)
        pcm.extend(struct.pack("<h", s))

    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(bytes(pcm))


def _lyrics_plain_text(lyrics: str) -> str:
    lines = []
    for line in (lyrics or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("[") and s.endswith("]"):
            continue
        lines.append(s)
    return " ".join(lines)


def _synthesize_vocals_mp3(lyrics_text: str, out_mp3: Path) -> tuple[bool, str]:
    """Create spoken-vocal MP3 from lyrics using gTTS."""
    try:
        from gtts import gTTS
    except Exception:
        return False, "gTTS not installed."
    try:
        plain = _lyrics_plain_text(lyrics_text)[:1800] or "Instrumental track."
        tts = gTTS(text=plain, lang="en", slow=False)
        tts.save(str(out_mp3))
        return True, str(out_mp3)
    except Exception as e:
        return False, str(e)


def _read_wav_duration_sec(wav_path: Path) -> float:
    with wave.open(str(wav_path), "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate() or 1
    return float(frames) / float(rate)


def _normalize_section_name(raw: str) -> str:
    s = (raw or "").strip().lower()
    if s.startswith("verse 1"):
        return "verse1"
    if s.startswith("verse 2"):
        return "verse2"
    if s.startswith("verse"):
        return "verse1"
    if s.startswith("final chorus"):
        return "final_chorus"
    if s.startswith("chorus"):
        return "chorus1"
    if s.startswith("bridge"):
        return "bridge"
    if s.startswith("intro"):
        return "intro"
    if s.startswith("outro"):
        return "outro"
    return "verse1"


def _parse_lyrics_by_section(lyrics_text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = "verse1"
    for raw in (lyrics_text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = _normalize_section_name(line[1:-1])
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return sections


def _section_windows(duration_sec: float) -> dict[str, tuple[float, float]]:
    total = max(float(duration_sec), 1.0)
    cuts = [
        ("intro", 0.00, 0.10),
        ("verse1", 0.10, 0.26),
        ("chorus1", 0.26, 0.39),
        ("verse2", 0.39, 0.55),
        ("chorus2", 0.55, 0.68),
        ("bridge", 0.68, 0.80),
        ("final_chorus", 0.80, 0.93),
        ("outro", 0.93, 1.00),
    ]
    return {name: (a * total, b * total) for name, a, b in cuts}


def _build_timed_lyrics_entries(lyrics_text: str, duration: float) -> list[tuple[float, str]]:
    windows = _section_windows(duration)
    by_section = _parse_lyrics_by_section(lyrics_text)
    if not by_section:
        return []

    section_order = ["verse1", "chorus1", "verse2", "chorus2", "bridge", "final_chorus", "outro"]
    # Reuse chorus text where possible so repeated hooks stay musically aligned.
    if "chorus2" not in by_section and "chorus1" in by_section:
        by_section["chorus2"] = list(by_section["chorus1"])
    if "final_chorus" not in by_section and "chorus1" in by_section:
        by_section["final_chorus"] = list(by_section["chorus1"])

    timed: list[tuple[float, str]] = []
    for sec in section_order:
        lines = by_section.get(sec) or []
        if not lines:
            continue
        start, end = windows[sec]
        span = max(end - start, 0.5)
        step = span / max(len(lines), 1)
        for i, txt in enumerate(lines):
            t = start + i * step + min(0.12, step * 0.12)
            when = max(0.0, min(t, max(duration - 0.05, 0.0)))
            timed.append((when, txt))
    timed.sort(key=lambda x: x[0])
    return timed


def _write_lrc_file(entries: list[tuple[float, str]], out_path: Path) -> None:
    def _fmt_lrc_time(sec: float) -> str:
        m = int(sec // 60)
        s = sec - (m * 60)
        return f"{m:02d}:{s:05.2f}"

    lines_out = [f"[{_fmt_lrc_time(sec)}]{txt}" for sec, txt in entries]
    out_path.write_text("\n".join(lines_out) + "\n", encoding="utf-8")


def _synthesize_synced_vocals_wav(
    timed_entries: list[tuple[float, str]],
    out_wav: Path,
) -> tuple[bool, str]:
    """
    Build a timed spoken vocal track by generating one TTS clip per line,
    delaying each clip to its timestamp, and mixing all clips together.
    """
    try:
        from gtts import gTTS
    except Exception:
        return False, "gTTS not installed."

    if not timed_entries:
        return False, "No timed lyric entries for synced vocals."

    try:
        with tempfile.TemporaryDirectory(prefix="luna_sync_vocals_") as td:
            temp_dir = Path(td)
            clips: list[tuple[Path, int]] = []
            for i, (sec, text) in enumerate(timed_entries):
                t = (text or "").strip()
                if not t:
                    continue
                clip_path = temp_dir / f"line_{i:03d}.mp3"
                # Keep each line concise for stable TTS and natural timing.
                gTTS(text=t[:220], lang="en", slow=False).save(str(clip_path))
                clips.append((clip_path, max(0, int(sec * 1000.0))))

            if not clips:
                return False, "No vocal clips generated."

            cmd = ["ffmpeg", "-y"]
            for clip_path, _ in clips:
                cmd.extend(["-i", str(clip_path)])

            parts = []
            labels = []
            for idx, (_, delay_ms) in enumerate(clips):
                lbl = f"v{idx}"
                parts.append(
                    f"[{idx}:a]aresample=44100,volume=1.18,adelay={delay_ms}|{delay_ms}[{lbl}]"
                )
                labels.append(f"[{lbl}]")
            parts.append(
                "".join(labels)
                + f"amix=inputs={len(labels)}:duration=longest:normalize=0,alimiter=limit=0.96[out]"
            )

            cmd.extend(
                [
                    "-filter_complex",
                    ";".join(parts),
                    "-map",
                    "[out]",
                    "-ar",
                    "44100",
                    "-ac",
                    "2",
                    str(out_wav),
                ]
            )
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        return True, str(out_wav)
    except FileNotFoundError:
        return False, "ffmpeg not found in PATH."
    except subprocess.CalledProcessError as e:
        return False, (e.stderr or e.stdout or str(e))[:700]
    except Exception as e:
        return False, str(e)


def create_lrc_for_project(project_folder: str | Path) -> tuple[bool, str]:
    """
    Create timestamped lyrics file (lyrics_sync.lrc) for an existing project folder.
    Timing is aligned to arrangement sections and total instrumental duration.
    """
    try:
        folder = Path(project_folder).resolve()
        if not folder.is_dir():
            return False, f"Project folder not found: {folder}"

        lyrics_path = folder / "lyrics.txt"
        wav_path = folder / "instrumental.wav"
        meta_path = folder / "meta.json"
        if not lyrics_path.is_file():
            return False, f"Missing lyrics.txt in {folder}"
        if not wav_path.is_file():
            return False, f"Missing instrumental.wav in {folder}"

        duration = _read_wav_duration_sec(wav_path)
        lyrics_text = lyrics_path.read_text(encoding="utf-8", errors="replace")
        timed = _build_timed_lyrics_entries(lyrics_text, duration)
        if not timed:
            return False, "No lyric lines found in lyrics.txt."

        lrc_path = folder / "lyrics_sync.lrc"
        _write_lrc_file(timed, lrc_path)

        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                meta = {}
            files = meta.get("files")
            if not isinstance(files, dict):
                files = {}
                meta["files"] = files
            files["lyrics_lrc"] = str(lrc_path)
            meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

        return True, str(lrc_path)
    except Exception as e:
        return False, f"LRC timing sync failed: {e}"


def _mix_instrumental_and_vocals(instrumental_wav: Path, vocals_path: Path, final_wav: Path) -> tuple[bool, str]:
    """
    Mix instrumental + vocals into final song.
    - If vocals are timed/stem WAV: mix directly.
    - If vocals are plain MP3: add light start delay.
    Requires ffmpeg in PATH.
    """
    vocals = vocals_path.resolve()
    if vocals.suffix.lower() == ".mp3":
        filter_complex = (
            "[1:a]volume=1.10,adelay=1800|1800[v];"
            "[0:a][v]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[out]"
        )
    else:
        filter_complex = (
            "[1:a]volume=1.10[v];"
            "[0:a][v]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[out]"
        )
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(instrumental_wav),
        "-i",
        str(vocals),
        "-filter_complex",
        filter_complex,
        "-map",
        "[out]",
        "-ar",
        "44100",
        "-ac",
        "2",
        str(final_wav),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return True, str(final_wav)
    except FileNotFoundError:
        return False, "ffmpeg not found in PATH."
    except subprocess.CalledProcessError as e:
        return False, (e.stderr or e.stdout or str(e))[:700]


def create_local_song_project(description: str) -> tuple[bool, str]:
    """Create local song assets from prompt and return output folder path."""
    desc = (description or "").strip()
    if not desc:
        return False, "Please provide a description. Example: !local_song cinematic synthwave about hope."
    try:
        style, bpm, minor = _pick_style(desc)
        base = (ALLOWED_BASE / "music_local").resolve()
        base.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        folder = (base / f"{ts}_{_slug(desc)}").resolve()
        folder.mkdir(parents=True, exist_ok=True)

        wav_path = folder / "instrumental.wav"
        lyrics_path = folder / "lyrics.txt"
        vocals_mp3_path = folder / "vocals.mp3"
        vocals_synced_path = folder / "vocals_synced.wav"
        lyrics_lrc_path = folder / "lyrics_sync.lrc"
        final_wav_path = folder / "final_song.wav"
        meta_path = folder / "meta.json"
        prompt_path = folder / "prompt.txt"

        _render_instrumental_wav(wav_path, bpm=bpm, minor=minor, duration_sec=180)
        ok_lyrics, lyrics = _generate_lyrics_with_ollama(desc, style, bpm)
        if not ok_lyrics:
            lyrics = _generate_lyrics_fallback(desc, style)
        lyrics_path.write_text(lyrics, encoding="utf-8")
        prompt_path.write_text(desc, encoding="utf-8")

        duration = _read_wav_duration_sec(wav_path)
        timed_entries = _build_timed_lyrics_entries(lyrics, duration)
        _write_lrc_file(timed_entries, lyrics_lrc_path)

        ok_voc, voc_msg = _synthesize_synced_vocals_wav(timed_entries, vocals_synced_path)
        used_vocals_path: Path = vocals_synced_path
        if not ok_voc:
            ok_voc, voc_msg = _synthesize_vocals_mp3(lyrics, vocals_mp3_path)
            used_vocals_path = vocals_mp3_path

        ok_mix = False
        mix_msg = ""
        if ok_voc:
            ok_mix, mix_msg = _mix_instrumental_and_vocals(wav_path, used_vocals_path, final_wav_path)
        if not ok_mix:
            # Fallback: keep a playable final wav by copying instrumental bytes.
            final_wav_path.write_bytes(wav_path.read_bytes())
            if not mix_msg:
                mix_msg = "Used instrumental fallback for final_song.wav."

        meta = {
            "description": desc,
            "style": style,
            "bpm": bpm,
            "mode": "minor" if minor else "major",
            "duration_sec": 180,
            "lyrics_source": "ollama" if ok_lyrics else "fallback_template",
            "files": {
                "instrumental": str(wav_path),
                "lyrics": str(lyrics_path),
                "lyrics_lrc": str(lyrics_lrc_path),
                "vocals_mp3": str(vocals_mp3_path) if vocals_mp3_path.is_file() else "",
                "vocals_synced": str(vocals_synced_path) if vocals_synced_path.is_file() else "",
                "final_song": str(final_wav_path),
                "prompt": str(prompt_path),
            },
            "mix": {
                "vocals_ok": ok_voc,
                "mix_ok": ok_mix,
                "mix_note": mix_msg,
                "vocals_source": "synced_timed_lines" if used_vocals_path == vocals_synced_path else "single_tts_fallback",
                "vocals_note": voc_msg,
            },
        }
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        return True, str(folder)
    except Exception as e:
        return False, f"Local song generation failed: {e}"
