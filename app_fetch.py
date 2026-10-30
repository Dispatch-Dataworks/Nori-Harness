# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""The Android app reaches the phone through the server (2026-10-29, operator's own ask: "if my servers are running the release, why don't they
have the latest apk?"). A paired phone updates from `GET /device/app.apk`, which serves data/phone_app/nori-companion.apk. Until now that file only
got there when someone ran `android_app/build.ps1 -Upload` against one container, so a server that was updated to a release still had no app to offer.
scripts/release.ps1 attaches the APK to every release; this module is the other half: when the daily update check (updates.py) shows a release whose
app is newer than the one this server offers, the server downloads it and offers it. Automatic, with a switch (Settings -> Updates) and a "fetch now".

What it will and will not do, because it downloads a file that other devices then install:
  * One HTTPS GET to the asset URL stored by updates.py, which accepted it only if it is exactly
    https://github.com/<repo>/releases/download/<tag>/nori-companion-<version>.apk. Redirects (GitHub serves assets from *.githubusercontent.com)
    are followed only to github.com and *.githubusercontent.com over https.
  * Never more than MAX_APK_BYTES, never longer than DOWNLOAD_DEADLINE_S; must match the SHA-256 GitHub published for the asset when it published one;
    must open as a zip with an AndroidManifest.xml and a classes*.dex and pass the zip CRC test. A file failing any of this is thrown away.
  * Never downgrades and never re-downloads the version it already has. A failure is recorded, shown on the Updates tab, and retried after
    RETRY_AFTER_FAILURE_S, not every cycle.
  * The APK is written beside the old one and moved into place (apk first, version.txt second), so a phone never sees a version the file does not match.
  * It cannot make a phone install anything the phone would not otherwise accept: Android only updates an app in place from an APK signed with the same
    key as the installed one, and the installer is still the person tapping "update" in the app.
Like the update check itself it never touches Nori's own code, git or Docker."""
from __future__ import annotations

import hashlib
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import config
import devices
import updates

DOWNLOAD_TIMEOUT_S = 30
DOWNLOAD_DEADLINE_S = 180
RETRY_AFTER_FAILURE_S = 6 * 3600
_CHUNK = 64 * 1024


# ── settings ─────────────────────────────────────────────────────────────
def _active_workspaces() -> list[int]:
    """Workspaces that want it: the update check is on, and so is the app fetch."""
    return [w for w in updates._workspace_ids()
            if updates.enabled(w) and config.get("workspace", w, "phone_app_auto_fetch")]


def _include_prereleases(workspaces: list[int]) -> bool:
    return any(config.get("workspace", w, "update_include_prereleases") for w in workspaces)


# ── what is on offer ─────────────────────────────────────────────────────
def wanted(workspaces: list[int] | None = None) -> dict | None:
    """The newest app among the stored releases (respecting the pre-release switch), as {"version", "name", "url", "size", "digest", "tag"}, or None."""
    ws = _active_workspaces() if workspaces is None else workspaces
    if not ws:
        return None
    include_pre = _include_prereleases(ws)
    best = None
    for rel in updates._row()["releases"]:
        if rel.get("prerelease") and not include_pre:
            continue
        for a in rel.get("assets") or []:
            m = updates.APK_ASSET_RE.match(a.get("name", ""))
            key = updates.parse_version(m.group(1)) if m else None
            if key and (best is None or key > best[0]):
                best = (key, {"version": m.group(1), "tag": rel["tag"], **a})
    return best[1] if best else None


def _newer_than_offered(version: str) -> bool:
    have = devices.apk_version()
    have_key = updates.parse_version(have) if have and devices.apk_path() else None
    return have_key is None or updates.parse_version(version) > have_key


# ── the record of the last attempt ───────────────────────────────────────
def _state_path():
    return devices.apk_dir() / "fetch.json"


def _state() -> dict:
    try:
        d = json.loads(_state_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _record(**fields) -> None:
    try:
        devices.apk_dir().mkdir(parents=True, exist_ok=True)
        tmp = _state_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(fields), encoding="utf-8")
        os.replace(tmp, _state_path())
    except OSError:
        pass  # the record is a courtesy; the fetch itself already succeeded or failed


# ── the download ─────────────────────────────────────────────────────────
def host_ok(url: str) -> bool:
    try:
        p = urllib.parse.urlsplit(url)
        host = (p.hostname or "").lower()
    except ValueError:
        return False
    return p.scheme == "https" and not p.username and (host == "github.com" or host.endswith(".githubusercontent.com"))


