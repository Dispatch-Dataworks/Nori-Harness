# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Web search and fetch -- Nori's way to reach the open web. Design
approved 2026-09-13 (session record), built 2026-09-14 once
TAVILY_API_KEY's placeholder had been sitting empty in .env for a day.

web_search is a direct REST call to Tavily -- no SDK, one dependency-free
HTTP POST. web_fetch is OUR OWN hardened fetch, not Tavily's -- Tavily's
own API doesn't offer arbitrary URL retrieval, and even if it did,
proxying every fetch through a third party would be a real privacy/cost
tradeoff nobody asked for.

_fetch_checked() is the single chokepoint for NETWORK safety (module-
private on purpose -- nothing outside this file should ever build its
own urllib call to an address that isn't ours): every URL this app ever
reaches over HTTP for an agent-initiated fetch goes through it, so the
SSRF protection below lives in exactly one place, not re-implemented per
caller. That guarantee is real and was always scoped to this one thing.

It was NOT, despite how this used to read, a content-safety guarantee
too -- there was no chokepoint at all for the actual CONTENT search()
and fetch() hand back, until 2026-09-14 (found live, the first day a
real Tavily key existed to test against: raw page text and raw search
results were both reaching a tool-calling turn completely unscreened,
the one path in this app that pulls content from the open internet
rather than a working folder the operator controls). _screen_web_content()
is that chokepoint now -- both search() and fetch() funnel their real
content through it before returning, one real screening call each, same
ingest.summarize_untrusted() every other untrusted-content path in this
app uses. Stated plainly rather than left implied: if this file ever
grows a third way to bring internet content back to a model, it goes
through _screen_web_content() too, or the guarantee this paragraph
describes is fiction again.

Read vs write, asymmetric on purpose:
  - GET is open-by-default, blocked only by an admin's own blacklist
    (web_domain_rules kind='read_block') -- reading the open web this way
    is the ordinary, comparatively low-risk case.
  - Anything else (POST/PUT/PATCH/DELETE) is closed-by-default, allowed
    only by an admin's own allow-list (kind='write_allow') -- an agent
    submitting data TO a third party is a meaningfully bigger risk
    (exfiltration, an unintended real side effect on someone else's
    site) and stays opt-in, one domain at a time.

SSRF: every hostname is resolved to ALL its addresses before connecting;
rejected if ANY of them is private/loopback/link-local/reserved/
multicast (ipaddress's own classification, not a hand-rolled range
list). A redirect is never auto-followed -- urllib's own redirect
handler is disabled below specifically so each hop re-enters
_check_url() from scratch (allow/blacklist AND a fresh DNS resolution
AND the same IP check), up to _MAX_REDIRECTS -- otherwise a clean-
looking public domain that 302s to http://169.254.169.254/ (a real,
common trick against cloud metadata endpoints) would sail through on
its first hop's check alone.

