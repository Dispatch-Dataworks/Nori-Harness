# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Google Drive + OneDrive (work and personal), read-only for the model --
no write or delete tool exists in this file, on purpose, and that's a
code guarantee, not a scope one: Drive's token carries the full `drive`
scope and OneDrive's carries Files.ReadWrite (checked directly for
both -- neither provider has a narrower scope that permits editing files
but not deleting them), so nothing about either OAuth grant itself stops
a write tool from being added later. The actual guarantee is structural:
this module registers exactly four tools (list_files/read_file_content
for Drive, list_files_onedrive/read_file_content_onedrive for OneDrive),
all read-only, and nothing else in it is wired into tools.register() at
all.

Drive/OneDrive WRITE (2026-09-18, operator's own explicit design for
Drive, extended to OneDrive without a separate decision since he said to
follow the same precedent unless there's a reason not to -- there isn't)
is a UI action, not a tool -- he triggers move/copy from the files page
himself (server.py's files_page/files_drive_post/files_onedrive_post),
never something the model or a peer can invoke. find_or_create_folder/
_file_exists_in_folder/upload_bytes_to_drive below exist only to serve
those UI actions. If a future change wants Nori herself to write to
either, that's a real, separate decision to make deliberately -- don't
wire these into tools.register() to get there quietly.

onedrive_work and onedrive_personal are two separate connected_accounts
rows (2026-09-18, two OneDrive accounts he wanted reachable) under two
separate Microsoft app registrations, but the SAME Graph API shape --
every function below branches only on "is this google_drive or is this
one of the onedrive_* providers," never on which OneDrive account,
since connected_accounts.authed_request already resolves the right
token from the provider string alone.

read_file_content is the reader/actor split applied to a real
untrusted-content source (after email, Phase 6): a Drive/OneDrive file
is content someone else may have written, same risk class as an email
body, so it goes through ingest.summarize_untrusted() before the model
ever sees it, exactly like triage_email.
"""
from __future__ import annotations

import json
import urllib.parse
import uuid

import connected_accounts
import ingest

_DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
_DRIVE_FILE_URL = "https://www.googleapis.com/drive/v3/files/{id}"
_DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart"

_ONEDRIVE_ROOT_CHILDREN_URL = "https://graph.microsoft.com/v1.0/me/drive/root/children"
_ONEDRIVE_ITEM_URL = "https://graph.microsoft.com/v1.0/me/drive/items/{id}"
# Simple upload only (2026-09-18) -- Graph's PUT-content endpoint caps at
# 4MB; anything bigger needs a resumable upload session, deliberately not
# built here (proportionate to what was asked, flagged as a real limit
# rather than silently truncating or erroring confusingly).
_ONEDRIVE_MAX_SIMPLE_UPLOAD_BYTES = 4 * 1024 * 1024
_ONEDRIVE_PROVIDERS = ("onedrive_work", "onedrive_personal")

# Google Docs/Sheets/Slides have no raw bytes of their own -- they have to
# be exported to a real format first. Slides has no plain-text export;
# left out deliberately rather than returning something misleading.
_EXPORT_MIME = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
}

_MAX_READ_BYTES = 200_000   # a sane cap on what's worth pulling into a turn's context
_MAX_UPLOAD_BYTES = 25_000_000  # keeps a single in-memory multipart body sane for personal use


def _escape_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _list_files_impl(session: dict, query: str | None = None, limit: int = 25) -> dict:
    q = "trashed = false" if not query else f"name contains '{_escape_query(query)}' and trashed = false"
    params = {"q": q, "pageSize": min(int(limit), 100), "fields": "files(id,name,mimeType,modifiedTime,size)"}
    url = f"{_DRIVE_FILES_URL}?{urllib.parse.urlencode(params)}"
    result = connected_accounts.authed_request(session["user_id"], "google_drive", url)
    if not result.get("ok"):
        return {"error": result["error"]}
    # name rides in unscreened, same accepted gap list_calendar_events
    # already has for its own summary field -- a file listing is useless
    # without names, and summarize_untrusted is built for body content,
    # not a short label. Flagged, not silently different from that
    # precedent.
    return {"files": [{"id": f["id"], "name": f.get("name", ""), "type": f.get("mimeType", ""),
                       "modified": f.get("modifiedTime")} for f in result["data"].get("files", [])]}


def _read_file_content_impl(session: dict, file_id: str) -> dict:
    meta_result = connected_accounts.authed_request(
        session["user_id"], "google_drive", f"{_DRIVE_FILE_URL.format(id=file_id)}?fields=name,mimeType,size")
    if not meta_result.get("ok"):
        return {"error": meta_result["error"]}
    meta = meta_result["data"]
    name, mime = meta.get("name", "this file"), meta.get("mimeType", "")
    if mime in _EXPORT_MIME:
        url = f"{_DRIVE_FILE_URL.format(id=file_id)}/export?mimeType={urllib.parse.quote(_EXPORT_MIME[mime])}"
    elif mime.startswith("application/vnd.google-apps."):
        kind = mime.rsplit(".", 1)[-1]
        return {"error": f"'{name}' is a Google {kind} in a format this can't export as text"}
    else:
        size = int(meta.get("size") or 0)
        if size > _MAX_READ_BYTES:
            return {"error": f"'{name}' is {size:,} bytes -- too large to read directly "
                             f"(limit {_MAX_READ_BYTES:,})"}
        url = f"{_DRIVE_FILE_URL.format(id=file_id)}?alt=media"
    result = connected_accounts.authed_request(session["user_id"], "google_drive", url, raw_response=True)
    if not result.get("ok"):
        return {"error": result["error"]}
    raw = result["data"]
    if len(raw) > _MAX_READ_BYTES:
        return {"error": f"'{name}' is too large to read directly (limit {_MAX_READ_BYTES:,} bytes)"}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"error": f"'{name}' doesn't look like text -- can't read this format"}
    return ingest.summarize_untrusted(text, kind="drive_file")


def _onedrive_row(f: dict) -> dict:
    return {"id": f.get("id", ""), "name": f.get("name", ""),
           "type": "folder" if "folder" in f else (f.get("file") or {}).get("mimeType", ""),
           "modified": f.get("lastModifiedDateTime")}


def _list_files_onedrive_impl(session: dict, query: str | None = None, limit: int = 25,
                              provider: str = "onedrive_work") -> dict:
    if provider not in _ONEDRIVE_PROVIDERS:
        return {"error": "provider must be onedrive_work or onedrive_personal"}
    limit = min(int(limit), 100)
    if query:
        url = (f"https://graph.microsoft.com/v1.0/me/drive/root/search(q='{urllib.parse.quote(query)}')"
              f"?$top={limit}")
    else:
        url = f"{_ONEDRIVE_ROOT_CHILDREN_URL}?$top={limit}"
    result = connected_accounts.authed_request(session["user_id"], provider, url)
    if not result.get("ok"):
        return {"error": result["error"]}
    # name rides in unscreened, same accepted gap as Drive's own listing --
    # a file listing is useless without names.
    return {"files": [_onedrive_row(f) for f in result["data"].get("value", [])]}


def _read_file_content_onedrive_impl(session: dict, file_id: str, provider: str = "onedrive_work") -> dict:
    if provider not in _ONEDRIVE_PROVIDERS:
        return {"error": "provider must be onedrive_work or onedrive_personal"}
    meta_result = connected_accounts.authed_request(
        session["user_id"], provider, f"{_ONEDRIVE_ITEM_URL.format(id=file_id)}?$select=name,file,size")
    if not meta_result.get("ok"):
        return {"error": meta_result["error"]}
    meta = meta_result["data"]
    name = meta.get("name", "this file")
    if "file" not in meta:
        return {"error": f"'{name}' is a folder, not a file"}
    size = int(meta.get("size") or 0)
    if size > _MAX_READ_BYTES:
        return {"error": f"'{name}' is {size:,} bytes -- too large to read directly "
                         f"(limit {_MAX_READ_BYTES:,})"}
    url = f"{_ONEDRIVE_ITEM_URL.format(id=file_id)}/content"
    result = connected_accounts.authed_request(session["user_id"], provider, url, raw_response=True)
    if not result.get("ok"):
        return {"error": result["error"]}
    raw = result["data"]
    if len(raw) > _MAX_READ_BYTES:
        return {"error": f"'{name}' is too large to read directly (limit {_MAX_READ_BYTES:,} bytes)"}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"error": f"'{name}' doesn't look like text -- can't read this format "
                         f"(Office binary formats like .docx/.xlsx aren't supported)"}
    return ingest.summarize_untrusted(text, kind="onedrive_file")


# ── move/copy-to-OneDrive UI action support (2026-09-18) --------------------
# Called only from server.py's files-page handler -- see module docstring
# for why these are never tools.register()'d.
def find_or_create_folder_onedrive(session: dict, name: str, provider: str = "onedrive_work") -> dict:
    if provider not in _ONEDRIVE_PROVIDERS:
        return {"error": "provider must be onedrive_work or onedrive_personal"}
    odata_filter = "name eq '" + name.replace("'", "''") + "'"
    url = (f"{_ONEDRIVE_ROOT_CHILDREN_URL}?$filter={urllib.parse.quote(odata_filter)}"
          f"&$select=id,name,folder")
    result = connected_accounts.authed_request(session["user_id"], provider, url)
    if not result.get("ok"):
        return {"error": result["error"]}
    existing = [f for f in result["data"].get("value", []) if "folder" in f]
    if existing:
        return {"ok": True, "folder_id": existing[0]["id"]}
    created = connected_accounts.authed_request(
        session["user_id"], provider, _ONEDRIVE_ROOT_CHILDREN_URL, method="POST",
        body={"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})
    if not created.get("ok"):
        return {"error": created["error"]}
    return {"ok": True, "folder_id": created["data"]["id"]}


def _onedrive_file_exists_in_folder(session: dict, name: str, folder_id: str, provider: str) -> dict:
    odata_filter = "name eq '" + name.replace("'", "''") + "'"
    url = (f"https://graph.microsoft.com/v1.0/me/drive/items/{folder_id}/children"
          f"?$filter={urllib.parse.quote(odata_filter)}&$select=id")
    result = connected_accounts.authed_request(session["user_id"], provider, url)
    if not result.get("ok"):
        return {"error": result["error"]}
    return {"ok": True, "exists": bool(result["data"].get("value"))}


def upload_bytes_to_onedrive(session: dict, *, name: str, content: bytes, folder_id: str,
                             provider: str = "onedrive_work") -> dict:
    """Simple upload only -- capped well below Graph's own 4MB ceiling for
    that endpoint (larger needs a resumable upload session, not built
    here, same proportionate-scope call as everywhere else in this file).
    Never overwrites: a same-name collision in the destination folder is
    refused with a clear reason, same discipline as Drive's own upload."""
    if provider not in _ONEDRIVE_PROVIDERS:
        return {"error": "provider must be onedrive_work or onedrive_personal"}
    if len(content) > _ONEDRIVE_MAX_SIMPLE_UPLOAD_BYTES:
        return {"error": f"'{name}' is {len(content):,} bytes -- too large to upload to OneDrive "
                         f"this way (limit {_ONEDRIVE_MAX_SIMPLE_UPLOAD_BYTES:,})"}
    exists = _onedrive_file_exists_in_folder(session, name, folder_id, provider)
    if "error" in exists:
        return exists
    if exists["exists"]:
        return {"error": f"a file named '{name}' already exists in that OneDrive folder -- "
                         f"rename the file here first, or choose a different destination folder"}
    url = f"https://graph.microsoft.com/v1.0/me/drive/items/{folder_id}:/{urllib.parse.quote(name)}:/content"
    result = connected_accounts.authed_request(
        session["user_id"], provider, url, method="PUT", body=content, content_type="application/octet-stream")
    if not result.get("ok"):
        return {"error": result["error"]}
    return {"ok": True, "file_id": result["data"].get("id")}


# ── move/copy-to-Drive UI action support (2026-09-18) ----------------------
# Called only from server.py's files-page handler -- see module docstring
# for why these are never tools.register()'d.
def find_or_create_folder(session: dict, name: str) -> dict:
    q = (f"name = '{_escape_query(name)}' and mimeType = 'application/vnd.google-apps.folder' "
        f"and trashed = false")
    url = f"{_DRIVE_FILES_URL}?q={urllib.parse.quote(q)}&fields=files(id,name)"
    result = connected_accounts.authed_request(session["user_id"], "google_drive", url)
    if not result.get("ok"):
        return {"error": result["error"]}
    existing = result["data"].get("files", [])
    if existing:
        return {"ok": True, "folder_id": existing[0]["id"]}
    created = connected_accounts.authed_request(
        session["user_id"], "google_drive", _DRIVE_FILES_URL, method="POST",
        body={"name": name, "mimeType": "application/vnd.google-apps.folder"})
    if not created.get("ok"):
        return {"error": created["error"]}
    return {"ok": True, "folder_id": created["data"]["id"]}


def _file_exists_in_folder(session: dict, name: str, folder_id: str) -> dict:
    q = f"name = '{_escape_query(name)}' and '{folder_id}' in parents and trashed = false"
    url = f"{_DRIVE_FILES_URL}?q={urllib.parse.quote(q)}&fields=files(id)"
    result = connected_accounts.authed_request(session["user_id"], "google_drive", url)
    if not result.get("ok"):
        return {"error": result["error"]}
    return {"ok": True, "exists": bool(result["data"].get("files"))}


def upload_bytes_to_drive(session: dict, *, name: str, content: bytes, mime_type: str, folder_id: str) -> dict:
    """One multipart/related request, metadata + content together. Never
    overwrites: a same-name collision in the destination folder is
    refused with a clear reason rather than silently duplicated or
    overwritten -- Drive itself allows duplicate names, which is exactly
    why this checks rather than trusting that not to be confusing."""
    if len(content) > _MAX_UPLOAD_BYTES:
        return {"error": f"'{name}' is {len(content):,} bytes -- too large to upload "
                         f"(limit {_MAX_UPLOAD_BYTES:,})"}
    exists = _file_exists_in_folder(session, name, folder_id)
    if "error" in exists:
        return exists
    if exists["exists"]:
        return {"error": f"a file named '{name}' already exists in that Drive folder -- "
                         f"rename the file here first, or choose a different destination folder"}
    boundary = "nori-drive-" + uuid.uuid4().hex
    metadata = json.dumps({"name": name, "parents": [folder_id]}).encode("utf-8")
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode("utf-8")
           + metadata
           + f"\r\n--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n".encode("utf-8")
           + content
           + f"\r\n--{boundary}--".encode("utf-8"))
    result = connected_accounts.authed_request(
        session["user_id"], "google_drive", _DRIVE_UPLOAD_URL, method="POST",
        body=body, content_type=f"multipart/related; boundary={boundary}")
    if not result.get("ok"):
        return {"error": result["error"]}
    return {"ok": True, "file_id": result["data"].get("id")}