class _GithubOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not host_ok(newurl):
            raise urllib.error.URLError(f"refusing a redirect to {urllib.parse.urlsplit(newurl).hostname or 'somewhere unexpected'}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url: str, limit: int) -> bytes:
    """The bytes at `url`, or OSError/ValueError. The seam tests replace."""
    if not host_ok(url):
        raise ValueError("not a GitHub download address")
    opener = urllib.request.build_opener(_GithubOnlyRedirects)
    req = urllib.request.Request(url, headers={"User-Agent": "Nori-app-fetch", "Accept": "application/octet-stream"})
    deadline = time.time() + DOWNLOAD_DEADLINE_S
    chunks, total = [], 0
    with opener.open(req, timeout=DOWNLOAD_TIMEOUT_S) as r:
        while True:
            b = r.read(_CHUNK)
            if not b:
                break
            total += len(b)
            if total > limit:
                raise ValueError("the download is larger than the size GitHub listed")
            if time.time() > deadline:
                raise OSError("the download took too long")
            chunks.append(b)
    return b"".join(chunks)


def check_apk(data: bytes, size: int, digest: str) -> str | None:
    """Why this is not the file it should be, or None."""
    if len(data) != size:
        return f"the file is {len(data)} bytes, GitHub listed {size}"
    if digest and "sha256:" + hashlib.sha256(data).hexdigest() != digest:
        return "the file does not match the checksum GitHub published"
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
            if "AndroidManifest.xml" not in names or not any(n.startswith("classes") and n.endswith(".dex") for n in names):
                return "the file is not an Android app"
            if z.testzip() is not None:
                return "the file is damaged"
    except zipfile.BadZipFile:
        return "the file is not an Android app"
    return None


def _install(data: bytes, version: str) -> None:
    d = devices.apk_dir()
    d.mkdir(parents=True, exist_ok=True)
    apk, ver = d / "nori-companion.apk", d / "version.txt"
    tmp_apk, tmp_ver = d / "nori-companion.apk.tmp", d / "version.txt.tmp"
    tmp_apk.write_bytes(data)
    tmp_ver.write_text(version, encoding="utf-8")
    os.replace(tmp_apk, apk)   # the file first, then the version that names it
    os.replace(tmp_ver, ver)


def fetch(want: dict) -> dict:
    """Download, check and install `want` (from wanted()). Never raises; {"ok", "version", "message"}, also recorded for the Updates tab."""
    version = want["version"]
    try:
        data = _download(want["url"], min(want["size"], updates.MAX_APK_BYTES))
        problem = check_apk(data, want["size"], want.get("digest") or "")
        if problem:
            raise ValueError(problem)
        _install(data, version)
    except (OSError, ValueError) as exc:
        msg = (str(exc) or exc.__class__.__name__)[:200]
        _record(ts=time.time(), ok=False, version=version, error=msg, next_try_ts=time.time() + RETRY_AFTER_FAILURE_S)
        return {"ok": False, "version": version, "message": msg}
    _record(ts=time.time(), ok=True, version=version, error="", next_try_ts=0)
    return {"ok": True, "version": version, "message": f"phones are now offered app {version}"}


# ── the hooks ────────────────────────────────────────────────────────────
def maybe_fetch() -> bool:
    """The scheduler's hook, after the update check: fetch the newest app if it is newer than the one on offer and a failed attempt is not still
    cooling down. True if a download was attempted. Cheap when there is nothing to do (it reads the stored release list)."""
    want = wanted()
    if want is None or not _newer_than_offered(want["version"]):
        return False
    st = _state()
    if st.get("version") == want["version"] and not st.get("ok") and time.time() < float(st.get("next_try_ts") or 0):
        return False
    fetch(want)
    return True


def fetch_now(workspace_id: int) -> dict:
    """The admin's button: look at the stored releases (checking GitHub first if there are none), and fetch the newest app regardless of the cooldown
    or of the switch. {"ok", "message"}."""
    if not updates._row()["releases"]:
        res = updates.check()
        if not res["ok"]:
            return {"ok": False, "message": f"couldn't reach GitHub: {res['error']}"}
    want = wanted([workspace_id])
    if want is None:
        return {"ok": False, "message": "no release has an Android app attached"}
    if not _newer_than_offered(want["version"]):
        return {"ok": True, "message": f"this server already offers app {want['version']}"}
    return fetch(want)


def status(workspace_id: int) -> dict:
    """For the Updates tab."""
    want = wanted([workspace_id])
    st = _state()
    return {"auto": bool(config.get("workspace", workspace_id, "phone_app_auto_fetch")), "offered": devices.apk_version() if devices.apk_path() else None,
            "newest": want["version"] if want else None, "last": st if st else None}
