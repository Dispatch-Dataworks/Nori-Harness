# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""The last few thousand lines the server printed, kept in memory (2026-10-30), so the debug endpoint (debug_api.py) can show recent errors and behaviour
without anyone having to get at the container's logs.

It is a tee, not a replacement: everything still goes where it went (the console, docker's log, the timestamped log files) and a copy of each complete
line is also kept in a fixed-size ring. Nothing is written to disk by this module and nothing leaves the process except through the debug endpoint, which
is off by default and scrubs what it returns. The ring is lost on restart, deliberately: it is for the current run.
"""
from __future__ import annotations

import collections
import re
import sys
import threading
import time

MAX_LINES = 4000
MAX_LINE_CHARS = 2000

_lines: "collections.deque[dict]" = collections.deque(maxlen=MAX_LINES)
_lock = threading.Lock()
_installed = False

# What makes a line worth showing under "errors". Deliberately broad: the point is not to miss one.
_ERROR_RE = re.compile(r"traceback|exception|\berror\b|\bfailed\b|\bfailure\b|raised|cut off|screening failed|refus|could not|can't|timed out|timeout|denied|unauthori[sz]ed",
                       re.I)


def level_of(text: str) -> str:
    return "error" if _ERROR_RE.search(text) else "info"


def add(stream: str, text: str) -> None:
    text = text.rstrip("\r\n")
    if not text:
        return
    with _lock:
        _lines.append({"ts": time.time(), "stream": stream, "text": text[:MAX_LINE_CHARS], "level": level_of(text)})


class _Tee:
    """Wraps a text stream: writes through unchanged, and keeps each completed line."""

    def __init__(self, inner, name: str):
        self._inner = inner
        self._name = name
        self._buf = ""
        self._buf_lock = threading.Lock()

    def write(self, s):
        n = self._inner.write(s)
        try:
            with self._buf_lock:
                self._buf += s if isinstance(s, str) else s.decode("utf-8", "replace")
                while "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    add(self._name, line)
                if len(self._buf) > 20000:        # a runaway write with no newline must not grow without bound
                    add(self._name, self._buf)
                    self._buf = ""
        except Exception:  # noqa: BLE001 -- keeping a copy must never break printing
            pass
        return n

    def flush(self):
        return self._inner.flush()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def install() -> None:
    """Start keeping a copy of stdout and stderr. Idempotent."""
    global _installed
    if _installed:
        return
    _installed = True
    sys.stdout = _Tee(sys.stdout, "out")
    sys.stderr = _Tee(sys.stderr, "err")


def recent(limit: int = 500, *, contains: str = "", level: str = "", stream: str = "", since_ts: float = 0.0) -> list[dict]:
    """Newest-last list of up to `limit` lines matching the filters."""
    limit = max(1, min(int(limit), MAX_LINES))
    needle = contains.lower()
    with _lock:
        rows = list(_lines)
    out = [r for r in rows if r["ts"] >= since_ts and (not level or r["level"] == level) and (not stream or r["stream"] == stream)
           and (not needle or needle in r["text"].lower())]
    return out[-limit:]


def clear() -> None:
    with _lock:
        _lines.clear()
