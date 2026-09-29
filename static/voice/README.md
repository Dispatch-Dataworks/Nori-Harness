# Voice reference clip

**Status: not currently read by anything.** The built voice layer speaks
through `gpt-4o-mini-tts`'s fixed preset voices (`nova`, picked by ear,
configurable in Settings) — that endpoint doesn't clone from a reference
sample at all, so this file plays no role in it.

This clip is kept anyway, deliberately, as a possible local voice-cloning
route if the OpenAI-preset path ever stops feeling right — a local TTS
model that clones from a single reference clip like this one is a
plausible future direction. That clip is personal to whoever's running
this instance — a specific voice, not generic art — so it is **not**
shipped in the repo. Same reasoning as `static/avatars/source/` and the
manuscript/model-weight exclusions in the root `.gitignore`, just applied
to a voice sample instead of art or text.

To keep your own reference clip here, for that possible future route:

1. Record or obtain a clean 10–20 second clip of the voice you want —
   quiet room, no background music, consistent mic distance, natural
   delivery (the clip's own emotional baseline carries into the clone).
   WAV, 44.1kHz or 48kHz, mono preferred, works best for most local
   voice-cloning models.
2. Save it here as exactly:
   ```
   static/voice/reference.wav
   ```

Nothing here is served over HTTP the way avatars are — this is a local
input to the TTS model, not a browser asset.