Write mode -- off/simulated/real, per workspace (config's
web_fetch_write_mode, 2026-09-14, operator's own explicit safety design):
a THIRD gate on top of the write allow-list above, specifically for
non-GET methods. 'off' (the default) denies before even resolving the
hostname. 'simulated' still runs every real policy check -- denied_by is
real and meaningful even here -- but returns before ever opening a
socket once those checks clear. The model is deliberately NOT told this
was simulated (operator's own explicit call, overriding an earlier one
of mine that leaned the other way): he wants to observe genuine
behavior when a write silently doesn't land, not behavior conditioned on
knowing it's a test. What actually reaches the model is indistinguishable
from a real transient network failure -- same wording urllib itself
raises, no distinguishing key, realistic timing on the first few
attempts -- see fetch()'s own sanitizing step and _SIMULATED_MESSAGES's
comment. The log is the one place the truth survives: simulated=1 on
the real row, unambiguous in the settings page, precisely because the
model-facing side of this is now deliberately opaque. config.py's own
set() enforces off->real as a two-step transition through simulated
first -- turning writes on for real can't happen in one move.

Every call, allowed or denied, real or simulated, is logged in full --
method, agent, URL, denied_by (which specific rule, if any), simulated,
HTTP status, and payload -- see _log()/recent_log() and store.py's own
migration comment. This was already true in shape before tonight (every
call already reached _log()); tonight added the columns that make the
audit trail actually legible rather than a bare ok/fail with a string.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

import store

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_MAX_REDIRECTS = 5
_FETCH_TIMEOUT_S = 12
_MAX_BODY_BYTES = 500_000
_USER_AGENT = "nori-webfetch/1.0"

# Simulated-write realism (2026-09-14, operator's own explicit override --
# he does NOT want the model to know it's being tested; a disclosed
# simulation was my own earlier call, reversed here on his). Both
# messages are the LITERAL strings Python's own urllib raises for a real
# timeout / a real connection refusal -- not close approximations,
# reused verbatim so there is no wording tell. Rotated by attempt count
# (not random) so a repeated probe to the same endpoint sees a flaky-
# looking pattern a real intermittently-down server would also produce,
# rather than the identical string forever.
_SIMULATED_MESSAGES = [
    "fetch failed: <urlopen error timed out>",
    "fetch failed: <urlopen error [Errno 111] Connection refused>",
]
# After this many simulated attempts at the SAME method+URL within the
# window below, stop paying the full realistic delay -- a real
# repeatedly-failing server often does start failing FASTER on retry
# (connection refused doesn't wait out a timeout), so this is itself
# realistic, not a shortcut that reads as one.
_SIMULATED_FAST_FAIL_AFTER = 3
_SIMULATED_ATTEMPT_WINDOW_S = 900


def _tavily_key() -> str:
    return os.environ.get("TAVILY_API_KEY", "").strip()


def configured() -> bool:
    return bool(_tavily_key())


# ── the content-screening chokepoint this file never actually had
# (2026-09-14, real gap found live, first day the Tavily key existed to
# test against) ───────────────────────────────────────────────────────
# _fetch_checked() below IS a real chokepoint, exactly as its own
# docstring claims -- but only for SSRF/network safety. Neither it nor
# search() ever screened the actual CONTENT that came back: a Tavily
# result's page text and a fetched page's raw body both went straight
# into the tool result an agent-facing turn sees, completely unscreened
# -- the one path in this app that pulls content from the open internet,
# not a working folder the operator controls, and it was the one with no
# screening at all. Every other untrusted-content path (read_file,
# search_files, a sub-agent job's own result) gets one real
# ingest.summarize_untrusted() pass before reaching a turn with real
# tools; this didn't, until now. search() and fetch() both funnel through
# this one function -- a genuine single chokepoint this time, not two
# independently hand-rolled screening calls that could drift or that a
# future third caller could just as easily skip.
_SCREEN_MAX_CHARS = 200_000  # same cap workfiles.read_file uses, capped before the ingest pass ever sees it


def _screen_web_content(text: str, *, kind: str) -> dict:
    import ingest  # local: same load-order reasoning every other lazy import in this app uses
    return ingest.summarize_untrusted(text[:_SCREEN_MAX_CHARS], kind=kind, preserve_content=True)


def _log(kind: str, target: str, *, ok: bool, reason: str | None = None,
         cost_usd: float | None = None, cost_is_actual: bool | None = None,
         method: str | None = None, agent: str | None = None, simulated: bool = False,
         denied_by: str | None = None, status: int | None = None, payload: str | None = None) -> None:
    """Full audit trail (2026-09-14, operator's own ask: "log every
    request... complete rather than sampled") -- every call already
    reached this function before tonight's change; what's new is the
    columns, not the coverage. denied_by and simulated are deliberately
    separate axes -- see store.py's own migration comment for why."""
    store.write(lambda c: c.execute(
        "INSERT INTO web_tool_log(ts, kind, target, ok, reason, cost_usd, cost_is_actual, "
        "method, agent, simulated, denied_by, status, payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (time.time(), kind, target[:500], 1 if ok else 0, (reason or None) and str(reason)[:500],
         cost_usd, None if cost_is_actual is None else (1 if cost_is_actual else 0),
         method, (agent or None) and str(agent)[:120], 1 if simulated else 0,
         denied_by, status, (payload or None) and str(payload)[:4000])))


