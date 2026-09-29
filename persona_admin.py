# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""The persona editor: what the settings page's Persona tab shows and does.

The persona is who Nori is -- her voice, her manner, what she will and will not do in conversation -- so editing it is the most powerful thing an operator can
change from a browser. Three properties are deliberate:

  * OPERATOR ONLY. This module is reachable from exactly one place, server.py's admin-gated Persona routes (an `admin` session and a valid CSRF token, the same as every
    other administration page). It is NOT registered as a tool, and nothing a tool can import reaches it: the model, a sub-agent, an MCP server and a connected peer agent
    all act through tools.py's registry, and no entry in it can write a prompt file. tests/test_nori_persona_admin.py holds that line structurally (what tools.py, the
    settings tool and every other tool module import) and behaviourally (a member session is refused).
  * ALWAYS A WAY BACK. Every save first snapshots the text it replaces (up to 100 versions, see promptdoc.py); an edit that is rejected changes nothing; a save is
    validated before it is written and read back after; the shipped default is a tracked file that "reset" restores at any time whatever state the live file is in; and
    the operator can freeze a known-good state as a baseline of their own. If the browser itself is unavailable, deleting `prompts/persona.md` returns to the shipped
    default (docs/persona.md).
  * A FRESH INSTALL NEEDS NOTHING. With no `persona.md` at all, the shipped default is used as-is; the operator's edits create `persona.md`, which is theirs and is never
    tracked or overwritten by an upgrade.

