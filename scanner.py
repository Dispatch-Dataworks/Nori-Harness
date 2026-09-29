# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Virus-scan interface for downloaded files -- one function behind
which the actual engine lives, so the download tool (workfiles.py)
never talks to a specific antivirus product directly. Built this way
on the operator's own explicit instruction, 2026-09-15: he's fine with
Defender today, expects to switch or make it configurable once this is
containerized and redistributable, and wants that later swap to be a
config change and a second backend function, not a rewrite of the
download path.

Backend picked by NORI_SCANNER_BACKEND (default 'defender' -- see
_BACKENDS below). Adding ClamAV, or anything else, is a new function of
the same shape plus one line in _BACKENDS -- nothing in workfiles.py
changes.

Three verdicts, not two -- this is the actual point of this module:
  "clean"        scanned, nothing matched. Safe to promote.
  "infected"     scanned, a real match. Never promote; caller deletes.
  "unavailable"  the backend couldn't produce a real verdict at all --
                 binary missing, crashed, timed out, or an unknown
                 backend name. This is NOT clean, and callers must
                 never treat it as clean by default -- see allowed()
                 below. Same deny-by-default posture as check_secrets'
                 own scan, and for the same reason: the failure mode
                 that matters is exactly the one where the safety
                 check silently didn't run, on a machine (or a
                 container) that doesn't have Defender at all.

Boundary this module does NOT cover, stated here so it's never
silently implied elsewhere: a clean scan means the bytes don't match a
known malware signature. It says nothing about whether the file's
CONTENT is safe to trust as instructions or data -- that's
ingest.py's own reader/actor split, a different problem. Not live for
images (never read as language), but if a caller ever downloads
text/documents through this path, a clean scan verdict must not be
read as license to skip content screening on top of it.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

SCANNER_BACKEND = os.environ.get("NORI_SCANNER_BACKEND", "defender").strip().lower()
# "refuse" (default, safe) or "allow" -- an explicit, deliberate opt-in
# an operator has to set themselves; never something a caller decides
# per-call.
ON_UNAVAILABLE = os.environ.get("NORI_SCANNER_ON_UNAVAILABLE", "refuse").strip().lower()
SCAN_TIMEOUT_S = float(os.environ.get("NORI_SCANNER_TIMEOUT_S", "30"))

_DEFENDER_PATH = r"C:\Program Files\Windows Defender\MpCmdRun.exe"
_THREAT_RE = re.compile(r"^Threat\s*:\s*(.+)$", re.MULTILINE)


class ScanResult:
    def __init__(self, verdict: str, *, threat: str | None = None, detail: str | None = None):
        self.verdict = verdict  # "clean" | "infected" | "unavailable"
        self.threat = threat
        self.detail = detail

    def __repr__(self) -> str:
        return f"ScanResult({self.verdict!r}, threat={self.threat!r}, detail={self.detail!r})"


def scan(path: Path) -> ScanResult:
    """The one function every caller uses. Never raises -- a backend
    that crashes or misbehaves comes back as "unavailable", the same
    as one that's simply missing, so a caller can't be surprised by an
    exception here and accidentally fail open."""
    backend = _BACKENDS.get(SCANNER_BACKEND)
    if backend is None:
        return ScanResult("unavailable", detail=f"unknown scanner backend {SCANNER_BACKEND!r}")
    try:
        return backend(path)
    except Exception as exc:  # noqa: BLE001 -- a backend crash must never read as "clean"
        return ScanResult("unavailable", detail=f"scanner backend {SCANNER_BACKEND!r} crashed: {exc}")


def allowed(result: ScanResult) -> bool:
    """THE decision point -- "should this file be promoted." A caller
    uses this rather than checking result.verdict itself, so the
    unavailable-means-refuse default can't quietly drift at some future
    call site that forgets the third case exists."""
    if result.verdict == "clean":
        return True
    if result.verdict == "infected":
        return False
    return ON_UNAVAILABLE == "allow"  # "unavailable"


def _scan_defender(path: Path) -> ScanResult:
    """Windows Defender via MpCmdRun.exe -- confirmed directly against a
    real detection (the standard EICAR AV test string, in a scratch
    directory well outside any nori path) before this was written, not
    assumed from documentation alone. -DisableRemediation is the whole
    point: WITH Defender's own remediation, a real detection took ~23
    real seconds and even self-reported "cleaning failed" despite the
    file ending up gone anyway -- an ambiguous signal not worth
    depending on. WITHOUT it, the same detection took ~84ms and left
    the file for OUR OWN code to delete, deterministically. A clean
    file scans in ~78ms either way. The trade-off, real and worth
    stating: -DisableRemediation also means the detection won't reach
    Defender's own event log or UI -- this module's caller is expected
    to log the real outcome durably itself (see workfiles.download_image),
    since that visibility doesn't come from Defender once this is set."""
    if not Path(_DEFENDER_PATH).is_file():
        return ScanResult("unavailable", detail="MpCmdRun.exe not found")
    try:
        proc = subprocess.run(
            [_DEFENDER_PATH, "-Scan", "-ScanType", "3", "-File", str(path), "-DisableRemediation"],
            capture_output=True, text=True, timeout=SCAN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return ScanResult("unavailable", detail=f"scan timed out after {SCAN_TIMEOUT_S:.0f}s")
    except OSError as exc:
        return ScanResult("unavailable", detail=f"couldn't run scanner: {exc}")
    if proc.returncode == 0:
        return ScanResult("clean")
    threat = _parse_defender_threat(proc.stdout)
    if threat:
        return ScanResult("infected", threat=threat, detail=proc.stdout[:2000])
    # Nonzero exit, no threat name parsed out -- documented Defender exit
    # code 2 covers BOTH "malware found" and "scan error" with no way to
    # tell them apart from the bare code. Never guess "clean" here.
    return ScanResult("unavailable", detail=f"scan exit {proc.returncode}: {(proc.stdout or '')[:500]}")


def _parse_defender_threat(stdout: str) -> str | None:
    m = _THREAT_RE.search(stdout or "")
    return m.group(1).strip() if m else None


def _scan_none(path: Path) -> ScanResult:
    """Explicit opt-out -- e.g. a Linux container with no Defender and
    no other engine wired in yet. Always "unavailable", never "clean":
    the only way a file gets promoted with this backend set is an
    operator ALSO setting NORI_SCANNER_ON_UNAVAILABLE=allow -- two
    deliberate choices, never a silent default."""
    return ScanResult("unavailable", detail="scanner backend is 'none' -- no scan performed")


_BACKENDS = {"defender": _scan_defender, "none": _scan_none}