def recent_log(limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit or 100), 500))
    return [dict(r) for r in store.read(lambda c: c.execute(
        "SELECT * FROM web_tool_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall())]


# ── search ────────────────────────────────────────────────────────────────
def search(query: str, *, max_results: int = 5, agent: str | None = None) -> dict:
    query = (query or "").strip()
    if not query:
        return {"ok": False, "reason": "empty query"}
    key = _tavily_key()
    if not key:
        # The graceful, plainly-stated unconfigured state the operator
        # asked for -- not an obscure connection failure or stack trace.
        reason = ("web search isn't set up yet -- the operator hasn't added a Tavily API key. "
                 "Tell him that plainly if he asks; it's not something you did wrong.")
        _log("search", query, ok=False, reason="not configured", agent=agent)
        return {"ok": False, "reason": reason}
    body = json.dumps({"api_key": key, "query": query,
                       "max_results": max(1, min(int(max_results or 5), 10)),
                       "search_depth": "basic"}).encode("utf-8")
    req = urllib.request.Request(TAVILY_SEARCH_URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        _log("search", query, ok=False, reason=f"HTTP {exc.code}: {detail}", agent=agent, status=exc.code)
        return {"ok": False, "reason": f"web search failed ({exc.code}): {detail}"}
    except Exception as exc:  # noqa: BLE001 -- network/JSON errors, never let a tool call raise
        _log("search", query, ok=False, reason=str(exc), agent=agent)
        return {"ok": False, "reason": f"web search failed: {exc}"}
    raw_results = data.get("results") or []
    results = [{"title": r.get("title"), "url": r.get("url")} for r in raw_results]
    blob_parts = []
    if data.get("answer"):
        blob_parts.append(f"Tavily's own summarized answer:\n{data['answer']}")
    for r in raw_results:
        blob_parts.append(f"{r.get('title')} ({r.get('url')})\n{(r.get('content') or '')[:1000]}")
    screened = (_screen_web_content("\n---\n".join(blob_parts), kind="web search results")
               if blob_parts else {"content": "", "suspicious": False, "truncated": False})
    # Tavily bills in account-level API credits, not a per-call dollar
    # figure this response hands back -- cost_usd stays honestly None
    # (see store.py's own table comment) rather than a guessed number.
    _log("search", query, ok=True, cost_usd=None, agent=agent, status=200)
    return {"ok": True, "query": query, "results": results,
            "content": screened.get("content") or screened.get("summary") or "",
            "suspicious": screened.get("suspicious", False),
            "truncated": screened.get("truncated", False)}


# ── fetch: SSRF-checked chokepoint ──────────────────────────────────────────
def _is_public_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
               or ip.is_multicast or ip.is_unspecified)


def _resolve_all_public(hostname: str) -> bool:
    """True only if EVERY address this hostname resolves to is public --
    a hostname resolving to even one internal address (DNS rebinding, a
    multi-A-record trick, split-horizon DNS) is rejected outright rather
    than racing which address the actual TCP connect happens to pick."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    return all(_is_public_ip(info[4][0]) for info in infos)


def _domain_matches(pattern: str, hostname: str) -> bool:
    """"*.example.com" matches "example.com" itself AND any subdomain --
    a plain suffix/endswith check alone would miss the bare domain, which
    is exactly the corner an admin blacklisting "*.evil.com" would expect
    covered without also having to list "evil.com" separately."""
    pattern = pattern.lower().strip().lstrip(".")
    hostname = hostname.lower().strip().rstrip(".")
    if pattern.startswith("*."):
        base = pattern[2:]
        return hostname == base or hostname.endswith("." + base)
    return hostname == pattern


def rules(kind: str) -> list[dict]:
    return [dict(r) for r in store.read(lambda c: c.execute(
        "SELECT * FROM web_domain_rules WHERE kind=? ORDER BY pattern", (kind,)).fetchall())]


def add_rule(kind: str, pattern: str) -> tuple[bool, str]:
    kind = (kind or "").strip()
    pattern = (pattern or "").strip().lower()
    if kind not in ("read_block", "write_allow"):
        return False, "invalid rule kind"
    if not pattern:
        return False, "empty pattern"
    try:
        store.write(lambda c: c.execute(
            "INSERT INTO web_domain_rules(kind, pattern, added_ts) VALUES (?,?,?)",
            (kind, pattern, time.time())))
    except Exception as exc:  # noqa: BLE001 -- almost certainly the UNIQUE constraint
        return False, f"already on the list, or invalid ({exc})"
    return True, "added"


def remove_rule(rule_id: int) -> None:
    store.write(lambda c: c.execute("DELETE FROM web_domain_rules WHERE id=?", (rule_id,)))


def _host_allowed(hostname: str, method: str) -> tuple[bool, str | None, str | None]:
    """(ok, denied_by, reason). denied_by is the machine-readable rule
    name (2026-09-14, audit trail) -- 'read_block' or 'write_allow' --
    None when nothing blocked it."""
    if method == "GET":
        for r in rules("read_block"):
            if _domain_matches(r["pattern"], hostname):
                return False, "read_block", f"{hostname} is on the read blacklist ({r['pattern']})"
        return True, None, None
    for r in rules("write_allow"):
        if _domain_matches(r["pattern"], hostname):
            return True, None, None
    return False, "write_allow", f"{hostname} is not on the write allow-list -- writes are closed by default"


def _check_url(url: str, method: str) -> tuple[bool, str | None, str | None]:
    """(ok, denied_by, reason-if-not). One function, reused for the
    original URL AND every redirect hop, so a redirect gets exactly the
    scrutiny an original request does -- see module docstring. denied_by
    (2026-09-14, audit trail) names which specific check failed --
    'scheme', 'no_hostname', 'read_block', 'write_allow', or 'ssrf' --
    None when every check clears."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False, "unparseable", "unparseable URL"
    if parsed.scheme not in ("http", "https"):
        return False, "scheme", f"scheme {parsed.scheme!r} not allowed -- only http/https"
    hostname = parsed.hostname
    if not hostname:
        return False, "no_hostname", "no hostname in URL"
    allowed, denied_by, reason = _host_allowed(hostname, method)
    if not allowed:
        return False, denied_by, reason
    if not _resolve_all_public(hostname):
        return False, "ssrf", f"{hostname} resolves to a non-public address -- refused"
    return True, None, None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Disables urllib's own automatic redirect-following -- returning
    None from redirect_request tells urlopen to raise HTTPError for a
    3xx instead of silently chasing Location itself. Manual control over
    each hop is the whole point (see module docstring)."""
    def redirect_request(self, *a, **kw):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _fetch_checked(url: str, *, method: str = "GET", body: str | None = None,
                   write_mode: str = "real", binary: bool = False,
                   max_bytes: int | None = None) -> dict:
    """THE chokepoint (see module docstring) -- every caller in this app
    that wants to reach an arbitrary URL goes through this, never a raw
    urllib call of its own. write_mode (2026-09-14, operator's own
    explicit safety design) only ever matters for a non-GET method --
    'off' denies before even resolving the hostname (fastest deny,
    matches the "closed by default" posture); 'simulated' still runs
    every real policy check (denied_by is real and meaningful even in
    simulated mode) but returns before ever opening a socket once those
    checks clear; 'real' is today's original behavior, unchanged.

    binary=True (2026-09-15, the download tool's own need) skips the
    UTF-8 decode and returns raw `bytes` instead of `content` --
    otherwise every safety step above is identical, same _check_url per
    hop, same redirect handling, same opener. One function two shapes
    share, rather than a second hand-rolled fetch loop -- an earlier
    version that DID duplicate the loop was tried and backed out, since
    the two copies drifted out of sync with each other over time.
    max_bytes overrides the default
    text cap -- the download tool passes its own (workfiles' own
    MAX_FILE_MB), since an image is reasonably larger than the
    500KB text/page cap this function was originally sized for."""
    method = (method or "GET").upper()
    cap = max_bytes if max_bytes is not None else _MAX_BODY_BYTES
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        return {"ok": False, "reason": f"method {method!r} not supported", "denied_by": "method"}
    if method != "GET" and write_mode == "off":
        return {"ok": False, "denied_by": "write_mode_off", "url": url,
                "reason": "writes are off for this agent -- ask an operator to enable simulated "
                         "or real mode in settings if this genuinely needs to happen."}
    ok, denied_by, reason = _check_url(url, method)
    if not ok:
        return {"ok": False, "reason": reason, "url": url, "denied_by": denied_by}
    if method != "GET" and write_mode == "simulated":
        # Cleared every real policy check above -- denied_by is None
        # here, deliberately: this isn't a denial, it's a withheld send.
        # This dict is TRUTH, for _log() only -- fetch() below is what
        # builds the sanitized, indistinguishable-from-a-real-failure
        # value that actually reaches the model. Never let this "reason"
        # or "would_have_sent" leak past fetch()'s own sanitizing step.
        return {"ok": False, "simulated": True, "url": url,
                "reason": "SIMULATED write -- not sent",
                "would_have_sent": {"method": method, "url": url, "body": body}}
    current = url
    for _hop in range(_MAX_REDIRECTS + 1):
        if _hop > 0:  # already checked once above for the original URL
            ok, denied_by, reason = _check_url(current, method)
            if not ok:
                return {"ok": False, "reason": reason, "url": current, "denied_by": denied_by}
        req = urllib.request.Request(
            current, method=method,
            data=(body.encode("utf-8") if (body and method != "GET") else None),
            headers={"User-Agent": _USER_AGENT})
        try:
            with _opener.open(req, timeout=_FETCH_TIMEOUT_S) as resp:
                data = resp.read(cap + 1)
                truncated = len(data) > cap
                if binary:
                    return {"ok": True, "status": resp.status, "url": resp.geturl(),
                            "bytes": data[:cap], "truncated": truncated}
                text = data[:cap].decode("utf-8", "replace")
                return {"ok": True, "status": resp.status, "url": resp.geturl(),
                        "content": text, "truncated": truncated}
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                current = urllib.parse.urljoin(current, exc.headers["Location"])
                continue
            detail = exc.read().decode("utf-8", "replace")[:300]
            return {"ok": False, "reason": f"fetch failed ({exc.code}): {detail}", "url": current,
                    "status": exc.code}
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            return {"ok": False, "reason": f"fetch failed: {exc}", "url": current}
    return {"ok": False, "reason": f"too many redirects (>{_MAX_REDIRECTS})", "url": current}


def fetch_binary(url: str, *, agent: str | None = None, max_bytes: int | None = None) -> dict:
    """The download tool's own entry point -- raw bytes, never decoded,
    never screened (_screen_web_content only ever runs inside fetch()
    below; this never calls that). Same SSRF/redirect chokepoint as an
    ordinary fetch, logged the same way, under kind='fetch_binary' so
    it's distinguishable in the audit trail from an ordinary text
    fetch. GET only -- there's no reason a download would ever write."""
    res = _fetch_checked(url, method="GET", binary=True, max_bytes=max_bytes)
    _log("fetch_binary", url, ok=res.get("ok", False), reason=None if res.get("ok") else res.get("reason"),
        method="GET", agent=agent, denied_by=res.get("denied_by"), status=res.get("status"))
    return res


def log_download_result(url: str, *, ok: bool, verdict: str, agent: str | None = None,
                        detail: str | None = None) -> None:
    """The scan-and-promote outcome for a download, logged into the
    same web_tool_log the fetch step itself already used (kind=
    'fetch_binary' above) -- a second row here, kind='download_result',
    so the two steps (could it be fetched at all vs. did the scan pass)
    are each their own event rather than one call's outcome overwriting
    the other's. Exists so workfiles.download_image() never has to
    reach into this module's own _log() directly -- one small public
    seam instead of leaning on a private convention. This is also what
    backfills the visibility scanner.py's own -DisableRemediation
    choice takes away from Defender's own event log/UI: `detail`
    carries the real threat name or scan-unavailable reason, `verdict`
    the clean/infected/unavailable outcome, `reason` (via `ok`) what
    the model itself was told."""
    _log("download_result", url, ok=ok, reason=None if ok else detail, payload=detail, agent=agent,
        denied_by=None if verdict == "clean" else verdict)


def _recent_simulated_attempts(method: str, url: str) -> int:
    """How many times this exact method+URL has already been simulated
    within _SIMULATED_ATTEMPT_WINDOW_S -- the fast-fail-after-N gate's
    only input. Queried against the real log rather than kept in memory:
    durable across a restart, and there's no second source of truth to
    let drift from what actually got logged."""
    cutoff = time.time() - _SIMULATED_ATTEMPT_WINDOW_S
    target = f"{method} {url}"
    row = store.read(lambda c: c.execute(
        "SELECT COUNT(*) AS n FROM web_tool_log WHERE simulated=1 AND target=? AND ts >= ?",
        (target, cutoff)).fetchone())
    return row["n"] if row else 0


def fetch(url: str, *, method: str = "GET", body: str | None = None,
         agent: str | None = None, write_mode: str = "real") -> dict:
    method_u = (method or "GET").upper()
    res = _fetch_checked(url, method=method_u, body=body, write_mode=write_mode)
    simulated = bool(res.get("simulated"))
    _log("fetch", f"{method_u} {url}", ok=res.get("ok", False),
        reason=None if res.get("ok") else res.get("reason"),
        method=method_u, agent=agent, simulated=simulated,
        denied_by=res.get("denied_by"), status=res.get("status"),
        payload=body if method_u != "GET" else None)
    if not simulated:
        if res.get("ok") and "content" in res:
            # `truncated` above is _fetch_checked's own -- the ORIGINAL
            # body got cut at MAX_BODY_BYTES. `content_truncated` here is
            # a different cut: the screened PREVIEW an agent actually
            # sees got cut at _SCREEN_MAX_CHARS/PRESERVE_CAP. Keeping both
            # rather than collapsing them into one flag -- they answer
            # different questions and conflating them would be exactly
            # the kind of silent inaccuracy this app avoids elsewhere.
            screened = _screen_web_content(res["content"], kind="fetched page")
            res = {**res, "content": screened.get("content") or screened.get("summary") or "",
                  "suspicious": screened.get("suspicious", False),
                  "content_truncated": screened.get("truncated", False)}
        return res
    # Everything past this point is sanitizing (2026-09-14, operator's
    # own explicit override -- see _SIMULATED_MESSAGES's own comment):
    # the TRUE result was already logged above; what the caller/model
    # actually receives from here on must be indistinguishable from a
    # real transient network failure -- same wording urllib itself would
    # raise, no "simulated" key, no "would_have_sent", no status code (a
    # real connection-level failure like this never has an HTTP status
    # either). attempt_count decides realistic pacing: an early attempt
    # pays the same real timeout a genuine one would; a repeated attempt
    # to the SAME endpoint fails fast (see _SIMULATED_FAST_FAIL_AFTER's
    # own comment for why that's realistic too, not a shortcut).
    attempt = _recent_simulated_attempts(method_u, url)  # this attempt is already logged, so count includes it
    if attempt <= _SIMULATED_FAST_FAIL_AFTER:
        time.sleep(_FETCH_TIMEOUT_S)
        message = _SIMULATED_MESSAGES[0]
    else:
        message = _SIMULATED_MESSAGES[(attempt - 1) % len(_SIMULATED_MESSAGES)]
    return {"ok": False, "reason": message, "url": url}


# ── tool registration ────────────────────────────────────────────────────
def _agent_label(session: dict) -> str:
    import accounts  # local: same load-order reasoning as tools.py's own late imports
    user = accounts.get_user(session["user_id"])
    return user["display_name"] if user else f"user {session['user_id']}"


def _web_search_impl(session: dict, query: str, max_results: int = 5) -> dict:
    import config  # local: same reasoning as accounts above
    if not config.get("workspace", session["workspace_id"], "web_search_enabled"):
        return {"ok": False, "reason": "web search is turned off in settings"}
    return search(query, max_results=max_results, agent=_agent_label(session))


def _web_fetch_impl(session: dict, url: str, method: str = "GET", body: str | None = None) -> dict:
    import config  # local: same reasoning as accounts above
    if not config.get("workspace", session["workspace_id"], "web_fetch_enabled"):
        return {"ok": False, "reason": "web fetch is turned off in settings"}
    write_mode = config.get("workspace", session["workspace_id"], "web_fetch_write_mode")
    return fetch(url, method=method, body=body, agent=_agent_label(session), write_mode=write_mode)


def _register_tools() -> None:
    import tools  # local: same reasoning as household.py/memory.py

    tools.register(tools.Tool(
        "web_search",
        {"type": "function", "function": {
            "name": "web_search",
            # Purpose text written for Nori specifically (2026-09-14,
            # operator's own requirement) -- a household assistant looking
            # things up for the people she works for, not a generic
            # "search the internet" blurb reused across agents.
            "description": ("Search the live web for something you or a household member needs to "
                            "know right now -- current info, a fact, a place, a how-to. Not "
                            "configured until the operator adds a Tavily API key; if it comes back "
                            "saying so, tell whoever asked plainly rather than pretending it worked."),
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "description": "1-10, default 5"}},
                "required": ["query"]}}},
        _web_search_impl, min_role="member", data_scope="workspace", risk_tier="B"))

    tools.register(tools.Tool(
        "web_fetch",
        {"type": "function", "function": {
            "name": "web_fetch",
            "description": ("Fetch the actual content of a specific URL -- a page a search result "
                            "pointed at, a link someone in the household shared with you. GET-only "
                            "reads are allowed by default; writing to a site (method other than GET) "
                            "only works for a domain the operator has explicitly allow-listed, and "
                            "refuses plainly otherwise -- that's not a bug to work around."),
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string"},
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                          "description": "default GET"},
                "body": {"type": "string", "description": "request body, only used for a non-GET method"}},
                "required": ["url"]}}},
        _web_fetch_impl, min_role="member", data_scope="workspace", risk_tier="B"))


_register_tools()
