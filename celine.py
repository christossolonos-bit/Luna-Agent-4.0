"""
Celine — Sub-agent (like George and Shadow) that handles voice clips / MP3 files sent on Discord (e.g. from your phone).
She transcribes the audio and decides whether it's a command for Shadow or chat for Luna.
Bot passes in transcribe and route-decider callables so Celine stays independent.
"""
from __future__ import annotations

import os
import tempfile
from typing import Callable

# Audio attachment extensions and MIME prefix for Discord voice messages / uploaded clips
CELINE_AUDIO_EXTENSIONS = (".mp3", ".m4a", ".ogg", ".webm", ".wav", ".opus")
CELINE_AUDIO_CONTENT_TYPE_PREFIX = "audio/"


async def save_first_audio_attachment(message) -> str | None:
    """
    If the message has an audio attachment, save it to a temp file and return the path.
    Caller must unlink the file when done. Returns None if no audio attachment.
    """
    if not getattr(message, "attachments", None):
        return None
    for att in message.attachments:
        fn = (getattr(att, "filename") or "").lower()
        ct = (getattr(att, "content_type") or "").lower()
        if fn.endswith(CELINE_AUDIO_EXTENSIONS) or ct.startswith(CELINE_AUDIO_CONTENT_TYPE_PREFIX):
            suffix = os.path.splitext(att.filename)[1] if getattr(att, "filename", None) else ".ogg"
            if suffix.lower() not in CELINE_AUDIO_EXTENSIONS:
                suffix = ".ogg"
            fd, path = tempfile.mkstemp(suffix=suffix, prefix="celine_voice_")
            try:
                os.close(fd)
                await att.save(path)
                return path
            except Exception as e:
                print(f"[Celine] Voice attachment save error: {e}", flush=True)
                try:
                    os.unlink(path)
                except Exception:
                    pass
                return None
    return None


async def process_voice_message(
    message,
    transcribe_fn: Callable[[str], str | None],
    route_decider: Callable[[str], str | None],
    *,
    run_in_thread=None,
) -> tuple[str | None, str | None]:
    """
    Handle a Discord message that may contain a voice/audio attachment (e.g. MP3 from phone).
    - If there is an audio attachment: save it, transcribe with transcribe_fn, then call route_decider(transcript)
      to get "shadow" (command) or "luna" (chat).
    - transcribe_fn(path) -> transcript text or None. Called from a thread if run_in_thread is provided.
    - route_decider(transcript) -> "shadow" | "luna" | None.
    Returns (transcribed_text, route). If no audio or transcription failed, (None, None).
    """
    path = await save_first_audio_attachment(message)
    if not path:
        return None, None
    try:
        if run_in_thread is not None:
            text = await run_in_thread(transcribe_fn, path)
        else:
            text = transcribe_fn(path)
        if not text or not (text := (text or "").strip()):
            return None, None
        route = route_decider(text) if route_decider else None
        return text, route
    except Exception as e:
        print(f"[Celine] Voice process error: {e}", flush=True)
        return None, None
    finally:
        try:
            if path and os.path.isfile(path):
                os.unlink(path)
        except Exception:
            pass
