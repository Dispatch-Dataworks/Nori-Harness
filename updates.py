# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

""""A newer Nori is available" (2026-10-26).

Anyone can self-host this, so a fix pushed to GitHub has to reach them: once a day Nori asks GitHub's releases API whether a release newer than the
one running exists, and shows the admin a chip in the header and an Updates tab (release notes + the exact commands). That is ALL it does.

  * It never changes its own code, never restarts itself, never touches git or the Docker socket. Updating is a person running two commands
    (docs/updates.md). There is no remote-code path in this feature, by design.
  * Only a *published GitHub Release* counts (a tag you cut on purpose): pushes to main never prompt anyone. Drafts are ignored; pre-releases count
    unless the admin turns that off (the project is in beta, so they are on by default).
  * What leaves the server: one HTTPS GET to api.github.com (the releases list, conditional on the last ETag, so an unchanged answer is a bodiless
    304). GitHub sees the server's IP and a "Nori-update-check" user agent. No instance id, no version, no data. An admin can switch it off.
  * Release notes come from GitHub, so they are untrusted TEXT: stored capped, rendered escaped, never as HTML; the link is only shown if it points
    at https://github.com/<repo>/.

The running version is the VERSION file in the repo root (baked into the image by the build, since .git is not). A release is: bump VERSION, tag it
v<VERSION>, publish a GitHub Release (docs/updates.md, "Cutting a release")."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import config
import store

HERE = Path(__file__).resolve().parent
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_DEFAULT_REPO = "Dispatch-Dataworks/Nori-Harness"
CHECK_EVERY_S = 24 * 3600          # one question a day
RETRY_AFTER_ERROR_S = 6 * 3600     # a failure isn't hammered
HTTP_TIMEOUT_S = 10
MAX_BODY_BYTES = 1_000_000
NOTES_CAP = 4000
KEEP_RELEASES = 10


def repo() -> str:
    """owner/name of the project whose releases are watched; a fork can point at its own with NORI_UPDATE_REPO."""
    r = (os.environ.get("NORI_UPDATE_REPO") or "").strip()
    return r if _REPO_RE.match(r) else _DEFAULT_REPO


def api_url() -> str:
    return f"https://api.github.com/repos/{repo()}/releases?per_page={KEEP_RELEASES}"


def current_version() -> str:
    v = (os.environ.get("NORI_VERSION") or "").strip()
    if not v:
        try:
            v = (HERE / "VERSION").read_text(encoding="utf-8").strip()
        except OSError:
            v = ""
    return v if parse_version(v) else "0.0.0-unknown"


# ── versions ────────────────────────────────────────────────────────────
_VER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


def parse_version(s) -> tuple | None:
    """A sortable key for a semantic version ("v1.2.3", "1.2.3-beta.2"), or None. A release outranks its own pre-releases; pre-release
    identifiers compare numerically when both are numbers, and a numeric one is lower than a word."""
    m = _VER_RE.match((s or "").strip()) if isinstance(s, str) else None
    if not m:
        return None
    major, minor, patch, pre = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
    if pre is None:
        return (major, minor, patch, 1, ())
    ids = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return (major, minor, patch, 0, ids)


def is_prerelease_version(s) -> bool:
    k = parse_version(s)
    return bool(k) and k[3] == 0


# ── the stored answer ─────────────────────────────────────────────────────
def _row() -> dict:
    r = store.read(lambda c: c.execute("SELECT * FROM update_check WHERE id=1").fetchone())
    if r is None:
        return {"checked_ts": 0.0, "next_check_ts": 0.0, "etag": "", "releases": [], "error": ""}
    d = dict(r)
    try:
        d["releases"] = json.loads(d.get("releases") or "[]")
    except (ValueError, TypeError):
        d["releases"] = []
    return d


def _save(**fields) -> None:
    cur = _row()
    cur.update(fields)
    store.write(lambda c: c.execute(
        "INSERT INTO update_check(id, checked_ts, next_check_ts, etag, releases, error) VALUES (1,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET checked_ts=excluded.checked_ts, next_check_ts=excluded.next_check_ts, "
        "etag=excluded.etag, releases=excluded.releases, error=excluded.error",
        (cur["checked_ts"], cur["next_check_ts"], cur["etag"], json.dumps(cur["releases"]), cur["error"])))


def _clean_releases(raw) -> list[dict]:
    """GitHub's list, reduced to what is shown, and only entries that are real published releases with a version tag."""
    out = []
    base = f"https://github.com/{repo()}/"
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict) or r.get("draft"):
            continue
        tag = r.get("tag_name")
        if not isinstance(tag, str) or not parse_version(tag):
            continue
        url = r.get("html_url") if isinstance(r.get("html_url"), str) else ""
        out.append({"tag": tag[:40], "name": str(r.get("name") or tag)[:120], "prerelease": bool(r.get("prerelease")),
                    "url": url if url.startswith(base) else "", "published": str(r.get("published_at") or "")[:20],
                    "notes": str(r.get("body") or "")[:NOTES_CAP]})
    return out[:KEEP_RELEASES]


