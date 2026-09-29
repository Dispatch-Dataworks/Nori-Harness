# Google & Microsoft

Source: `nori/oauth.py` (the generic OAuth2 flow and provider table),
`nori/connected_accounts.py` (storage and the one authenticated-request
chokepoint), `nori/email_calendar.py`, `nori/contacts.py`, `nori/drive.py`,
`nori/sharepoint.py`.

This page is both the setup walkthrough (exact redirect URIs, exact
scopes, exact console steps) and the code-level model — what's
actually connected, what tools exist, and which limits are enforced by
the granted OAuth scope versus only by which tool this app happens to
have built.

## How it fits together

Every provider is a row in `oauth.py`'s `PROVIDERS` dict: a label,
real OAuth endpoints, a scope string, and which two `.env` variable
names hold its client ID/secret. `connected_accounts.py` stores the
resulting tokens (encrypted at rest) and is the one place every
authenticated API call actually goes through. None of this needs code
changes to use — just credentials in `nori.env` and a restart. The
settings page (`/settings?tab=accounts`, admin) shows one row per
provider — "not configured" until you supply credentials, then a real
**connect** link once you do, independently per provider.

## Setting up Google (one app, one credential pair, four connections)

**1. Create a Google Cloud project.** [console.cloud.google.com](https://console.cloud.google.com/) →
create a new project (any name).

**2. Enable four APIs** — each is a separate toggle, even though one
OAuth app covers all of them. In the project's **APIs & Services →
Library**, search for and enable each of: Gmail API, Google Calendar
API, People API (this is what backs Google *Contacts* — there's no
separately-named "Contacts API"), Google Drive API.

**3. Configure the OAuth consent screen** (APIs & Services → OAuth
consent screen):
- User type: **External** (unless you have a Google Workspace org to
  restrict to **Internal** — External is right for a personal Gmail
  account). **This choice matters beyond setup: an External app in
  the default "Testing" status will silently stop working after 7
  days — see Troubleshooting's "Tokens expiring" below before you
  connect anything, not after it breaks.**
- App name/support email: anything real enough to pass Google's basic
  checks.
- **Add yourself as a test user**, under Audience/Test users — see
  Troubleshooting below for why this matters more than it looks.

**4. Create the OAuth client ID** (APIs & Services → Credentials →
Create Credentials → OAuth client ID → **Web application**).
Authorized redirect URIs — add all four, exactly (replace the
hostname if your `NORI_PUBLIC_URL` differs):
```
https://<your-hostname>/oauth/callback/gmail
https://<your-hostname>/oauth/callback/google_calendar
https://<your-hostname>/oauth/callback/google_contacts
https://<your-hostname>/oauth/callback/google_drive
```
Copy the **Client ID** and **Client secret**.

**5. Put them in `nori.env`:**
```
GOOGLE_CLIENT_ID=<client id>
GOOGLE_CLIENT_SECRET=<client secret>
```
One pair, shared across all four Google connections — not four
separate names.

**6. Restart nori**, sign in as admin, open `/settings?tab=accounts`.
Each Google row now shows **connect** instead of "not configured."
Click each one you want — they're independent; connecting Gmail
doesn't connect Drive.

## Setting up Microsoft (two app registrations, two credential pairs)

Unlike Google, this is **two separate Azure app registrations** — a
work Microsoft 365 tenant account and a personal Microsoft account
can't be connected through one shared app the way Google's four
products share one.

### App 1 — the work-tenant app (mail, calendar, contacts, work OneDrive, SharePoint)

1. [portal.azure.com](https://portal.azure.com) → **App registrations** → **New registration**. Name it anything.
2. Supported account types: **"Accounts in any organizational
   directory (Any Microsoft Entra ID tenant - Multitenant) and
   personal Microsoft accounts."** (Harmless left this way even though
   the personal account uses App 2 below — no need to narrow it.)
3. Redirect URI, platform **Web** — add all four:
   ```
   https://<your-hostname>/oauth/callback/outlook
   https://<your-hostname>/oauth/callback/outlook_contacts
   https://<your-hostname>/oauth/callback/onedrive_work
   https://<your-hostname>/oauth/callback/sharepoint
   ```
4. Register, then copy the **Application (client) ID** from the Overview page.
5. **Certificates & secrets → New client secret** → copy the **Value** immediately (shown once).
6. **API permissions → Add a permission → Microsoft Graph → Delegated
   permissions** — add all five: `Mail.ReadWrite`, `Calendars.ReadWrite`,
   `Contacts.ReadWrite`, `Files.ReadWrite`, `Sites.ReadWrite.All`.
   (`User.Read` is already there by default — leave it, basic sign-in
   profile only.)
7. **Click "Grant admin consent"** at the top of the API permissions
   page — fine to click yourself if you're the tenant's own admin.

### App 2 — the personal-account app (personal OneDrive only)

1. Same portal, **New registration** again — a second, separate app.
2. Supported account types: **"Personal Microsoft accounts only"** — different from App 1.
3. Redirect URI, platform **Web** — just one:
   ```
   https://<your-hostname>/oauth/callback/onedrive_personal
   ```
4. Same as App 1: copy the client ID, create a client secret, copy its value.
5. **API permissions → Add a permission → Microsoft Graph → Delegated** → add just `Files.ReadWrite`. No admin-consent click needed — a personal account consents for itself.

### Both credential pairs in `nori.env`

```
NORI_OUTLOOK_CLIENT_ID=<App 1 client id>
NORI_OUTLOOK_CLIENT_SECRET=<App 1 secret value>
NORI_OUTLOOK_PERSONAL_CLIENT_ID=<App 2 client id>
NORI_OUTLOOK_PERSONAL_CLIENT_SECRET=<App 2 secret value>
```

Restart, then connect each Microsoft row on `/settings?tab=accounts`
independently — `outlook`, `outlook_contacts`, `onedrive_work`, and
`sharepoint` all use App 1's credentials but are still four separate
connections; `onedrive_personal` uses App 2's and is its own
connection.

## Ten providers, one flow

`gmail`, `google_calendar`, `google_contacts`, `google_drive`,
`outlook`, `outlook_contacts`, `onedrive_work`, `onedrive_personal`,
`sharepoint`, `skylight` — each connects per-user (not workspace-wide;
each household member connects their own account) through the same
generic authorization-code exchange in `oauth.py`. `skylight`'s entry
is deliberately left with no real endpoints filled in — there's no
verified public OAuth spec for it available, and it may not even use
OAuth; the operator has to confirm the real mechanism before it can
work at all, rather than this app shipping plausible-looking guessed
URLs.

## Tokens

Stored encrypted at rest ([The enforcement model](enforcement-model.md),
same mechanism as the sub-agent roster's API keys) — access token,
refresh token, and expiry, per user per provider.
`connected_accounts.authed_request()` is the one place every real HTTP
call to any of these providers goes through: it refreshes proactively
(60 seconds before expiry, not only after a call fails on one) and
diagnoses a failure specifically rather than wrapping it in one
generic message — a 401 despite a locally-fresh-looking token means
the provider revoked access since the last refresh; a 403 means either
a missing scope or the API not being enabled on the console project,
and the provider's own error text says which. A revoked or expired
refresh token marks the account `needs_reconnect`, with the real
reason stored — not "reconnect" with no context, and not a later
turn's invented explanation for why email suddenly stopped working
(see [Confabulation](confabulation.md)).

## What each connection grants

| Connection | Scope | Grants |
|---|---|---|
| Gmail | `gmail.modify` | Read, label, move, draft. **Does not** exclude sending at the scope level — no send tool exists in code. |
| Google Calendar | `calendar.events` | Read/create/update events on the primary calendar only (not calendar-list management). |
| Google Contacts | `contacts` | Full read/write on contacts. No write tool exists in code. |
| Google Drive | `drive` (full, not `drive.file`) | Read/write/delete on every file the account can see. Read is a tool; write is a UI action only (the files page), never something she or a peer can trigger. |
| Outlook (mail) | `Mail.ReadWrite` | Read, categorize, draft. Sending is genuinely blocked by the provider itself — `Mail.Send` is a separate permission, never requested. |
| Outlook (calendar) | `Calendars.ReadWrite` | Read/create events. |
| Outlook Contacts | `Contacts.ReadWrite` | Full read/write. No write tool exists in code — same as Google Contacts. |
| OneDrive (work + personal) | `Files.ReadWrite` | Read/write/delete on that account's OneDrive. Read is a tool; write is a UI action only, same as Google Drive. |
| SharePoint | `Sites.ReadWrite.All` | Every site in the tenant the app can see — genuinely tenant-wide, an explicit choice (see below). |

## The scope-versus-code distinction, concretely

Every provider here requests a broader OAuth scope than the tools
built on top of it actually use — checked directly against each
provider's own permission reference, not assumed:

- **Gmail** requests `gmail.modify` (superset of read-only, covers
  drafting and label/move) — this scope *does* technically permit
  sending mail (Google has no scope that allows drafting but not
  sending). The real, enforced guarantee that `create_draft` can never
  send is code-only: there is no send tool registered anywhere in
  `email_calendar.py`. The scope *does not* permit permanent,
  bypass-Trash deletion — that needs the full `mail.google.com` scope,
  never requested — so that one guarantee actually is provider-enforced.
- **Google Contacts** requests the full `contacts` scope (no
  read+write-but-no-delete scope exists) — no delete tool is
  registered, a code-only guarantee.
- **Google Drive** requests the full `drive` scope, not the narrower
  `drive.file` (which would only see files this app itself created or
  ones explicitly picked via a file-open dialog — neither describes
  "read my existing Drive"). `drive.py` registers exactly four
  read-only tools (`list_files_drive`/`read_file_content_drive` for
  Drive, `list_files_onedrive`/`read_file_content_onedrive` for
  OneDrive) and nothing else — moving or copying a file is a UI action
  the operator triggers themselves from the files page, never
  something a model or peer can invoke. A future change wiring write
  access into a tool is a real, separate decision to make deliberately.
- **Outlook** requests `Mail.ReadWrite Calendars.ReadWrite` —
  Microsoft Graph, unlike Google, *does* separate sending
  (`Mail.Send`) as its own permission, never requested here, so "no
  send" for Outlook is a genuine provider-enforced guarantee, not just
  "no send function exists." Graph does *not* separate soft vs.
  permanent mail deletion the way Gmail does, so that guarantee is
  code-only, same caveat as Gmail's contacts/Drive equivalents.
- **SharePoint** requests `Sites.ReadWrite.All` — tenant-wide, a
  deliberate operator choice (every site in the tenant reachable, not
  a per-site allowlist via the narrower `Sites.Selected`, which is the
  one permission Graph itself enforces per-site). No delete tool
  exists in `sharepoint.py`; same code-only caveat, with a wider blast
  radius given the tenant-wide grant.

See [The enforcement model](enforcement-model.md) for the general
version of this split — provider-enforced vs. code-only vs. not
enforced at all is a real spectrum, not a binary.

## What's actually enforced, and by whom

The one thing worth internalizing rather than skimming:

| Property | Provider | Enforced by | Detail |
|---|---|---|---|
| Can't send email | Gmail | **Code only** | `gmail.modify` technically permits `messages.send`/`drafts.send` too — the only thing preventing it is that no function in `email_calendar.py` calls either. |
| Can't send email | Outlook | **Scope** | `Mail.ReadWrite` doesn't include `Mail.Send` at all — a separate permission, never requested. Real protection even if a send-shaped bug shipped. |
| Can't permanently delete mail | Gmail | **Scope** | Permanent (bypass-Trash) delete needs the full `mail.google.com` scope; `gmail.modify` can't do it regardless of what code exists. |
| Can't permanently delete mail | Outlook | **Code only** | Graph doesn't scope-separate soft vs. permanent delete — `Mail.ReadWrite` covers both. The only thing preventing it is that no delete tool exists. |
| Can't write/delete Drive or OneDrive files (from a tool) | Both | **Code only** | Both scopes (`drive` full, `Files.ReadWrite`) permit write and delete; only a *read* tool is registered for either. Write happens exclusively as a UI action on the files page — never something the model or a peer can trigger. |
| Can't write/delete Contacts | Both | **Code only** | Both scopes permit it; no write tool exists yet for either provider. |
| SharePoint reach is bounded to specific sites | SharePoint | **Not enforced at all** | `Sites.ReadWrite.All` is genuinely tenant-wide — every site the app can see, every SharePoint tool can reach. The narrower `Sites.Selected` permission *would* have given a real, Graph-enforced per-site boundary, but was traded away deliberately. No code-level narrowing exists either — `list_sharepoint_sites` is the only boundary a caller gets. |

## Tools

- **Email** (`email_calendar.py`) — `list_emails`, `triage_email`,
  `list_email_labels`, `modify_email_labels`, `create_draft` (never
  send). Blocked entirely for a peer-motivated turn
  (`connected_accounts.peer_blocked`) — email is private correspondence,
  held back regardless of trust level until the operator says otherwise.
- **Calendar** — `list_calendar_events`, `create_calendar_event`.
  Open to a peer-motivated turn only once that peer is fully trusted
  (`connected_accounts.peer_trust_gate`) — a lower bar than email, but
  still gated.
- **Contacts** (`contacts.py`) — `list_contacts`, `search_contacts`.
  Read-only; no write tool exists.
- **Drive/OneDrive** (`drive.py`) — `list_files_drive`,
  `read_file_content_drive`, `list_files_onedrive`,
  `read_file_content_onedrive`. Read-only, as above.
- **SharePoint** (`sharepoint.py`) — `list_sharepoint_sites`,
  `list_sharepoint_files`, `read_sharepoint_file_content`. Read-only.

## Untrusted content

Anything read back from these providers — an email body, a file's
contents, a calendar event's description — is external content a
person or system outside this app authored, and goes through the same
[content-screening](content-screening.md) discipline as email,
web pages, or Home Assistant state before it can influence a
tool-capable turn.

## Troubleshooting

- **`redirect_uri_mismatch` (Google) / `AADSTS50011` or similar
  (Microsoft)** — the redirect URI has to match **exactly**: same
  scheme (`https`), same host, same path, no trailing slash either
  side. Copy the URIs above verbatim rather than retyping them. If
  `NORI_PUBLIC_URL` changes (a new tunnel hostname), every provider's
  registered redirect URIs need updating to match, not just the env var.
- **Wrong account-type choice** — the work-tenant app must allow
  personal accounts *or* org accounts as configured above; a Microsoft
  app registered for a single specific tenant only will reject sign-in
  from any other account with an `AADSTS7000215`/`AADSTS50020`-style
  error. The personal-only app (App 2) will similarly reject a work
  account. If sign-in fails specifically at Microsoft's own login page
  (before it ever reaches Nori), this is the first thing to check.
- **Missing `NORI_PUBLIC_URL`** — the settings page's connect link
  fails outright with a specific message (`NORI_PUBLIC_URL isn't set —
  the operator needs to configure...`) rather than attempting a broken
  redirect. Set it in `nori.env` and restart.
- **Tokens expiring** — two different mechanisms, don't confuse them:
  - **Google, OAuth consent screen in "Testing" publishing status**
    (the default for a newly created app, and what step 3 above sets
    up): refresh tokens expire after **exactly 7 days**, on a fixed
    clock, regardless of use — not a bug, and not specific to this
    app; every Testing-status Google app works this way. **Every
    connection will die a week after connecting** unless you address
    this — this app tells you when it happens (a proactive ping, and a
    row on the accounts page — see below), but the account is still
    dead until you act. Three ways to actually deal with it, with a
    real tradeoff between them:
    - **Move the consent screen to "In production"** (Audience →
      Publishing status, in the same OAuth consent screen settings as
      step 3) removes the 7-day limit outright. **If your app's User
      type is External** (the default, and the right choice for a
      personal Gmail account — see step 3) **and it requests Gmail or
      Calendar scopes, moving to production may require Google's own
      verification review**, since those are classified as sensitive
      scopes — expect Google to ask for more than a click, potentially
      including a review turnaround. Know that going in rather than
      discovering it mid-flow.
    - **If you have a Google Workspace org, set the consent screen's
      User type to "Internal" instead of External** (step 3) — an
      Internal app has no 7-day Testing limit and never needs Google's
      verification review, regardless of which scopes it requests.
      This is the easier path if a Workspace org is available to you;
      it isn't available to a personal Gmail account.
    - **A hosted third-party connector** (a service that handles the
      Google OAuth app for you) sidesteps this entirely — no Google
      Cloud project of your own to manage, so no Testing limit and no
      verification review to think about. The honest tradeoff: your
      mail and calendar then pass through that connector's own
      infrastructure on the way to this app, not just between you and
      Google. Whether that's an acceptable exchange for not managing
      your own OAuth app is yours to weigh, not something to discover
      after the fact — this app doesn't ship a connection to one, on
      purpose, so making that choice is always a deliberate step you take.
  - **Microsoft**: refresh tokens are generally valid for a rolling
    ~90 days, extended on each use, but can be invalidated sooner by a
    password change, revoked consent, or Conditional Access policy.
  - Either way, this app surfaces the real state proactively, not just
    on request: a needs-reconnecting account triggers a ping (naming
    which account, and the likely Testing-status cause specifically
    when the evidence fits — see "Pings" in settings to enable/disable
    it like any other), and `/settings?tab=accounts` always shows the
    same diagnosis on its own row, not just "not connected." The reason
    shown came from the provider's own real error, not a guess.
    Reconnect from that page.
