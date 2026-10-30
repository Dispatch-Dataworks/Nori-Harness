#!/usr/bin/env python3
"""Ask one or more Nori agents their debug endpoint (docs/debug.md). Read-only.

    NORI_DEBUG_TOKEN=...  python scripts/debug_query.py --agent nori=https://nori.example.com --agent iron=https://iron.example.com errors
    NORI_DEBUG_TOKEN=...  NORI_DEBUG_AGENTS="nori=https://a,iron=https://b"  python scripts/debug_query.py conversation user=Tester limit=20
    python scripts/debug_query.py --token-env MY_VAR --agent sage=https://sage.example.com overview

The token is read from an environment variable (NORI_DEBUG_TOKEN by default), never from the command line, so it does not land in shell history or a
process list. With no section it lists them. `key=value` words after the section become query parameters (limit=, user=, grep=, level=, ...).
Standard library only. Exit status 0 if every agent answered, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def ask(base: str, section: str, params: dict, token: str, timeout: float) -> tuple[int, object]:
    url = base.rstrip("/") + "/debug" + (("/" + section) if section else "")
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": "nori-debug-query"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"error": raw[:200] or e.reason}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):                       # conversations contain any character; a Windows console would choke on some
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent", action="append", default=[], metavar="NAME=URL", help="an agent to ask (repeatable); default: NORI_DEBUG_AGENTS")
    ap.add_argument("--token-env", default="NORI_DEBUG_TOKEN", help="the environment variable holding the token")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--compact", action="store_true", help="one line of JSON per agent")
    ap.add_argument("section", nargs="?", default="", help="overview, health, errors, logs, conversation, ... (none: list them)")
    ap.add_argument("params", nargs="*", help="key=value query parameters")
    a = ap.parse_args(argv)

    token = os.environ.get(a.token_env, "").strip()
    if not token:
        print(f"set {a.token_env} to the debug token first (it is deliberately not accepted on the command line)", file=sys.stderr)
        return 2
    specs = a.agent or [s for s in os.environ.get("NORI_DEBUG_AGENTS", "").split(",") if s.strip()]
    agents = []
    for s in specs:
        name, _, url = s.strip().partition("=")
        if not url:
            name, url = urllib.parse.urlsplit(s.strip()).hostname or s.strip(), s.strip()
        agents.append((name, url))
    if not agents:
        print("name at least one agent: --agent name=https://host  (or set NORI_DEBUG_AGENTS)", file=sys.stderr)
        return 2
    params = dict(p.split("=", 1) for p in a.params if "=" in p)

    bad = 0
    for name, url in agents:
        status, body = ask(url, a.section, params, token, a.timeout)
        if status != 200:
            bad += 1
            hint = {404: "off, or not a Nori that has the debug endpoint", 401: "wrong token", 429: "rate limited", 0: "unreachable"}.get(status, "")
            body = {"agent": name, "status": status, "hint": hint, "response": body}
        else:
            body = {"agent": name, **body}
        print(json.dumps(body, ensure_ascii=False) if a.compact else json.dumps(body, indent=2, ensure_ascii=False, default=str))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
