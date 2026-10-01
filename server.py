#!/usr/bin/env python3
# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Nori — server entry point. Auth/RBAC (Phase 2) and persona/chat (Phase 3)
are real and HTTP-tested. Tools, memory, and everything past that are still
later phases.

Security posture (same shape as a sibling application's, retyped, own env vars):
  - binds 127.0.0.1 only by default (NORI_BIND_HOST overrides — needed once
    this runs in a container, where the boundary does the isolation instead)
  - no password anywhere in .env — the admin account is created through
    the app itself, on first run, against a genuinely empty database
  - SQLite-persisted sessions (HttpOnly/SameSite=Lax/Secure/24h), CSRF on
    every POST, per-IP failed-login rate limiting
  - session tokens stored hashed, never raw, at rest
  - CSP default-src 'none'; every piece of user-entered text HTML-escaped
    before it reaches a page

UI (2026-09-11 redesign): a fixed, dark, mobile-first app shell. This
redesign settled 100dvh + min-height:0, visualViewport-driven keyboard
handling, and the avatar peek/recede resolution to the mobile-space
problem, and left the card board at placeholder status.
"""
from __future__ import annotations

import datetime
import hmac
import html
import http.cookies
import json
import mimetypes
import os
import re
import socket
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# IPv4-only DNS resolution, process-wide (2026-09-13) -- found investigating
# real "model latency": every outbound HTTPS call this app makes (chat.py's
# OpenRouter/direct-OpenAI calls, voice.py, mcp_client.py, and web.py's
# upcoming search/fetch) was burning ~42 SECONDS per call, and it had
# nothing to do with any model, provider, or reasoning setting -- confirmed
# by timing a bare, unauthenticated GET with no API involved at all, to two
# unrelated hosts, both landing at the same ~42s. Root-caused with a raw
# socket test: IPv6 connectivity on this machine/network is broken (packets
# go out, nothing comes back -- it times out rather than failing fast), and
# each host resolves to two IPv6 addresses that Python's stdlib tries
# BEFORE ever falling back to IPv4 (no "happy eyeballs" parallel racing in
# urllib/http.client) -- ~21s wasted per address, twice, before the IPv4
# fallback that actually works in under 50ms. Nothing in this app has ever
# successfully used IPv6 for anything -- there is no working behavior this
# could take away, only a broken one it stops attempting. Applied once,
# process-wide, before any network code runs -- every module that makes an
# outbound call (chat.py, voice.py, mcp_client.py, oauth.py, the upcoming
# web search/fetch tool) benefits with no per-module change needed.
#
# Second pass, same day: the first version below filtered getaddrinfo's
# RESULT to IPv4 only, but still asked the OS resolver for AF_UNSPEC (both
# families) on every call. It cut a bare request from ~42s to ~0.3s in
# isolated testing, but real turns kept costing ~88s/round even after this
# was deployed and confirmed loaded -- and the mystery only resolved when
# the operator disabled IPv6 at the OS/network level himself and turns got fast on
# the SAME code, same process, no further change. That reconciles only one
# way: the OS-level resolution/connection step itself can stall on this
# machine when IPv6 is in play at all (Windows does background IPv6
# reachability probing that can add latency to unrelated socket calls when
# an adapter's IPv6 route is dead), independent of which addresses end up
# in the returned list -- so filtering the output after the fact never
# touched it. Forcing AF_INET on the underlying OS call itself (below),
# not just on what we do with its answer, is what actually keeps the OS
# out of IPv6 entirely. Kept the old filter as a fallback for the case
# where AF_INET narrowing itself fails (a genuinely IPv6-only host).
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    req_family = family if family not in (0, socket.AF_UNSPEC) else socket.AF_INET
    try:
        return _orig_getaddrinfo(host, port, req_family, type, proto, flags)
    except socket.gaierror:
        # This host genuinely has no IPv4 address -- fall back to the
        # original, unfiltered call rather than making it unreachable.
        results = _orig_getaddrinfo(host, port, family, type, proto, flags)
        ipv4 = [r for r in results if r[0] == socket.AF_INET]
        return ipv4 or results


socket.getaddrinfo = _ipv4_only_getaddrinfo

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE
# Moved out of the project folder entirely -- a directory sibling to
# this repo's own checkout, so a real key never sits anywhere git could
# ever see it, not even behind .gitignore. Derived from the repo's own
# location (sibling directory, "<repo-name>-env") rather than a
# hardcoded absolute path -- no operator's specific drive letter or
# username belongs in checked-in code.
ENV_PATH = REPO_ROOT.parent / (REPO_ROOT.name + "-env") / "nori.env"


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# MUST run before importing any of the app's own modules below -- every one
# of them reads its own tunables from os.environ as module-level constants
# at import time (NORI_MODEL, NORI_TOOL_RATE_LIMIT, this file's own
# BIND_HOST/PORT, ...). A real bug, caught by actually running the fresh-
# clone test rather than assuming it would work: with load_env() called
# only inside main() (which runs after all these imports already happened),
# a value that only ever existed in .env -- never a real pre-set shell env
# var -- was silently ignored. Loading .env first, before a single app
# module is imported, is the fix; calling load_env() again later changes
# nothing (os.environ.setdefault is a no-op the second time) but doesn't
# hurt either, so main() still calls it too, for anyone reading main() in
# isolation and expecting it to be the place .env gets loaded.
load_env(ENV_PATH)

import accounts
import capabilities
import chat
import config
import crypto
import diagnostics
import connected_accounts
import contacts
import context
import conversation
import drive
import email_calendar
import emotion
import household
import homeassistant
import imagegen
import integration_health
import jobs
import mcp_servers
import models
import meals
import medialog
import memory
import peers
import persona_admin
import self_knowledge
import tuning_admin
import settings_tool
import oauth
import providers
import recurrence
import scheduler
import schedules
import sharepoint
import store
import catalog
import notes
import reminders
import sub_agents
import tasks
import timing
import trackers
import tool_builder
import turns
import usertime
import voice
import webtools
import workfiles

AVATAR_DIR = HERE / "static" / "avatars"
_AVATAR_EXTS = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
               "svg": "image/svg+xml", "gif": "image/gif", "webp": "image/webp"}
# Derived cache, sibling to AVATAR_DIR -- same relationship GEN_DIR/thumbs
# already has (2026-09-16, operator's own finding: the real avatar art is
# 900x900 PNGs, ~800KB-1MB each, served RAW into a header chip that never
# shows them past 80px -- the identical waste the photo grid had before
# thumbnails, just never caught here since nothing measured it until
# asked). AVATAR_MAX_DIM covers the largest real on-screen use with
# headroom for 3x DPI (the hero panel's ~300px box; the header
# chip/voice-avatar are both smaller) without also serving that at 512px
# in a 64px chip on a phone.
AVATAR_THUMB_DIR = AVATAR_DIR / "thumbs"
AVATAR_MAX_DIM = 256
# PWA icon set -- generated once from static/avatars/source/neutral.png,
# see static/pwa/README.md for how and why "neutral". Static files, not
# generated per-request like the avatars above (there's no per-state
# variation to justify that here -- one fixed icon set for the whole app).
PWA_DIR = HERE / "static" / "pwa"
# Emoji picker data (2026-09-15): the standard Unicode emoji set -- built
# from Unicode's own emoji-test.txt (group/name per codepoint), skin-tone
# and hair-style variants of People & Body dropped (default presentation
# only) to keep this to one entry per distinct emoji rather than every
# modifier combination. 1902 entries, ~56KB uncompressed, built once and
# checked in rather than regenerated. Read once at import,
# served as-is by serve_emoji_data() below; never regenerated per-request.
# Bump the filename's own version suffix (v1 -> v2) if this ever needs to
# change, rather than mutating it in place -- the route below sends a
# year-long immutable Cache-Control, so a same-URL change would just never
# reach an existing session.
_EMOJI_DATA_JSON = (HERE / "static" / "emoji-data.v1.json").read_bytes()
# Needed to build an exact OAuth redirect_uri -- the operator registers
# this same URL in each provider's own console, so it has to be real and
# stable, not guessed from whatever Host header a request happens to carry.
PUBLIC_URL = os.environ.get("NORI_PUBLIC_URL", "").rstrip("/")
# 127.0.0.1 is the default and the right one for running directly on a
# machine -- the tunnel is the only thing meant to reach it. A container
# needs this raised to 0.0.0.0 (the container boundary does the isolation
# instead), which is exactly why it's a setting and not a literal in the
# bind call below.
BIND_HOST = os.environ.get("NORI_BIND_HOST", "127.0.0.1")
PORT = int(os.environ.get("NORI_PORT", "8877"))
SECURE_COOKIE = os.environ.get("NORI_ALLOW_INSECURE_COOKIE", "") not in ("1", "true", "yes")
# Off by default (2026-09-26, pre-publication review finding): CF-Connecting-IP/
# X-Forwarded-For are request headers, not connection facts -- anyone who can
# reach this server directly can set them to anything, including someone else's
# real IP, and Handler.ip() (below) feeds straight into the per-IP login rate
# limiter. Trusting them unconditionally means that limiter is trivially
# defeated the moment this app is reachable directly (not fronted by a proxy
# that strips/overwrites inbound copies of these headers before adding its own).
# Set this only when actually deployed behind such a proxy -- a reverse proxy,
# load balancer, or Cloudflare tunnel that the operator controls and that
# guarantees no client-supplied copy of these headers survives to reach here.
TRUST_PROXY_HEADERS = os.environ.get("NORI_TRUST_PROXY", "") in ("1", "true", "yes")
START_TS = time.time()


class _TimestampedWriter:
    """Wraps a file object so every line written to it gets its own
    ISO-timestamp prefix (2026-09-17, prompted by a real same-day
    crash-loop incident). Before this, .nori.log/.nori.err carried
    every print() line, every TIMING line, and every raw Python traceback
    with NO timestamp at all -- diagnosing a real crash-loop after the
    fact meant a wall of unlabeled tracebacks with no way to tell which
    one happened when, or to correlate against a deploy, a reboot, or
    anything else with a real clock. Stamps every line, not just once per
    block -- a multi-line traceback still shows one call's own real start
    time on its first line, and this way a genuinely stuck/silent gap
    inside one is visible too, not just between blocks.

    Line-buffers internally (matching the underlying file's own
    buffering=1) rather than stamping raw write() calls directly:
    print()/traceback.print_exception() don't call write() once per
    line consistently, so stamping naively could split one real line
    across two stamps or leave a mid-line stamp -- buffering until each
    real newline and stamping the complete line is what makes this
    correct for both a plain print() and a multi-write traceback dump."""
    def __init__(self, f):
        self._f = f
        self._buf = ""

    def _stamp(self) -> str:
        return datetime.datetime.now().isoformat(timespec="seconds")

    def write(self, s: str) -> int:
        if not s:
            return 0
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._f.write(f"{self._stamp()} {line}\n")
        return len(s)

    def flush(self) -> None:
        if self._buf:
            self._f.write(f"{self._stamp()} {self._buf}")
            self._buf = ""
        self._f.flush()

    def __getattr__(self, name):
        return getattr(self._f, name)


def _redirect_logs() -> None:
    """Write our own log files so nori_ctl.ps1 can Start-Process us WITHOUT
    -RedirectStandardOutput/Error -- a sibling application hit a real issue where that
    form makes PowerShell's Start-Process hang the launcher until the child
    exits. Same fix, retyped, own files."""
    if os.environ.get("NORI_NO_LOGFILE"):
        return
    try:
        import sys
        out = open(HERE / ".nori.log", "a", buffering=1, encoding="utf-8")
        err = open(HERE / ".nori.err", "a", buffering=1, encoding="utf-8")
        sys.stdout, sys.stderr = _TimestampedWriter(out), _TimestampedWriter(err)
    except OSError:
        pass


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def _ago(ts: float) -> str:
    """Coarse relative time for "updated by X · _ago(ts)" rows -- plain
    enough not to need a JS library for something shown once per row."""
    s = time.time() - ts
    if s < 90:
        return "just now"
    m = s / 60
    if m < 90:
        return f"{int(m)}m ago"
    h = m / 60
    if h < 36:
        return f"{int(h)}h ago"
    return f"{int(h / 24)}d ago"


def _due_label(ts: float) -> str:
    """Compact, human due-date label for a task card/detail view --
    overdue is called out explicitly rather than left to a plain date to
    imply, since that's the one thing worth a glance catching."""
    now = time.time()
    when = time.strftime("%b %d, %H:%M" if ts - now < 86400 else "%b %d", time.localtime(ts))
    return f"overdue -- was due {when}" if ts < now else f"due {when}"


def _recur_label(t: dict) -> str:
    if t["recur_type"] == "interval":
        return f"every {t['recur_interval_min']} min"
    return f"daily {t['recur_time_hour']:02d}:{t['recur_time_minute']:02d}"


def _truncate(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def info_tip(text: str) -> str:
    """The shared long-explanation component (2026-09-18, design pass --
    his own instruction: brief text stays inline, longer explanations
    go behind a circled info icon instead of sitting as body text under
    every field). One function so every settings/admin tab that needs
    this reaches for the identical markup rather than hand-writing the
    three-element structure -- see TOOLTIP_JS in APP_JS for the shared
    toggle behavior (one popover open at a time, closes on outside
    click/Escape, no new CSP surface). `text` is plain, unescaped
    input -- this function escapes it, so call sites pass the raw
    explanation, not pre-escaped HTML."""
    return (f"<span class=info-wrap><button type=button class=info-btn aria-expanded=false "
           f"aria-label='More detail'>i</button><span class=info-pop>{esc(text)}</span></span>")


def _debug_text(debug: dict) -> str:
    """Plain, unescaped multi-line text for one assistant message's debug
    panel (2026-09-30) -- mirrors info_tip()'s own "plain text in, this
    function escapes it" contract; renders via .dbg-pop's white-space:
    pre-wrap, so \\n is all the layout this needs. Shared by the server-
    rendered bubble (_bubble()) and the client-side JS builder
    (makeDebugBtn() in APP_JS) so the two never drift apart -- built once
    here, the JS version is a straight line-for-line port."""
    lines = [
        f"Provider: {debug.get('provider_label')} ({debug.get('provider_type')})",
        f"Model: {debug.get('model_alias')} ({debug.get('model_name')})",
        f"Position: {debug.get('chain_position')} of {debug.get('chain_length')}",
        f"Request ID: {debug.get('request_id') or '—'}",
        f"Time: {time.strftime('%b %d, %H:%M:%S', time.localtime(debug.get('ts') or time.time()))}",
        f"Latency: {debug.get('latency_ms')} ms",
        f"Tokens: {debug.get('prompt_tokens', 0)} in / {debug.get('completion_tokens', 0)} out",
        "Cost: " + ("unavailable" if debug.get("cost_unavailable") else f"${debug.get('cost_usd', 0):.4f}"),
    ]
    failed = debug.get("failed_attempts") or []
    if failed:
        lines.append("Failed attempts:")
        lines += [f"  {f['alias']} ({f['provider_type']}): {f['error']}" for f in failed]
    calls = debug.get("tool_calls") or []
    if calls:
        lines.append("Tool calls:")
        lines += [f"  round {c['round']}: {c['name']}({json.dumps(c['args'])})" for c in calls]
    if debug.get("reasoning"):
        lines.append("Reasoning:")
        lines.append(debug["reasoning"])
    return "\n".join(lines)


def _debug_btn(debug: dict | None) -> str:
    """The message-bubble sibling to _copy_btn() -- a small ⓘ that toggles
    the panel above, next to the copy button. Empty string (no button at
    all) when there's no debug meta (round-limit/leak-failure fallback
    text that never reached a real model call -- see server.py's own
    persist-site comment)."""
    if not debug:
        return ""
    return (f"<span class=info-wrap><button type=button class=info-btn aria-expanded=false "
           f"aria-label='Message info'>ⓘ</button><span class='info-pop dbg-pop'>"
           f"{esc(_debug_text(debug))}</span></span>")


# ── design tokens + shared component styles ──────────────────────────────
# One dark visual identity, not a light theme inverted -- a deliberate
# product decision, not viewer-adaptive. System-ui
# stack throughout, no webfont: dependency-light is the right call for
# something self-hosted, "modern" comes from scale/spacing/weight instead.
BASE_CSS = """
:root{
  --bg:#0b0c10; --surface:#15171c; --surface-2:#1d2027; --surface-3:#262a33;
  --border:#2a2e37; --text:#f1f2f4; --text-dim:#a7acb8; --text-mute:#6e7482;
  --accent:#7c8cff; --accent-strong:#94a1ff; --on-accent:#12131a; --danger:#e5675f;
  --k-note:#7c8cff; --k-task:#34d399; --k-reminder:#f5a524;
}
*{box-sizing:border-box}
html{height:100%;overflow:hidden}
html,body{overscroll-behavior:none}
body{margin:0;min-height:100%;background:var(--bg);color:var(--text);
  font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;-webkit-tap-highlight-color:transparent}
h1,h2,h3{margin:0;text-wrap:balance;font-weight:600}
a{color:var(--accent);text-decoration:none}
code{background:var(--surface-2);border:1px solid var(--border);border-radius:4px;padding:.1rem .35rem;
  font-size:.85em}
.muted{color:var(--text-dim);font-size:.85rem}
.err{color:var(--danger);font-size:.88rem;margin:0 0 .6rem}
.info{color:var(--text);font-weight:600;font-size:.88rem;margin:0 0 .6rem}

/* -- form controls, used everywhere: auth pages, settings, files, admin -- */
.field{display:flex;flex-direction:column;gap:.35rem;margin:0 0 .9rem}
.field label{font-size:.82rem;color:var(--text-dim)}
input[type=text],input[type=password],input[type=number],textarea,select{
  height:44px;border-radius:10px;border:1px solid var(--border);background:var(--surface-2);
  color:var(--text);padding:0 .8rem;font:inherit;font-size:.94rem;width:100%}
input[type=file]{color:var(--text-dim);font-size:.84rem}
textarea{height:auto;min-height:88px;padding:.6rem .8rem;font-family:ui-monospace,Consolas,monospace;font-size:.85rem}
input:focus,textarea:focus,select:focus{outline:2px solid var(--accent);outline-offset:1px}
input::placeholder,textarea::placeholder{color:var(--text-mute)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:.4rem;height:44px;padding:0 1.1rem;
  border-radius:11px;border:1px solid var(--border);background:var(--surface-2);color:var(--text);
  font:inherit;font-size:.9rem;font-weight:600;cursor:pointer;white-space:nowrap}
.btn:active{background:var(--surface-3)}
.btn-primary{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.btn-primary:active{background:var(--accent-strong)}
.btn-ghost{background:none;border-color:transparent;color:var(--text-dim);padding:0 .5rem;height:auto}
.btn-danger{border-color:var(--danger);color:var(--danger)}
.btn-danger:active{background:var(--danger);color:var(--surface)}
.btn-block{width:100%}
.chip-row{display:flex;gap:.4rem;flex-wrap:wrap;margin:0 0 1rem}
.chip{padding:.4rem .8rem;border-radius:99px;background:var(--surface-2);border:1px solid var(--border);
  color:var(--text-dim);font-size:.82rem;font-weight:600}
.chip.active{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.chip.err{background:var(--danger);border-color:var(--danger);color:var(--on-accent)}
.settings-tabs{position:relative;display:flex;gap:6px;overflow-x:auto;padding:2px 2px 8px;
  overscroll-behavior-x:contain;scrollbar-width:thin}
.settings-tabs .chip{display:flex;align-items:center;min-height:44px;flex-shrink:0;white-space:nowrap}
.settings-title{font-size:1.1rem;margin-bottom:16px}

/* -- settings navigation (2026-09-19, design pass -- replaces a two-row
   horizontally-scrolling chip strip that stopped working once the tab
   count passed 20: hard to scan, hard to use one-handed, and the exact
   sideways-scroll-trap shape this app avoids everywhere else. Same
   drill-in shape the board's own task/note/reminder pages already use
   (a list, tap through to detail, chevron back), given its own second
   level here rather than invented fresh: on a phone, /settings with no
   ?tab= is a plain vertical menu (grouped exactly as _settings_groups
   already decided -- this pass never touched that); picking a row
   drills into that tab full-screen with its own "All settings" link
   back to the menu, distinct from the page header's own "Settings"
   chevron (untouched, still goes to chat, per his own instruction).
   At >=900px both panes are simply always visible together, master/
   detail style, the same before/after-900px reveal `.hero` already
   uses elsewhere in this file -- no JS, no new scroll container,
   .shell-main stays the one real scroll ancestor either way; the rail
   sticks to the viewport while the content column scrolls past it. */
.settings-shell{margin-bottom:4px}
.settings-content{display:none;min-width:0}
.settings-back{display:none;align-items:center;gap:.3rem;color:var(--text-dim);font-size:.85rem;
  font-weight:600;margin-bottom:14px;min-height:32px}
.settings-shell--active .settings-rail{display:none}
.settings-shell--active .settings-content{display:block}
.settings-shell--active .settings-back{display:inline-flex}
.settings-group{margin-bottom:18px}
.settings-group:last-child{margin-bottom:0}
.settings-group-label{display:block;margin:0 0 6px;padding:0 2px;color:var(--text-mute);
  font-size:.72rem;text-transform:uppercase;letter-spacing:.04em}
.settings-row{display:flex;align-items:center;justify-content:space-between;gap:.6rem;min-height:48px;
  padding:0 14px;margin-bottom:6px;border-radius:12px;background:var(--surface);
  border:1px solid var(--border);color:var(--text);font-size:.92rem;font-weight:600}
.settings-row:last-child{margin-bottom:0}
.settings-row:active{background:var(--surface-2)}
.settings-row.active{background:var(--surface-2);border-color:var(--accent)}
.settings-row-chevron{color:var(--text-mute);font-size:1.1rem;flex-shrink:0}
.settings-row.active .settings-row-chevron{color:var(--accent)}
.settings-placeholder{padding:32px 16px;text-align:center}
.field-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.7rem .9rem;margin:0 0 .8rem}
.field-grid .field{margin:0}
@media(min-width:900px){
  .settings-shell{display:flex;gap:28px;align-items:flex-start}
  .settings-rail{flex:0 0 240px;position:sticky;top:16px}
  .settings-content{display:block}
  .settings-shell--active .settings-rail{display:block}
  .settings-shell--active .settings-back{display:none}
}

/* -- data tables (2026-09-19, design pass): raw <table> had NO styling
   of its own anywhere in this app before this -- browser-default
   borderless cells, on every admin tab that used one. A wide one (a
   log with several columns) still doesn't fit a phone screen just
   because the borders are nicer, so .table-scroll is the one place
   this app lets something scroll sideways on purpose: a bounded data
   grid inside its own card, never the page and never navigation --
   exactly the distinction the settings nav rebuild above draws, not a
   contradiction of it. */
table{width:100%;border-collapse:collapse;font-size:.88rem}
th,td{padding:.5rem .6rem;text-align:left;border-bottom:1px solid var(--border);vertical-align:top}
th{color:var(--text-mute);font-size:.72rem;text-transform:uppercase;letter-spacing:.03em;font-weight:700}
tr:last-child td{border-bottom:0}
.table-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:0 -2px;padding:0 2px}
.table-scroll table{min-width:520px}
.peer-debug{margin:20px 0;border:1px solid var(--border);border-radius:12px;overflow:hidden}
.peer-debug summary{padding:14px;cursor:pointer;font-weight:600;background:var(--surface-2)}
.peer-debug .msglist{max-height:60vh;padding-bottom:14px}

/* -- list rows: files, sub-agents, invites, tool drafts -- one component,
   reused everywhere a list of things needs showing -- */
.list-row{display:flex;align-items:center;gap:.7rem;padding:.65rem 0;border-bottom:1px solid var(--border)}
.list-row:last-child{border-bottom:0}
.list-icon{width:36px;height:36px;border-radius:10px;background:var(--surface-2);display:flex;
  align-items:center;justify-content:center;font-size:1.05rem;flex-shrink:0}
.list-meta{flex:1;min-width:0;display:flex;flex-direction:column}
.list-meta b{font-size:.9rem;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* memory facts run long (full sentences, sometimes multiple) -- let those
   wrap instead of clipping to one line like the shorter list titles do */
.list-meta b.wrap{white-space:normal;overflow:visible;text-overflow:clip;word-break:break-word}
.list-meta small{font-size:.74rem;color:var(--text-mute)}
.list-actions{display:flex;gap:.2rem;flex-shrink:0}
.list-actions form{display:inline}
.icon-action{width:40px;height:40px;border-radius:50%;border:0;background:none;color:var(--text-dim);
  font-size:1rem;cursor:pointer}
.icon-action:active{background:var(--surface-3)}

/* -- full history page: everything a message carries, paged, searchable -- */
.hist-wrap{max-width:720px;margin:0 auto;padding:.8rem 1rem 2rem}
/* .hist-sticky (2026-09-19): the search form + match-nav stay visible
   while jumping between results -- sticks to the top of .shell-main,
   the scroll container from the design pass (unchanged, still the one
   real scroll ancestor here) at .shell-main's own padding edge, well
   below .shell-header (a separate flex sibling, not part of this
   scroll box at all, so no offset math is needed for it here the way
   HISTORY_JUMP_JS below has to do for a sibling application's differently-shaped
   page). Its real rendered height (not guessed) is what the jump
   script subtracts before centering a hit in what's actually left. */
.hist-sticky{position:sticky;top:0;z-index:5;background:var(--surface);padding-bottom:.8rem;
  margin-bottom:.8rem;border-bottom:1px solid var(--border)}
.hist-sticky .hist-search{margin-bottom:0}
.hist-sticky .hist-matchnav{margin:.6rem 0 0}
.hist-search{display:flex;gap:.5rem;margin-bottom:1rem;flex-wrap:wrap}
.hist-search input[type=search]{flex:1;min-width:0}
.hist-matchnav{display:flex;gap:.8rem;align-items:center;flex-wrap:wrap;margin:0 0 1rem;font-size:.85rem}
.hist-list{display:flex;flex-direction:column;gap:.5rem}
.hist-row{border:1px solid var(--border);border-radius:12px;padding:.7rem .85rem;background:var(--surface)}
.hist-row.user{background:var(--surface-2)}
.hist-row.hist-hit{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset}
.hist-meta{display:flex;align-items:center;gap:.4rem;flex-wrap:wrap;margin-bottom:.4rem}
.hist-ts{font-size:.74rem;color:var(--text-mute);font-family:ui-monospace,Consolas,monospace}
.hist-badge{font-size:.68rem;font-weight:600;padding:.15rem .5rem;border-radius:99px;
  background:var(--surface-2);color:var(--text-dim);white-space:nowrap}
.hist-content{font-size:.92rem;white-space:pre-wrap;overflow-wrap:anywhere}
.hist-reason{margin-top:.4rem;font-size:.78rem;color:var(--text-mute);font-style:italic}
.hist-pagenav{display:flex;justify-content:space-between;gap:.8rem;margin-top:1.2rem}
.hist-pagenav a{display:flex;align-items:center;justify-content:center;flex:1;min-height:44px;
  border:1px solid var(--border);border-radius:11px}
.hist-pagenav a:active{background:var(--surface-3)}

/* -- photo reel: mobile-first grid, small tiles on a narrow viewport,
   more columns as width allows -- auto-fill rather than a fixed count so
   this never needs a breakpoint of its own. */
.photo-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(104px,1fr));gap:.5rem}
.photo-tile{margin:0;position:relative;border:1px solid var(--border);border-radius:10px;
  overflow:hidden;background:var(--surface)}
.photo-tile img{display:block;width:100%;aspect-ratio:1;object-fit:cover}
.photo-tile figcaption{font-size:.72rem;color:var(--text-dim);padding:.35rem .5rem 0}
.photo-ts{display:block;font-size:.66rem;color:var(--text-mute);padding:.15rem .5rem .4rem;
  font-family:ui-monospace,Consolas,monospace}

/* -- surface card (2026-09-18, design pass): every settings/admin page
   used a bare, unstyled .section div -- no elevation, no border --
   while the board had its own accent-colored .card. This is the one
   shared "a block of related content" surface for everywhere that
   isn't the board, so the app reads as one visual language instead of
   two. A `.section` with only an <h2> and no wrapped content (a bare
   divider label, not a real content block) will read oddly boxed --
   the fix there is giving it real content to wrap, not exempting the
   class; the few spots that still do this get fixed in their own
   phase (the "Pings" tab split, "Household"'s own invite divider),
   not patched around here. */
.section{margin:0 0 16px;background:var(--surface);border:1px solid var(--border);
  border-radius:14px;padding:16px 18px}
.section h2{font-size:.98rem;color:var(--text-dim);text-transform:uppercase;letter-spacing:.03em;
  font-weight:700;margin:0 0 12px}
.section h3{font-size:.86rem;color:var(--text-dim);margin:14px 0 8px}
.section:first-child{margin-top:0}

/* -- info tooltip (2026-09-18, design pass -- "brief text near the
   field; longer ones expandable behind a circled icon," his own
   instruction): SHORT explanations stay as plain inline .muted text,
   no component needed. This is only for the genuinely long ones --
   voice consent, write-mode, security screening and the like -- so
   they don't sit as a wall of body text under every field. Same
   tap-to-reveal shape #imageviewerprompt already uses (proven, zero
   new CSP surface -- see TIP_JS in APP_JS), extended with a plain CSS
   hover for desktop since a mouse shouldn't need a tap. The popover
   is a plain absolutely-positioned overlay with NO overflow-y of its
   own -- never a second scroll container that could fight
   .shell-main's, the exact bug class both prior scroll-trap incidents
   came from. */
.info-wrap{position:relative;display:inline-block}
.info-btn{display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;
  border-radius:50%;border:1px solid var(--border);background:var(--surface-2);color:var(--text-mute);
  font-size:.68rem;font-weight:700;cursor:pointer;flex-shrink:0;padding:0;font:inherit;
  vertical-align:middle;line-height:1}
.info-btn:hover,.info-btn:focus-visible,.info-btn[aria-expanded=true]{color:var(--text);border-color:var(--accent)}
.info-pop{display:none;position:absolute;left:0;top:26px;z-index:6;
  width:min(300px,calc(100vw - 40px));background:var(--surface-3);border:1px solid var(--border);
  border-radius:10px;padding:.6rem .8rem;font-size:.8rem;line-height:1.45;color:var(--text-dim);
  box-shadow:0 10px 28px rgba(0,0,0,.35)}
.info-btn[aria-expanded=true] + .info-pop{display:block}
@media (hover:hover){.info-wrap:hover .info-pop{display:block}}

/* per-message debug panel (2026-09-30, operator's own ask) -- same
   info-wrap/info-btn/info-pop toggle mechanics above, just a wider,
   left-aligned, pre-wrapped variant (provider/model/reasoning text runs
   longer than this component's usual one-line explanations) and its own
   icon so it doesn't get confused with the identical-looking copy
   button. Click-to-select isn't needed here the way the OAuth connect
   fields need it -- this is read-only reference info, not a value to
   copy elsewhere. */
.dbg-pop{width:min(360px,calc(100vw - 40px));white-space:pre-wrap;text-align:left;
  max-height:60vh;overflow-y:auto}
.dbg-pop b{color:var(--text)}
.msg-actions{display:inline-flex;gap:2px;align-items:center}

/* ── auth pages (setup/login/invite) + one-off error pages (2026-09-18,
   design pass -- these used to be a separate, simpler shell with no
   header/chevron at all; now the identical fixed shell every other
   page uses, "every page gets the chevron without exception," just
   with a narrower centered card instead of .page-content's wider
   760px prose width -- every current caller is a short form or a
   couple of lines of error text, never long-form reading. -- */
.auth-card{max-width:22rem;margin:2.5rem auto 2rem;padding:20px 22px;
  background:var(--surface);border:1px solid var(--border);border-radius:16px}
.auth-card h1{font-size:1.3rem;margin-bottom:.3rem}
.auth-card p{color:var(--text-dim);font-size:.9rem;line-height:1.5}

/* ── the fixed app shell -- header/main(/footer), only main scrolls.
   100dvh with a 100vh fallback for browsers that predate dvh; JS
   (KEYBOARD_JS) sets an explicit pixel height from visualViewport once it
   runs, which is what actually keeps the composer above an open on-screen
   keyboard -- dvh alone doesn't reliably react to that across browsers.
   position:fixed;inset:0 is the part that actually holds the header in
   place on a real phone: overflow:hidden alone (what this had before)
   stops NORMAL overflow, but iOS Safari's own elastic/rubber-band scroll
   gesture has a long-documented history of still moving the document a
   few pixels regardless -- overscroll-behavior is unreliably honored for
   the root document on iOS specifically (works fine for the CHROMIUM
   emulation this was originally tested in, which is exactly why this
   didn't show up in the browser tool's own mobile emulation and needed a
   real phone to surface). position:fixed removes body from document flow
   entirely, so there's nothing at the document level left for that
   gesture to grab -- the standard, well-established fix for a fixed app
   shell on iOS, not a new invention. -- */
body.app{position:fixed;inset:0;height:100vh;height:100dvh;display:flex;flex-direction:column;overflow:hidden}
/* z-index:45, not 20 -- this element is a stacking-context root (position:
   relative + z-index), so EVERYTHING nested inside it, including the
   menu-sheet dropdown's own z-index:60, is capped at this element's own
   rank when compared against body-level siblings. #nb-backdrop is a
   body-level sibling at z-index:40; at 20 the header (and the open menu
   inside it) rendered BELOW that backdrop despite menu-sheet's higher
   number, so every tap on a menu item actually landed on the backdrop
   instead (proven via document.elementFromPoint, not assumed) -- the menu
   visibly opened but every tap silently just closed it again. 45 clears
   the backdrop (40) while staying below the board sheet (50) and avatar
   peek (55), both of which are body-level siblings already unaffected by
   this trap and must stay able to cover the header when THEY open. */
.shell-header{flex:0 0 auto;display:flex;align-items:center;gap:.6rem;padding:14px;
  padding-top:calc(14px + env(safe-area-inset-top));background:var(--surface);
  border-bottom:1px solid var(--border);position:relative;z-index:45}
.hdr-left{display:flex;align-items:center;gap:.7rem;min-width:0}
.hdr-spacer{flex:1}
.hdr-back{width:44px;height:44px;border-radius:50%;display:flex;align-items:center;justify-content:center;
  font-size:1.25rem;color:var(--text-dim);flex-shrink:0}
.hdr-back:active{background:var(--surface-2)}
.hdr-title{font-size:1.02rem;font-weight:600}
.avatar-chip{width:44px;height:44px;border-radius:50%;overflow:hidden;flex-shrink:0;border:0;padding:0;
  cursor:pointer;background:var(--surface-3)}
.avatar-chip img{width:100%;height:100%;object-fit:cover;display:block}
.hdr-text{display:flex;flex-direction:column;gap:1px;min-width:0}
.hdr-name{font-size:.95rem;font-weight:600}
.hdr-state{font-size:.74rem;color:var(--text-dim);text-transform:capitalize;display:flex;align-items:center;gap:4px}
.hdr-state i{width:6px;height:6px;border-radius:50%;background:currentColor;flex-shrink:0}
.shell-header--chat{flex-wrap:wrap;gap:12px}
.shell-header--chat .avatar-chip{width:80px;height:80px;border-radius:18px}
.shell-header--chat .avatar-chip img{object-fit:contain}
.shell-header--chat .hdr-name{font-size:1.2rem}
.shell-header--chat .hdr-state{font-size:.85rem}
.chat-actions{display:flex;gap:6px;flex:1 0 100%;min-width:0}
.chat-actions a{display:flex;align-items:center;justify-content:center;flex:1;min-height:44px;
  padding:0 8px;border:1px solid var(--border);border-radius:10px;background:var(--surface-2);
  color:var(--text);font-size:.8rem;font-weight:600}
.chat-actions a:active{background:var(--surface-3)}
@media (min-width:900px){
  .shell-header--chat .avatar-chip{width:64px;height:64px}
  .chat-actions{flex:0 1 auto;order:0}
  .chat-actions a{padding:0 14px}
}
.icon-btn{position:relative;width:44px;height:44px;border-radius:50%;border:0;background:none;
  color:var(--text-dim);font-size:1.15rem;cursor:pointer;flex-shrink:0}
.icon-btn:active{background:var(--surface-2)}
.badge{position:absolute;top:2px;right:2px;min-width:15px;height:15px;border-radius:8px;background:var(--accent);
  color:var(--on-accent);font-size:.6rem;font-weight:700;display:flex;align-items:center;justify-content:center;
  padding:0 3px}
.menu-wrap{position:relative}
.menu-sheet{position:absolute;top:52px;right:0;min-width:180px;background:var(--surface-2);
  max-height:calc(var(--app-height,100dvh) - 132px - env(safe-area-inset-top));overflow-y:auto;
  border:1px solid var(--border);border-radius:12px;padding:.4rem;display:flex;flex-direction:column;gap:2px;
  z-index:60;transform:translateY(-6px);opacity:0;pointer-events:none;transition:transform .15s,opacity .15s;
  box-shadow:0 12px 30px -10px rgba(0,0,0,.5)}
.menu-sheet.show{transform:translateY(0);opacity:1;pointer-events:auto}
.menu-sheet a,.menu-sheet .menu-logout{display:block;padding:.6rem .7rem;border-radius:8px;color:var(--text);
  font-size:.88rem;background:none;border:0;text-align:left;width:100%;cursor:pointer;font:inherit}
.menu-sheet a:active,.menu-sheet .menu-logout:active{background:var(--surface-3)}
.nb-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:40;opacity:0;pointer-events:none;
  transition:opacity .2s}
.nb-backdrop.show{opacity:1;pointer-events:auto}

.shell-main{flex:1 1 auto;min-height:0}
.shell-main:not(.shell-main--split){overflow-y:auto;overscroll-behavior:contain;-webkit-overflow-scrolling:touch;
  padding:16px 16px calc(16px + env(safe-area-inset-bottom))}
.shell-main--split{display:flex;flex-direction:row;overflow:hidden}
.page-content{max-width:760px;margin:0 auto}
.shell-footer{flex:0 0 auto;display:flex;gap:.55rem;align-items:flex-end;padding:10px 12px;
  padding-bottom:calc(10px + env(safe-area-inset-bottom));background:var(--surface);
  border-top:1px solid var(--border)}
/* Auto-growing composer (2026-09-13) -- a <textarea>, not <input>, so it
   can hold a real newline. min-height matches the old 44px pill so a
   single empty line looks the same as before; max-height caps growth at
   whichever is smaller, ~7 lines or 35% of the ACTUAL visible viewport
   (var(--app-height), the same visualViewport-driven value KEYBOARD_JS
   already keeps current -- not 100dvh/vh directly, which don't shrink
   when the on-screen keyboard opens) -- so the cap stays sensible with
   the keyboard up on a short phone screen, not just at rest. JS
   (autoGrowComposer in CHAT_JS) does the actual per-keystroke resizing;
   this rule only sets the bounds and disables the native resize handle
   and default textarea line-wrap chrome.*/
.shell-footer textarea#msgInput{flex:1;min-height:44px;max-height:min(160px,calc(var(--app-height,100dvh)*.35));
  border-radius:22px;resize:none;overflow-y:hidden;padding:11px 16px;line-height:1.3;font:inherit}
.send-btn{width:44px;height:44px;border-radius:50%;flex-shrink:0;background:var(--accent);color:var(--on-accent);
  border:0;font-size:1.1rem;cursor:pointer;display:flex;align-items:center;justify-content:center}
.send-btn:active{background:var(--accent-strong)}
/* opens voice mode -- same 44px target as send-btn (it's a primary
   composer control). Plain icon button; all the recording/speaking/busy
   state now lives on .voice-mic-btn inside the modal itself, not here. */
.mic-btn{width:44px;height:44px;border-radius:50%;flex-shrink:0;background:var(--surface-3);color:var(--text);
  border:1px solid var(--border);font-size:1.05rem;cursor:pointer;display:flex;align-items:center;justify-content:center}
@keyframes mic-pulse{0%,100%{box-shadow:0 0 0 0 rgba(192,57,43,.45)}50%{box-shadow:0 0 0 7px rgba(192,57,43,0)}}
/* Pending photo attachment (2026-09-15, ported from a sibling application's identical
   composer-attach) -- staged after picking/pasting/dropping an image,
   cleared on send or via its own remove button. A compact 44px-tall chip
   sitting inline in the same footer row (nori's footer is a flex ROW, not
   a column the way a sibling application's own composer is -- this keeps the row's
   height and layout unchanged whether or not a photo is staged, rather
   than reflowing the whole footer taller). Base rule carries no `display`
   -- [hidden] must win by default, same reasoning as #scrollbtn's own
   note above. */
.composer-attach{align-items:center;gap:.3rem;flex-shrink:0}
.composer-attach:not([hidden]){display:flex}
.composer-attach img{height:44px;width:44px;object-fit:cover;border-radius:10px;display:block;flex-shrink:0}
.composer-attach button{flex-shrink:0;width:20px;height:20px;padding:0;border-radius:50%;font-size:.85rem;
  line-height:1;border:0;display:flex;align-items:center;justify-content:center;
  background:var(--surface-3);color:var(--text)}

/* Emoji picker (2026-09-15) -- a same-size circular button next to mic/
   attach, opening a compact panel anchored just above the composer
   itself rather than a full-screen modal like voice/photo: the whole
   point on mobile is NOT fighting the composer for vertical space, an
   area this app has already fought over twice (voice mode, photo
   attach). visibility:hidden (not just the transform) while closed --
   same idiom .board already uses below -- so it's out of the tab order
   and hit-testing while off-screen, not just visually gone. */
.emoji-btn{width:44px;height:44px;border-radius:50%;flex-shrink:0;background:var(--surface-3);color:var(--text);
  border:1px solid var(--border);font-size:1.15rem;cursor:pointer;display:flex;align-items:center;justify-content:center}
.emoji-panel{position:fixed;left:0;right:0;z-index:65;background:var(--surface-2);border-top:1px solid var(--border);
  max-height:min(340px,45vh);display:flex;flex-direction:column;transform:translateY(100%);visibility:hidden;
  transition:transform .15s ease;box-shadow:0 -8px 24px -8px rgba(0,0,0,.4)}
.emoji-panel.show{transform:translateY(0);visibility:visible}
.emoji-panel-head{flex:0 0 auto;padding:.5rem .6rem .3rem}
.emoji-panel-head input{width:100%;margin:0}
.emoji-tabs{flex:0 0 auto;display:flex;gap:.1rem;overflow-x:auto;padding:.3rem .5rem;border-bottom:1px solid var(--border)}
.emoji-tabs button{background:none;border:0;font-size:1.05rem;padding:.3rem .5rem;border-radius:8px;flex-shrink:0;color:var(--text-mute)}
.emoji-tabs button.active{background:var(--surface-3);color:var(--text)}
.emoji-grid{flex:1;min-height:0;overflow-y:auto;-webkit-overflow-scrolling:touch;display:grid;
  grid-template-columns:repeat(auto-fill,minmax(38px,1fr));padding:.3rem;align-content:start}
.emoji-grid button{font-size:1.35rem;background:none;border:0;border-radius:8px;padding:0;aspect-ratio:1;cursor:pointer;line-height:1}
.emoji-grid button:hover,.emoji-grid button:active{background:var(--surface-3)}
.emoji-grid-empty{color:var(--text-mute);font-size:.85rem;padding:1rem;text-align:center;grid-column:1/-1}

/* -- voice conversation mode: a full-screen modal (mobile and desktop
   alike -- this is a deliberate, immersive mode, not a small popover),
   opened by tapping .mic-btn. Highest z-index in the app (70): it's meant
   to cover literally everything else, including the board sheet (50) and
   avatar peek (55), while it's open. -- */
.voice-modal{position:fixed;inset:0;z-index:70;background:var(--bg);display:flex;flex-direction:column;
  visibility:hidden;opacity:0;transition:opacity .2s,visibility .2s;
  padding-top:env(safe-area-inset-top);padding-bottom:env(safe-area-inset-bottom)}
.voice-modal.show{visibility:visible;opacity:1}
.voice-modal-inner{flex:1;min-height:0;display:flex;flex-direction:column;padding:16px;position:relative}
.voice-close{position:absolute;top:16px;right:16px;width:40px;height:40px;border-radius:50%;border:0;
  background:var(--surface-2);color:var(--text);font-size:1.1rem;cursor:pointer;z-index:2}
.voice-avatar-wrap{flex:0 0 auto;display:flex;flex-direction:column;align-items:center;gap:.4rem;padding:40px 0 10px}
.voice-avatar{width:min(200px,48vw);height:min(200px,48vw);object-fit:contain;border-radius:22px;
  background:var(--surface-2)}
.voice-state-label{text-transform:capitalize;color:var(--text-dim);font-size:.85rem}
/* the visible exchange -- the operator's own requirement: a wrong transcription
   has to be SEEN, not silently become a message. Reuses the same .msg
   bubble language the main chat uses, just in its own scroll region so
   the modal itself never needs page-level scrolling. */
.voice-transcript{flex:1 1 auto;min-height:0;overflow-y:auto;overscroll-behavior:contain;display:flex;
  flex-direction:column;gap:8px;padding:8px 4px}
.voice-status{flex:0 0 auto;text-align:center;color:var(--text-mute);font-size:.82rem;min-height:1.3em;
  padding:2px 12px 4px}
.voice-status.err{color:var(--danger)}
.voice-controls{flex:0 0 auto;display:flex;align-items:center;justify-content:center;gap:18px;
  padding:8px 0 calc(10px + env(safe-area-inset-bottom))}
.voice-mic-btn{width:76px;height:76px;border-radius:50%;background:var(--accent);color:var(--on-accent);
  border:0;font-size:1.8rem;cursor:pointer;display:flex;align-items:center;justify-content:center}
.voice-mic-btn.recording{background:#c0392b;color:#fff;animation:mic-pulse 1.1s ease-in-out infinite}
.voice-mic-btn.busy{opacity:.6}
.voice-mic-btn.speaking{background:var(--surface-3);color:var(--text)}
/* display lives ONLY in the :not([hidden]) rule -- author CSS beats the
   UA's [hidden]{display:none} regardless of specificity, so a base rule
   setting display:flex unconditionally would silently defeat
   voiceStopBtn.hidden=true (this exact bug, already fixed once for
   #scrollbtn -- see its own comment above). */
.voice-stop-btn{width:52px;height:52px;border-radius:50%;background:var(--surface-3);color:var(--text);
  border:1px solid var(--border);font-size:1.15rem;cursor:pointer;align-items:center;justify-content:center}
.voice-stop-btn:not([hidden]){display:flex}
@media (prefers-reduced-motion: reduce){.mic-btn.recording,.voice-mic-btn.recording{animation:none}}

/* -- avatar peek: full portrait, transient. tap the header chip, or it
   shows itself briefly on a real state change. Desktop hides this --
   the persistent hero panel already does this job there. -- */
.peek{position:fixed;left:12px;right:12px;top:calc(var(--app-height,100dvh) / 2);max-width:560px;
  height:min(720px,calc(var(--app-height,100dvh) - 32px - env(safe-area-inset-top) - env(safe-area-inset-bottom)));
  display:flex;flex-direction:column;padding:0;color:var(--text);font:inherit;text-align:left;cursor:pointer;
  margin:0 auto;background:var(--surface-2);border:1px solid var(--border);border-radius:18px;overflow:hidden;
  z-index:55;box-shadow:0 20px 40px -12px rgba(0,0,0,.55);transform:translateY(-50%) scale(.97);opacity:0;
  visibility:hidden;pointer-events:none;transition:transform .3s cubic-bezier(.34,1.3,.64,1),opacity .25s,visibility .25s}
.peek.show{transform:translateY(-50%) scale(1);opacity:1;visibility:visible;pointer-events:auto}
.peek img{width:100%;height:100%;flex:1;min-height:0;object-fit:contain;display:block}
.peek-cap{display:block;flex:0 0 auto;padding:.7rem .9rem .9rem}
.peek-cap b{text-transform:capitalize;font-size:.95rem}
.peek-cap span{display:block;font-size:.78rem;color:var(--text-dim);margin-top:2px}

/* -- messages -- */
/* chat-col is the non-scrolling positioning parent #scrollbtn anchors to
   -- it has to be a SEPARATE element from .msglist itself, not .msglist
   with position:relative, because .msglist is the thing that scrolls;
   an absolutely-positioned child of a scrolling element scrolls away
   with it. Anchoring to this stationary wrapper instead is what keeps
   the button visually fixed near the bottom regardless of scroll
   position -- same relationship a sibling application's .wrap/#log have. -- */
.chat-col{flex:1 1 auto;min-width:0;min-height:0;position:relative;display:flex;flex-direction:column;
  overflow:hidden}
/* bottom padding is a deliberately reserved gutter, not just visual
   breathing room -- #scrollbtn floats at a fixed spot near the bottom of
   the visible column (see its own rule below), and without genuinely
   blank space reserved past the last real message even when scrolled all
   the way down, the button would sit on top of that message's text
   instead of below it. */
.msglist{flex:1 1 auto;min-height:0;overflow-y:auto;overscroll-behavior:contain;-webkit-overflow-scrolling:touch;
  padding:14px 14px 54px;display:flex;flex-direction:column;gap:9px}
.msglist.dragover{outline:2px dashed var(--text-dim);outline-offset:-4px}
/* .msglist reused outside chat's own bounded split-shell context (2026-09-15,
   the peer full-log page) -- found by actually trying to scroll it on a real
   mobile viewport, not by reading the CSS: .msglist's own overflow-y:auto +
   overscroll-behavior:contain make sense when IT is the real scroll
   container (chat's split layout, where a flex parent bounds its height and
   it genuinely has its own overflow to consume). Reused as-is on an
   ordinary page, .msglist has no flex parent to bound it, so it just grows
   to fit all its content (clientHeight==scrollHeight, nothing to scroll
   internally) -- but overscroll-behavior:contain still SCOPES the scroll
   gesture to it and refuses to let an unconsumed wheel/touch delta chain up
   to .shell-main, the actual scrollable ancestor. The visible symptom is
   indistinguishable from split_main's own trap: a page that looks right and
   has real overflow on .shell-main, but never actually moves. This modifier
   neutralizes exactly the scroll-consuming properties, keeping only the
   layout ones (flex column, gap, padding) .msglist is reused for here. */
.msglist--flow{flex:none;overflow:visible;overscroll-behavior:auto;-webkit-overflow-scrolling:auto}
/* the scroll-to-bottom button. Its OWN base rule must never set
   display:flex unconditionally -- author CSS beats the UA's
   [hidden]{display:none} regardless of specificity, so toggling
   btn.hidden in JS would silently do nothing (this exact bug, found and
   fixed in a sibling application today). display lives ONLY in the :not([hidden])
   rule below. */
#scrollbtn{position:absolute;left:50%;transform:translateX(-50%);bottom:14px;width:40px;height:40px;
  border-radius:50%;background:var(--surface-3);color:var(--text);border:1px solid var(--border);
  box-shadow:0 4px 14px -2px rgba(0,0,0,.5);font-size:1.05rem;line-height:1;cursor:pointer;padding:0;
  align-items:center;justify-content:center;z-index:15}
#scrollbtn:not([hidden]){display:flex}
.msg{max-width:82%;padding:.6rem .8rem;border-radius:16px;font-size:.92rem;line-height:1.45;
  white-space:pre-wrap;word-wrap:break-word}
.msg.user{align-self:flex-end;background:var(--accent);color:var(--on-accent);border-bottom-right-radius:5px}
.msg.assistant{align-self:flex-start;background:var(--surface-2);border-bottom-left-radius:5px}
.msg-meta{display:flex;align-items:center;gap:5px;font-size:.7rem;color:var(--text-mute);align-self:flex-start;
  margin:0 2px}
.msg-meta i{width:7px;height:7px;border-radius:50%;display:inline-block;flex-shrink:0}
/* Copy button (2026-09-15) -- sits under the text, inside the same
   bubble, its own line (display:block, not inline after the last word)
   -- small and quiet on purpose, not a second visible control fighting
   the bubble for attention until it's actually touched. */
.copy-btn{display:block;margin-top:4px;background:none;border:0;padding:1px 5px;
  font-size:.72rem;line-height:1.5;color:var(--text-mute);cursor:pointer;border-radius:6px}
.copy-btn:hover{background:var(--surface-3);color:var(--text)}
.copy-btn.copied{color:var(--accent)}
/* /peers -- one of these per connected peer, stacked in the same
   scrolling .msglist a conversation's own bubbles sit in (not a
   separate scroll region -- there's usually just one peer, and a
   second one is just another one of these plus its own bubbles below,
   no layout change needed). */
.peer-hdr{align-self:stretch;display:flex;flex-direction:column;gap:2px;padding:10px 2px 2px;
  border-bottom:1px solid var(--border);margin-bottom:6px}
.peer-hdr b{font-size:.95rem}
.peer-hdr .muted{font-size:.78rem}
/* §11.1 -- a peer's request pending the operator's own approval. Sits
   above that peer's message log, in the same stretch-width column. */
.approval-block{align-self:stretch;background:var(--surface-2);border:1px solid var(--border);
  border-radius:10px;padding:8px 10px;margin-bottom:8px;font-size:.85rem}
.approval-row{display:flex;justify-content:space-between;align-items:center;gap:8px;
  padding:6px 0;border-top:1px solid var(--border)}
.approval-row:first-of-type{border-top:none}
.approval-row .muted{display:block;font-size:.72rem;margin-top:2px}

/* -- tool-call line: deliberately NOT a bubble -- no background, radius,
   padding, or max-width, just small centered muted text, so it reads as
   an aside about what she did rather than a third party in the
   conversation. Same idea as a sibling application's .toolline. -- */
.toolline{align-self:center;color:var(--text-mute);font-size:.72rem;opacity:.8;margin:.2rem 0}
.msg-image{padding:.35rem;background:var(--surface-2)}
.msg-caption{padding:.35rem .3rem 0;font-size:.85rem}
/* Chat photo, capped (2026-09-15, operator's own ask): min(320px,100%),
   not a bare percentage of the bubble -- nori's own chat column has no
   overall max-width the way a sibling application's .wrap does, so a plain % here
   would still grow to whatever the desktop window happens to be. 320px
   holds regardless of window width; the 100% half is what still shrinks
   it correctly on a phone narrower than that. */
.chatimage{display:block;max-width:min(320px,100%);padding:0;border:0;background:transparent;
  border-radius:12px;cursor:zoom-in}
.chatimage img{display:block;max-width:100%;border-radius:inherit}
.chatimage:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
/* Full-screen viewer -- ported from a sibling application's identical <dialog>-based
   one (see IMAGE_VIEWER_JS's own comment for why click-anywhere-closes
   can't trap on mobile: the dialog's own content has no dead click
   zone). */
#imageviewer{position:fixed;inset:0;margin:0;width:100vw;height:100vh;height:100dvh;
  max-width:none;max-height:none;padding:0;border:0;background:rgba(0,0,0,.95);color:#fff;
  overflow:hidden;overscroll-behavior:contain;cursor:zoom-out}
#imageviewer::backdrop{background:#000}
#imageviewer img{display:block;width:100%;height:100%;object-fit:contain}
/* -- prompt/caption indicator (2026-09-16): collapsed by default -- a
   small bottom-left pill, never covering the photo, per the operator's own ask.
   Tapping it (IMAGE_VIEWER_JS, which stops the click from reaching the
   dialog's own click-anywhere-closes) swaps to .expanded, which grows
   the same element into a real, scrollable text box; tapping it again
   (or the image, or anywhere else) collapses/closes as before. -- */
#imageviewerprompt{position:absolute;left:1rem;bottom:max(.75rem,env(safe-area-inset-bottom));
  max-width:min(75vw,26rem);padding:.4rem .75rem;border-radius:1rem;border:0;
  background:rgba(0,0,0,.65);color:#fff;font:inherit;font-size:.78rem;line-height:1.4;
  text-align:left;cursor:pointer}
#imageviewerprompt[hidden]{display:none}
#imageviewerprompt .iv-text{display:none;max-height:32vh;overflow-y:auto;white-space:pre-wrap}
#imageviewerprompt.expanded{border-radius:.7rem;max-width:min(90vw,32rem)}
#imageviewerprompt.expanded .iv-badge{display:none}
#imageviewerprompt.expanded .iv-text{display:block}
#imageviewerclose{position:absolute;top:max(.75rem,env(safe-area-inset-top));
  right:max(.75rem,env(safe-area-inset-right));width:2.75rem;height:2.75rem;padding:0;
  border:0;border-radius:50%;background:rgba(0,0,0,.65);color:#fff;font-size:1.5rem;cursor:pointer}
#imageviewerprev,#imageviewernext{position:absolute;top:50%;transform:translateY(-50%);width:3rem;height:4.5rem;padding:0;border:0;
  border-radius:.6rem;background:rgba(0,0,0,.55);color:#fff;font-size:2rem;line-height:1;cursor:pointer}
#imageviewerprev{left:max(.5rem,env(safe-area-inset-left))}
#imageviewernext{right:max(.5rem,env(safe-area-inset-right))}
#imageviewerprev[hidden],#imageviewernext[hidden]{display:none}
#imageviewerhint{position:absolute;bottom:max(.75rem,env(safe-area-inset-bottom));
  left:50%;transform:translateX(-50%);margin:0;padding:.3rem .7rem;border-radius:1rem;
  background:rgba(0,0,0,.65);font-size:.8rem;white-space:nowrap;pointer-events:none}

/* -- note viewer: a <dialog>, same top-layer reasoning #imageviewer
   already relies on (2026-09-17, his own ask -- "tapping opens the
   modal, and the modal lets him move between notes"). Unlike
   #imageviewer this holds real, possibly-long TEXT, so it's a centered
   card rather than edge-to-edge black, and .nv-body needs its own
   scroll -- given the exact same bounded-flex-parent shape (flex:0 0
   auto head/foot, flex:1 1 auto min-height:0 overflow-y:auto middle)
   that .board/.board-list and chat's own #msglist already use
   correctly, not a new pattern that could repeat his scroll-trap
   incidents. Being a real <dialog> (browser top-layer) is itself the
   strongest guarantee here: it sits completely outside .shell-main/
   .board-list's own scroll containers, so nothing it does can trap
   THEIR scroll either way. */
/* display:flex is scoped to [open] deliberately (found live, testing
   this exact modal): a bare #noteviewer{display:flex} would override
   the browser's own default `dialog:not([open]){display:none}`,
   leaving it visibly rendered on every page load before showModal() is
   ever called. #imageviewer never had this bug because its own CSS
   never sets `display` at all -- this one does, because it needs
   flex-column for its head/body/foot layout, so the [open] scope is
   what keeps that from also fighting the closed state. */
#noteviewer{position:fixed;inset:0;margin:auto;width:min(92vw,34rem);
  max-height:min(85vh,85dvh);padding:0;border:0;border-radius:16px;
  background:var(--surface);color:var(--text);overflow:hidden}
#noteviewer[open]{display:flex;flex-direction:column}
#noteviewer::backdrop{background:rgba(0,0,0,.6)}
.nv-head{flex:0 0 auto;display:flex;align-items:center;gap:.5rem;padding:12px 14px;
  border-bottom:1px solid var(--border)}
.nv-head-kind{font-size:.7rem;text-transform:uppercase;letter-spacing:.04em;color:var(--text-mute);
  flex:1 1 auto;display:flex;align-items:center;gap:.4rem}
#noteviewerclose{flex:0 0 auto;width:2.25rem;height:2.25rem;padding:0;border:0;border-radius:50%;
  background:var(--surface-2);color:var(--text-dim);font-size:1.2rem;cursor:pointer}
.nv-body{flex:1 1 auto;min-height:0;overflow-y:auto;overscroll-behavior:contain;padding:14px}
#noteviewertitle{margin:0 0 .3rem;font-size:1.05rem}
#noteviewercreator{margin:0 0 .6rem;color:var(--text-mute);font-size:.78rem}
#noteviewertext{white-space:pre-wrap;font-size:.9rem;line-height:1.5;color:var(--text-dim)}
.nv-foot{flex:0 0 auto;display:flex;align-items:center;justify-content:space-between;gap:8px;
  padding:10px 14px;border-top:1px solid var(--border)}
.nv-nav{width:2.5rem;height:2.5rem;padding:0;border:1px solid var(--border);border-radius:50%;
  background:var(--surface-2);color:var(--text);font-size:1.1rem;cursor:pointer}
.nv-nav:disabled{opacity:.35;cursor:default}

/* -- async send: the message is drawn the instant you hit send, not once
   a full page reload comes back. .pending is the "still sending" state
   (dimmed, a small note under the bubble); it's removed the moment a real
   server id comes back, success or failure. .failed is a distinct state
   from .pending, not a reuse of it -- a failed send needs to stay visibly
   different until retried, where pending is expected to resolve itself
   in under a second. -- */
.msg.pending{opacity:.55}
.pending-note{font-size:.68rem;color:var(--text-mute);align-self:flex-end;margin:2px 2px 0}
.msg.failed{background:var(--surface-3);border:1px solid var(--danger)}
.msg-error-row{display:flex;align-items:center;gap:.5rem;align-self:flex-end;margin-top:3px}
.msg-error-text{font-size:.74rem;color:var(--danger)}
.retry-btn{height:28px;padding:0 .7rem;border-radius:8px;border:1px solid var(--danger);background:none;
  color:var(--danger);font:inherit;font-size:.74rem;font-weight:600;cursor:pointer}
.retry-btn:active{background:var(--danger);color:var(--surface)}
.msg.typing{color:var(--text-mute);font-style:italic}
.queued-note{font-size:.7rem;color:var(--text-mute);align-self:flex-start;margin:0 2px}

/* -- desktop hero panel: persistent, whole-portrait, never cropped
   (object-fit:contain -- the circular cover-crop stays reserved for the
   small admin-grid treatment, a different job at a different size).
   Hidden on mobile -- the header chip + peek is the mobile answer. -- */
.hero{display:none}
@media (min-width:900px){
  .hero{display:block;flex:0 0 300px;position:relative;background:var(--surface);border-right:1px solid var(--border)}
  .hero-frame{position:relative;width:100%;height:100%;overflow:hidden;background:var(--surface-3)}
  .hero-img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain}
  .hero-img-cur{animation:heroFadeIn .5s ease-out both}
  .hero-cap{position:absolute;left:0;right:0;bottom:0;padding:.6rem .8rem;color:#fff;text-transform:capitalize;
    background:linear-gradient(transparent,rgba(0,0,0,.55));font-size:.85rem}
  .peek{display:none}
}
@keyframes heroFadeIn{from{opacity:0}to{opacity:1}}

/* -- card board (2026-09-16: the tasks system, WISHLIST.md's "card
   surface" scoped to just tasks -- see tasks.py/_board_panel()). Closed
   by default. The header button opens a desktop side panel or a
   full-screen mobile sheet -- already mobile-first by construction, the
   sheet takes the entire viewport below 900px. Desktop chat remains
   usable alongside it. -- */
.board{background:var(--surface);display:flex;flex-direction:column;min-height:0;position:relative}
.board-head{flex:0 0 auto;padding:14px 16px 10px;border-bottom:1px solid var(--border);display:flex;
  align-items:center;justify-content:space-between}
.board-list{flex:1 1 auto;min-height:0;overflow-y:auto;overscroll-behavior:contain;padding:12px;
  padding-bottom:76px;display:flex;flex-direction:column;gap:10px}
.board-close{display:block;width:36px;height:36px;border-radius:50%;border:0;background:var(--surface-2);
  color:var(--text-dim);font-size:1rem;cursor:pointer}
.board-trackers-btn{position:absolute;left:16px;bottom:16px;height:44px;padding:0 14px;
  border-radius:22px;border:1px solid var(--border);background:var(--surface-2);color:var(--text);
  font:inherit;font-size:.85rem;font-weight:600;display:inline-flex;align-items:center;gap:.4rem;
  text-decoration:none}
@media (max-width:899px){
  .board{position:fixed;inset:0;z-index:50;transform:translateY(100%);visibility:hidden;
    transition:transform .32s cubic-bezier(.4,0,.2,1),visibility .32s;padding-top:env(safe-area-inset-top)}
  .board.open{transform:translateY(0);visibility:visible}
}
@media (min-width:900px){
  .board{display:none;width:280px;flex:0 0 280px;border-left:1px solid var(--border)}
  .board.open{display:flex}
}
/* width/text-align/font/cursor here (2026-09-17) are resets that only
   matter for a <button>-based card -- the note stack's own cards,
   which open the viewer modal on tap rather than navigating (see
   NOTE_VIEWER_JS) -- and are harmless no-ops on the <a>-based task/
   reminder cards, which already behave this way by default. */
.card{background:var(--surface-2);border:1px solid var(--border);border-left:3px solid var(--k-note);
  border-radius:12px;padding:.75rem .85rem;font-size:.86rem;line-height:1.4;display:block;width:100%;
  text-align:left;font:inherit;cursor:pointer;text-decoration:none;color:inherit}
.card-task{border-left-color:var(--k-task)}
.card-task.pri-high{border-left-color:var(--danger)}
.card-task.pri-low{border-left-color:var(--text-mute)}
.card-reminder{border-left-color:var(--k-reminder)}
.card-kind{font-size:.66rem;text-transform:uppercase;letter-spacing:.04em;color:var(--text-mute);
  margin-bottom:.3rem;display:flex;gap:.4rem;flex-wrap:wrap}
.card-title{font-weight:600}
/* -- read vs needs-attention (2026-09-17, his own ask: "bold or
   otherwise clearly marked as waiting on him, versus read ones") --
   the DEFAULT card look (bold title, full opacity) IS the needs-
   attention state, unchanged from before this existed; .card--read is
   the one new, deliberately quieter state, not the other way around,
   since a fresh item demanding attention is the case that should look
   exactly as it always has. -- */
.card--read{opacity:.62}
.card--read .card-title{font-weight:400}
/* -- notes stack (2026-09-17, his own ask: "stack rather than
   listing... only 2-3 visible... an entry point to browsing all of
   them, not a truncated list"). .note-stack is position:relative, in
   the board's own normal flex flow -- it does NOT introduce a second
   scroll container of its own, deliberately (his own scroll-trap
   warning): the real card (in normal flow) sets the stack's height,
   the 1-2 .stack-peek divs behind it are position:absolute WITHIN that
   same box, so nothing here changes what .board-list itself scrolls.
   Peeks are decoration only -- aria-hidden, no pointer events, no tap
   target -- the modal's own prev/next is how he reaches any of them. */
.note-stack{position:relative;margin:0}
/* :not(.stack-peek) here is load-bearing, found live testing this exact
   stack: a peek div is ALSO a .note-stack>.card (both classes at once),
   so without the exclusion this rule's position:relative -- same
   specificity family as .stack-peek's own rule below, but matching
   BOTH selectors -- won every peek back into normal document flow,
   rendering full-height text bands instead of a hidden-behind peek. */
.note-stack>.card:not(.stack-peek){position:relative;z-index:2}
.stack-peek{position:absolute;inset:0;z-index:1;pointer-events:none;padding:.75rem .85rem}
.stack-peek[data-depth='1']{transform:translate(5px,6px) scale(.98);opacity:.7;z-index:1}
.stack-peek[data-depth='2']{transform:translate(10px,12px) scale(.96);opacity:.42;z-index:0}
.note-stack-count{position:absolute;top:-7px;right:-7px;z-index:3;background:var(--accent);
  color:var(--on-accent);font-size:.68rem;font-weight:700;border-radius:99px;line-height:1;
  padding:.22rem .45rem;pointer-events:none}
.card-body{margin-top:.25rem;color:var(--text-dim);font-size:.8rem}
.card-due{color:var(--k-reminder)}
.card-recur{color:var(--text-mute)}
/* -- category (2026-09-16, operator's own addition): a plain, low-key
   pill -- deliberately NOT drawn from the priority/danger/reminder
   accent triad those already mean something else (urgency/kind), so a
   "business" task's badge never reads as more urgent than a "personal"
   one just because of its category. -- */
.card-cat{background:var(--surface-3);color:var(--text-dim);border-radius:99px;
  padding:.05rem .5rem;font-size:.64rem;text-transform:none;letter-spacing:0}
/* -- note (2026-09-16) -- explicit even though .card's own base rule
   already defaults to --k-note (WISHLIST.md's original card sketch
   picked that color for exactly this type before "note" itself existed
   as a real card), so this survives that base rule ever changing later
   without silently going the wrong color. -- */
.card-note{border-left-color:var(--k-note)}
/* -- reminder (2026-09-16) -- the ORIGINAL wishlist placeholder's own
   --k-reminder token, defined 2026-09-11 and unused until this feature
   actually existed to reuse it. -- */
.card-reminder{border-left-color:var(--k-reminder)}
.card-check{display:flex;align-items:center;gap:.5rem;cursor:default;margin-bottom:.3rem}
.card-check input{width:18px;height:18px;accent-color:var(--k-task)}
.board-foot-note{font-size:.74rem;color:var(--text-mute);text-align:center;padding:.6rem 4px 0}
/* -- schedule/task edit-history disclosure (2026-09-15/16) -- plain
   <details>/<summary>, just recolored for the dark theme; no JS needed
   for the expand/collapse itself. -- */
.sched-history{margin-top:.5rem;font-size:.78rem;color:var(--text-dim)}
.sched-history summary{cursor:pointer;color:var(--text-mute);font-size:.74rem}
.sched-history .list-row{padding:.35rem 0;border-bottom:1px solid var(--border)}
.sched-history .list-row:last-child{border-bottom:0}
/* -- the add button: a FAB pinned to the board's own bottom-right,
   opening a small menu of item TYPES rather than acting on tap itself --
   deliberately extensible (the operator's own instruction) even though
   "task" is the only entry today; a second type is a second
   .board-add-item link, nothing structural. -- */
.board-add-wrap{position:absolute;right:16px;bottom:16px;display:flex;flex-direction:column;
  align-items:flex-end;gap:8px}
.board-add-btn{width:52px;height:52px;border-radius:50%;border:0;background:var(--accent);
  color:var(--on-accent);font-size:1.6rem;line-height:1;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.35);
  transition:transform .15s ease}
.board-add-btn[aria-expanded=true]{transform:rotate(45deg)}
.board-add-menu{display:flex;flex-direction:column;gap:6px;align-items:flex-end}
.board-add-item{background:var(--surface-2);border:1px solid var(--border);border-radius:10px;
  padding:.5rem .85rem;font-size:.85rem;color:var(--text);text-decoration:none;white-space:nowrap;
  box-shadow:0 2px 10px rgba(0,0,0,.25)}

@media (prefers-reduced-motion:reduce){.hero-img-cur{animation:none;opacity:1}.board,.peek,.menu-sheet{transition:none}}

/* -- household inventory: the status dot reuses the board card's own
   accent triad (--danger/--k-reminder/--k-task) so "this needs
   attention" reads in the same visual language app-wide, not a fourth
   palette invented just for this page -- */
.status-dot{display:inline-block;width:14px;height:14px;border-radius:50%;flex-shrink:0}

/* -- meal plan: a week of day sections, each holding its 3 meal-type
   rows. The left border is the "is this filled in" signal -- solid
   accent for a planned meal, dashed and muted for a gap -- so scanning
   for "what's missing" doesn't depend on reading every row's text. -- */
.meal-row{display:flex;align-items:center;gap:.5rem;padding:.5rem 0 .5rem .6rem;
  border-bottom:1px solid var(--border);border-left:3px solid var(--accent)}
.meal-row.meal-empty{border-left:3px dashed var(--text-mute)}
.meal-row:last-child{border-bottom:0}
.meal-type-badge{width:60px;flex-shrink:0;font-size:.68rem;text-transform:uppercase;
  letter-spacing:.03em;color:var(--text-mute)}
.week-nav{display:flex;align-items:center;justify-content:space-between;margin-bottom:.2rem}
.week-label{font-weight:600;font-size:.95rem}

/* -- notification permission bar: a slim strip prepended to the chat
   column client-side (see CHAT_JS), never server-rendered -- it only
   ever appears when permission is still 'default', so there's no
   server-side state to get wrong about whether to show it. -- */
.notif-bar{flex:0 0 auto;display:flex;align-items:center;gap:.5rem;padding:.55rem .7rem;
  background:var(--surface-2);border-bottom:1px solid var(--border);font-size:.82rem;color:var(--text-dim)}
.notif-bar span{flex:1}
.notif-bar button{height:32px;padding:0 .7rem;border-radius:8px;border:1px solid var(--border);
  background:var(--surface-3);color:var(--text);font:inherit;font-size:.78rem;font-weight:600;cursor:pointer}
.notif-bar button.btn-ghost{background:none;border-color:transparent;color:var(--text-mute)}
"""

# PWA head tags -- on every page (including pre-login: setup/login/invite),
# not just the authenticated shell, so installability doesn't depend on
# having signed in first. theme-color matches the header's own --surface,
# not the darker page --bg, so the OS status bar blends with the actual
# header bar an installed window shows, not the canvas behind it.
PWA_HEAD = (
    "<meta name=theme-color content='#15171c'>"
    "<link rel=manifest href='/manifest.webmanifest'>"
    "<link rel=icon href='/icon-192.png' type='image/png'>"
    "<link rel=apple-touch-icon href='/icon-180.png'>"
)

# Registers the service worker on every page for the same reason PWA_HEAD
# is everywhere -- a household member installing from the login screen,
# before their own session exists, still needs this to have run. The SW
# itself (_SW_JS below) does no caching at all, so there is deliberately
# nothing here to invalidate/version across a login/logout on shared
# hardware -- see _SW_JS's own docstring-equivalent comment for why that
# was a hard requirement, not a simplification.
PWA_JS = (
    "<script>if('serviceWorker' in navigator){"
    "navigator.serviceWorker.register('/sw.js').catch(function(){});}</script>"
)

# Ported from a sibling application's proven version -- real VAPID push was rejected
# there since a self-hosted box can't guarantee it's reachable to push TO,
# so it buys nothing over polling for an app that's open or recently
# backgrounded anyway. Deliberately has NO
# fetch handler and caches nothing -- this app is multi-user, installed on
# possibly-shared hardware, and a service worker that cached so much as one
# authenticated page could serve one household member's data to another
# from the cache alone. Install/activate/notification-click focus only.
_SW_JS = (
    "self.addEventListener('install',function(e){self.skipWaiting();});"
    "self.addEventListener('activate',function(e){e.waitUntil(self.clients.claim());});"
    "self.addEventListener('notificationclick',function(e){"
    "e.notification.close();"
    "e.waitUntil(self.clients.matchAll({type:'window',includeUncontrolled:true}).then(function(cs){"
    "for(var i=0;i<cs.length;i++){if('focus' in cs[i])return cs[i].focus();}"
    "if(self.clients.openWindow)return self.clients.openWindow('/');"
    "}));"
    "});"
)

# The settings/notify tab is server-rendered and can't know the one thing
# that actually decides whether notifications work: this browser's real
# Notification.permission. A denied or still-default permission looked, from
# the UI alone, identical to a working setup -- found diagnosing the same gap
# in a sibling application's own server.py. No-ops on every other page
# (the element it targets only exists on the notify tab).
NOTIFY_STATUS_JS = (
    "<script>(function(){"
    "var el=document.getElementById('notif-permission-text');if(!el)return;"
    "if(!('Notification' in window)){"
    "el.innerHTML=\"<span class=muted>not supported</span> \\u2014 this browser doesn't offer notifications here.\";return;}"
    "var p=Notification.permission;"
    "if(p==='granted'){el.innerHTML=\"<b>granted</b> \\u2014 this browser/device will show them.\";}"
    "else if(p==='denied'){el.innerHTML=\"<span class=err>blocked</span> \\u2014 notifications were declined for this "
    "site. Re-enable them in this browser's site settings for this page (on iOS: Settings \\u2192 the installed app "
    "\\u2192 Notifications, or remove it from your Home Screen and add it again), then reload this page.\";}"
    "else{el.innerHTML=\"<span class=muted>not yet granted</span> \\u2014 go to the chat screen, you'll see a prompt "
    "to turn them on there.\";}"
    "})();</script>"
)

# /history's own jump-to-result centering (2026-09-19, real bug: the
# highlighted match got a CSS class but nothing ever scrolled to it --
# the browser just loaded the page at the top). Fires on every /history
# render, not just a search -- the .hist-hit guard makes it a no-op on
# an ordinary browse/paginate load where nothing is highlighted, so this
# doesn't need a second code path for "did this request come from a
# search."  Centers in the space actually left AFTER .hist-sticky (the
# search form + match-nav, now sticky -- see that class's own CSS
# comment) rather than the whole viewport, using .hist-sticky's real
# measured height, not a guessed constant -- centering by raw viewport
# height would put the target's midpoint underneath the sticky bar by
# however tall it is, exactly the bug he flagged. Runs twice (immediately
# and again on window 'load') for the same reason a sibling application's own
# initialChatPosition() does -- an image inside a message row can still
# be loading when this first runs, and its own layout only settles once
# it does.
HISTORY_JUMP_JS = (
    "<script>(function(){"
    "function centerHistoryHit(){"
    "var hit=document.querySelector('.hist-hit');if(!hit)return;"
    "var scroller=document.querySelector('.shell-main');if(!scroller)return;"
    "var sticky=document.querySelector('.hist-sticky');"
    "var stickyH=sticky?sticky.offsetHeight:0;"
    "var availableH=scroller.clientHeight-stickyH;"
    "var rect=hit.getBoundingClientRect();"
    "var targetCenter=rect.top+rect.height/2;"
    "var desiredCenter=stickyH+availableH/2;"
    "scroller.scrollTop+=(targetCenter-desiredCenter);"
    "}"
    "centerHistoryHit();"
    "window.addEventListener('load',centerHistoryHit);"
    "})();</script>"
)

# The visualViewport shim -- the part 100dvh alone doesn't reliably solve.
# Behavior genuinely varies across mobile browsers when the on-screen
# keyboard opens; rather than trust one CSS unit to cover all of them, this
# sets the shell's real pixel height from the actual visual viewport on
# every resize/scroll of it, so the composer is always within whatever
# space is genuinely left, keyboard included.
KEYBOARD_JS = (
    "<script>(function(){"
    "function sync(){if(!window.visualViewport)return;"
    "document.body.style.height=window.visualViewport.height+'px';"
    "document.body.style.setProperty('--app-height',window.visualViewport.height+'px');}"
    "if(window.visualViewport){sync();"
    "visualViewport.addEventListener('resize',sync);"
    "visualViewport.addEventListener('scroll',sync);}"
    "})();</script>"
)

# Shared interaction script for every app-shell page: the header's overflow
# menu, the board sheet (mobile), and the avatar peek (chat page only --
# harmless no-op elsewhere since the elements it looks for won't exist).
APP_JS = (
    "<script>"
    "function $(i){return document.getElementById(i);}"
    "function nbBackdrop(show){var bd=$('nbBackdrop');if(!bd){bd=document.createElement('div');"
    "bd.id='nbBackdrop';bd.className='nb-backdrop';bd.onclick=nbCloseAll;document.body.appendChild(bd);}"
    "bd.classList.toggle('show',show);}"
    "function nbCloseAll(){var m=$('menuSheet'),b=$('board'),p=$('peek');"
    "clearTimeout(window.__peekT);window.__peekManual=false;"
    "if(m)m.classList.remove('show');if(b)b.classList.remove('open');if(p)p.classList.remove('show');"
    "var bb=$('boardBtn');if(bb)bb.setAttribute('aria-expanded','false');"
    "var am=$('boardAddMenu');if(am)am.hidden=true;"
    "var abtn=$('boardAddBtn');if(abtn)abtn.setAttribute('aria-expanded','false');"
    # closeVoiceModal (defined in CHAT_JS, chat_page only) is exposed on
    # window precisely so this shared, page-wide function -- Escape,
    # opening the menu/board elsewhere -- can also close voice mode
    # without APP_JS needing to know CHAT_JS exists at all.
    "if(window.closeVoiceModal)window.closeVoiceModal();"
    "nbBackdrop(false);}"
    "function nbShowPeek(manual){var p=$('peek');if(!p||window.matchMedia('(min-width:900px)').matches)return;"
    "if(!manual&&(window.__peekManual||$('menuSheet')?.classList.contains('show')||$('board')?.classList.contains('open')))return;"
    "if(manual){nbCloseAll();window.__peekManual=true;nbBackdrop(true);}"
    # Never let the popup open behind (or squeezed above) the on-screen
    # keyboard (2026-09-14) -- blur whatever's currently focused, almost
    # always the composer, so the keyboard actually dismisses and the
    # popup gets the real full viewport (var(--app-height) already
    # shrinks for the keyboard, but that's not the same as the keyboard
    # actually being gone, which is what he wants to see). Covers both
    # triggers: tapping the avatar chip while the composer happens to be
    # focused, and a real state change landing while he's actively typing.
    "if(document.activeElement&&document.activeElement.blur)document.activeElement.blur();"
    "p.classList.add('show');clearTimeout(window.__peekT);"
    "if(!manual)window.__peekT=setTimeout(function(){p.classList.remove('show');},4000);}"
    "function nbToggle(which){var el=which==='menu'?$('menuSheet'):$('board');if(!el)return;"
    "var open=which==='menu'?el.classList.contains('show'):el.classList.contains('open');"
    "nbCloseAll();if(!open){el.classList.add(which==='menu'?'show':'open');"
    "if(which==='board'&&$('boardBtn'))$('boardBtn').setAttribute('aria-expanded','true');"
    # refreshBoard (defined in CHAT_JS, chat_page only -- the only page
    # with a board at all) is exposed on window for the same reason
    # closeVoiceModal is above: this shared toggle shouldn't need to know
    # CHAT_JS exists. Opening the board always fetches current state
    # rather than trusting what was rendered at page load (2026-09-16).
    "if(which==='board'&&window.refreshBoard)window.refreshBoard();"
    "nbBackdrop(which==='menu'||!window.matchMedia('(min-width:900px)').matches);}}"
    # Bound here via addEventListener, never an inline onclick= HTML
    # attribute -- CSP's script-src 'unsafe-inline' covers a <script>
    # block's OWN contents (this file) but does not extend to inline
    # event-handler attributes in the markup those scripts render. Found by
    # actually clicking the board/menu buttons in a CSP-enforcing browser:
    # the avatar chip (already wired this way) worked, the onclick=
    # attributes silently didn't -- not assumed from reading the spec.
    "var mb=$('menuBtn');if(mb)mb.addEventListener('click',function(){nbToggle('menu');});"
    "var bb=$('boardBtn');if(bb)bb.addEventListener('click',function(){nbToggle('board');});"
    "var bc=$('boardClose');if(bc)bc.addEventListener('click',function(){nbToggle('board');});"
    # The add menu is its own small toggle, deliberately not routed
    # through nbToggle/nbCloseAll's "only one sheet open at a time" logic
    # -- it lives INSIDE the already-open board, not as a sibling
    # surface competing with it, so opening it must not close the board
    # itself. It still gets closed by nbCloseAll's own board comment above)
    # whenever anything else closes the board.
    "(function(){var bab=$('boardAddBtn'),bam=$('boardAddMenu');if(!bab||!bam)return;"
    "bab.addEventListener('click',function(e){e.stopPropagation();var open=!bam.hidden;"
    "bam.hidden=open;bab.setAttribute('aria-expanded',open?'false':'true');});})();"
    # Info tooltip (2026-09-18, design pass) -- one shared toggle for
    # every .info-btn on every page, event-delegated on document.body
    # (same shape nbToggle's own listeners use) rather than a
    # per-instance addEventListener, so a page that adds one later
    # needs zero JS wiring of its own. Click elsewhere, or another
    # .info-btn, closes whatever's open first -- only one popover open
    # at a time, same "one sheet" discipline nbCloseAll already applies
    # to menu/board/peek.
    # positionTip (2026-09-18, found live testing the very first real
    # usage of this component): the popover defaults to left:0 in CSS,
    # which clips off the RIGHT edge of the viewport whenever its own
    # .info-btn sits in the right half of a narrow screen -- the common
    # case, not a rare one, since the icon usually trails the end of a
    # line of text. First fix flipped to right:0 in that case -- but that
    # in turn clips the LEFT edge whenever the anchor is close enough to
    # the left that the popover's own width overshoots past x=0 (found
    # live testing settings/chatvoice's "tool call rounds -- live chat"
    # tip, whose icon sits left-of-center on a narrow screen). Fixed by
    # computing an explicit px offset that clamps BOTH edges into the
    # viewport instead of ping-ponging between two fixed sides.
    "function positionTip(pop){pop.style.left='0';"
    "var r=pop.getBoundingClientRect();var left=0;"
    "var over=r.right-(window.innerWidth-8);if(over>0)left=-over;"
    "if(r.left+left<8)left=8-r.left;"
    "pop.style.left=left+'px';}"
    "function closeAllTips(){document.querySelectorAll('.info-btn[aria-expanded=true]')"
    ".forEach(function(b){b.setAttribute('aria-expanded','false');"
    "var p=b.nextElementSibling;if(p){p.style.left='';}});}"
    "document.body.addEventListener('click',function(e){"
    "var btn=e.target.closest('.info-btn');"
    "if(btn){var open=btn.getAttribute('aria-expanded')==='true';closeAllTips();"
    "if(!open){btn.setAttribute('aria-expanded','true');"
    "var p=btn.nextElementSibling;if(p)positionTip(p);}"
    "e.stopPropagation();return;}"
    "closeAllTips();});"
    # Hover (desktop only, mirrors the CSS @media(hover:hover) rule)
    # needs the identical positioning fix -- CSS alone can't measure
    # the viewport, so this is the one place TIP_JS has to run even
    # though the show/hide itself stays pure CSS.
    "document.body.addEventListener('mouseenter',function(e){"
    "var wrap=e.target.closest&&e.target.closest('.info-wrap');if(!wrap)return;"
    "var p=wrap.querySelector('.info-pop');if(p)positionTip(p);}, true);"
    "document.addEventListener('keydown',function(e){if(e.key==='Escape'){nbCloseAll();closeAllTips();}});"
    # data-confirm on a <form> (2026-09-18, design pass) -- found
    # sweeping the file for inline event-handler attributes while
    # checking the CSP on the new tooltip component: three destructive
    # actions (note delete, schedule delete, context-tuning reset) used
    # inline onclick=/onsubmit="return confirm(...)" attributes, which
    # this app's own script-src 'unsafe-inline' does NOT cover -- same
    # gap already found and fixed once before for onclick= on the
    # board/menu buttons (see nbToggle's own comment), just missed on
    # these three since they predated that fix. All three silently
    # skipped their confirmation dialog and deleted/reset immediately.
    # One shared, delegated submit listener replaces all three rather
    # than three separate fixes, and covers any future one for free.
    "document.body.addEventListener('submit',function(e){"
    "var msg=e.target.getAttribute('data-confirm');"
    "if(msg&&!confirm(msg))e.preventDefault();});"
    "window.matchMedia('(min-width:900px)').addEventListener('change',nbCloseAll);"
    "document.querySelectorAll('.settings-tabs').forEach(function(row){var active=row.querySelector('.active');"
    "if(active)row.scrollLeft=Math.max(0,active.offsetLeft-row.clientWidth+active.offsetWidth+2);});"
    "(function(){var chip=$('avatarChip'),peek=$('peek');if(chip&&peek){"
    "chip.addEventListener('click',function(){if(peek.classList.contains('show'))nbCloseAll();else nbShowPeek(true);});"
    "peek.addEventListener('click',nbCloseAll);"
    "if(window.__noriStateChanged__)nbShowPeek(false);"
    "}})();"
    "</script>"
)

# chat_page only -- the async send/typing/retry/poll machinery. Ported
# from a sibling application's proven send flow -- a sibling application hit exactly these
# problems -- a second concurrent turn from two tabs, a silent-looking
# 90s wait that made people reload and double-send -- and this reuses
# those fixes rather than rediscovering them, with one real
# addition a sibling application doesn't have: a genuine per-message error+retry
# state, not a toast that disappears. Reads window.NORI_CHAT (csrf/state/
# last_id/colors), set by an inline script chat_page emits before this.
CHAT_JS = (
    "<script>(function(){"
    "var cfg=window.NORI_CHAT||{};"
    "var msglist=document.getElementById('msglist');"
    "var CSRF=cfg.csrf,last=cfg.last_id||0,curState=cfg.state,COLORS=cfg.colors||{};"
    "var seenIds=new Set();"
    "var sending=false,polling=false,unread=0;"
    "var voiceModalOpen=false;"  # set/read by the voice-mode block far below; declared here since addMessage (right below) already needs to check it
    "function stateColor(s){return COLORS[s]||'#95a5a6';}"
    "function setSendDisabled(on){var b=document.getElementById('sendBtn'),i=document.getElementById('msgInput');"
    "if(b)b.disabled=on;if(i)i.disabled=on;}"

    # Auto-growing composer (2026-09-13): the textarea grows with content
    # up to the CSS max-height cap, then scrolls internally instead of
    # growing further -- 'auto' before measuring scrollHeight is required
    # (not optional): without collapsing the height first, scrollHeight
    # only ever reports the CURRENT (already-tall) box's content height,
    # which never shrinks back down on delete. overflowY is toggled by
    # comparing the natural (uncapped) scrollHeight against the same cap
    # the CSS uses, so a scrollbar only appears once content genuinely
    # exceeds it, never while still growing toward it. scrollDown() (not
    # scrollDown(true)) at the end re-follows the chat to bottom ONLY if
    # already pinned there -- same rule a new message follows -- so the
    # composer growing taller doesn't yank someone back down mid-read.
    "var msgInput=document.getElementById('msgInput');"
    "var MSG_MAX_H=160;"
    "function autoGrowComposer(){if(!msgInput)return;"
    "msgInput.style.height='auto';"
    "var full=msgInput.scrollHeight;"
    "msgInput.style.height=Math.min(full,MSG_MAX_H)+'px';"
    "msgInput.style.overflowY=full>MSG_MAX_H?'auto':'hidden';"
    "scrollDown();}"
    "if(msgInput){msgInput.addEventListener('input',autoGrowComposer);autoGrowComposer();}"

    # Guards the composer's own post-send refocus against undoing
    # nbShowPeek's blur (2026-09-14) -- her reply, which can carry a new
    # state, arrives from the same send that's about to refocus the
    # composer at its own tail; an unconditional inp.focus() there would
    # bring the keyboard straight back up while the popup is still shown.
    # Skips refocusing only while the popup is actually visible;
    # otherwise behaves exactly like the old bare inp.focus().
    "function refocusComposerUnlessPeeking(){var p=$('peek');"
    "if(p&&p.classList.contains('show'))return;"
    "if(msgInput)msgInput.focus();}"

    # Enter vs. newline (2026-09-13): a phone's on-screen keyboard has no
    # Shift key, so Enter-sends-Shift+Enter-newlines (the desktop
    # convention) would make a newline unreachable on mobile -- the
    # composer would functionally regress to the old single-line input
    # for anyone on a touch device. Picked the split the operator asked for
    # instead: on a coarse (touch) pointer, Enter always inserts a
    # newline -- native <textarea> behavior, no JS needed -- and sending
    # is button-only; on a fine (mouse/trackpad) pointer, plain Enter
    # sends (matching the desktop convention people expect from every
    # other chat app) and Shift+Enter still inserts a newline, also
    # native, so only plain Enter needs intercepting. matchMedia
    # (pointer:coarse) is checked once, not per keystroke, and used
    # instead of a viewport-width check because it reflects the actual
    # input hardware -- a maximized desktop window and a foldable tablet
    # can both be "wide", but only one of them has a real Shift key.
    # isComposing guards IME composition (e.g. entering Japanese/Chinese
    # text) -- Enter confirming a candidate there must never also send.
    "var isCoarsePointer=!!(window.matchMedia&&window.matchMedia('(pointer:coarse)').matches);"
    "if(msgInput)msgInput.addEventListener('keydown',function(e){"
    "if(isCoarsePointer)return;"
    "if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();"
    "var f=document.getElementById('composerForm');if(!f)return;"
    "if(f.requestSubmit)f.requestSubmit();else f.dispatchEvent(new Event('submit',{cancelable:true}));}});"

    # Auto-scroll -- ported from a sibling application (found and fixed there today):
    # only auto-follow when already pinned near the bottom, so a message
    # landing while you're scrolled up to read history doesn't yank you
    # back down. NEAR_BOTTOM_PX=200 (not 0) on purpose -- "close enough
    # that the newest message is already basically in view", the same
    # threshold a sibling application settled on after 60px proved too tight in
    # practice. setPinned() is the one place both the auto-follow flag and
    # the scroll-button's visibility change together, so they can't drift
    # apart. The double requestAnimationFrame (not a single one, and not a
    # setTimeout) waits for the browser to actually finish laying out the
    # just-appended content before reading scrollHeight -- a single rAF
    # can still fire a frame early on some browsers, per a sibling application's own
    # notes on why its first attempt at this was occasionally short by
    # one message's height.
    "var pinned=true;var NEAR_BOTTOM_PX=200;"
    "function setPinned(v){pinned=v;var btn=document.getElementById('scrollbtn');if(btn)btn.hidden=pinned;}"
    "function scrollDown(force){if(force)setPinned(true);if(!pinned||!msglist)return;"
    "requestAnimationFrame(function(){requestAnimationFrame(function(){msglist.scrollTop=msglist.scrollHeight;});});}"
    "if(msglist)msglist.addEventListener('scroll',function(){"
    "setPinned(msglist.scrollHeight-msglist.scrollTop-msglist.clientHeight<NEAR_BOTTOM_PX);});"
    "var scrollBtn=document.getElementById('scrollbtn');"
    "if(scrollBtn)scrollBtn.addEventListener('click',function(){scrollDown(true);});"

    # addMessage is the ONE place a message ever reaches the DOM live, and
    # the dedup point -- ported directly from a sibling application's seenIds pattern:
    # a message with a real (server-assigned) id is only ever drawn once,
    # ever, so a poll() catching up on a message this same tab already
    # drew optimistically (or a sweep answer arriving twice) can't double-
    # render it. A message with id==null (our own optimistic echo, before
    # the server has answered) is never added to seenIds and always draws.
    # Copy button (2026-09-15, operator's own ask) -- assistant messages
    # only, both entry points (server-rendered history on page load, and
    # this live addMessage() path) build the exact same element via this
    # one helper, so the delegated click handler below never needs to
    # care which path drew a given bubble. The raw text rides in
    # dataset.copy -- read back verbatim, never re-scraped from the
    # bubble's own rendered text (which would have to skip over this
    # very button's own label to get it right).
    "function makeCopyBtn(text){var btn=document.createElement('button');"
    "btn.type='button';btn.className='copy-btn';btn.setAttribute('aria-label','Copy message');"
    "btn.dataset.copy=text;btn.textContent='\\u{1F4CB}';return btn;}"
    # Debug panel (2026-09-30, operator's own ask) -- mirrors
    # server.py's own _debug_text()/_debug_btn() line for line so the
    # server-rendered history and this live path show identical content;
    # info-wrap/info-btn/info-pop reuse the same generic toggle (TIP_JS,
    # above) that every settings-page info_tip() already relies on --
    # zero new JS wiring needed for the toggle itself.
    "function debugText(d){var lines=["
    "'Provider: '+d.provider_label+' ('+d.provider_type+')',"
    "'Model: '+d.model_alias+' ('+d.model_name+')',"
    "'Position: '+d.chain_position+' of '+d.chain_length,"
    "'Request ID: '+(d.request_id||'\\u2014'),"
    "'Time: '+new Date((d.ts||Date.now()/1000)*1000).toLocaleString(),"
    "'Latency: '+d.latency_ms+' ms',"
    "'Tokens: '+(d.prompt_tokens||0)+' in / '+(d.completion_tokens||0)+' out',"
    "'Cost: '+(d.cost_unavailable?'unavailable':'$'+(d.cost_usd||0).toFixed(4))];"
    "if(d.failed_attempts&&d.failed_attempts.length){lines.push('Failed attempts:');"
    "d.failed_attempts.forEach(function(f){lines.push('  '+f.alias+' ('+f.provider_type+'): '+f.error);});}"
    "if(d.tool_calls&&d.tool_calls.length){lines.push('Tool calls:');"
    "d.tool_calls.forEach(function(c){lines.push('  round '+c.round+': '+c.name+'('+JSON.stringify(c.args)+')');});}"
    "if(d.reasoning){lines.push('Reasoning:');lines.push(d.reasoning);}"
    "return lines.join('\\n');}"
    "function makeDebugBtn(d){var wrap=document.createElement('span');wrap.className='info-wrap';"
    "var btn=document.createElement('button');btn.type='button';btn.className='info-btn';"
    "btn.setAttribute('aria-expanded','false');btn.setAttribute('aria-label','Message info');"
    "btn.textContent='\\u24d8';"
    "var pop=document.createElement('span');pop.className='info-pop dbg-pop';pop.textContent=debugText(d);"
    "wrap.appendChild(btn);wrap.appendChild(pop);return wrap;}"
    # msgActions (2026-09-30) -- wraps whichever of copy/debug actually
    # apply to this message in one flex row (see .msg-actions CSS) so two
    # buttons sit side by side instead of each forcing its own block-level
    # line the way .copy-btn's display:block would otherwise stack them.
    "function msgActions(m){var has=false;var wrap=document.createElement('span');"
    "wrap.className='msg-actions';"
    "if(m.role==='assistant'&&m.content){wrap.appendChild(makeCopyBtn(m.content));has=true;}"
    "if(m.role==='assistant'&&m.meta&&m.meta.debug){wrap.appendChild(makeDebugBtn(m.meta.debug));has=true;}"
    "return has?wrap:null;}"
    # navigator.clipboard needs a secure context (CSP doesn't gate it --
    # it's not a script-src/connect-src resource load at all, just a
    # browser permission tied to https/localhost) -- Nori's only ever
    # served over https or through the tunnel, so the real-world gap this
    # covers is an OLDER browser without the Clipboard API at all, not a
    # CSP block. document.execCommand('copy') is deprecated but still
    # works everywhere that lacks navigator.clipboard, and needs nothing
    # from CSP either -- it's a synchronous DOM operation, not a network
    # or script load.
    "function copyMessageText(text){"
    "if(navigator.clipboard&&window.isSecureContext)return navigator.clipboard.writeText(text);"
    "return new Promise(function(resolve,reject){try{"
    "var ta=document.createElement('textarea');ta.value=text;"
    "ta.style.position='fixed';ta.style.opacity='0';ta.style.left='-9999px';"
    "document.body.appendChild(ta);ta.focus();ta.select();"
    "var ok=document.execCommand('copy');document.body.removeChild(ta);"
    "ok?resolve():reject(new Error('copy failed'));"
    "}catch(e){reject(e);}});}"
    "document.body.addEventListener('click',function(e){"
    "var btn=e.target.closest('.copy-btn');if(!btn)return;"
    "var text=btn.dataset.copy||'';"
    "copyMessageText(text).then(function(){"
    "btn.classList.add('copied');btn.textContent='\\u2713';"
    "setTimeout(function(){btn.classList.remove('copied');btn.textContent='\\u{1F4CB}';},1400);"
    "}).catch(function(){"
    "btn.textContent='\\u2715';"
    "setTimeout(function(){btn.textContent='\\u{1F4CB}';},1400);"
    "});"
    "});"

    # toolRunLine mirrors chat_page's own _tool_run_line exactly (same cap,
    # same "and N more" fallback, same wording) -- one server-rendered
    # history and one live-arriving path, same collapsed-run look either
    # way (2026-09-18, operator's own ask).
    "var TOOL_RUN_SHOW_MAX=5;"
    "function toolRunLine(names){"
    "if(names.length<=TOOL_RUN_SHOW_MAX)return 'used '+names.join(', ');"
    "return 'used '+names.slice(0,TOOL_RUN_SHOW_MAX).join(', ')+' and '+(names.length-TOOL_RUN_SHOW_MAX)+' more';}"

    "function addMessage(m){"
    "if(m.id!=null){if(seenIds.has(m.id))return null;seenIds.add(m.id);if(m.id>last)last=m.id;}"
    "if(m.kind==='tool'&&(m.content||'').trim()==='used set_emotion')return null;"
    "if(m.kind==='tool'){"
    "var name=(m.content||'').replace(/^used /,'');"
    # _toolNames gates the merge, not just the className -- photoFailedNote
    # reuses .toolline for an unrelated "photo failed" note, and that one
    # must never get a tool name silently appended into it.
    "var last_=msglist.lastElementChild;"
    "if(last_&&last_.className==='toolline'&&last_._toolNames){"
    "last_._toolNames.push(name);last_.textContent=toolRunLine(last_._toolNames);"
    "scrollDown();return last_;}"
    "var t=document.createElement('div');t.className='toolline';t._toolNames=[name];"
    "t.textContent=toolRunLine([name]);msglist.appendChild(t);scrollDown();return t;}"
    "if(m.role==='assistant'&&m.emotion){var meta=document.createElement('div');meta.className='msg-meta';"
    "var dot=document.createElement('i');dot.style.background=stateColor(m.emotion);"
    "meta.appendChild(dot);meta.appendChild(document.createTextNode(m.emotion));msglist.appendChild(meta);}"
    "if(m.kind==='image'){var b=document.createElement('div');b.className='msg '+m.role+' msg-image';"
    "if(m.id!=null)b.id='message-'+m.id;"
    # Same .chatimage button IMAGE_VIEWER_JS opens the modal from
    # (2026-09-15) -- one click target, server-rendered history and this
    # live path both build it the same way.
    "var btn=document.createElement('button');btn.type='button';btn.className='chatimage';"
    "btn.setAttribute('aria-label','View image full screen');"
    "if(m.meta&&m.meta.prompt)btn.dataset.prompt=m.meta.prompt;"
    "var img=document.createElement('img');img.loading='lazy';img.alt='';"
    # img_src (2026-09-15, uploadChatPhoto's own optimistic echo) -- a
    # local blob: URL, drawn before the server has assigned a real
    # file_id at all. Every other caller (a real stored message, from
    # the initial page render or poll()) has no img_src and falls
    # through to the ordinary /image/<file_id> route, unchanged.
    "img.src=m.img_src||('/image/'+((m.meta&&m.meta.file_id)||''));"
    "btn.appendChild(img);b.appendChild(btn);"
    "if(m.content){var cap=document.createElement('div');cap.className='msg-caption';"
    "cap.textContent=m.content;b.appendChild(cap);"
    "if(m.role==='assistant')b.appendChild(makeCopyBtn(m.content));}"
    "msglist.appendChild(b);scrollDown();"
    "if(voiceModalOpen){vAppendTurn(m.role,m.content);if(m.role==='assistant')vSpeak(m.id);}"
    "return b;}"
    "var b=document.createElement('div');b.className='msg '+m.role;"
    "if(m.id!=null)b.id='message-'+m.id;"
    "b.textContent=m.content;"
    "var acts=msgActions(m);if(acts)b.appendChild(acts);"
    "msglist.appendChild(b);scrollDown();"
    # Voice mode mirror -- the ONE hook that makes voice mode's transcript
    # and auto-playback work regardless of what triggered the message
    # (a reply to something just spoken, a poll picking up a proactive
    # ping, a peer-driven message) -- see vAppendTurn/vSpeak below. Never
    # for tool lines (those return earlier, above, and never reach here).
    "if(voiceModalOpen){vAppendTurn(m.role,m.content);if(m.role==='assistant')vSpeak(m.id);}"
    "return b;}"

    # -- casual chat photos (2026-09-15, ported from a sibling application's identical
    # mechanism -- attach button, paste, drag-and-drop). Picking/pasting/
    # dropping a photo only STAGES it (setPendingAttachment) -- the actual
    # upload happens from the composer's own submit handler, at the same
    # moment as any other send, carrying whatever's in the text box as the
    # caption. This is what makes it one message, not two: select a photo,
    # type a caption, hit send once.
    "var pendingAttachment=null;"
    "function setPendingAttachment(file){"
    "if(pendingAttachment)URL.revokeObjectURL(pendingAttachment.url);"
    "var url=URL.createObjectURL(file);pendingAttachment={file:file,url:url};"
    "var thumb=$('composerAttachThumb');if(thumb)thumb.src=url;"
    "var wrap=$('composerAttach');if(wrap)wrap.hidden=false;"
    "refocusComposerUnlessPeeking();}"
    "function clearPendingAttachment(){"
    "if(pendingAttachment)URL.revokeObjectURL(pendingAttachment.url);pendingAttachment=null;"
    "var wrap=$('composerAttach');if(wrap)wrap.hidden=true;"
    "var thumb=$('composerAttachThumb');if(thumb)thumb.src='';}"
    "var attachRemoveBtn=$('composerAttachRemove');"
    "if(attachRemoveBtn)attachRemoveBtn.addEventListener('click',clearPendingAttachment);"
    "var attachBtn=$('attachBtn'),fileInput=$('fileInput');"
    "if(attachBtn&&fileInput){attachBtn.addEventListener('click',function(){fileInput.click();});"
    "fileInput.addEventListener('change',function(e){var f=e.target.files[0];e.target.value='';"
    "if(f)setPendingAttachment(f);});}"
    "if(msgInput)msgInput.addEventListener('paste',function(e){"
    "var items=e.clipboardData&&e.clipboardData.items;if(!items)return;"
    "for(var i=0;i<items.length;i++){var it=items[i];"
    "if(it.type&&it.type.indexOf('image/')===0){var f=it.getAsFile();"
    "if(f){e.preventDefault();setPendingAttachment(f);break;}}}});"
    "if(msglist){"
    "msglist.addEventListener('dragover',function(e){e.preventDefault();msglist.classList.add('dragover');});"
    "msglist.addEventListener('dragleave',function(){msglist.classList.remove('dragover');});"
    "msglist.addEventListener('drop',function(e){e.preventDefault();msglist.classList.remove('dragover');"
    "var f=e.dataTransfer&&e.dataTransfer.files&&e.dataTransfer.files[0];"
    "if(f&&f.type&&f.type.indexOf('image/')===0)setPendingAttachment(f);});}"

    # A photo failing to upload gets a plain muted note, not the
    # retry-button error UI showError/settleSent use for a failed text
    # send -- a failed upload never stored a message and there's no
    # stored file left to retry against (a sibling application's own chatPhotoFailed
    # is exactly this same simpler treatment, same reasoning).
    "function photoFailedNote(msg){var n=document.createElement('div');n.className='toolline';"
    "n.textContent='photo failed — '+msg;msglist.appendChild(n);scrollDown();}"

    "async function uploadChatPhoto(file,caption){"
    "if(!file||sending)return;sending=true;setSendDisabled(true);"
    "var blobUrl=URL.createObjectURL(file);"
    "var b=addMessage({role:'user',kind:'image',content:caption||'',id:null,img_src:blobUrl,meta:{}});"
    "b.classList.add('pending');scrollDown(true);"
    "var note=document.createElement('div');note.className='pending-note';note.textContent='sending…';"
    "b.insertAdjacentElement('afterend',note);typing(true);"
    "try{"
    "var fd=new FormData();fd.append('csrf',CSRF);fd.append('file',file);"
    "if(caption)fd.append('caption',caption);"
    "var r=await fetch('/chat-photo',{method:'POST',body:fd});"
    "var d=await r.json();typing(false);"
    "if(!d.ok){note.remove();b.classList.remove('pending');photoFailedNote(d.error||'unknown error');}"
    "else{"
    "if(d.user_id!=null){seenIds.add(d.user_id);b.id='message-'+d.user_id;if(d.user_id>last)last=d.user_id;}"
    "note.remove();b.classList.remove('pending');"
    # The vision reading only exists once the server call finishes, so it
    # can't have been in the optimistic echo above -- append it to that
    # SAME bubble now rather than waiting for the next poll() (which, by
    # the seenIds convention above, would never redraw our own message
    # at all).
    "if(d.own_vision){var vd=document.createElement('div');vd.className='msg-caption';"
    "vd.textContent=d.own_vision.vision_description;b.appendChild(vd);}"
    "if(d.queued){var qn=document.createElement('div');qn.className='queued-note';"
    "qn.textContent=\"sent — she's still answering something else, this lands with it\";"
    "b.insertAdjacentElement('afterend',qn);}"
    "else{(d.messages||[]).forEach(function(m){addMessage(m);maybeNotify(m);});"
    "if(d.state)applyState(d.state);}}}"
    "catch(err){typing(false);note.remove();b.classList.remove('pending');photoFailedNote('connection problem');}"
    "sending=false;setSendDisabled(false);refocusComposerUnlessPeeking();}"

    # The typing indicator -- ported verbatim (threshold included): a
    # silent ". . ." across a genuinely slow provider call reads as hung,
    # which is what made people reload and send again in a sibling application. After
    # 20s the same row switches to an elapsed-time readout so a long turn
    # still looks alive instead of stuck.
    "var thinkTimer=null,thinkStart=0;"
    "function typing(on){var t=document.getElementById('typingRow');"
    "if(on&&!t){t=document.createElement('div');t.id='typingRow';t.className='msg assistant typing';"
    "t.textContent='· · ·';msglist.appendChild(t);scrollDown();thinkStart=Date.now();"
    "thinkTimer=setInterval(function(){var el=document.getElementById('typingRow');if(!el)return;"
    "var secs=Math.round((Date.now()-thinkStart)/1000);"
    "if(secs>=20)el.textContent='still thinking… ('+secs+'s)';},1000);}"
    "if(!on){if(t)t.remove();if(thinkTimer){clearInterval(thinkTimer);thinkTimer=null;}}}"

    # The live unlock: async send means the current state is known the
    # moment a reply (or a poll) reports it, not only at the next full
    # page load -- so the crossfade this used to need a reload for can
    # just run live instead. Same three render sites the page-load path
    # already had (header chip, desktop hero, mobile peek), no fourth
    # place to keep in sync.
    "function applyState(newState){if(!newState||newState===curState)return;curState=newState;"
    "var chipImg=document.querySelector('#avatarChip img');if(chipImg)chipImg.src='/avatar/'+newState;"
    "var hs=document.querySelector('.hdr-state');if(hs){var d=hs.querySelector('i'),s=hs.querySelector('span');"
    "if(d)d.style.background=stateColor(newState);if(s)s.textContent=newState;}"
    "var hc=document.querySelector('.hero-img-cur');"
    "if(hc){hc.src='/avatar/'+newState;hc.alt=newState;hc.classList.remove('hero-img-cur');void hc.offsetWidth;"
    "hc.classList.add('hero-img-cur');}"
    "var hcap=document.querySelector('.hero-cap');if(hcap)hcap.textContent=newState;"
    "var peek=document.getElementById('peek');"
    "if(peek){var pi=peek.querySelector('img'),pb=peek.querySelector('.peek-cap b');"
    "if(pi){pi.src='/avatar/'+newState;pi.alt='Nori: '+newState;}if(pb)pb.textContent=newState;"
    "nbShowPeek(false);}"
    "var va=document.getElementById('voiceAvatar');if(va){va.src='/avatar/'+newState;va.alt=newState;}"
    "var vsl=document.getElementById('voiceStateLabel');if(vsl)vsl.textContent=newState;}"

    # Local notifications -- poll-driven (a side effect of the same
    # setInterval(poll,...) below, not a separate mechanism), never push:
    # no VAPID, no subscription, nothing server-initiated. Ported from
    # a sibling application's own proven version -- real push was rejected there too,
    # for the same reason: a self-hosted box can't guarantee it's
    # reachable to push TO anyway. Gated, in order: the
    # per-user toggle, an actual granted permission, the tab genuinely
    # hidden (a focused tab already shows the message in the bubble list
    # -- a notification on top of that would be redundant, not helpful),
    # and quiet hours.
    "function inQuiet(){var N=window.NOTIFY;if(!N)return false;"
    "var h=new Date().getHours(),a=N.quiet_start,b=N.quiet_end;"
    "return a===b?false:(a<b?(h>=a&&h<b):(h>=a||h<b));}"
    "async function notify(m){try{"
    "var N=window.NOTIFY;if(!N||!N.enabled)return;"
    "if(!('Notification' in window)||Notification.permission!=='granted')return;"
    "if(!document.hidden)return;"
    "if(inQuiet())return;"
    "var reg=await navigator.serviceWorker.getRegistration();if(!reg)return;"
    "var body=(m.content||'').slice(0,140);"
    "var nm=(window.NORI_CHAT&&window.NORI_CHAT.assistant_name)||'Nori';"
    "reg.showNotification(nm,{body:body,tag:'nori-msg',renotify:true,"
    "icon:'/icon-192.png',badge:'/icon-192.png'});"
    "}catch(e){}}"
    "function bumpBadge(){unread++;if(navigator.setAppBadge)navigator.setAppBadge(unread).catch(function(){});}"
    "function clearBadge(){unread=0;if(navigator.clearAppBadge)navigator.clearAppBadge().catch(function(){});}"
    "document.addEventListener('visibilitychange',function(){if(!document.hidden)clearBadge();});"
    "window.addEventListener('focus',clearBadge);"
    # The one place a just-arrived message decides whether it's worth a
    # notification: never her own tool-call lines, never the user's own
    # echo (that only ever reaches addMessage directly, never this path).
    "function maybeNotify(m){if(m.kind!=='tool'&&m.role!=='user'&&document.hidden){notify(m);bumpBadge();}}"

    # Per-message error + retry -- the one piece a sibling application doesn't have
    # (it falls back to a generic new bubble on failure). uid, when known,
    # means the user's message really did save server-side and a retry
    # should ask her to answer THAT row (POST /retry) rather than create a
    # second, duplicate user message; uid==null means the request never
    # got a response at all, so retry re-sends the original text fresh.
    "function showError(el,text,uid,message){el.classList.add('failed');"
    "var row=document.createElement('div');row.className='msg-error-row';"
    "var msg=document.createElement('span');msg.className='msg-error-text';msg.textContent=message||\"couldn't send\";"
    "var btn=document.createElement('button');btn.type='button';btn.className='retry-btn';btn.textContent='retry';"
    "btn.addEventListener('click',function(){doRetry(el,text,uid,row);});"
    "row.appendChild(msg);row.appendChild(btn);el.insertAdjacentElement('afterend',row);}"

    "function settleSent(el,note,text,d){"
    "if(d.user_id!=null){seenIds.add(d.user_id);el.id='message-'+d.user_id;if(d.user_id>last)last=d.user_id;}"
    "note.remove();el.classList.remove('pending');"
    "if(d.queued){var qn=document.createElement('div');qn.className='queued-note';"
    "qn.textContent=\"sent — she's still answering something else, this lands with it\";"
    "el.insertAdjacentElement('afterend',qn);}"
    "else if(d.ok===false){showError(el,text,d.user_id!=null?d.user_id:null,d.error);}"
    "else{(d.messages||[]).forEach(function(m){addMessage(m);maybeNotify(m);});"
    "if(d.state)applyState(d.state);}"
    "onBoardCount(d.board_count);}"

    # One shared board-refresh mechanism for every JSON response that
    # might have changed it (poll, send, retry) rather than one per item
    # type -- the badge count always updates from whatever the response
    # carried, and the card list itself only re-fetches (via /board/
    # fragment, the same endpoint the initial open uses) when the board
    # is actually open to see it (2026-09-16).
    "function updateBoardCount(n){var b=$('boardCount');if(!b||typeof n!=='number')return;"
    "b.textContent=n;b.hidden=!n;}"
    "function refreshBoard(){var list=$('boardList');if(!list)return;"
    "fetch('/board/fragment').then(function(r){return r.json();}).then(function(d){"
    "list.innerHTML=d.html;updateBoardCount(d.count);}).catch(function(){});}"
    "function onBoardCount(n){updateBoardCount(n);"
    "var board=$('board');if(board&&board.classList.contains('open'))refreshBoard();}"
    "window.refreshBoard=refreshBoard;"

    "async function doRetry(el,text,uid,errorRow){"
    "if(sending)return;sending=true;setSendDisabled(true);errorRow.remove();"
    "el.classList.remove('failed');el.classList.add('pending');scrollDown(true);"
    "var note=document.createElement('div');note.className='pending-note';note.textContent='sending…';"
    "el.insertAdjacentElement('afterend',note);typing(true);"
    "try{var url,body;"
    "if(uid!=null){url='/retry';body='csrf='+encodeURIComponent(CSRF)+'&user_id='+encodeURIComponent(uid);}"
    "else{url='/send';body='csrf='+encodeURIComponent(CSRF)+'&text='+encodeURIComponent(text);}"
    "var r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:body});"
    "var d=await r.json();typing(false);settleSent(el,note,text,d);}"
    "catch(err){typing(false);note.remove();el.classList.remove('pending');showError(el,text,uid);}"
    "sending=false;setSendDisabled(false);}"

    # Shared send path -- the composer's own submit handler and voice
    # mode's "STT succeeded, now actually say it" step (see the voice-mode
    # block below) both funnel through this ONE function, so a spoken turn
    # gets the exact same optimistic-bubble/pending-note/error+retry
    # handling a typed one always had, instead of a second, divergent copy
    # of that logic -- and, just as importantly, the same turns.run() lock
    # server-side (send_msg), so a voice turn can never race a typed one.
    # Returns the parsed /send response so a caller (voice mode) can tell
    # success from failure without re-deriving it. voice=true adds
    # source=voice to the POST body -- send_msg reads that back and tags
    # both this turn's messages' meta with it (2026-09-13).
    "async function sendText(text,voice){"
    "var b=addMessage({role:'user',content:text,id:null});b.classList.add('pending');scrollDown(true);"
    "var note=document.createElement('div');note.className='pending-note';note.textContent='sending…';"
    "b.insertAdjacentElement('afterend',note);typing(true);"
    "try{"
    "var body='csrf='+encodeURIComponent(CSRF)+'&text='+encodeURIComponent(text);"
    "if(voice)body+='&source=voice';"
    "var r=await fetch('/send',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},"
    "body:body});"
    "var d=await r.json();typing(false);settleSent(b,note,text,d);return d;}"
    "catch(err){typing(false);note.remove();b.classList.remove('pending');showError(b,text,null);"
    "return {ok:false,error:'connection problem'};}}"

    # In-flight guard, two layers, same reasoning as a sibling application: `sending`
    # (a plain boolean) blocks a second submit from starting at all;
    # setSendDisabled is the VISIBLE half -- a second tap while one's
    # already running is blocked visibly, not silently ignored.
    "var form=document.getElementById('composerForm');"
    "if(form){form.addEventListener('submit',async function(e){"
    "e.preventDefault();var inp=document.getElementById('msgInput');var text=inp.value.trim();"
    "if(sending)return;"
    # A staged photo takes this same submit (one message, not two) --
    # the button and Enter both just mean "send whatever's ready", text
    # alone, photo alone, or photo-plus-caption. uploadChatPhoto is the
    # one place that actually posts it, fed whatever's currently in the
    # text box as the caption (2026-09-15, ported from a sibling application).
    "if(pendingAttachment){var f=pendingAttachment.file;clearPendingAttachment();"
    "inp.value='';autoGrowComposer();await uploadChatPhoto(f,text);refocusComposerUnlessPeeking();return;}"
    "if(!text)return;sending=true;setSendDisabled(true);inp.value='';autoGrowComposer();"
    "await sendText(text);"
    "sending=false;setSendDisabled(false);refocusComposerUnlessPeeking();});}"

    # Server-side lock/queue (turns.py) covers two tabs; this poll is what
    # lets a QUEUED tab (or a proactive ping, or a second tab entirely)
    # actually see the reply land without anyone reloading. Same 15s
    # interval and the same "skip while sending" guard a sibling application uses --
    # /send's own response already carries anything new for the tab that
    # sent it, so polling during that would just be redundant traffic.
    "async function poll(){if(sending||polling)return;polling=true;"
    "try{var r=await fetch('/poll?since='+last);var d=await r.json();"
    "(d.messages||[]).forEach(function(m){addMessage(m);maybeNotify(m);});"
    "if(d.state)applyState(d.state);"
    "onBoardCount(d.board_count);}"
    "catch(e){}polling=false;}"
    "setInterval(poll,15000);"

    # Voice conversation mode (2026-09-13, replaces the old per-message
    # play button + inline hold-to-record mic entirely). micBtn (composer)
    # just opens the modal; everything else lives here. Pointer events on
    # voiceMicBtn (not separate mouse/touch listeners) cover both a mouse
    # hold and a real touch-and-hold the same way -- press starts
    # recording, release (anywhere, including dragging off the button --
    # pointerleave/pointercancel) stops it and triggers STT.
    #
    # Autoplay unlock: a plain Audio element's first ever play() has to
    # happen inside a real user-gesture call stack or mobile Safari blocks
    # it. vUnlock() plays a silent clip synchronously inside the mic's
    # OWN pointerdown handler (a genuine tap) -- once that succeeds, the
    # SAME <audio> element stays "unlocked" for the rest of the page, so
    # setting its src to a real TTS reply and calling play() again later
    # (after the STT/send round trip, several awaits removed from the
    # original tap) is still allowed. This is the whole reason push-to-
    # talk fixes the autoplay problem the old tts_autoplay setting never
    # could: every turn genuinely starts with a tap, not a guess.
    "(function(){"
    "var micBtn=$('micBtn'),voiceModal=$('voiceModal'),voiceMicBtn=$('voiceMicBtn'),"
    "voiceStopBtn=$('voiceStopBtn'),voiceClose=$('voiceClose'),voiceStatusEl=$('voiceStatus');"
    "if(!micBtn||!voiceModal)return;"
    "var vAudio=new Audio();var vUnlocked=false;"
    "var SILENT_WAV='data:audio/wav;base64,UklGRigAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=';"
    "function vUnlock(){if(vUnlocked)return;try{vAudio.src=SILENT_WAV;var p=vAudio.play();"
    "if(p&&p.catch)p.catch(function(){});vAudio.pause();vUnlocked=true;}catch(e){}}"
    "function vSetStatus(text,isErr){if(!voiceStatusEl)return;voiceStatusEl.textContent=text||'';"
    "voiceStatusEl.classList.toggle('err',!!isErr);}"
    "function vAppendTurn(role,text){var vt=$('voiceTranscript');if(!vt)return;"
    "var el=document.createElement('div');el.className='msg '+(role==='user'?'user':'assistant');"
    "el.textContent=text;vt.appendChild(el);vt.scrollTop=vt.scrollHeight;}"
    "window.vAppendTurn=vAppendTurn;"

    # Deliberately does NOT call the shared nbCloseAll() -- that function
    # (see APP_JS) now also closes voice mode itself (so Escape/backdrop
    # can dismiss it too), so calling it FROM here would immediately
    # close the modal this function just opened, in the same tick. The
    # modal's z-index (70) already covers menu/board/peek visually; there
    # is nothing under it that needs closing first.
    "function openVoiceModal(){voiceModalOpen=true;voiceModal.classList.add('show');"
    "voiceModal.setAttribute('aria-hidden','false');vSetStatus('');}"
    "function closeVoiceModal(){voiceModalOpen=false;voiceModal.classList.remove('show');"
    "voiceModal.setAttribute('aria-hidden','true');vStop();if(!vAudio.paused)vAudio.pause();"
    "voiceMicBtn.classList.remove('speaking');if(voiceStopBtn)voiceStopBtn.hidden=true;}"
    "window.closeVoiceModal=closeVoiceModal;"
    "micBtn.addEventListener('click',openVoiceModal);"
    "if(voiceClose)voiceClose.addEventListener('click',closeVoiceModal);"

    # Playback -- vSpeak is called from addMessage's voice-mode mirror
    # hook (above) the instant a real assistant reply lands, spoken or
    # not. Barge-in lives in vStart below (a fresh mic press pauses
    # whatever's currently playing before it starts listening); the stop
    # button covers "just stop her, I'm not about to talk" without
    # starting a recording at all.
    #
    # 'speaking' used to go on immediately, right when this function is
    # called -- but /voice/tts/<id> still has to generate the audio
    # server-side first (real seconds of latency worth accounting for),
    # so the mic showed "speaking" for that whole stretch before
    # any sound existed. Moved to vAudio's own 'playing' event (fires once
    # playback has actually started, not just been requested), and the
    # real wait now gets its own honest status text instead of silence.
    "function vSpeak(mid){if(mid==null)return;vSetStatus('generating voice…');"
    "vAudio.onerror=function(){vSetStatus(\"couldn't play her voice -- the text reply above is still there.\",true);"
    "voiceMicBtn.classList.remove('speaking');if(voiceStopBtn)voiceStopBtn.hidden=true;};"
    "vAudio.onplaying=function(){voiceMicBtn.classList.add('speaking');"
    "if(voiceStopBtn)voiceStopBtn.hidden=false;vSetStatus('');};"
    "vAudio.onended=function(){voiceMicBtn.classList.remove('speaking');if(voiceStopBtn)voiceStopBtn.hidden=true;};"
    "vAudio.src='/voice/tts/'+mid;"
    "vAudio.play().catch(function(){"
    "vSetStatus('tap the mic once to let this browser play her voice.',true);"
    "voiceMicBtn.classList.remove('speaking');if(voiceStopBtn)voiceStopBtn.hidden=true;});}"
    "window.vSpeak=vSpeak;"
    "if(voiceStopBtn)voiceStopBtn.addEventListener('click',function(){"
    "vAudio.pause();voiceMicBtn.classList.remove('speaking');voiceStopBtn.hidden=true;});"

    "if(!navigator.mediaDevices||!window.MediaRecorder){"
    "voiceMicBtn.disabled=true;vSetStatus(\"voice input isn't supported in this browser.\",true);return;}"
    "var recorder=null,chunks=[],recording=false,stream=null,busy=false;"
    "function pickType(){var cands=['audio/webm','audio/ogg','audio/mp4'];"
    "for(var i=0;i<cands.length;i++){if(MediaRecorder.isTypeSupported(cands[i]))return cands[i];}return '';}"
    "async function vStart(){if(recording||busy)return;vUnlock();"
    # Barge-in: talking over her mid-reply just works -- no separate
    # "interrupt" control needed, the same press that starts listening
    # also cuts off whatever she was saying.
    "if(!vAudio.paused)vAudio.pause();voiceMicBtn.classList.remove('speaking');if(voiceStopBtn)voiceStopBtn.hidden=true;"
    "try{stream=await navigator.mediaDevices.getUserMedia({audio:true});}"
    "catch(e){vSetStatus('microphone permission denied -- allow mic access for this site to talk to her.',true);return;}"
    "chunks=[];var mt=pickType();"
    "recorder=mt?new MediaRecorder(stream,{mimeType:mt}):new MediaRecorder(stream);"
    "recorder.ondataavailable=function(e){if(e.data&&e.data.size)chunks.push(e.data);};"
    "recorder.onstop=function(){stream.getTracks().forEach(function(t){t.stop();});"
    "vHandleRecording(new Blob(chunks,{type:recorder.mimeType||'audio/webm'}));};"
    "recording=true;voiceMicBtn.classList.add('recording');vSetStatus('listening…');recorder.start();}"
    "function vStop(){if(!recording)return;recording=false;voiceMicBtn.classList.remove('recording');"
    "try{recorder.stop();}catch(e){}}"
    "voiceMicBtn.addEventListener('pointerdown',function(e){e.preventDefault();vStart();});"
    "voiceMicBtn.addEventListener('pointerup',vStop);"
    "voiceMicBtn.addEventListener('pointerleave',vStop);"
    "voiceMicBtn.addEventListener('pointercancel',vStop);"

    "async function vHandleRecording(blob){"
    "if(blob.size<1000){vSetStatus('');return;}"  # a hold under ~1 frame of audio is an accidental tap
    "busy=true;voiceMicBtn.classList.add('busy');vSetStatus('transcribing…');"
    "try{"
    "var fd=new FormData();fd.append('csrf',CSRF);"
    "fd.append('file',blob,'speech.'+(blob.type.indexOf('mp4')>=0?'mp4':blob.type.indexOf('ogg')>=0?'ogg':'webm'));"
    "var r=await fetch('/voice/stt',{method:'POST',body:fd});var d=await r.json();"
    "if(!d.ok||!d.text){"
    "vSetStatus(d.error||\"didn't catch that -- hold the mic a little longer and try again.\",true);"
    "busy=false;voiceMicBtn.classList.remove('busy');return;}"
    "vSetStatus('sending…');"
    "var d2=await sendText(d.text,true);"
    "busy=false;voiceMicBtn.classList.remove('busy');"
    "if(d2.ok===false){vSetStatus(d2.error||\"she couldn't reply -- hold the mic to try again.\",true);return;}"
    "vSetStatus('');"
    "}catch(e){vSetStatus(\"connection problem -- check you're online and try again.\",true);"
    "busy=false;voiceMicBtn.classList.remove('busy');}}"
    "})();"

    # One-tap permission prompt -- a dismissible bar, client-inserted (no
    # server-side "have they seen this" state to get wrong). Only shows
    # when permission is genuinely still 'default' (never asked) and the
    # per-user toggle is on; requestPermission() has to run inside a real
    # click handler, which is the whole reason this can't just be a
    # settings-page checkbox. Ported from a sibling application's own proven version.
    "(function(){try{"
    "var N=window.NOTIFY;"
    "if(!N||!N.enabled)return;"
    "if(!('Notification' in window)||Notification.permission!=='default')return;"
    "if(localStorage.getItem('nori_notif_dismissed')==='1')return;"
    "var bar=document.createElement('div');bar.className='notif-bar';"
    "bar.innerHTML='<span>get a notification when Nori messages</span>"
    "<button id=notifOn type=button>turn on</button>"
    "<button id=notifNo type=button class=btn-ghost>not now</button>';"
    "var col=document.querySelector('.chat-col');"
    "if(col)col.prepend(bar);"
    "var onBtn=document.getElementById('notifOn');"
    "if(onBtn)onBtn.addEventListener('click',async function(){"
    "try{await Notification.requestPermission();}catch(e){}bar.remove();});"
    "var noBtn=document.getElementById('notifNo');"
    "if(noBtn)noBtn.addEventListener('click',function(){"
    "try{localStorage.setItem('nori_notif_dismissed','1');}catch(e){}bar.remove();});"
    "}catch(e){}})();"

    # Emoji picker (2026-09-15). Data is fetched lazily on first open, not
    # on page load -- most sessions never open it, and the ~56KB set
    # (fetched once, then browser-cached for a month by the route's own
    # headers) has no reason to be part of every chat page load. Shares
    # msgInput/autoGrowComposer/isCoarsePointer with the composer code
    # above rather than redeclaring them -- this whole block still runs
    # inside CHAT_JS's own IIFE.
    "var emojiData=null,emojiDataPromise=null,emojiActiveGroup=0;"
    "function emojiLoadData(){"
    "if(emojiDataPromise)return emojiDataPromise;"
    "emojiDataPromise=fetch('/emoji-data.v1.json').then(function(r){return r.json();})"
    ".then(function(d){emojiData=d;return d;});"
    "return emojiDataPromise;}"

    # Insert-at-cursor (not append): reads the CURRENT selection range,
    # splices the emoji into it, then puts the caret right after what it
    # just inserted -- so typing continues from there, not from wherever
    # the text used to end. Setting .value doesn't fire 'input', so
    # autoGrowComposer() is called explicitly, same as the programmatic
    # clear-on-send path above already does.
    "function emojiInsert(ch){if(!msgInput)return;"
    "var start=msgInput.selectionStart||0,end=msgInput.selectionEnd||0;"
    "var val=msgInput.value;"
    "msgInput.value=val.slice(0,start)+ch+val.slice(end);"
    "var pos=start+ch.length;"
    "msgInput.selectionStart=msgInput.selectionEnd=pos;"
    "autoGrowComposer();msgInput.focus();}"

    "function emojiRenderGrid(items){var grid=document.getElementById('emojiGrid');if(!grid)return;"
    "grid.textContent='';"
    "if(!items.length){var empty=document.createElement('div');"
    "empty.className='emoji-grid-empty';empty.textContent='no matches';grid.appendChild(empty);return;}"
    "var frag=document.createDocumentFragment();var cap=Math.min(items.length,400);"
    "for(var i=0;i<cap;i++){(function(it){"
    "var b=document.createElement('button');b.type='button';b.textContent=it[0];"
    "b.setAttribute('aria-label',it[1]);"
    "b.addEventListener('click',function(){emojiInsert(it[0]);emojiClose();});"
    "frag.appendChild(b);})(items[i]);}"
    "grid.appendChild(frag);}"

    "function emojiShowGroup(gi){emojiActiveGroup=gi;"
    "var tabs=document.querySelectorAll('#emojiTabs button');"
    "for(var i=0;i<tabs.length;i++)tabs[i].classList.toggle('active',i===gi);"
    "if(!emojiData)return;"
    "emojiRenderGrid(emojiData.items.filter(function(it){return it[2]===gi;}));}"

    "function emojiSearch(q){q=q.trim().toLowerCase();"
    "if(!q){emojiShowGroup(emojiActiveGroup);return;}"
    "if(!emojiData)return;"
    "emojiRenderGrid(emojiData.items.filter(function(it){return it[1].indexOf(q)!==-1;}));}"

    "function emojiClose(){var panel=document.getElementById('emojiPanel');"
    "if(panel)panel.classList.remove('show');"
    "document.removeEventListener('click',emojiOutsideClick,true);}"

    # Capture-phase so this runs before a click ON the toggle button itself
    # bubbles to that button's own handler below -- contains() on both the
    # panel and the button means neither a grid tap nor a re-tap of the
    # button to close it ever gets treated as "outside".
    "function emojiOutsideClick(e){var panel=document.getElementById('emojiPanel'),"
    "btn=document.getElementById('emojiBtn');if(!panel)return;"
    "if(panel.contains(e.target)||(btn&&btn.contains(e.target)))return;"
    "emojiClose();}"

    # Blurring msgInput on a coarse (touch) pointer before showing the
    # panel dismisses the OS keyboard first, same isCoarsePointer check
    # the Enter-vs-newline split above already uses -- the panel then
    # takes roughly the space the keyboard would have, instead of both
    # trying to occupy the screen at once. panel.style.bottom is measured
    # from the composer's own real rendered height (mic button, staged
    # photo, etc. all change it) rather than assumed.
    "function emojiOpen(){var panel=document.getElementById('emojiPanel');if(!panel)return;"
    "if(isCoarsePointer&&msgInput)msgInput.blur();"
    "var footerEl=document.querySelector('.shell-footer');"
    "if(footerEl)panel.style.bottom=footerEl.offsetHeight+'px';"
    "panel.classList.add('show');"
    "document.addEventListener('click',emojiOutsideClick,true);"
    "if(!emojiData){var grid=document.getElementById('emojiGrid');"
    "if(grid){grid.textContent='';var l=document.createElement('div');"
    "l.className='emoji-grid-empty';l.textContent='loading…';grid.appendChild(l);}}"
    "emojiLoadData().then(function(){emojiShowGroup(emojiActiveGroup);});}"

    "var emojiBtn=document.getElementById('emojiBtn');"
    "if(emojiBtn)emojiBtn.addEventListener('click',function(e){e.stopPropagation();"
    "var panel=document.getElementById('emojiPanel');"
    "if(panel&&panel.classList.contains('show')){emojiClose();}else{emojiOpen();}});"
    "var emojiSearchInput=document.getElementById('emojiSearchInput');"
    "if(emojiSearchInput)emojiSearchInput.addEventListener('input',"
    "function(){emojiSearch(emojiSearchInput.value);});"
    "var emojiTabsEl=document.getElementById('emojiTabs');"
    "if(emojiTabsEl)emojiTabsEl.addEventListener('click',function(e){"
    "var b=e.target.closest?e.target.closest('button'):null;if(!b)return;"
    "var idx=Array.prototype.indexOf.call(emojiTabsEl.children,b);"
    "if(idx>=0){if(emojiSearchInput)emojiSearchInput.value='';emojiShowGroup(idx);}});"

    # Initial position: bottom, forced. Called both at script-parse time
    # (the DOM is already there -- this script sits at the end of body)
    # and again on window 'load' -- belt and suspenders matching
    # a sibling application's own double-call, in case layout still settles slightly
    # after this script itself has already run.
    "scrollDown(true);"
    "window.addEventListener('load',function(){scrollDown(true);});"
    "})();</script>"
)

# Full-screen image viewer (2026-09-15, operator's own ask) -- delegated to
# document.body, not #msglist, so the SAME script works on /photos' own
# .photo-grid too without a second copy -- ported from a sibling application's
# identical dialog-based one (see its own comment there for why click-
# anywhere-closes on the dialog itself is what rules out a mobile trap: a
# tap on the open dialog always has somewhere to go, the close itself).
IMAGE_GALLERY_JS = "<script>" + r"""
(function(){
  var v = document.getElementById('imageviewer');
  if (!v) return;
  var img = v.querySelector('img');
  var prev = document.getElementById('imageviewerprev');
  var next = document.getElementById('imageviewernext');
  if (!img || !prev || !next) return;
  var pr = document.getElementById('imageviewerprompt');
  var prt = pr ? pr.querySelector('.iv-text') : null;
  var cur = null;
  function items(){ return Array.prototype.slice.call(document.querySelectorAll('.chatimage')); }
  function sync(){
    var l = items(), i = l.indexOf(cur);
    var on = v.open && i >= 0 && l.length > 1;
    prev.hidden = !on || i <= 0;
    next.hidden = !on || i >= l.length - 1;
  }
  document.addEventListener('click', function(e){
    var b = e.target.closest && e.target.closest('.chatimage');
    if (b) { cur = b; setTimeout(sync, 0); }
  }, true);
  function go(d){
    var l = items(), i = l.indexOf(cur);
    if (i < 0) return;
    var b = l[i + d];
    var im = b && b.querySelector('img');
    if (!im) return;
    cur = b;
    img.src = b.dataset.full || im.currentSrc || im.src;
    img.alt = im.alt || '';
    var p = b.dataset.prompt;
    if (pr) {
      if (prt) prt.textContent = p ? ('Prompt: ' + p) : '';
      pr.hidden = !p;
      pr.classList.remove('expanded');
      pr.setAttribute('aria-expanded', 'false');
    }
    sync();
  }
  prev.addEventListener('click', function(e){ e.stopPropagation(); go(-1); });
  next.addEventListener('click', function(e){ e.stopPropagation(); go(1); });
  document.addEventListener('keydown', function(e){
    if (!v.open) return;
    if (e.key === 'ArrowLeft') { go(-1); e.preventDefault(); }
    else if (e.key === 'ArrowRight') { go(1); e.preventDefault(); }
  });
  var x0 = null;
  v.addEventListener('touchstart', function(e){ x0 = e.touches.length === 1 ? e.touches[0].clientX : null; }, {passive: true});
  v.addEventListener('touchend', function(e){
    if (x0 === null) return;
    var dx = e.changedTouches[0].clientX - x0;
    x0 = null;
    if (Math.abs(dx) > 60) go(dx > 0 ? -1 : 1);
  }, {passive: true});
  v.addEventListener('close', function(){ cur = null; prev.hidden = true; next.hidden = true; });
})();
""" + "</script>"

IMAGE_VIEWER_JS = (
    "<script>(function(){"
    "var viewer=document.getElementById('imageviewer');if(!viewer)return;"
    "var viewerImg=viewer.querySelector('img');"
    "var viewerPrompt=document.getElementById('imageviewerprompt');"
    "var viewerPromptText=viewerPrompt.querySelector('.iv-text');"
    "var opener=null;"
    "document.body.addEventListener('click',function(e){"
    "var btn=e.target.closest('.chatimage');if(!btn)return;"
    "var img=btn.querySelector('img');if(!img)return;"
    "opener=btn;"
    # data-full (2026-09-15, /photos' own grid tiles) names the REAL,
    # full-size image when the tile's own <img> is showing a thumbnail
    # instead -- checked first, so the modal always opens the original;
    # falls back to the img's own src when data-full isn't there (every
    # chat bubble, which never got a thumbnail swap -- one image per
    # message, not a dense grid, wasn't the slow page that prompted this fix).
    "viewerImg.src=btn.dataset.full||img.currentSrc||img.src;viewerImg.alt=img.alt||'';"
    # Prompt gate lives entirely in whether data-prompt is present on the
    # button (2026-09-15 server-side comment, _bubble()/_photo_tile()) --
    # nothing here re-derives "was this generated," it just reads what's
    # already there or shows nothing. Collapsed on every open (2026-09-16)
    # -- a new photo never inherits the previous one's expanded state.
    "var p=btn.dataset.prompt;"
    "viewerPromptText.textContent=p?('Prompt: '+p):'';"
    "viewerPrompt.hidden=!p;"
    "viewerPrompt.classList.remove('expanded');"
    "viewerPrompt.setAttribute('aria-expanded','false');"
    "viewer.showModal();"
    "});"
    # The indicator's own click toggles it open/closed and must NOT reach
    # the dialog's click-anywhere-closes below -- stopPropagation is the
    # one thing most likely to get missed here (2026-09-16, the operator's
    # own flag): without it, tapping the box both expands it AND immediately
    # closes the whole modal, since the click still bubbles to `viewer`.
    "viewerPrompt.addEventListener('click',function(e){"
    "e.stopPropagation();"
    "var open=viewerPrompt.classList.toggle('expanded');"
    "viewerPrompt.setAttribute('aria-expanded',open?'true':'false');"
    "});"
    "viewer.addEventListener('click',function(){viewer.close();});"
    "viewer.addEventListener('close',function(){"
    "viewerImg.removeAttribute('src');"
    "if(opener&&opener.isConnected)opener.focus({preventScroll:true});"
    "opener=null;"
    "});"
    "})();</script>"
)

# Note viewer (2026-09-17, his own ask) -- chat_page only, same as
# IMAGE_VIEWER_JS, since the board (and the notes stack) only ever
# renders there. Fetches /board/notes.json fresh on every open rather
# than caching across opens -- personal notes at personal-use volumes,
# and always-fresh is a simpler invariant than inventing a cache-
# invalidation story for something this small. window.refreshBoard/
# updateBoardCount (defined in CHAT_JS, see its own comment on the
# board-refresh mechanism) are read defensively via `window.` -- same
# cross-block reasoning APP_JS's own nbToggle already uses for
# window.refreshBoard, just in the other direction here.
NOTE_VIEWER_JS = (
    "<script>(function(){"
    "var viewer=document.getElementById('noteviewer');if(!viewer)return;"
    "var vTitle=document.getElementById('noteviewertitle');"
    "var vCreator=document.getElementById('noteviewercreator');"
    "var vText=document.getElementById('noteviewertext');"
    "var vCat=document.getElementById('noteviewercat');"
    "var vEdit=document.getElementById('notevieweredit');"
    "var vPrev=document.getElementById('noteviewerprev');"
    "var vNext=document.getElementById('noteviewernext');"
    "var vClose=document.getElementById('noteviewerclose');"
    "var notesData=[],curIdx=-1;"
    # textContent everywhere below, deliberately -- never innerHTML.
    # The JSON payload's own strings (title/body/category/creator) are
    # untouched user content; textContent makes injection structurally
    # impossible here rather than relying on the server having escaped
    # it correctly (which board_notes_json_get deliberately does NOT
    # do -- see its own comment on why esc()'d text would double-escape
    # through this exact path).
    "function render(){"
    "var n=notesData[curIdx];if(!n)return;"
    "vTitle.textContent=n.title;vCat.textContent=n.category;"
    "vCreator.textContent='added by '+n.creator;"
    "vText.textContent=n.body||'';"
    "vEdit.href='/board/note/'+n.id;"
    "vPrev.disabled=curIdx<=0;vNext.disabled=curIdx>=notesData.length-1;"
    "if(n.needs_attention){n.needs_attention=false;"
    "var cfg=window.NORI_CHAT||{};"
    "fetch('/board/notes/'+n.id+'/read',{method:'POST',"
    "headers:{'Content-Type':'application/x-www-form-urlencoded'},"
    "body:'csrf='+encodeURIComponent(cfg.csrf||'')})"
    ".then(function(r){return r.json();}).then(function(d){"
    "if(typeof d.board_count==='number'&&window.updateBoardCount)window.updateBoardCount(d.board_count);"
    "if(window.refreshBoard)window.refreshBoard();"
    "}).catch(function(){});}}"
    "function openAt(id){"
    "fetch('/board/notes.json').then(function(r){return r.json();}).then(function(d){"
    "notesData=d.notes||[];var idx=-1;"
    "for(var i=0;i<notesData.length;i++){if(notesData[i].id===id){idx=i;break;}}"
    "if(idx<0)return;curIdx=idx;render();viewer.showModal();"
    "}).catch(function(){});}"
    "document.body.addEventListener('click',function(e){"
    "var btn=e.target.closest('.card-note');"
    "if(!btn||btn.classList.contains('stack-peek')||!btn.dataset.noteId)return;"
    "openAt(parseInt(btn.dataset.noteId,10));"
    "});"
    "vPrev.addEventListener('click',function(){if(curIdx>0){curIdx--;render();}});"
    "vNext.addEventListener('click',function(){if(curIdx<notesData.length-1){curIdx++;render();}});"
    "vClose.addEventListener('click',function(){viewer.close();});"
    # Closes only on a genuine backdrop click -- a click on any of the
    # dialog's own children (the head/body/foot together cover its
    # whole box) never satisfies e.target===viewer, so the scrollable
    # body, the prev/next/edit/close controls, and the text itself are
    # all safe to click without also closing the modal. No
    # stopPropagation() needed anywhere, unlike IMAGE_VIEWER_JS's own
    # prompt indicator, which deliberately has the opposite default
    # (click-anywhere-closes) to fight instead.
    "viewer.addEventListener('click',function(e){if(e.target===viewer)viewer.close();});"
    "})();</script>"
)

def page_simple(title: str, inner: str, *, back_href: str = "/") -> bytes:
    """One-off error/status pages (403/404/429/CSRF-blocked) -- now the
    identical fixed shell page_app() gives every other page (2026-09-18,
    design pass), just with the narrower .auth-card instead of
    .page-content, since every caller is a couple of lines of text.
    back_href defaults to chat, correct for the generic error responses
    here, which are almost always hit mid-session; pass an explicit
    href for anything reached before a session exists (this module-
    level function has no `self`, so it can't call the identical
    _hdr_back() method every session-aware page uses -- this is the
    same one-line header markup, duplicated deliberately rather than
    promoting _hdr_back to a module-level function purely to share it,
    which would touch every one of its many existing call sites for a
    cosmetic-only refactor)."""
    header = (f"<div class=hdr-left><a class=hdr-back href='{esc(back_href)}'>‹</a>"
             f"<div class=hdr-title>{esc(title)}</div></div>")
    return page_app(title, header, f"<div class=auth-card>{inner}</div>")


def page_app(title: str, header_inner: str, main_inner: str, footer_inner: str = "",
            *, split_main: bool = False, extra_js: str = "", chat_header: bool = False,
            content_max_width: str | None = None) -> bytes:
    """Every authenticated page -- the fixed shell. header/footer stay put;
    main scrolls (or, for chat's split layout, contains its own scrolling
    children instead of scrolling itself -- see .shell-main--split).
    extra_js is appended after the shared APP_JS -- chat_page's own send/
    typing/retry/poll script, which nothing else needs."""
    main_class = "shell-main shell-main--split" if split_main else "shell-main"
    header_class = "shell-header shell-header--chat" if chat_header else "shell-header"
    footer_html = f"<footer class=shell-footer>{footer_inner}</footer>" if footer_inner else ""
    # .page-content (2026-09-18, design pass): every non-chat page's
    # content used to stretch edge-to-edge with no width cap at all --
    # fine on a phone, thin and hard to scan on a wide desktop monitor.
    # One shared wrapper, applied everywhere split_main isn't set
    # (chat's own 3-column desktop layout genuinely needs the full
    # width, so it's excluded rather than fought), rather than each
    # page inventing its own max-width the way history_page's own
    # .hist-wrap already had to. content_max_width (2026-09-19, settings
    # nav rebuild) overrides that shared 760px cap for ONE caller
    # (settings' own rail+content master/detail) rather than widening it
    # globally -- 760px split into a rail plus a real content column left
    # both too narrow, checked live, not guessed; every other page keeps
    # the original 760px untouched.
    style = f" style='max-width:{esc(content_max_width)}'" if content_max_width else ""
    content = main_inner if split_main else f"<div class=page-content{style}>{main_inner}</div>"
    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1,viewport-fit=cover'>"
        f"{PWA_HEAD}"
        f"<title>{esc(title)}</title><style>{BASE_CSS}</style></head>"
        "<body class=app>"
        f"<header class='{header_class}'>{header_inner}</header>"
        f"<main class='{main_class}'>{content}</main>"
        f"{footer_html}{KEYBOARD_JS}{APP_JS}{PWA_JS}{extra_js}"
        "</body></html>"
    ).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "Nori/0.1"

    def log_message(self, fmt, *args):  # quiet by default; real logging lands later
        pass

    # -- plumbing --
    def ip(self) -> str:
        # CF-Connecting-IP/X-Forwarded-For are only trustworthy when something
        # in front of this process actually strips client-supplied copies
        # before setting its own -- see TRUST_PROXY_HEADERS above. Without
        # that, self.client_address[0] (the actual TCP peer) is the only value
        # a client can't spoof, and is what the login rate limiter needs.
        if TRUST_PROXY_HEADERS:
            return (self.headers.get("CF-Connecting-IP")
                    or self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                    or self.client_address[0])
        return self.client_address[0]

    def token(self) -> str:
        try:
            return http.cookies.SimpleCookie(self.headers.get("Cookie", "")).get(
                "ns", http.cookies.Morsel()).value or ""
        except http.cookies.CookieError:
            return ""

    def send(self, code, body=b"", extra=None, *, ctype="text/html; charset=utf-8"):
        self._timing_status = code  # read back by do_GET/do_POST's own request-level timer
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        # Audited 2026-09-12 against what the app actually loads/executes/
        # fetches, directive by directive, rather than patching one more
        # violation reactively (this had already happened three times --
        # img-src, script-src, and connect-src were each added only after
        # something broke silently in production and someone had to go
        # find out why). Every directive below is either the precise
        # minimum the app needs or an explicit 'none' -- nothing is left
        # to default-src's fallback to guess right by omission:
        #   script-src / style-src 'unsafe-inline' -- every page's CSS and
        #     JS ships as inline <style>/<script> blocks (BASE_CSS, APP_JS,
        #     CHAT_JS, KEYBOARD_JS) plus inline style="" attributes -- no
        #     external stylesheet or script anywhere (verified by grep: no
        #     <script src=, no <link rel=stylesheet, no webfont -- system-ui
        #     stack throughout, by design).
        #   img-src 'self' blob: -- every real <img> is same-origin
        #     (/avatar/<state>, /image/<file_id>); blob: added 2026-09-15
        #     for the chat-photo composer's own staged-thumbnail and
        #     optimistic-echo previews (URL.createObjectURL(file)) -- CSP
        #     doesn't treat a blob: object URL as 'self' even though it
        #     never leaves the browser, so without this the preview was
        #     silently blocked (a broken-image icon, not a console-visible
        #     failure at a glance) until the real /image/<file_id> URL
        #     replaced it on a later render. No external image, no data:
        #     URI anywhere -- unchanged.
        #   connect-src 'self' -- every fetch() call in CHAT_JS targets a
        #     hardcoded relative path (/send, /retry, /poll, /voice/stt);
        #     no external endpoint, no websocket.
        #   font-src 'none' -- no webfont anywhere, system-ui stack only.
        #   media-src 'self' (2026-09-12, was 'none' until the voice layer
        #     landed) -- every <audio> src is a same-origin /voice/tts/<id>
        #     fetch; nothing external.
        #   worker-src 'self' -- the PWA service worker (2026-09-12; was
        #     'none' before it existed). manifest-src 'self' -- same date,
        #     for the <link rel=manifest> the PWA install path needs.
        #   object-src 'none' -- no <object>/<embed>; belt-and-braces on
        #     top of default-src's own fallback, made explicit so a later
        #     default-src change can't silently loosen it too.
        #   frame-ancestors 'none' -- anti-clickjacking. The ONE directive
        #     here that default-src 'none' does NOT cover (per spec,
        #     frame-ancestors/base-uri/form-action never inherit from
        #     default-src) -- X-Frame-Options: DENY above already blocks
        #     framing, but this had no CSP-level backstop of its own until
        #     now.
        #   base-uri 'none' / form-action 'self' -- unchanged, already
        #     correct (every <form> posts to a relative same-origin path;
        #     OAuth's external redirects are server-side Location
        #     redirects, which neither directive governs).
        self.send_header("Content-Security-Policy",
                        "default-src 'none'; base-uri 'none'; form-action 'self'; "
                        "frame-ancestors 'none'; script-src 'unsafe-inline'; "
                        "style-src 'unsafe-inline'; img-src 'self' blob:; connect-src 'self'; "
                        "font-src 'none'; media-src 'self'; object-src 'none'; "
                        "worker-src 'self'; manifest-src 'self'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def cookie(self, token: str) -> dict:
        a = f"ns={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={accounts.SESSION_TTL_S}"
        return {"Set-Cookie": a + ("; Secure" if SECURE_COOKIE else "")}

    def clear_cookie(self) -> dict:
        return {"Set-Cookie": "ns=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"
                              + ("; Secure" if SECURE_COOKIE else "")}

    def form(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0 or n > 65536:
            return {}
        raw = self.rfile.read(n)
        return {k: v[0] for k, v in urllib.parse.parse_qs(
            raw.decode("utf-8", "replace"), keep_blank_values=True).items()}

    def csrf_ok(self, sess: dict, form: dict) -> bool:
        csrf = form.get("csrf", "")
        return hmac.compare_digest(csrf if isinstance(csrf, str) else "", sess["csrf"])

    def send_json(self, data: dict, code: int = 200):
        return self.send(code, json.dumps(data, default=str).encode("utf-8"), ctype="application/json")

    def forbidden(self):
        return self.send(403, page_simple("forbidden", "<p>admin only.</p>"))

    def not_found(self):
        return self.send(404, page_simple("not found", "<p>not found</p>"))

    # -- GET --
    def do_GET(self):
        # Request-level timing (2026-09-13, config.debug_timing_enabled)
        # -- wraps the WHOLE request, from routing through whichever
        # handler runs through the response actually being written, as
        # one measurement. Workspace isn't known yet at this point (no
        # session resolved), so this uses the workspace-independent check
        # (timing.start_anywhere), same reasoning as ingest.py's
        # screening timer. A thin wrapper around the real routing logic
        # (_do_GET_inner) rather than instrumenting every branch of it --
        # every route gets this for free, including one added later.
        path = self.path.partition("?")[0]
        turn = timing.start_anywhere("http")
        self._timing_turn = turn  # readable by chat_page/settings_page for their own nested breakdown
        try:
            with turn.stage("request", method="GET", path=path):
                return self._do_GET_inner()
        finally:
            if turn is not timing.NULL_TURN:
                turn.stages[-1]["status"] = getattr(self, "_timing_status", None)
            turn.finish()

    def _do_GET_inner(self):
        path, _, qs = self.path.partition("?")
        if path == "/healthz":
            try:
                store.read(lambda c: c.execute("SELECT 1").fetchone())
                return self.send(200, b'{"status":"ok"}', ctype="application/json")
            except Exception:  # noqa: BLE001
                return self.send(503, b'{"status":"degraded"}', ctype="application/json")

        if path.startswith("/avatar/"):
            return self.serve_avatar(path.rsplit("/", 1)[1])

        # PWA shell -- public, no session needed. Nothing here varies by
        # user (unlike a sibling application's per-persona name/photo manifest --
        # Nori's own name and icon are fixed), and it has to work from the
        # login/setup screen too, since a household member installing on
        # their own device hits that page before any session of theirs
        # exists at all.
        if path == "/manifest.webmanifest":
            return self.serve_manifest()
        if path == "/sw.js":
            return self.serve_sw()
        if path in ("/icon-192.png", "/icon-512.png", "/icon-180.png"):
            return self.serve_pwa_icon(path.lstrip("/"))
        if path == "/emoji-data.v1.json":
            return self.serve_emoji_data()

        # Public documentation page (2026-09-15) -- has to work before any
        # account exists on this instance too, same reasoning as the PWA
        # shell routes just above: someone evaluating whether to self-host
        # her hits this before /setup, not after.
        if path == "/about":
            return self.about_page()

        if not accounts.any_users_exist():
            if path == "/setup":
                return self.setup_form()
            return self.send(303, b"", {"Location": "/setup"})

        if path.startswith("/invite/"):
            return self.invite_form(path.rsplit("/", 1)[1])

        sess = accounts.get_session(self.token())
        if path == "/login":
            if sess:
                return self.send(303, b"", {"Location": "/"})
            return self.login_form()
        if sess is None:
            return self.send(303, b"", {"Location": "/login"})

        if path == "/":
            return self.chat_page(sess)
        if path == "/poll":
            return self.poll_get(sess, urllib.parse.parse_qs(qs).get("since", ["0"])[0])
        if path == "/board/fragment":
            return self.board_fragment_get(sess)
        if path == "/board/notes.json":
            return self.board_notes_json_get(sess)
        if path == "/history":
            return self.history_page(sess, urllib.parse.parse_qs(qs))
        if path == "/photos":
            return self.photos_page(sess, urllib.parse.parse_qs(qs))
        if path == "/settings":
            q = urllib.parse.parse_qs(qs)
            tab = q.get("tab", [""])[0]
            # No ?tab= at all -- the settings HOME screen (2026-09-19,
            # nav redesign), not a silent default into whatever tab used
            # to be first. A garbage/typo'd tab value still falls through
            # to settings_page's own existing fallback (unchanged) --
            # this only intercepts the genuinely-empty case, since that's
            # the one every bare /settings link/bookmark actually hits.
            if not tab:
                return self.settings_menu_page(sess)
            return self.settings_page(sess, tab,
                                      err=q.get("err", [""])[0], info=q.get("msg", [""])[0],
                                      edit_id=q.get("edit", [""])[0], cat_filter=q.get("cat", [""])[0])
        if path.startswith("/voice/tts/"):
            return self.voice_tts_get(sess, path[len("/voice/tts/"):])
        if path == "/inventory":
            return self.inventory_page(sess)
        if path == "/inventory/edit":
            return self.inventory_edit_page(sess, urllib.parse.parse_qs(qs).get("name", [""])[0])
        if path == "/trackers":
            return self.trackers_page(sess)
        if path == "/board/new":
            return self.board_new_page(sess, urllib.parse.parse_qs(qs).get("type", ["task"])[0])
        if path.startswith("/board/task/"):
            return self.board_task_page(sess, path.rsplit("/", 1)[1])
        if path.startswith("/board/note/"):
            return self.board_note_page(sess, path.rsplit("/", 1)[1])
        if path.startswith("/board/reminder/"):
            return self.board_reminder_page(sess, path.rsplit("/", 1)[1])
        if path == "/meals":
            return self.meals_page(sess, urllib.parse.parse_qs(qs).get("start", [""])[0])
        if path == "/peers":
            q = urllib.parse.parse_qs(qs)
            query = urllib.parse.urlencode({"tab": "peers", **{
                key: q[key][0] for key in ("err", "msg") if q.get(key)}})
            return self.send(303, b"", {"Location": "/settings?" + query + "#peer-debug"})
        if path.startswith("/admin/peers/") and path.endswith("/log"):
            parts = path.split("/")
            if len(parts) == 5:
                return self.peers_log_page(sess, parts[3])
        if path.startswith("/connect/"):
            return self.connect_get(sess, path.rsplit("/", 1)[1])
        if path.startswith("/oauth/callback/"):
            return self.oauth_callback_get(sess, path.rsplit("/", 1)[1], urllib.parse.parse_qs(qs))
        if path.startswith("/image-thumb/"):
            return self.image_thumb_get(sess, path[len("/image-thumb/"):])
        if path.startswith("/image/"):
            return self.image_get(sess, path[len("/image/"):])
        if path == "/admin/persona/preview":
            return self.persona_preview_get(sess, urllib.parse.parse_qs(qs))
        if path == "/admin/contexttuning/preview":
            return self.context_preview_get(sess, urllib.parse.parse_qs(qs))
        settings_aliases = {"/admin/invite": "household", "/admin/subagents": "subagents",
                            "/admin/tools": "tools", "/admin/avatars": "avatars",
                            "/admin/mcp": "mcp", "/admin/peers": "peers", "/admin/models": "models",
                            "/admin/webtools": "webtools", "/admin/homeassistant": "homeassistant",
                            "/admin/contexttuning": "contexttuning", "/admin/persona": "persona",
                            "/admin/media": "media", "/admin/health": "health",
                            "/admin/memorybackend": "memorybackend"}
        if path in settings_aliases:
            if sess["role"] != "admin":
                return self.forbidden()
            return self.send(303, b"", {"Location": "/settings?tab=" + settings_aliases[path]})
        if path == "/files":
            return self.files_page(sess, urllib.parse.parse_qs(qs).get("path", [""])[0])
        if path.startswith("/files/download/"):
            return self.files_download(sess, urllib.parse.unquote(path[len("/files/download/"):]))
        return self.not_found()

    # HEAD = the exact same routing/auth/handler as GET, headers only --
    # send() already gates the body write on `self.command != "HEAD"`, so
    # aliasing the method is the whole fix. Not a separate stub that could
    # drift from do_GET as routes are added.
    do_HEAD = do_GET

    # -- POST --
    def do_POST(self):
        # Same request-level timing wrapper as do_GET -- see its own
        # comment.
        path = self.path.partition("?")[0]
        turn = timing.start_anywhere("http")
        self._timing_turn = turn
        try:
            with turn.stage("request", method="POST", path=path):
                return self._do_POST_inner()
        finally:
            if turn is not timing.NULL_TURN:
                turn.stages[-1]["status"] = getattr(self, "_timing_status", None)
            turn.finish()

    def _do_POST_inner(self):
        path = self.path.partition("?")[0]

        # Handled before form() -- a multipart upload's body needs its own
        # read (checked against the working-folder size cap, not form()'s
        # small 64KB urlencoded-body limit) and its own parser (Python 3.13
        # dropped the stdlib cgi module).
        if path == "/files/upload":
            return self.files_upload_post()

        # A recorded audio blob, not urlencoded -- same reasoning as the
        # upload route just above (its own read, its own multipart parse,
        # ahead of form()'s small urlencoded-body limit).
        if path == "/voice/stt":
            return self.voice_stt_post()

        # A casual chat photo -- not urlencoded, same reasoning as the two
        # routes just above (its own read, its own multipart parse, ahead
        # of form()'s small urlencoded-body limit).
        if path == "/chat-photo":
            return self.chat_photo_post()

        # PACI inbound (see the PACI specification) -- a peer authenticates itself via
        # HMAC over its own shared secret (peers.verify_inbound), not a
        # session cookie, so this has to sit ahead of both the login gate
        # and the any_users_exist() setup gate, the same reasoning as the
        # PWA shell routes in do_GET. Reads its own raw JSON body rather
        # than going through form() (urlencoded parsing), same as the
        # upload route just above.
        if path.startswith("/paci/inbound/"):
            return self.paci_inbound_post(path)

        form = self.form()

        if not accounts.any_users_exist():
            if path == "/setup":
                return self.setup_post(form)
            return self.send(303, b"", {"Location": "/setup"})

        if path.startswith("/invite/"):
            return self.invite_accept_post(path.rsplit("/", 1)[1], form)

        if path == "/login":
            return self.login_post(form)

        sess = accounts.get_session(self.token())
        # note-viewer's mark-read call (2026-09-17) is fetch()-driven, same
        # reasoning /send and /retry already document -- a stale/expired
        # session must come back as JSON the JS can actually read, not an
        # HTML redirect fetch() would follow and then fail to parse.
        is_json_api = path in ("/send", "/retry") or (
            path.startswith("/board/notes/") and path.endswith("/read"))
        if sess is None:
            if is_json_api:
                return self.send_json({"ok": False, "error": "not signed in"}, 401)
            return self.send(303, b"", {"Location": "/login"})
        if not self.csrf_ok(sess, form):
            if is_json_api:
                return self.send_json({"ok": False, "error": "bad csrf token -- reload and try again"}, 403)
            return self.send(403, page_simple("blocked", "<p>bad CSRF token — reload and try again</p>"))

        if path == "/logout":
            accounts.delete_session(self.token())
            return self.send(303, b"", {**self.clear_cookie(), "Location": "/login"})
        if path == "/settings/pings":
            return self.settings_pings_post(sess, form)
        if path == "/settings/chatvoice":
            return self.settings_chatvoice_post(sess, form)
        if path == "/settings/notify":
            return self.settings_notify_post(sess, form)
        if path == "/settings/timezone":
            return self.settings_timezone_post(sess, form)
        if path == "/settings/memory/resolve":
            return self.settings_memory_resolve_post(sess, form)
        if path == "/settings/memory/pin":
            return self.settings_memory_pin_post(sess, form)
        if path == "/settings/memory/safety":
            return self.settings_memory_safety_post(sess, form)
        if path == "/settings/memory/edit":
            return self.settings_memory_edit_post(sess, form)
        if path == "/settings/memory/delete":
            return self.settings_memory_delete_post(sess, form)
        if path == "/settings/notes/resolve":
            return self.notes_resolve_post(sess, form)
        if path == "/settings/household":
            return self.settings_household_post(sess, form)
        if path == "/settings/household/debug":
            return self.settings_household_debug_post(sess, form)
        if path == "/settings/schedules":
            return self.schedules_create_post(sess, form)
        if path == "/settings/schedules/update":
            return self.schedules_update_post(sess, form)
        if path.startswith("/settings/schedules/") and path.endswith("/toggle"):
            return self.schedules_toggle_post(sess, path.split("/")[3], form)
        if path.startswith("/settings/schedules/") and path.endswith("/delete"):
            return self.schedules_delete_post(sess, path.split("/")[3], form)
        if path == "/board/tasks":
            return self.board_task_create_post(sess, form)
        if path.startswith("/board/tasks/") and path.endswith("/update"):
            return self.board_task_update_post(sess, path.split("/")[3], form)
        if path.startswith("/board/tasks/") and path.endswith("/close"):
            return self.board_task_close_post(sess, path.split("/")[3], form)
        if path == "/board/notes":
            return self.board_note_create_post(sess, form)
        if path.startswith("/board/notes/") and path.endswith("/update"):
            return self.board_note_update_post(sess, path.split("/")[3], form)
        if path.startswith("/board/notes/") and path.endswith("/delete"):
            return self.board_note_delete_post(sess, path.split("/")[3], form)
        if path.startswith("/board/notes/") and path.endswith("/read"):
            return self.board_note_read_post(sess, path.split("/")[3], form)
        if path == "/board/reminders":
            return self.board_reminder_create_post(sess, form)
        if path.startswith("/board/reminders/") and path.endswith("/update"):
            return self.board_reminder_update_post(sess, path.split("/")[3], form)
        if path.startswith("/board/reminders/") and path.endswith("/close"):
            return self.board_reminder_close_post(sess, path.split("/")[3], form)
        if path.startswith("/board/reminders/") and path.endswith("/progress"):
            return self.board_reminder_progress_post(sess, path.split("/")[3], form)
        if path == "/settings/categories":
            return self.categories_add_post(sess, form)
        if path.startswith("/settings/categories/") and path.endswith("/rename"):
            return self.categories_rename_post(sess, path.split("/")[3], form)
        if path.startswith("/settings/categories/") and path.endswith("/disable"):
            return self.categories_disable_post(sess, path.split("/")[3], form)
        if path == "/trackers/types":
            return self.trackers_type_add_post(sess, form)
        if path.startswith("/trackers/types/") and path.endswith("/disable"):
            return self.trackers_type_disable_post(sess, path.split("/")[3], form)
        if path.startswith("/trackers/types/") and path.endswith("/enable"):
            return self.trackers_type_enable_post(sess, path.split("/")[3], form)
        if path.startswith("/trackers/types/") and path.endswith("/delete"):
            return self.trackers_type_delete_post(sess, path.split("/")[3], form)
        if path.startswith("/trackers/types/") and path.endswith("/update"):
            return self.trackers_type_update_post(sess, path.split("/")[3], form)
        if path.startswith("/trackers/types/") and path.endswith("/log"):
            return self.trackers_log_post(sess, path.split("/")[3], form)
        if path.startswith("/trackers/entries/") and path.endswith("/update"):
            return self.trackers_entry_update_post(sess, path.split("/")[3], form)
        if path == "/inventory/upsert":
            return self.inventory_upsert_post(sess, form)
        if path == "/inventory/remove":
            return self.inventory_remove_post(sess, form)
        if path == "/meals/set":
            return self.meals_set_post(sess, form)
        if path == "/meals/clear":
            return self.meals_clear_post(sess, form)
        if path.startswith("/disconnect/"):
            return self.disconnect_post(sess, path.rsplit("/", 1)[1], form)
        if path == "/admin/invite":
            return self.invite_admin_post(sess, form)
        if path == "/admin/subagents":
            return self.subagents_admin_post(sess, form)
        if path.startswith("/admin/subagents/") and path.endswith("/toggle"):
            return self.subagents_toggle_post(sess, path.split("/")[3], form)
        if path.startswith("/admin/subagents/") and path.endswith("/limits"):
            return self.subagents_limits_post(sess, path.split("/")[3], form)
        if path.startswith("/admin/subagents/") and path.endswith("/delete"):
            return self.subagents_delete_post(sess, path.split("/")[3], form)
        if path == "/admin/subagents/test":
            return self.subagents_test_post(sess, form)
        if path == "/admin/models":
            return self.models_create_post(sess, form)
        if path == "/admin/models/chain":
            return self.models_chain_post(sess, form)
        if path.startswith("/admin/models/"):
            parts = path.split("/")
            if len(parts) == 5 and parts[4] in ("toggle", "effort", "delete"):
                handler = {"toggle": self.models_toggle_post, "effort": self.models_effort_post,
                          "delete": self.models_delete_post}[parts[4]]
                return handler(sess, parts[3], form)
        if path == "/admin/providers":
            return self.providers_create_post(sess, form)
        if path.startswith("/admin/providers/"):
            parts = path.split("/")
            if len(parts) == 5 and parts[4] == "delete":
                return self.providers_delete_post(sess, parts[3], form)
            if len(parts) == 6 and parts[4] == "oauth" and parts[5] in ("complete", "check"):
                handler = (self.providers_oauth_complete_post if parts[5] == "complete"
                          else self.providers_oauth_check_post)
                return handler(sess, parts[3], form)
            if len(parts) == 5 and parts[4] == "models":
                return self.providers_list_models_post(sess, parts[3], form)
        if path == "/admin/webtools/rules":
            return self.webtools_rule_add_post(sess, form)
        if path.startswith("/admin/webtools/rules/"):
            parts = path.split("/")
            if len(parts) == 6 and parts[5] == "delete":
                return self.webtools_rule_delete_post(sess, parts[4], form)
        if path == "/admin/webtools/write-mode":
            return self.webtools_write_mode_post(sess, form)
        if path == "/admin/webtools/enabled":
            return self.webtools_enabled_post(sess, form)
        if path == "/admin/homeassistant/discover":
            return self.homeassistant_discover_post(sess, form)
        if path == "/admin/homeassistant/exposure":
            return self.homeassistant_exposure_post(sess, form)
        if path == "/admin/memorybackend/connection":
            return self.memory_backend_connection_post(sess, form)
        if path == "/admin/memorybackend/backend":
            return self.memory_backend_switch_post(sess, form)
        if path == "/admin/memorybackend/migrate":
            return self.memory_backend_migrate_post(sess, form)
        if path == "/admin/persona":
            return self.persona_admin_post(sess, form, "save")
        if path.startswith("/admin/persona/") and path[len("/admin/persona/"):] in persona_admin.ACTIONS:
            return self.persona_admin_post(sess, form, path[len("/admin/persona/"):])
        if path == "/admin/contexttuning":
            return self.context_admin_post(sess, form)
        if path.startswith("/admin/contexttuning/") and path[len("/admin/contexttuning/"):] in tuning_admin.ACTIONS:
            return self.context_admin_action(sess, form, path[len("/admin/contexttuning/"):])
        if path == "/admin/media":
            return self.media_admin_post(sess, form)
        if path == "/admin/avatars":
            return self.avatars_admin_post(sess, form)
        if path == "/admin/backups":
            return self.backups_admin_post(sess, form)
        if path == "/admin/backups/run":
            return self.backups_run_post(sess, form)
        if path == "/admin/backups/check-remote":
            return self.backups_check_remote_post(sess, form)
        if path == "/admin/backups/prune-remote":
            return self.backups_prune_remote_post(sess, form)
        if path == "/admin/health":
            return self.integration_health_settings_post(sess, form)
        if path == "/admin/health/check":
            return self.integration_health_check_post(sess, form)
        if path == "/admin/tools":
            return self.tools_admin_post(sess, form)
        if path.startswith("/admin/tools/"):
            parts = path.split("/")
            if len(parts) == 5:
                return self.tools_action_post(sess, parts[3], parts[4], form)
        if path == "/admin/mcp":
            return self.mcp_admin_post(sess, form)
        if path.startswith("/admin/mcp/"):
            parts = path.split("/")
            if len(parts) == 5 and parts[4] in ("sync", "toggle", "delete", "tool", "purpose"):
                return self.mcp_action_post(sess, parts[3], parts[4], form)
        if path == "/admin/peers":
            return self.peers_admin_post(sess, form)
        if path == "/admin/peers/recent-context":
            return self.peers_recent_context_post(sess, form)
        if path == "/admin/peers/debug-limit":
            return self.peers_debug_limit_post(sess, form)
        if path.startswith("/admin/peers/"):
            parts = path.split("/")
            if len(parts) == 5 and parts[4] in ("toggle", "delete", "purpose", "limits", "pingnow", "trust", "resync"):
                return self.peers_action_post(sess, parts[3], parts[4], form)
        if path.startswith("/peers/approvals/"):
            parts = path.split("/")
            if len(parts) == 5 and parts[4] in ("approve", "deny"):
                return self.peers_approval_post(sess, parts[3], parts[4])
        if path == "/files/mkdir":
            return self.files_mkdir_post(sess, form)
        if path == "/files/delete":
            return self.files_delete_post(sess, form)
        if path == "/files/vision":
            return self.files_vision_post(sess, form)
        if path == "/files/drive":
            return self.files_drive_post(sess, form)
        if path == "/files/onedrive":
            return self.files_onedrive_post(sess, form)
        if path == "/files/sharepoint":
            return self.files_sharepoint_post(sess, form)
        if path == "/send":
            return self.send_msg(sess, form)
        if path == "/retry":
            return self.retry_post(sess, form)
        return self.not_found()

    # -- first run --
    def setup_form(self, err: str = ""):
        e = f"<p class=err>{esc(err)}</p>" if err else ""
        # The one genuinely degenerate chevron target in this whole
        # pass (2026-09-18, design pass -- "say what you did" for
        # pre-auth pages): first-run setup, before any account or
        # session exists at all -- there's nothing real to go back to.
        # Points at /about (public, no login) rather than omitting the
        # chevron, so the page still matches every other page's shell
        # rather than being the one exception.
        self.send(200, page_simple("set up nori", (
            "<h1>set up nori</h1>"
            "<p>no account exists yet on this instance. create the first one — "
            "it becomes the admin account.</p>" + e +
            "<form method=post action='/setup'>"
            "<div class=field><input type=text name=display_name placeholder='your name' required autofocus></div>"
            "<div class=field><input type=password name=password placeholder=password required "
            "autocomplete=new-password minlength=8></div>"
            "<button class='btn btn-primary btn-block'>create admin account</button></form>"
        ), back_href="/about"))

    def setup_post(self, form: dict):
        name = (form.get("display_name") or "").strip()
        password = form.get("password") or ""
        if not name or len(password) < 8:
            return self.setup_form("name is required and password needs at least 8 characters")
        user = accounts.bootstrap_admin(name, password)
        if user is None:
            # lost a first-run race to another request — fall through to login
            return self.send(303, b"", {"Location": "/login"})
        return self.send(303, b"", {**self.cookie(accounts.new_session(user, self.ip())), "Location": "/"})

    # -- login --
    def login_form(self, err: str = ""):
        e = f"<p class=err>{esc(err)}</p>" if err else ""
        # href='/about' (2026-09-18) -- no chat exists to go back to
        # before signing in; the public about page is the one real
        # destination that makes sense here.
        self.send(200, page_simple("nori", (
            "<h1>nori</h1>" + e +
            "<form method=post action='/login'>"
            "<div class=field><input type=text name=display_name placeholder=name required autofocus></div>"
            "<div class=field><input type=password name=password placeholder=password required "
            "autocomplete=current-password></div>"
            "<button class='btn btn-primary btn-block'>sign in</button></form>"
        ), back_href="/about"))

    def login_post(self, form: dict):
        ip = self.ip()
        if accounts.rate_limited(ip):
            return self.send(429, page_simple("slow down", "<p>too many attempts — wait a few minutes</p>",
                                              back_href="/about"))
        name = form.get("display_name") if isinstance(form.get("display_name"), str) else ""
        password = form.get("password") if isinstance(form.get("password"), str) else ""
        user = accounts.authenticate(name or "", password or "")
        if user is not None:
            return self.send(303, b"", {**self.cookie(accounts.new_session(user, ip)), "Location": "/"})
        accounts.record_fail(ip)
        time.sleep(0.3)
        self.login_form("wrong name or password")

    # -- about (2026-09-15, operator's own ask) --
    def about_page(self):
        """Public, no session required -- documentation for anyone
        evaluating or self-hosting her, same audience as README.md/
        INSTALL.md, not the household member using her day to day.
        Every number and explanation below reads from the same real
        sources explain_self (self_knowledge.py) reads from, so this page
        and that tool can't quietly say two different things about the
        same subject. Deliberately generic -- no specific connected MCP
        server, peer, or sub-agent roster entry appears here; those are
        this operator's own private setup, not documentation. Compare
        settings_tool.py's own secret-exclusion discipline: nothing here
        is a hand-maintained duplicate of a number that lives elsewhere."""
        import capabilities
        import compaction
        import jobs
        import memory
        import peers

        memory_rows = "".join(f"<li><b>{esc(t)}</b> — {esc(memory.TYPE_HELP[t])}</li>"
                              for t in memory.TYPES)
        trust_rows = "".join(f"<li><b>{esc(level)}</b> — {esc(meaning)}</li>"
                             for level, meaning in peers.TRUST_LEVEL_MEANING.items())
        html = (HERE / "about.html").read_text(encoding="utf-8")
        html = (html
                .replace("{{BUILTIN_COUNT}}", str(capabilities.builtin_count()))
                .replace("{{MEMORY_ROWS}}", memory_rows)
                .replace("{{ALWAYS_LOAD}}", esc(", ".join(memory.ALWAYS_LOAD_TYPES)))
                .replace("{{COMPACTION_EXPLAIN}}", esc(compaction.COMPACTION_EXPLAIN))
                .replace("{{TRUST_ROWS}}", trust_rows)
                .replace("{{SUBAGENT_EXPLAIN}}", esc(jobs.SUBAGENT_LIMITS_EXPLAIN)))
        # Real page_app(), not page_simple() (2026-09-18, design pass --
        # "refactor for looks and readability") -- this is long-form
        # prose, not a short form/error message, so it gets the wider
        # .page-content (760px) rather than .auth-card's narrow 22rem;
        # about.html's own old inline max-width/margin wrapper div is
        # gone below, .page-content already does that job. Chevron
        # points at /login -- the one real "next step" from a public
        # page that's otherwise a dead end before signing in.
        header = (f"<div class=hdr-left><a class=hdr-back href='/login'>‹</a>"
                 f"<div class=hdr-title>About Nori</div></div>")
        self.send(200, page_app("about nori", header, html))

    # -- invites --
    def invite_form(self, raw_token: str, err: str = ""):
        inv = accounts.get_pending_invite(raw_token)
        if inv is None:
            return self.send(404, page_simple("invite", "<p>this invite link is invalid or has expired.</p>",
                                              back_href="/about"))
        e = f"<p class=err>{esc(err)}</p>" if err else ""
        self.send(200, page_simple("join nori", (
            f"<h1>join nori</h1><p>you've been invited as <b>{esc(inv['display_name'])}</b> "
            f"({esc(inv['role'])}). set your own password to finish — nobody else has seen or set it.</p>" + e +
            f"<form method=post action='/invite/{esc(raw_token)}'>"
            "<div class=field><input type=password name=password placeholder=password required "
            "autocomplete=new-password minlength=8 autofocus></div>"
            "<button class='btn btn-primary btn-block'>set password and join</button></form>"
        ), back_href="/about"))

    def invite_accept_post(self, raw_token: str, form: dict):
        password = form.get("password") or ""
        if len(password) < 8:
            return self.invite_form(raw_token, "password needs at least 8 characters")
        user = accounts.accept_invite(raw_token, password)
        if user is None:
            return self.send(404, page_simple("invite", "<p>this invite link is invalid or has expired.</p>",
                                              back_href="/about"))
        return self.send(303, b"", {**self.cookie(accounts.new_session(user, self.ip())), "Location": "/"})

    # -- shared app-shell header --
    def _hdr_menu(self, sess: dict) -> str:
        items = ("<a href='/'>Chat</a><a href='/inventory'>Inventory</a><a href='/meals'>Meals</a>"
                "<a href='/trackers'>Trackers</a><a href='/files'>Files</a><a href='/settings'>Settings</a>")
        items += (f"<form method=post action='/logout'><input type=hidden name=csrf value='{esc(sess['csrf'])}'>"
                 "<button class=menu-logout type=submit>log out</button></form>")
        return ("<div class=menu-wrap>"
               "<button class=icon-btn id=menuBtn aria-label=Menu>⋯</button>"
               f"<div class=menu-sheet id=menuSheet>{items}</div></div>")

    def _hdr_back(self, title: str, *, href: str = "/") -> str:
        # href defaults to "/" (chat) -- every authenticated page's own
        # real "back." The pre-auth pages (setup/login/invite) and
        # /about pass their own explicit href (2026-09-18, design pass:
        # "every page gets the chevron... say what you did" for the
        # pages where there's no chat to go back to yet) -- see
        # setup_form/login_form/invite_form/about_page's own comments
        # for exactly what each one points at and why.
        return f"<div class=hdr-left><a class=hdr-back href='{esc(href)}'>‹</a><div class=hdr-title>{esc(title)}</div></div>"

    def _app_header(self, sess: dict, left_html: str, *, board_button: bool = False) -> str:
        board = ""
        if board_button:
            # id=boardCount + [hidden] rather than the span only existing
            # when n>0 (2026-09-17) -- BOARD_JS updates this element's own
            # text/hidden state live off every /poll response, which needs
            # a stable node to target, not one it might have to create.
            n = self._board_count(sess)
            hidden = "" if n else " hidden"
            badge = f"<span class=badge id=boardCount{hidden}>{n}</span>"
            board = ("<button type=button class=icon-btn id=boardBtn aria-label=Board aria-controls=board aria-expanded=false>"
                     f"🗂{badge}</button>")
        return f"{left_html}<div class=hdr-spacer></div>{board}{self._hdr_menu(sess)}"

    # -- the board: open tasks as cards, plus an extensible "add" menu --
    def _task_card(self, t: dict) -> str:
        due_html = ""
        if t["due_ts"]:
            due_html = f"<span class=card-due>{esc(_due_label(t['due_ts']))}</span>"
        recur_html = f"<span class=card-recur>↻ {esc(_recur_label(t))}</span>" if t["recur_type"] else ""
        body_html = f"<div class=card-body>{esc(_truncate(t['body'], 90))}</div>" if t.get("body") else ""
        cat_html = f"<span class='card-cat cat-{esc(t['category'])}'>{esc(t['category'])}</span>"
        read_cls = "" if t["needs_attention"] else " card--read"
        return (
            f"<a class='card card-task pri-{esc(t['priority'])}{read_cls}' href='/board/task/{t['id']}'>"
            f"<div class=card-kind>task {cat_html}"
            f"{' · ' + due_html if due_html else ''}{recur_html}</div>"
            f"<div class=card-title>{esc(t['name'])}</div>{body_html}</a>")

    def _note_card(self, n: dict) -> str:
        """A real <button>, not an <a href> -- opening the full-screen
        viewer MODAL is now the tap target for a note (2026-09-17, his
        own ask: "tapping opens the modal, and the modal lets him move
        between notes"), unlike tasks/reminders, which still navigate to
        a real page. NOTE_VIEWER_JS reads data-note-id off any element
        matching .card-note (event delegation, same shape IMAGE_VIEWER_JS
        already uses for .chatimage) -- editing/history/delete still
        live at /board/note/<id>, reachable from inside the modal
        itself, not removed."""
        body_html = f"<div class=card-body>{esc(_truncate(n['body'], 90))}</div>" if n.get("body") else ""
        cat_html = f"<span class='card-cat cat-{esc(n['category'])}'>{esc(n['category'])}</span>"
        read_cls = "" if n["needs_attention"] else " card--read"
        return (
            f"<button type=button class='card card-note{read_cls}' data-note-id={n['id']}>"
            f"<div class=card-kind>note {cat_html}</div>"
            f"<div class=card-title>{esc(n['title'])}</div>{body_html}</button>")

    def _reminder_card(self, r: dict) -> str:
        cat_html = f"<span class='card-cat cat-{esc(r['category'])}'>{esc(r['category'])}</span>"
        due_html = f"<span class=card-due>{esc(_due_label(r['next_due_ts']))}</span>"
        nag_html = (f"<span class=card-recur>nagged {r['nag_count']}/{r['nag_max_count']}</span>"
                   if r["nag_count"] else "")
        body_html = f"<div class=card-body>{esc(_truncate(r['body'], 90))}</div>" if r.get("body") else ""
        read_cls = "" if r["needs_attention"] else " card--read"
        return (
            f"<a class='card card-reminder{read_cls}' href='/board/reminder/{r['id']}'>"
            f"<div class=card-kind>reminder {cat_html} · {due_html}{nag_html}</div>"
            f"<div class=card-title>{esc(r['name'])}</div>{body_html}</a>")

    def _note_stack_html(self, real_notes: list) -> str:
        """Notes stack rather than list (2026-09-17, his own ask): newest
        to oldest, needs-attention pinned on top -- `real_notes` already
        arrives newest-first from notes.list_for_user(), so a STABLE sort
        on "needs attention first" preserves that recency order within
        each of the two buckets, no secondary key needed. Only the
        topmost card is real and clickable; 1-2 more peek out behind it
        as pure decoration (aria-hidden, pointer-events:none) -- just
        enough to read as "there's more here," not a second/third real
        tap target, since the modal's own prev/next is how he actually
        gets to any of them. A small count badge is the other half of
        "entry point to browsing all of them, not a truncated list" --
        the number visibly exceeds what he can see stacked."""
        if not real_notes:
            return ""
        ordered = sorted(real_notes, key=lambda n: 0 if n["needs_attention"] else 1)
        visible = ordered[:3]
        top, peeks = visible[0], visible[1:]
        def _peek(n: dict, depth: int) -> str:
            read_cls = "" if n["needs_attention"] else " card--read"
            return (f"<div class='card card-note stack-peek{read_cls}' data-depth={depth} aria-hidden=true>"
                    f"<div class=card-title>{esc(n['title'])}</div></div>")
        peeks_html = "".join(_peek(n, i) for i, n in enumerate(peeks, start=1))
        count_html = f"<span class=note-stack-count>{len(ordered)}</span>" if len(ordered) > 1 else ""
        return f"<div class=note-stack>{peeks_html}{self._note_card(top)}{count_html}</div>"

    # -- board item accessors (2026-09-17, operator's own ask: "one
    # refresh path, not three") -- every place that needs to know what's
    # currently on the board (the initial page render, the header badge,
    # /poll, the /board/fragment refresh endpoint) calls THIS, not its
    # own copy of "list open tasks + notes + reminders." Adding a fourth
    # board item type later means changing this one place, not the
    # three-or-four call sites that would otherwise each need their own
    # update. -- */
    def _board_items(self, sess: dict) -> tuple[list, list, list]:
        return (tasks.list_for_user(sess)["tasks"], notes.list_for_user(sess)["notes"],
               reminders.list_for_user(sess)["reminders"])

    def _board_count(self, sess: dict) -> int:
        # Needs-attention items, not a total open-item count (2026-09-17,
        # his own instruction, replacing what this counted before) -- an
        # open task/note/reminder he's already viewed no longer inflates
        # the badge just for existing.
        open_tasks, real_notes, active_reminders = self._board_items(sess)
        return (sum(1 for t in open_tasks if t["needs_attention"])
               + sum(1 for n in real_notes if n["needs_attention"])
               + sum(1 for r in active_reminders if r["needs_attention"]))

    def _board_cards_html(self, sess: dict) -> str:
        """The board's own dynamic content -- cards plus the empty-state
        note -- factored out from _board_panel() so /board/fragment (the
        AJAX refresh both "open the board" and "poll while it's open"
        call, see BOARD_JS) can return exactly this and nothing else,
        rather than re-deriving it or re-rendering the whole panel shell
        (header/close button/add-menu, none of which ever change)."""
        open_tasks, real_notes, active_reminders = self._board_items(sess)
        cards = ("".join(self._task_card(t) for t in open_tasks)
                + self._note_stack_html(real_notes)
                + "".join(self._reminder_card(r) for r in active_reminders))
        empty = ("<p class=board-foot-note>nothing on the board -- tap + to add something.</p>"
                if not open_tasks and not real_notes and not active_reminders else "")
        return cards + empty

    def _board_panel(self, sess: dict) -> str:
        """The board -- real open tasks, notes, and reminders as cards
        (WISHLIST.md's "card surface", 2026-09-11; tasks/notes shipped
        2026-09-16, reminders the same day right after). Closed tasks,
        deleted notes, and closed reminders never show here -- see
        /settings?tab=tasks|notes|reminders for the full record.
        Tapping a task/reminder card is a real page navigation to a
        full-screen detail view -- this app has no client router
        anywhere else, and a real page is simpler and just as immediate.
        A note card is the one exception (2026-09-17, his own ask):
        tapping it opens NOTE_VIEWER_JS's own modal instead, so he can
        move between notes without leaving the board -- editing/history/
        delete still live at the same real /board/note/<id> page,
        reachable from inside that modal. The add button at the bottom opens a
        small menu of item TYPES, not a single hardcoded add-task
        shortcut -- deliberately extensible (the operator's own
        instruction from the tasks build); reminder is the THIRD entry
        that shell was built for, added here with no rework at all --
        confirms the menu design again, doesn't need to change it.

        This renders the board's INITIAL state, at page load -- BOARD_JS
        re-fetches _board_cards_html()'s own content fresh (via /board/
        fragment) the moment the board is opened, and again on every
        /poll cycle while it's still open, rather than trusting what was
        rendered here staying correct for the life of the page (2026-09-
        17, operator's own ask -- the same staleness problem the mid-turn
        photo bug already was, just for board rows instead of images)."""
        add_menu = (
            "<div class=board-add-wrap>"
            "<div class=board-add-menu id=boardAddMenu hidden>"
            "<a class=board-add-item href='/board/new?type=task'>📋 Task</a>"
            "<a class=board-add-item href='/board/new?type=note'>📝 Note</a>"
            "<a class=board-add-item href='/board/new?type=reminder'>⏰ Reminder</a>"
            "</div>"
            "<button type=button class=board-add-btn id=boardAddBtn aria-label='Add to board' "
            "aria-expanded=false aria-controls=boardAddMenu>+</button>"
            "</div>")
        # Trackers get one button, not a card per type/entry (2026-09-16,
        # his own instruction: "reachable from the board but don't clutter
        # it as items") -- floats bottom-LEFT, opposite corner from the
        # add-FAB above, so the two never compete. Points at trackers' own
        # first-class page (moved out of Settings 2026-09-17, alongside
        # /inventory and /meals) rather than a second, duplicate view --
        # that page already does everything ("view and update") a
        # board-reachable tracker surface would need.
        trackers_btn = "<a class=board-trackers-btn href='/trackers'>📊 Trackers</a>"
        return (
            "<div class=board id=board>"
            "<div class=board-head><h2>Board</h2>"
            "<button class=board-close id=boardClose aria-label=Close>✕</button></div>"
            f"<div class=board-list id=boardList>{self._board_cards_html(sess)}</div>"
            f"{trackers_btn}{add_menu}</div>")

    def board_fragment_get(self, sess: dict):
        """AJAX refresh target -- returns exactly what _board_cards_html()
        would render right now, plus the current count, as JSON. The one
        place that answers "what's actually on the board this second,"
        called on open and on every poll while open (BOARD_JS) rather
        than each item type getting its own refresh mechanism."""
        self.send_json({"html": self._board_cards_html(sess), "count": self._board_count(sess)})

    # -- task detail/add pages (2026-09-16, see tasks.py) --------------
    _TASK_FIELD_LABELS = {"name": "name", "body": "details", "priority": "priority",
                          "category": "category", "due_ts": "due", "recur_type": "repeats",
                          "recur_interval_min": "interval (min)", "recur_time_hour": "hour",
                          "recur_time_minute": "minute", "needs_attention": "needs attention"}

    def _task_fmt_value(self, field: str, val) -> str:
        if val is None:
            return "(none)"
        if field == "due_ts":
            return esc(time.strftime("%b %d, %Y %H:%M", time.localtime(val)))
        if field == "needs_attention":
            return "yes" if val else "no"
        return esc(str(val))

    def _task_history_html(self, task_id: int) -> str:
        events = tasks.history_for(task_id)
        return self._render_history(
            events, field_labels=self._TASK_FIELD_LABELS, fmt_fn=self._task_fmt_value, noun="task",
            plain_actions={"created": "added to the board", "closed": "closed",
                          "completed": "completed -- recurs again"})

    def _task_creator_badge(self, row: dict) -> str:
        if row["created_by_type"] == "peer":
            return esc(row.get("created_by_peer_name") or "a peer")
        return {"user": "him", "nori": "her"}.get(row["created_by_type"], esc(row["created_by_type"]))

    def _task_due_str(self, ts: float | None) -> str:
        """Round-trips through tasks.parse_when -- what the edit form
        pre-fills the due field with is itself a valid value to resubmit
        unchanged, same as every other prefilled form field in this app."""
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""

    def _task_form(self, sess: dict, csrf: str, *, action: str, edit_row: dict | None = None) -> str:
        e = edit_row or {}
        pri_opts = "".join(
            f"<option value='{p}'{' selected' if e.get('priority', 'normal') == p else ''}>{p}</option>"
            for p in tasks.PRIORITIES)
        cats = catalog.list_categories(sess, tasks.CATEGORY_DOMAIN, enabled_only=True)["categories"]
        def _cat_opt(c: dict) -> str:
            name = esc(c["name"])
            selected = " selected" if e.get("category") == c["name"] else ""
            return f"<option value='{name}'{selected}>{name}</option>"
        cat_opts = "".join(_cat_opt(c) for c in cats)
        recur_type = e.get("recur_type") or ""
        recur_opts = "".join(
            f"<option value='{v}'{' selected' if recur_type == v else ''}>{lbl}</option>"
            for v, lbl in (("", "no repeat"), ("interval", "every N minutes"), ("time", "daily at a time")))
        return (
            f"<form method=post action='{action}'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            f"<div class=field><label>name<input type=text name=name required "
            f"value='{esc(e.get('name', ''))}'></label></div>"
            f"<div class=field><label>details (optional)"
            f"<textarea name=body rows=3>{esc(e.get('body') or '')}</textarea></label></div>"
            f"<div class=field><label>priority<select name=priority>{pri_opts}</select></label> "
            f"<label>category<select name=category>{cat_opts}</select></label></div>"
            f"<div class=field><label>due (optional)<input type=text name=due "
            f"value='{esc(self._task_due_str(e.get('due_ts')))}' "
            f"placeholder=\"e.g. tomorrow, in 3 days, 2026-09-20 17:00\"></label></div>"
            f"<div class=field><label>repeats<select name=recur_type>{recur_opts}</select></label> "
            f"<label>every (min)<input type=number name=recur_interval_min min={recurrence.MIN_INTERVAL_MIN} "
            f"max={recurrence.MAX_INTERVAL_MIN} value='{e.get('recur_interval_min') or 60}'></label> "
            f"<label>hour<input type=number name=recur_time_hour min=0 max=23 "
            f"value='{e.get('recur_time_hour') if e.get('recur_time_hour') is not None else 9}'></label> "
            f"<label>minute<input type=number name=recur_time_minute min=0 max=59 "
            f"value='{e.get('recur_time_minute') if e.get('recur_time_minute') is not None else 0}'></label></div>"
            f"<button class='btn btn-primary'>{'save changes' if edit_row else 'add task'}</button>"
            "</form>")

    def board_new_page(self, sess: dict, item_type: str, err: str = ""):
        """The add-menu's own target -- extensible by construction (the
        operator's own instruction): a new item type means a new
        .board-add-item link plus a branch here, never a redesign. "note"
        landed exactly this way (2026-09-16) -- one new elif, no rework,
        confirming the design rather than needing to revisit it. Anything
        that isn't a real type falls through to "task", the original
        default."""
        e = f"<p class=err>{esc(err)}</p>" if err else ""
        if item_type == "note":
            form = self._note_form(sess, esc(sess['csrf']), action='/board/notes')
            title = "new note"
        elif item_type == "reminder":
            form = self._reminder_form(sess, esc(sess['csrf']), action='/board/reminders')
            title = "new reminder"
        else:
            form = self._task_form(sess, esc(sess['csrf']), action='/board/tasks')
            title = "new task"
        main = f"{e}<div class=section>{form}</div>"
        self.send(200, page_app(f"{title} · nori", self._app_header(sess, self._hdr_back(title.title())), main))

    def board_task_page(self, sess: dict, task_id: str, err: str = "", info: str = ""):
        try:
            row = tasks.get_for_user(sess, int(task_id))
        except ValueError:
            row = None
        if row is None:
            return self.send(404, page_simple("board", "<p>no such task.</p>"))
        # Opening this full-screen detail page is the unambiguous "he
        # viewed it" signal (2026-09-17, his own design point -- merely
        # being rendered as a card on the board is NOT enough, or every
        # item would mark read the moment the board loads and the whole
        # feature would do nothing). No-ops silently if already read.
        tasks.set_needs_attention(sess, row["id"], False, actor="user")
        csrf = esc(sess["csrf"])
        e = f"<p class=err>{esc(err)}</p>" if err else ""
        i = f"<p class=info>{esc(info)}</p>" if info else ""
        badges = (
            f"<span class='hist-badge'>{self._task_creator_badge(row)}</span> "
            f"<span class='hist-badge'>{esc(row['priority'])}</span> "
            f"<span class='hist-badge'>{esc(row['category'])}</span> "
            f"<span class='hist-badge'>{'open' if row['status'] == 'open' else 'closed'}</span>")
        meta_bits = []
        if row["due_ts"]:
            meta_bits.append(esc(_due_label(row["due_ts"])))
        if row["recur_type"]:
            meta_bits.append(f"repeats {esc(_recur_label(row))}")
        meta = f"<p class=muted>{' &middot; '.join(meta_bits)}</p>" if meta_bits else ""
        close_html = ""
        if row["status"] == "open":
            close_html = (
                "<div class=section><h2>close</h2>"
                f"<form method=post action='/board/tasks/{row['id']}/close'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                "<div class=field><label>note (optional)<input type=text name=note "
                "placeholder='how it actually got done'></label></div>"
                "<button class='btn btn-danger'>close task</button></form></div>")
        update_action = f"/board/tasks/{row['id']}/update"
        main = (
            f"{e}{i}<p>{badges}</p>{meta}"
            f"<div class=section><h2>edit</h2>"
            f"{self._task_form(sess, csrf, action=update_action, edit_row=row)}</div>"
            f"{close_html}"
            f"<div class=section><h2>history</h2>{self._task_history_html(row['id'])}</div>"
        )
        self.send(200, page_app(f"{row['name']} · nori", self._app_header(sess, self._hdr_back(row["name"])), main))

    # NOTE: these three call tasks.add()/update()/close() DIRECTLY, never
    # the _task_*_impl tool wrappers -- a plain browser POST from his own
    # settings/board UI has no _peer_act/_peer_context to read (those only
    # ever exist inside a real model turn), so actor="user" is passed
    # explicitly here, the same way schedules_create_post already does
    # for schedules.create() -- calling the tool wrapper instead would
    # silently mislabel every one of his own edits as hers.
    def board_task_create_post(self, sess: dict, form: dict):
        kw = self._task_form_kwargs(form)
        r = tasks.add(sess, created_by_type="user", **kw)
        if r.get("error"):
            return self.board_new_page(sess, "task", err=r["error"])
        return self.send(303, b"", {"Location": f"/board/task/{r['task_id']}"})

    def board_task_update_post(self, sess: dict, task_id: str, form: dict):
        kw = self._task_form_kwargs(form)
        r = tasks.update(sess, int(task_id), actor="user", **kw)
        if r.get("error"):
            return self.board_task_page(sess, task_id, err=r["error"])
        return self.send(303, b"", {"Location": f"/board/task/{task_id}"})

    def board_task_close_post(self, sess: dict, task_id: str, form: dict):
        r = tasks.close(sess, int(task_id), note=(form.get("note") or None), actor="user")
        if r.get("error"):
            return self.board_task_page(sess, task_id, err=r["error"])
        return self.send(303, b"", {"Location": f"/board/task/{task_id}"})

    @staticmethod
    def _task_form_kwargs(form: dict) -> dict:
        recur_type = form.get("recur_type") or None
        def _int(key):
            v = form.get(key)
            return int(v) if v not in (None, "") else None
        return {
            "name": form.get("name", ""), "body": form.get("body") or None,
            "priority": form.get("priority", "normal"), "category": form.get("category", "other"),
            "due": form.get("due") or "",
            "recur_type": recur_type, "recur_interval_min": _int("recur_interval_min"),
            "recur_time_hour": _int("recur_time_hour"), "recur_time_minute": _int("recur_time_minute"),
        }

    # -- note detail/add pages (2026-09-16, see notes.py) --------------
    _NOTE_FIELD_LABELS = {"title": "title", "body": "body", "category": "category",
                          "needs_attention": "needs attention"}

    def _note_fmt_value(self, field: str, val) -> str:
        if val is None:
            return "(none)"
        if field == "needs_attention":
            return "yes" if val else "no"
        return esc(str(val))

    def _note_history_html(self, note_id: int) -> str:
        events = notes.history_for(note_id)
        return self._render_history(
            events, field_labels=self._NOTE_FIELD_LABELS, fmt_fn=self._note_fmt_value, noun="note",
            plain_actions={"created": "added to the board"})

    def _note_creator_badge(self, row: dict) -> str:
        if row["created_by_type"] == "peer":
            return esc(row.get("created_by_peer_name") or "a peer")
        return {"user": "him", "nori": "her"}.get(row["created_by_type"], esc(row["created_by_type"]))

    def _note_form(self, sess: dict, csrf: str, *, action: str, edit_row: dict | None = None) -> str:
        e = edit_row or {}
        cats = catalog.list_categories(sess, notes.CATEGORY_DOMAIN, enabled_only=True)["categories"]
        def _cat_opt(c: dict) -> str:
            name = esc(c["name"])
            selected = " selected" if e.get("category") == c["name"] else ""
            return f"<option value='{name}'{selected}>{name}</option>"
        cat_opts = "".join(_cat_opt(c) for c in cats)
        return (
            f"<form method=post action='{action}'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            f"<div class=field><label>title<input type=text name=title required "
            f"value='{esc(e.get('title', ''))}'></label></div>"
            f"<div class=field><label>body (optional)"
            f"<textarea name=body rows=4>{esc(e.get('body') or '')}</textarea></label></div>"
            f"<div class=field><label>category<select name=category>{cat_opts}</select></label></div>"
            f"<button class='btn btn-primary'>{'save changes' if edit_row else 'add note'}</button>"
            "</form>")

    def board_note_page(self, sess: dict, note_id: str, err: str = "", info: str = ""):
        try:
            row = notes.get_for_user(sess, int(note_id))
        except ValueError:
            row = None
        if row is None:
            return self.send(404, page_simple("board", "<p>no such note.</p>"))
        csrf = esc(sess["csrf"])
        e = f"<p class=err>{esc(err)}</p>" if err else ""
        i = f"<p class=info>{esc(info)}</p>" if info else ""
        badges = (
            f"<span class='hist-badge'>{self._note_creator_badge(row)}</span> "
            f"<span class='hist-badge'>{esc(row['category'])}</span>")
        update_action = f"/board/notes/{row['id']}/update"
        main = (
            f"{e}{i}<p>{badges}</p>"
            f"<div class=section><h2>edit</h2>"
            f"{self._note_form(sess, csrf, action=update_action, edit_row=row)}</div>"
            "<div class=section><h2>delete</h2>"
            f"<form method=post action='/board/notes/{row['id']}/delete' "
            "data-confirm='Delete this note? This can&#39;t be undone.'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            "<button class='btn btn-danger'>delete note</button></form></div>"
            f"<div class=section><h2>history</h2>{self._note_history_html(row['id'])}</div>"
        )
        self.send(200, page_app(f"{row['title']} · nori", self._app_header(sess, self._hdr_back(row["title"])), main))

    # NOTE: same reasoning as the task POST handlers -- these call
    # notes.add()/update()/delete() DIRECTLY, never a _note_*_impl tool
    # wrapper, since a plain browser POST has no _peer_act/_peer_context
    # to read; actor="user" is passed explicitly.
    def board_note_create_post(self, sess: dict, form: dict):
        kw = self._note_form_kwargs(form)
        r = notes.add(sess, created_by_type="user", **kw)
        if r.get("error"):
            return self.board_new_page(sess, "note", err=r["error"])
        return self.send(303, b"", {"Location": f"/board/note/{r['note_id']}"})

    def board_note_update_post(self, sess: dict, note_id: str, form: dict):
        kw = self._note_form_kwargs(form)
        r = notes.update(sess, int(note_id), actor="user", **kw)
        if r.get("error"):
            return self.board_note_page(sess, note_id, err=r["error"])
        return self.send(303, b"", {"Location": f"/board/note/{note_id}"})

    def board_note_delete_post(self, sess: dict, note_id: str, form: dict):
        r = notes.delete(sess, int(note_id), actor="user")
        if r.get("error"):
            return self.board_note_page(sess, note_id, err=r["error"])
        return self.send(303, b"", {"Location": "/"})

    @staticmethod
    def _note_creator_label(n: dict) -> str:
        """Plain text, deliberately NOT esc()'d -- unlike _note_creator_
        badge (HTML embedded server-side), this rides in a JSON payload
        that NOTE_VIEWER_JS inserts via textContent, never innerHTML, so
        double-escaping an ampersand or quote is the real risk here, not
        injection."""
        if n["created_by_type"] == "peer":
            return n.get("created_by_peer_name") or "a peer"
        return {"user": "him", "nori": "her"}.get(n["created_by_type"], n["created_by_type"])

    def board_notes_json_get(self, sess: dict):
        """What NOTE_VIEWER_JS fetches once, the moment the modal first
        opens -- every current note, same needs-attention-first/newest-
        first order _note_stack_html() renders, so prev/next inside the
        modal moves through them in the same order he'd see peeking out
        of the stack. Personal notes at personal-use volumes -- one fetch
        of the full list, not a paginated API, same proportionate choice
        the rest of this board makes."""
        real_notes = notes.list_for_user(sess)["notes"]
        ordered = sorted(real_notes, key=lambda n: 0 if n["needs_attention"] else 1)
        out = [{"id": n["id"], "title": n["title"], "body": n.get("body") or "",
               "category": n["category"], "needs_attention": bool(n["needs_attention"]),
               "creator": self._note_creator_label(n)} for n in ordered]
        self.send_json({"notes": out})

    def board_note_read_post(self, sess: dict, note_id: str, form: dict):
        try:
            nid = int(note_id)
        except ValueError:
            return self.send_json({"ok": False, "error": "bad id"}, 400)
        r = notes.set_needs_attention(sess, nid, False, actor="user")
        if r.get("error"):
            return self.send_json({"ok": False, "error": r["error"]}, 404)
        # board_count rides along, same "one refresh path" shape /poll
        # and /send/retry's own responses already use -- the badge can
        # update the instant a note's marked read without a separate
        # round trip just for the count.
        return self.send_json({"ok": True, "board_count": self._board_count(sess)})

    @staticmethod
    def _note_form_kwargs(form: dict) -> dict:
        return {"title": form.get("title", ""), "body": form.get("body") or None,
               "category": form.get("category", "other")}

    # -- reminder detail/add pages (2026-09-16, see reminders.py) ------
    _REMINDER_FIELD_LABELS = {"name": "name", "body": "body", "category": "category",
                              "due_hour": "due hour", "due_minute": "due minute",
                              "recur_type": "repeats", "once_date": "date",
                              "recur_weekday": "weekday", "recur_month_day": "day of month",
                              "recur_week_ordinal": "week", "nag_interval_min": "nag interval (min)",
                              "nag_max_count": "max nags", "needs_attention": "needs attention"}
    _WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

    def _reminder_fmt_value(self, field: str, val) -> str:
        if val is None:
            return "(none)"
        if field == "recur_weekday" and isinstance(val, int) and 0 <= val <= 6:
            return esc(self._WEEKDAY_NAMES[val])
        if field == "recur_week_ordinal":
            return "last" if val == -1 else esc(f"{val}{('st' if val==1 else 'nd' if val==2 else 'rd' if val==3 else 'th')}")
        if field == "needs_attention":
            return "yes" if val else "no"
        return esc(str(val))

    def _reminder_history_html(self, reminder_id: int) -> str:
        events = reminders.history_for(reminder_id)
        return self._render_history(
            events, field_labels=self._REMINDER_FIELD_LABELS, fmt_fn=self._reminder_fmt_value, noun="reminder",
            plain_actions={"created": "added", "completed": "completed", "missed": "missed -- not done in time"})

    def _reminder_creator_badge(self, row: dict) -> str:
        if row["created_by_type"] == "peer":
            return esc(row.get("created_by_peer_name") or "a peer")
        return {"user": "him", "nori": "her"}.get(row["created_by_type"], esc(row["created_by_type"]))

    def _reminder_recur_label(self, r: dict) -> str:
        if not r["recur_type"]:
            return f"once, {esc(r.get('once_date') or '?')}"
        t = r["recur_type"]
        hm = f"{r['due_hour']:02d}:{r['due_minute']:02d}"
        if t == "time":
            return f"daily at {hm}"
        if t == "weekly":
            return f"weekly on {self._WEEKDAY_NAMES[r['recur_weekday']]} at {hm}"
        if t == "monthly_day":
            return f"monthly on day {r['recur_month_day']} at {hm}"
        if t == "monthly_weekday":
            ord_lbl = "last" if r["recur_week_ordinal"] == -1 else f"{r['recur_week_ordinal']}th"
            return f"monthly on the {ord_lbl} {self._WEEKDAY_NAMES[r['recur_weekday']]} at {hm}"
        return esc(t)

    def _reminder_form(self, sess: dict, csrf: str, *, action: str, edit_row: dict | None = None) -> str:
        e = edit_row or {}
        cats = catalog.list_categories(sess, reminders.CATEGORY_DOMAIN, enabled_only=True)["categories"]
        def _cat_opt(c: dict) -> str:
            name = esc(c["name"])
            selected = " selected" if e.get("category") == c["name"] else ""
            return f"<option value='{name}'{selected}>{name}</option>"
        cat_opts = "".join(_cat_opt(c) for c in cats)
        recur_type = e.get("recur_type") or ""
        recur_opts = "".join(
            f"<option value='{v}'{' selected' if recur_type == v else ''}>{lbl}</option>"
            for v, lbl in (("", "once"), ("time", "daily"), ("weekly", "weekly"),
                          ("monthly_day", "monthly (day of month)"), ("monthly_weekday", "monthly (Nth weekday)")))
        weekday_opts = "".join(
            f"<option value='{i}'{' selected' if e.get('recur_weekday') == i else ''}>{name}</option>"
            for i, name in enumerate(self._WEEKDAY_NAMES))
        ordinal_opts = "".join(
            f"<option value='{o}'{' selected' if e.get('recur_week_ordinal') == o else ''}>"
            f"{'last' if o == -1 else str(o)}</option>" for o in recurrence.WEEK_ORDINALS)
        return (
            f"<form method=post action='{action}'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            f"<div class=field><label>name<input type=text name=name required "
            f"value='{esc(e.get('name', ''))}'></label></div>"
            f"<div class=field><label>details (optional)"
            f"<textarea name=body rows=3>{esc(e.get('body') or '')}</textarea></label></div>"
            f"<div class=field><label>category<select name=category>{cat_opts}</select></label></div>"
            f"<div class=field><label>due hour<input type=number name=due_hour min=0 max=23 "
            f"value='{e.get('due_hour') if e.get('due_hour') is not None else 9}'></label> "
            f"<label>due minute<input type=number name=due_minute min=0 max=59 "
            f"value='{e.get('due_minute') if e.get('due_minute') is not None else 0}'></label></div>"
            f"<div class=field><label>repeats<select name=recur_type>{recur_opts}</select></label></div>"
            f"<div class=field><label>once date (if 'once' above)<input type=text name=once_date "
            f"value='{esc(e.get('once_date') or '')}' placeholder=\"e.g. tomorrow, 2026-09-20\"></label></div>"
            f"<div class=field><label>weekday (weekly/monthly-Nth-weekday)"
            f"<select name=recur_weekday>{weekday_opts}</select></label></div>"
            f"<div class=field><label>day of month (monthly-day)<input type=number name=recur_month_day "
            f"min=1 max=31 value='{e.get('recur_month_day') or 1}'></label></div>"
            f"<div class=field><label>week (monthly-Nth-weekday)"
            f"<select name=recur_week_ordinal>{ordinal_opts}</select></label></div>"
            f"<div class=field><label>nag every (min)<input type=number name=nag_interval_min "
            f"min={reminders.MIN_NAG_INTERVAL_MIN} "
            f"value='{e.get('nag_interval_min') or reminders.DEFAULT_NAG_INTERVAL_MIN}'></label> "
            f"<label>max nags<input type=number name=nag_max_count min=1 max={reminders.MAX_NAG_COUNT} "
            f"value='{e.get('nag_max_count') or reminders.DEFAULT_NAG_MAX_COUNT}'></label></div>"
            f"<button class='btn btn-primary'>{'save changes' if edit_row else 'add reminder'}</button>"
            "</form>")

    def board_reminder_page(self, sess: dict, reminder_id: str, err: str = "", info: str = ""):
        try:
            row = reminders.get_for_user(sess, int(reminder_id))
        except ValueError:
            row = None
        if row is None:
            return self.send(404, page_simple("board", "<p>no such reminder.</p>"))
        # Same "opening this page is the unambiguous view signal" as
        # board_task_page (2026-09-17) -- note this doesn't fight the nag
        # loop: mark_nagged() re-raises needs_attention on every fresh
        # nag regardless of what this clears it to.
        reminders.set_needs_attention(sess, row["id"], False, actor="user")
        csrf = esc(sess["csrf"])
        e = f"<p class=err>{esc(err)}</p>" if err else ""
        i = f"<p class=info>{esc(info)}</p>" if info else ""
        badges = (
            f"<span class='hist-badge'>{self._reminder_creator_badge(row)}</span> "
            f"<span class='hist-badge'>{esc(row['category'])}</span> "
            f"<span class='hist-badge'>{'active' if row['status'] == 'active' else 'closed'}</span> "
            f"<span class='hist-badge'>{esc(row['occurrence_status'])}</span>")
        meta = (f"<p class=muted>{esc(self._reminder_recur_label(row))} &middot; "
               f"next due {esc(_due_label(row['next_due_ts']))} &middot; "
               f"nagged {row['nag_count']}/{row['nag_max_count']}</p>")
        progress_html = ""
        close_html = ""
        if row["status"] == "active":
            status_opts = "".join(f"<option value='{s}'>{s}</option>" for s in reminders.PROGRESS_STATUSES)
            progress_html = (
                "<div class=section><h2>record progress</h2>"
                f"<form method=post action='/board/reminders/{row['id']}/progress'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                "<div class=field><label>status (optional)<select name=status>"
                f"<option value=''>--</option>{status_opts}</select></label></div>"
                "<div class=field><label>note (optional)<input type=text name=note "
                "placeholder='what he said, or what you noticed'></label></div>"
                "<button class=btn>record</button></form></div>")
            close_html = (
                "<div class=section><h2>close</h2>"
                f"<form method=post action='/board/reminders/{row['id']}/close'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                "<div class=field><label>note (optional)<input type=text name=note "
                "placeholder='how it actually got done'></label></div>"
                "<button class='btn btn-danger'>close</button></form></div>")
        update_action = f"/board/reminders/{row['id']}/update"
        main = (
            f"{e}{i}<p>{badges}</p>{meta}"
            f"<div class=section><h2>edit</h2>"
            f"{self._reminder_form(sess, csrf, action=update_action, edit_row=row)}</div>"
            f"{progress_html}{close_html}"
            f"<div class=section><h2>history</h2>{self._reminder_history_html(row['id'])}</div>"
        )
        self.send(200, page_app(f"{row['name']} · nori", self._app_header(sess, self._hdr_back(row["name"])), main))

    # NOTE: same reasoning as task/note POST handlers -- these call
    # reminders.add()/update()/close()/record_progress() DIRECTLY, never
    # a _reminder_*_impl tool wrapper, since a plain browser POST has no
    # _peer_act/_peer_context to read; actor="user" is passed explicitly.
    def board_reminder_create_post(self, sess: dict, form: dict):
        kw = self._reminder_form_kwargs(form)
        r = reminders.add(sess, created_by_type="user", **kw)
        if r.get("error"):
            return self.board_new_page(sess, "reminder", err=r["error"])
        return self.send(303, b"", {"Location": f"/board/reminder/{r['reminder_id']}"})

    def board_reminder_update_post(self, sess: dict, reminder_id: str, form: dict):
        kw = self._reminder_form_kwargs(form)
        r = reminders.update(sess, int(reminder_id), actor="user", **kw)
        if r.get("error"):
            return self.board_reminder_page(sess, reminder_id, err=r["error"])
        return self.send(303, b"", {"Location": f"/board/reminder/{reminder_id}"})

    def board_reminder_close_post(self, sess: dict, reminder_id: str, form: dict):
        r = reminders.close(sess, int(reminder_id), note=(form.get("note") or None), actor="user")
        if r.get("error"):
            return self.board_reminder_page(sess, reminder_id, err=r["error"])
        return self.send(303, b"", {"Location": f"/board/reminder/{reminder_id}"})

    def board_reminder_progress_post(self, sess: dict, reminder_id: str, form: dict):
        r = reminders.record_progress(sess, int(reminder_id), note=(form.get("note") or None),
                                      status=(form.get("status") or None), actor="user")
        if r.get("error"):
            return self.board_reminder_page(sess, reminder_id, err=r["error"])
        return self.send(303, b"", {"Location": f"/board/reminder/{reminder_id}"})

    @staticmethod
    def _reminder_form_kwargs(form: dict) -> dict:
        recur_type = form.get("recur_type") or None
        def _int(key):
            v = form.get(key)
            return int(v) if v not in (None, "") else None
        return {
            "name": form.get("name", ""), "body": form.get("body") or None,
            "category": form.get("category", "other"),
            "due_hour": _int("due_hour") or 0, "due_minute": _int("due_minute") or 0,
            "recur_type": recur_type, "once_date": form.get("once_date") or None,
            "recur_weekday": _int("recur_weekday"), "recur_month_day": _int("recur_month_day"),
            "recur_week_ordinal": _int("recur_week_ordinal"),
            "nag_interval_min": _int("nag_interval_min") or reminders.DEFAULT_NAG_INTERVAL_MIN,
            "nag_max_count": _int("nag_max_count") or reminders.DEFAULT_NAG_MAX_COUNT,
        }

    # -- admin: create an invite --
    def invite_admin_form(self, sess: dict, err: str = "", link: str = ""):
        if sess["role"] != "admin":
            return self.forbidden()
        return self.settings_page(sess, "household", err=err, invite_link=link)

    def _household_members(self, sess: dict, link: str = "") -> str:
        shown = (f"<p class=info>invite link (shown once — copy it now): <code>{esc(link)}</code></p>"
                if link else "")
        rows = "".join(
            f"<div class=list-row><div class=list-icon>👤</div><div class=list-meta>"
            f"<b>{esc(u['display_name'])}</b><small>{esc(u['role'])} · {esc(u['status'])}</small></div></div>"
            for u in accounts.list_users(sess["workspace_id"]))
        return (
            f"<div class=section><h2>Invite someone</h2></div>{shown}"
            "<form method=post action='/admin/invite'>"
            f"<input type=hidden name=csrf value='{esc(sess['csrf'])}'>"
            "<div class=field><input type=text name=display_name placeholder='their name' required></div>"
            "<div class=field><select name=role><option value=member selected>member</option>"
            "<option value=admin>admin</option></select></div>"
            "<button class='btn btn-primary btn-block'>create invite</button></form>"
            f"<div class=section><h2>Members</h2>{rows}</div>"
        )

    def invite_admin_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        name = (form.get("display_name") or "").strip()
        role = form.get("role") if form.get("role") in ("admin", "member") else "member"
        if not name:
            return self.invite_admin_form(sess, "name is required")
        _uid, raw = accounts.create_invite(sess["workspace_id"], name, role, sess["user_id"])
        link = f"/invite/{raw}"
        return self.invite_admin_form(sess, link=link)

    # -- admin: sub-agent roster --
    def subagents_admin_form(self, sess: dict, err: str = "", info: str = "", test_result: dict | None = None):
        """test_result (2026-09-30, operator's own ask: a way to test a
        sub-agent's own provider/model config directly, without a live
        chat turn) -- {"label", "prompt", "ok", "content"|"error", "usage"}
        from subagents_test_post, rendered as its own section. Calls
        through the exact same chat.call_for_model() dispatch_subagent's
        own job runner uses (see jobs.py's _call()) -- same provider/auth/
        translation, just synchronous and with no job row, no tool access,
        no persona/system prompt (neither this nor a real dispatched job
        ever gets one -- see jobs.py's own module docstring)."""
        if sess["role"] != "admin":
            return self.forbidden()
        e = f"<p class=err>{esc(err)}</p>" if err else ""
        i = f"<p class=info>{esc(info)}</p>" if info else ""
        csrf = esc(sess["csrf"])
        model_by_id = {m["id"]: m for m in models.list_all()}
        job_counts = sub_agents.job_counts()
        def _row(a: dict) -> str:
            tools_desc = ("no tools" if a["tool_call_limit"] == 0 else
                         f"up to {a['tool_call_limit']} tool call(s), "
                         f"{a['tool_byte_limit']:,} bytes/job")
            m = model_by_id.get(a["model_id"])
            model_desc = m["alias"] if m else "(no model configured)"
            jc = job_counts.get(a["id"], 0)
            confirm_msg = (f"Delete {a['label']!r}? It has {jc} job(s) in its history -- those will be "
                          f"deleted too. This can't be undone." if jc else "")
            confirm_attr = f" data-confirm=\"{esc(confirm_msg)}\"" if confirm_msg else ""
            return (
                f"<div class=list-row><div class=list-icon>🤖</div>"
                f"<div class=list-meta><b>{esc(a['label'])}</b><small>{esc(model_desc)} · "
                f"{'enabled' if a['enabled'] else 'disabled'} · "
                f"{esc(tools_desc)}</small>"
                f"<form method=post action='/admin/subagents/{a['id']}/limits' "
                f"style='margin-top:.4rem;display:flex;gap:.4rem;align-items:center;flex-wrap:wrap'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                f"<label style='font-size:.85em'>tool calls/job"
                f"<input type=number name=tool_call_limit min=0 max={sub_agents.TOOL_CALL_LIMIT_MAX} "
                f"value={a['tool_call_limit']} style='width:5em;margin-left:.3em'></label>"
                f"<label style='font-size:.85em'>bytes/job"
                f"<input type=number name=tool_byte_limit min={sub_agents.TOOL_BYTE_LIMIT_MIN} "
                f"max={sub_agents.TOOL_BYTE_LIMIT_MAX} value={a['tool_byte_limit']} "
                f"style='width:8em;margin-left:.3em'></label>"
                f"<button class='btn'>save limits</button></form></div>"
                f"<div class=list-actions><form method=post action='/admin/subagents/{a['id']}/toggle'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                f"<button class='btn'>{'disable' if a['enabled'] else 'enable'}</button></form>"
                f"<form method=post action='/admin/subagents/{a['id']}/delete'{confirm_attr}>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                "<input type=hidden name=force value=1>"
                "<button class='btn btn-danger'>delete</button></form></div></div>")

        rows = "".join(_row(a) for a in sub_agents.list_all()) or "<p class=muted>none configured yet</p>"

        def _running_row(j: dict) -> str:
            elapsed_s = time.time() - (j["started_ts"] or j["created_ts"])
            elapsed = (f"{elapsed_s / 60:.0f} min" if elapsed_s >= 60 else f"{elapsed_s:.0f} sec")
            task_preview = j["task"][:100] + ("…" if len(j["task"]) > 100 else "")
            return (f"<div class=list-row><div class=list-icon>⏳</div>"
                   f"<div class=list-meta><b>job #{j['id']} · {esc(j['agent_label'])}</b>"
                   f"<small>{esc(j['status'])} · {esc(elapsed)} · for {esc(j['display_name'])}</small>"
                   f"<small class=muted>{esc(task_preview)}</small></div></div>")

        def _interrupted_row(j: dict) -> str:
            when = time.strftime("%b %d, %H:%M", time.localtime(j["finished_ts"]))
            task_preview = j["task"][:100] + ("…" if len(j["task"]) > 100 else "")
            return (f"<div class=list-row><div class=list-icon>⚠️</div>"
                   f"<div class=list-meta><b>job #{j['id']} · {esc(j['agent_label'])}</b>"
                   f"<small>interrupted {esc(when)} · for {esc(j['display_name'])}</small>"
                   f"<small class=muted>{esc(task_preview)}</small></div></div>")

        running = jobs.running_jobs()
        running_html = ("".join(_running_row(j) for j in running) if running
                        else "<p class=muted>nothing running right now.</p>")
        running_section = (
            f"<div class=section><h2>running now</h2>"
            f"<p class=muted style='margin:0 0 .6rem'>Every job currently queued or running, across "
            f"the whole household -- not just yours. A row that's been running far longer than a "
            f"real job should is worth a look, but a server restart doesn't leave stale rows like "
            f"this sitting forever -- the next start sweeps anything still queued or running and "
            f"marks it interrupted (below).</p>{running_html}</div>"
        )

        interrupted = jobs.recently_interrupted()
        interrupted_section = (
            f"<div class=section><h2>recently interrupted</h2>"
            f"<p class=muted style='margin:0 0 .6rem'>Jobs a server restart caught mid-flight -- "
            f"marked, not retried, so check_job reports exactly this instead of a job that looks "
            f"like it's still running.</p>"
            f"{''.join(_interrupted_row(j) for j in interrupted)}</div>"
        ) if interrupted else ""

        model_opts = "".join(
            f"<option value='{m['id']}'>{esc(m['alias'])}</option>" for m in models.list_enabled())
        call_limit_tip = info_tip(
            "0 means no tools at all -- a plain completion, same as every sub-agent before this "
            "existed. Above 0, it can read (not write) your own working folder via list_files/"
            "read_file, up to this many calls, one job at a time.")
        byte_limit_tip = info_tip(
            "Cumulative cap across every tool result in one job -- only matters when tool calls are "
            "allowed above. 100 calls each returning a large file costs very differently than 100 "
            "small ones, so this is capped independently of the call count.")
        add_form = (
            "<form method=post action='/admin/subagents'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            "<div class=field><input type=text name=label placeholder='label, e.g. summarizer' required></div>"
            f"<div class=field><label>model</label><select name=model_id required>{model_opts}"
            "</select></div>"
            f"<div class=field><label>tool calls per job {call_limit_tip}</label>"
            f"<input type=number name=tool_call_limit min=0 max={sub_agents.TOOL_CALL_LIMIT_MAX} value=0></div>"
            f"<div class=field><label>bytes read per job {byte_limit_tip}</label>"
            f"<input type=number name=tool_byte_limit min={sub_agents.TOOL_BYTE_LIMIT_MIN} "
            f"max={sub_agents.TOOL_BYTE_LIMIT_MAX} value={sub_agents.TOOL_BYTE_LIMIT_DEFAULT}></div>"
            "<button class='btn btn-primary btn-block'>add</button></form>"
        ) if model_opts else (
            "<p class=muted>no enabled models yet -- add one in "
            "<a href='/settings?tab=models'>Model Config</a> first.</p>")

        # -- Test a sub-agent directly (2026-09-30) -- a raw prompt straight
        # to its configured model, synchronous, no job row, no tool access,
        # no persona -- for checking a provider/model config actually
        # works without waiting on a live chat turn to decide to ask it.
        agent_opts = "".join(f"<option value='{a['id']}'>{esc(a['label'])}</option>"
                             for a in sub_agents.list_all())
        test_form = (
            "<form method=post action='/admin/subagents/test'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            f"<div class=field><label>sub-agent</label><select name=sub_agent_id required>{agent_opts}"
            "</select></div>"
            "<div class=field><label>prompt</label>"
            "<textarea name=prompt rows=3 placeholder='a raw test prompt' required></textarea></div>"
            "<button class='btn btn-primary btn-block'>send</button></form>"
        ) if agent_opts else "<p class=muted>add a sub-agent above first.</p>"
        test_result_html = ""
        if test_result:
            if test_result["ok"]:
                usage = test_result.get("usage") or {}
                usage_line = (f"<small>{usage.get('prompt_tokens', 0)} in / "
                             f"{usage.get('completion_tokens', 0)} out</small>" if usage else "")
                test_result_html = (
                    f"<div class=section><h2>result: {esc(test_result['label'])}</h2>"
                    f"<p class=muted style='margin:0 0 .4rem'>prompt: {esc(test_result['prompt'])}</p>"
                    f"<div class=list-row><div class=list-meta><b class=wrap>{esc(test_result['content'])}</b>"
                    f"{usage_line}</div></div></div>")
            else:
                test_result_html = (
                    f"<div class=section><h2>result: {esc(test_result['label'])}</h2>"
                    f"<p class=muted style='margin:0 0 .4rem'>prompt: {esc(test_result['prompt'])}</p>"
                    f"<p class=err>{esc(test_result['error'])}</p></div>")

        main = (
            f"{e}{i}"
            f"{running_section}"
            f"{interrupted_section}"
            "<p class=muted>she picks a label from this list to hand work off to -- "
            "never an endpoint or model of her own choosing. Models come from "
            "<a href='/settings?tab=models'>Model Config</a>.</p>"
            f"<div class=section>{add_form}</div>"
            f"<div class=section><h2>roster</h2>{rows}</div>"
            f"<div class=section><h2>test a sub-agent</h2>{test_form}</div>"
            f"{test_result_html}"
        )
        self._settings_response(sess, "subagents", main)

    def subagents_admin_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        try:
            call_limit = int(form.get("tool_call_limit") or 0)
            byte_limit = int(form.get("tool_byte_limit") or sub_agents.TOOL_BYTE_LIMIT_DEFAULT)
            model_id = int(form.get("model_id") or 0)
        except ValueError:
            return self.subagents_admin_form(sess, "tool limits and model must be valid")
        ok, result = sub_agents.create(
            sess["user_id"], form.get("label") or "", model_id, call_limit, byte_limit)
        if not ok:
            return self.subagents_admin_form(sess, str(result))
        return self.subagents_admin_form(sess)

    def subagents_toggle_post(self, sess: dict, sub_agent_id: str, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        try:
            sid = int(sub_agent_id)
        except ValueError:
            return self.subagents_admin_form(sess, "bad id")
        agent = sub_agents.get(sid)
        if agent is None:
            return self.subagents_admin_form(sess, "no such sub-agent")
        sub_agents.set_enabled(sid, not agent["enabled"])
        return self.subagents_admin_form(sess)

    def subagents_delete_post(self, sess: dict, sub_agent_id: str, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        try:
            sid = int(sub_agent_id)
        except ValueError:
            return self.subagents_admin_form(sess, "bad id")
        if sub_agents.get(sid) is None:
            return self.subagents_admin_form(sess, "no such sub-agent")
        force = bool(form.get("force"))
        ok, msg = sub_agents.delete(sid, force=force)
        return self.subagents_admin_form(sess, err=("" if ok else msg), info=(msg if ok else ""))

    def subagents_test_post(self, sess: dict, form: dict):
        """See subagents_admin_form's own docstring -- a raw, synchronous
        test prompt straight to a sub-agent's configured model, same
        dispatch chat.py's real job runner uses (jobs.py's own _call()),
        just called directly here instead of from a background job
        thread -- no job row, no tool access, no persona, same as a real
        dispatched job already gets none of either."""
        if sess["role"] != "admin":
            return self.forbidden()
        try:
            sid = int(form.get("sub_agent_id", ""))
        except (TypeError, ValueError):
            return self.subagents_admin_form(sess, "bad id")
        agent = sub_agents.get(sid)
        if agent is None:
            return self.subagents_admin_form(sess, "no such sub-agent")
        prompt = (form.get("prompt") or "").strip()
        if not prompt:
            return self.subagents_admin_form(sess, "a prompt is required")
        entry = models.get_with_provider(agent["model_id"]) if agent.get("model_id") else None
        if entry is None:
            return self.subagents_admin_form(
                sess, f"{agent['label']!r} has no model configured -- pick one above first")
        try:
            result = chat.call_for_model(entry, [{"role": "user", "content": prompt}],
                                        tools=None, timeout=60)
        except chat.ModelError as exc:
            return self.subagents_admin_form(sess, test_result={
                "label": agent["label"], "prompt": prompt, "ok": False, "error": str(exc)})
        return self.subagents_admin_form(sess, test_result={
            "label": agent["label"], "prompt": prompt, "ok": True,
            "content": result.get("content") or "(empty reply)", "usage": result.get("usage")})

    def subagents_limits_post(self, sess: dict, sub_agent_id: str, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        try:
            sid = int(sub_agent_id)
            call_limit = int(form.get("tool_call_limit") or 0)
            byte_limit = int(form.get("tool_byte_limit") or sub_agents.TOOL_BYTE_LIMIT_DEFAULT)
        except ValueError:
            return self.subagents_admin_form(sess, "bad id or tool limits")
        if sub_agents.get(sid) is None:
            return self.subagents_admin_form(sess, "no such sub-agent")
        err = sub_agents.set_limits(sid, call_limit, byte_limit)
        if err:
            return self.subagents_admin_form(sess, err)
        return self.subagents_admin_form(sess)

    # -- admin: Providers + model roster + primary/fallback chain. Controls
    # both Nori's own primary/fallback models (this page) and what shows up
    # in the sub-agent form's model dropdown.
    def models_admin_form(self, sess: dict, err: str = "", info: str = "", browse: dict | None = None):
        """Providers + Models + primary/fallback chain (2026-09-30, see
        providers.py/models.py) -- reworked from the old OpenRouter-only
        roster+single-default form. Three sections: configured Providers
        (connect/disconnect), the Model roster (alias+provider+model
        name), and the primary/fallback chain picked from enabled Models.
        There is deliberately no default provider or model -- an instance
        with nothing configured here shows that honestly instead of
        silently assuming OpenRouter.

        browse (2026-09-30, see providers.list_models()) -- optional
        {"provider_id", "provider_label", "q", "ok", "items"|"error"}
        from providers_list_models_post, rendered as its own section so
        the admin can pick a real model name instead of guessing one."""
        if sess["role"] != "admin":
            return self.forbidden()
        e = f"<p class=err>{esc(err)}</p>" if err else ""
        i = f"<p class=info>{esc(info)}</p>" if info else ""
        csrf = esc(sess["csrf"])
        wsid = sess["workspace_id"]

        # -- Providers --
        def _copyfield(value: str, open_url: str | None = None) -> str:
            """A readonly, click-to-select, one-click-copy field -- fixes
            the old plain-text flash message, which just wrapped a long
            URL/code across several lines with no way to grab it cleanly
            (2026-09-30, real complaint). No dependency on CHAT_JS (not
            loaded on settings pages) -- document.execCommand('copy') is
            the same fallback this app's own copyMessageText() already
            uses, just inlined here since this is the only place on a
            settings page that needs it."""
            v = esc(value)
            # href uses open_url, NOT v -- real bug, found live (2026-09-30):
            # for the device-code case (Copilot/xAI), `value` is the short
            # user code and `open_url` is the actual verification page --
            # two DIFFERENT strings. Using `v` here happened to work for
            # Anthropic/OpenAI's manual-code flow purely by coincidence
            # (there, value and open_url are called with the identical
            # authorize_url), which is exactly how this went unnoticed.
            open_link = (f"<a class=btn href='{esc(open_url)}' target=_blank rel=noopener>open</a>"
                        if open_url else "")
            return (
                "<div style='display:flex;gap:.4rem;align-items:center;margin-top:.4rem'>"
                f"<input type=text readonly value='{v}' onclick='this.select()' "
                "style='flex:1;font-family:monospace;font-size:.8rem;min-width:0'>"
                "<button type=button class=btn onclick=\"this.previousElementSibling.select();"
                "document.execCommand('copy');this.textContent='copied';"
                "setTimeout(()=>this.textContent='copy',1200)\">copy</button>"
                f"{open_link}</div>")

        all_providers = providers.list_all(wsid)
        provider_rows = []
        for p in all_providers:
            status_chip = {"connected": "<span class='chip active'>connected</span>",
                          "needs_reauth": "<span class='chip warn'>needs reconnecting</span>",
                          "unconfigured": "<span class=chip>connecting…</span>",
                          "error": "<span class='chip warn'>error</span>"}.get(
                p["status"], f"<span class=chip>{esc(p['status'])}</span>")
            connect_ui = ""
            # pending_display() needs the RAW row (pending_enc intact) --
            # `p` itself comes from providers.list_all(), which strips
            # every *_enc column for UI safety (see that function's own
            # docstring), pending_enc included. Passing `p` straight in
            # here silently returned None for EVERY OAuth provider type
            # (real bug, found live 2026-09-30: the authorize link/device
            # code never rendered for any of them, just the button below
            # it) -- a fresh providers.get(p['id']) re-fetches the intact
            # row for this one lookup, still never handed anywhere beyond
            # pending_display()'s own already-safe derived output.
            if p["status"] in ("unconfigured", "needs_reauth") and p["type"] in (
                    "anthropic_oauth", "openai_oauth"):
                pending = providers.pending_display(providers.get(p["id"]))
                link_ui = (_copyfield(pending["authorize_url"], open_url=pending["authorize_url"])
                          if pending else "")
                connect_ui = (
                    f"{link_ui}"
                    f"<form method=post action='/admin/providers/{p['id']}/oauth/complete' "
                    "style='display:flex;gap:.4rem;margin-top:.4rem'>"
                    f"<input type=hidden name=csrf value='{csrf}'>"
                    "<input type=text name=pasted placeholder='paste the code (or the failed "
                    "redirect URL) here' style='flex:1'>"
                    "<button class=btn>finish connecting</button></form>")
            elif p["status"] in ("unconfigured", "needs_reauth") and p["type"] in ("github_copilot", "xai_oauth"):
                pending = providers.pending_display(providers.get(p["id"]))
                code_ui = (_copyfield(pending["user_code"], open_url=pending["verification_uri"])
                          if pending else "")
                connect_ui = (
                    f"{code_ui}"
                    f"<form method=post action='/admin/providers/{p['id']}/oauth/check' style='margin-top:.4rem'>"
                    f"<input type=hidden name=csrf value='{csrf}'>"
                    "<button class=btn>check if authorized yet</button></form>")
            browse_btn = (
                f"<form method=post action='/admin/providers/{p['id']}/models'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                "<button class=btn>list models</button></form>") if p["status"] == "connected" else ""
            provider_rows.append(
                "<div class=list-row><div class=list-meta>"
                f"<b>{esc(p['label'])}</b> {status_chip}"
                f"<small>{esc(providers.TYPES.get(p['type'], {}).get('label', p['type']))}</small>"
                f"{connect_ui}</div>"
                f"<div class=list-actions>{browse_btn}"
                f"<form method=post action='/admin/providers/{p['id']}/delete'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                "<button class='btn btn-danger'>remove</button></form></div></div>")
        type_opts = "".join(f"<option value='{k}'>{esc(v['label'])}</option>" for k, v in providers.TYPES.items())
        main_provider = (
            f"<div class=section><h2>Providers</h2>"
            "<p class=muted>a configured connection to an LLM backend -- an API key (OpenRouter, or "
            "straight to Anthropic/OpenAI with your own key), or an OAuth-connected subscription. "
            "Nothing is assumed by default.</p>"
            "<form method=post action='/admin/providers' style='display:flex;gap:.5rem;flex-wrap:wrap;"
            "align-items:flex-end;margin-bottom:.8rem'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            f"<div class=field style='margin:0'><label>type</label><select name=type>{type_opts}</select></div>"
            "<div class=field style='margin:0;flex:1'><label>label</label>"
            "<input type=text name=label placeholder='e.g. OpenRouter'></div>"
            "<div class=field style='margin:0;flex:1'><label>API key (API-key types only)</label>"
            "<input type=password name=api_key autocomplete=off></div>"
            "<button class='btn btn-primary'>add / connect</button></form>"
            f"{''.join(provider_rows) or '<p class=muted>none configured yet.</p>'}</div>"
        )

        # -- Browse results (2026-09-30, see providers.list_models()) --
        main_browse = ""
        if browse:
            if not browse["ok"]:
                main_browse = (f"<div class=section><h2>models on {esc(browse['provider_label'])}</h2>"
                              f"<p class=err>{esc(browse['error'])}</p></div>")
            else:
                items = browse["items"]
                q = (browse.get("q") or "").strip().lower()
                shown = [m for m in items if not q or q in m["id"].lower() or q in m["label"].lower()]
                cap = 150
                truncated = len(shown) - cap
                shown = shown[:cap]

                def _result_row(m: dict) -> str:
                    alias_default = esc(m["label"] if m["label"] != m["id"] else m["id"].rsplit("/", 1)[-1])
                    return (
                        "<div class=list-row><div class=list-meta>"
                        f"<b>{esc(m['label'])}</b><small>{esc(m['id'])}</small></div>"
                        f"<form method=post action='/admin/models' style='display:flex;gap:.4rem;"
                        "align-items:center'>"
                        f"<input type=hidden name=csrf value='{csrf}'>"
                        f"<input type=hidden name=provider_id value='{browse['provider_id']}'>"
                        f"<input type=hidden name=model_name value='{esc(m['id'])}'>"
                        f"<input type=text name=alias value='{alias_default}' style='width:12em'>"
                        "<button class=btn>add</button></form></div>")

                main_browse = (
                    f"<div class=section><h2>models on {esc(browse['provider_label'])}</h2>"
                    f"<p class=muted>{len(items)} available"
                    f"{f', showing first {cap} of {len(shown) + max(truncated, 0)} matching' if truncated > 0 else ''}"
                    "-- filter narrows by id/name, alias is editable before you add one.</p>"
                    f"<form method=post action='/admin/providers/{browse['provider_id']}/models' "
                    "style='display:flex;gap:.4rem;margin-bottom:.6rem'>"
                    f"<input type=hidden name=csrf value='{csrf}'>"
                    f"<input type=text name=q value='{esc(browse.get('q') or '')}' placeholder='filter…' "
                    "style='flex:1'>"
                    "<button class=btn>filter</button></form>"
                    f"{''.join(_result_row(m) for m in shown) or '<p class=muted>no matches.</p>'}</div>"
                )

        # -- Models --
        enabled_providers = [p for p in all_providers if p["enabled"] and p["status"] == "connected"]
        provider_opts = "".join(f"<option value='{p['id']}'>{esc(p['label'])}</option>" for p in enabled_providers)
        all_models = models.list_all()
        provider_label_by_id = {p["id"]: p["label"] for p in all_providers}
        model_rows = []
        for m in all_models:
            provider_label = provider_label_by_id.get(m["provider_id"], "(no provider -- needs relinking)")
            model_rows.append(
                "<div class=list-row><div class=list-meta>"
                f"<b>{esc(m['alias'])}</b>"
                f"<small>{esc(provider_label)} · {esc(m['model_name'])} · "
                f"{'enabled' if m['enabled'] else 'disabled'}</small>"
                f"<form method=post action='/admin/models/{m['id']}/effort' style='display:flex;gap:.4rem;"
                "align-items:center;margin-top:.4rem'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                "<label class=muted style='font-size:.78rem'>reasoning effort</label>"
                f"<select name=reasoning_effort style='width:auto'>"
                + "".join(f"<option value='{v}'{' selected' if (m['reasoning_effort'] or '') == v else ''}>"
                         f"{v or '(no reasoning)'}</option>"
                         for v in ("", "minimal", "low", "medium", "high", "xhigh", "max"))
                + "</select><button class=btn>save</button></form>"
                "</div>"
                f"<div class=list-actions><form method=post action='/admin/models/{m['id']}/toggle'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                f"<button class=btn>{'disable' if m['enabled'] else 'enable'}</button></form>"
                f"<form method=post action='/admin/models/{m['id']}/delete'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                "<button class='btn btn-danger'>delete</button></form></div></div>")
        add_model_form = (
            "<form method=post action='/admin/models'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            "<div class=field><label>alias</label>"
            "<input type=text name=alias placeholder='e.g. ClaudeLite' required></div>"
            f"<div class=field><label>provider</label><select name=provider_id required>{provider_opts}</select></div>"
            "<div class=field><label>model name</label>"
            "<input type=text name=model_name placeholder=\"that provider's own model name\" required></div>"
            "<div class=field><label>reasoning effort (leave blank if the model doesn't use one)</label>"
            "<select name=reasoning_effort>"
            + "".join(f"<option value='{v}'>{v or '(no reasoning)'}</option>"
                     for v in ("", "minimal", "low", "medium", "high", "xhigh", "max"))
            + "</select></div>"
            "<button class='btn btn-primary btn-block'>add</button></form>"
        ) if provider_opts else "<p class=muted>connect a provider above first.</p>"
        main_models = (
            f"<div class=section><h2>Models</h2>"
            "<p class=muted>an alias + the provider it runs through + that provider's own model name.</p>"
            f"{add_model_form}"
            f"<div style='margin-top:1rem'>{''.join(model_rows) or '<p class=muted>none yet.</p>'}</div></div>"
        )

        # -- Primary + fallback chain --
        chain = models.get_chain(wsid)
        chain_ids = [c["id"] for c in chain]
        enabled_models = models.list_enabled()

        def _chain_select(name: str, selected: int | None) -> str:
            opts = "<option value=''>(none)</option>" + "".join(
                f"<option value='{m['id']}'{' selected' if m['id'] == selected else ''}>{esc(m['alias'])}</option>"
                for m in enabled_models)
            return f"<select name='{name}'>{opts}</select>"

        fallback_slots = chain_ids[1:] + [None] * max(0, 3 - len(chain_ids[1:]))
        chain_form = (
            "<form method=post action='/admin/models/chain'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            f"<div class=field><label>primary</label>{_chain_select('primary', chain_ids[0] if chain_ids else None)}</div>"
            + "".join(f"<div class=field><label>fallback {n + 1}</label>{_chain_select(f'fallback_{n}', v)}</div>"
                     for n, v in enumerate(fallback_slots))
            + "<button class='btn btn-primary btn-block'>save</button></form>"
        ) if enabled_models else "<p class=muted>enable a model above first.</p>"
        assistant_name = config.get("workspace", wsid, "assistant_name")
        main_chain = (
            f"<div class=section><h2>{esc(assistant_name)}'s primary &amp; fallback models</h2>"
            "<p class=muted>applies immediately, no restart needed. A fallback is tried only if an "
            "earlier one's provider fails for that turn.</p>"
            f"{chain_form}</div>"
        )

        self._settings_response(sess, "models", f"{e}{i}{main_provider}{main_browse}{main_models}{main_chain}")

    def providers_create_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        ptype = form.get("type") or ""
        label = form.get("label") or ""
        wsid = sess["workspace_id"]
        if ptype in providers.API_KEY_TYPES:
            ok, msg = providers.create_api_key(wsid, ptype, label, form.get("api_key") or "", sess["user_id"])
            return self.models_admin_form(sess, err=("" if ok else msg), info=(msg if ok else ""))
        if ptype in ("anthropic_oauth", "openai_oauth"):
            ok, result = providers.begin_oauth_manual(wsid, ptype, label, sess["user_id"])
            if not ok:
                return self.models_admin_form(sess, err=result)
            # The authorize link itself now shows persistently under the
            # provider's own row (see pending_display() in models_admin_form)
            # -- a real, copyable link/button, not a long URL crammed into
            # this one-shot flash message the way it used to be.
            return self.models_admin_form(sess, info="added -- see below to connect")
        if ptype in ("github_copilot", "xai_oauth"):
            ok, msg, data = providers.begin_device_flow(wsid, ptype, label, sess["user_id"])
            if not ok:
                return self.models_admin_form(sess, err=msg)
            return self.models_admin_form(sess, info="added -- see below to connect")
        return self.models_admin_form(sess, err="pick a provider type")

    def providers_delete_post(self, sess: dict, provider_id: str, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        try:
            pid = int(provider_id)
        except ValueError:
            return self.models_admin_form(sess, "bad id")
        providers.delete(pid)
        return self.models_admin_form(sess, info="removed")

    def providers_list_models_post(self, sess: dict, provider_id: str, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        try:
            pid = int(provider_id)
        except ValueError:
            return self.models_admin_form(sess, "bad id")
        provider = providers.get(pid)
        if provider is None:
            return self.models_admin_form(sess, "no such provider")
        ok, result = providers.list_models(provider)
        browse = {"provider_id": pid, "provider_label": provider["label"], "q": form.get("q") or "",
                 "ok": ok, "items": result if ok else None, "error": result if not ok else None}
        return self.models_admin_form(sess, browse=browse)

    def providers_oauth_complete_post(self, sess: dict, provider_id: str, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        try:
            pid = int(provider_id)
        except ValueError:
            return self.models_admin_form(sess, "bad id")
        ok, msg = providers.complete_oauth_manual(pid, form.get("pasted") or "")
        return self.models_admin_form(sess, err=("" if ok else msg), info=(msg if ok else ""))

    def providers_oauth_check_post(self, sess: dict, provider_id: str, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        try:
            pid = int(provider_id)
        except ValueError:
            return self.models_admin_form(sess, "bad id")
        ok, msg = providers.check_device_flow(pid)
        return self.models_admin_form(sess, err=("" if ok else msg), info=(msg if ok else ""))

    def models_create_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        try:
            provider_id = int(form.get("provider_id") or 0)
        except ValueError:
            return self.models_admin_form(sess, "bad provider")
        ok, msg = models.create(form.get("alias") or "", provider_id, form.get("model_name") or "",
                                form.get("reasoning_effort"))
        return self.models_admin_form(sess, err=("" if ok else msg), info=(msg if ok else ""))

    def models_toggle_post(self, sess: dict, model_id: str, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        try:
            mid = int(model_id)
        except ValueError:
            return self.models_admin_form(sess, "bad id")
        row = models.get(mid)
        if row is None:
            return self.models_admin_form(sess, "no such model")
        models.set_enabled(mid, not row["enabled"])
        return self.models_admin_form(sess)

    def models_delete_post(self, sess: dict, model_id: str, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        try:
            mid = int(model_id)
        except ValueError:
            return self.models_admin_form(sess, "bad id")
        models.delete(mid)
        return self.models_admin_form(sess, info="deleted")

    def models_effort_post(self, sess: dict, model_id: str, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        try:
            mid = int(model_id)
        except ValueError:
            return self.models_admin_form(sess, "bad id")
        ok, msg = models.set_reasoning_effort(mid, form.get("reasoning_effort"))
        return self.models_admin_form(sess, err=("" if ok else msg), info=(msg if ok else ""))

    def models_chain_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        ids = []
        for key in ["primary", "fallback_0", "fallback_1", "fallback_2"]:
            v = (form.get(key) or "").strip()
            if v:
                try:
                    ids.append(int(v))
                except ValueError:
                    return self.models_admin_form(sess, "bad model id")
        # de-dupe, preserving order -- the same model picked twice (e.g.
        # left as both primary and a fallback slot) would otherwise violate
        # model_chain's own (workspace_id, priority) shape for no benefit,
        # since retrying the exact same model on its own failure buys nothing.
        seen = set()
        ids = [i for i in ids if not (i in seen or seen.add(i))]
        models.set_chain(sess["workspace_id"], ids)
        return self.models_admin_form(sess, info="saved")

    # -- admin: web search/fetch domain rules (2026-09-14) --------------------
    def webtools_admin_form(self, sess: dict, err: str = "", info: str = ""):
        if sess["role"] != "admin":
            return self.forbidden()
        e = f"<p class=err>{esc(err)}</p>" if err else ""
        i = f"<p class=info>{esc(info)}</p>" if info else ""
        csrf = esc(sess["csrf"])

        def _rule_rows(kind: str) -> str:
            rows = webtools.rules(kind)
            if not rows:
                return "<p class=muted>none set</p>"
            out = []
            for r in rows:
                out.append(
                    "<div class=list-row><div class=list-meta><b>" + esc(r["pattern"]) + "</b></div>"
                    f"<div class=list-actions><form method=post action='/admin/webtools/rules/{r['id']}/delete'>"
                    f"<input type=hidden name=csrf value='{csrf}'><button class=btn>remove</button></form></div></div>")
            return "".join(out)

        status = ("<span class='chip active'>Tavily key set</span>" if webtools.configured()
                 else "<span class=chip>no Tavily key yet -- web_search will say so plainly, not fail obscurely</span>")

        # On/off for each tool, separately (2026-09-15, operator's own
        # ask) -- distinct from write_mode below, which only governs
        # non-GET fetch requests once fetch itself is already on.
        wsid = sess["workspace_id"]
        search_on = config.get("workspace", wsid, "web_search_enabled")
        fetch_on = config.get("workspace", wsid, "web_fetch_enabled")
        enabled_form = (
            "<div class=section><h2>on/off</h2>"
            "<form method=post action='/admin/webtools/enabled' style='display:flex;gap:1rem;"
            "flex-wrap:wrap;align-items:center'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            f"<label><input type=checkbox name=web_search_enabled{' checked' if search_on else ''}> "
            "web search</label>"
            f"<label><input type=checkbox name=web_fetch_enabled{' checked' if fetch_on else ''}> "
            "web fetch</label>"
            "<button class='btn btn-primary'>save</button></form></div>")

        write_mode = config.get("workspace", sess["workspace_id"], "web_fetch_write_mode")
        write_mode_tip = info_tip(
            "Off: every non-GET request is denied before the hostname is even resolved. Simulated: "
            "every real check still runs (denied_by is real and meaningful here too) but nothing is "
            "ever actually sent -- logged truthfully here, while what reaches her looks like an "
            "ordinary transient network failure, deliberately: this is for observing how she "
            "actually behaves when a write silently doesn't land, not behavior she's adjusted "
            "knowing it's a test. The log is the only place the truth lives -- check here, not "
            "what she says about why it failed. Real: an allowed write actually goes out.")
        write_mode_form = (
            f"<div class=section><h2>write mode {write_mode_tip}</h2>"
            "<p class=muted>Off refuses every write outright; simulated logs what would have "
            "happened without sending it; real actually sends it. Off can't jump straight to real "
            "-- simulated first, as two separate deliberate steps.</p>"
            "<form method=post action='/admin/webtools/write-mode' style='display:flex;gap:.4rem;"
            "align-items:center'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            "<select name=mode>"
            + "".join(f"<option value='{m}'{' selected' if write_mode == m else ''}>{m}</option>"
                     for m in ("off", "simulated", "real"))
            + "</select><button class='btn btn-primary'>save</button></form></div>")

        def _collapse_repeats(rows: list[dict]) -> list[dict]:
            """Consecutive simulated attempts at the SAME method+target
            collapse into one entry with a count (2026-09-14, operator's
            own concern: a retry loop against a simulated endpoint is
            noise at best in this view). The underlying log stays fully
            complete -- see recent_log() -- this only affects display.
            Only simulated rows collapse; a real ok/denied/failed run of
            repeats is left exactly as logged, since those are genuinely
            distinct events worth seeing individually."""
            out = []
            for row in rows:  # newest-first, same order recent_log() returns
                if (row.get("simulated") and out and out[-1].get("simulated")
                        and out[-1]["target"] == row["target"]):
                    out[-1]["_count"] = out[-1].get("_count", 1) + 1
                    continue
                out.append(dict(row))
            return out

        def _log_row(row: dict) -> str:
            count = row.get("_count", 1)
            if row.get("simulated"):
                outcome = ("<span class=chip style='background:#5a3d00;color:#ffcf66;border-color:#a66'>"
                          f"SIMULATED -- not sent{f' ×{count}' if count > 1 else ''}</span>")
            elif row.get("denied_by"):
                outcome = f"<span class=err>denied ({esc(row['denied_by'])})</span>"
            elif row["ok"]:
                outcome = "<span class=ok>ok</span>" + (f" · {row['status']}" if row.get("status") else "")
            else:
                outcome = "<span class=err>failed</span>" + (f" · {row['status']}" if row.get("status") else "")
            when = time.strftime("%m-%d %H:%M:%S", time.localtime(row["ts"]))
            meta = " · ".join(x for x in (
                esc(row.get("method") or ""), esc(row.get("agent") or ""), when) if x)
            payload = (f"<div class=muted style='font-family:monospace;font-size:.75rem;"
                      f"white-space:pre-wrap;margin-top:.25rem'>payload: {esc(row['payload'][:500])}</div>"
                      if row.get("payload") else "")
            return (
                "<div class=list-row><div class=list-meta>"
                f"<b>{esc(row['kind'])}</b> {outcome} <span class=muted>{meta}</span>"
                f"<small>{esc(row['target'])}{' — ' + esc(row['reason']) if row['reason'] else ''}</small>"
                f"{payload}</div></div>")
        log_rows = "".join(_log_row(row) for row in _collapse_repeats(webtools.recent_log(50)))

        main = (
            f"{e}{i}"
            f"<div class=section><h2>status</h2><p>{status}</p>"
            "<p class=muted>web_search is a direct Tavily call; web_fetch is our own hardened fetch -- "
            "every call, allowed or denied, real or simulated, is logged below in full.</p></div>"
            "<div class=section><h2>read blacklist</h2>"
            "<p class=muted>GET is open by default -- a domain here (or a <code>*.example.com</code> "
            "wildcard, which also blocks the bare domain) is the exception.</p>"
            "<form method=post action='/admin/webtools/rules' style='display:flex;gap:.4rem'>"
            f"<input type=hidden name=csrf value='{csrf}'><input type=hidden name=kind value=read_block>"
            "<input type=text name=pattern placeholder='example.com or *.example.com' style='flex:1'>"
            "<button class='btn btn-primary'>block</button></form>"
            f"{_rule_rows('read_block')}</div>"
            "<div class=section><h2>write allow-list</h2>"
            "<p class=muted>anything other than GET is closed by default -- only a domain listed "
            "here may be written to, and only when write mode below is simulated or real.</p>"
            "<form method=post action='/admin/webtools/rules' style='display:flex;gap:.4rem'>"
            f"<input type=hidden name=csrf value='{csrf}'><input type=hidden name=kind value=write_allow>"
            "<input type=text name=pattern placeholder='example.com or *.example.com' style='flex:1'>"
            "<button class='btn btn-primary'>allow</button></form>"
            f"{_rule_rows('write_allow')}</div>"
            f"{enabled_form}"
            f"{write_mode_form}"
            f"<div class=section><h2>recent activity</h2>{log_rows or '<p class=muted>nothing yet</p>'}</div>"
        )
        self._settings_response(sess, "webtools", main)

    def webtools_rule_add_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        ok, msg = webtools.add_rule(form.get("kind") or "", form.get("pattern") or "")
        return self.webtools_admin_form(sess, err=("" if ok else msg), info=(msg if ok else ""))

    def webtools_rule_delete_post(self, sess: dict, rule_id: str, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        try:
            rid = int(rule_id)
        except ValueError:
            return self.webtools_admin_form(sess, "bad id")
        webtools.remove_rule(rid)
        return self.webtools_admin_form(sess, info="removed")

    def webtools_enabled_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        config.set("workspace", sess["workspace_id"], "web_search_enabled", "web_search_enabled" in form)
        config.set("workspace", sess["workspace_id"], "web_fetch_enabled", "web_fetch_enabled" in form)
        return self.webtools_admin_form(sess, info="saved")

    def webtools_write_mode_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        try:
            config.set("workspace", sess["workspace_id"], "web_fetch_write_mode",
                      form.get("mode") or "off")
        except ValueError as exc:
            return self.webtools_admin_form(sess, err=str(exc))
        return self.webtools_admin_form(sess, info="saved")

    # -- admin: memory backend (2026-10-01) -- where her typed memory
    # actually lives: local (default) or Nodrya, via config.py's
    # memory_backend switch and memory.py's NodryaMemoryBackend. No UI
    # existed for this before -- the three nodrya_* keys were readable
    # by config.get() the moment the backend class shipped, but nothing
    # in server.py ever wrote them, so the switch was unreachable except
    # by hand-editing the database. The token field here is the one real
    # secret: encrypted with crypto.encrypt() before it's ever written,
    # never echoed back once set (the form shows "set" not the value),
    # and a blank submit leaves whatever's already stored untouched --
    # same convention as every password-style field elsewhere in this
    # app. The URL isn't encrypted -- NodryaMemoryBackend always sends
    # the token as a separate Bearer header, never embedded in the URL,
    # so the URL alone isn't a credential, just an endpoint.
    def memory_backend_admin_form(self, sess: dict, err: str = "", info: str = ""):
        if sess["role"] != "admin":
            return self.forbidden()
        e = f"<p class=err>{esc(err)}</p>" if err else ""
        i = f"<p class=info>{esc(info)}</p>" if info else ""
        csrf = esc(sess["csrf"])
        wsid = sess["workspace_id"]

        backend = config.get("workspace", wsid, "memory_backend")
        url = config.get("workspace", wsid, "nodrya_mcp_url") or ""
        token_enc = config.get("workspace", wsid, "nodrya_mcp_token") or ""
        category_id = int(config.get("workspace", wsid, "nodrya_memory_category_id") or 0)
        configured = bool(url and token_enc and category_id > 0)

        active = ("<span class='chip active'>using Nodrya</span>" if backend == "nodrya"
                 else "<span class=chip>using local storage</span>")
        conn_status = ("<span class='chip active'>connection details set</span>" if configured
                      else "<span class=chip>not configured yet -- fill in all three fields below</span>")

        backend_form = (
            "<div class=section><h2>active backend</h2>"
            f"<p>{active}</p>"
            "<p class=muted>Local keeps every memory in Nori's own database, same as today. "
            "Nodrya routes her typed memory reads/writes through your Nodrya account instead, "
            "with a local write-through cache so recall still works instantly -- see "
            "memory.py's NodryaMemoryBackend. Switching to Nodrya is blocked here until the "
            "connection below is fully filled in.</p>"
            "<form method=post action='/admin/memorybackend/backend'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            "<div class=field><label>backend</label><select name=memory_backend>"
            f"<option value=local{' selected' if backend == 'local' else ''}>local (default)</option>"
            f"<option value=nodrya{' selected' if backend == 'nodrya' else ''}"
            f"{' disabled' if not configured else ''}>nodrya</option>"
            "</select></div>"
            "<button class='btn btn-primary'>save</button></form></div>")

        token_placeholder = "leave blank to keep the current token" if token_enc else "paste your Nodrya token"
        category_value = str(category_id) if category_id else ""
        conn_form = (
            "<div class=section><h2>Nodrya connection</h2>"
            f"<p>{conn_status}</p>"
            "<form method=post action='/admin/memorybackend/connection'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            "<div class=field><label>MCP endpoint URL</label>"
            f"<input name=nodrya_mcp_url value='{esc(url)}' placeholder='https://...'></div>"
            "<div class=field><label>MCP token (write scope)</label>"
            f"<input type=password name=nodrya_mcp_token placeholder='{esc(token_placeholder)}'></div>"
            "<div class=field><label>memory category id</label>"
            f"<input type=number name=nodrya_memory_category_id value='{category_value}' min=0></div>"
            "<button class='btn btn-primary'>save connection</button></form></div>")

        migrate_form = ""
        if configured:
            migrate_form = (
                "<div class=section><h2>migrate existing memories</h2>"
                "<p class=muted>Copies every memory currently stored locally -- for every "
                "household member, not just you -- into Nodrya as a real note each. Safe to run "
                "more than once: anything already copied is skipped, never duplicated. Local "
                "rows aren't touched or deleted -- this only adds to Nodrya. Can take a while "
                "with a lot of history; the page will wait for it to finish.</p>"
                "<form method=post action='/admin/memorybackend/migrate' "
                "data-confirm=\"Copy every local memory into Nodrya now? This makes real writes "
                "to your Nodrya account.\">"
                f"<input type=hidden name=csrf value='{csrf}'>"
                "<button class='btn btn-primary'>migrate local memories to Nodrya</button></form></div>")

        self._settings_response(sess, "memorybackend", e + i + backend_form + conn_form + migrate_form)

    def memory_backend_connection_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        wsid = sess["workspace_id"]
        url = (form.get("nodrya_mcp_url") or "").strip()
        token = (form.get("nodrya_mcp_token") or "").strip()
        cat_raw = (form.get("nodrya_memory_category_id") or "").strip()
        try:
            category_id = int(cat_raw) if cat_raw else 0
        except ValueError:
            return self.memory_backend_admin_form(sess, err="category id must be a whole number")
        if category_id < 0:
            return self.memory_backend_admin_form(sess, err="category id can't be negative")
        config.set("workspace", wsid, "nodrya_mcp_url", url)
        config.set("workspace", wsid, "nodrya_memory_category_id", category_id)
        if token:
            config.set("workspace", wsid, "nodrya_mcp_token", crypto.encrypt(token))
        return self.memory_backend_admin_form(sess, info="connection saved")

    def memory_backend_switch_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        wsid = sess["workspace_id"]
        backend = (form.get("memory_backend") or "local").strip()
        if backend not in ("local", "nodrya"):
            return self.memory_backend_admin_form(sess, err=f"{backend!r} isn't a known backend")
        if backend == "nodrya":
            url = config.get("workspace", wsid, "nodrya_mcp_url") or ""
            token_enc = config.get("workspace", wsid, "nodrya_mcp_token") or ""
            category_id = int(config.get("workspace", wsid, "nodrya_memory_category_id") or 0)
            if not (url and token_enc and category_id > 0):
                return self.memory_backend_admin_form(
                    sess, err="fill in the Nodrya connection below before switching to it")
        config.set("workspace", wsid, "memory_backend", backend)
        return self.memory_backend_admin_form(sess, info="saved")

    def memory_backend_migrate_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        result = memory.migrate_local_to_nodrya(sess["workspace_id"])
        if not result["ok"]:
            return self.memory_backend_admin_form(sess, err=result["error"])
        msg = f"migrated {result['migrated']}, already copied {result['skipped']}"
        if result["failed"]:
            msg += f", {len(result['failed'])} failed -- check the logs"
            print(f"server.memory_backend_migrate_post: {len(result['failed'])} row(s) failed: "
                 f"{result['failed']}", flush=True)
        return self.memory_backend_admin_form(sess, info=msg)

    # -- admin: Home Assistant (2026-09-15) -- discover from the real
    # instance, then an explicit per-entity checklist to expose. Discovery
    # and the checklist are two separate forms/routes on purpose, mirroring
    # homeassistant.py's own discover_entities()/list_exposed_entities()
    # split: rediscovering never resubmits exposure state, and saving
    # exposure never re-hits the real HA API.
    def homeassistant_admin_form(self, sess: dict, err: str = "", info: str = ""):
        if sess["role"] != "admin":
            return self.forbidden()
        e = f"<p class=err>{esc(err)}</p>" if err else ""
        i = f"<p class=info>{esc(info)}</p>" if info else ""
        csrf = esc(sess["csrf"])

        status = ("<span class='chip active'>connected</span>" if homeassistant.configured()
                 else "<span class=chip>HOME_ASSISTANT_URL/HOME_ASSISTANT_API_KEY aren't set in "
                      ".env yet</span>")

        rows = homeassistant.all_entities()
        by_domain: dict = {}
        for r in rows:
            by_domain.setdefault(r["domain"], []).append(r)
        checklist = ""
        if not rows:
            checklist = "<p class=muted>nothing discovered yet -- use discover/refresh above.</p>"
        else:
            sections = []
            for domain in sorted(by_domain):
                entity_rows = "".join(
                    "<div class=list-row><div class=list-meta>"
                    f"<b>{esc(r['friendly_name'] or r['entity_id'])}</b>"
                    f"<small>{esc(r['entity_id'])}</small></div>"
                    "<div class=list-actions style='gap:1rem'>"
                    f"<label><input type=checkbox id='en__{esc(r['entity_id'])}' "
                    f"name='enabled__{esc(r['entity_id'])}'"
                    f"{' checked' if r['enabled'] else ''}> enabled</label>"
                    f"<label><input type=checkbox data-ha-peer='{esc(r['entity_id'])}' "
                    f"name='peer__{esc(r['entity_id'])}'"
                    f"{' checked' if r['enabled_for_peers'] else ''}> enabled for peers</label>"
                    "</div></div>"
                    for r in by_domain[domain])
                sections.append(f"<div class=section><h2>{esc(domain)}</h2>{entity_rows}</div>")
            # Peer-enabled implies Nori-enabled -- enforced in code too
            # (homeassistant.set_exposure()), this is the UI half of that
            # same guarantee (2026-09-15, operator's own explicit ask: "don't
            # rely on him checking both"). addEventListener throughout, same
            # convention the chat composer's own JS already settled on.
            enforce_js = (
                "<script>(function(){"
                "document.querySelectorAll('[data-ha-peer]').forEach(function(peerCb){"
                "var enCb=document.getElementById('en__'+peerCb.dataset.haPeer);"
                "if(!enCb)return;"
                "peerCb.addEventListener('change',function(){if(peerCb.checked)enCb.checked=true;});"
                "enCb.addEventListener('change',function(){if(!enCb.checked)peerCb.checked=false;});"
                "});"
                "})();</script>")
            checklist = (
                f"<form method=post action='/admin/homeassistant/exposure'>"
                f"<input type=hidden name=csrf value='{csrf}'>{''.join(sections)}"
                f"<button class='btn btn-primary btn-block'>save exposure</button></form>{enforce_js}")

        def _log_row(row: dict) -> str:
            outcome = ("<span class=ok>ok</span>" if row["ok"]
                      else f"<span class=err>failed{' — ' + esc(row['reason']) if row['reason'] else ''}</span>")
            when = time.strftime("%m-%d %H:%M:%S", time.localtime(row["ts"]))
            meta = " · ".join(x for x in (
                esc(row.get("agent") or ""), esc(row.get("service") or ""), when) if x)
            return (
                "<div class=list-row><div class=list-meta>"
                f"<b>{esc(row['kind'])}</b> {outcome} <span class=muted>{meta}</span>"
                f"<small>{esc(row.get('entity_id') or '')}</small></div></div>")
        log_rows = "".join(_log_row(row) for row in homeassistant.recent_log(50))

        main = (
            f"{e}{i}"
            f"<div class=section><h2>status</h2><p>{status}</p>"
            "<p class=muted>Everything below is disabled by default -- she's unaware of any "
            "entity you haven't explicitly checked. <b>Enabled</b> means she can see and use "
            "it. <b>Enabled for peers</b> means a connected peer can reach it too, on top of "
            "that peer's own trust level (prompt/full) -- checking it also checks Enabled, "
            "since a peer reaching something she can't see herself wouldn't make sense. No "
            "separate rule by device type here -- a lock and a light go through the same "
            "checklist; you decide per entity.</p>"
            f"<form method=post action='/admin/homeassistant/discover'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            "<button class='btn btn-primary'>discover / refresh from Home Assistant</button>"
            "</form></div>"
            f"<div class=section><h2>exposed entities</h2>{checklist}</div>"
            f"<div class=section><h2>recent activity</h2>{log_rows or '<p class=muted>nothing yet</p>'}</div>"
        )
        self._settings_response(sess, "homeassistant", main)

    def homeassistant_discover_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        res = homeassistant.discover_entities()
        if not res.get("ok"):
            return self.homeassistant_admin_form(sess, err=res.get("reason") or "discovery failed")
        return self.homeassistant_admin_form(sess, info=f"discovered {res['count']} entities")

    def homeassistant_exposure_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        for row in homeassistant.all_entities():
            eid = row["entity_id"]
            homeassistant.set_exposure(
                eid, enabled=f"enabled__{eid}" in form, enabled_for_peers=f"peer__{eid}" in form)
        return self.homeassistant_admin_form(sess, info="saved")

    # -- personal: scheduled tasks (2026-09-15, see schedules.py) --------
    _SCHEDULE_CREATOR_LABEL = {"user": "him", "nori": "her", "peer": None}  # peer resolved to a real name below

    def _schedule_creator_badge(self, row: dict) -> str:
        if row["created_by_type"] == "peer":
            return esc(row.get("created_by_peer_name") or "a peer")
        return self._SCHEDULE_CREATOR_LABEL.get(row["created_by_type"], row["created_by_type"])

    def _schedule_when(self, row: dict) -> str:
        if row["schedule_type"] == "interval":
            return f"every {row['interval_min']} min"
        return f"daily at {row['time_hour']:02d}:{row['time_minute']:02d}"

    def _schedule_deliver_label(self, row: dict, peer_names: dict) -> str:
        peer_name = esc(peer_names.get(row["deliver_peer_id"], "?")) if row["deliver_peer_id"] else "?"
        return {"user": "him", "peer": peer_name, "both": f"him + {peer_name}", "none": "nobody (act only)"
               }.get(row["deliver_to"], row["deliver_to"])

    # -- edit history (2026-09-15, operator's own follow-up): "not just a
    # last-modified field, he wants the trail" -- rendered from
    # schedules.history_for(), the same actor-tagged event log shape
    # memory.py's memory_events already established, reused rather than
    # invented a third time (see schedules.py's own module docstring).
    _SCHEDULE_FIELD_LABELS = {"name": "name", "instruction": "instruction",
                              "required_tool": "required tool", "deliver_to": "deliver to",
                              "deliver_peer_id": "deliver-to peer", "schedule_type": "schedule type",
                              "interval_min": "interval (min)", "time_hour": "hour",
                              "time_minute": "minute", "enabled": "enabled"}

    def _schedule_fmt_value(self, field: str, val, peer_names: dict) -> str:
        if val is None:
            return "(none)"
        if field == "deliver_peer_id":
            return esc(peer_names.get(val, f"#{val}"))
        if field == "enabled":
            return "yes" if val else "no"
        return esc(str(val))

    # -- shared history rendering (2026-09-16) -- schedules.py's own
    # edit-history UI (2026-09-15) generalized the moment tasks.py's
    # history needed the identical "who/when/what changed, or what was
    # just done" rendering rather than a second copy. Every field-name
    # translation and value-formatting quirk stays with its OWN caller
    # (schedules' deliver_peer_id needing a peer-name lookup, say) --
    # this only owns the actor label and the common event-list shape.
    def _history_who(self, actor: str, actor_peer_name: str | None) -> str:
        if actor == "peer":
            return esc(actor_peer_name or "a peer")
        return {"user": "him", "nori": "her"}.get(actor, esc(actor))

    def _render_history(self, events: list[dict], *, field_labels: dict, fmt_fn, noun: str,
                        plain_actions: dict | None = None) -> str:
        """fmt_fn(field, value) -> escaped display string. plain_actions
        maps an action with no diff and no note to its own fixed label
        (e.g. {'created': 'created this X'}); anything else with neither
        a diff nor a note just shows its own action word."""
        if not events:
            return ""
        plain_actions = plain_actions or {}
        def _one(ev: dict) -> str:
            who = self._history_who(ev["actor"], ev.get("actor_peer_name"))
            when = time.strftime("%b %d, %H:%M", time.localtime(ev["ts"]))
            if ev["changes"]:
                detail = "; ".join(
                    f"{field_labels.get(f, f)}: {fmt_fn(f, c['old'])} &rarr; {fmt_fn(f, c['new'])}"
                    for f, c in ev["changes"].items())
                if ev.get("note"):
                    detail += f" ({esc(ev['note'])})"
            elif ev.get("note"):
                detail = esc(ev["note"])
            else:
                detail = plain_actions.get(ev["action"], esc(ev["action"]))
            return (f"<div class=list-row><div class=list-meta>"
                   f"<small>{esc(when)} &middot; <b>{who}</b> {esc(ev['action'])}</small>"
                   f"<small class=muted>{detail}</small></div></div>")
        return (f"<details class=sched-history><summary>{esc(noun)} history ({len(events)})</summary>"
               f"{''.join(_one(ev) for ev in events)}</details>")

    def _schedule_history_html(self, schedule_id: int, peer_names: dict) -> str:
        events = schedules.history_for(schedule_id)
        return self._render_history(
            events, field_labels=self._SCHEDULE_FIELD_LABELS,
            fmt_fn=lambda f, v: self._schedule_fmt_value(f, v, peer_names),
            noun="edit", plain_actions={"created": "created this schedule"})

    def _schedule_form(self, sess: dict, csrf: str, peers_list: list, *, edit_row: dict | None = None) -> str:
        e = edit_row or {}
        action = "/settings/schedules/update" if edit_row else "/settings/schedules"
        peer_opts = "".join(
            f"<option value='{p['id']}'{' selected' if e.get('deliver_peer_id') == p['id'] else ''}>"
            f"{esc(p['name'])}</option>" for p in peers_list)
        deliver_to = e.get("deliver_to", "user")
        deliver_opts = "".join(
            f"<option value='{v}'{' selected' if deliver_to == v else ''}>{lbl}</option>"
            for v, lbl in (("user", "him"), ("peer", "a peer"), ("both", "him + a peer"),
                           ("none", "nobody (act, don't report)")))
        schedule_type = e.get("schedule_type", "interval")
        type_opts = "".join(
            f"<option value='{v}'{' selected' if schedule_type == v else ''}>{lbl}</option>"
            for v, lbl in (("interval", "every N minutes"), ("time", "daily at a specific time")))
        hidden_id = f"<input type=hidden name=schedule_id value='{e['id']}'>" if edit_row else ""
        return (
            f"<form method=post action='{action}' class='sched-form'>"
            f"<input type=hidden name=csrf value='{csrf}'>{hidden_id}"
            f"<div class=field><label>name<input type=text name=name required "
            f"value='{esc(e.get('name', ''))}'></label></div>"
            f"<div class=field><label>instruction (free text -- what to actually do when this fires)"
            f"<textarea name=instruction rows=3 required>{esc(e.get('instruction', ''))}</textarea></label></div>"
            f"<div class=field><label>required tool (optional -- exact tool name, checked live before "
            f"firing)<input type=text name=required_tool value='{esc(e.get('required_tool') or '')}' "
            f"placeholder='e.g. ha_control'></label></div>"
            f"<div class=field><label>deliver to<select name=deliver_to>{deliver_opts}</select></label> "
            + (f"<label>which peer<select name=deliver_peer_id><option value=''>--</option>{peer_opts}"
               f"</select></label>" if peers_list else "<span class=muted>no peers configured</span>")
            + "</div>"
            f"<div class=field><label>schedule<select name=schedule_type>{type_opts}</select></label> "
            f"<label>interval (min)<input type=number name=interval_min min={schedules.MIN_INTERVAL_MIN} "
            f"max={schedules.MAX_INTERVAL_MIN} value='{e.get('interval_min') or 15}'></label> "
            f"<label>hour (0-23)<input type=number name=time_hour min=0 max=23 "
            f"value='{e.get('time_hour') if e.get('time_hour') is not None else 8}'></label> "
            f"<label>minute (0-59)<input type=number name=time_minute min=0 max=59 "
            f"value='{e.get('time_minute') if e.get('time_minute') is not None else 0}'></label></div>"
            f"<button class='btn btn-primary'>{'save changes' if edit_row else 'create schedule'}</button>"
            + (f" <a class=btn href='/settings?tab=schedules'>cancel</a>" if edit_row else "")
            + "</form>")

    def schedules_admin_form(self, sess: dict, err: str = "", info: str = "", *, edit_id: str = ""):
        e = f"<p class=err>{esc(err)}</p>" if err else ""
        i = f"<p class=info>{esc(info)}</p>" if info else ""
        csrf = esc(sess["csrf"])
        peers_list = peers.list_peers(sess)
        peer_names = {p["id"]: p["name"] for p in peers_list}

        edit_row = None
        if edit_id:
            try:
                edit_row = schedules.get_for_user(sess, int(edit_id))
            except ValueError:
                edit_row = None

        rows = schedules.list_for_user(sess)["schedules"]
        if not rows:
            list_html = "<p class=muted>no scheduled tasks yet.</p>"
        else:
            def _row(r: dict) -> str:
                status_note = ""
                if r.get("last_status") and r["last_status"] != "ok":
                    status_note = (f"<div class=err style='margin-top:.2rem'>last run: "
                                   f"{esc(r['last_status'])}{' -- ' + esc(r['last_error']) if r.get('last_error') else ''}"
                                   f"</div>")
                next_txt = time.strftime("%b %d, %H:%M", time.localtime(r["next_run_ts"]))
                return (
                    "<div class=list-row><div class=list-meta>"
                    f"<b>{esc(r['name'])}</b> "
                    f"<span class='hist-badge'>{self._schedule_creator_badge(r)}</span> "
                    f"<span class='hist-badge{'' if r['enabled'] else ' hist-kind-off'}'>"
                    f"{'enabled' if r['enabled'] else 'disabled'}</span>"
                    f"<small>{esc(self._schedule_when(r))} · next: {next_txt} · "
                    f"delivers to: {self._schedule_deliver_label(r, peer_names)}"
                    f"{' · needs ' + esc(r['required_tool']) if r['required_tool'] else ''}</small>"
                    f"<small class=muted>{esc(r['instruction'])}</small>{status_note}"
                    f"{self._schedule_history_html(r['id'], peer_names)}</div>"
                    "<div class=list-actions>"
                    f"<a class=btn href='/settings?tab=schedules&amp;edit={r['id']}'>edit</a>"
                    f"<form method=post action='/settings/schedules/{r['id']}/toggle' style='display:inline'>"
                    f"<input type=hidden name=csrf value='{csrf}'>"
                    f"<button class=btn>{'disable' if r['enabled'] else 'enable'}</button></form>"
                    f"<form method=post action='/settings/schedules/{r['id']}/delete' style='display:inline' "
                    "data-confirm='Delete this schedule?'>"
                    f"<input type=hidden name=csrf value='{csrf}'>"
                    "<button class='btn btn-danger'>delete</button></form></div></div>")
            list_html = "".join(_row(r) for r in rows)

        main = (
            f"{e}{i}"
            "<div class=section><p class=muted>Wakes her up later -- on an interval or at a specific "
            "daily time -- with a free-text instruction to act on. No fixed menu of task types: "
            "write it the way you'd tell a person what to do. If it needs a specific tool, name it "
            "as \"required tool\" -- checked live right before firing, so a disabled or unavailable "
            "tool is a clear, logged skip instead of her discovering it mid-turn. Runs regardless of "
            "quiet hours or the ping window -- those still govern whether you're actually pinged about "
            "it, never whether it happens.</p></div>"
            f"<div class=section><h2>{'edit schedule' if edit_row else 'new schedule'}</h2>"
            f"{self._schedule_form(sess, csrf, peers_list, edit_row=edit_row)}</div>"
            f"<div class=section><h2>your scheduled tasks</h2>{list_html}</div>"
        )
        self._settings_response(sess, "schedules", main)

    def schedules_create_post(self, sess: dict, form: dict):
        try:
            interval_min = int(form["interval_min"]) if form.get("interval_min") not in (None, "") else None
            time_hour = int(form["time_hour"]) if form.get("time_hour") not in (None, "") else None
            time_minute = int(form["time_minute"]) if form.get("time_minute") not in (None, "") else None
            deliver_peer_id = int(form["deliver_peer_id"]) if form.get("deliver_peer_id") else None
        except ValueError:
            return self.schedules_admin_form(sess, err="bad number in one of the schedule fields")
        r = schedules.create(
            sess, name=form.get("name", ""), instruction=form.get("instruction", ""),
            required_tool=form.get("required_tool") or None, deliver_to=form.get("deliver_to", "user"),
            deliver_peer_id=deliver_peer_id, schedule_type=form.get("schedule_type", "interval"),
            interval_min=interval_min, time_hour=time_hour, time_minute=time_minute,
            created_by_type="user")
        if r.get("error"):
            return self.schedules_admin_form(sess, err=r["error"])
        return self.schedules_admin_form(sess, info=f"created \"{form.get('name', '')}\"")

    def schedules_update_post(self, sess: dict, form: dict):
        try:
            schedule_id = int(form.get("schedule_id", ""))
            interval_min = int(form["interval_min"]) if form.get("interval_min") not in (None, "") else None
            time_hour = int(form["time_hour"]) if form.get("time_hour") not in (None, "") else None
            time_minute = int(form["time_minute"]) if form.get("time_minute") not in (None, "") else None
            deliver_peer_id = int(form["deliver_peer_id"]) if form.get("deliver_peer_id") else None
        except ValueError:
            return self.schedules_admin_form(sess, err="bad number in one of the schedule fields")
        r = schedules.edit(
            sess, schedule_id, name=form.get("name", ""), instruction=form.get("instruction", ""),
            required_tool=form.get("required_tool") or None, deliver_to=form.get("deliver_to", "user"),
            deliver_peer_id=deliver_peer_id, schedule_type=form.get("schedule_type", "interval"),
            interval_min=interval_min, time_hour=time_hour, time_minute=time_minute)
        if r.get("error"):
            return self.schedules_admin_form(sess, err=r["error"], edit_id=form.get("schedule_id", ""))
        return self.schedules_admin_form(sess, info="saved")

    def schedules_toggle_post(self, sess: dict, schedule_id: str, form: dict):
        try:
            row = schedules.get_for_user(sess, int(schedule_id))
        except ValueError:
            row = None
        if row is None:
            return self.schedules_admin_form(sess, err="no such schedule")
        schedules.set_enabled(sess, row["id"], not row["enabled"])
        return self.schedules_admin_form(sess, info="saved")

    def schedules_delete_post(self, sess: dict, schedule_id: str, form: dict):
        try:
            r = schedules.delete(sess, int(schedule_id))
        except ValueError:
            r = {"error": "bad id"}
        if r.get("error"):
            return self.schedules_admin_form(sess, err=r["error"])
        return self.schedules_admin_form(sess, info="deleted")

    # -- settings: full task record, including closed (2026-09-16, the
    # operator's own explicit ask) -- read-only here on purpose. Every
    # write path (add/edit/close) already lives on the board itself
    # (/board/new, /board/task/<id>) -- this tab exists so a closed task
    # doesn't just vanish from where he can see it, not to duplicate a
    # second set of forms for the same actions.
    def tasks_admin_form(self, sess: dict, *, category: str | None = None):
        r = tasks.list_for_user(sess, status=None, category=category)
        if r.get("error"):
            category, r = None, tasks.list_for_user(sess, status=None)
        rows = r["tasks"]
        # Category filter (2026-09-16, operator's own addition) -- chips,
        # not a form: a plain link per category (?tab=tasks&cat=x) is
        # enough, no submit/JS needed, same query-string-driven filtering
        # /history already uses for its own search. The visual-marker half
        # of "usable, not just stored" lives on the board's own cards
        # instead (a real filter there would mean reloading the whole
        # chat page just to change it) -- this tab is where a real filter
        # actually fits, since it's already a dedicated page.
        def _chip(value: str | None, label: str) -> str:
            active = " active" if category == value else ""
            href = "/settings?tab=tasks" + (f"&cat={value}" if value else "")
            return f"<a class='chip{active}' href='{href}'>{esc(label)}</a>"
        cat_names = [c["name"] for c in catalog.list_categories(sess, tasks.CATEGORY_DOMAIN)["categories"]]
        chips = _chip(None, "all") + "".join(_chip(c, c) for c in cat_names)
        filter_html = f"<div class=settings-tabs style='margin-bottom:.8rem'>{chips}</div>"
        if not rows:
            list_html = "<p class=muted>no tasks here yet.</p>"
        else:
            def _row(t: dict) -> str:
                due_html = f" &middot; {esc(_due_label(t['due_ts']))}" if t["due_ts"] else ""
                recur_html = f" &middot; repeats {esc(_recur_label(t))}" if t["recur_type"] else ""
                return (
                    "<div class=list-row><div class=list-meta>"
                    f"<b><a href='/board/task/{t['id']}'>{esc(t['name'])}</a></b> "
                    f"<span class='hist-badge'>{self._task_creator_badge(t)}</span> "
                    f"<span class='hist-badge'>{esc(t['priority'])}</span> "
                    f"<span class='hist-badge'>{esc(t['category'])}</span> "
                    f"<span class='hist-badge{'' if t['status'] == 'open' else ' hist-kind-off'}'>"
                    f"{esc(t['status'])}</span>"
                    f"<small>{due_html}{recur_html}</small>"
                    f"{self._task_history_html(t['id'])}</div></div>")
            list_html = "".join(_row(t) for t in rows)
        main = (
            "<div class=section><p class=muted>Every task, open or closed -- the board itself only "
            "shows open ones. Add or edit from the board; this is the full record.</p></div>"
            f"<div class=section>{filter_html}<h2>tasks ({len(rows)})</h2>{list_html}</div>"
        )
        self._settings_response(sess, "tasks", main)

    def notes_admin_form(self, sess: dict, *, category: str | None = None):
        csrf = esc(sess["csrf"])
        # Flagged-for-review (2026-09-16, his own instruction: apply the
        # same peer-triggered destructive-op handling memory.py's forget()
        # established, consistently) -- same "keep"/"remove" review shape
        # as the memory settings tab's own flagged-for-review section.
        flags = notes.removal_candidates(sess)
        flag_html = ""
        if flags:
            def _flag_row(f: dict) -> str:
                return (
                    f"<div class=list-row><div class=list-meta>"
                    f"<b>{esc(f['note_title'])}</b>"
                    f"<small>a connected peer asked to delete this</small></div>"
                    f"<div class=list-actions>"
                    f"<form method=post action='/settings/notes/resolve'>"
                    f"<input type=hidden name=csrf value='{csrf}'>"
                    f"<input type=hidden name=note_id value='{f['note_id']}'>"
                    f"<input type=hidden name=action value=dismiss>"
                    f"<button class=btn>keep</button></form>"
                    f"<form method=post action='/settings/notes/resolve'>"
                    f"<input type=hidden name=csrf value='{csrf}'>"
                    f"<input type=hidden name=note_id value='{f['note_id']}'>"
                    f"<input type=hidden name=action value=remove>"
                    f"<button class='btn btn-danger'>remove</button></form></div></div>")
            flag_html = f"<div class=section><h2>flagged for review</h2>{''.join(_flag_row(f) for f in flags)}</div>"
        r = notes.list_for_user(sess, category=category)
        if r.get("error"):
            category, r = None, notes.list_for_user(sess)
        rows = r["notes"]
        def _chip(value: str | None, label: str) -> str:
            active = " active" if category == value else ""
            href = "/settings?tab=notes" + (f"&cat={value}" if value else "")
            return f"<a class='chip{active}' href='{href}'>{esc(label)}</a>"
        cat_names = [c["name"] for c in catalog.list_categories(sess, notes.CATEGORY_DOMAIN)["categories"]]
        chips = _chip(None, "all") + "".join(_chip(c, c) for c in cat_names)
        filter_html = f"<div class=settings-tabs style='margin-bottom:.8rem'>{chips}</div>"
        if not rows:
            list_html = "<p class=muted>no notes here yet.</p>"
        else:
            def _row(n: dict) -> str:
                return (
                    "<div class=list-row><div class=list-meta>"
                    f"<b><a href='/board/note/{n['id']}'>{esc(n['title'])}</a></b> "
                    f"<span class='hist-badge'>{self._note_creator_badge(n)}</span> "
                    f"<span class='hist-badge'>{esc(n['category'])}</span>"
                    f"{self._note_history_html(n['id'])}</div></div>")
            list_html = "".join(_row(n) for n in rows)
        main = (
            "<div class=section><p class=muted>Every note that currently exists -- deleting one on "
            "the board removes it here too (delete is real for notes, unlike tasks). A peer-requested "
            "delete never removes one outright -- it lands in \"flagged for review\" above instead, "
            "for him to actually confirm. Add or edit from the board; this is the full record.</p></div>"
            f"{flag_html}"
            f"<div class=section>{filter_html}<h2>notes ({len(rows)})</h2>{list_html}</div>"
        )
        self._settings_response(sess, "notes", main)

    def notes_resolve_post(self, sess: dict, form: dict):
        try:
            note_id = int(form.get("note_id", ""))
        except ValueError:
            return self.notes_admin_form(sess)
        notes.resolve_removal_flag(sess, note_id, form.get("action", ""))
        return self.notes_admin_form(sess)

    def reminders_admin_form(self, sess: dict, *, category: str | None = None):
        r = reminders.list_for_user(sess, status=None, category=category)
        rows = r["reminders"]
        def _chip(value: str | None, label: str) -> str:
            active = " active" if category == value else ""
            href = "/settings?tab=reminders" + (f"&cat={value}" if value else "")
            return f"<a class='chip{active}' href='{href}'>{esc(label)}</a>"
        cat_names = [c["name"] for c in catalog.list_categories(sess, reminders.CATEGORY_DOMAIN)["categories"]]
        chips = _chip(None, "all") + "".join(_chip(c, c) for c in cat_names)
        filter_html = f"<div class=settings-tabs style='margin-bottom:.8rem'>{chips}</div>"
        if not rows:
            list_html = "<p class=muted>no reminders here yet.</p>"
        else:
            def _row(rem: dict) -> str:
                return (
                    "<div class=list-row><div class=list-meta>"
                    f"<b><a href='/board/reminder/{rem['id']}'>{esc(rem['name'])}</a></b> "
                    f"<span class='hist-badge'>{self._reminder_creator_badge(rem)}</span> "
                    f"<span class='hist-badge'>{esc(rem['category'])}</span> "
                    f"<span class='hist-badge{'' if rem['status'] == 'active' else ' hist-kind-off'}'>"
                    f"{esc(rem['status'])}</span>"
                    f"<small>{esc(self._reminder_recur_label(rem))} &middot; next due "
                    f"{esc(_due_label(rem['next_due_ts']))}</small>"
                    f"{self._reminder_history_html(rem['id'])}</div></div>")
            list_html = "".join(_row(rem) for rem in rows)
        main = (
            "<div class=section><p class=muted>Every reminder, active or closed -- the board itself "
            "only shows active ones. Add or edit from the board; this is the full record, including "
            "every nag/progress/completion entry in its own history.</p></div>"
            f"<div class=section>{filter_html}<h2>reminders ({len(rows)})</h2>{list_html}</div>"
        )
        self._settings_response(sess, "reminders", main)

    # -- trackers page (2026-09-16, originally a Settings tab; moved to
    # its own first-class page 2026-09-17 -- see trackers_page() below).
    # No board card exists for a tracker (a time series doesn't fit the
    # board's card shape the way a task/note/reminder does -- trackers.py's
    # own module docstring calls this a judgment call, not something
    # settled), so unlike tasks/notes/reminders there's no /board/
    # tracker/<id> to link out to: the add and edit forms live directly
    # on this one page instead.
    def _tracker_value_str(self, type_row: dict, *, value_1, value_2, value_text) -> str:
        kind, meta = type_row["value_kind"], type_row["value_meta"]
        if kind == "boolean":
            return meta.get("true_label", "taken") if value_1 else meta.get("false_label", "not taken")
        if kind == "pair":
            labels = meta.get("labels", ["value 1", "value 2"])
            return f"{esc(labels[0])} {value_1}, {esc(labels[1])} {value_2}"
        if kind == "text":
            return esc(value_text or "")
        unit = f" {esc(type_row['unit'])}" if type_row.get("unit") else ""
        return f"{value_1}{unit}"

    def _tracker_type_lookup(self, sess: dict, type_id: int) -> dict | None:
        for t in trackers.list_types(sess)["types"]:
            if t["id"] == type_id:
                return t
        return None

    def _tracker_type_form(self, csrf: str, *, edit_row: dict | None = None, entry_count: int = 0) -> str:
        e = edit_row or {}
        meta = e.get("value_meta") or {}
        locked = edit_row is not None and entry_count > 0
        # Structural fields (kind/unit/labels) are LOCKED once a type has
        # any entries at all (2026-09-16, his own bug report -- "changing
        # the unit silently reinterprets them"): rendered `disabled` here
        # so the form itself shows the constraint rather than only
        # discovering it from a refusal after submitting. A `disabled`
        # field is never submitted at all, which is exactly what update_
        # type() needs to leave those fields untouched (see its own
        # _UNSET-vs-absent handling).
        dis = " disabled" if locked else ""
        kind_opts = "".join(
            f"<option value='{k}'{' selected' if e.get('value_kind', 'number') == k else ''}>{k}</option>"
            for k in trackers.VALUE_KINDS)
        labels = meta.get("labels") or [None, None]
        # Short lead-in stays inline; the reasoning + what-to-do-instead
        # -- the actual long part -- moves behind the info icon
        # (2026-09-18, design pass, his own instruction).
        lock_note = ""
        if locked:
            lock_tip = info_tip(
                "Changing them now would misrepresent what already got recorded under the old meaning. "
                "Rename is still free regardless of entry count; add a new tracker type instead if you "
                "need a different unit or kind.")
            noun = "entry" if entry_count == 1 else "entries"
            lock_note = f"<p class=muted>{entry_count} logged {noun} -- kind/unit/labels are locked. {lock_tip}</p>"
        action = f"/trackers/types/{e['id']}/update" if edit_row else "/trackers/types"
        hidden_id = f"<input type=hidden name=type_id value='{e['id']}'>" if edit_row else ""
        return (
            f"<form method=post action='{action}'>"
            f"<input type=hidden name=csrf value='{csrf}'>{hidden_id}{lock_note}"
            f"<div class=field><label>name<input type=text name=name required "
            f"value='{esc(e.get('name', ''))}' placeholder='e.g. water, weight, mood'></label></div>"
            f"<div class=field><label>kind<select name=value_kind{dis}>{kind_opts}</select></label> "
            f"<label>unit (optional)<input type=text name=unit{dis} value='{esc(e.get('unit') or '')}' "
            "placeholder='e.g. oz, lbs' style='width:6rem'></label></div>"
            + ("" if locked else "<p class=muted>Only used by the matching kind above -- fill in whichever apply:</p>") +
            f"<div class=field><label>pair label 1<input type=text name=component_1_label{dis} "
            f"value='{esc(labels[0] or '')}' placeholder='e.g. systolic' style='width:9rem'></label> "
            f"<label>pair label 2<input type=text name=component_2_label{dis} value='{esc(labels[1] or '')}' "
            "placeholder='e.g. diastolic' style='width:9rem'></label></div>"
            f"<div class=field><label>scale min<input type=number name=scale_min{dis} "
            f"value='{meta.get('min', '')}' style='width:5rem'></label> "
            f"<label>scale max<input type=number name=scale_max{dis} value='{meta.get('max', '')}' "
            "style='width:5rem'></label> "
            f"<label>min label<input type=text name=scale_min_label{dis} value='{esc(meta.get('min_label') or '')}' "
            "style='width:7rem'></label> "
            f"<label>max label<input type=text name=scale_max_label{dis} value='{esc(meta.get('max_label') or '')}' "
            "style='width:7rem'></label></div>"
            f"<div class=field><label>true label<input type=text name=true_label{dis} "
            f"value='{esc(meta.get('true_label') or '')}' placeholder='taken' style='width:7rem'></label> "
            f"<label>false label<input type=text name=false_label{dis} value='{esc(meta.get('false_label') or '')}' "
            "placeholder='not taken' style='width:7rem'></label></div>"
            f"<button class='btn btn-primary'>{'save changes' if edit_row else 'add tracker type'}</button></form>")

    def _tracker_log_form(self, csrf: str, type_row: dict, *, action: str, edit_entry: dict | None = None) -> str:
        e = edit_entry or {}
        kind, meta = type_row["value_kind"], type_row["value_meta"]
        if kind == "number":
            unit = f" {esc(type_row['unit'])}" if type_row.get("unit") else ""
            value_html = (f"<label>value{unit}<input type=number step=any name=value_1 required "
                         f"value='{e.get('value_1', '')}'></label>")
        elif kind == "pair":
            labels = meta.get("labels", ["value 1", "value 2"])
            value_html = (f"<label>{esc(labels[0])}<input type=number step=any name=value_1 required "
                         f"value='{e.get('value_1', '')}' style='width:6rem'></label> "
                         f"<label>{esc(labels[1])}<input type=number step=any name=value_2 required "
                         f"value='{e.get('value_2', '')}' style='width:6rem'></label>")
        elif kind == "boolean":
            checked = " checked" if e.get("value_1") else ""
            value_html = (f"<label><input type=checkbox name=value_1_bool{checked}> "
                         f"{esc(meta.get('true_label', 'taken'))}</label>")
        elif kind == "scale":
            lo, hi = meta.get("min", 0), meta.get("max", 10)
            value_html = (f"<label>value ({lo}-{hi})<input type=number name=value_1 min={lo} max={hi} required "
                         f"value='{e.get('value_1', '')}' style='width:5rem'></label>")
        else:
            value_html = (f"<label>value<input type=text name=value_text required "
                         f"value='{esc(e.get('value_text') or '')}'></label>")
        ts_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(e["ts"])) if e.get("ts") else ""
        hidden_type_id = f"<input type=hidden name=type_id value='{type_row['id']}'>" if edit_entry else ""
        return (
            f"<form method=post action='{action}'>"
            f"<input type=hidden name=csrf value='{csrf}'>{hidden_type_id}"
            f"<div class=field>{value_html}</div>"
            f"<div class=field><label>note (optional)<input type=text name=note "
            f"value='{esc(e.get('note') or '')}'></label></div>"
            f"<div class=field><label>when<input type=text name=ts value='{esc(ts_str)}' "
            "placeholder=\"e.g. now, yesterday, 2026-09-16 08:00\"></label></div>"
            f"<button class='btn btn-primary'>{'save changes' if edit_entry else 'log entry'}</button></form>")

    def trackers_page(self, sess: dict, err: str = "", info: str = ""):
        """First-class page, alongside /inventory and /meals (2026-09-17,
        moved out of Settings on his own instruction) -- same shell, same
        nav placement, same plain page_app() (no split_main, no board
        button) those two already use correctly. Everything that was in
        the settings tab stays: type management (add/edit/enable/
        disable/delete) and the entry list (log/edit), just reached at
        /trackers instead of /settings?tab=trackers."""
        csrf = esc(sess["csrf"])
        e = f"<p class=err>{esc(err)}</p>" if err else ""
        i = f"<p class=info>{esc(info)}</p>" if info else ""
        types = trackers.list_types(sess)["types"]
        def _type_section(t: dict) -> str:
            dim = "" if t["enabled"] else " style='opacity:.55'"
            unit_html = f" ({esc(t['unit'])})" if t.get("unit") else ""
            badge = (f"<span class='hist-badge{'' if t['enabled'] else ' hist-kind-off'}'>"
                    f"{'enabled' if t['enabled'] else 'disabled'}</span>")
            toggle_form = (
                f"<form method=post action='/trackers/types/{t['id']}/"
                f"{'disable' if t['enabled'] else 'enable'}' style='display:inline'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                f"<button class=btn>{'disable' if t['enabled'] else 're-enable'}</button></form>")
            # Delete is always offered, never pre-hidden based on a
            # windowed entry count -- delete_type() itself is the real
            # gate (refuses with a clear reason the moment any entry
            # exists, at any age), so attempting it is the only way to
            # get an authoritative answer rather than guessing here.
            delete_form = (
                f"<form method=post action='/trackers/types/{t['id']}/delete' style='display:inline'>"
                f"<input type=hidden name=csrf value='{csrf}'><button class='btn btn-danger'>delete</button></form>")
            entry_count = trackers.entry_count_for_type(sess, t["id"])
            edit_html = (f"<details class=sched-history><summary>edit</summary>"
                        f"{self._tracker_type_form(csrf, edit_row=t, entry_count=entry_count)}</details>")
            hist = trackers.history_for_type(sess, t["id"], since="365 days ago")
            entries = hist.get("entries", [])
            def _entry_row(en: dict) -> str:
                when = time.strftime("%b %d, %H:%M", time.localtime(en["ts"]))
                val = self._tracker_value_str(t, value_1=en["value_1"], value_2=en["value_2"],
                                              value_text=en["value_text"])
                note_html = f" &middot; {esc(en['note'])}" if en.get("note") else ""
                edit_action = f"/trackers/entries/{en['id']}/update"
                return (
                    "<details class=sched-history><summary>"
                    f"<small>{esc(when)} &middot; {val}{note_html}</small></summary>"
                    f"{self._tracker_log_form(csrf, t, action=edit_action, edit_entry=en)}</details>")
            entries_html = "".join(_entry_row(en) for en in reversed(entries)) or "<p class=muted>no entries yet.</p>"
            log_form = self._tracker_log_form(csrf, t, action=f"/trackers/types/{t['id']}/log")
            trunc_note = " (showing the most recent 500)" if hist.get("truncated") else ""
            return (
                f"<div class=section{dim}><h2>{esc(t['name'])}{unit_html} {badge}</h2>"
                f"<p class=muted>{esc(t['value_kind'])}</p>"
                f"<div class=list-row><div class=list-actions>{toggle_form}{delete_form}</div></div>"
                f"{edit_html}"
                f"<h3>log a new entry</h3>{log_form}"
                f"<h3>entries, last year ({len(entries)}){trunc_note}</h3>{entries_html}</div>")
        types_html = "".join(_type_section(t) for t in types) or "<p class=muted>no tracker types yet.</p>"
        main = (
            f"{e}{i}<div class=section><p class=muted>Every tracker type she's defined, and the entries "
            "logged against each -- add a new type, log entries yourself, or edit/disable existing ones.</p></div>"
            f"{types_html}"
            f"<div class=section><h2>new tracker type</h2>{self._tracker_type_form(csrf)}</div>")
        # Plain page_app(), no split_main -- the same shell inventory_page/
        # meals_page already use correctly (verified against a real
        # scroll-trap incident: split_main is for a page whose CONTENT
        # provides its own internal scrolling child, which this doesn't
        # have any more than inventory/meals do). .shell-main's own
        # overflow-y:auto is the only scroll container here.
        self.send(200, page_app("trackers", self._app_header(sess, self._hdr_back("Trackers")), main))

    @staticmethod
    def _tracker_type_form_kwargs(form: dict) -> dict:
        def _int(key):
            v = form.get(key)
            return int(v) if v not in (None, "") else None
        return {
            "name": form.get("name", ""), "value_kind": form.get("value_kind", "number"),
            "unit": form.get("unit") or None,
            "component_1_label": form.get("component_1_label") or None,
            "component_2_label": form.get("component_2_label") or None,
            "scale_min": _int("scale_min"), "scale_max": _int("scale_max"),
            "scale_min_label": form.get("scale_min_label") or None,
            "scale_max_label": form.get("scale_max_label") or None,
            "true_label": form.get("true_label") or None, "false_label": form.get("false_label") or None,
        }

    @staticmethod
    def _tracker_type_update_form_kwargs(form: dict) -> dict:
        """Unlike the add form, a `disabled` HTML field (the structural
        ones, once entries lock them -- see _tracker_type_form's own
        `dis`) is never submitted at all, so its key is simply ABSENT
        from `form` -- checked with `in`, not `.get()`, so an absent
        field maps to trackers.update_type()'s own _UNSET default (leave
        alone) rather than being read as an explicit None (clear it)."""
        kw = {}
        if "name" in form:
            kw["name"] = form.get("name", "")
        if "value_kind" in form:
            kw["value_kind"] = form.get("value_kind")
        if "unit" in form:
            kw["unit"] = form.get("unit") or None
        for k in ("component_1_label", "component_2_label", "scale_min_label", "scale_max_label",
                 "true_label", "false_label"):
            if k in form:
                kw[k] = form.get(k) or None
        for k in ("scale_min", "scale_max"):
            if k in form:
                v = form.get(k)
                kw[k] = int(v) if v not in (None, "") else None
        return kw

    @staticmethod
    def _tracker_entry_form_kwargs(form: dict, type_row: dict) -> dict:
        kind = type_row["value_kind"]
        def _f(key):
            v = form.get(key)
            return float(v) if v not in (None, "") else None
        kw = {"note": form.get("note") or None, "ts": form.get("ts") or None}
        if kind in ("number", "scale"):
            kw["value_1"] = _f("value_1")
        elif kind == "pair":
            kw["value_1"], kw["value_2"] = _f("value_1"), _f("value_2")
        elif kind == "boolean":
            kw["value_1"] = 1 if form.get("value_1_bool") else 0
        else:
            kw["value_text"] = form.get("value_text") or None
        return kw

    # NOTE: these four call trackers.add_type()/disable_type()/log_entry()/
    # update_entry() DIRECTLY, never the _tracker_*_impl tool wrappers --
    # same reasoning as board_task_create_post above: a plain browser POST
    # from his own settings UI has no _peer_act/_peer_context to read, so
    # actor="user"/created_by_type="user" is passed explicitly here.
    def trackers_type_add_post(self, sess: dict, form: dict):
        kw = self._tracker_type_form_kwargs(form)
        r = trackers.add_type(sess, created_by_type="user", **kw)
        if r.get("error"):
            return self.trackers_page(sess, err=r["error"])
        return self.trackers_page(sess, info="added")

    def trackers_type_disable_post(self, sess: dict, type_id: str, form: dict):
        try:
            r = trackers.disable_type(sess, int(type_id), actor="user")
        except ValueError:
            r = {"error": "bad id"}
        if r.get("error"):
            return self.trackers_page(sess, err=r["error"])
        return self.trackers_page(sess, info="disabled")

    def trackers_type_enable_post(self, sess: dict, type_id: str, form: dict):
        try:
            r = trackers.enable_type(sess, int(type_id), actor="user")
        except ValueError:
            r = {"error": "bad id"}
        if r.get("error"):
            return self.trackers_page(sess, err=r["error"])
        return self.trackers_page(sess, info="re-enabled")

    def trackers_type_delete_post(self, sess: dict, type_id: str, form: dict):
        try:
            r = trackers.delete_type(sess, int(type_id), actor="user")
        except ValueError:
            r = {"error": "bad id"}
        if r.get("error"):
            return self.trackers_page(sess, err=r["error"])
        return self.trackers_page(sess, info="deleted")

    def trackers_type_update_post(self, sess: dict, type_id: str, form: dict):
        try:
            tid = int(type_id)
        except ValueError:
            return self.trackers_page(sess, err="bad id")
        kw = self._tracker_type_update_form_kwargs(form)
        r = trackers.update_type(sess, tid, actor="user", **kw)
        if r.get("error"):
            return self.trackers_page(sess, err=r["error"])
        return self.trackers_page(sess, info="saved")

    def trackers_log_post(self, sess: dict, type_id: str, form: dict):
        try:
            tid = int(type_id)
        except ValueError:
            return self.trackers_page(sess, err="bad id")
        type_row = self._tracker_type_lookup(sess, tid)
        if type_row is None:
            return self.trackers_page(sess, err="no such tracker type")
        kw = self._tracker_entry_form_kwargs(form, type_row)
        r = trackers.log_entry(sess, type_id=tid, created_by_type="user", **kw)
        if r.get("error"):
            return self.trackers_page(sess, err=r["error"])
        return self.trackers_page(sess, info="logged")

    def trackers_entry_update_post(self, sess: dict, entry_id: str, form: dict):
        try:
            eid = int(entry_id)
        except ValueError:
            return self.trackers_page(sess, err="bad id")
        try:
            tid = int(form.get("type_id", ""))
        except ValueError:
            return self.trackers_page(sess, err="bad type id")
        type_row = self._tracker_type_lookup(sess, tid)
        if type_row is None:
            return self.trackers_page(sess, err="no such tracker type")
        kw = self._tracker_entry_form_kwargs(form, type_row)
        r = trackers.update_entry(sess, eid, actor="user", **kw)
        if r.get("error"):
            return self.trackers_page(sess, err=r["error"])
        return self.trackers_page(sess, info="updated")

    # -- categories (2026-09-16, see catalog.py) -- his own UI to add/
    # rename/disable the sets tasks/notes/reminders file under, "same as
    # everything else" (his own instruction). One page, all domains --
    # matches the tool surface's own single category_* family rather
    # than a separate admin page per item type.
    def categories_admin_form(self, sess: dict, err: str = "", info: str = ""):
        csrf = esc(sess["csrf"])
        e = f"<p class=err>{esc(err)}</p>" if err else ""
        i = f"<p class=info>{esc(info)}</p>" if info else ""
        def _domain_section(domain: str) -> str:
            cats = catalog.list_categories(sess, domain)["categories"]
            def _row(c: dict) -> str:
                dim = "" if c["enabled"] else " style='opacity:.55'"
                return (
                    f"<div class=list-row{dim}><div class=list-meta>"
                    f"<b>{esc(c['name'])}</b> "
                    f"<span class='hist-badge{'' if c['enabled'] else ' hist-kind-off'}'>"
                    f"{'enabled' if c['enabled'] else 'disabled'}</span></div>"
                    "<div class=list-actions>"
                    f"<form method=post action='/settings/categories/{c['id']}/rename' style='display:inline'>"
                    f"<input type=hidden name=csrf value='{csrf}'>"
                    f"<input type=text name=new_name value='{esc(c['name'])}' style='width:8rem'>"
                    "<button class=btn>rename</button></form>"
                    + (f"<form method=post action='/settings/categories/{c['id']}/disable' style='display:inline'>"
                       f"<input type=hidden name=csrf value='{csrf}'>"
                       "<button class=btn>disable</button></form>" if c["enabled"] else "")
                    + "</div></div>")
            rows_html = "".join(_row(c) for c in cats) or "<p class=muted>none yet.</p>"
            add_form = (
                f"<form method=post action='/settings/categories' style='margin-top:.6rem'>"
                f"<input type=hidden name=csrf value='{csrf}'><input type=hidden name=domain value='{domain}'>"
                "<input type=text name=name placeholder='new category name' required>"
                "<button class='btn btn-primary'>add</button></form>")
            return f"<div class=section><h2>{esc(domain)}</h2>{rows_html}{add_form}</div>"
        main = (
            f"{e}{i}<div class=section><p class=muted>Not a fixed list -- add, rename, or disable "
            "the categories tasks/notes/reminders file under. Disabling never deletes anything already "
            "filed under a category; it only stops it being offered for new items.</p></div>"
            + "".join(_domain_section(d) for d in catalog.DOMAINS)
        )
        self._settings_response(sess, "categories", main)

    def categories_add_post(self, sess: dict, form: dict):
        r = catalog.add(sess, domain=form.get("domain", ""), name=form.get("name", ""), created_by_type="user")
        if r.get("error"):
            return self.categories_admin_form(sess, err=r["error"])
        return self.categories_admin_form(sess, info="added")

    def categories_rename_post(self, sess: dict, category_id: str, form: dict):
        try:
            r = catalog.rename(sess, int(category_id), new_name=form.get("new_name", ""), actor="user")
        except ValueError:
            r = {"error": "bad id"}
        if r.get("error"):
            return self.categories_admin_form(sess, err=r["error"])
        return self.categories_admin_form(sess, info="renamed")

    def categories_disable_post(self, sess: dict, category_id: str, form: dict):
        try:
            r = catalog.set_enabled(sess, int(category_id), False, actor="user")
        except ValueError:
            r = {"error": "bad id"}
        if r.get("error"):
            return self.categories_admin_form(sess, err=r["error"])
        return self.categories_admin_form(sess, info="disabled")

    # -- admin: context-tuning pane (2026-09-14) -- every value that
    # affects prompt composition, workspace-scoped like everything else
    # on this page. (key, min, max, label, plain-language blurb).
    _CONTEXT_FIELDS = [
        ("context_window_msgs", 1, 200,
         "raw messages, at most",
         "How many of the most recent messages ride along verbatim."),
        ("compaction_enabled", None, None,
         "summarize older conversation",
         "Whether conversation that's aged out of the raw window above gets folded in as short "
         "generated summaries at all. Off: older conversation is retrievable only if she reaches "
         "for search_history — nothing about it rides along automatically."),
        ("compaction_max_segments", 0, 30,
         "summarized sessions, at most",
         "How many past summarized sessions ride along, most recent first."),
        ("compaction_budget_tokens", 0, 3000,
         "summary token budget",
         "A token ceiling on the total text of those summaries. Whichever of this or the count "
         "above hits first wins."),
        ("compaction_session_gap_hours", 0.25, 24,
         "hours of quiet that starts a new session",
         "How long a gap between messages has to be before it counts as a new session boundary "
         "for summarizing. Only affects sessions summarized from now on — an already-summarized "
         "one keeps the span it was generated with."),
        ("peer_recent_cap", 1, 20,
         "recent peer messages, at most",
         "How many already-handled messages from a peer show up as background awareness."),
        ("peer_recent_window_hours", 0, 72,
         "hours back",
         "...and only from within this many hours. 0 turns this block off entirely."),
        ("memory_max_tokens", 50, 3000,
         "ordinary memory token budget",
         "Room for everyday (unpinned) memory — the oldest unpinned facts drop first once this "
         "fills up."),
        ("memory_pinned_max_tokens", 50, 2000,
         "pinned memory token budget",
         "A separate ceiling just for pinned memory, surfaced proximately via precheck rather "
         "than here in the standing prompt. Pins never compete with the ordinary budget above "
         "for space — this exists only so a pin-heavy stretch can't consume the whole prompt."),
    ]

    def context_admin_form(self, sess: dict, err: str = "", info: str = ""):
        if sess["role"] != "admin":
            return self.forbidden()
        e = f"<p class=err>{esc(err)}</p>" if err else ""
        i = f"<p class=info>{esc(info)}</p>" if info else ""
        csrf = esc(sess["csrf"])
        wsid = sess["workspace_id"]
        rows = []
        for key, lo, hi, label, blurb in self._CONTEXT_FIELDS:
            default, typ = config.spec()[key][0], config.spec()[key][1]
            val = config.get("workspace", wsid, key)
            if typ is bool:
                inp = (f"<select name='{key}'><option value=1 {'selected' if val else ''}>on</option>"
                      f"<option value=0 {'selected' if not val else ''}>off</option></select>")
            else:
                step = "" if typ is int else " step='0.25'"
                inp = f"<input type=number name='{key}' value='{val}' min='{lo}' max='{hi}'{step}>"
            # A raw <table> row per field (2026-09-19 design pass, fixed
            # alongside the nav rebuild -- same "raw unstyled markup"
            # class of problem): the label+long-blurb+input+default shape
            # didn't fit two table cells cleanly and never stacked on a
            # phone. Real .field block instead, blurb behind info_tip
            # like every other long explanation on this page now does
            # (2026-09-18's own convention, just missed here) -- default
            # value as a short trailing hint, not a third column.
            rows.append(
                f"<div class=field><label>{esc(label)} {info_tip(blurb)}</label>{inp}"
                f"<span class=muted style='font-size:.78rem'>default: {default}</span></div>")
        user = accounts.get_user(sess["user_id"])
        comp = context.composition_breakdown(sess, sess["user_id"], user["display_name"])
        comp_rows = "".join(
            f"<tr><td>{esc(r['label'])}</td><td class=muted>{r['tokens']} tok</td>"
            f"<td class=muted>{r['pct']}%</td>"
            # var(--line)/var(--sent) (2026-09-18, design pass): neither
            # was ever a real token -- this bar's track/fill never
            # actually rendered a background at all until now, found
            # sweeping the stylesheet for undefined custom properties.
            f"<td style='width:40%'><div style='background:var(--surface-3);border-radius:.3rem;"
            f"overflow:hidden;height:.6rem'><div style='background:var(--accent);height:100%;"
            f"width:{min(r['pct'], 100)}%'></div></div></td></tr>"
            for r in comp["sections"])
        main = (
            f"{e}{i}"
            "<div class=section><h2>what this actually costs</h2>"
            "<p class=muted>The real assembled prompt for your own next reply, measured just "
            "now — not estimated. Reload this tab after changing a value below to see the effect. "
            "The core trade-off running through all of it: more raw history or more summarized "
            "history means less room for persona and memory, which is exactly the influence "
            "you're trying to protect — there's no setting that gives you more of everything at "
            "once.</p>"
            f"<div class=table-scroll><table>{comp_rows}</table></div>"
            f"<p class=muted style='margin-top:.5rem'><b>{comp['total_tokens']} tokens total</b></p>"
            "<p class=muted>Measured against your own account — another household member's "
            "actual prompt will differ by their own memory/conversation, not by these settings, "
            "which are shared workspace-wide.</p></div>"
            "<div class=section><h2>tune the composition</h2>"
            "<p class=muted>Every value that affects what she actually sees each turn, shared "
            "across the whole household. Nothing here needs a restart — a save applies on the "
            "very next reply, to anyone.</p>"
            "<form method=post action='/admin/contexttuning'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            f"{''.join(rows)}"
            "<p><button class='btn btn-primary'>save</button></p></form></div>"
            + tuning_admin.render_layers(wsid, sess["csrf"])
        )
        self._settings_response(sess, "contexttuning", main)

    def context_admin_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        values = {key: form[key] for key, *_rest in self._CONTEXT_FIELDS if isinstance(form.get(key), str)}
        ok, msg = tuning_admin.act(sess["workspace_id"], sess["user_id"], "save", {"values": values})
        return self.context_admin_form(sess, err="" if ok else msg, info=msg if ok else "")

    # -- admin: the persona editor (persona_admin.py holds the page and the actions; these
    # are the ONLY routes to it, both admin-gated, POSTs behind the global CSRF check). --
    def persona_admin_form(self, sess: dict, err: str = "", info: str = "", draft: str | None = None):
        if sess["role"] != "admin":
            return self.forbidden()
        self._settings_response(sess, "persona", persona_admin.render(sess["csrf"], err=err, info=info, draft=draft))

    def persona_admin_post(self, sess: dict, form: dict, action: str):
        if sess["role"] != "admin":
            return self.forbidden()
        ok, msg = persona_admin.act(action, form)
        draft = form.get("text") if (action == "save" and not ok and isinstance(form.get("text"), str)) else None
        return self.persona_admin_form(sess, err="" if ok else msg, info=msg if ok else "", draft=draft)

    def persona_preview_get(self, sess: dict, q: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        body = persona_admin.render_preview((q.get("target") or [""])[0], (q.get("name") or [""])[0], sess["csrf"])
        if body is None:
            return self.not_found()
        self._settings_response(sess, "persona", body)

    def context_preview_get(self, sess: dict, q: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        body = tuning_admin.render_preview(sess["workspace_id"], (q.get("target") or [""])[0], (q.get("id") or [""])[0], sess["csrf"])
        if body is None:
            return self.not_found()
        self._settings_response(sess, "contexttuning", body)

    def context_admin_action(self, sess: dict, form: dict, action: str):
        """Baseline save, and the three ways back (baseline, shipped defaults, history). Admin only; the values are the tuning keys and nothing else."""
        if sess["role"] != "admin":
            return self.forbidden()
        ok, msg = tuning_admin.act(sess["workspace_id"], sess["user_id"], action, {"id": form.get("id")})
        return self.context_admin_form(sess, err="" if ok else msg, info=msg if ok else "")

    # -- admin: image generation (generate_image_selfie / imagine_image).
    # One shared household budget for both tools (operator's own decision,
    # 2026-09-14) -- this tab is where that's surfaced and where the cap
    # is set, same shape as context_admin_form's own settings-table +
    # save pattern.
    def media_admin_form(self, sess: dict, err: str = "", info: str = ""):
        if sess["role"] != "admin":
            return self.forbidden()
        e = f"<p class=err>{esc(err)}</p>" if err else ""
        i = f"<p class=info>{esc(info)}</p>" if info else ""
        csrf = esc(sess["csrf"])
        wsid = sess["workspace_id"]
        status = medialog.spend_status(wsid, sess["user_id"])
        has_ref = imagegen.reference_bytes() is not None
        ref_chip = ("<span class='chip active'>reference photo present</span>" if has_ref
                   else "<span class=chip>no reference photo found on disk -- generate_image_selfie "
                        "will fail until static/avatars/source/neutral.png exists</span>")
        enabled = config.get("workspace", wsid, "image_gen_enabled")
        precheck = config.get("workspace", wsid, "image_content_precheck_enabled")
        model = config.get("workspace", wsid, "image_model")
        cap = config.get("workspace", wsid, "image_daily_cap_usd")
        per_req = config.get("workspace", wsid, "image_cost_per_request_usd")
        cap_tip = info_tip("Shared by both image tools, whole household.")
        per_req_tip = info_tip("Used when the provider doesn't return a real figure for a given request.")
        settings_form = (
            "<div class=section><h2>settings</h2>"
            f"<p class=muted>{ref_chip}</p>"
            "<form method=post action='/admin/media'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            "<div class=field><label>image generation enabled</label>"
            f"<select name=image_gen_enabled><option value=1{' selected' if enabled else ''}>on</option>"
            f"<option value=0{' selected' if not enabled else ''}>off</option></select></div>"
            "<div class=field><label for=image_content_precheck_enabled>image content pre-check</label>"
            "<select id=image_content_precheck_enabled name=image_content_precheck_enabled>"
            f"<option value=1{' selected' if precheck else ''}>on</option>"
            f"<option value=0{' selected' if not precheck else ''}>off</option></select>"
            "<p class=muted>Checks prompts for flagged words before sending them to the image service. "
            "Turn off to skip this local check for both image tools. "
            "Prompt content rules and the provider's own checks still apply.</p></div>"
            "<div class=field><label>model (OpenRouter)</label>"
            f"<input type=text name=image_model value='{esc(model)}'></div>"
            f"<div class=field><label>daily budget (USD) {cap_tip}</label>"
            f"<input type=number name=image_daily_cap_usd value='{cap}' min=0 step=0.10></div>"
            f"<div class=field><label>per-image cost estimate (USD) {per_req_tip}</label>"
            f"<input type=number name=image_cost_per_request_usd value='{per_req}' min=0 step=0.005></div>"
            "<button class='btn btn-primary btn-block'>save</button></form></div>")
        spend_block = (
            "<div class=section><h2>spend</h2>"
            f"<p>today: <b>${status['spent_today_usd']:.2f}</b> / ${status['daily_cap_usd']:.2f} "
            f"&nbsp;&middot;&nbsp; remaining today: <b>${status['remaining_today_usd']:.2f}</b> "
            f"&nbsp;&middot;&nbsp; lifetime: ${status['spent_total_usd']:.2f}</p></div>")
        rows = medialog.recent_log(wsid)
        if rows:
            log_rows = "".join(
                f"<tr><td class=muted style='white-space:nowrap'>{time.strftime('%m-%d %H:%M', time.localtime(r['ts']))}</td>"
                f"<td>{esc(r['purpose'])}</td>"
                f"<td>{'<span class=ok>ok</span>' if r['ok'] else '<span class=err>failed</span>'}</td>"
                f"<td class=muted>{esc((r['prompt'] or '')[:80])}</td>"
                f"<td class=muted>{'$' + format(r['cost_usd'], '.4f') if r['cost_usd'] is not None else ''}</td>"
                f"<td class=muted>{esc(r['error'] or '')[:120]}</td></tr>"
                for r in rows)
            log_block = (f"<div class=section><h2>recent activity</h2>"
                        f"<div class=table-scroll><table>{log_rows}</table></div></div>")
        else:
            log_block = "<div class=section><h2>recent activity</h2><p class=muted>nothing generated yet</p></div>"
        self._settings_response(sess, "media", f"{e}{i}{settings_form}{spend_block}{log_block}")

    def media_admin_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        wsid = sess["workspace_id"]
        errors = []
        for key in ("image_gen_enabled", "image_content_precheck_enabled", "image_model",
                    "image_daily_cap_usd", "image_cost_per_request_usd"):
            if key not in form or not isinstance(form[key], str):
                continue
            try:
                config.set("workspace", wsid, key, form[key])
            except (KeyError, ValueError) as exc:
                errors.append(str(exc))
        return self.media_admin_form(sess, err="; ".join(errors) if errors else "",
                                     info="saved" if not errors else "")

    # -- admin: backups (2026-09-18) -- see backup.py's own module
    # docstring for the full design (what's included/excluded, encryption,
    # retention, why remote deletion is a manual button here rather than
    # automatic).
    _BACKUP_DEST_LABELS = {"": "local only (no cloud upload)", "google_drive": "Google Drive",
                          "onedrive_work": "OneDrive (work)", "onedrive_personal": "OneDrive (personal)",
                          "sharepoint": "SharePoint"}

    def backups_admin_form(self, sess: dict, err: str = "", info: str = "", candidates: list | None = None):
        if sess["role"] != "admin":
            return self.forbidden()
        import backup
        wsid = sess["workspace_id"]
        csrf = esc(sess["csrf"])
        e = f"<p class=err>{esc(err)}</p>" if err else ""
        i = f"<p class=info>{esc(info)}</p>" if info else ""
        enabled = config.get("workspace", wsid, "backup_enabled")
        hour = config.get("workspace", wsid, "backup_hour")
        retention = config.get("workspace", wsid, "backup_retention_days")
        provider = config.get("workspace", wsid, "backup_remote_provider")
        site_id = config.get("workspace", wsid, "backup_sharepoint_site_id")
        key_status = backup.key_status()
        if not key_status["configured"]:
            key_chip = ("<span class=chip>NORI_BACKUP_KEY not set in .env -- backups will fail until "
                       "it is; see docs/backups.md</span>")
        elif not key_status["ok"]:
            key_chip = f"<span class='chip err'>NORI_BACKUP_KEY is {esc(key_status['reason'])}</span>"
        else:
            mode_txt = "a passphrase" if key_status["mode"] == "passphrase" else "a generated Fernet key"
            key_chip = f"<span class='chip active'>NORI_BACKUP_KEY set ({mode_txt})</span>"
        dest_opts = "".join(
            f"<option value={p or 'none'}{' selected' if p == provider else ''}>{esc(label)}"
            f"{'' if p == '' or oauth.is_configured(p) else ' (not connected yet)'}</option>"
            for p, label in self._BACKUP_DEST_LABELS.items())
        settings_form = (
            "<div class=section><h2>settings</h2>"
            f"<p class=muted>{key_chip}</p>"
            "<form method=post action='/admin/backups'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            "<div class=field><label>daily backups enabled</label>"
            f"<select name=backup_enabled><option value=1{' selected' if enabled else ''}>on</option>"
            f"<option value=0{' selected' if not enabled else ''}>off</option></select></div>"
            "<div class=field><label>runs at (local hour, 0-23)</label>"
            f"<input type=number name=backup_hour value='{hour}' min=0 max=23></div>"
            "<div class=field><label>keep local backups for (days)</label>"
            f"<input type=number name=backup_retention_days value='{retention}' min=1></div>"
            "<div class=field><label>upload destination</label>"
            f"<select name=backup_remote_provider>{dest_opts}</select></div>"
            "<div class=field><label>SharePoint site id</label>"
            f"<input type=text name=backup_sharepoint_site_id value='{esc(site_id)}' "
            "placeholder='only used if destination is SharePoint'></div>"
            "<button class='btn btn-primary btn-block'>save</button></form></div>")
        manual = (
            "<div class=section><h2>manual</h2>"
            "<form method=post action='/admin/backups/run'>"
            f"<input type=hidden name=csrf value='{csrf}'><button class=btn>back up now</button></form>"
            "<p class=muted style='margin-top:.6rem'>Creates a backup immediately, uploads it if a "
            "destination is configured above, then prunes local backups past retention -- the exact "
            "same thing the daily schedule does.</p></div>")
        rows = backup.history()
        if rows:
            def _hist_row(r):
                size_txt = f"{r['size_bytes']:,} bytes" if r.get("size_bytes") else ""
                status_txt = ("<span class=ok>ok</span>" if r["status"] == "ok"
                             else "<span class=err>" + esc(r["status"]) + "</span>")
                return (f"<tr><td class=muted style='white-space:nowrap'>"
                       f"{time.strftime('%m-%d %H:%M', time.localtime(r['ts']))}</td>"
                       f"<td>{esc(r.get('app', ''))}</td><td>{esc(r.get('action', ''))}</td>"
                       f"<td>{status_txt}</td><td class=muted>{esc(r.get('name', ''))}</td>"
                       f"<td class=muted>{size_txt}</td>"
                       f"<td class=muted>{esc(r.get('error') or '')[:200]}</td></tr>")
            hist_rows = "".join(_hist_row(r) for r in rows)
            hist_block = (f"<div class=section><h2>recent activity</h2>"
                         f"<div class=table-scroll><table>{hist_rows}</table></div></div>")
        else:
            hist_block = "<div class=section><h2>recent activity</h2><p class=muted>no backups run yet</p></div>"
        remote_block = ""
        if provider:
            if candidates is not None:
                if candidates:
                    total = sum(c.get("size_bytes", 0) for c in candidates) if any("size_bytes" in c for c in candidates) else None
                    cand_rows = "".join(f"<li>{esc(c['name'])}</li>" for c in candidates)
                    ids = ",".join(c["id"] for c in candidates)
                    remote_block = (
                        "<div class=section><h2>clean up remote backups</h2>"
                        f"<p>{len(candidates)} backup(s) on {esc(self._BACKUP_DEST_LABELS.get(provider, provider))} "
                        f"older than {retention} days:</p><ul>{cand_rows}</ul>"
                        "<form method=post action='/admin/backups/prune-remote'>"
                        f"<input type=hidden name=csrf value='{csrf}'>"
                        f"<input type=hidden name=ids value='{esc(ids)}'>"
                        "<button class=btn>delete these now</button></form></div>")
                else:
                    remote_block = ("<div class=section><h2>clean up remote backups</h2>"
                                    "<p class=muted>nothing older than retention out there.</p></div>")
            else:
                remote_block = (
                    "<div class=section><h2>clean up remote backups</h2>"
                    "<p class=muted>remote backups aren't pruned automatically -- deliberately, see "
                    "backup.py's own docstring. Check what's old enough to remove first:</p>"
                    "<form method=post action='/admin/backups/check-remote'>"
                    f"<input type=hidden name=csrf value='{csrf}'>"
                    "<button class=btn>check for old remote backups</button></form></div>")
        self._settings_response(sess, "backups", f"{e}{i}{settings_form}{manual}{remote_block}{hist_block}")

    def backups_admin_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        wsid = sess["workspace_id"]
        errors = []
        for key in ("backup_enabled", "backup_hour", "backup_retention_days", "backup_sharepoint_site_id"):
            if key not in form or not isinstance(form[key], str):
                continue
            try:
                config.set("workspace", wsid, key, form[key])
            except (KeyError, ValueError) as exc:
                errors.append(str(exc))
        dest = form.get("backup_remote_provider")
        if isinstance(dest, str):
            try:
                config.set("workspace", wsid, "backup_remote_provider", "" if dest == "none" else dest)
            except (KeyError, ValueError) as exc:
                errors.append(str(exc))
        return self.backups_admin_form(sess, err="; ".join(errors) if errors else "",
                                       info="saved" if not errors else "")

    def backups_run_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        import backup
        wsid = sess["workspace_id"]
        result = backup.run_daily(
            provider=config.get("workspace", wsid, "backup_remote_provider") or None,
            site_id=config.get("workspace", wsid, "backup_sharepoint_site_id") or None,
            retention_days=config.get("workspace", wsid, "backup_retention_days"))
        if result["nori_backup"]["status"] != "ok":
            return self.backups_admin_form(sess, err=result["nori_backup"].get("error", "backup failed"))
        return self.backups_admin_form(sess, info="backup complete -- see recent activity below")

    # ── integration health (2026-09-19) -- one page, every outward-facing
    # integration, same PACI §4.1 pattern extended beyond peers. See
    # integration_health.py's own module docstring for the state
    # vocabulary and the Tavily cost tradeoff this page's own copy below
    # refers to. ──────────────────────────────────────────────────────────
    _INTEGRATION_HEALTH_CHIP = {
        "healthy": ("ok", "healthy"), "not_configured": ("muted", "not configured"),
        "unreachable": ("err", "unreachable"), "auth_expired": ("err", "credentials expired"),
        "scope_missing": ("err", "missing permission"), "api_disabled": ("err", "API disabled"),
        "rate_limited": ("err", "rate limited"), "error": ("err", "error"),
        "unknown": ("muted", "not checked yet"),
    }

    def _integration_health_chip(self, entry: dict) -> str:
        css, label = self._INTEGRATION_HEALTH_CHIP.get(entry["status"], ("muted", entry["status"]))
        title = f" title='{esc(entry['detail'])[:200]}'" if entry.get("detail") else ""
        return f"<span class={css}{title} style='font-size:.7rem;font-weight:normal'>● {label}</span>"

    def integration_health_admin_form(self, sess: dict, err: str = "", info: str = ""):
        if sess["role"] != "admin":
            return self.forbidden()
        csrf = esc(sess["csrf"])
        e = f"<p class=err>{esc(err)}</p>" if err else ""
        i = f"<p class=info>{esc(info)}</p>" if info else ""
        entries = integration_health.get_all()

        def _row(entry):
            checked = (time.strftime("%m-%d %H:%M", time.localtime(entry["checked_ts"]))
                      if entry.get("checked_ts") else "never")
            explain = esc(entry.get("explain") or "")[:300]
            detail = esc(entry.get("detail") or "")[:200]
            note = (f"{explain}<br><span style='opacity:.65'>{detail}</span>" if explain and detail != explain
                   else (explain or detail))
            return (f"<tr><td>{esc(entry['label'])}</td><td>{self._integration_health_chip(entry)}</td>"
                   f"<td class=muted style='white-space:nowrap'>{checked}</td>"
                   f"<td class=muted style='font-size:.8rem'>{note}</td></tr>")

        order = ["home_assistant", "tavily"] + [k for k, _, _ in integration_health._OAUTH_CHECKS]
        rows = "".join(_row(entries[k]) for k in order if k in entries)
        mcp_rows = "".join(_row(v) for k, v in entries.items() if k.startswith("mcp:"))
        if not mcp_rows:
            mcp_rows = "<tr><td colspan=4 class=muted>no MCP servers connected</td></tr>"
        table = (
            "<div class=section><h2>integrations</h2>"
            "<p class=muted style='margin:0 0 .6rem'>Cheap, real connectivity/auth checks -- no model "
            "call, ever -- same pattern as the PACI peer health check on the Peer connections tab. "
            "Auto-checked on the interval below; web search is config-only on that automatic cycle "
            "(key present or not) and only actually verified live, here, on demand -- a real search "
            "call costs a Tavily credit, so it's never spent on a timer.</p>"
            "<form method=post action='/admin/health/check' style='margin-bottom:.6rem'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            "<button class=btn>check all now (live)</button></form>"
            "<div class=table-scroll>"
            f"<table><tr><th>integration</th><th>status</th><th>checked</th><th>detail</th></tr>"
            f"{rows}{mcp_rows}</table></div></div>")
        wsid = sess["workspace_id"]
        interval = config.get("workspace", wsid, "integration_health_interval_minutes")
        settings_form = (
            "<div class=section><h2>settings</h2>"
            "<form method=post action='/admin/health'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            "<div class=field><label>auto-check interval (minutes)</label>"
            f"<input type=number name=integration_health_interval_minutes value='{interval}' min=1></div>"
            "<button class='btn btn-primary btn-block'>save</button></form></div>")
        self._settings_response(sess, "health", f"{e}{i}{table}{settings_form}")

    def integration_health_settings_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        try:
            config.set("workspace", sess["workspace_id"], "integration_health_interval_minutes",
                       form.get("integration_health_interval_minutes", ""))
        except (KeyError, ValueError) as exc:
            return self.integration_health_admin_form(sess, err=str(exc))
        return self.integration_health_admin_form(sess, info="saved")

    def integration_health_check_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        integration_health.run_all_checks(live_tavily=True)
        return self.integration_health_admin_form(
            sess, info="checked every integration just now, including a live web-search probe")

    def backups_check_remote_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        import backup
        wsid = sess["workspace_id"]
        provider = config.get("workspace", wsid, "backup_remote_provider")
        if not provider:
            return self.backups_admin_form(sess, err="no upload destination is configured")
        result = backup.remote_prune_candidates(
            provider, config.get("workspace", wsid, "backup_retention_days"),
            site_id=config.get("workspace", wsid, "backup_sharepoint_site_id") or None)
        if "error" in result:
            return self.backups_admin_form(sess, err=result["error"])
        return self.backups_admin_form(sess, candidates=result["candidates"])

    def backups_prune_remote_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        import backup
        wsid = sess["workspace_id"]
        provider = config.get("workspace", wsid, "backup_remote_provider")
        ids = [i for i in (form.get("ids") or "").split(",") if i]
        if not provider or not ids:
            return self.backups_admin_form(sess, err="nothing to delete")
        result = backup.remote_prune_execute(
            provider, ids, site_id=config.get("workspace", wsid, "backup_sharepoint_site_id") or None)
        if result.get("failed"):
            return self.backups_admin_form(
                sess, err=f"deleted {len(result['deleted'])}, failed on {len(result['failed'])}")
        return self.backups_admin_form(sess, info=f"deleted {len(result['deleted'])} remote backup(s)")

    # -- admin: MCP client connections. Admin-only for the whole page,
    # same reasoning as sub-agents/tool-builder: a new connection is where
    # the trust decision (what does this remote code actually do) and the
    # tier assignment get made, and a remote server's own advertised tool
    # list is never enough justification on its own. Disclosed limitation:
    # a scope='user' ("just me") connection is always scoped to whichever
    # admin creates it -- a household with members other than the admin
    # who each want their own personal MCP connection isn't served well by
    # this yet; fine for a single-admin household, a real gap otherwise.
    @staticmethod
    def _tool_row(t: dict, *, show_role: bool = True) -> str:
        badge = ("<span class='chip active'>active</span>" if t["active"]
                 else f"<span class=chip>{esc(t['reason'] or 'not active')}</span>")
        meta_bits = []
        if show_role and t.get("min_role"):
            meta_bits.append(t["min_role"])
        if t.get("risk_tier"):
            meta_bits.append(f"tier {t['risk_tier']}")
        meta = f"<small>{esc(' · '.join(meta_bits))}</small>" if meta_bits else ""
        purpose = f"<small>{esc(t['purpose'] or '')}</small>" if t.get("purpose") else ""
        return (f"<div class=list-row><div class=list-meta><b>{esc(t['name'])}</b>{purpose}{meta}"
                f"</div>{badge}</div>")

    def active_tools_page(self, sess: dict):
        """Read-only -- her tools as they actually stand right now, grouped
        by where each one comes from, disabled-but-configured shown as such
        rather than silently missing (see capabilities.py's own docstring
        for why a page built from tools._REGISTRY alone would get that
        wrong). Available to every role: capabilities.gather() already
        scopes MCP/peer entries to what this session owns."""
        data = capabilities.gather(sess)
        blocks = [
            "<p class=muted>What she can actually call right now, and why anything else isn't -- "
            "not a fixed list, this reflects your account and role as they stand today.</p>"
        ]

        builtin_html = "".join(self._tool_row(t) for t in data["builtin"])
        blocks.append(f"<div class=section><h2>Built in</h2>{builtin_html}</div>")

        for group in data["mcp"]:
            state = "connected" if group["enabled"] else "disabled"
            rows = "".join(self._tool_row(t, show_role=False) for t in group["tools"]) \
                or "<p class=muted>no tools synced from this connection yet.</p>"
            blocks.append(
                f"<div class=section><h2>{esc(group['source'])} · {state}</h2>"
                f"<p class=muted>{esc(group['purpose'])}</p>{rows}</div>")

        for group in data["peers"]:
            state = "connected" if group["enabled"] else "disabled"
            rows = "".join(self._tool_row(t, show_role=False) for t in group["tools"])
            blocks.append(
                f"<div class=section><h2>{esc(group['source'])} (peer) · {state}</h2>"
                f"<p class=muted>{esc(group['purpose'])}</p>{rows}</div>")

        if data["tool_builder"]:
            rows = "".join(self._tool_row(t, show_role=False) for t in data["tool_builder"])
            blocks.append(f"<div class=section><h2>Generated tools</h2>{rows}</div>")

        self._settings_response(sess, "active_tools", "".join(blocks))

    # -- settings: About (2026-09-19, operator's own ask, direct response to
    # a real confabulation instance -- she asserted a settings "about" page
    # existed before one did). Deliberately NOT a duplicate of the public
    # /about page: that one is generic documentation for a stranger
    # evaluating her (about.html, no session); this is THIS instance's own
    # live status -- available to every role, same reasoning active_tools_page
    # already uses, since nothing here (build/uptime/model/tool count/
    # storage/integration summary) is sensitive. Every number reads from a
    # real source already used elsewhere (about.html, self_knowledge.py,
    # integration_health.py) rather than a second hand-maintained copy.
    _ABOUT_DOC_LINKS = (
        ("docs/README.md", "the full self-hosting documentation -- one page per system"),
        ("README.md", "a shorter overview: setup, admin pages, what's genuinely hard about self-hosting"),
    )

    @staticmethod
    def _fmt_bytes(n: float) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
            n /= 1024
        return f"{n:.1f}GB"

    def instance_about_page(self, sess: dict):
        import subprocess

        import chat
        import integration_health
        import tools
        import workfiles

        # git rev-parse, not a hand-maintained VERSION string -- there's no
        # version scheme in this app to duplicate or drift from; the real
        # commit this instance is actually running is always a true answer,
        # "unknown" (not a crash) if git isn't on PATH or this isn't a real
        # checkout.
        build = "unknown"
        try:
            build = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"], cwd=str(HERE), timeout=3,
                stderr=subprocess.DEVNULL).decode().strip() or "unknown"
        except Exception:  # noqa: BLE001 -- a status page's own lookup must never itself fail the page
            pass

        uptime_s = int(time.time() - START_TS)
        days, rem = divmod(uptime_s, 86400)
        hours, rem = divmod(rem, 3600)
        minutes = rem // 60
        uptime_txt = (f"{days}d {hours}h {minutes}m" if days
                     else f"{hours}h {minutes}m" if hours else f"{minutes}m")

        slug, _effort = chat._resolve_model(sess["workspace_id"])
        model_txt = slug or chat.DEFAULT_MODEL

        tool_count = len(tools.active_schemas(sess))

        db_bytes = store.DB_PATH.stat().st_size if store.DB_PATH.exists() else 0
        work_bytes, _n = workfiles._dir_usage(workfiles.WORKFILES_DIR)

        counts: dict[str, int] = {}
        for entry in integration_health.get_all().values():
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        integ_txt = ", ".join(f"{n} {status.replace('_', ' ')}" for status, n in sorted(counts.items())) \
            or "none configured"

        doc_rows = "".join(f"<li><code>{esc(name)}</code> — {esc(desc)}</li>"
                           for name, desc in self._ABOUT_DOC_LINKS)

        body = (
            "<div class=section><h2>this instance</h2>"
            "<table>"
            f"<tr><td>build</td><td class=muted>{esc(build)}</td></tr>"
            f"<tr><td>uptime</td><td class=muted>{esc(uptime_txt)}</td></tr>"
            f"<tr><td>model</td><td class=muted>{esc(model_txt)}</td></tr>"
            f"<tr><td>tools available to you</td><td class=muted>{tool_count}</td></tr>"
            f"<tr><td>database</td><td class=muted>{esc(self._fmt_bytes(db_bytes))}</td></tr>"
            f"<tr><td>working folder</td><td class=muted>{esc(self._fmt_bytes(work_bytes))}</td></tr>"
            "</table></div>"
            "<div class=section><h2>integrations</h2>"
            f"<p class=muted>{esc(integ_txt)}</p>"
            "<p><a href='/settings?tab=health'>full integration health -&gt;</a></p></div>"
            "<div class=section><h2>documentation</h2>"
            f"<ul>{doc_rows}</ul>"
            "<p class=muted>See also the public <a href='/about'>/about</a> page -- the same "
            "generic overview a stranger evaluating this instance sees, unauthenticated.</p></div>"
        )
        self._settings_response(sess, "about", body)

    def mcp_admin_form(self, sess: dict, err: str = "", info: str = ""):
        if sess["role"] != "admin":
            return self.forbidden()
        e = f"<p class=err>{esc(err)}</p>" if err else ""
        i = f"<p class=info>{esc(info)}</p>" if info else ""
        csrf = esc(sess["csrf"])
        blocks = []
        for s in mcp_servers.list_servers(sess):
            trows = []
            for t in mcp_servers.list_server_tools(s["id"]):
                role_opts = "".join(
                    f"<option value={v}{' selected' if t['min_role'] == v else ''}>{v}</option>"
                    for v in ("admin", "member"))
                scope_opts = "".join(
                    f"<option value={v}{' selected' if t['data_scope'] == v else ''}>{v}</option>"
                    for v in ("self", "workspace", "system"))
                tier_opts = "".join(
                    f"<option value={v}{' selected' if t['risk_tier'] == v else ''}>tier {v}</option>"
                    for v in ("A", "B", "C", "D"))
                trows.append(
                    "<div class=list-row><div class=list-meta>"
                    f"<b>{esc(t['tool_name'])}</b>"
                    f"<small>{esc(t['description'] or '')}</small></div></div>"
                    f"<form method=post action='/admin/mcp/{s['id']}/tool' style='display:flex;gap:.4rem;"
                    "flex-wrap:wrap;align-items:center;margin:0 0 1rem'>"
                    f"<input type=hidden name=csrf value='{csrf}'>"
                    f"<input type=hidden name=tool_name value='{esc(t['tool_name'])}'>"
                    f"<select name=min_role>{role_opts}</select>"
                    f"<select name=data_scope>{scope_opts}</select>"
                    f"<select name=risk_tier>{tier_opts}</select>"
                    "<label><input type=checkbox name=enabled "
                    + ("checked " if t["enabled"] else "") + "> enabled</label>"
                    "<button class=btn>save</button></form>")
            tools_html = "".join(trows) or "<p class=muted>no tools synced yet.</p>"
            blocks.append(
                f"<div class=section><h2>{esc(s['name'])} · {esc(s['scope'])}</h2>"
                f"<p class=muted>{esc(s['url'])} · {'enabled' if s['enabled'] else 'disabled'}</p>"
                f"<form method=post action='/admin/mcp/{s['id']}/purpose' style='display:flex;gap:.4rem;"
                "margin:0 0 .8rem'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                f"<input type=text name=purpose value='{esc(s['purpose'] or '')}' "
                "placeholder=\"what is this for? e.g. 'my personal notes app'\" style='flex:1'>"
                "<button class=btn>save</button></form>"
                "<div class=list-actions>"
                f"<form method=post action='/admin/mcp/{s['id']}/sync'>"
                f"<input type=hidden name=csrf value='{csrf}'><button class=btn>sync tools</button></form>"
                f"<form method=post action='/admin/mcp/{s['id']}/toggle'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                f"<button class=btn>{'disable' if s['enabled'] else 'enable'}</button></form>"
                f"<form method=post action='/admin/mcp/{s['id']}/delete'>"
                f"<input type=hidden name=csrf value='{csrf}'><button class='btn btn-danger'>delete</button></form>"
                f"</div>{tools_html}</div>")
        main = (
            f"{e}{i}"
            "<div class=section><h2>connect a new server</h2>"
            "<p class=muted>Her tools from a newly-connected server land enabled but "
            "locked to admin-only until you widen them below -- the server's own "
            "description of a tool is never enough to trust it on its own.</p>"
            "<form method=post action='/admin/mcp'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            "<div class=field><label>name</label>"
            "<input type=text name=name placeholder='e.g. nodrya' required></div>"
            "<div class=field><label>what is this for?</label>"
            "<input type=text name=purpose placeholder=\"e.g. 'my personal notes app'\"></div>"
            "<div class=field><label>server URL</label>"
            "<input type=password name=url placeholder='https://.../mcp' required autocomplete=off></div>"
            "<div class=field><label>who can use it</label><select name=scope>"
            "<option value=user>just me</option><option value=workspace>whole household</option>"
            "</select></div>"
            "<div class=field><label>auth</label><select name=auth_type>"
            "<option value=none>no auth</option>"
            "<option value=bearer>bearer token</option>"
            "<option value=url_embedded>token is embedded in the URL above (no separate token)</option>"
            "</select></div>"
            "<div class=field><label>token</label>"
            "<input type=password name=credential placeholder='only for \"bearer token\" -- leave blank otherwise' "
            "autocomplete=off></div>"
            "<button class='btn btn-primary btn-block'>connect</button></form></div>"
            + "".join(blocks)
        )
        self._settings_response(sess, "mcp", main)

    def mcp_admin_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        result = mcp_servers.create_server(
            sess, scope=form.get("scope") or "user", name=form.get("name") or "",
            url=form.get("url") or "", auth_type=form.get("auth_type") or "none",
            credential=form.get("credential") or None, purpose=form.get("purpose") or None)
        if "error" in result:
            return self.mcp_admin_form(sess, err=result["error"])
        return self.mcp_admin_form(sess, info=f"connected {form.get('name')} -- now sync its tools below")

    def mcp_action_post(self, sess: dict, server_id_raw: str, action: str, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        try:
            server_id = int(server_id_raw)
        except ValueError:
            return self.mcp_admin_form(sess, err="bad id")
        if action == "sync":
            result = mcp_servers.sync_tools(sess, server_id)
            if "error" in result:
                return self.mcp_admin_form(sess, err=result["error"])
            return self.mcp_admin_form(
                sess, info=f"synced -- {result['discovered']} tool(s) discovered, "
                          f"{result['removed']} no longer offered")
        if action == "toggle":
            server = mcp_servers.get_server(server_id)
            if server is None:
                return self.mcp_admin_form(sess, err="no such server")
            result = mcp_servers.set_server_enabled(sess, server_id, not server["enabled"])
        elif action == "delete":
            result = mcp_servers.delete_server(sess, server_id)
        elif action == "tool":
            result = mcp_servers.set_tool_grant(
                sess, server_id, form.get("tool_name") or "",
                min_role=form.get("min_role") or "admin", data_scope=form.get("data_scope") or "self",
                risk_tier=form.get("risk_tier") or "D", enabled="enabled" in form)
        elif action == "purpose":
            result = mcp_servers.set_purpose(sess, server_id, form.get("purpose") or "")
        else:
            return self.not_found()
        if "error" in result:
            return self.mcp_admin_form(sess, err=result["error"])
        return self.mcp_admin_form(sess, info="saved")

    # -- admin: peers (PACI, see the PACI specification) --
    # the PACI specification v0.9, §4.1 -- names the actual failure, never a bare
    # red dot. `unknown` (no check has run yet) reads as neutral, not
    # alarming -- a brand-new or just-enabled peer hasn't had a tick
    # reach it yet, which isn't itself bad news.
    _HEALTH_CHIP = {
        "healthy": ("ok", "healthy"), "unreachable": ("err", "unreachable"),
        "hmac_mismatch": ("err", "HMAC mismatch"), "limits_diverged": ("err", "limits diverged"),
        "clock_skew": ("err", "clock skew"), "unknown": ("muted", "not checked yet"),
    }

    def _health_chip(self, peer_id: int) -> str:
        h = peers.health_status(peer_id)
        css, label = self._HEALTH_CHIP.get(h["status"], ("muted", h["status"]))
        title = ""
        if h["detail"]:
            title = " title='" + esc(json.dumps(h["detail"]))[:200] + "'"
        return f"<span class={css}{title} style='font-size:.7rem;font-weight:normal'>● {label}</span>"

    def _diagnostics_html(self) -> str:
        """The one place to look for "why was there no reply?" and "why was my
        service down?" (2026-09-18) -- reply_requested decisions, round-limit
        hits, model-call failures, failed peer tool calls and the
        supervisor's own outcome log, merged newest-first (see
        diagnostics.py). Replaces three separate per-peer disclosures.
        Problems are marked and counted up front; an empty list is itself an
        answer, and is worded as one."""
        evs = diagnostics.events(40)
        bad = sum(1 for x in evs if not x["ok"])
        if not evs:
            body = ("<p class=muted>nothing recorded yet -- no request has needed a decision, no turn "
                    "has hit a limit, no model call or peer send has failed, and the supervisor has "
                    "had nothing to report.</p>")
        else:
            def _row(x):
                mark = "<span class=ok>ok</span>" if x["ok"] else "<span class=err>problem</span>"
                who = f"{esc(x['peer'])} · " if x["peer"] else ""
                detail = f"<br><span class=muted>{esc(x['detail'])}</span>" if x["detail"] else ""
                return (f"<tr><td class=muted style='white-space:nowrap'>"
                        f"{time.strftime('%m-%d %H:%M:%S', time.localtime(x['ts']))}</td>"
                        f"<td>{mark}</td><td class=muted style='white-space:nowrap'>{esc(x['source'])}</td>"
                        f"<td>{who}{esc(x['title'])}{detail}</td></tr>")
            body = f"<div class=table-scroll><table>{''.join(_row(x) for x in evs)}</table></div>"
        summary = f"{bad} problem(s) in the last {len(evs)} events" if evs else "nothing recorded"
        return (
            "<div class=section><h2>why was there no reply -- or why was it down?</h2>"
            "<p class=muted>one timeline, newest first: reply_requested decisions, turns that hit "
            "their round limit, model calls that failed after retries, peer sends/checks that "
            "failed, and the supervisor's own start/restart outcomes.</p>"
            f"<details class=sched-history{' open' if bad else ''}><summary>{esc(summary)}</summary>"
            f"{body}</details></div>")

    def peers_admin_form(self, sess: dict, err: str = "", info: str = ""):
        if sess["role"] != "admin":
            return self.forbidden()
        e = f"<p class=err>{esc(err)}</p>" if err else ""
        i = f"<p class=info>{esc(info)}</p>" if info else ""
        csrf = esc(sess["csrf"])
        blocks = []
        for p in peers.list_peers(sess):
            eff = peers.effective_limits(p["id"]) or {}
            # the PACI specification §4: turn_limit/cooldown_minutes/daily_cap are
            # negotiated -- showing only what you configured, with no sign
            # of the actually-enforced number, is exactly how "I raised
            # this" and "it's still capped at the old value" looked
            # identical until this was added.
            def _note(label, key):
                you, e = p[key], eff.get(key)
                if e is None or e == you:
                    return f"{label}: {you}"
                return f"{label}: you set {you}, effective {e} (capped by {esc(p['name'])}'s own side)"
            eff_html = (
                "<div style='display:flex;gap:.6rem;align-items:baseline;flex-wrap:wrap;margin:0 0 .8rem'>"
                f"<p class=muted style='margin:0'>effective (negotiated) values -- "
                f"{_note('turns/conv', 'turn_limit')} · {_note('cooldown min', 'cooldown_minutes')} · "
                f"{_note('daily cap', 'daily_cap')}</p>"
                f"<form method=post action='/admin/peers/{p['id']}/resync' style='display:inline'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                "<button class=btn style='font-size:.72rem;padding:.2rem .55rem'>re-sync now</button>"
                "</form></div>")
            # Identity/purpose/actions stay visible; limits/trust/
            # screening -- the dense part, 6 numeric fields plus two
            # policy controls each with their own paragraph of
            # explanation -- collapse behind a <details> (2026-09-18,
            # design pass, his own instruction: "peer fields collapse
            # behind a disclosure"). Same native, JS-free primitive
            # already proven for task/note/reminder/schedule history.
            advanced = (
                f"{eff_html}"
                f"<form method=post action='/admin/peers/{p['id']}/limits'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                "<div class=field-grid>"
                f"<div class=field><label>turns/conv</label><input type=text name=turn_limit value='{p['turn_limit']}'></div>"
                f"<div class=field><label>cooldown min</label><input type=text name=cooldown_minutes value='{p['cooldown_minutes']}'></div>"
                f"<div class=field><label>daily cap</label><input type=text name=daily_cap value='{p['daily_cap']}'></div>"
                f"<div class=field><label>expiry hrs</label><input type=text name=expiry_hours value='{p['expiry_hours']}'></div>"
                f"<div class=field><label>resend cap</label><input type=text name=resend_cap value='{p['resend_cap']}'></div>"
                f"<div class=field><label>reply_requested/hr</label>"
                f"<input type=text name=reply_requested_cap value='{p['reply_requested_cap']}'></div>"
                f"<div class=field><label>health check every (min)</label>"
                f"<input type=text name=health_check_interval_minutes value='{p['health_check_interval_minutes']}'></div>"
                f"<div class=field><label>clock skew warn (sec)</label>"
                f"<input type=text name=health_check_clock_skew_warn_s value='{p['health_check_clock_skew_warn_s']}'></div>"
                "</div>"
                "<button class=btn>save limits</button></form>"
                f"<form method=post action='/admin/peers/{p['id']}/trust' style='display:flex;gap:.4rem;"
                "align-items:center;margin:0 0 .8rem'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                "<label>what can they ask you to change? <select name=trust_level>"
                + "".join(f"<option value='{lvl}'{' selected' if p['trust_level'] == lvl else ''}>"
                         f"{label}</option>"
                         for lvl, label in (("none", "nothing (none)"),
                                            ("prompt", "ask you first (prompt)"),
                                            ("full", "whatever they ask (full)"))) +
                "</select></label>"
                "<button class=btn>save</button></form>"
                "<p class=muted style='margin:0 0 .8rem'>governs requests like turning your MCP "
                "connections on/off -- 'none' refuses them outright, 'prompt' holds them for your "
                "approval in the peer messages section below, 'full' carries them out immediately. Every outcome is logged "
                "either way.</p>"
                f"<form method=post action='/admin/peers/{p['id']}/receipts' style='display:flex;gap:.4rem;"
                "align-items:center;margin:0 0 .8rem'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                "<label>tell them when their message was read? <select name=receipt_granularity>"
                + "".join(f"<option value='{lvl}'{' selected' if p.get('receipt_granularity', 'off') == lvl else ''}>"
                         f"{label}</option>"
                         for lvl, label in (("off", "no (off)"),
                                            ("coarse", "only that she saw it (coarse)"),
                                            ("full", "everything, including if it reached you (full)"))) +
                "</select></label>"
                "<button class=btn>save</button></form>"
                "<p class=muted style='margin:0 0 .8rem'>the PACI specification §7.1: 'full' tells this peer "
                "the moment a message they sent reaches an ordinary conversation with YOU, not just "
                "her own peer-motivated turn -- which is evidence you were actively present at that "
                "moment. Fine between your own two agents; off by default for anyone else.</p>"
                f"<form method=post action='/admin/peers/{p['id']}/screening' style='display:flex;"
                "gap:.4rem;align-items:center;margin:0 0 .4rem'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                f"<label><input type=checkbox name=enabled value=1 "
                f"{'checked' if p['screening_enabled'] else ''}> screen messages from this peer for "
                "prompt-injection before she sees them</label>"
                "<button class=btn>save</button></form>"
                "<p class=muted style='margin:0 0 .8rem'>this is an injection defense, not a "
                "preference -- turning it off means a message from this peer is never checked at "
                "all before it reaches her: no category, no suspicious flag, nothing evaluated. The "
                "message still reaches her either way; the audit log still marks an unscreened one "
                "explicitly so it never reads as \"checked and clean.\" On by default for every peer.</p>"
                + (f"<p class=err style='margin:0 0 .8rem'><strong>Both are on for "
                   f"{esc(p['name'])} right now:</strong> full trust (they can make you run tools) "
                   "and screening off (nothing they send is checked for injection first). Together "
                   f"that's effectively unmediated control over your tools from {esc(p['name'])} -- "
                   "a real combination to want for two peers you control, but stated here plainly "
                   "rather than left to work out from two separate settings.</p>"
                   if p['trust_level'] == 'full' and not p['screening_enabled'] else ""))
            # The both-on warning still shows OUTSIDE the disclosure too,
            # collapsed or not -- a real risk combination is exactly the
            # kind of thing that shouldn't require opening "advanced" to
            # even notice.
            warning_visible = (
                f"<p class=err style='margin:0 0 .8rem'><strong>Full trust and screening off for "
                f"{esc(p['name'])}:</strong> open \"limits, trust &amp; screening\" below.</p>"
                if p['trust_level'] == 'full' and not p['screening_enabled'] else "")
            blocks.append(
                f"<div class=section><h2>{esc(p['name'])} · {esc(p['scope'])} {self._health_chip(p['id'])}</h2>"
                f"<p class=muted>{esc(p['url'])} · {'enabled' if p['enabled'] else 'disabled'} · "
                f"agent id {esc(p['self_agent_id'])} (give this connection's inbound URL to the other "
                f"side, using their own agent id in the path they give you)</p>"
                f"<form method=post action='/admin/peers/{p['id']}/purpose' style='display:flex;gap:.4rem;"
                "margin:0 0 .8rem'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                f"<input type=text name=purpose value='{esc(p['purpose'] or '')}' "
                "placeholder=\"what is this peer, and what's talking to them for? "
                "(this is written from her side only -- it is never sent to the peer)\" style='flex:1'>"
                "<button class=btn>save</button></form>"
                f"{warning_visible}"
                f"<details class=sched-history><summary>limits, trust &amp; screening</summary>{advanced}</details>"
                "<div class=list-actions style='margin-top:.6rem'>"
                f"<form method=post action='/admin/peers/{p['id']}/pingnow'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                f"<button class=btn {'disabled' if not p['enabled'] else ''}>check in now</button></form>"
                f"<form method=post action='/admin/peers/{p['id']}/toggle'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                f"<button class=btn>{'disable' if p['enabled'] else 'enable'}</button></form>"
                f"<form method=post action='/admin/peers/{p['id']}/delete' "
                f"data-confirm='Delete the connection to {esc(p['name'])}? This can&#39;t be undone.'>"
                f"<input type=hidden name=csrf value='{csrf}'><button class='btn btn-danger'>delete</button>"
                "</form></div></div>")
        wsid = sess["workspace_id"]
        recent_cap_settings = (
            "<div class=section><h2>recent peer awareness</h2>"
            "<p class=muted>read-only background on already-handled peer exchanges, folded into "
            "every turn's context the same way memory is -- never a trigger, never something she's "
            "asked to act on (that's pending messages, above/below). Set the window to 0 to turn "
            "this off entirely.</p>"
            "<form method=post action='/admin/peers/recent-context' style='display:flex;gap:.4rem;"
            "align-items:center;flex-wrap:wrap'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            f"<label>at most <input type=text name=cap size=2 "
            f"value='{config.get('workspace', wsid, 'peer_recent_cap')}'> messages</label>"
            f"<label>within the last <input type=text name=window_hours size=3 "
            f"value='{config.get('workspace', wsid, 'peer_recent_window_hours')}'> hours</label>"
            "<button class=btn>save</button></form></div>")
        main = (
            f"{e}{i}{self._diagnostics_html()}{recent_cap_settings}"
            "<p class=muted>a peer is another agent process she has an ongoing, symmetric "
            "conversation with -- not a tool she calls into. Setup is two-sided: create the "
            "connection here, then give the peer's own operator this connection's inbound URL "
            "(shown below once created) and the same shared secret you enter here.</p>"
            "<form method=post action='/admin/peers'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            "<div class=field><input type=text name=name placeholder='a short name for this peer' required></div>"
            "<div class=field><label>what is this peer, and what's talking to them for?</label>"
            "<input type=text name=purpose placeholder=\"e.g. 'a household assistant on my other server -- "
            "the point is relaying whether a chore got done or a package arrived'\">"
            "</div>"
            "<div class=field><label>their inbound URL</label>"
            "<input type=text name=url placeholder='http://host:port/paci/inbound/&lt;their id for this "
            "connection&gt;' required autocomplete=off></div>"
            "<div class=field><label>shared secret</label>"
            "<input type=password name=psk placeholder='pre-shared key -- same value entered on their side' "
            "required autocomplete=off></div>"
            "<div class=field><select name=scope>"
            "<option value=user>just me</option><option value=workspace>whole household</option>"
            "</select></div>"
            "<button class='btn btn-primary btn-block'>connect</button></form>"
            + "".join(blocks)
        )
        self._settings_response(sess, "peers", main + self._peer_messages_panel(sess, open_log=bool(err or info)))

    def peers_recent_context_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        try:
            cap = int(form.get("cap", ""))
            window_hours = int(form.get("window_hours", ""))
        except ValueError:
            return self.peers_admin_form(sess, err="cap and window hours must be whole numbers")
        if cap < 1:
            return self.peers_admin_form(sess, err="cap must be at least 1")
        if window_hours < 0:
            return self.peers_admin_form(sess, err="window hours can't be negative -- use 0 to turn it off")
        wsid = sess["workspace_id"]
        config.set("workspace", wsid, "peer_recent_cap", cap)
        config.set("workspace", wsid, "peer_recent_window_hours", window_hours)
        return self.peers_admin_form(sess, info="saved")

    def peers_debug_limit_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        try:
            limit = int(form.get("limit", ""))
        except ValueError:
            return self.peers_admin_form(sess, err="limit must be a whole number")
        if limit < 1:
            return self.peers_admin_form(sess, err="limit must be at least 1")
        config.set("workspace", sess["workspace_id"], "peer_debug_limit", limit)
        return self.peers_admin_form(sess, info="saved")

    def peers_admin_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        result = peers.create_peer(
            sess, scope=form.get("scope") or "user", name=form.get("name") or "",
            url=form.get("url") or "", psk=form.get("psk") or "", purpose=form.get("purpose") or None)
        if "error" in result:
            return self.peers_admin_form(sess, err=result["error"])
        peer = peers.get_peer(result["peer_id"])
        return self.peers_admin_form(
            sess, info=f"connected {form.get('name')} -- its agent id is {peer['self_agent_id']!r}; "
                      f"give the peer's operator an inbound URL for this connection")

    def peers_action_post(self, sess: dict, peer_id_raw: str, action: str, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        try:
            peer_id = int(peer_id_raw)
        except ValueError:
            return self.peers_admin_form(sess, err="bad id")
        if action == "toggle":
            peer = peers.get_peer(peer_id)
            if peer is None:
                return self.peers_admin_form(sess, err="no such peer")
            result = peers.set_peer_enabled(sess, peer_id, not peer["enabled"])
        elif action == "delete":
            result = peers.delete_peer(sess, peer_id)
        elif action == "purpose":
            result = peers.set_purpose(sess, peer_id, form.get("purpose") or "")
        elif action == "limits":
            try:
                result = peers.set_limits(
                    sess, peer_id, turn_limit=int(form.get("turn_limit", 10)),
                    cooldown_minutes=int(form.get("cooldown_minutes", 60)),
                    daily_cap=int(form.get("daily_cap", 4)), expiry_hours=int(form.get("expiry_hours", 24)),
                    resend_cap=int(form.get("resend_cap", 1)),
                    reply_requested_cap=int(form.get("reply_requested_cap", 3)),
                    health_check_interval_minutes=int(form.get("health_check_interval_minutes", 5)),
                    health_check_clock_skew_warn_s=int(form.get("health_check_clock_skew_warn_s", 60)))
            except ValueError:
                result = {"error": "limits must be whole numbers"}
        elif action == "pingnow":
            result = peers.force_checkin(sess, peer_id)
            if "error" not in result:
                return self.peers_admin_form(
                    sess, info="requested -- her message (if she sends one) will appear in chat "
                              "when ready")
        elif action == "trust":
            result = peers.set_trust_level(sess, peer_id, form.get("trust_level") or "prompt")
        elif action == "receipts":
            result = peers.set_receipt_granularity(sess, peer_id, form.get("receipt_granularity") or "off")
        elif action == "screening":
            result = peers.set_screening_enabled(sess, peer_id, form.get("enabled") == "1")
        elif action == "resync":
            result = peers.resync_now(sess, peer_id)
            if "error" not in result:
                eff = result["effective"]
                return self.peers_admin_form(
                    sess, info=f"re-synced -- effective now: turns/conv {eff['turn_limit']}, "
                              f"cooldown {eff['cooldown_minutes']} min, daily cap {eff['daily_cap']}")
        else:
            return self.not_found()
        if "error" in result:
            return self.peers_admin_form(sess, err=result["error"])
        return self.peers_admin_form(sess, info="saved")

    def peers_approval_post(self, sess: dict, action_id_raw: str, action: str):
        try:
            action_id = int(action_id_raw)
        except ValueError:
            return self.send(303, b"", {"Location": "/settings?tab=peers&err=bad+id#peer-debug"})
        result = peers.resolve_pending_action(sess, action_id, approve=(action == "approve"))
        msg = result.get("error") or ("approved" if action == "approve" else "denied")
        return self.send(303, b"", {"Location": f"/settings?tab=peers&msg={urllib.parse.quote(msg)}#peer-debug"})

    def paci_inbound_post(self, path: str):
        try:
            peer_id = int(path.rsplit("/", 1)[1])
        except ValueError:
            return self.send_json({"type": "error", "body": {"reason": "bad peer id"}}, 400)
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0 or n > 65536:
            return self.send_json({"type": "error", "body": {"reason": "missing or oversized body"}}, 400)
        raw = self.rfile.read(n)
        # self.headers (an email.message.Message) is passed as-is, not
        # dict()-converted -- its .get() is case-insensitive, and a plain
        # dict built from it would key on whatever casing this particular
        # client happened to send, silently breaking lookup for any peer
        # implementation that doesn't send the exact casing this one uses.
        code, resp = peers.handle_inbound(
            peer_id, method="POST", path=path, headers=self.headers, raw_body=raw)
        return self.send_json(resp, code)

    # Peer conversation diagnostics and pending approvals live with peer
    # settings. list_peers(sess) retains the existing visibility scope.
    @staticmethod
    def _peer_msg_preview(body: dict) -> str:
        """Two genuinely different shapes, by direction -- checked
        against the real data, not assumed (2026-09-15, second round:
        the first fix here only handled the received shape and sent
        every SENT row to '(unreadable)').

        RECEIVED: the SCREENED wrapper (category/priority/
        suggested_action/suspicious/content/truncated -- see
        ingest.summarize_untrusted(preserve_content=True)), with the
        real envelope one level down as a JSON-encoded string inside
        `content`. Every inbound message goes through this -- it's
        untrusted, external content.

        SENT: body IS the real envelope already ({"text": ...} for a
        message, {"day_state", "note", "confidence"} for a status) --
        never screened, because it's Nori's own trusted, composed
        text, not external content reaching her. There is no
        `content` key to unwrap; re-serializing the dict she already
        has and feeding it through the SAME unwrapper
        (peers._render_peer_content(), already correct for both real
        envelope shapes -- see its own day_state/text branches) covers
        this without a second, parallel rendering path."""
        raw_content = body.get("content")
        if raw_content is None:
            raw_content = json.dumps(body)
        return peers._render_peer_content(raw_content)

    @staticmethod
    def _fmt_peer_ts(ts: float) -> str:
        # Nori has no existing fmt_ts helper (that's a sibling application's own) --
        # found only by actually loading this page, which crashed on the
        # NameError instead of a clean error: an unhandled exception here
        # kills the request thread mid-response, so the browser sees a
        # dropped connection, not a readable 500. Same style as the
        # inline strftime calls already used elsewhere in this file
        # (meals_page's day/week labels), not a new convention.
        return time.strftime("%b %d, %H:%M", time.localtime(ts))

    def _peer_messages_panel(self, sess: dict, *, open_log: bool = False) -> str:
        csrf = esc(sess["csrf"])
        limit = config.get("workspace", sess["workspace_id"], "peer_debug_limit")
        limit_form = (
            "<form method=post action='/admin/peers/debug-limit' style='display:flex;gap:.4rem;"
            "align-items:center;margin:0 0 .6rem'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            f"<label style='font-size:.85em'>showing, newest first, at most "
            f"<input type=text name=limit size=3 value='{limit}' style='margin:0 .3em'></label>"
            "<button class='btn' style='font-size:.85em'>save</button></form>")
        sections = []
        for p in peers.list_peers(sess):
            pending = [a for a in peers.list_pending_actions(sess, p["id"]) if a["status"] == "pending"]
            pending_html = ""
            if pending:
                open_log = True
                items = []
                for a in pending:
                    args_txt = json.loads(a["tool_args_json"])
                    items.append(
                        f"<div class=approval-row><div>"
                        f"<b>{esc(a['tool_name'])}</b>"
                        f"<span class=muted>{esc(json.dumps(args_txt))} · "
                        f"asked {esc(self._fmt_peer_ts(a['requested_ts']))} · "
                        f"expires {esc(self._fmt_peer_ts(a['expires_ts']))}</span></div>"
                        f"<form method=post action='/peers/approvals/{a['id']}/approve' style='display:inline'>"
                        f"<input type=hidden name=csrf value='{csrf}'><button class=btn>approve</button></form>"
                        f"<form method=post action='/peers/approvals/{a['id']}/deny' style='display:inline'>"
                        f"<input type=hidden name=csrf value='{csrf}'>"
                        "<button class='btn btn-danger'>deny</button></form></div>")
                pending_html = (f"<div class=approval-block><b>{esc(p['name'])} is waiting on your "
                                f"approval:</b>{''.join(items)}</div>")
            # message/status/request are the only types ever actually
            # stored as a peer_messages row (hello/hello_ack/ack/bye are
            # protocol control traffic, never persisted here) -- this
            # matches peers.py's own _TURN_TYPES exactly, not guessed.
            #
            # Newest first, capped at this workspace's own peer_debug_limit
            # (2026-09-14, operator's own ask) -- a quick-glance panel, not
            # a transcript, so recency beats completeness here. Nothing
            # past the cap is unreachable, just not on THIS page: the count
            # below says how much more there is, and "full log" links to
            # peers_log_page's own unabridged, oldest-first view.
            total = store.read(lambda c: c.execute(
                "SELECT count(*) AS n FROM peer_messages WHERE peer_id=? AND type IN "
                "('message','status','request','reply_requested')", (p["id"],)).fetchone())["n"]
            rows = store.read(lambda c: c.execute(
                "SELECT * FROM peer_messages WHERE peer_id=? AND type IN "
                "('message','status','request','reply_requested') "
                "ORDER BY ts DESC LIMIT ?", (p["id"], limit)).fetchall())
            entries = []
            for m in (dict(r) for r in rows):
                body = json.loads(m["body_json"])
                preview = self._peer_msg_preview(body)
                side = "user" if m["direction"] == "sent" else "assistant"
                flags = []
                if m["direction"] == "sent" and m["status"] == "expired":
                    flags.append("never delivered")
                if m["direction"] == "received" and body.get("suspicious"):
                    flags.append("flagged suspicious")
                flag_html = f" · {esc(', '.join(flags))}" if flags else ""
                entries.append(
                    f"<div class='msg {side}'>{esc(preview)}"
                    f"<div class=msg-meta>{esc(self._fmt_peer_ts(m['ts']))}{flag_html}</div></div>")
            log_html = "".join(entries) or f"<p class=muted>nothing exchanged with {esc(p['name'])} yet.</p>"
            more_html = (f"<p class=muted style='margin:.5rem 0 0'>showing the {len(rows)} most recent of "
                        f"{total} — <a href='/admin/peers/{p['id']}/log'>full log, oldest first</a></p>"
                        if total > len(rows) else "")
            sections.append(
                f"<div class=peer-hdr><b>{esc(p['name'])}</b>"
                f"<span class=muted>{esc(p['purpose'] or 'no description set yet')}</span></div>"
                f"{pending_html}{log_html}{more_html}")
        body = "".join(sections) if sections else (
            "<p class=muted>no peers connected yet — an admin can add one from settings.</p>")
        return (f"<details class=peer-debug id=peer-debug{' open' if open_log else ''}>"
                "<summary>Peer messages (debug)</summary>"
                f"<div class=msglist>{limit_form}{body}</div></details>")

    # Full, unabridged (up to _PEER_LOG_MAX) peer log -- the debug panel's
    # own "beyond the cap" escape hatch (2026-09-14, operator's own ask:
    # a debug page that silently truncates is the same shape of problem as
    # everything else fixed today). Same visibility as the panel itself --
    # not admin-only, since peers.list_peers(sess) already scopes to what
    # this session can see, same gate the panel uses.
    _PEER_LOG_MAX = 1000

    def peers_log_page(self, sess: dict, peer_id_raw: str):
        try:
            peer_id = int(peer_id_raw)
        except ValueError:
            return self.not_found()
        peer = next((p for p in peers.list_peers(sess) if p["id"] == peer_id), None)
        if peer is None:
            return self.not_found()
        total = store.read(lambda c: c.execute(
            "SELECT count(*) AS n FROM peer_messages WHERE peer_id=? AND type IN "
            "('message','status','request','reply_requested')", (peer_id,)).fetchone())["n"]
        # Newest _PEER_LOG_MAX by query, then reversed for display -- if
        # there's ever more than that, keeping the most RECENT history
        # readable matters more than the oldest, so that's what a truncation
        # here drops first.
        rows = list(reversed(store.read(lambda c: c.execute(
            "SELECT * FROM peer_messages WHERE peer_id=? AND type IN "
            "('message','status','request','reply_requested') "
            "ORDER BY ts DESC LIMIT ?", (peer_id, self._PEER_LOG_MAX)).fetchall())))
        entries = []
        for m in (dict(r) for r in rows):
            body = json.loads(m["body_json"])
            preview = self._peer_msg_preview(body)
            side = "user" if m["direction"] == "sent" else "assistant"
            flags = []
            if m["direction"] == "sent" and m["status"] == "expired":
                flags.append("never delivered")
            if m["direction"] == "received" and body.get("suspicious"):
                flags.append("flagged suspicious")
            flag_html = f" · {esc(', '.join(flags))}" if flags else ""
            entries.append(
                f"<div class='msg {side}'>{esc(preview)}"
                f"<div class=msg-meta>{esc(self._fmt_peer_ts(m['ts']))}{flag_html}</div></div>")
        log_html = "".join(entries) or "<p class=muted>nothing exchanged yet.</p>"
        note = (f"oldest first · {len(rows)} of {total} total" +
               (f" · the {total - len(rows)} oldest aren't shown here" if total > len(rows) else ""))
        main = (f"<p class=muted>{esc(note)}</p><div class='msglist msglist--flow'>{log_html}</div>")
        # Chevron goes to chat, not back to the peers tab (2026-09-18,
        # design pass -- "every page gets a back-to-chat button," made
        # literal/consistent everywhere rather than each page picking
        # its own "back" target). Was pointing at /settings?tab=peers
        # #peer-debug before this; that's a real loss of one click's
        # convenience, worth knowing about, but a chevron that means
        # two different things on two different pages isn't the
        # predictable affordance he asked for.
        left = self._hdr_back(f"{peer['name']} — full log")
        self.send(200, page_app(f"{peer['name']} — full log", self._app_header(sess, left), main))

    # -- admin: tool builder --
    @staticmethod
    def _parse_params_spec(text: str) -> dict:
        """One param per line: 'name:type:description'. type/description
        optional (defaults to string, no description). Deliberately simple
        -- a human admin authoring by hand, not a full JSON-schema editor."""
        props: dict = {}
        required = []
        valid_types = {"string", "number", "integer", "boolean", "array"}
        for line in (text or "").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(":", 2)
            pname = parts[0].strip()
            if not pname:
                continue
            ptype = parts[1].strip().lower() if len(parts) > 1 and parts[1].strip().lower() in valid_types else "string"
            pdesc = parts[2].strip() if len(parts) > 2 else ""
            props[pname] = {"type": ptype, **({"description": pdesc} if pdesc else {})}
            required.append(pname)
        return {"type": "object", "properties": props, "required": required}

    def tools_admin_form(self, sess: dict, err: str = "", info: str = ""):
        if sess["role"] != "admin":
            return self.forbidden()
        e = f"<p class=err>{esc(err)}</p>" if err else ""
        i = f"<p class=info>{esc(info)}</p>" if info else ""
        csrf = esc(sess["csrf"])
        rows = []
        for d in tool_builder.list_drafts():
            flags = json.loads(d["static_flags"] or "[]")
            bits = [
                "clean" if not flags else f"FLAGGED: {', '.join(flags)}",
                "dry-run ok" if d["dry_run_ok"] else ("dry-run failed" if d["dry_run_ok"] == 0 else "no dry-run yet"),
                "approved" if d["approved"] else "not approved",
                "enabled" if d["enabled"] else "disabled",
                d["available_to"],
            ]
            actions = (f"<form method=post action='/admin/tools/{d['id']}/dry_run'>"
                      f"<input type=hidden name=csrf value='{csrf}'><button class=btn>dry-run</button></form>")
            if not d["approved"]:
                actions += (f"<form method=post action='/admin/tools/{d['id']}/approve'>"
                           f"<input type=hidden name=csrf value='{csrf}'>"
                           f"<input type=text name=confirm placeholder='type: {esc(tool_builder.APPROVE_PHRASE)}'>"
                           f"<button class=btn>approve</button></form>")
            else:
                if d["enabled"]:
                    actions += (f"<form method=post action='/admin/tools/{d['id']}/disable'>"
                               f"<input type=hidden name=csrf value='{csrf}'><button class=btn>disable</button></form>")
                else:
                    actions += (f"<form method=post action='/admin/tools/{d['id']}/enable'>"
                               f"<input type=hidden name=csrf value='{csrf}'><button class=btn>enable</button></form>")
                if d["available_to"] != "all_members":
                    actions += (f"<form method=post action='/admin/tools/{d['id']}/widen'>"
                               f"<input type=hidden name=csrf value='{csrf}'>"
                               "<button class=btn>widen to all members</button></form>")
            rows.append(f"<div class=list-row style='flex-direction:column;align-items:stretch;gap:.4rem'>"
                       f"<div><b>{esc(d['name'])}</b> v{d['version']} — {esc(' / '.join(bits))}<br>"
                       f"<span class=muted>{esc(d['description'])}</span></div>"
                       f"<div class=chip-row>{actions}</div></div>")
        rows_html = "".join(rows) or "<p class=muted>no drafts yet</p>"
        main = (
            f"{e}{i}"
            "<div class=section><h2>new draft</h2>"
            "<p class=muted>draft → static check → dry-run → approve (typed "
            "confirmation) → enable → widen. Every approved+enabled tool needs a "
            "server restart to actually go live -- no hot-reload, by design.</p>"
            "<form method=post action='/admin/tools'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            "<div class=field><label>tool name</label>"
            "<input type=text name=name placeholder='e.g. add_numbers' required></div>"
            "<div class=field><label>description</label>"
            "<input type=text name=description placeholder='description for the model' required></div>"
            "<div class=field><label>parameters</label><textarea name=params rows=3 "
            "placeholder='one param per line: name:type:description'></textarea></div>"
            "<div class=field><label>code</label><textarea name=code rows=8 "
            "placeholder='def run(args):&#10;    return {&quot;result&quot;: args[&quot;a&quot;] "
            "+ args[&quot;b&quot;]}'></textarea></div>"
            "<button class='btn btn-primary btn-block'>create draft</button></form></div>"
            f"<div class=section><h2>drafts</h2>{rows_html}</div>"
        )
        self._settings_response(sess, "tools", main)

    def tools_admin_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        name = (form.get("name") or "").strip()
        description = (form.get("description") or "").strip()
        code = form.get("code") or ""
        if not name or not description or not code.strip():
            return self.tools_admin_form(sess, "name, description, and code are all required")
        params = self._parse_params_spec(form.get("params") or "")
        r = tool_builder.create_draft(sess["user_id"], name, description, params, code)
        flag_note = "" if not r["static_flags"] else f" — FLAGGED: {', '.join(r['static_flags'])}"
        return self.tools_admin_form(sess, info=f"draft #{r['id']} ({name} v{r['version']}) created{flag_note}")

    def tools_action_post(self, sess: dict, draft_id: str, action: str, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        try:
            did = int(draft_id)
        except ValueError:
            return self.tools_admin_form(sess, "bad id")
        if action == "dry_run":
            return self.tools_admin_form(sess, info=f"dry-run result: {tool_builder.dry_run(did, sess['user_id'])}")
        if action == "approve":
            ok, msg = tool_builder.approve(did, sess["user_id"], form.get("confirm") or "")
        elif action == "enable":
            ok, msg = tool_builder.enable(did, sess["user_id"])
        elif action == "disable":
            ok, msg = tool_builder.disable(did, sess["user_id"])
        elif action == "widen":
            ok, msg = tool_builder.widen(did, sess["user_id"])
        else:
            return self.tools_admin_form(sess, "unknown action")
        return self.tools_admin_form(sess, err=("" if ok else msg), info=(msg if ok else ""))

    @staticmethod
    def _settings_groups(sess: dict) -> list:
        # "Pings" split into three (2026-09-18, design pass -- his own
        # instruction, "further reorg at your discretion"): the old
        # single tab bundled ping timing + chat display + voice + a
        # cost dashboard under one name that only really described the
        # first of those. Narrowed "Pings" to just ping timing; "Chat &
        # voice" for display/voice prefs; "Usage & cost" for the
        # read-only spend dashboard, since reporting isn't a setting.
        groups = [("Personal", (("pings", "Pings"), ("chatvoice", "Chat & voice"),
                                 ("cost", "Usage & cost"), ("notify", "Notifications"),
                                 ("memory", "Memory"), ("accounts", "Connected accounts"),
                                 ("active_tools", "Active tools"), ("schedules", "Scheduled tasks"),
                                 ("tasks", "Tasks"), ("notes", "Notes"), ("reminders", "Reminders"),
                                 ("categories", "Categories"), ("about", "About")))]
        if sess["role"] == "admin":
            groups.append(("Administration", (("household", "Household"), ("peers", "Peer connections"),
                                               ("subagents", "Sub-agents"), ("mcp", "MCP servers"),
                                               ("tools", "Tool builder"), ("models", "Model config"),
                                               ("avatars", "Avatars"), ("webtools", "Web search/fetch"),
                                               ("homeassistant", "Home Assistant"),
                                               ("memorybackend", "Memory backend"),
                                               ("persona", "Persona"), ("contexttuning", "Context tuning"), ("media", "Images"),
                                               ("backups", "Backups"), ("health", "Integration health"))))
        else:
            groups.append(("Connections", (("peers", "Peer connections"),)))
        return groups

    def _settings_rail(self, groups: list, active_tab: str) -> str:
        """The grouped list every settings screen shares -- rendered on
        EVERY request regardless of which tab (or none) is active, same
        as the .hero panel elsewhere in this file always renders and
        lets CSS decide visibility per viewport: a mobile menu screen, a
        mobile detail screen's hidden-but-present rail, and a desktop
        rail all read this exact same markup, so there's only one place
        that can drift from _settings_groups's own grouping. active_tab
        "" (the menu screen) highlights nothing, which is correct -- you
        aren't "in" any tab there."""
        out = []
        for group, tabs in groups:
            rows = "".join(
                f"<a class='settings-row{' active' if key == active_tab else ''}' "
                + ("aria-current=page " if key == active_tab else "")
                + f"href='/settings?tab={key}'>{esc(label)}<span class=settings-row-chevron>›</span></a>"
                for key, label in tabs)
            out.append(f"<div class=settings-group><div class=settings-group-label>{esc(group)}</div>{rows}</div>")
        return "".join(out)

    def _settings_shell(self, sess: dict, active_tab: str, detail_html: str) -> str:
        groups = self._settings_groups(sess)
        rail = self._settings_rail(groups, active_tab)
        active_cls = " settings-shell--active" if active_tab else ""
        return (f"<div class='settings-shell{active_cls}'>"
               f"<nav class=settings-rail aria-label='Settings sections'>{rail}</nav>"
               f"<div class=settings-content>{detail_html}</div></div>")

    def settings_menu_page(self, sess: dict):
        """Bare /settings, no ?tab= -- the settings HOME screen (2026-09-19
        design pass), not a silent fallback to whatever tab used to be
        first alphabetically. Mobile: just the grouped list, full screen.
        Desktop: the same list in its rail, plus a placeholder in the
        content pane, since there's nothing selected yet to show there."""
        placeholder = "<div class=settings-placeholder><p class=muted>Pick a section to see its settings.</p></div>"
        shell = self._settings_shell(sess, "", placeholder)
        self.send(200, page_app("Settings", self._app_header(sess, self._hdr_back("Settings")), shell,
                                content_max_width="1100px"))

    def _settings_response(self, sess: dict, tab: str, body: str, *, extra_js: str = ""):
        groups = self._settings_groups(sess)
        labels = {key: label for _, tabs in groups for key, label in tabs}
        if tab not in labels:
            return self.forbidden()
        detail = (f"<a class=settings-back href='/settings'>‹ All settings</a>"
                  f"<h1 class=settings-title>{esc(labels[tab])}</h1>{body}")
        shell = self._settings_shell(sess, tab, detail)
        self.send(200, page_app("Settings · " + labels[tab], self._app_header(sess, self._hdr_back("Settings")),
                                shell, extra_js=extra_js, content_max_width="1100px"))

    def _cost_summary_html(self, uid: int) -> str:
        """Real spend, from conversation.cost_summary() (2026-09-13,
        operator's own ask -- asked to estimate this three times in one
        day, so it's now a real number he can just look at). Read-only --
        nothing here submits anything, so it lives outside the settings
        form above it rather than inside it."""
        s = conversation.cost_summary(uid, days=7)

        def fmt(bucket: dict) -> str:
            if bucket["n"] == 0:
                return "nothing yet"
            if bucket["unavailable"]:
                note = (f" ({bucket['unavailable']} of {bucket['n']} turns didn't report a cost -- "
                        f"direct-provider calls don't)")
                return f"${bucket['cost']:.4f}+ {esc(note)}" if bucket["cost"] else f"unavailable{esc(note)}"
            return f"${bucket['cost']:.4f}"

        rows = "".join(
            f"<tr><td>{esc(kind)}</td><td>{fmt(b)}</td><td>{b['n']}</td></tr>"
            for kind, b in sorted(s["period"]["by_kind"].items()))

        # Sub-agent jobs (2026-09-14) -- same real-cost pattern, kept as its
        # own table rather than folded into "by kind" above: a dispatched
        # job isn't a conversation turn, and "attributable to the sub-
        # agent" means bucketing by agent label, not message kind.
        j = jobs.cost_summary(uid, days=7)
        job_rows = "".join(
            f"<tr><td>{esc(label)}</td><td>{fmt(b)}</td><td>{b['n']}</td></tr>"
            for label, b in sorted(j["period"]["by_agent"].items()))
        job_section = (
            f"<p class=muted style='margin:.8rem 0 .2rem'><b>sub-agent jobs, last {j['days']} days:</b> "
            f"{fmt(j['period'])} across {j['period']['n']} job(s)</p>"
            f"<div class=table-scroll><table>{job_rows}</table></div>" if job_rows else "")

        # Email ping signal's own spend (2026-09-26, the operator: "he should be able to see what the email
        # signal has cost and whether it's been skipping -- a cap that silently disables a feature he
        # enabled is the failure mode we keep fixing"). Its own section, same reasoning as sub-agent
        # jobs above: not a conversation turn, so it can't live in the "by kind" table either.
        email_spend = email_calendar.email_signal_spend_status(uid)
        cap = config.get("user", uid, "ping_signal_email_daily_cap_usd")
        email_signal_section = (
            f"<p class=muted style='margin:.8rem 0 .2rem'><b>email nag, last 24h:</b> "
            f"${email_spend['spent_today_usd']:.4f} of ${cap:.2f}/day, "
            f"{email_spend['checked_today']} check(s), "
            f"{email_spend['skipped_today']} skipped for budget</p>")

        return (
            "<div class=section><h2>what this is costing</h2>"
            f"<p class=muted style='margin:0 0 .6rem'>Real per-call cost where the model provider "
            f"reports one (OpenRouter always does when asked; a direct-provider call, when one "
            f"succeeds, doesn't -- see chat.py). Never estimated or invented: a total with any "
            f"unaccounted-for turns says so plainly rather than silently under-reporting.</p>"
            "<div class=table-scroll><table>"
            f"<tr><td>today</td><td>{fmt(s['today'])}</td><td>{s['today']['n']}</td></tr>"
            f"<tr><td>last {s['days']} days</td><td>{fmt(s['period'])}</td><td>{s['period']['n']}</td></tr>"
            "</table></div>"
            + (f"<p class=muted style='margin:.6rem 0 0'><b>by kind, last {s['days']} days:</b></p>"
               f"<div class=table-scroll><table>{rows}</table></div>" if rows else "")
            + job_section
            + email_signal_section
            + "</div>")

    def settings_page(self, sess: dict, tab: str, err: str = "", info: str = "", *, invite_link: str = "",
                      edit_id: str = "", cat_filter: str = ""):
        if tab == "peers":
            if sess["role"] == "admin":
                return self.peers_admin_form(sess, err=err, info=info)
            # Moving the view must not revoke members' existing access to
            # scoped diagnostics and approvals, or expose admin controls.
            banner = (f"<p class=err>{esc(err)}</p>" if err else "")
            banner += (f"<p class=info>{esc(info)}</p>" if info else "")
            return self._settings_response(sess, "peers", banner + self._peer_messages_panel(sess, open_log=bool(err or info)))
        if tab == "active_tools":
            # Available to every role, admin or member -- capabilities.gather()
            # already scopes MCP/peer tools to what THIS session owns via the
            # same owner_check every real dispatch uses, so there's no
            # admin-only data to leak here, unlike household/mcp/tools below.
            return self.active_tools_page(sess)
        if tab == "about":
            # Available to every role, same reasoning as active_tools --
            # read-only, nothing here (build/uptime/model/tool count/
            # storage/integration summary) is sensitive.
            return self.instance_about_page(sess)
        if tab == "schedules":
            # Personal, like pings/memory -- a schedule always belongs to
            # ONE account (schedules.py's own module docstring: even a
            # peer-created entry attaches to the peer's owning account),
            # never shared workspace state, so no admin gate here either.
            return self.schedules_admin_form(sess, err=err, info=info, edit_id=edit_id)
        if tab == "tasks":
            # Personal, same reasoning -- "see all tasks including closed
            # ones" (the operator's own ask): the board itself only ever
            # shows open ones, so this is the one place the full record,
            # closed tasks included, is visible at all.
            return self.tasks_admin_form(sess, category=cat_filter or None)
        if tab == "notes":
            # Same reasoning as tasks -- lists every note that currently
            # exists, with its own edit history. Unlike a closed task, a
            # DELETED note has no row left to list here at all -- its
            # note_events rows still survive in the database itself
            # (deliberately, see notes.delete()), just not surfaced
            # through this particular listing, which only ever shows
            # live rows, same as tasks_admin_form does for open+closed.
            return self.notes_admin_form(sess, category=cat_filter or None)
        if tab == "reminders":
            # Same reasoning as tasks -- lists active+closed, with each
            # one's own history (nags, progress, completion/missed).
            return self.reminders_admin_form(sess, category=cat_filter or None)
        if tab == "categories":
            # Personal, same as tasks/notes -- not shared workspace
            # state, one page covering every domain (task/note/reminder)
            # rather than a separate admin page per item type.
            return self.categories_admin_form(sess, err=err, info=info)
        admin_pages = {"subagents": self.subagents_admin_form, "mcp": self.mcp_admin_form,
                       "tools": self.tools_admin_form, "models": self.models_admin_form,
                       "avatars": self.avatars_admin_page, "webtools": self.webtools_admin_form,
                       "homeassistant": self.homeassistant_admin_form,
                       "contexttuning": self.context_admin_form, "persona": self.persona_admin_form, "media": self.media_admin_form,
                       "backups": self.backups_admin_form, "health": self.integration_health_admin_form,
                       "memorybackend": self.memory_backend_admin_form}
        if tab in admin_pages or tab == "household":
            if sess["role"] != "admin":
                return self.forbidden()
            if tab in admin_pages:
                return admin_pages[tab](sess)
        if tab not in ("pings", "chatvoice", "cost", "accounts", "household", "memory", "notify"):
            tab = "pings"
        csrf = esc(sess["csrf"])
        e = f"<p class=err>{esc(err)}</p>" if err else ""
        i = f"<p class=info>{esc(info)}</p>" if info else ""

        if tab == "pings":
            uid = sess["user_id"]
            # Narrowed to just ping timing (2026-09-18, design pass --
            # was 5 unrelated concerns under this one name; see
            # "chatvoice"/"cost" tabs below and Household's own debug
            # toggle for where the rest moved).
            body = (
                "<div class=section>"
                "<form method=post action='/settings/pings'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                "<div class=field><label><input type=checkbox name=ping_enabled "
                + ("checked " if config.get("user", uid, "ping_enabled") else "") +
                "> she can message me first</label></div>"
                "<div class=field><label>window start (hour, 0-23)</label>"
                f"<input type=text name=ping_window_start value='{config.get('user', uid, 'ping_window_start')}'></div>"
                "<div class=field><label>window end (hour, 0-23)</label>"
                f"<input type=text name=ping_window_end value='{config.get('user', uid, 'ping_window_end')}'></div>"
                "<div class=field><label>minimum minutes between unprompted messages</label>"
                f"<input type=text name=ping_min_gap_min value='{config.get('user', uid, 'ping_min_gap_min')}'></div>"
                # Derived from scheduler.signal_registry() (the operator, 2026-09-26: "the settings tab is
                # derived from what the ping actually pushes, never a hand-maintained list") -- a new
                # domain module's own scheduler.register_signal() call is the only place a toggle for
                # it is ever created; nothing here names a signal by hand. A disabled one is skipped
                # before its function is even called (scheduler.outstanding_reason()), never just a
                # suppressed mention.
                "<div class=field><label>what she can bring up on her own</label>"
                + "".join(
                    f"<div class=field><label><input type=checkbox name='{esc(s['config_key'])}' "
                    + ("checked " if config.get("user", uid, s["config_key"]) else "") +
                    f"> {esc(s['label'])}</label></div>"
                    for s in scheduler.signal_registry()
                ) +
                "</div>"
                # Email's own cadence, a plain count-per-day (the operator, 2026-09-26: "that's how he expressed
                # it and it's what he'll want to adjust"), spread across his own ping window above, not
                # a fixed interval around the clock -- see email_calendar._email_check_interval_seconds().
                "<div class=field><label>email nag: checks per day (spread across the window above)</label>"
                f"<input type=text name=ping_signal_email_checks_per_day "
                f"value='{config.get('user', uid, 'ping_signal_email_checks_per_day')}'></div>"
                "<button class='btn btn-primary btn-block'>save</button></form></div>"
            )
        elif tab == "chatvoice":
            uid = sess["user_id"]
            cur_voice = config.get("user", uid, "tts_voice")
            voice_opts = "".join(
                f"<option value='{v}'{' selected' if v == cur_voice else ''}>{v}</option>" for v in voice.VOICES)
            vision_tip = info_tip(
                "Sends a photo you post in chat to a 3rd-party vision model so she can describe it. "
                "Separate from the working-folder vision setting on the Files page, since these are "
                "different photos with different privacy tradeoffs. With this off, she's told plainly "
                "a photo arrived with whatever caption you gave it, but not what it shows.")
            rounds_chat_tip = info_tip(
                "How many tool-call steps she can chain in one reply before she has to answer. You're "
                "watching a live chat, so this can be generous -- if a reply ever stops with \"hit the "
                "tool-call limit,\" raise this.")
            rounds_proactive_tip = info_tip(
                "Same idea, but for a message she sends on her own (a scheduled check-in, a peer nudge, "
                "\"check in now\"). Nobody's watching those happen, so this stays lower on purpose.")
            voice_select_tip = info_tip(
                "One of OpenAI's fixed preset voices -- gpt-4o-mini-tts speaks from a set list, it "
                "doesn't clone a sample. Used whenever she speaks in a voice conversation.")
            voice_input_tip = info_tip(
                "Adds a mic button that opens a push-to-talk voice mode: hold it to speak, she answers "
                "out loud. Holding it records your mic and sends that recording to OpenAI's speech-to-"
                "text API to transcribe, and her reply is sent to OpenAI's text-to-speech API to speak "
                "it -- real audio leaves this machine both ways. It's never always-listening -- only "
                "while you're physically holding the mic button.")
            body = (
                "<div class=section><h2>chat</h2>"
                "<form method=post action='/settings/chatvoice'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                "<div class=field><label><input type=checkbox name=show_tool_calls "
                + ("checked " if config.get("user", uid, "show_tool_calls") else "") +
                "> show a line when she uses a tool</label></div>"
                f"<div class=field><label><input type=checkbox name=chat_vision_enabled "
                + ("checked " if config.get("user", uid, "chat_vision_enabled") else "") +
                f"> let her actually see photos you send in chat {vision_tip}</label></div>"
                f"<div class=field><label>tool call rounds -- live chat {rounds_chat_tip}</label>"
                f"<input type=text name=tool_rounds_chat value='{config.get('user', uid, 'tool_rounds_chat')}'></div>"
                f"<div class=field><label>tool call rounds -- unprompted / background {rounds_proactive_tip}</label>"
                f"<input type=text name=tool_rounds_proactive value='{config.get('user', uid, 'tool_rounds_proactive')}'></div>"
                "<h3>voice</h3>"
                f"<div class=field><label>her voice {voice_select_tip}</label>"
                f"<select name=tts_voice>{voice_opts}</select></div>"
                f"<div class=field><label><input type=checkbox name=voice_input_enabled "
                + ("checked " if config.get("user", uid, "voice_input_enabled") else "") +
                f"> let me have voice conversations with her {voice_input_tip}</label></div>"
                "<button class='btn btn-primary btn-block'>save</button></form></div>"
            )
        elif tab == "cost":
            body = self._cost_summary_html(sess["user_id"])
        elif tab == "notify":
            uid = sess["user_id"]
            peer_msg_tip = info_tip(
                "While she is talking with a connected peer, she can choose to tell you something "
                "directly if it is actually worth it -- no cap on how often; these quiet hours "
                "already handle the timing, and her own judgment (delivered alongside whatever the "
                "peer said) is what decides whether it is worth interrupting you for.")
            tz_tip = info_tip(
                "Every 'today', quiet-hours window, reminder due time, and scheduled task fire time "
                "is computed against this zone -- not the server's own clock. Auto-filled from a "
                "connected Google Calendar the first time you connect one; change it here if it's "
                "ever wrong, or if you move. A real IANA name, e.g. America/New_York, America/"
                "Los_Angeles, Europe/London -- not just an offset, so it stays correct across a "
                "daylight-saving change instead of drifting an hour twice a year.")
            body = (
                f"<div class=section><h2>your timezone {tz_tip}</h2>"
                "<form method=post action='/settings/timezone' style='display:flex;gap:.5rem'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                f"<input type=text name=timezone value='{esc(usertime.zone_name(uid))}' "
                "placeholder='America/New_York' style='flex:1'>"
                "<button class=btn>save</button></form></div>"
                "<div class=section><h2>how notifications work here</h2>"
                "<p class=muted style='line-height:1.5'>This is <b>not</b> a cloud service. "
                "A notification is raised by the app on your own device when it polls the "
                "server and finds something new -- including something she said unprompted. "
                "That means it only arrives while <b>(a)</b> this machine is on and Nori's "
                "server is running, and <b>(b)</b> the app is open, or was open recently and "
                "is just backgrounded. A fully closed tab or installed app won't wake up -- "
                "you'll see what you missed, and the unread badge, next time you open it. "
                "<b>iOS</b> only delivers these if you've added Nori to your home screen "
                "first (iOS 16.4+); a plain Safari tab gets nothing. iOS is also stricter than "
                "\"recently backgrounded\" above suggests: Safari suspends an installed app's "
                "background execution almost as soon as you leave it, well before the poll above "
                "could complete -- in practice, expect these only while you actually have Nori open "
                "in front of you. Anything you missed still shows up, in chat and as the unread "
                "badge, the moment you open it again. Turning notifications on "
                "needs a tap on the chat screen's own prompt -- a settings toggle here can't "
                "trigger the permission request.</p></div>"
                "<div class=section><h2>this browser's status</h2>"
                "<p class=muted id=notif-permission-text>checking&hellip;</p></div>"
                "<div class=section>"
                "<form method=post action='/settings/notify'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                "<div class=field><label><input type=checkbox name=notify_enabled "
                + ("checked " if config.get("user", uid, "notify_enabled") else "") +
                "> notify me when she messages</label></div>"
                # Peer-message note (2026-09-18, moved off the old "Pings"
                # tab, which had a whole section for this with no
                # control attached to it at all) -- lands here instead,
                # behind the icon, next to the one field it's actually
                # related to (quiet hours/timing).
                + (f"<div class=field><label>quiet hours start (local hour, 0-23) {peer_msg_tip}</label>"
                  f"<input type=text name=notify_quiet_start value='{config.get('user', uid, 'notify_quiet_start')}'></div>") +
                "<div class=field><label>quiet hours end (local hour, 0-23)</label>"
                f"<input type=text name=notify_quiet_end value='{config.get('user', uid, 'notify_quiet_end')}'></div>"
                "<p class=muted>set start = end to disable quiet hours. During them a message "
                "still lands in chat, just without a notification.</p>"
                "<button class='btn btn-primary btn-block'>save</button></form></div>"
            )
        elif tab == "memory":
            uid = sess["user_id"]
            flags = memory.removal_candidates(uid)
            flag_html = ""
            if flags:
                frows = []
                for f in flags:
                    flagged_by = " (peer-requested)" if f["actor"] == "peer" else ""
                    frows.append(
                        f"<div class=list-row><div class=list-meta>"
                        f"<b class=wrap>[{esc(f['type'] or '?')}] {esc(f['value'] or '')}</b>"
                        f"<small>flagged{esc(flagged_by)}: {esc(f['note'] or '')}</small></div>"
                        f"<div class=list-actions>"
                        f"<form method=post action='/settings/memory/resolve'>"
                        f"<input type=hidden name=csrf value='{csrf}'>"
                        f"<input type=hidden name=memory_id value='{f['memory_id']}'>"
                        f"<input type=hidden name=action value=dismiss>"
                        f"<button class=btn>keep</button></form>"
                        f"<form method=post action='/settings/memory/resolve'>"
                        f"<input type=hidden name=csrf value='{csrf}'>"
                        f"<input type=hidden name=memory_id value='{f['memory_id']}'>"
                        f"<input type=hidden name=action value=remove>"
                        f"<button class='btn btn-danger'>remove</button></form></div></div>")
                flag_html = f"<div class=section><h2>flagged for review</h2>{''.join(frows)}</div>"
            rows = memory.all_rows(uid)
            by_type: dict = {}
            for r in rows:
                by_type.setdefault(r["type"], []).append(r)
            sections = []
            for t in memory.TYPES:
                entries = by_type.get(t)
                if not entries:
                    continue
                erows = "".join(
                    f"<div class=list-row><div class=list-meta>"
                    f"<b class=wrap>{esc(d['value'])}</b>"
                    f"<small>{esc(d['source'])}{' · pinned' if d['pinned'] else ''}"
                    f"{' · SAFETY/CONSTRAINT' if d['safety_tier'] else ''}"
                    f"{' · ' + esc(', '.join(d['tags'])) if d['tags'] else ''}</small>"
                    f"<form method=post action='/settings/memory/edit' "
                    f"style='display:flex;gap:.4rem;margin-top:.4rem'>"
                    f"<input type=hidden name=csrf value='{csrf}'>"
                    f"<input type=hidden name=memory_id value='{d['id']}'>"
                    f"<textarea name=value rows=2 style='flex:1;font:inherit'>{esc(d['value'])}</textarea>"
                    f"<button class=btn>save</button></form></div>"
                    f"<div class=list-actions>"
                    f"<form method=post action='/settings/memory/pin'>"
                    f"<input type=hidden name=csrf value='{csrf}'>"
                    f"<input type=hidden name=memory_id value='{d['id']}'>"
                    f"<input type=hidden name=action value='{'unpin' if d['pinned'] else 'pin'}'>"
                    f"<button class=btn>{'unpin' if d['pinned'] else 'pin'}</button></form>"
                    f"<form method=post action='/settings/memory/safety'>"
                    f"<input type=hidden name=csrf value='{csrf}'>"
                    f"<input type=hidden name=memory_id value='{d['id']}'>"
                    f"<input type=hidden name=action value='{'unsafety' if d['safety_tier'] else 'safety'}'>"
                    f"<button class=btn>{'unmark safety' if d['safety_tier'] else 'mark safety'}</button></form>"
                    f"<form method=post action='/settings/memory/delete' "
                    f"data-confirm=\"Delete this memory? This can't be undone.\">"
                    f"<input type=hidden name=csrf value='{csrf}'>"
                    f"<input type=hidden name=memory_id value='{d['id']}'>"
                    f"<button class='btn btn-danger'>delete</button></form>"
                    f"</div></div>"
                    for d in entries)
                sections.append(f"<div class=section><h2>{esc(t)}</h2>{erows}</div>")
            body = (flag_html + "".join(sections)) or "<p class=muted>nothing remembered yet.</p>"
        elif tab == "household":
            if sess["role"] != "admin":
                body = "<p class=muted>admin only.</p>"
            else:
                ws = accounts.get_workspace(sess["workspace_id"])
                debug_tip = info_tip(
                    "For the whole household, not just you -- this is for finding out where a slow "
                    "turn's time actually goes (context building, the model call itself per round, "
                    "tool calls, content screening, persisting the reply), not a personal preference. "
                    "Writes a real timestamped line to the server's own log per turn; costs one extra "
                    "database read per turn either way, effectively nothing when off.")
                body = (
                    "<div class=section>"
                    "<form method=post action='/settings/household'>"
                    f"<input type=hidden name=csrf value='{csrf}'>"
                    f"<div class=field><input type=text name=name value='{esc(ws['name'] if ws else '')}' required></div>"
                    "<button class='btn btn-primary btn-block'>rename household</button></form></div>"
                    # Debug timing moved here from the old "Pings" tab
                    # (2026-09-18, design pass) -- it's workspace-scoped,
                    # not personal, so it belongs with every other
                    # workspace-level control, not a household member's
                    # own settings.
                    "<div class=section>"
                    "<form method=post action='/settings/household/debug'>"
                    f"<input type=hidden name=csrf value='{csrf}'>"
                    f"<div class=field><label><input type=checkbox name=debug_timing_enabled "
                    + ("checked " if config.get("workspace", sess["workspace_id"], "debug_timing_enabled") else "") +
                    f"> log per-turn timing {debug_tip}</label></div>"
                    "<button class='btn btn-primary btn-block'>save</button></form></div>"
                ) + self._household_members(sess, invite_link)
        elif tab == "accounts":
            rows = []
            for provider, row in connected_accounts.list_for_user(sess["user_id"]).items():
                label = oauth.PROVIDERS.get(provider, {}).get("label", provider)
                disconnect_form = (f"<form method=post action='/disconnect/{provider}'>"
                                   f"<input type=hidden name=csrf value='{csrf}'>"
                                   "<button class=btn>disconnect</button></form>")
                if row["status"] == "connected":
                    action = disconnect_form
                    note = "connected"
                elif row["status"] == "needs_reconnect":
                    # A real connection went bad -- distinct from never having
                    # connected at all (2026-09-18), so this doesn't read as
                    # "not connected" (which reads as "never tried") when
                    # it's actually "was working, now isn't." reconnect_diagnosis()
                    # is the same function the proactive ping signal uses
                    # (connected_accounts.py), so this page and that ping
                    # never disagree about the same failure.
                    reason = connected_accounts.reconnect_diagnosis(provider, row)
                    reason_txt = f" -- {reason}" if reason else ""
                    action = (f"<a class=btn href='/connect/{provider}'>reconnect</a>{disconnect_form}"
                             if oauth.is_configured(provider) else disconnect_form)
                    note = f"needs reconnecting{esc(reason_txt)}"
                elif oauth.is_configured(provider):
                    action = f"<a class=btn href='/connect/{provider}'>connect</a>"
                    note = "not connected"
                else:
                    action = ""
                    note = "not connected · operator hasn't configured this provider yet"
                rows.append(f"<div class=list-row><div class=list-icon>🔗</div>"
                           f"<div class=list-meta><b>{esc(label)}</b><small>{note}</small></div>"
                           f"<div class=list-actions>{action}</div></div>")
            body = "".join(rows)

        self._settings_response(sess, tab, f"{e}{i}{body}", extra_js=NOTIFY_STATUS_JS)

    def settings_pings_post(self, sess: dict, form: dict):
        # Narrowed to just ping timing (2026-09-18 -- see "chatvoice"
        # tab's own POST handler below for the fields that used to live
        # in this same form before the Pings split).
        uid = sess["user_id"]
        try:
            window_start = int(form.get("ping_window_start", 8))
            window_end = int(form.get("ping_window_end", 22))
            min_gap = int(form.get("ping_min_gap_min", 90))
            email_checks_per_day = int(form.get("ping_signal_email_checks_per_day", 4))
        except (TypeError, ValueError):
            return self.settings_page(sess, "pings", err="hours and minutes must be whole numbers")
        if email_checks_per_day < 1:
            return self.settings_page(sess, "pings", err="email checks per day must be at least 1")
        config.set("user", uid, "ping_enabled", "ping_enabled" in form)
        config.set("user", uid, "ping_window_start", window_start)
        config.set("user", uid, "ping_window_end", window_end)
        config.set("user", uid, "ping_min_gap_min", min_gap)
        config.set("user", uid, "ping_signal_email_checks_per_day", email_checks_per_day)
        for s in scheduler.signal_registry():
            config.set("user", uid, s["config_key"], s["config_key"] in form)
        return self.settings_page(sess, "pings", info="saved")

    def settings_chatvoice_post(self, sess: dict, form: dict):
        uid = sess["user_id"]
        try:
            # A generous, not unbounded, ceiling -- this is still the
            # backstop against a runaway tool-calling loop burning tokens,
            # so a fat-fingered value shouldn't quietly remove that.
            rounds_chat = int(form.get("tool_rounds_chat", 12))
            rounds_proactive = int(form.get("tool_rounds_proactive", 4))
        except (TypeError, ValueError):
            return self.settings_page(sess, "chatvoice", err="tool rounds must be whole numbers")
        if not (1 <= rounds_chat <= 40):
            return self.settings_page(sess, "chatvoice", err="live-chat tool rounds must be 1-40")
        if not (1 <= rounds_proactive <= 40):
            return self.settings_page(sess, "chatvoice", err="background tool rounds must be 1-40")
        config.set("user", uid, "show_tool_calls", "show_tool_calls" in form)
        config.set("user", uid, "chat_vision_enabled", "chat_vision_enabled" in form)
        config.set("user", uid, "tool_rounds_chat", rounds_chat)
        config.set("user", uid, "tool_rounds_proactive", rounds_proactive)
        tts_voice = form.get("tts_voice") or voice.DEFAULT_VOICE
        config.set("user", uid, "tts_voice", tts_voice if tts_voice in voice.VOICES else voice.DEFAULT_VOICE)
        config.set("user", uid, "voice_input_enabled", "voice_input_enabled" in form)
        return self.settings_page(sess, "chatvoice", info="saved")

    def settings_notify_post(self, sess: dict, form: dict):
        uid = sess["user_id"]
        try:
            config.set("user", uid, "notify_enabled", "notify_enabled" in form)
            config.set("user", uid, "notify_quiet_start", int(form.get("notify_quiet_start", 23)))
            config.set("user", uid, "notify_quiet_end", int(form.get("notify_quiet_end", 8)))
        except (TypeError, ValueError):
            return self.settings_page(sess, "notify", err="hours must be whole numbers")
        return self.settings_page(sess, "notify", info="saved")

    def settings_timezone_post(self, sess: dict, form: dict):
        tz = (form.get("timezone") or "").strip()
        if not tz:
            return self.settings_page(sess, "notify", err="timezone can't be blank")
        if not usertime.is_valid_zone(tz):
            return self.settings_page(sess, "notify",
                                      err=f"{tz!r} isn't a real IANA timezone name -- try something "
                                          f"like America/New_York or Europe/London")
        config.set("user", sess["user_id"], "timezone", tz)
        return self.settings_page(sess, "notify", info="timezone saved")

    def settings_memory_resolve_post(self, sess: dict, form: dict):
        try:
            memory_id = int(form.get("memory_id", ""))
        except (TypeError, ValueError):
            return self.settings_page(sess, "memory", err="bad id")
        action = form.get("action") or ""
        result = memory.resolve_removal_flag(sess["user_id"], memory_id, action)
        if "error" in result:
            return self.settings_page(sess, "memory", err=result["error"])
        return self.settings_page(sess, "memory", info="removed" if action == "remove" else "kept")

    def settings_memory_pin_post(self, sess: dict, form: dict):
        try:
            memory_id = int(form.get("memory_id", ""))
        except (TypeError, ValueError):
            return self.settings_page(sess, "memory", err="bad id")
        pin = (form.get("action") or "") != "unpin"
        result = memory.set_pinned(sess["user_id"], memory_id, pin)
        if "error" in result:
            return self.settings_page(sess, "memory", err=result["error"])
        return self.settings_page(sess, "memory", info="pinned" if pin else "unpinned")

    def settings_memory_safety_post(self, sess: dict, form: dict):
        """The operator's own way to retroactively mark (or un-mark) an
        existing memory as safety/constraint-tier -- see memory.py's
        set_safety_tier docstring for why this is a human judgment call,
        never inferred from the fact's own content."""
        try:
            memory_id = int(form.get("memory_id", ""))
        except (TypeError, ValueError):
            return self.settings_page(sess, "memory", err="bad id")
        flag = (form.get("action") or "") != "unsafety"
        result = memory.set_safety_tier(sess["user_id"], memory_id, flag)
        if "error" in result:
            return self.settings_page(sess, "memory", err=result["error"])
        return self.settings_page(sess, "memory", info="marked safety/constraint" if flag else "unmarked")

    def settings_memory_edit_post(self, sess: dict, form: dict):
        try:
            memory_id = int(form.get("memory_id", ""))
        except (TypeError, ValueError):
            return self.settings_page(sess, "memory", err="bad id")
        result = memory.edit_memory(sess["user_id"], memory_id, form.get("value") or "")
        if "error" in result:
            return self.settings_page(sess, "memory", err=result["error"])
        return self.settings_page(sess, "memory", info="saved")

    def settings_memory_delete_post(self, sess: dict, form: dict):
        try:
            memory_id = int(form.get("memory_id", ""))
        except (TypeError, ValueError):
            return self.settings_page(sess, "memory", err="bad id")
        result = memory.delete_memory(sess["user_id"], memory_id)
        if "error" in result:
            return self.settings_page(sess, "memory", err=result["error"])
        return self.settings_page(sess, "memory", info="deleted")

    def settings_household_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        accounts.rename_workspace(sess["workspace_id"], form.get("name") or "")
        return self.settings_page(sess, "household", info="saved")

    def settings_household_debug_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        config.set("workspace", sess["workspace_id"], "debug_timing_enabled", "debug_timing_enabled" in form)
        return self.settings_page(sess, "household", info="saved")

    # -- household inventory: a first-class destination, not a settings
    # tab -- everyday, glance-and-go, the kind of thing checked from a
    # phone in a supermarket aisle. Member-accessible with no role check
    # here, same as the tools themselves (household.py's min_role="member"
    # -- gatekeeping "we're out of milk" behind admin would defeat the
    # point). Every read/write below calls straight into household.py's
    # own functions -- the same code Nori's tools call -- so the UI and
    # her can never see two different realities of this table, and
    # attribution is free: upsert_item/remove_item already stamp
    # updated_by from session["user_id"], whoever that session belongs to.
    STATUS_LABEL = {"out": "out of stock", "low": "running low", "ok": "in stock"}
    STATUS_DOT = {"out": "var(--danger)", "low": "var(--k-reminder)", "ok": "var(--k-task)"}

    def inventory_page(self, sess: dict, err: str = "", info: str = ""):
        csrf = esc(sess["csrf"])
        items = household.list_items(sess, include_meta=True)["items"]
        names = {u["id"]: u["display_name"] for u in accounts.list_users(sess["workspace_id"])}
        groups: dict[str, list] = {}
        for it in items:
            groups.setdefault(it["status"], []).append(it)
        sections = []
        for status in ("out", "low", "ok"):
            rows = groups.get(status) or []
            if not rows:
                continue
            rrows = []
            for it in rows:
                who = names.get(it["updated_by"], "someone")
                qty = f"{esc(it['quantity'])} · " if it["quantity"] else ""
                qname = urllib.parse.quote(it["name"])
                rrows.append(
                    "<div class=list-row>"
                    f"<div class=list-icon><span class=status-dot style='background:{self.STATUS_DOT[status]}'></span></div>"
                    f"<div class=list-meta><b><a href='/inventory/edit?name={qname}'>{esc(it['name'])}</a></b>"
                    f"<small>{qty}{esc(who)} · {_ago(it['updated_ts'])}</small></div>"
                    "<div class=list-actions>"
                    "<form method=post action='/inventory/remove'>"
                    f"<input type=hidden name=csrf value='{csrf}'>"
                    f"<input type=hidden name=name value='{esc(it['name'])}'>"
                    "<button class=icon-action aria-label=Remove>✕</button></form></div></div>")
            sections.append(f"<div class=section><h2>{self.STATUS_LABEL[status]} ({len(rows)})</h2>{''.join(rrows)}</div>")
        body = "".join(sections) or "<p class=muted>nothing in the inventory yet — add the first item below.</p>"
        e_html = f"<p class=err>{esc(err)}</p>" if err else ""
        i_html = f"<p class=info>{esc(info)}</p>" if info else ""
        opts = "".join(f"<option value='{s}'{' selected' if s == 'ok' else ''}>{s}</option>" for s in household.STATUSES)
        main = f"{e_html}{i_html}{body}"
        footer = (
            "<form method=post action='/inventory/upsert' style='display:contents'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            "<input type=text name=name placeholder='item name' required style='flex:1;min-width:0'>"
            f"<select name=status style='width:88px'>{opts}</select>"
            "<input type=text name=quantity placeholder=qty style='width:56px'>"
            "<button class=send-btn aria-label=Add>+</button></form>"
        )
        self.send(200, page_app("inventory", self._app_header(sess, self._hdr_back("Inventory")), main, footer))

    def inventory_edit_page(self, sess: dict, name: str, err: str = ""):
        items = household.list_items(sess, include_meta=True)["items"]
        item = next((i for i in items if i["name"] == name), None)
        if item is None:
            return self.inventory_page(sess, err="no such item")
        names = {u["id"]: u["display_name"] for u in accounts.list_users(sess["workspace_id"])}
        who = names.get(item["updated_by"], "someone")
        csrf = esc(sess["csrf"])
        opts = "".join(f"<option value='{s}'{' selected' if s == item['status'] else ''}>{s}</option>"
                      for s in household.STATUSES)
        e_html = f"<p class=err>{esc(err)}</p>" if err else ""
        main = (
            f"{e_html}<div class=section>"
            f"<p class=muted>last updated by {esc(who)} · {_ago(item['updated_ts'])}</p>"
            "<form method=post action='/inventory/upsert'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            f"<input type=hidden name=name value='{esc(name)}'>"
            f"<div class=field><label>status</label><select name=status>{opts}</select></div>"
            f"<div class=field><label>quantity</label>"
            f"<input type=text name=quantity value='{esc(item['quantity'] or '')}'></div>"
            "<button class='btn btn-primary btn-block'>save</button></form>"
            "<form method=post action='/inventory/remove' style='margin-top:.8rem'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            f"<input type=hidden name=name value='{esc(name)}'>"
            "<button class='btn btn-danger btn-block'>remove item</button></form></div>"
        )
        self.send(200, page_app("edit item", self._app_header(sess, self._hdr_back(name)), main))

    def inventory_upsert_post(self, sess: dict, form: dict):
        name = (form.get("name") or "").strip()
        status = form.get("status") or "ok"
        quantity = (form.get("quantity") or "").strip() or None
        result = household.upsert_item(sess, name, status, quantity)
        if "error" in result:
            return self.inventory_page(sess, err=result["error"])
        return self.inventory_page(sess, info=f"saved {name}")

    def inventory_remove_post(self, sess: dict, form: dict):
        result = household.remove_item(sess, (form.get("name") or "").strip())
        if "error" in result:
            return self.inventory_page(sess, err=result["error"])
        return self.inventory_page(sess, info="removed")

    # -- meal planning: same reasoning as inventory above -- first-class,
    # member-accessible, reads/writes go straight through meals.py's own
    # functions. A WEEK view rather than a flat list since meals are
    # calendar-shaped by nature: 7 day-sections down the page (each its
    # own scrolling-friendly block, not a cross-axis grid that would need
    # horizontal scroll on a phone), 3 meal-type rows per day, prev/next
    # week navigation via a `start` query param. The default window begins
    # today; explicit dates allow browsing other weeks. Each row's form
    # preserves the selected start date when saving or clearing a meal.
    def _week_start(self, start_param: str) -> "datetime.date":
        today = datetime.date.today()
        if start_param:
            try:
                return datetime.date.fromisoformat(start_param)
            except ValueError:
                pass
        return today

    def meals_page(self, sess: dict, start_param: str, err: str = "", info: str = ""):
        start = self._week_start(start_param)
        end = start + datetime.timedelta(days=6)
        rows = meals.list_meals(sess, start.isoformat(), end.isoformat())["meals"]
        by_day: dict[str, dict[str, str]] = {}
        for r in rows:
            by_day.setdefault(r["meal_date"], {})[r["meal_type"]] = r["description"]
        csrf = esc(sess["csrf"])
        today = datetime.date.today()
        sections = []
        for i in range(7):
            day = start + datetime.timedelta(days=i)
            iso = day.isoformat()
            label = f"{day.strftime('%a')} {day.month}/{day.day}" + (" · today" if day == today else "")
            day_rows = []
            for mt in meals.MEAL_TYPES:
                desc = (by_day.get(iso) or {}).get(mt, "")
                row_cls = "meal-row meal-set" if desc else "meal-row meal-empty"
                clear_btn = (
                    "<form method=post action='/meals/clear'>"
                    f"<input type=hidden name=csrf value='{csrf}'>"
                    f"<input type=hidden name=meal_date value='{iso}'>"
                    f"<input type=hidden name=meal_type value='{mt}'>"
                    f"<input type=hidden name=start value='{start.isoformat()}'>"
                    "<button class=icon-action aria-label=Clear>✕</button></form>"
                ) if desc else ""
                day_rows.append(
                    f"<div class='{row_cls}'><span class=meal-type-badge>{mt}</span>"
                    "<form method=post action='/meals/set' style='flex:1;display:flex;gap:.4rem;min-width:0'>"
                    f"<input type=hidden name=csrf value='{csrf}'>"
                    f"<input type=hidden name=meal_date value='{iso}'>"
                    f"<input type=hidden name=meal_type value='{mt}'>"
                    f"<input type=hidden name=start value='{start.isoformat()}'>"
                    f"<input type=text name=description value='{esc(desc)}' placeholder='nothing planned yet' "
                    "style='flex:1;min-width:0'>"
                    "<button class=icon-action aria-label=Save>✓</button></form>"
                    f"{clear_btn}</div>")
            sections.append(f"<div class=section><h2>{esc(label)}</h2>{''.join(day_rows)}</div>")
        prev_start = (start - datetime.timedelta(days=7)).isoformat()
        next_start = (start + datetime.timedelta(days=7)).isoformat()
        week_label = f"{start.strftime('%b')} {start.day} – {end.strftime('%b')} {end.day}"
        jump_today = ("" if start == today else
                     "<div style='text-align:right;margin-bottom:.8rem'>"
                     "<a class='btn btn-ghost' href='/meals'>today onward</a></div>")
        nav = (
            "<div class=week-nav>"
            f"<a class=icon-btn href='/meals?start={prev_start}' aria-label='previous week'>‹</a>"
            f"<div class=week-label>{esc(week_label)}</div>"
            f"<a class=icon-btn href='/meals?start={next_start}' aria-label='next week'>›</a></div>"
            f"{jump_today}"
        )
        e_html = f"<p class=err>{esc(err)}</p>" if err else ""
        i_html = f"<p class=info>{esc(info)}</p>" if info else ""
        main = f"{nav}{e_html}{i_html}{''.join(sections)}"
        self.send(200, page_app("meal plan", self._app_header(sess, self._hdr_back("Meal plan")), main))

    def meals_set_post(self, sess: dict, form: dict):
        start = form.get("start") or ""
        result = meals.set_meal(sess, form.get("meal_date") or "", form.get("description") or "",
                                form.get("meal_type") or "dinner")
        if "error" in result:
            return self.meals_page(sess, start, err=result["error"])
        return self.meals_page(sess, start, info="saved")

    def meals_clear_post(self, sess: dict, form: dict):
        start = form.get("start") or ""
        meals.clear_meal(sess, form.get("meal_date") or "", form.get("meal_type") or "dinner")
        return self.meals_page(sess, start, info="cleared")

    def connect_get(self, sess: dict, provider: str):
        if provider not in connected_accounts.PROVIDERS:
            return self.not_found()
        if not PUBLIC_URL:
            return self.settings_page(sess, "accounts",
                                      err="NORI_PUBLIC_URL isn't set -- the operator needs to configure "
                                          "it before any OAuth connection can work (the exact callback "
                                          "URL has to be registered with the provider).")
        if not oauth.is_configured(provider):
            label = oauth.PROVIDERS.get(provider, {}).get("label", provider)
            return self.settings_page(sess, "accounts",
                                      err=f"{label} isn't configured on this instance yet -- the "
                                          f"operator needs to register an OAuth app with the provider "
                                          f"and supply the resulting credentials.")
        state = oauth.new_state()
        connected_accounts.start_pending(sess["user_id"], provider, state)
        redirect_uri = f"{PUBLIC_URL}/oauth/callback/{provider}"
        url = oauth.authorize_url(provider, state, redirect_uri)
        return self.send(303, b"", {"Location": url})

    def oauth_callback_get(self, sess: dict, provider: str, q: dict):
        code = (q.get("code") or [""])[0]
        state = (q.get("state") or [""])[0]
        if not connected_accounts.check_state(sess["user_id"], provider, state):
            return self.settings_page(sess, "accounts", err="OAuth state mismatch -- try connecting again.")
        if not code:
            return self.settings_page(sess, "accounts", err="the provider didn't return an authorization code.")
        redirect_uri = f"{PUBLIC_URL}/oauth/callback/{provider}"
        result = oauth.exchange_code(provider, code, redirect_uri)
        if not result.get("ok"):
            return self.settings_page(sess, "accounts", err=f"connection failed: {result.get('error')}")
        connected_accounts.store_tokens(sess["user_id"], provider, result["access_token"],
                                        result.get("refresh_token"), result.get("expires_in"))
        if provider == "google_calendar":
            # Auto-fill his timezone from the calendar's own reported zone
            # (2026-09-18, real bug: this used to not exist at all) --
            # only if he's never set usertime's timezone himself. One
            # cheap metadata call, not a full events fetch.
            cal = connected_accounts.authed_request(
                sess["user_id"], provider, "https://www.googleapis.com/calendar/v3/calendars/primary")
            if cal.get("ok"):
                usertime.maybe_set_from_calendar(sess["user_id"], cal["data"].get("timeZone"))
        return self.settings_page(sess, "accounts", info="connected")

    def disconnect_post(self, sess: dict, provider: str, form: dict):
        connected_accounts.disconnect(sess["user_id"], provider)
        return self.settings_page(sess, "accounts", info="disconnected")

    # -- working folder --
    # The human's own view over the SAME data workfiles.py's tools give her
    # -- upload/download/mkdir/delete are always the operator acting on
    # their own folder, so none of the provenance restrictions that bind
    # the tools (write_file/delete_file) apply here; see workfiles.py.
    @staticmethod
    def _parse_multipart(body: bytes, content_type: str) -> dict:
        """Just enough of multipart/form-data to pull named fields and one
        file upload out of a browser form POST -- Python 3.13 dropped the
        stdlib cgi module, and this app doesn't need a general-purpose
        parser. Returns {field_name: str} for plain fields and
        {field_name: (filename, bytes)} for a file field."""
        m = re.search(r'boundary="?([^";]+)"?', content_type)
        if not m:
            return {}
        boundary = ("--" + m.group(1)).encode()
        result: dict = {}
        for part in body.split(boundary):
            # Fixed-width trims only -- exactly one CRLF on each end, per
            # RFC 2046 framing. A `.strip(b"\r\n")` here (an earlier,
            # buggy version of this code had exactly that) removes a
            # variable-length RUN of \r/\n bytes instead of one fixed
            # pair, silently eating a real trailing newline that happens
            # to be the last byte of an uploaded text file's own content.
            # Caught by actually uploading a file ending in a real \n and
            # diffing it against the download -- one byte short.
            if part.startswith(b"\r\n"):
                part = part[2:]
            if part.endswith(b"\r\n"):
                part = part[:-2]
            if not part or part == b"--" or b"\r\n\r\n" not in part:
                continue
            headers, _, data = part.partition(b"\r\n\r\n")
            disp = headers.decode("utf-8", "replace")
            nm = re.search(r'name="([^"]*)"', disp)
            if not nm:
                continue
            fn = re.search(r'filename="([^"]*)"', disp)
            result[nm.group(1)] = (fn.group(1), data) if fn else data.decode("utf-8", "replace")
        return result

    def files_upload_post(self):
        sess = accounts.get_session(self.token())
        if sess is None:
            return self.send(303, b"", {"Location": "/login"})
        n = int(self.headers.get("Content-Length", 0) or 0)
        cap = int((workfiles.MAX_FILE_MB + 2) * 1024 * 1024)  # small overhead for the other form fields
        if n <= 0 or n > cap:
            self.rfile.read(min(max(n, 0), cap))  # drain what we safely can so the socket isn't left mid-body
            return self.files_page(sess, "", err=f"upload too large -- max {workfiles.MAX_FILE_MB:.0f} MB per file")
        body = self.rfile.read(n)
        fields = self._parse_multipart(body, self.headers.get("Content-Type", ""))
        if not self.csrf_ok(sess, {"csrf": fields.get("csrf", "")}):
            return self.send(403, page_simple("blocked", "<p>bad CSRF token — reload and try again</p>"))
        target_dir = fields.get("path") or ""
        upload = fields.get("file")
        if not isinstance(upload, tuple) or not upload[0]:
            return self.files_page(sess, target_dir, err="choose a file first")
        filename = upload[0].replace("\\", "/").rsplit("/", 1)[-1]  # strip any client-supplied directory part
        rel = f"{target_dir}/{filename}" if target_dir else filename
        result = workfiles.upload_file(sess, rel, upload[1])
        if "error" in result:
            return self.files_page(sess, target_dir, err=result["error"])
        return self.files_page(sess, target_dir, info=f"uploaded {esc(filename)}")

    def files_download(self, sess: dict, rel_path: str):
        target = workfiles.resolve_for_download(sess, rel_path)
        if target is None:
            return self.not_found()
        # Always a forced download, never rendered inline: a stored file is
        # untrusted content (see workfiles.py) -- serving it with its own
        # content-type inline under Nori's own origin would let an
        # uploaded .html/.svg execute script AS this app, a real stored-XSS
        # path against the session cookie. octet-stream + attachment closes
        # that regardless of what the file actually is.
        name = target.name.replace('"', "")
        return self.send(200, target.read_bytes(), ctype="application/octet-stream",
                         extra={"Content-Disposition": f'attachment; filename="{name}"'})

    def image_get(self, sess: dict, file_id: str):
        """A generated image (generate_image_selfie/imagine_image) OR a
        casual chat photo the user uploaded (2026-09-15) -- both live in
        the same GEN_DIR and share this one serving route. Safe unlike
        files_download's forced octet-stream, even for a user upload,
        because imagegen.store_chat_upload() already validated the real
        bytes against a strict jpeg/png/webp signature check before
        writing it here -- none of those formats can execute as script,
        unlike an arbitrary upload could (see workfiles.py's own
        forced-download posture for exactly that risk on a general file).
        Always served with a real image Content-Type (sniff_mime), never
        something a browser could execute regardless. Scoped to the
        caller's own workspace via media_log -- a household's images
        aren't public just because the file_id is unguessable."""
        row = store.read(lambda c: c.execute(
            "SELECT 1 FROM media_log WHERE file_id=? AND workspace_id=? AND ok=1",
            (file_id, sess["workspace_id"])).fetchone())
        if not row:
            return self.send(404, b"")
        p = imagegen.GEN_DIR / file_id
        if not p.is_file():
            return self.send(404, b"")
        data = p.read_bytes()
        return self.send(200, data, ctype=imagegen.sniff_mime(data[:16]),
                         extra={"Cache-Control": "private, max-age=86400"})

    def image_thumb_get(self, sess: dict, file_id: str):
        """Same authorization as image_get above (the same media_log
        check, not a looser one just because it's "only" a thumbnail) --
        this route is for /photos' grid, which loads a lot more tiles per
        page view than a single chat bubble ever does, not a reason to
        skip the ownership check. Generates on first request if the
        backfill script hasn't reached this file_id yet (or ran before it
        existed) -- imagegen.generate_thumbnail() is itself generate-once,
        so a second request for the same photo just reads the cached
        file. Falls back to serving the REAL original, not a broken tile
        or a 404, whenever a thumbnail can't be made at all (corrupt
        source, Pillow unavailable) -- graceful in both directions the
        operator asked for: missing thumbnail, and missing thumbnailing capability
        entirely."""
        row = store.read(lambda c: c.execute(
            "SELECT 1 FROM media_log WHERE file_id=? AND workspace_id=? AND ok=1",
            (file_id, sess["workspace_id"])).fetchone())
        if not row:
            return self.send(404, b"")
        if imagegen.generate_thumbnail(file_id):
            data = imagegen.thumb_path(file_id).read_bytes()
            return self.send(200, data, ctype="image/jpeg",
                             extra={"Cache-Control": "private, max-age=86400"})
        p = imagegen.GEN_DIR / file_id
        if not p.is_file():
            return self.send(404, b"")
        data = p.read_bytes()
        return self.send(200, data, ctype=imagegen.sniff_mime(data[:16]),
                         extra={"Cache-Control": "private, max-age=86400"})

    def files_mkdir_post(self, sess: dict, form: dict):
        target_dir = form.get("path") or ""
        name = (form.get("name") or "").strip()
        if not name:
            return self.files_page(sess, target_dir, err="name is required")
        rel = f"{target_dir}/{name}" if target_dir else name
        result = workfiles.create_folder(sess, rel)
        if "error" in result:
            return self.files_page(sess, target_dir, err=result["error"])
        return self.files_page(sess, target_dir, info=f"created {esc(name)}")

    def files_delete_post(self, sess: dict, form: dict):
        rel = form.get("path") or ""
        target_dir = rel.rsplit("/", 1)[0] if "/" in rel else ""
        result = workfiles.delete_file_ui(sess, rel)
        if "error" in result:
            return self.files_page(sess, target_dir, err=result["error"])
        return self.files_page(sess, target_dir, info="deleted")

    def files_vision_post(self, sess: dict, form: dict):
        config.set("user", sess["user_id"], "workfile_vision_enabled", "vision_enabled" in form)
        return self.files_page(sess, form.get("path") or "", info="saved")

    _DRIVE_DEFAULT_FOLDER = "Nori"

    def files_drive_post(self, sess: dict, form: dict):
        """Human-triggered only -- a UI action, never a tool (2026-09-18,
        operator's own explicit design: Drive write is his call to make,
        per file, not something Nori or a peer can invoke). move deletes
        the working-folder copy only after the Drive upload has actually
        succeeded -- never the other way around, so a failed upload can
        never lose the only copy of a file."""
        rel = form.get("path") or ""
        mode = form.get("mode") or "copy"
        target_dir = rel.rsplit("/", 1)[0] if "/" in rel else ""
        if mode not in ("move", "copy"):
            return self.files_page(sess, target_dir, err="mode must be move or copy")
        local_path = workfiles.resolve_for_download(sess, rel)
        if local_path is None:
            return self.files_page(sess, target_dir, err="no such file")
        folder_name = (form.get("folder") or "").strip() or self._DRIVE_DEFAULT_FOLDER
        folder = drive.find_or_create_folder(sess, folder_name)
        if "error" in folder:
            return self.files_page(sess, target_dir, err=folder["error"])
        content = local_path.read_bytes()
        mime_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
        result = drive.upload_bytes_to_drive(sess, name=local_path.name, content=content,
                                             mime_type=mime_type, folder_id=folder["folder_id"])
        if "error" in result:
            return self.files_page(sess, target_dir, err=result["error"])
        if mode == "move":
            workfiles.delete_file_ui(sess, rel)
        verb = "moved" if mode == "move" else "copied"
        return self.files_page(sess, target_dir,
                               info=f"{verb} {esc(local_path.name)} to Drive folder \"{esc(folder_name)}\"")

    def files_onedrive_post(self, sess: dict, form: dict):
        """Same shape and same reasoning as files_drive_post -- human-
        triggered only, never a tool. provider picks which of his two
        OneDrive accounts (work/personal) this specific move/copy targets."""
        rel = form.get("path") or ""
        mode = form.get("mode") or "copy"
        provider = form.get("provider") or "onedrive_work"
        target_dir = rel.rsplit("/", 1)[0] if "/" in rel else ""
        if mode not in ("move", "copy"):
            return self.files_page(sess, target_dir, err="mode must be move or copy")
        if provider not in ("onedrive_work", "onedrive_personal"):
            return self.files_page(sess, target_dir, err="provider must be onedrive_work or onedrive_personal")
        local_path = workfiles.resolve_for_download(sess, rel)
        if local_path is None:
            return self.files_page(sess, target_dir, err="no such file")
        folder_name = (form.get("folder") or "").strip() or self._DRIVE_DEFAULT_FOLDER
        folder = drive.find_or_create_folder_onedrive(sess, folder_name, provider=provider)
        if "error" in folder:
            return self.files_page(sess, target_dir, err=folder["error"])
        content = local_path.read_bytes()
        result = drive.upload_bytes_to_onedrive(sess, name=local_path.name, content=content,
                                                folder_id=folder["folder_id"], provider=provider)
        if "error" in result:
            return self.files_page(sess, target_dir, err=result["error"])
        if mode == "move":
            workfiles.delete_file_ui(sess, rel)
        verb = "moved" if mode == "move" else "copied"
        acct_label = oauth.PROVIDERS.get(provider, {}).get("label", provider)
        return self.files_page(sess, target_dir,
                               info=f"{verb} {esc(local_path.name)} to {esc(acct_label)} folder \"{esc(folder_name)}\"")

    def files_sharepoint_post(self, sess: dict, form: dict):
        """Same shape as files_drive_post -- human-triggered only, never a
        tool. site_id names which SharePoint site (of potentially every
        site in the tenant -- Sites.ReadWrite.All has no per-site
        boundary) this move/copy targets; he gets that id from her via
        list_sharepoint_sites."""
        rel = form.get("path") or ""
        mode = form.get("mode") or "copy"
        site_id = (form.get("site_id") or "").strip()
        target_dir = rel.rsplit("/", 1)[0] if "/" in rel else ""
        if mode not in ("move", "copy"):
            return self.files_page(sess, target_dir, err="mode must be move or copy")
        if not site_id:
            return self.files_page(sess, target_dir, err="a SharePoint site id is required")
        local_path = workfiles.resolve_for_download(sess, rel)
        if local_path is None:
            return self.files_page(sess, target_dir, err="no such file")
        folder_name = (form.get("folder") or "").strip() or self._DRIVE_DEFAULT_FOLDER
        folder = sharepoint.find_or_create_folder(sess, site_id, folder_name)
        if "error" in folder:
            return self.files_page(sess, target_dir, err=folder["error"])
        content = local_path.read_bytes()
        result = sharepoint.upload_bytes_to_sharepoint(sess, site_id=site_id, name=local_path.name,
                                                       content=content, folder_id=folder["folder_id"])
        if "error" in result:
            return self.files_page(sess, target_dir, err=result["error"])
        if mode == "move":
            workfiles.delete_file_ui(sess, rel)
        verb = "moved" if mode == "move" else "copied"
        return self.files_page(sess, target_dir,
                               info=f"{verb} {esc(local_path.name)} to SharePoint folder \"{esc(folder_name)}\"")

    # -- voice layer (2026-09-12): TTS playback + push-to-talk STT. Both
    # hit OpenAI directly (voice.py, OAI_API_KEY) -- see that module's own
    # docstring for why this isn't on OPENROUTER_API_KEY instead.
    def voice_tts_get(self, sess: dict, msg_id_raw: str):
        try:
            msg_id = int(msg_id_raw)
        except ValueError:
            return self.not_found()
        msg = conversation.get_own(sess["user_id"], msg_id)
        # Ownership-scoped (get_own, same check /retry uses) AND
        # role-scoped -- only ever speaks back something SHE said, never
        # reads the user's own message text back to them.
        if msg is None or msg["role"] != "assistant" or not (msg["content"] or "").strip():
            return self.not_found()
        voice_name = config.get("user", sess["user_id"], "tts_voice")
        # A separate request/response cycle from the turn that produced
        # this reply (voice mode fetches this only after the text lands),
        # so it gets its own small Turn rather than trying to nest inside
        # one that's already finished -- tagged with the message id so it
        # can still be correlated by eye if needed.
        turn = timing.start(sess["workspace_id"], "tts")
        try:
            with turn.stage("tts_generate", message_id=msg_id):
                audio = voice.tts_bytes(msg["content"], voice=voice_name,
                                        emotion_state=msg["emotion"] or emotion.DEFAULT_STATE)
        except voice.VoiceError:
            turn.finish()
            return self.send(502, b"", ctype="text/plain")
        turn.finish()
        return self.send(200, audio, ctype="audio/mpeg")

    def voice_stt_post(self):
        # Sub-stage timing (2026-09-13, a follow-up on the voice
        # latency report): splits what the coarse /voice/stt request-level
        # TIMING line used to bundle into one number -- receiving the
        # upload (network transfer from the browser plus multipart parse)
        # versus the actual STT API call -- so "is it the upload or the
        # model" has a real answer instead of one sample's worth of
        # guessing. audio_bytes rides on the stt_call stage since that's
        # the number that actually explains a slow STT call; upload_receive
        # already reports its own ms, which is the upload-speed signal.
        pt = getattr(self, "_timing_turn", timing.NULL_TURN)
        sess = accounts.get_session(self.token())
        if sess is None:
            return self.send_json({"ok": False, "error": "not signed in"}, 401)
        if not config.get("user", sess["user_id"], "voice_input_enabled"):
            return self.send_json({"ok": False, "error": "voice input isn't turned on -- enable it in Settings first"}, 403)
        n = int(self.headers.get("Content-Length", 0) or 0)
        cap = 15 * 1024 * 1024  # a push-to-talk clip; generous but not unbounded
        if n <= 0 or n > cap:
            self.rfile.read(min(max(n, 0), cap))
            return self.send_json({"ok": False, "error": "recording too large or empty"}, 400)
        with pt.stage("upload_receive", content_length=n):
            body = self.rfile.read(n)
            fields = self._parse_multipart(body, self.headers.get("Content-Type", ""))
        if not self.csrf_ok(sess, {"csrf": fields.get("csrf", "")}):
            return self.send_json({"ok": False, "error": "bad csrf token -- reload and try again"}, 403)
        upload = fields.get("file")
        if not isinstance(upload, tuple) or not upload[1]:
            return self.send_json({"ok": False, "error": "no recording received"}, 400)
        filename, audio = upload
        try:
            with pt.stage("stt_call", audio_bytes=len(audio)):
                text = voice.transcribe_bytes(audio, filename=filename or "speech.webm", content_type="")
        except voice.VoiceError as exc:
            return self.send_json({"ok": False, "error": str(exc)}, 502)
        return self.send_json({"ok": True, "text": text})

    def files_page(self, sess: dict, cur_path: str, err: str = "", info: str = ""):
        listing = workfiles.list_files(sess, cur_path)
        if "error" in listing:
            cur_path = ""
            listing = workfiles.list_files(sess, "")
        csrf = esc(sess["csrf"])
        cur = listing["path"]

        crumbs = ["<a href='/files'>files</a>"]
        acc = ""
        for part in [p for p in cur.split("/") if p]:
            acc = f"{acc}/{part}" if acc else part
            crumbs.append(f"<a href='/files?path={urllib.parse.quote(acc)}'>{esc(part)}</a>")
        # Drive/OneDrive/SharePoint move/copy (2026-09-18, operator's own
        # explicit design for Drive, extended the same way to OneDrive/
        # SharePoint: a UI action he triggers per file, never a tool Nori
        # or a peer can call). Each only shown once actually configured --
        # no point offering a control that can only ever fail.
        drive_configured = oauth.is_configured("google_drive")
        onedrive_work_configured = oauth.is_configured("onedrive_work")
        onedrive_personal_configured = oauth.is_configured("onedrive_personal")
        sharepoint_configured = oauth.is_configured("sharepoint")
        rows = []
        for e in listing["entries"]:
            qp = urllib.parse.quote(e["path"])
            if e["kind"] == "folder":
                rows.append(f"<div class=list-row><div class=list-icon>📁</div>"
                           f"<div class=list-meta><b><a href='/files?path={qp}'>{esc(e['name'])}</a></b>"
                           f"<small>folder</small></div>"
                           f"<div class=list-actions><form method=post action='/files/delete'>"
                           f"<input type=hidden name=csrf value='{csrf}'>"
                           f"<input type=hidden name=path value='{esc(e['path'])}'>"
                           f"<button class=icon-action aria-label=Delete>✕</button></form></div></div>")
            else:
                size_kb = max(1, e["size_bytes"] // 1024)
                drive_html = ""
                if drive_configured:
                    drive_html = (
                        "<details class=sched-history><summary>move/copy to Drive</summary>"
                        "<form method=post action='/files/drive' "
                        "style='display:flex;gap:.4rem;flex-wrap:wrap;margin-top:.4rem;align-items:center'>"
                        f"<input type=hidden name=csrf value='{csrf}'>"
                        f"<input type=hidden name=path value='{esc(e['path'])}'>"
                        "<select name=mode><option value=copy>copy (keep here too)</option>"
                        "<option value=move>move (remove from here)</option></select>"
                        f"<input type=text name=folder placeholder='Drive folder (default: {esc(self._DRIVE_DEFAULT_FOLDER)})' "
                        "style='flex:1;min-width:9em'>"
                        "<button class=btn>go</button></form></details>")
                onedrive_html = ""
                if onedrive_work_configured or onedrive_personal_configured:
                    acct_opts = "".join(
                        f"<option value={p}>{esc(oauth.PROVIDERS[p]['label'])}</option>"
                        for p, ok in (("onedrive_work", onedrive_work_configured),
                                     ("onedrive_personal", onedrive_personal_configured)) if ok)
                    onedrive_html = (
                        "<details class=sched-history><summary>move/copy to OneDrive</summary>"
                        "<form method=post action='/files/onedrive' "
                        "style='display:flex;gap:.4rem;flex-wrap:wrap;margin-top:.4rem;align-items:center'>"
                        f"<input type=hidden name=csrf value='{csrf}'>"
                        f"<input type=hidden name=path value='{esc(e['path'])}'>"
                        f"<select name=provider>{acct_opts}</select>"
                        "<select name=mode><option value=copy>copy (keep here too)</option>"
                        "<option value=move>move (remove from here)</option></select>"
                        f"<input type=text name=folder placeholder='OneDrive folder (default: {esc(self._DRIVE_DEFAULT_FOLDER)})' "
                        "style='flex:1;min-width:9em'>"
                        "<button class=btn>go</button></form></details>")
                sharepoint_html = ""
                if sharepoint_configured:
                    sharepoint_html = (
                        "<details class=sched-history><summary>move/copy to SharePoint</summary>"
                        "<form method=post action='/files/sharepoint' "
                        "style='display:flex;gap:.4rem;flex-wrap:wrap;margin-top:.4rem;align-items:center'>"
                        f"<input type=hidden name=csrf value='{csrf}'>"
                        f"<input type=hidden name=path value='{esc(e['path'])}'>"
                        "<select name=mode><option value=copy>copy (keep here too)</option>"
                        "<option value=move>move (remove from here)</option></select>"
                        "<input type=text name=site_id placeholder='SharePoint site id (ask her: "
                        "list_sharepoint_sites) ' required style='flex:1;min-width:14em'>"
                        f"<input type=text name=folder placeholder='folder (default: {esc(self._DRIVE_DEFAULT_FOLDER)})' "
                        "style='flex:1;min-width:9em'>"
                        "<button class=btn>go</button></form></details>")
                rows.append(f"<div class=list-row><div class=list-icon>📄</div>"
                           f"<div class=list-meta><b>{esc(e['name'])}</b>"
                           f"<small>{esc(e['created_by'] or 'user')} · {size_kb} KB</small>"
                           f"{drive_html}{onedrive_html}{sharepoint_html}</div>"
                           f"<div class=list-actions>"
                           f"<a class=icon-action href='/files/download/{qp}' aria-label=Download>⬇</a>"
                           f"<form method=post action='/files/delete'>"
                           f"<input type=hidden name=csrf value='{csrf}'>"
                           f"<input type=hidden name=path value='{esc(e['path'])}'>"
                           f"<button class=icon-action aria-label=Delete>✕</button></form></div></div>")
        list_html = "".join(rows) or "<p class=muted>empty.</p>"
        vision_on = config.get("user", sess["user_id"], "workfile_vision_enabled")
        e_html = f"<p class=err>{esc(err)}</p>" if err else ""
        i_html = f"<p class=info>{esc(info)}</p>" if info else ""
        main = (
            f"{e_html}{i_html}"
            f"<div class=section><p class=muted>{' / '.join(crumbs)}</p>"
            f"<p class=muted>{listing['usage_mb']:.1f} / {listing['limit_mb']:.0f} MB · "
            f"{listing['item_count']} / {listing['limit_count']} items</p>"
            f"{list_html}</div>"
            "<div class=section><h2>new folder</h2>"
            "<form method=post action='/files/mkdir' style='display:flex;gap:.5rem'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            f"<input type=hidden name=path value='{esc(cur)}'>"
            "<input type=text name=name placeholder='folder name' required>"
            "<button class=btn>create</button></form></div>"
            "<div class=section><h2>vision</h2>"
            "<form method=post action='/files/vision'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            f"<input type=hidden name=path value='{esc(cur)}'>"
            f"<label><input type=checkbox name=vision_enabled {'checked' if vision_on else ''}> "
            "let her look at images in here (sends them to a vision model)</label> "
            "<button class=btn>save</button></form></div>"
        )
        footer = (
            "<form method=post action='/files/upload' enctype='multipart/form-data' style='display:contents'>"
            f"<input type=hidden name=csrf value='{csrf}'>"
            f"<input type=hidden name=path value='{esc(cur)}'>"
            "<input type=file name=file required style='flex:1'>"
            "<button class='btn btn-primary'>upload</button></form>"
        )
        self.send(200, page_app("files", self._app_header(sess, self._hdr_back("Files")), main, footer))

    # -- avatars --
    @staticmethod
    def _find_avatar_file(state: str):
        """Checks extensions in _AVATAR_EXTS' declared order (png first) --
        that order IS the tie-break if an operator somehow has both a
        panic.png and a panic.svg, so it's centralized here once rather
        than implicitly duplicated anywhere else that needs to answer
        "is this state real or a placeholder." Returns (path, mime) or
        (None, None)."""
        for ext, mime in _AVATAR_EXTS.items():
            p = AVATAR_DIR / f"{state}.{ext}"
            if p.is_file():
                return p, mime
        return None, None

    def serve_avatar(self, state: str):
        """Public, no session needed -- these are emotion-word graphics,
        not user content. Serves a real file if the operator has dropped
        one in (see static/avatars/README.md); otherwise a generated
        placeholder. Swapping in real files later needs no other change.

        Resized once, reused forever (2026-09-16) -- imagegen.
        generate_thumb_for(), the exact same resize primitive the photo
        grid's own thumbnails use, not a second implementation. Falls
        back to the real, full-size original on any failure (Pillow
        missing, an unusual format it can't open) -- same graceful
        degradation the photo thumbnails already have, never a broken
        image."""
        if state not in emotion.STATES:
            return self.send(404, b"")
        p, mime = self._find_avatar_file(state)
        if p:
            thumb = AVATAR_THUMB_DIR / f"{state}.jpg"
            if imagegen.generate_thumb_for(p, thumb, AVATAR_MAX_DIM):
                return self.send(200, thumb.read_bytes(), ctype="image/jpeg",
                                 extra={"Cache-Control": "public, max-age=3600"})
            return self.send(200, p.read_bytes(), ctype=mime, extra={"Cache-Control": "public, max-age=3600"})
        svg = emotion.placeholder_svg(state).encode("utf-8")
        return self.send(200, svg, ctype="image/svg+xml", extra={"Cache-Control": "public, max-age=3600"})

    # -- PWA: manifest, service worker, icons. All public, no session --
    # see the do_GET routing comment for why that's actually correct here,
    # not just convenient (nothing below varies by user).
    def serve_manifest(self):
        wsid = accounts.the_workspace_id()
        name = config.get("workspace", wsid, "assistant_name") if wsid is not None else "Nori"
        m = {
            "name": name, "short_name": name, "id": "/",
            "start_url": "/", "scope": "/", "display": "standalone",
            # Not locked to "portrait" despite the mobile-first chat UI --
            # Nori also has a real desktop 3-column layout (the hero
            # panel + conversation + board) that a landscape-oriented
            # installed window should be free to use. "any" is already
            # the spec default when omitted; written out so it reads as
            # a considered choice, not a gap.
            "orientation": "any",
            "background_color": "#0b0c10", "theme_color": "#15171c",
            "icons": [
                {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
            ],
        }
        self.send(200, json.dumps(m).encode(), {"Cache-Control": "no-cache"},
                 ctype="application/manifest+json")

    def serve_sw(self):
        self.send(200, _SW_JS.encode(), {"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
                 ctype="application/javascript")

    # Public, no session -- same reasoning as the PWA shell routes above:
    # static, doesn't vary by user, and the emoji picker's own JS (CHAT_JS)
    # only fetches this the first time someone actually opens it, not on
    # every page load. Long, immutable cache -- the filename's own version
    # suffix is how this gets busted, see _EMOJI_DATA_JSON's own comment.
    def serve_emoji_data(self):
        self.send(200, _EMOJI_DATA_JSON, {"Cache-Control": "public, max-age=2592000, immutable"},
                 ctype="application/json")

    def serve_pwa_icon(self, filename: str):
        p = PWA_DIR / filename
        if not p.is_file():
            return self.not_found()
        self.send(200, p.read_bytes(), ctype="image/png", extra={"Cache-Control": "public, max-age=86400"})

    def avatars_admin_page(self, sess: dict, err: str = "", info: str = ""):
        """Exactly the check you want when you've just dropped in a batch
        of files and don't want to hope: every configured state, whether
        it's actually resolving to a real file or silently falling back to
        a placeholder, and which file if so -- read fresh off disk on every
        load, not cached, so it reflects whatever's in static/avatars/ at
        this exact moment."""
        if sess["role"] != "admin":
            return self.forbidden()
        wsid = sess["workspace_id"]
        e = f"<p class=err>{esc(err)}</p>" if err else ""
        i = f"<p class=info>{esc(info)}</p>" if info else ""
        name_form = (
            "<div class=section><h2>her name</h2>"
            "<form method=post action='/admin/avatars'>"
            f"<input type=hidden name=csrf value='{esc(sess['csrf'])}'>"
            "<div class=field><label>what she's called on this instance</label>"
            f"<input type=text name=assistant_name maxlength=60 required "
            f"value='{esc(str(config.get('workspace', wsid, 'assistant_name')))}'></div>"
            "<p class=muted>Changes what she calls herself, her chat header, notifications, and how "
            "she introduces herself to another PACI-speaking assistant she's connected to. The product "
            "and this harness stay Nori regardless -- this is just what YOUR instance goes by.</p>"
            "<button class='btn btn-primary'>save</button></form></div>")
        emo_on = config.get("workspace", wsid, "emotion_enabled")
        toggle = (
            "<div class=section><h2>her visible emotional state</h2>"
            "<form method=post action='/admin/avatars'>"
            f"<input type=hidden name=csrf value='{esc(sess['csrf'])}'>"
            "<div class=field><label>on/off</label>"
            f"<select name=emotion_enabled><option value=1{' selected' if emo_on else ''}>on</option>"
            f"<option value=0{' selected' if not emo_on else ''}>off</option></select></div>"
            "<p class=muted>Off: the avatar always shows neutral, set_emotion is not offered to her at "
            "all, and the per-turn reminder to reconsider her state stops appearing -- nothing "
            "emotion-related reaches her context, the UI, or the turn.</p>"
            "<button class='btn btn-primary'>save</button></form></div>")
        rows = []
        real_count = 0
        for state in emotion.STATES:
            p, _mime = self._find_avatar_file(state)
            if p:
                real_count += 1
                status = f"<span style='color:var(--k-task)'>real file — {esc(p.name)}</span>"
            else:
                status = "<span style='color:var(--danger)'>PLACEHOLDER — no file matched</span>"
            rows.append(
                f"<div class=list-row><img src='/avatar/{state}?cb={time.time()}' width=40 height=40 "
                f"style='border-radius:50%;flex-shrink:0;object-fit:cover'>"
                f"<div class=list-meta><b>{esc(state)}</b><small>{status}</small></div></div>")
        main = (
            e + i + name_form + toggle +
            f"<p class=info>{real_count} / {len(emotion.STATES)} states have a real file; "
            f"the rest are showing the generated placeholder.</p>"
            f"<p class=muted>Checked directly against {esc(str(AVATAR_DIR))} on this exact "
            f"page load -- reload after adding files, no restart needed for an existing state name. "
            f"A NEW state name (not yet in emotions.json) won't appear here until it's added there "
            f"and the server is restarted.</p>"
            f"<div class=section>{''.join(rows)}</div>"
        )
        self._settings_response(sess, "avatars", main)

    def avatars_admin_post(self, sess: dict, form: dict):
        if sess["role"] != "admin":
            return self.forbidden()
        wsid = sess["workspace_id"]
        for key in ("assistant_name", "emotion_enabled"):
            val = form.get(key)
            if isinstance(val, str):
                try:
                    config.set("workspace", wsid, key, val)
                except (KeyError, ValueError) as exc:
                    return self.avatars_admin_page(sess, err=str(exc))
        return self.avatars_admin_page(sess, info="saved")

    @staticmethod
    def _msg_marker(state: str) -> str:
        """Per-message historical marker: a dot in that state's configured
        color plus a text label. Not an image -- scrolling back through a
        long conversation doesn't need 24 repeated portraits, and a color
        already exists for every state (emotions.json) even before an
        avatar file does."""
        s = state if state in emotion.STATES else emotion.DEFAULT_STATE
        color = emotion.COLORS.get(s, "#95a5a6")
        return f"<div class=msg-meta><i style='background:{esc(color)}'></i>{esc(s)}</div>"

    def _hero_panel(self, current_state: str) -> str:
        """Desktop only (see .hero's media query) -- the persistent, large
        avatar presence. Whole-portrait, never cropped to a circle
        (object-fit:contain -- that crop stays reserved for the small
        admin-grid treatment, a different job at a different size). Only
        the current image is rendered: transparent portraits must never
        have a previous emotion's portrait underneath them."""
        cur = current_state if current_state in emotion.STATES else emotion.DEFAULT_STATE
        layers = f"<img class='hero-img hero-img-cur' src='/avatar/{cur}' alt='{esc(cur)}'>"
        return f"<div class=hero><div class=hero-frame>{layers}<div class=hero-cap>{esc(cur)}</div></div></div>"

    def _peek_panel(self, current_state: str) -> str:
        """Mobile only (see .peek's desktop media query) -- the transient
        full-portrait overlay, triggered by tapping the header avatar chip
        or, automatically and briefly, by a real state change (see APP_JS's
        window.__noriStateChanged__ check)."""
        cur = current_state if current_state in emotion.STATES else emotion.DEFAULT_STATE
        return (f"<button type=button class=peek id=peek aria-label='Close avatar preview'>"
                f"<img src='/avatar/{cur}' alt='Nori: {esc(cur)}'>"
                f"<span class=peek-cap><b>{esc(cur)}</b><span>tap anywhere to close</span></span></button>")

    def _voice_modal(self, current_state: str) -> str:
        """Voice conversation mode (2026-09-13) -- a full-screen, immersive
        replacement for the old per-message play button + inline hold-to-
        record mic. Opened by tapping the composer's mic button; the JS
        side (CHAT_JS) owns every bit of its behavior -- this just renders
        the static shell (avatar, transcript region, status line, and the
        two controls) once per page load. Rendered even when
        voice_input_enabled is off (cheap, inert markup) so the toggle can
        be flipped on without a page reload adding it in.

        Reuses the push-to-talk PRESS itself to unlock audio playback for
        this page (see CHAT_JS's vUnlock) -- every spoken turn starts with
        a real tap, which is exactly the user gesture autoplay policies
        require, so unlike the old opt-in tts_autoplay setting this never
        needs to guess whether a browser will allow it."""
        cur = current_state if current_state in emotion.STATES else emotion.DEFAULT_STATE
        return (
            "<div class=voice-modal id=voiceModal aria-hidden=true>"
            "<div class=voice-modal-inner>"
            "<button type=button class=voice-close id=voiceClose aria-label='Close voice mode'>✕</button>"
            "<div class=voice-avatar-wrap>"
            f"<img class=voice-avatar id=voiceAvatar src='/avatar/{cur}' alt='{esc(cur)}'>"
            f"<div class=voice-state-label id=voiceStateLabel>{esc(cur)}</div>"
            "</div>"
            "<div class=voice-transcript id=voiceTranscript></div>"
            "<div class=voice-status id=voiceStatus aria-live=polite></div>"
            "<div class=voice-controls>"
            "<button type=button class=voice-stop-btn id=voiceStopBtn hidden aria-label='Stop playback'>⏹</button>"
            "<button type=button class=voice-mic-btn id=voiceMicBtn aria-label='Hold to talk'>\U0001f399</button>"
            "</div></div></div>"
        )

    # -- full-screen image viewer (2026-09-15, operator's own ask) -- one
    # modal, shared by the chat page's own bubbles and /photos' tiles
    # (IMAGE_VIEWER_JS below opens it from any `.chatimage` button on the
    # page, wherever it lives), rather than two viewers to keep in sync.
    # Ported from a sibling application's identical <dialog>-based one: native
    # showModal()/close(), a click ANYWHERE on the dialog closes it
    # (including its own content -- there is no separate "safe zone" to
    # miss), and native Escape support for free. That native click-to-
    # close is also what rules out the mobile trap the operator flagged --
    # there's no click target on the open dialog that does nothing.
    @staticmethod
    def _image_viewer_html() -> str:
        # imageviewerprompt (2026-09-16, operator's own ask -- was a big
        # box overlaying the photo, now collapsed by default): a small
        # bottom-left indicator when there's a prompt/caption at all
        # (still gated on data-prompt being present, same as before --
        # never shown on a plain uploaded photo with nothing to say), .iv-
        # text holding the real content, only shown once tapped open. See
        # IMAGE_VIEWER_JS for why this specific element's own clicks
        # don't fall through to the dialog's click-anywhere-closes.
        return ("<dialog id=imageviewer aria-label='Full screen image' aria-describedby=imageviewerhint>"
               "<img alt=''><button type=button id=imageviewerprompt hidden aria-expanded=false>"
               "<span class=iv-badge>ⓘ caption</span><span class=iv-text></span></button>"
               "<button type=button id=imageviewerclose aria-label='Close image' autofocus>&times;</button>"
               "<button type=button id=imageviewerprev aria-label='Previous image' hidden>&#8249;</button><button type=button id=imageviewernext aria-label='Next image' hidden>&#8250;</button>"
               "<p id=imageviewerhint>tap anywhere to close</p></dialog>")

    # -- note viewer (2026-09-17, his own ask) -- the notes stack's own
    # tap target. Unlike #imageviewer, real content (a possibly long
    # body) and real controls (prev/next/edit) live inside it, so a
    # click anywhere does NOT close it -- only a genuine backdrop click
    # does (NOTE_VIEWER_JS checks e.target===the dialog itself, which a
    # click on any of its own children, covering the whole card, never
    # satisfies -- no stopPropagation() needed on the buttons at all).
    @staticmethod
    def _note_viewer_html() -> str:
        return (
            "<dialog id=noteviewer aria-label=Note>"
            "<div class=nv-head><span class=nv-head-kind>note "
            "<span class='card-cat' id=noteviewercat></span></span>"
            "<button type=button id=noteviewerclose aria-label='Close note'>&times;</button></div>"
            "<div class=nv-body><h2 id=noteviewertitle></h2>"
            "<p id=noteviewercreator></p>"
            "<div id=noteviewertext></div></div>"
            "<div class=nv-foot>"
            "<button type=button class=nv-nav id=noteviewerprev aria-label='Previous note'>‹</button>"
            "<a class=btn id=notevieweredit href='#'>edit</a>"
            "<button type=button class=nv-nav id=noteviewernext aria-label='Next note'>›</button>"
            "</div></dialog>")

    # -- chat --
    def chat_page(self, sess: dict):
        # Page-render breakdown (2026-09-13) -- reuses the SAME Turn
        # do_GET's own request-level timer already created (stashed on
        # self._timing_turn), so this shows up as nested stages inside
        # that one request's own TIMING line, not a second one. A page
        # that queries the database a dozen times shows up as a slow
        # "fetch_messages"/"render" stage, not just a slow request overall
        # -- a different fix (fewer queries) than a slow model call would
        # ever point at.
        pt = getattr(self, "_timing_turn", timing.NULL_TURN)
        user = accounts.get_user(sess["user_id"])
        sess["_name"] = user["display_name"]
        current_state = emotion.get_state(sess["user_id"])
        show_tools = config.get("user", sess["user_id"], "show_tool_calls")
        with pt.stage("fetch_messages"):
            msgs = conversation.recent(sess["user_id"], include_tool=show_tools)

        def _copy_btn(text: str) -> str:
            # Assistant-only, real text only (2026-09-15, operator's own
            # ask) -- the raw text lives in data-copy, HTML-attribute-
            # escaped same as data-prompt already is just below; the
            # shared click handler (CHAT_JS) reads it back via
            # btn.dataset.copy, never re-scraping the bubble's own
            # rendered content, so this works identically whether the
            # bubble came from this server render or addMessage()'s live
            # one.
            return (f"<button type=button class=copy-btn aria-label='Copy message' "
                   f"data-copy='{esc(text)}'>\U0001f4cb</button>")

        # Tool-call runs collapse onto one line (2026-09-18, operator's own
        # ask -- a turn that chains several tool calls used to leave one
        # .toolline per call, which reads as clutter once a turn chains
        # more than one or two). Only CONSECUTIVE tool-kind rows merge --
        # a real message in between starts a new run, since that message
        # is what those calls led to and shouldn't be visually folded in
        # with whatever comes after it. Names, not just a count, stay on
        # the line -- he still wants to know what ran -- capped at
        # _TOOL_RUN_SHOW_MAX names before falling back to "and N more" so
        # a long chain can't make the line itself unreadable.
        _TOOL_RUN_SHOW_MAX = 5

        def _tool_run_line(names: list) -> str:
            if len(names) <= _TOOL_RUN_SHOW_MAX:
                return "used " + ", ".join(names)
            shown = ", ".join(names[:_TOOL_RUN_SHOW_MAX])
            return f"used {shown} and {len(names) - _TOOL_RUN_SHOW_MAX} more"

        def _group_tool_runs(rows: list) -> list:
            grouped, run = [], []
            for m in rows:
                if m["kind"] == "tool":
                    content = m["content"] or ""
                    run.append(content[5:] if content.startswith("used ") else content)
                    continue
                if run:
                    grouped.append({"kind": "tool", "content": _tool_run_line(run)})
                    run = []
                grouped.append(m)
            if run:
                grouped.append({"kind": "tool", "content": _tool_run_line(run)})
            return grouped

        def _bubble(m):
            if m["kind"] == "tool":
                return f"<div class=toolline>{esc(m['content'])}</div>"
            marker = self._msg_marker(m["emotion"]) if m.get("emotion") else ""
            if m["kind"] == "image":
                meta = json.loads(m["meta"]) if m.get("meta") else {}
                fid = esc(str(meta.get("file_id", "")))
                cap = f"<div class=msg-caption>{esc(m['content'])}</div>" if m.get("content") else ""
                copy_btn = _copy_btn(m["content"]) if m["role"] == "assistant" and m.get("content") else ""
                # prompt (2026-09-15) -- present only on a message SHE
                # generated (generate_image_selfie/imagine_image write it
                # into meta at creation); absent on anything he uploaded.
                # That absence is the whole gate -- no separate check
                # needed for "is this a generated image."
                prompt = meta.get("prompt")
                prompt_attr = f" data-prompt='{esc(prompt)}'" if prompt else ""
                return (f"{marker}<div id='message-{m['id']}' class='msg {esc(m['role'])} msg-image'>"
                       f"<button type=button class=chatimage aria-label='View image full screen'{prompt_attr}>"
                       f"<img src='/image/{fid}' alt='' loading=lazy></button>{cap}{copy_btn}</div>")
            copy_btn = _copy_btn(m["content"]) if m["role"] == "assistant" and m.get("content") else ""
            debug_btn = ""
            if m["role"] == "assistant" and m.get("meta"):
                try:
                    debug_btn = _debug_btn(json.loads(m["meta"]).get("debug"))
                except (ValueError, TypeError):
                    pass
            actions = f"<span class=msg-actions>{copy_btn}{debug_btn}</span>" if copy_btn or debug_btn else ""
            return (f"{marker}<div id='message-{m['id']}' class='msg {esc(m['role'])}'>"
                   f"{esc(m['content'])}{actions}</div>")

        # the state shown on the LAST page load, before whatever just
        # happened (a new reply, or nothing if this is a plain reload) --
        # the peek announces a change to current_state. Now ALSO
        # (see CHAT_JS's applyState) triggered live the moment a send/poll
        # response reports a change -- this page-load path stays as the
        # one that covers "you just opened the tab after she pinged you
        # while you were away," which a live update obviously can't have
        # caught.
        assistant_states = [m["emotion"] for m in msgs if m["role"] == "assistant" and m.get("emotion")]
        prev_state = assistant_states[-2] if len(assistant_states) >= 2 else current_state
        state_changed = prev_state != current_state
        last_id = msgs[-1]["id"] if msgs else 0

        bubbles = "".join(_bubble(m) for m in _group_tool_runs(msgs)) or "<p class=muted>say something to get started.</p>"
        color = emotion.COLORS.get(current_state, "#95a5a6")
        assistant_name = config.get("workspace", sess["workspace_id"], "assistant_name")
        hdr_left = (f"<div class=hdr-left><button type=button class=avatar-chip id=avatarChip aria-label='View {esc(assistant_name)} avatar'>"
                   f"<img src='/avatar/{esc(current_state)}' alt=''></button>"
                   f"<div class=hdr-text><div class=hdr-name>{esc(assistant_name)}</div>"
                   f"<div class=hdr-state><i style='background:{esc(color)}'></i>"
                   f"<span>{esc(current_state)}</span></div>"
                   "</div></div>")
        header = self._app_header(sess, hdr_left, board_button=True)
        header += ("<nav class=chat-actions aria-label='Chat quick actions'>"
                   "<a href='/inventory'>Inventory</a><a href='/meals'>Meals</a>"
                   "<a href='/trackers'>Trackers</a><a href='/files'>Files</a><a href='/history'>History</a>"
                   "<a href='/photos'>Photos</a><a href='/settings'>Settings</a></nav>")
        # Safe against a stray "</script>" inside a state name or the csrf
        # token (neither can actually contain one, but this costs nothing
        # and every other piece of embedded JSON in this app does the same).
        chat_cfg = json.dumps({"csrf": sess["csrf"], "state": current_state, "last_id": last_id,
                              "colors": emotion.COLORS, "assistant_name": assistant_name}).replace("</", "<\\/")
        notify_cfg = json.dumps({
            "enabled": bool(config.get("user", sess["user_id"], "notify_enabled")),
            "quiet_start": config.get("user", sess["user_id"], "notify_quiet_start"),
            "quiet_end": config.get("user", sess["user_id"], "notify_quiet_end"),
        }).replace("</", "<\\/")
        main = (
            self._hero_panel(current_state) +
            "<div class=chat-col>"
            f"<div class=msglist id=msglist>{bubbles}</div>"
            "<button id=scrollbtn type=button hidden aria-label='Scroll to latest'>↓</button>"
            "</div>" +
            self._peek_panel(current_state) +
            self._board_panel(sess) +
            self._voice_modal(current_state) +
            self._image_viewer_html() +
            self._note_viewer_html() +
            f"<script>window.__noriStateChanged__={'true' if state_changed else 'false'};"
            f"window.NORI_CHAT={chat_cfg};window.NOTIFY={notify_cfg};</script>"
        )
        # Voice conversation mode -- only rendered at all if the per-user
        # toggle (off by default, see the "pings" settings tab) is on.
        # Hidden rather than disabled when off: no dead control sitting in
        # the composer for a household member who never turned this on.
        # Tapping it opens the modal (see CHAT_JS) -- it no longer records
        # anything itself.
        mic_btn = ""
        if config.get("user", sess["user_id"], "voice_input_enabled"):
            mic_btn = f"<button id=micBtn type=button class=mic-btn aria-label='Talk to {esc(assistant_name)}'>\U0001f399</button>"
        footer = (
            "<form id=composerForm method=post action='/send' style='display:contents'>"
            "<input type=hidden name=csrf value='" + esc(sess["csrf"]) + "'>"
            "<div class=composer-attach id=composerAttach hidden>"
            "<img id=composerAttachThumb alt=''>"
            "<button type=button id=composerAttachRemove aria-label='Remove photo'>&times;</button></div>"
            f"{mic_btn}"
            "<button type=button id=attachBtn class=mic-btn aria-label='Attach a photo'>\U0001f4ce</button>"
            "<input type=file id=fileInput accept='image/jpeg,image/png,image/webp' style='display:none'>"
            "<button type=button id=emojiBtn class=emoji-btn aria-label='Insert emoji'>\U0001f642</button>"
            "<textarea id=msgInput name=text placeholder='message nori' required rows=1 autocomplete=off></textarea>"
            "<button id=sendBtn class=send-btn type=submit aria-label=Send>➤</button></form>"
            "<div class=emoji-panel id=emojiPanel>"
            "<div class=emoji-panel-head>"
            "<input type=text id=emojiSearchInput placeholder='Search emoji' autocomplete=off></div>"
            "<div class=emoji-tabs id=emojiTabs>"
            "<button type=button aria-label='Smileys &amp; Emotion'>\U0001f642</button>"
            "<button type=button aria-label='People &amp; Body'>\U0001f9d1</button>"
            "<button type=button aria-label='Animals &amp; Nature'>\U0001f43b</button>"
            "<button type=button aria-label='Food &amp; Drink'>\U0001f354</button>"
            "<button type=button aria-label=Activities>⚽</button>"
            "<button type=button aria-label='Travel &amp; Places'>✈️</button>"
            "<button type=button aria-label=Objects>\U0001f4a1</button>"
            "<button type=button aria-label=Symbols>❤️</button>"
            "<button type=button aria-label=Flags>\U0001f3f3️</button></div>"
            "<div class=emoji-grid id=emojiGrid></div></div>"
        )
        with pt.stage("render"):
            self.send(200, page_app("nori", header, main, footer, split_main=True,
                                    extra_js=CHAT_JS + IMAGE_VIEWER_JS + IMAGE_GALLERY_JS + NOTE_VIEWER_JS, chat_header=True))

    # -- full history (2026-09-14, operator's own ask): everything a
    # message actually carries all day -- timestamps, kind, emotion,
    # voice source, per-turn cost, the working-memory reason -- none of
    # it curated the way chat_page's DEFAULT_WINDOW/include_tool split
    # is. Paged, newest first by default. Search lands you at that point
    # in the real timeline (conversation.history_page's center_id path),
    # never a bare filtered list -- see conversation.py's own module
    # comment for why that distinction matters.
    def history_page(self, sess: dict, q: dict):
        user_id = sess["user_id"]

        def _int_param(name):
            v = q.get(name, [""])[0]
            try:
                return int(v) if v else None
            except ValueError:
                return None

        query = (q.get("q", [""])[0] or "").strip()
        hours_raw = q.get("hours_ago", [""])[0]
        try:
            hours_ago = float(hours_raw) if hours_raw else None
        except ValueError:
            hours_ago = None
        before_id, after_id, jump_id = _int_param("before"), _int_param("after"), _int_param("jump")

        matches: list[int] = []
        searching = bool(query) or hours_ago is not None
        if searching:
            matches = conversation.history_match_ids(user_id, query=query or None, hours_ago=hours_ago)
            if jump_id is None:
                jump_id = matches[0] if matches else None

        msgs = conversation.history_page(user_id, before_id=before_id, after_id=after_id, center_id=jump_id)
        newest_overall, oldest_overall = conversation.max_id(user_id), conversation.min_id(user_id)
        at_latest = bool(msgs) and msgs[0]["id"] == newest_overall
        at_oldest = bool(msgs) and msgs[-1]["id"] == oldest_overall
        rows = "".join(self._history_row(m, highlight_id=jump_id, user_id=user_id) for m in msgs) \
            or "<p class=muted>nothing here yet.</p>"

        match_nav = ""
        if searching:
            if not matches:
                match_nav = "<p class=muted>no matches.</p>"
            else:
                pos = matches.index(jump_id) + 1 if jump_id in matches else 1
                older_match = matches[pos] if pos < len(matches) else None  # matches is newest-first
                newer_match = matches[pos - 2] if pos >= 2 else None
                bits = [f"<span class=muted>match {pos} of {len(matches)}</span>"]
                if newer_match is not None:
                    bits.append(f"<a href='/history?{self._history_qs(q, jump=newer_match)}'>&uarr; newer match</a>")
                if older_match is not None:
                    bits.append(f"<a href='/history?{self._history_qs(q, jump=older_match)}'>&darr; older match</a>")
                match_nav = f"<div class=hist-matchnav>{''.join(bits)}</div>"

        page_nav = "<div class=hist-pagenav>"
        if msgs and not at_latest:
            page_nav += f"<a href='/history?after={msgs[0]['id']}'>&uarr; newer</a>"
        if msgs and not at_oldest:
            page_nav += f"<a href='/history?before={msgs[-1]['id']}'>&darr; older</a>"
        page_nav += "</div>"

        search_form = (
            "<form method=get action='/history' class=hist-search>"
            f"<input type=search name=q value='{esc(query)}' placeholder='search the real conversation…'>"
            f"<input type=number name=hours_ago value='{esc(str(int(hours_ago)) if hours_ago else '')}' "
            "placeholder='hours ago' style='width:6em'>"
            "<button class=btn type=submit>jump</button></form>")

        # _hdr_back("History"), not the bare hdr-text div this used to
        # have -- found during the design pass sweep (2026-09-18):
        # /history had no back chevron at all, the one real gap "every
        # page without exception" actually caught.
        header = self._app_header(sess, self._hdr_back("History"))
        main = (f"<div class=hist-wrap><div class=hist-sticky>{search_form}{match_nav}</div>"
               f"<div class=hist-list>{rows}</div>{page_nav}</div>")
        self.send(200, page_app("history · nori", header, main, extra_js=HISTORY_JUMP_JS))

    def _history_qs(self, q: dict, **overrides) -> str:
        """Rebuilds the current query string with one or more params
        replaced -- used by match-nav links so jumping between hits keeps
        the same active search active instead of losing it."""
        merged = {k: v[0] for k, v in q.items() if v and v[0]}
        merged.update({k: str(v) for k, v in overrides.items()})
        return urllib.parse.urlencode(merged)

    def _history_row(self, m: dict, *, highlight_id: int | None, user_id: int) -> str:
        try:
            meta = json.loads(m["meta"]) if m.get("meta") else {}
        except (ValueError, TypeError):
            meta = {}
        kind_label = {"peer_proactive": "peer", "job_proactive": "job"}.get(m["kind"], m["kind"])
        badges = [f"<span class='hist-badge hist-kind-{esc(m['kind'])}'>{esc(kind_label)}</span>"]
        if m.get("emotion"):
            color = emotion.COLORS.get(m["emotion"], "#95a5a6")
            badges.append(f"<span class=hist-badge style='background:{esc(color)}22;color:{esc(color)}'>"
                         f"{esc(m['emotion'])}</span>")
        if meta.get("source") == "voice":
            badges.append("<span class=hist-badge>\U0001f399 voice</span>")
        if "cost_usd" in meta:
            cost_txt = "cost n/a" if meta.get("cost_unavailable") else f"${meta['cost_usd']:.4f}"
            badges.append(f"<span class=hist-badge>{esc(cost_txt)}</span>")
        reason = meta.get("reason")
        reason_html = f"<div class=hist-reason>because: {esc(reason)}</div>" if reason else ""
        ts_txt = usertime.fmt(user_id, m["ts"], "%b %d, %Y %H:%M:%S")
        hl = " hist-hit" if highlight_id is not None and m["id"] == highlight_id else ""
        if m["kind"] == "image":
            fid = esc(str(meta.get("file_id", "")))
            content_html = f"<img src='/image/{fid}' alt='' loading=lazy style='max-width:16rem;border-radius:8px'>"
            if m.get("content"):
                content_html += f"<div class=msg-caption>{esc(m['content'])}</div>"
        else:
            content_html = esc(m['content'])
        return (f"<div id='hist-{m['id']}' class='hist-row {esc(m['role'])}{hl}'>"
               f"<div class=hist-meta><span class=hist-ts>{esc(ts_txt)}</span>{''.join(badges)}</div>"
               f"<div class=hist-content>{content_html}</div>{reason_html}</div>")

    # -- photo reel (2026-09-15, operator's own ask) -- every image ever
    # sent in chat, reusing history_page's own cursor exactly (kind='image'
    # is the only difference) rather than a second paging mechanism. A
    # plain grid on an ordinary (non-split) page_app shell, deliberately
    # NOT reusing .msglist -- see that class's own comment on the scroll
    # trap a fixed shell + an inner overflow:auto container produces on a
    # long page; .photo-grid has no overflow property of its own, so
    # .shell-main stays the one real scroll container, same as /history.
    def photos_page(self, sess: dict, q: dict):
        user_id = sess["user_id"]

        def _int_param(name):
            v = q.get(name, [""])[0]
            try:
                return int(v) if v else None
            except ValueError:
                return None

        before_id, after_id = _int_param("before"), _int_param("after")
        msgs = conversation.history_page(user_id, before_id=before_id, after_id=after_id,
                                         kind="image", limit=24)
        has_newer = bool(msgs) and bool(conversation.history_page(
            user_id, after_id=msgs[0]["id"], kind="image", limit=1))
        has_older = bool(msgs) and bool(conversation.history_page(
            user_id, before_id=msgs[-1]["id"], kind="image", limit=1))
        tiles = "".join(self._photo_tile(m) for m in msgs) or "<p class=muted>no photos yet.</p>"

        page_nav = "<div class=hist-pagenav>"
        if has_newer:
            page_nav += f"<a href='/photos?after={msgs[0]['id']}'>&uarr; newer</a>"
        if has_older:
            page_nav += f"<a href='/photos?before={msgs[-1]['id']}'>&darr; older</a>"
        page_nav += "</div>"

        # Same missing-chevron gap history_page had -- fixed the same way.
        header = self._app_header(sess, self._hdr_back("Photos"))
        main = (f"<div class=hist-wrap><div class=photo-grid>{tiles}</div>{page_nav}</div>"
               + self._image_viewer_html())
        self.send(200, page_app("photos · nori", header, main, extra_js=IMAGE_VIEWER_JS + IMAGE_GALLERY_JS))

    def _photo_tile(self, m: dict) -> str:
        try:
            meta = json.loads(m["meta"]) if m.get("meta") else {}
        except (ValueError, TypeError):
            meta = {}
        fid = esc(str(meta.get("file_id", "")))
        ts_txt = time.strftime("%b %d, %Y %H:%M", time.localtime(m["ts"]))
        cap = f"<figcaption>{esc(m['content'])}</figcaption>" if m.get("content") else ""
        # Same chatimage button the chat page uses (2026-09-15) -- one
        # modal, opened from both entry points, not two viewers to keep
        # in sync. prompt gate: see _bubble()'s own comment.
        prompt = meta.get("prompt")
        prompt_attr = f" data-prompt='{esc(prompt)}'" if prompt else ""
        # /photos' own grid tile loads the THUMBNAIL (image-thumb/); the
        # button carries data-full so the modal (IMAGE_VIEWER_JS) still
        # opens the real original -- see that script's own comment. Every
        # other .chatimage user (chat bubbles) has no data-full and no
        # thumbnail route involved at all, unchanged.
        return (f"<figure class=photo-tile>"
               f"<button type=button class=chatimage aria-label='View photo full screen'{prompt_attr} "
               f"data-full='/image/{fid}'>"
               f"<img src='/image-thumb/{fid}' alt='' loading=lazy></button>"
               f"{cap}<span class=photo-ts>{esc(ts_txt)}</span></figure>")

    def _run_turn(self, sess: dict, user: dict, *, voice: bool = False,
                 extra_message: dict | None = None) -> dict:
        """Answers whatever the newest message for this user currently is
        -- chat.run() always works off the live conversation window, so by
        the time this runs (a fresh send, or a turns.py sweep answering a
        message that arrived mid-turn) the right thing to answer is
        already the latest row conversation.recent() returns. Never stores
        a fake reply on failure -- an honest {"ok": False} is what lets
        the client show a real retry, not a made-up assistant line.

        before_id snapshots the high-water mark right before the turn runs
        -- same idiom turns.py already uses for its own before/after
        bookkeeping -- so any kind='tool' lines chat.run() logged mid-turn
        (gated by show_tool_calls, see chat.py) come back in THIS response
        alongside the final reply, not delayed to the next poll.

        voice (2026-09-13, voice conversation mode) -- True when this turn
        answers a push-to-talk turn (send_msg's own source=voice form
        field) or retries one (retry_post reads it back off the original
        user row). Tags the reply's meta the same way cost fields already
        are -- inert to the model (render_for_model doesn't special-case
        it), just a real record of how the turn happened for anything that
        reads history later (search_history, compaction, a "why did this
        conversation take this turn" question).

        extra_message (2026-09-15, chat_photo_post's own real image on the
        introducing turn) is passed straight through to chat.run()'s own
        param of the same name -- see that function's docstring."""
        before_id = conversation.max_id(user["id"])
        # Timing instrumentation (2026-09-13, config.debug_timing_enabled,
        # see timing.py) -- one Turn per answered message, spanning the
        # whole thing this function is responsible for: the model-turn
        # pipeline INSIDE chat.run() (passed down so its own internal
        # stages land in this SAME turn_id/log line) plus persisting the
        # reply, which chat.run() itself has no part in.
        turn = timing.start(user["workspace_id"], "voice" if voice else "chat")
        try:
            # peer_pending="user" (2026-09-19, operator's own reversal of
            # the 2026-09-13 correction, for THIS call site specifically):
            # this turn answers something HE just said, and his own answer
            # was "inject unread peer content into every turn from me" --
            # the 2026-09-13 concern (a pending message riding along and
            # pulling this reply off-topic) is a real tradeoff he's
            # choosing to accept here, not an oversight. "user" specifically
            # (not "peer") -- a message already shown to a peer-motivated
            # turn is NOT thereby read for this dimension; see peers.
            # pending_delivery_messages()'s own docstring. Still never set
            # for a turn nobody asked for (a proactive ping, a schedule, a
            # reminder -- see scheduler.py) -- this is specifically about
            # turns FROM him.
            res = chat.run(sess, user["id"], user["display_name"], extra_message=extra_message,
                           max_rounds=config.get("user", user["id"], "tool_rounds_chat"),
                           peer_pending="user", timing_turn=turn)
        except chat.ModelError as exc:
            turn.finish()
            if exc.kind == "blocked_upstream":
                return {"ok": False, "error": f"the model provider stopped responding: {exc}",
                       "error_kind": "blocked_upstream"}
            return {"ok": False, "error": f"couldn't reach the model: {exc}"}
        reply = res["text"]
        # kind='image' included alongside kind='tool' (2026-09-15, real bug:
        # a mid-turn image -- generate_image_selfie/imagine_image, or a
        # casual chat photo's own reply -- never appeared in THIS response
        # at all before this fix, only up to 15s later via the next poll()).
        # Same two kinds a sibling application's own _run_interactive_turn already
        # collects here (its `kinds = "('image','tool')"` clause) -- this
        # was the one place that comparison actually diverged.
        tool_msgs = []
        for r in conversation.since(user["id"], before_id, include_tool=True):
            if r["kind"] == "tool":
                tool_msgs.append({"id": r["id"], "role": r["role"], "content": r["content"],
                                 "emotion": None, "kind": "tool"})
            elif r["kind"] == "image":
                try:
                    img_meta = json.loads(r["meta"]) if r.get("meta") else None
                except (ValueError, TypeError):
                    img_meta = None
                tool_msgs.append({"id": r["id"], "role": r["role"], "content": r["content"],
                                 "emotion": r.get("emotion"), "kind": "image", "meta": img_meta})
        # Read AFTER chat.run() -- if she called set_emotion this turn, the
        # message gets tagged with the state she ended the turn in.
        state = emotion.get_state(user["id"])
        reply_meta = conversation.cost_meta(res["usage"])
        if voice:
            reply_meta["source"] = "voice"
        # debug panel (2026-09-30, operator's own ask) -- provider/model/
        # chain-position/request-id/reasoning/latency/failed-fallback-
        # attempts (dispatch_meta, built in chat.call_via_chain) plus this
        # turn's own tool-call log, folded under one "debug" sub-key so it
        # doesn't collide with cost_meta's existing fields. None (not a
        # key at all) when dispatch_meta is unset -- a round-limit/leak-
        # failure fallback text that never reached a real model call, so
        # there's honestly nothing to show, not an empty panel.
        dispatch_meta = res.get("dispatch_meta")
        if dispatch_meta:
            reply_meta["debug"] = {**dispatch_meta, "tool_calls": res.get("tool_calls") or [],
                                   "ts": time.time(), "cost_usd": reply_meta["cost_usd"],
                                   "cost_unavailable": reply_meta["cost_unavailable"],
                                   "prompt_tokens": reply_meta["prompt_tokens"],
                                   "completion_tokens": reply_meta["completion_tokens"]}
        with turn.stage("persist_reply"):
            aid = conversation.add_message(user["id"], "assistant", reply, emotion=state, meta=reply_meta)
        turn.finish()
        return {"ok": True, "messages": tool_msgs + [{"id": aid, "role": "assistant", "content": reply,
                                                       "emotion": state, "kind": "chat", "meta": reply_meta}],
               "state": state}

    def send_msg(self, sess: dict, form: dict):
        """JSON, not a redirect -- the client renders its own optimistic
        echo and reconciles this response against it (see CHAT_JS). The
        user's message is stored FIRST, always, before turns.run() even
        decides whether to answer it now or queue behind an already-
        running turn -- so a queued send is delayed, never lost.

        This is the ONE entry point voice mode's own transcribed-text
        turns go through too (see CHAT_JS's sendText) -- a voice turn is
        not a separate code path, just this same one with source=voice
        set on the request, so it gets turns.run()'s lock/queue for free
        instead of needing its own. A failed/empty STT never reaches here
        at all (the client only calls this once it has real text), so
        there is nothing here to guard against an empty voice turn beyond
        the same blank-text check every send already has."""
        text = (form.get("text") or "").strip()
        if not text:
            return self.send_json({"ok": True, "messages": []})
        voice = form.get("source") == "voice"
        user_id = sess["user_id"]
        user = accounts.get_user(user_id)
        uid = conversation.add_message(user_id, "user", text,
                                       meta={"source": "voice"} if voice else None)
        payload = turns.run(user_id,
                           lambda: self._run_turn(sess, user, voice=voice),
                           lambda _orphan: self._run_turn(sess, user, voice=voice))
        payload["user_id"] = uid
        payload["board_count"] = self._board_count(sess)
        return self.send_json(payload)

    def retry_post(self, sess: dict, form: dict):
        """Re-answers an already-stored message -- never creates a second
        user row. Ownership (get_own) and "not already answered" (has_
        reply_after) are both checked so a stale button (another tab
        already got a reply, or retried this exact message) fails cleanly
        with a real reason instead of double-replying."""
        user_id = sess["user_id"]
        try:
            target_id = int(form.get("user_id", ""))
        except (TypeError, ValueError):
            return self.send_json({"ok": False, "error": "bad id"}, 400)
        row = conversation.get_own(user_id, target_id)
        if row is None or row["role"] != "user":
            return self.send_json({"ok": False, "error": "no such message"}, 404)
        if conversation.has_reply_after(user_id, target_id):
            return self.send_json({"ok": False, "error": "already answered -- reload to see it"}, 409)
        # Voice-origin carries over from the original message being
        # retried, not from this request (a retry has no source field of
        # its own) -- so a voice turn that failed and gets retried still
        # tags its reply as voice-originated.
        voice = False
        if row.get("meta"):
            try:
                voice = json.loads(row["meta"]).get("source") == "voice"
            except (ValueError, TypeError):
                voice = False
        user = accounts.get_user(user_id)
        payload = turns.run(user_id,
                           lambda: self._run_turn(sess, user, voice=voice),
                           lambda _orphan: self._run_turn(sess, user, voice=voice))
        payload["user_id"] = target_id
        payload["board_count"] = self._board_count(sess)
        return self.send_json(payload)

    def chat_photo_post(self):
        """A casual photo sent directly in chat -- attach/paste/drop,
        ported from a sibling application's identical mechanism (2026-09-15; Nori
        never had this before -- every prior image message was one she
        generated herself). Reads its own raw multipart body ahead of
        form()'s small urlencoded-body limit, same as files_upload_post/
        voice_stt_post above.

        She sees the REAL photo directly, multimodal, on THIS turn only,
        if chat_vision_enabled produced a real description AND her real
        active model accepts image input (chat.model_accepts_images(),
        resolved per-workspace since her model isn't a single fixed
        constant the way a sibling application's is). Every later render of this
        message uses the cached text description instead -- see
        conversation.render_for_model(). The description is generated
        and cached here regardless of whether direct multimodal is used
        this turn, so the fallback always exists and future renders
        never re-run the vision call. Errors come back as JSON, not a
        page -- this is a chat action, not a form navigation."""
        sess = accounts.get_session(self.token())
        if sess is None:
            return self.send(303, b"", {"Location": "/login"})
        n = int(self.headers.get("Content-Length", 0) or 0)
        cap = int((workfiles.MAX_FILE_MB + 2) * 1024 * 1024)  # small overhead for the other form fields
        if n <= 0 or n > cap:
            self.rfile.read(min(max(n, 0), cap))  # drain what we safely can so the socket isn't left mid-body
            return self.send_json({"ok": False,
                                   "error": f"that file is over the {workfiles.MAX_FILE_MB:.0f} MB limit"})
        body = self.rfile.read(n)
        fields = self._parse_multipart(body, self.headers.get("Content-Type", ""))
        if not self.csrf_ok(sess, {"csrf": fields.get("csrf", "")}):
            return self.send_json({"ok": False, "error": "bad CSRF token -- reload and try again"})
        upload = fields.get("file")
        if not isinstance(upload, tuple) or not upload[1]:
            return self.send_json({"ok": False, "error": "no file was selected."})
        _filename, data = upload
        r = imagegen.store_chat_upload(data)
        if not r.get("ok"):
            return self.send_json({"ok": False, "error": r.get("reason", "upload failed")})
        caption = fields.get("caption", "")
        caption = caption.strip() if isinstance(caption, str) else ""
        user_id = sess["user_id"]
        user = accounts.get_user(user_id)
        # One vision call, stored in meta right away -- render_for_model()
        # reuses it on every later render (window or summary) instead of
        # this ever running again for this message.
        vres = imagegen.describe_chat_photo(user_id, data, r["mime"])
        meta = {"file_id": r["file_id"], "mime": r["mime"]}
        if vres:
            meta["description"] = vres["description"]
            meta["vision_model"] = vres["model"]
            meta["vision_ts"] = vres["ts"]
        uid = conversation.add_message(user_id, "user", caption, kind="image", meta=meta)
        medialog.log_chat_upload(workspace_id=sess["workspace_id"], user_id=user_id, file_id=r["file_id"])
        model_slug, _ = chat._resolve_model(sess["workspace_id"])
        if vres and chat.model_accepts_images(model_slug):
            extra = imagegen.chat_photo_extra_user_multimodal(data, r["mime"], caption)
        else:
            extra = imagegen.chat_photo_extra_user(vres["description"] if vres else None, caption)
        payload = turns.run(user_id,
                           lambda: self._run_turn(sess, user, extra_message=extra),
                           lambda _orphan: self._run_turn(sess, user, extra_message=extra))
        payload["ok"] = True
        payload["user_id"] = uid
        # the vision reading didn't exist yet when the client drew its own
        # optimistic echo of this exact upload -- hand it back so the JS
        # can append it to that same bubble now, instead of it only ever
        # showing up on a second tab via poll().
        if vres:
            payload["own_vision"] = {"id": uid, "vision_description": vres["description"],
                                     "vision_model": vres["model"], "vision_ts": vres["ts"]}
        return self.send_json(payload)

    def poll_get(self, sess: dict, since_raw: str):
        """The catch-up feed: a second tab, or a proactive ping that
        landed with nobody watching, shows up here without a reload.
        Also carries the current emotion state so a change that happened
        mid-conversation (not just at the moment THIS tab sent something)
        still updates live -- the actual unlock async send makes possible.

        board_count (2026-09-17, operator's own ask -- the header badge
        must stay correct as things are added/closed, not just at page
        load) rides along on this SAME already-running 15s cycle rather
        than a separate poll of its own -- one refresh path, reused, not
        a second timer just for the board. BOARD_JS uses it to update the
        badge every cycle regardless of whether the board is open, and
        additionally re-fetches /board/fragment for the board's own
        content when it IS open (see that route's own docstring)."""
        try:
            since = int(since_raw)
        except (TypeError, ValueError):
            since = 0
        rows = conversation.since(sess["user_id"], since,
                                  include_tool=config.get("user", sess["user_id"], "show_tool_calls"))
        return self.send_json({
            "messages": [{"id": r["id"], "role": r["role"], "content": r["content"],
                         "emotion": r.get("emotion"), "kind": r.get("kind"),
                         "meta": json.loads(r["meta"]) if r.get("meta") else None} for r in rows],
            "state": emotion.get_state(sess["user_id"]),
            "board_count": self._board_count(sess),
        })


def main() -> int:
    _redirect_logs()
    load_env(ENV_PATH)
    store.init()
    # Providers/Models rework (2026-09-30, see providers.py) -- there is
    # deliberately no default provider anymore, so no startup warning about
    # a missing OPENROUTER_API_KEY: a fresh install with nothing configured
    # in Settings > Models is the normal expected state, not a misconfig.
    # migrate_from_env() is the upgrade path for an instance that WAS
    # relying on that env var -- a no-op on a fresh install (no workspace,
    # nothing to migrate) and a no-op on a second startup (a provider
    # already exists by then).
    wsid = accounts.the_workspace_id()
    if wsid is not None:
        admin_row = store.read(lambda c: c.execute(
            "SELECT id FROM users WHERE workspace_id=? AND role='admin' ORDER BY id LIMIT 1",
            (wsid,)).fetchone())
        if admin_row is not None:
            providers.migrate_from_env(wsid, admin_row["id"])
    n = tool_builder.load_approved_tools()
    m = mcp_servers.register_all()
    p = peers.register_all()
    memory.register_peer_actions()  # see that function's own docstring for why this can't run at memory.py's own import time
    homeassistant.register_peer_actions()  # same reasoning, see that module's own docstring
    schedules.register_peer_actions()  # same reasoning, see that module's own docstring
    tasks.register_peer_actions()  # same reasoning, see that module's own docstring
    notes.register_peer_actions()  # same reasoning, see that module's own docstring
    reminders.register_peer_actions()  # same reasoning, see that module's own docstring
    trackers.register_peer_actions()  # same reasoning, see that module's own docstring
    swept = jobs.sweep_orphaned()
    peers.start()
    scheduler.start()
    print(f"nori on http://{BIND_HOST}:{PORT}  (secure-cookie={SECURE_COOKIE})  "
         f"({n} generated tool(s), {m} MCP server(s), {p} peer(s) loaded, "
         f"{len(swept)} orphaned job(s) swept)", flush=True)
    ThreadingHTTPServer((BIND_HOST, PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