# ── tool registration (read-only) ─────────────────────────────────────────
def _register_tools() -> None:
    import tools  # local: same reasoning as every other subsystem here

    tools.register(tools.Tool(
        "list_files_drive",
        {"type": "function", "function": {
            "name": "list_files_drive",
            "description": "List or search files in a connected Google Drive (name, type, modified time).",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "optional -- matches against file name"},
                "limit": {"type": "integer", "default": 25}}}}},
        # peer_trust_gate (2026-09-18): Drive isn't his private
        # correspondence the way email is -- same bar as calendar/
        # contacts, open to a peer-motivated turn only once fully trusted.
        _list_files_impl, min_role="member", data_scope="self", risk_tier="A",
        owner_check=connected_accounts.peer_trust_gate))

    tools.register(tools.Tool(
        "read_file_content_drive",
        {"type": "function", "function": {
            "name": "read_file_content_drive",
            "description": ("Read a Drive file's text content safely -- runs it through the "
                            "untrusted-content summarizer rather than exposing raw content "
                            "directly. Google Docs/Sheets export to text/CSV; plain text files "
                            "read directly; other formats (Slides, images, binaries) aren't "
                            "supported."),
            "parameters": {"type": "object", "properties": {
                "file_id": {"type": "string"}},
                "required": ["file_id"]}}},
        _read_file_content_impl, min_role="member", data_scope="self", risk_tier="A",
        owner_check=connected_accounts.peer_trust_gate))

    tools.register(tools.Tool(
        "list_files_onedrive",
        {"type": "function", "function": {
            "name": "list_files_onedrive",
            "description": "List or search files in a connected OneDrive, work or personal (name, type, modified time).",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "optional -- matches against file name"},
                "limit": {"type": "integer", "default": 25},
                "provider": {"type": "string", "enum": list(_ONEDRIVE_PROVIDERS), "default": "onedrive_work"}}}}},
        _list_files_onedrive_impl, min_role="member", data_scope="self", risk_tier="A",
        owner_check=connected_accounts.peer_trust_gate))

    tools.register(tools.Tool(
        "read_file_content_onedrive",
        {"type": "function", "function": {
            "name": "read_file_content_onedrive",
            "description": ("Read a OneDrive file's text content safely -- runs it through the "
                            "untrusted-content summarizer rather than exposing raw content "
                            "directly. Plain text files read directly; Office binary formats "
                            "(.docx/.xlsx/.pptx), images, and other binaries aren't supported."),
            "parameters": {"type": "object", "properties": {
                "file_id": {"type": "string"},
                "provider": {"type": "string", "enum": list(_ONEDRIVE_PROVIDERS), "default": "onedrive_work"}},
                "required": ["file_id"]}}},
        _read_file_content_onedrive_impl, min_role="member", data_scope="self", risk_tier="A",
        owner_check=connected_accounts.peer_trust_gate))


_register_tools()
