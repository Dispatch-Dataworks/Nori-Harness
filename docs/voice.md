# Voice

Source: `nori/voice.py`.

## Direct OpenAI, not OpenRouter

Text-to-speech (`gpt-4o-mini-tts`) and speech-to-text
(`gpt-4o-mini-transcribe`) both run on `OAI_API_KEY`, a direct OpenAI
key separate from `OPENROUTER_API_KEY`. This is a real requirement,
not a preference: OpenRouter's TTS model collection carries no OpenAI
TTS model at all, so the text-to-speech half needs a direct key
regardless. Speech-to-text could technically go through OpenRouter
instead (it does carry `gpt-4o-mini-transcribe`), but both stay on the
same direct key for simplicity — one vendor, one pipeline, for a
feature that's otherwise all-OpenAI anyway.

Without `OAI_API_KEY` set, voice is simply off — nothing else on this
app is affected, and the failure is a specific, named error
(`VoiceError`), not a silent gap.

## Text-to-speech

13 named presets (`alloy`, `ash`, `ballad`, `coral`, `echo`, `fable`,
`marin`, `nova`, `onyx`, `sage`, `shimmer`, `verse`, `cedar`) — the
fixed roster this endpoint actually ships, verified against OpenAI's
own current docs rather than assumed from an older list. No
reference-audio cloning exists on this endpoint. Each household member
picks their own voice (`tts_voice` in [Settings](settings.md), default
`nova`) — a call worth revisiting by ear, so it's a per-user setting,
not a constant.

**Emotional prosody is real, not decorative.** `gpt-4o-mini-tts`
accepts a genuinely steerable `instructions` parameter — OpenAI's own
documentation names "emotional range" as one of the dimensions it
actually controls, which is what made this model the pick over a
plainer TTS model in the first place. Every synthesis request carries
one instruction built from her [current emotional state](emotions.md)
("speak this the way someone who is genuinely feeling `<state>` right
now would say it..."), one generic template rather than a hand-authored
line per state — the state name is the only per-state content the
template needs, and it reads naturally for every entry in the emotion
list, including ones that might seem awkward to hand-write for
individually.

## Speech-to-text

A plain multipart upload built by hand, not through a library — no
current stdlib module writes a multipart request (Python's `cgi`
module, which used to help with this, was removed), and hand-building
one side of the format is no harder than what parsing it already
requires elsewhere in this app. The filename and content type come
from the browser's own recording and are sanitized before use, since
they land directly inside a header this app constructs itself. An
empty transcription result is reported as a specific, honest failure
("nothing audible was recorded"), not silently treated as an empty
message.

## What isn't built

There's no automatic "always speak replies aloud" mode — voice is
invoked per message from the client, both directions. Extending it to
a different provider, or to always-on playback, is a real design
decision to make deliberately (see [Contributing](contributing.md)),
not something to bolt on quietly under this module's existing shape.