# ── asking GitHub ─────────────────────────────────────────────────────────
def _fetch(etag: str) -> tuple[int, str, list | None]:
    """(status, etag, parsed JSON or None). 304 = unchanged. Network and HTTP errors are raised as OSError/ValueError for check() to record."""
    req = urllib.request.Request(api_url(), headers={
        "Accept": "application/vnd.github+json", "User-Agent": "Nori-update-check", "X-GitHub-Api-Version": "2022-11-28",
        **({"If-None-Match": etag} if etag else {})})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
            raw = r.read(MAX_BODY_BYTES + 1)
            if len(raw) > MAX_BODY_BYTES:
                raise ValueError("GitHub's answer was unexpectedly large")
            return r.status, r.headers.get("ETag") or "", json.loads(raw)
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return 304, etag, None
        raise OSError(f"GitHub answered {exc.code}" + (" (rate limited -- will retry later)" if exc.code in (403, 429) else "")) from exc


def check() -> dict:
    """Ask GitHub now. Never raises: the outcome (new list, unchanged, or the error) is recorded and returned as status fields."""
    now = time.time()
    cur = _row()
    try:
        status, etag, data = _fetch(cur.get("etag") or "")
    except (OSError, ValueError) as exc:
        msg = (str(exc) or exc.__class__.__name__)[:200]
        _save(checked_ts=now, next_check_ts=now + RETRY_AFTER_ERROR_S, error=msg)
        return {"ok": False, "error": msg}
    if status == 304:
        _save(checked_ts=now, next_check_ts=now + CHECK_EVERY_S, error="")
        return {"ok": True, "changed": False}
    _save(checked_ts=now, next_check_ts=now + CHECK_EVERY_S, etag=etag, releases=_clean_releases(data), error="")
    return {"ok": True, "changed": True}


def _workspace_ids() -> list[int]:
    return [r["id"] for r in store.read(lambda c: c.execute("SELECT id FROM workspaces").fetchall())]


def enabled(workspace_id: int) -> bool:
    return bool(config.get("workspace", workspace_id, "update_check_enabled"))


def maybe_check() -> bool:
    """The scheduler's hook: ask GitHub when it's due AND at least one workspace has the check on. True if a request was made."""
    if not any(enabled(w) for w in _workspace_ids()):
        return False
    if time.time() < _row().get("next_check_ts", 0.0):
        return False
    check()
    return True


# ── what the UI shows ─────────────────────────────────────────────────────
def status(workspace_id: int) -> dict:
    row = _row()
    include_pre = bool(config.get("workspace", workspace_id, "update_include_prereleases"))
    cur = current_version()
    cur_key = parse_version(cur)
    candidates = [r for r in row["releases"] if include_pre or not r["prerelease"]]
    best = max(candidates, key=lambda r: parse_version(r["tag"]), default=None)
    available = bool(best and cur_key and parse_version(best["tag"]) > cur_key)
    dismissed = config.get("workspace", workspace_id, "update_dismissed_version") or ""
    return {"enabled": enabled(workspace_id), "include_prereleases": include_pre, "current": cur,
            "latest": best["tag"] if best else None, "release": best, "available": available,
            "dismissed": bool(available and dismissed == best["tag"]),
            "checked_ts": row.get("checked_ts") or 0.0, "error": row.get("error") or "", "repo": repo()}


def banner_for(workspace_id: int) -> dict | None:
    """The release to announce in the header, or None: only while the check is on, a newer release exists, and the admin hasn't skipped it."""
    if not enabled(workspace_id):
        return None
    st = status(workspace_id)
    return st["release"] if st["available"] and not st["dismissed"] else None


def dismiss(workspace_id: int) -> bool:
    """Skip the release currently on offer (a newer one will prompt again)."""
    st = status(workspace_id)
    if not st["available"]:
        return False
    config.set("workspace", workspace_id, "update_dismissed_version", st["latest"])
    return True


def update_steps(tag: str | None) -> dict:
    """The commands for the two ways people run Nori. `tag` has already been validated as a version tag, so it is safe to print in a command."""
    target = tag if tag and parse_version(tag) else None
    checkout = f"git checkout {target}" if target else "git pull"
    return {
        "docker": ["git fetch --tags", checkout, "docker compose up -d --build"],
        "plain": ["git fetch --tags", checkout, "pip install -r requirements.txt", "pwsh nori_ctl.ps1 restart    # or restart however you run it"],
    }
