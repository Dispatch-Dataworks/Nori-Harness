# Settings model

Source: `nori/config.py` (the only module running raw SQL against the
generic `settings` table) and `nori/settings_tool.py` (the tool that
lets her read them back).

## How a setting is declared

Every setting lives in one dict, `config._SPEC`, as a 5-tuple:
`(default, type, scope, secret, label)`.

- **default / type** — the value used until someone sets one, and the
  Python type (`bool`/`int`/`float`/`str`) used to coerce and validate
  a new value.
- **scope** — `"user"` (per-person: timezone, ping preferences, voice)
  or `"workspace"` (shared by the whole household: model choice, image
  generation budget, backup schedule). Declared once, here, rather than
  left to each call site to get right — `config.get`/`config.set` still
  take scope as an explicit argument for backward compatibility, but
  `scope_for(key)` is the single source of truth a new caller can check
  against.
- **secret** — see below. Required on every entry, not defaulted.
- **label** — a short, human phrase used when a setting is shown back
  to her (`check_integration_health`'s ilk) or to an admin, instead of
  the raw key.

## Deny-by-default secret exclusion

This is the one piece of this model worth understanding before adding
anything to it. `settings_tool.get_settings` — the tool that lets Nori
read her own configuration back — calls `config.readable_spec()`,
which returns **every entry NOT marked `secret=True`**. A new setting
added without thinking about this is excluded from her own read access
automatically, not exposed by omission. This is [enforced](enforcement-model.md),
not a convention: there's no separate denylist that a future addition
could just forget to update, because the exclusion is computed from
the same declaration every other property of that setting already
comes from.

In practice nothing in `_SPEC` today actually holds a secret — real
secrets live either as environment variables (never in this table at
all) or in their own dedicated encrypted column (`sub_agents.api_key_enc`,
`peers.psk_enc` — see [Auth & roles](auth-and-roles.md#secrets-at-rest)).
The `secret` flag exists so *the next* setting added here doesn't
become an accident by default.

## Reading and writing

`config.get(scope, scope_id, key)` / `config.set(scope, scope_id, key,
value)` are the only two functions that touch the table directly.
`set()` validates the incoming value against the declared type before
writing — a bad value is rejected with a real error, not silently
coerced or stored malformed. A few settings enforce an extra rule on
top of type-checking (`web_fetch_write_mode` can't jump straight from
`off` to `real` — it has to pass through `simulated` first, a
deliberate two-step transition; see [Content screening](content-screening.md)/
[Web search & fetch](web-search-and-fetch.md)).

## Her own read access

`settings_tool.py` registers `get_settings` (read, every non-secret key,
grouped by scope) and `update_settings` (write, one key at a time, same
validation `config.set` already does — never a bypass of it). Both are
member-level, `data_scope="self"` — she can read and change settings
for the account she's acting as, never another user's, and never a
workspace-scoped setting through this path if that would mean a member
silently changing something admin-facing (check the current tool
registration in `settings_tool.py` for exactly which keys that
excludes today, since this is exactly the kind of boundary worth
verifying against the source rather than trusting a paraphrase).

## Where a value actually shows up

The **Settings** page in the running app (`server.py`'s own
`_settings_groups`) is organized into the same "Personal"/
"Administration" split described in [Auth & roles](auth-and-roles.md) —
that page's own grouping is hand-maintained separately from `_SPEC`
(a setting's scope doesn't automatically decide which admin tab it
renders under), so the two can, in principle, drift; if you add a
setting, add its own form field too, in the same change.

**Every tab is one path with a query string, not a separate URL:**
`/settings?tab=webtools`, `/settings?tab=peers`, `/settings?tab=accounts`,
and so on — not `/webtools` or `/peers` as their own pages (a couple
of short aliases, like `/admin/peers`, exist and just redirect into
this same form). The canonical list of valid tabs lives in code, not
worth hand-copying here since it'll drift — see `settings_aliases` in
`server.py`.
