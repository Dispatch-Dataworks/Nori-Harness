# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Google Contacts (People API) + Outlook Contacts (Microsoft Graph),
read-only -- least-privilege default, same reasoning as Gmail's read-only
scope was originally: nothing here can add, edit, or delete a contact,
and no such tool exists for either provider (2026-09-18: Google widened
to Contacts.ReadWrite/contacts scope, but no write tool followed that
widening -- same for Outlook's Contacts.ReadWrite here, first build.
Widen later if he wants write access, not a decision made here). Same
OAuth/connected_accounts machinery as email_calendar.py; see
connected_accounts.authed_request()'s own docstring for what "not
connected"/"needs reconnecting"/a real API error each look like from a
tool's own result.
"""
from __future__ import annotations

import urllib.parse

import connected_accounts

_PEOPLE_LIST_URL = "https://people.googleapis.com/v1/people/me/connections"
_PEOPLE_SEARCH_URL = "https://people.googleapis.com/v1/people:searchContacts"
_PERSON_FIELDS = "names,emailAddresses,phoneNumbers"
_OUTLOOK_CONTACTS_URL = "https://graph.microsoft.com/v1.0/me/contacts"


def _person_row(p: dict) -> dict:
    names = p.get("names") or [{}]
    emails = [e.get("value") for e in (p.get("emailAddresses") or []) if e.get("value")]
    phones = [ph.get("value") for ph in (p.get("phoneNumbers") or []) if ph.get("value")]
    return {"resource_name": p.get("resourceName", ""), "name": names[0].get("displayName", ""),
           "emails": emails, "phones": phones}


def _outlook_contact_row(p: dict) -> dict:
    emails = [e.get("address") for e in (p.get("emailAddresses") or []) if e.get("address")]
    phones = [ph for ph in ((p.get("businessPhones") or []) + ([p["mobilePhone"]] if p.get("mobilePhone") else []))]
    return {"resource_name": p.get("id", ""), "name": p.get("displayName", "") or "",
           "emails": emails, "phones": phones}


def _list_contacts_impl(session: dict, limit: int = 25, provider: str = "google_contacts") -> dict:
    if provider not in ("google_contacts", "outlook_contacts"):
        return {"error": "provider must be google_contacts or outlook_contacts"}
    if provider == "google_contacts":
        url = f"{_PEOPLE_LIST_URL}?personFields={_PERSON_FIELDS}&pageSize={min(int(limit), 100)}"
        result = connected_accounts.authed_request(session["user_id"], "google_contacts", url)
        if not result.get("ok"):
            return {"error": result["error"]}
        return {"contacts": [_person_row(p) for p in result["data"].get("connections", [])]}
    params = {"$top": min(int(limit), 100)}
    url = f"{_OUTLOOK_CONTACTS_URL}?{urllib.parse.urlencode(params)}"
    result = connected_accounts.authed_request(session["user_id"], "outlook_contacts", url)
    if not result.get("ok"):
        return {"error": result["error"]}
    return {"contacts": [_outlook_contact_row(p) for p in result["data"].get("value", [])]}


def _search_contacts_impl(session: dict, query: str, provider: str = "google_contacts") -> dict:
    if provider not in ("google_contacts", "outlook_contacts"):
        return {"error": "provider must be google_contacts or outlook_contacts"}
    if provider == "google_contacts":
        url = f"{_PEOPLE_SEARCH_URL}?query={urllib.parse.quote(query)}&readMask={_PERSON_FIELDS}"
        result = connected_accounts.authed_request(session["user_id"], "google_contacts", url)
        if not result.get("ok"):
            return {"error": result["error"]}
        return {"contacts": [_person_row(r.get("person", {})) for r in result["data"].get("results", [])]}
    # $filter, not $search (2026-09-18) -- $search needs an eventual-
    # consistency header on Graph; startswith covers the common "find
    # someone by name" case without that extra plumbing.
    q = _escape_odata(query)
    params = {"$filter": f"startswith(displayName,'{q}')"}
    url = f"{_OUTLOOK_CONTACTS_URL}?{urllib.parse.urlencode(params)}"
    result = connected_accounts.authed_request(session["user_id"], "outlook_contacts", url)
    if not result.get("ok"):
        return {"error": result["error"]}
    return {"contacts": [_outlook_contact_row(p) for p in result["data"].get("value", [])]}


def _escape_odata(value: str) -> str:
    return value.replace("'", "''")


# ── tool registration ────────────────────────────────────────────────────
def _register_tools() -> None:
    import tools  # local: same reasoning as every other subsystem here

    tools.register(tools.Tool(
        "list_contacts",
        {"type": "function", "function": {
            "name": "list_contacts",
            "description": "List contacts (name, email, phone) from a connected Google or Outlook account.",
            "parameters": {"type": "object", "properties": {
                "limit": {"type": "integer", "default": 25},
                "provider": {"type": "string", "enum": ["google_contacts", "outlook_contacts"],
                            "default": "google_contacts"}}}}},
        # peer_trust_gate (2026-09-18, proposed rather than assumed):
        # lower-stakes than email -- open to a peer-motivated turn once
        # that specific peer is fully trusted. See connected_accounts.py.
        _list_contacts_impl, min_role="member", data_scope="self", risk_tier="A",
        owner_check=connected_accounts.peer_trust_gate))

    tools.register(tools.Tool(
        "search_contacts",
        {"type": "function", "function": {
            "name": "search_contacts",
            "description": "Search contacts by name from a connected Google or Outlook account.",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string"},
                "provider": {"type": "string", "enum": ["google_contacts", "outlook_contacts"],
                            "default": "google_contacts"}},
                "required": ["query"]}}},
        _search_contacts_impl, min_role="member", data_scope="self", risk_tier="A",
        owner_check=connected_accounts.peer_trust_gate))


_register_tools()
