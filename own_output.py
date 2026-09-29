# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Her own past output, shown back to her.

THE RULE THIS MODULE EXISTS FOR: anything malformed that gets STORED and later
RE-RENDERED into her prompt becomes a template she imitates.

What the model sees is its own earlier assistant turns. If one of those is
wrong -- a tool call typed out as prose, a bracketed annotation the app added
that she then copied as if it were her reply, an app-generated fallback line
("(hit the 6-step tool-call limit ...)") stored as though she had said it --
every later turn has an example of it sitting in her window, and models copy
their own examples. One stored artefact seeds the next.

So: the DATABASE keeps what actually happened (the record is never edited),
and every place that renders her own past words back into a prompt goes
through `scrub()`, which removes the artefact at render time. What the app
itself needs to tell her (for example that a photo went out) is added as a
clearly marked "(app note, not part of what you said: ...)" line, never as a
bracketed form she could type herself.

Any new feature that shows her her own past output -- a summary, a search
result, a transcript, a digest -- must do the same. tests/test_nori_own_output.py
fails when a module starts reading stored messages and has not been triaged.

Detecting a leak at generation time (chat.py) uses the same pattern
(`leak_pattern`), so what is caught going in and what is scrubbed coming back
out cannot drift apart.
"""
from __future__ import annotations

import re

EMPTY = ""   # what a message that was ONLY an artefact renders as; callers skip empty assistant lines

# Lines the app itself writes into her transcript as if she had said them (chat.py's
# failure and limit messages). They are recorded, but they are not her voice.
_APP_FALLBACKS = (
    re.compile(r"^\(hit the \d+-step tool-call limit.*\)$", re.S),
    re.compile(r"^\(something didn't go through.*\)$", re.S),
    re.compile(r"^\(no reply .{1,3} the model returned nothing\)$", re.S),
)

# The bracketed annotation older versions of this app rendered for a photo she had sent
# ("[you sent him a photo] with: ..."), which she then copied as her own reply.
_ANNOTATION = re.compile(r"\[\s*(?:you|i|she|he|nori)\s+sent\b[^\]\n]*\](?:\s*with:.*)?", re.I)


def leak_pattern(tool_names: set[str], *, prose_verbs: bool = True) -> re.Pattern | None:
    """The shapes a tool call takes when the model types it out instead of calling it. `prose_verbs` includes the plain-sentence form ("using search_history"): right for
    judging a reply as it is generated (a whole reply that is that phrase is a leak), too blunt for cutting text out of stored history, where a real sentence such as "you can
    use search_history for that" must survive -- so scrub() passes False."""
    if not tool_names:
        return None
    pat = "|".join(re.escape(n) for n in sorted(tool_names, key=len, reverse=True))
    verbs = rf'|\b(?:calling|invoking|using|use)\s+(?:the\s+)?(?:{pat})\b' if prose_verbs else ''
    return re.compile(
        r'</?tool_(?:call|response)\b'
        rf'|\{{\s*"name"\s*:\s*"(?:{pat})"'
        rf'|[\[<]\s*(?:calling|using|invoking|use)?\s*(?:the\s+)?(?:{pat})\b'
        # Backtick-wrapping alone is NOT enough -- found in real history
        # (nori id=252): "`mcp1_get_note` returns outgoing links..." is
        # ordinary markdown for naming a technical term in a real technical
        # discussion, not an attempted call, and backticks are a common,
        # legitimate convention for that in a way [ and < are not. Only
        # counted when the backtick-wrapped name is immediately followed by
        # a real call shape -- an opening paren -- same as the true
        # positive this needed to keep catching (`send_image(prompt=...)`).
        rf'|`\s*(?:{pat})\s*\('
        rf'{verbs}'
        # Two more shapes, found live (2026-09-12) in Nori's own real
        # history on gpt-4.1-mini -- a different model from the one the
        # patterns above were tuned against, and it invented notation neither prior shape covers.
        # The detector was confirmed still fully wired and running on
        # these exact turns (checked directly, not assumed) -- it simply
        # never matched; these are new gaps, not a regression.
        #   1. Bare function-call syntax, no backtick at all: the ENTIRE
        #      reply was once just `set_emotion(state="happy")`. No space
        #      between name and paren is the load-bearing distinction
        #      from a genuine prose parenthetical ("recall (which
        #      searches memory)..."), which always has a space there.
        rf'|\b(?:{pat})\('
        #   2. "<name> called: <value>" -- name first, then the past-
        #      tense verb "called", never covered by the calling/using/
        #      invoking/use list above (all present-participle/
        #      imperative, always BEFORE the name, not after).
        rf'|\b(?:{pat})\s+called\b',
        re.IGNORECASE)


def app_note(text: str) -> str:
    """How the app tells her something about her own past turn: marked as the app's, in a form she cannot mistake for her own words."""
    return f"(app note, not part of what you said: {text})"


def registered_names() -> set:
    """Every tool name the app knows right now (native and dynamically registered)."""
    import tools
    return set(tools._REGISTRY)


def find(text: str, names: set | None = None):
    """The first prose-call artefact in `text` as a regex match, else None. Pure text."""
    if not text or not isinstance(text, str):
        return None
    rx = leak_pattern(names if names is not None else registered_names(), prose_verbs=False)
    return rx.search(text) if rx else None


def scrub(text, role: str = "assistant", names: set | None = None):
    """`text` as it may be shown back to her. For her OWN words: app-generated fallback lines render as nothing, prose-call artefacts and copyable
    photo annotations are cut (from where the artefact starts to the end of its line; what she said before it stays). Other roles come back untouched.
    The stored record is not changed."""
    if role != "assistant" or not text or not isinstance(text, str):
        return text
    stripped = text.strip()
    if any(p.match(stripped) for p in _APP_FALLBACKS):
        return EMPTY
    out = text
    names = names if names is not None else registered_names()
    for _ in range(8):
        m = find(out, names)
        if not m:
            break
        eol = out.find("\n", m.end())
        eol = len(out) if eol < 0 else eol
        prefix = re.sub(r"[\[\(\{<`*\s]+$", "", out[:m.start()])
        prefix = re.sub(r"(?:\b(?:call|calling|use|using|invoke|invoking|run|running|the)\s*)+$", "", prefix, flags=re.I)
        out = prefix.rstrip(" \t") + out[eol:]
    for _ in range(4):
        m = _ANNOTATION.search(out)
        if not m:
            break
        out = out[:m.start()] + out[m.end():]
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out if out or not text.strip() else EMPTY
