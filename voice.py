# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Direct-OpenAI integration for the voice layer -- text-to-speech
(gpt-4o-mini-tts) and speech-to-text (gpt-4o-mini-transcribe). Both run
through OpenAI directly, on OAI_API_KEY, not OpenRouter -- OpenRouter's
real TTS model collection carries no OpenAI TTS model at all (checked
against the actual collection page, not assumed), so a direct key is
required for the TTS half regardless. Keeping STT on the same direct key
too, rather than splitting it off onto OPENROUTER_API_KEY (which DOES
carry gpt-4o-mini-transcribe), is a deliberate simplicity choice: one
vendor, one pipeline, for a feature that's otherwise all-OpenAI anyway.
This is the settled design for the voice layer, not a default to
revisit lightly.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid

# mimetypes.guess_type maps .webm to "video/webm" (it's a container format
# shared with video) -- wrong for what a browser's MediaRecorder actually
# produces for an audio-only capture. Named explicitly rather than trusting
# the stdlib guess, for the handful of formats a real MediaRecorder emits.
_AUDIO_TYPES = {"webm": "audio/webm", "ogg": "audio/ogg", "oga": "audio/ogg",
                "mp4": "audio/mp4", "m4a": "audio/mp4", "wav": "audio/wav", "mp3": "audio/mpeg"}

TTS_MODEL = "gpt-4o-mini-tts"
STT_MODEL = "gpt-4o-mini-transcribe"
SPEECH_URL = "https://api.openai.com/v1/audio/speech"
TRANSCRIBE_URL = "https://api.openai.com/v1/audio/transcriptions"
API_TIMEOUT_S = int(os.environ.get("NORI_VOICE_TIMEOUT_S", "30"))

# The fixed preset roster gpt-4o-mini-tts actually ships (verified against
# OpenAI's own current docs, 2026-09-12 -- not simply trusted from an
# earlier written assumption): no reference-audio cloning on this endpoint,
# just these 13 named presets. nova is the operator's own pick, made by ear
# across the real roster -- kept as a per-user setting (config.py's
# tts_voice, default below), not a constant, since picking one by
# listening is exactly the kind of call they'll want to revisit.
VOICES = ("alloy", "ash", "ballad", "coral", "echo", "fable", "marin",
          "nova", "onyx", "sage", "shimmer", "verse", "cedar")
DEFAULT_VOICE = "nova"


class VoiceError(Exception):
    pass


def _key() -> str:
    k = os.environ.get("OAI_API_KEY", "")
    if not k:
        raise VoiceError("OAI_API_KEY not set -- the voice layer needs a direct OpenAI key, "
                         "separate from OPENROUTER_API_KEY (OpenRouter carries no OpenAI TTS model)")
    return k


def _instructions_for(emotion_state: str) -> str:
    """gpt-4o-mini-tts's `instructions` param is genuinely steerable
    prosody -- OpenAI's own docs name "Emotional range" as one of the
    dimensions it controls (verified, not assumed from the model name
    alone), which is what made this model the pick over tts-1 in the
    first place. One generic template, not 24 hand-authored lines against
    emotions.json -- the state name is the only per-state content this
    needs, and it reads naturally straight into the template for every
    entry in that list, "deadpan" and "flirty" included."""
    return (f"Speak this the way someone who is genuinely feeling {emotion_state} right now "
            f"would say it -- let that come through in tone, pacing, and emphasis, not just "
            f"the words themselves.")


def tts_bytes(text: str, *, voice: str, emotion_state: str) -> bytes:
    if voice not in VOICES:
        voice = DEFAULT_VOICE
    body = {
        "model": TTS_MODEL,
        "input": text,
        "voice": voice,
        "instructions": _instructions_for(emotion_state),
        "response_format": "mp3",
    }
    req = urllib.request.Request(
        SPEECH_URL, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_S) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise VoiceError(f"TTS request failed ({exc.code}): {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise VoiceError(f"TTS request failed: {exc}") from exc


def transcribe_bytes(audio: bytes, *, filename: str, content_type: str) -> str:
    """filename/content_type come from the client's own recording (a
    MediaRecorder blob) -- sanitized before use since they land in a
    multipart header we build by hand (no library does this for us; see
    server.py's own _parse_multipart docstring on why -- Python 3.13
    dropped the stdlib cgi module, and writing one side of this format is
    no harder than reading it)."""
    filename = (filename or "speech.webm").replace('"', "").replace("\r", "").replace("\n", "")
    boundary = uuid.uuid4().hex
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    ctype = (content_type or _AUDIO_TYPES.get(ext) or "application/octet-stream").replace("\r", "").replace("\n", "")
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{STT_MODEL}\r\n'.encode("utf-8"),
        (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
         f'Content-Type: {ctype}\r\n\r\n').encode("utf-8"),
        audio,
        f"\r\n--{boundary}--\r\n".encode("utf-8"),
    ]
    body = b"".join(parts)
    req = urllib.request.Request(
        TRANSCRIBE_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {_key()}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise VoiceError(f"transcription failed ({exc.code}): {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise VoiceError(f"transcription failed: {exc}") from exc
    text = (data.get("text") or "").strip()
    if not text:
        raise VoiceError("transcription came back empty -- nothing audible was recorded")
    return text
