# Emotions

Source: `nori/emotion.py` (state storage and decay), `nori/precheck.py`
(the injection mechanism), `nori/emotions.json` (the configured state
list).

## The state list is config, not code

Adding a new emotional state means adding an entry to `emotions.json`
plus an avatar image — never touching `emotion.py` itself. That's
deliberate, and it matters beyond convenience: another operator can
ship an entirely different set of states without a code change at all.
The tool that lets her set one (`set_emotion`) is enum-constrained
against whatever that file currently lists, so she can never invent a
state the configured set doesn't include.

## Persists, and decays — computed, never written back

A turn with no `set_emotion` call just keeps whatever state was
already set — there's no reset-per-turn. Decay to `neutral` after a
period of real wall-clock inactivity (`NORI_EMOTION_DECAY_MINUTES`,
default 45) is computed at *read* time from the stored state and its
last-updated timestamp — never a background process writing a
"decayed" state back into the row. The next real `set_emotion` call is
what actually changes the stored value; a decayed read just reports
what the effective state currently is.

Decay is wall-clock, not turn-count, on purpose: the states aren't
points on one ordered scale, so "N turns of silence" doesn't mean the
same thing after a 30-second gap as after a three-day one the way a
real time threshold does.

## Shown, never narrated

She does not describe her emotional state in prose — the avatar
carries that visually. This is enforced at the mechanical level (tool-
usage guidance) and reinforced separately at the character level (the
persona states plainly that she has a real emotional range, backing
the same rule from the other direction) — two different layers making
the same behavior true, not one relying on the other.

## Why a per-turn reminder exists at all — a real, measured finding

Two different attempts to get her to reconsider her emotional state on
her own — a standing line in the persona, and a mechanical instruction
in tool-usage guidance — measurably failed to move how often
`set_emotion` actually got called, tested twice, for real, against a
real model. What did work, the only thing that did, was a plain,
direct, in-context reminder placed immediately before the reply — not
better wording, a better *position*. `precheck.py` generalizes that
one finding into reusable infrastructure: any module can register a
short check (`session, user_id → line or None`); every registered
check's current line is combined into exactly one system message,
appended as the *last* message before the model replies — never folded
into the standing system prompt context.py assembles. See
[Context & tuning](context.md#two-things-deliberately-proximate-not-standing)
for the general principle this demonstrated, and
[Peer agents](peer-agents.md#how-a-pending-message-actually-reaches-her)
for the same lesson found independently a second time, in a completely
different system.

A check with nothing to add this turn contributes nothing — no empty
bullet point, no standing-seeming filler token cost when there's
genuinely nothing to reconsider.

## Extending it

A second precheck (the module's own docstring names "new mail" as an
example) registers with `precheck.register(fn)` the same way a
[scheduler](scheduler.md#extending-it) signal provider does — no
change to `precheck.py` itself required, and no risk of quietly
reintroducing the standing-prompt failure mode this module exists to
avoid.
