# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""SharePoint (Microsoft Graph), read-only for the model -- no write or
delete tool exists in this file, on purpose, same code-not-scope shape as
drive.py: the connected token carries Sites.ReadWrite.All (2026-09-18,
operator's own explicit choice -- he wants every site in the tenant
reachable, not a Sites.Selected allowlist; see oauth.py's own comment for
the tradeoff that decision makes plainly, not silently). Nothing about
that scope stops a write tool from being added later; the actual
guarantee is structural: this module registers exactly three tools
(list_sharepoint_sites, list_sharepoint_files, read_sharepoint_file_content),
all read-only.

Write (2026-09-18) is a UI action, not a tool, same precedent as Drive/
OneDrive -- find_or_create_folder/upload_bytes_to_sharepoint below exist
only to serve server.py's files-page action, never wired into
tools.register().

Scope note (2026-09-18): every tool here reaches EVERY site
Sites.ReadWrite.All can see, tenant-wide -- there is no per-site
narrowing at the code level either, since that wasn't asked for. A site
picker in list_sharepoint_sites is the only boundary a caller gets.

read_sharepoint_file_content is the reader/actor split applied to a real
untrusted-content source, same as triage_email/read_file_content(_drive/
_onedrive): a document living in a SharePoint site may have been written
by anyone with access to that site, not just him.