Functions here return plain data or HTML strings and touch no request object, so they can be tested without a server.
"""
from __future__ import annotations

import html
import re
import time

import persona
import restore_ui

HIST_RE = re.compile(r"^persona-\d{8}-\d{6}(?:-\d+)?\.md$")
HISTORY_SHOWN = 20
ACTIONS = ("save", "restore", "baseline_save", "baseline_revert", "reset_default")


def _e(s) -> str:
    return html.escape(str(s), quote=True)


def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def status() -> dict:
    """Where the live text comes from: the shipped default (nothing edited yet), the operator's own file, or the operator's file when it happens to equal the default."""
    has_file = persona.PERSONA_PATH.is_file()
    return {"customized": has_file and not persona.is_default(), "has_file": has_file, "tokens": persona.approx_tokens(),
            "baseline": persona.has_baseline(), "baseline_ts": persona.baseline_saved_ts(), "history": persona.history()}


def act(action: str, form: dict) -> tuple[bool, str]:
    """Do one persona action. `form` carries `text` (save) or `name` (restore). Returns (ok, message). Never raises on bad input: an unknown action or a non-string field is
    a refusal, not a crash. A refused action changes nothing."""
    text = form.get("text")
    name = form.get("name")
    if action == "save":
        if not isinstance(text, str):
            return False, "no text to save"
        ok, msg = persona.save(text)
        return ok, ("saved" if ok else f"not saved: {msg}")
    if action == "restore":
        if not isinstance(name, str) or not HIST_RE.match(name):
            return False, "no such version"
        ok, msg = persona.restore(name)
        return ok, ("restored that version (the text it replaced is in the history)" if ok else f"not restored: {msg}")
    if action == "baseline_save":
        ok, msg = persona.save_baseline()
        return ok, ("saved as your baseline" if ok else f"not saved: {msg}")
    if action == "baseline_revert":
        ok, msg = persona.revert_to_baseline()
        return ok, ("reverted to your baseline (the text it replaced is in the history)" if ok else f"not reverted: {msg}")
    if action == "reset_default":
        ok, msg = persona.reset_to_default()
        return ok, ("reset to the shipped default (the text it replaced is in the history)" if ok else f"not reset: {msg}")
    return False, "unknown action"


def history_text(name) -> str | None:
    return persona.history_text(name) if isinstance(name, str) and HIST_RE.match(name) else None


def _save_baseline_form(csrf: str, has_baseline: bool, baseline_ts) -> str:
    confirm = (f"Replace your baseline from {_when(baseline_ts)} with the text that is live now?" if has_baseline else "Save the text that is live now as your baseline?")
    return (f"<form method=post action='/admin/persona/baseline_save' style='display:inline-block' data-confirm='{_e(confirm)}'>"
            f"<input type=hidden name=csrf value='{_e(csrf)}'><button class='btn btn-primary'>save the current text as my baseline</button></form>")


def previous_name() -> str | None:
    h = persona.history()
    return h[0]["name"] if h else None


def target_text(target: str, name=None) -> tuple[str | None, str]:
    """(text, label) for a restore target: baseline, default, previous (one edit back) or history (a named version)."""
    if target == "baseline":
        return (persona.baseline_text(), "your saved baseline") if persona.has_baseline() else (None, "no baseline saved yet")
    if target == "default":
        t = persona.default_text()
        return (t, "the shipped default") if t.strip() else (None, "no shipped default found")
    if target == "previous":
        n = previous_name()
        return (persona.history_text(n), "the version before your last edit") if n else (None, "no earlier version")
    if target == "history":
        t = history_text(name)
        return (t, f"the version from {name}") if t is not None else (None, "no such version")
    return None, "unknown target"


_RESTORE = {"baseline": ("baseline_revert", "Restore my baseline"), "default": ("reset_default", "Restore the shipped default"),
            "previous": ("restore", "Go back one edit"), "history": ("restore", "Restore this version")}


def render_preview(target: str, name, csrf: str) -> str | None:
    """The page between deciding to go back and going back: what it restores to, the exact change, then the button. None when the target does not exist."""
    text, label = target_text(target, name)
    if text is None:
        return None
    action, button = _RESTORE[target]
    fields = {}
    if target in ("previous", "history"):
        fields["name"] = name if target == "history" else previous_name()
    return restore_ui.preview_page(
        heading=f"Restore {label}", restores_to=f"This replaces the live persona with {label}. The text it replaces is kept in the history, so you can undo this.",
        body_html=restore_ui.diff_html(persona.load_file(), text), csrf=csrf, action_path=f"/admin/persona/{action}", fields=fields, back_href="/settings?tab=persona",
        confirm=f"Replace the live persona with {label}?", button=button)


def render(csrf: str, err: str = "", info: str = "", draft: str | None = None) -> str:
    """The Persona tab body. `draft` is the text a rejected save should keep showing, so a validation failure never costs the operator what they typed."""
    st = status()
    if st["customized"]:
        source = "You are using your own edited persona."
    elif st["has_file"]:
        source = "You are using your own copy of the persona (currently identical to the shipped default)."
    else:
        source = "You are using the shipped default persona. Nothing has been edited yet."
    shown = draft if draft is not None else persona.load_file()
    e = f"<p class=err>{_e(err)}</p>" if err else ""
    i = f"<p class=info>{_e(info)}</p>" if info else ""
    bts = st["baseline_ts"]
    hist = st["history"]
    hist_rows = "".join(f"<tr><td>{_e(_when(h['ts']))}</td><td class=muted>{h['bytes']} bytes</td>"
                        f"<td><a href='/admin/persona/preview?target=history&name={_e(h['name'])}'>preview and restore</a></td></tr>" for h in hist[:HISTORY_SHOWN])         or "<tr><td class=muted>No earlier versions yet: the first edit you save will be kept here.</td></tr>"
    cards = (
        restore_ui.card("Back to my baseline", "The last version you marked as good, on purpose. It only changes when you save a new baseline; editing, resetting or restoring never touches it.",
                        "/admin/persona/preview?target=baseline" if st["baseline"] else None,
                        note=(f"Saved {_when(bts)} ({restore_ui.age(bts)})." if st["baseline"] and bts else "You have not saved one yet: use the button below when you like how she is.")) +
        restore_ui.card("Back to the shipped default", "What a fresh install gets. It never changes and is always available, whatever state the live text is in.",
                        "/admin/persona/preview?target=default") +
        restore_ui.card("Back one edit", "The version this replaced when you last saved. Doing it twice puts you back where you started; to go further back, pick a version from the history below.",
                        "/admin/persona/preview?target=previous" if hist else None,
                        note=(f"From {_when(hist[0]['ts'])}." if hist else "")))
    return (
        f"{e}{i}"
        "<div class=section><h2>who she is</h2>"
        f"<p class=muted>{_e(source)} About {st['tokens']} tokens, sent with every reply. This is the character text: her voice, manner and boundaries in conversation. "
        "How she uses her tools is a separate, mechanical document and is not edited here. Only administrators can change this page, and she has no tool that can.</p>"
        "<form method=post action='/admin/persona'>"
        f"<input type=hidden name=csrf value='{_e(csrf)}'>"
        "<div class=field><label for=persona-text>persona</label>"
        f"<textarea id=persona-text name=text rows=24 style='width:100%;font-family:monospace;font-size:.85rem'>{_e(shown)}</textarea>"
        f"<span class=muted style='font-size:.78rem'>Keep the two marker lines ({_e(persona.promptdoc.START)} and {_e(persona.promptdoc.END)}): only the text between them "
        "is sent. A save that fails validation changes nothing.</span></div>"
        "<p><button class='btn btn-primary'>save persona</button></p></form></div>"
        "<div class=section><h2>going back</h2>"
        "<p class=muted>Three layers: the <b>shipped default</b> (never changes), your <b>baseline</b> (a last-known-good you set on purpose), and the <b>live text</b> above. "
        "Each way back shows you exactly what would change before it does anything, and every restore keeps the text it replaces.</p>"
        f"{cards}<p style='margin-top:.6rem'>{_save_baseline_form(csrf, st['baseline'], bts)}</p></div>"
        f"<div class=section><h2>history</h2><div class=table-scroll><table>{hist_rows}</table></div></div>"
    )
