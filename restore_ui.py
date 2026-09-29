# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""The shared pieces of the "three layers" restore interface used by the persona editor and the context-tuning page: shipped default, your baseline, and the current
working value, with three clearly separate ways back (baseline, default, one edit back) and a preview of exactly what a restore would change before it happens.

Pure functions returning HTML strings; no request object, no storage."""
from __future__ import annotations

import difflib
import html
import time


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def age(ts: float, now: float | None = None) -> str:
    s = max(0.0, (now if now is not None else time.time()) - ts)
    if s < 90:
        return "just now"
    if s < 90 * 60:
        return f"{int(s / 60)} minutes ago"
    if s < 36 * 3600:
        return f"{int(s / 3600)} hours ago"
    return f"{int(s / 86400)} days ago"


def diff_html(current: str, target: str) -> str:
    """A line diff of `current` -> `target` (what the restore would do). Identical text says so plainly rather than showing an empty box."""
    a = current.replace("\r\n", "\n").split("\n")
    b = target.replace("\r\n", "\n").split("\n")
    if a == b:
        return "<p class=muted>Identical to what is live now: restoring this would change nothing.</p>"
    out = []
    for line in difflib.unified_diff(a, b, "live now", "after the restore", lineterm="", n=2):
        e = esc(line)
        if line.startswith("+") and not line.startswith("+++"):
            out.append(f"<span style='background:rgba(46,160,67,.22);display:block'>{e}</span>")
        elif line.startswith("-") and not line.startswith("---"):
            out.append(f"<span style='background:rgba(248,81,73,.22);display:block'>{e}</span>")
        elif line.startswith("@@") or line.startswith("---") or line.startswith("+++"):
            out.append(f"<span class=muted style='display:block'>{e}</span>")
        else:
            out.append(f"<span style='display:block'>{e}</span>")
    return "<pre style='white-space:pre-wrap;font-size:.85rem;margin:0'>" + "".join(out) + "</pre>"


def rows_html(rows: list[dict]) -> str:
    """The same idea for a set of values: only what would change, old -> new."""
    changed = [r for r in rows if r["changed"]]
    if not changed:
        return "<p class=muted>Every value already matches: restoring this would change nothing.</p>"
    body = "".join(f"<tr><td>{esc(r['desc'])}<br><span class=muted style='font-size:.78rem'>{esc(r['key'])}</span></td>"
                   f"<td>{esc(r['current'])}</td><td>&rarr;</td><td><b>{esc(r['target'])}</b></td></tr>" for r in changed)
    same = len(rows) - len(changed)
    tail = f"<p class=muted>{same} other value{'s' if same != 1 else ''} already match and will not change.</p>" if same else ""
    return f"<div class=table-scroll><table><tr><th>setting</th><th>live now</th><th></th><th>after the restore</th></tr>{body}</table></div>{tail}"


def card(title: str, what: str, href: str | None, *, note: str = "") -> str:
    """One restore target: what it is, when it dates from, and a Preview link (never a one-click restore). `href` None = not available yet."""
    link = f"<a class=btn href='{esc(href)}'>preview and restore</a>" if href else "<span class=muted>not available yet</span>"
    n = f"<p class=muted style='margin:.2rem 0'>{esc(note)}</p>" if note else ""
    return (f"<div class=section style='margin:.6rem 0'><h3 style='margin:.1rem 0'>{esc(title)}</h3>"
            f"<p class=muted style='margin:.2rem 0'>{esc(what)}</p>{n}<p style='margin:.3rem 0'>{link}</p></div>")


def preview_page(*, heading: str, restores_to: str, body_html: str, csrf: str, action_path: str, fields: dict, back_href: str, confirm: str, button: str) -> str:
    """The page between "I want to go back" and going back: says what it restores to, shows the change, and only then offers the button."""
    hid = "".join(f"<input type=hidden name='{esc(k)}' value='{esc(v)}'>" for k, v in fields.items())
    return (f"<div class=section><h2>{esc(heading)}</h2><p class=muted>{esc(restores_to)}</p>{body_html}"
            f"<form method=post action='{esc(action_path)}' style='margin-top:.8rem' data-confirm='{esc(confirm)}'>"
            f"<input type=hidden name=csrf value='{esc(csrf)}'>{hid}<button class='btn btn-primary'>{esc(button)}</button></form>"
            f"<p><a href='{esc(back_href)}'>cancel and go back</a></p></div>")