Only a site's DEFAULT document library is read here (list_sharepoint_files/
read_sharepoint_file_content) -- most sites have exactly one that matters
in practice. A named-library picker for sites with multiple libraries
isn't built (not asked for, flagged rather than silently limited without
saying so).
"""
from __future__ import annotations

import urllib.parse

import connected_accounts
import ingest

_SITES_SEARCH_URL = "https://graph.microsoft.com/v1.0/sites"
_SITE_DRIVE_ROOT_CHILDREN_URL = "https://graph.microsoft.com/v1.0/sites/{site_id}/drive/root/children"
_SITE_DRIVE_ITEM_URL = "https://graph.microsoft.com/v1.0/sites/{site_id}/drive/items/{item_id}"

_MAX_READ_BYTES = 200_000
_MAX_UPLOAD_BYTES = 4 * 1024 * 1024  # same simple-upload ceiling as OneDrive, same reasoning


def _site_row(s: dict) -> dict:
    return {"site_id": s.get("id", ""), "name": s.get("displayName") or s.get("name") or "",
           "url": s.get("webUrl", "")}


def _list_sharepoint_sites_impl(session: dict, query: str | None = None, limit: int = 25) -> dict:
    # Graph's site search wants a real search term or the literal "*" for
    # "everything reachable" -- there's no bare "list all sites" verb.
    q = query if query else "*"
    params = {"search": q, "$top": min(int(limit), 100)}
    url = f"{_SITES_SEARCH_URL}?{urllib.parse.urlencode(params)}"
    result = connected_accounts.authed_request(session["user_id"], "sharepoint", url)
    if not result.get("ok"):
        return {"error": result["error"]}
    return {"sites": [_site_row(s) for s in result["data"].get("value", [])]}


def _sharepoint_row(f: dict) -> dict:
    return {"id": f.get("id", ""), "name": f.get("name", ""),
           "type": "folder" if "folder" in f else (f.get("file") or {}).get("mimeType", ""),
           "modified": f.get("lastModifiedDateTime")}


def _list_sharepoint_files_impl(session: dict, site_id: str, limit: int = 25) -> dict:
    params = {"$top": min(int(limit), 100)}
    url = f"{_SITE_DRIVE_ROOT_CHILDREN_URL.format(site_id=site_id)}?{urllib.parse.urlencode(params)}"
    result = connected_accounts.authed_request(session["user_id"], "sharepoint", url)
    if not result.get("ok"):
        return {"error": result["error"]}
    return {"files": [_sharepoint_row(f) for f in result["data"].get("value", [])]}


def _read_sharepoint_file_content_impl(session: dict, site_id: str, file_id: str) -> dict:
    meta_url = f"{_SITE_DRIVE_ITEM_URL.format(site_id=site_id, item_id=file_id)}?$select=name,file,size"
    meta_result = connected_accounts.authed_request(session["user_id"], "sharepoint", meta_url)
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
    content_url = f"{_SITE_DRIVE_ITEM_URL.format(site_id=site_id, item_id=file_id)}/content"
    result = connected_accounts.authed_request(session["user_id"], "sharepoint", content_url, raw_response=True)
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
    return ingest.summarize_untrusted(text, kind="sharepoint_file")


# ── move/copy-to-SharePoint UI action support (2026-09-18) ------------------
# Called only from server.py's files-page handler -- see module docstring
# for why these are never tools.register()'d.
def find_or_create_folder(session: dict, site_id: str, name: str) -> dict:
    odata_filter = "name eq '" + name.replace("'", "''") + "'"
    url = (f"{_SITE_DRIVE_ROOT_CHILDREN_URL.format(site_id=site_id)}?$filter="
          f"{urllib.parse.quote(odata_filter)}&$select=id,name,folder")
    result = connected_accounts.authed_request(session["user_id"], "sharepoint", url)
    if not result.get("ok"):
        return {"error": result["error"]}
    existing = [f for f in result["data"].get("value", []) if "folder" in f]
    if existing:
        return {"ok": True, "folder_id": existing[0]["id"]}
    created = connected_accounts.authed_request(
        session["user_id"], "sharepoint", _SITE_DRIVE_ROOT_CHILDREN_URL.format(site_id=site_id),
        method="POST", body={"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})
    if not created.get("ok"):
        return {"error": created["error"]}
    return {"ok": True, "folder_id": created["data"]["id"]}


def _file_exists_in_folder(session: dict, site_id: str, name: str, folder_id: str) -> dict:
    odata_filter = "name eq '" + name.replace("'", "''") + "'"
    url = (f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive/items/{folder_id}/children"
          f"?$filter={urllib.parse.quote(odata_filter)}&$select=id")
    result = connected_accounts.authed_request(session["user_id"], "sharepoint", url)
    if not result.get("ok"):
        return {"error": result["error"]}
    return {"ok": True, "exists": bool(result["data"].get("value"))}


def upload_bytes_to_sharepoint(session: dict, *, site_id: str, name: str, content: bytes, folder_id: str) -> dict:
    """Simple upload only, same 4MB ceiling and same reasoning as OneDrive's
    upload_bytes_to_onedrive -- a resumable-session upload for larger
    files isn't built here. Never overwrites: a same-name collision in
    the destination folder is refused with a clear reason."""
    if len(content) > _MAX_UPLOAD_BYTES:
        return {"error": f"'{name}' is {len(content):,} bytes -- too large to upload to SharePoint "
                         f"this way (limit {_MAX_UPLOAD_BYTES:,})"}
    exists = _file_exists_in_folder(session, site_id, name, folder_id)
    if "error" in exists:
        return exists
    if exists["exists"]:
        return {"error": f"a file named '{name}' already exists in that SharePoint folder -- "
                         f"rename the file here first, or choose a different destination folder"}
    url = (f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive/items/{folder_id}:/"
          f"{urllib.parse.quote(name)}:/content")
    result = connected_accounts.authed_request(
        session["user_id"], "sharepoint", url, method="PUT", body=content,
        content_type="application/octet-stream")
    if not result.get("ok"):
        return {"error": result["error"]}
    return {"ok": True, "file_id": result["data"].get("id")}


# ── tool registration (read-only) ─────────────────────────────────────────
def _register_tools() -> None:
    import tools  # local: same reasoning as every other subsystem here

    tools.register(tools.Tool(
        "list_sharepoint_sites",
        {"type": "function", "function": {
            "name": "list_sharepoint_sites",
            "description": ("Find SharePoint sites in the connected tenant by name -- omit query "
                            "for everything reachable. Returns each site's id (needed by the "
                            "other SharePoint tools), name, and URL."),
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "optional -- matches against site name"},
                "limit": {"type": "integer", "default": 25}}}}},
        # peer_trust_gate: same bar as Drive/OneDrive -- not his private
        # correspondence, open once a peer is fully trusted.
        _list_sharepoint_sites_impl, min_role="member", data_scope="self", risk_tier="A",
        owner_check=connected_accounts.peer_trust_gate))

    tools.register(tools.Tool(
        "list_sharepoint_files",
        {"type": "function", "function": {
            "name": "list_sharepoint_files",
            "description": ("List files in a SharePoint site's default document library. Use "
                            "list_sharepoint_sites first for a real site_id."),
            "parameters": {"type": "object", "properties": {
                "site_id": {"type": "string"}, "limit": {"type": "integer", "default": 25}},
                "required": ["site_id"]}}},
        _list_sharepoint_files_impl, min_role="member", data_scope="self", risk_tier="A",
        owner_check=connected_accounts.peer_trust_gate))

    tools.register(tools.Tool(
        "read_sharepoint_file_content",
        {"type": "function", "function": {
            "name": "read_sharepoint_file_content",
            "description": ("Read a SharePoint file's text content safely -- runs it through the "
                            "untrusted-content summarizer rather than exposing raw content "
                            "directly. Plain text files read directly; Office binary formats "
                            "aren't supported."),
            "parameters": {"type": "object", "properties": {
                "site_id": {"type": "string"}, "file_id": {"type": "string"}},
                "required": ["site_id", "file_id"]}}},
        _read_sharepoint_file_content_impl, min_role="member", data_scope="self", risk_tier="A",
        owner_check=connected_accounts.peer_trust_gate))


_register_tools()
