# Computer use (browser bridge)

> Computer use is a section of **Settings → Devices** (there is no separate tab): the browser you pair, its own switches, and the
> master switch / session / allowed sites / approvals / log all live on that one page. See [Devices](devices.md).

Lets Nori see and use **your own browser** — your real tabs and logged-in sessions — through a small Chrome extension, only while you have started a session, only on sites you have listed. Off by default. Phase 1 is the browser; other desktop apps (screen capture + mouse/keyboard via a host-side agent) are a later phase and are not built.

## How it fits together

```
Chrome (your PC)                         Nori container
┌──────────────────────┐   long-poll    ┌──────────────────────────┐
│ browser_extension/   │ ─────────────▶ │ POST /bridge/poll        │
│  background.js       │ ◀───────────── │   bearer token, no cookie│
│  runs 1 command at a │   command      │ bridge.py                │
│  time in an allowed  │                │  queue = audit log       │
│  tab                 │ ─────────────▶ │ POST /bridge/result      │
└──────────────────────┘    answer      │  browser_* tools         │
                                        └──────────────────────────┘
```

The extension connects **out** to Nori (so nothing on your PC listens for connections) with a revocable bearer token. Chrome no longer allows remote-debugging your normal profile, which is why this is an extension rather than DevTools automation.

## Setup (once)

1. `docker compose up -d --build`.
2. *Settings → Devices*: under **Browser**, **pair a browser** (a one-time code; or the "advanced" token option); under **Computer use** on the same page, tick **allow computer use on this instance** and add the **allowed sites** (e.g. `*.github.com`).
3. In Chrome: *Settings → Devices → Browser → download the extension* (a zip served by Nori; unzip it somewhere permanent), then `chrome://extensions` → Developer mode → **Load unpacked** → the unzipped `nori-extension` folder (or the repo's `browser_extension` folder). Open the extension's options and paste Nori's address (e.g. `http://127.0.0.1:8877`) and the token. The popup shows "idle (no session)" when it's connected.

## Using it

*Settings → Devices → Computer use → start a session* (15 / 30 / 60 / 120 minutes). Only then do the `browser_*` tools exist for Nori. **STOP NOW** ends the session immediately and cancels everything pending; the extension popup's **Pause** stops it from your side. A session also ends when its time is up, or if you switch the master setting off.

Tools: `browser_tabs`, `browser_open`, `browser_read`, `browser_click`, `browser_type`, `browser_scroll`, `browser_screenshot`.

## Standing look (read-only, no session)

Settings → Devices → Computer use → **standing look** lets her use the *read-only* tools -- `browser_tabs`, `browser_read`,
`browser_screenshot`, `browser_scroll`, and `browser_open` (always a **new** tab) -- without a running session, on allowed sites
only, while the master switch is on and a browser is paired. `browser_click` and `browser_type` still need a session and your
approval; the extension itself refuses them in this mode ("a session isn't running; I can only look"). The allowlist, hidden
sites, private-window and sensitive-field rules are unchanged.

## What is enforced (in code, not by asking the model nicely)

| Rule | Where |
|---|---|
| Off unless the master switch is on **and** a session is running **and** a browser is paired; admin accounts only; sub-agents and peers never get these tools | `bridge.is_enabled` (the tools' live `enabled` gate) |
| Deny-by-default **site allowlist**, editable only on the admin page, never by a tool | `bridge_site_rules`; checked in `bridge._t_open` and again, against the live tab's real URL before **and** after every action, in the extension (`lib.js`) |
| Tabs on other sites are not listed, read, or touched (hidden count only) | extension `actTabs` |
| A redirect or click that lands on a non-allowed site is reverted | extension `actOpen` / `actAct` |
| **Risky actions pause for your approval**: submit buttons, labels like Send/Pay/Buy/Delete/Post/Sign out/Accept/Share/Save/Download…, typing + Enter. Nothing happens until you approve in the chat card, the notification, or Settings → Devices → Computer use | `lib.js classify` → `needs_approval` → `bridge_commands.status='awaiting_approval'` |
| Approval is for **that element**: the approved command carries its fingerprint; if the page changed, it refuses | `lib.js fingerprint`, `actAct` |
| Links to a non-allowed site are blocked outright (not merely paused) | `lib.js classify` |
| **Password / card / CVV / SSN / routing / one-time-code fields are never typed into**, and read as "(sensitive field)" — approved or not | `lib.js isSensitiveField`, `pageCollect` |
| Everything read from a page is **untrusted**: page text goes through the same injection screening as `web_fetch`; screenshots are described by a vision model, then screened. A page the screener flags makes **every later click/type in the session need approval** | `bridge._screen`, `_mark_if_suspicious` |
| Every command, its outcome, who approved it, and when is a row you can read | `bridge_commands`, *recent activity* |
| Bad tokens count against the same per-IP lockout as login; request bodies are size-capped; tokens are stored hashed | `server.bridge_post`, `bridge.create_token` |

## Honest limits

- **You are trusting the extension and the tabs you allow.** An allowed site that is itself hostile (or has an XSS) can still show Nori text aimed at her. The screener and the approval pauses are mitigations, not a guarantee; keep the allowlist to sites you'd be comfortable having Nori act on.
- The risky-action word list is a heuristic. A custom "Go" button that sends mail is still caught only if it's a form submit or its label matches. When in doubt, don't allow the site.
- A click's *consequences* aren't visible to the extension beyond the page it lands on.
- Sessions are single-user: only the admin account that paired the browser can use it.
- Screenshots go to your configured vision model provider; nothing is stored.
- A multi-step browsing task uses many tool rounds (`tool_rounds_chat`, default 12) and holds your chat while it runs; each browser command waits at most 25 s.
- The extension needs host permission for `<all_urls>` to work on whichever sites you allow; it only ever acts on allowlisted hosts.

## Tests

`tests/test_nori_bridge.py` (server half: allowlist, tokens, session/kill switch, queue, approvals, screening, HTTP routes, admin handlers), `tests/test_browser_extension.js` (`node tests/test_browser_extension.js`: the extension's allowlist and approval rules), both sharing `tests/bridge_vectors.json` so host matching can't drift between Python and JS. The page-side DOM functions were also exercised in a real browser (sensitive-field masking, typing events, clicks, submit detection).
