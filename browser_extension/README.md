# Nori browser bridge (v0.2.0)

A Chrome (Manifest V3) extension that connects this browser to **your own** Nori server. It never talks to anyone
else, nothing on this computer listens for connections (the extension calls out to Nori and waits for the reply),
and everything it can do is also switched by Nori's settings, so **both sides have to agree** before anything
happens. The contract with the server is in `docs/devices.md` ("Browser extension additions").

## What it does

| Feature | When it happens | Off unless |
|---|---|---|
| **Desktop notifications** from Nori (replies, pings, reminders, finished jobs, approval requests) | when Nori has something for you | on by default once paired; one switch in Nori, one in the options page |
| **Approve / Deny** buttons on an approval notification | when you press one | you press it |
| **Send to Nori** (page, selection, link, screenshot; to the chat, a note or a task) | when you right-click a menu item or press "Send this page" in the popup | you press it |
| **Ask about this page** (side panel) | when you press Ask | you press it |
| **Browsing time** (host names and seconds) | every 60 s while you are active in Chrome | Nori's "browsing time" setting is on *and* you are not paused |
| **Use my browser in a session** (list tabs, read, screenshot, click, type, open) | only during a session you start in Nori | Nori says a session is running, on sites you allowed |
| **Standing look** (list tabs, read, screenshot, open a *new* tab) | when Nori asks, with no session | Nori's "standing look" setting is on; allowed sites only; never click or type |

## Install (developer mode)

1. Open `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and choose this folder (`browser_extension`).
4. Pin the Nori icon from the puzzle-piece menu if you want the popup one click away.

To update after pulling new code: open `chrome://extensions` and press the reload arrow on the Nori card.
Needs Chrome 116 or newer for the side panel button (the rest works on older Manifest V3 browsers).

## Pair it

1. In Nori: **Settings → Devices → "Pair a browser"**. You get a one-time code like `K7QX-29MD`, valid 15 minutes.
2. Click the extension's **Options** (popup → Options). Enter Nori's address and the code, optionally a name, press **Pair**.
3. The popup now shows the status ("idle (no session)", "session running", ...). The code is not stored.

Already have a token (Settings → Computer use → "create a token", starts with `nbr_`)? Use "I already have a token"
on the options page.

If Nori later answers "unknown token" (you removed this browser in Settings → Devices) the popup says
**removed, re-pair** and the extension stops contacting the server until you pair again. If the feature is switched
off on the server it says "switched off on the server" and tries again every minute.

**Remove this browser** (options) forgets the token and everything tied to it here. Also remove the browser in
Nori's Settings → Devices so the old token is dead.

## Permissions, and why each is needed

| Permission | Why |
|---|---|
| `host_permissions: <all_urls>` | Nori's sessions can act on any site *you* allow, so the extension may need to read/click on one; and it must reach your Nori server wherever it lives. It never acts on a site that is not on your allowed list, and never on hidden sites. |
| `tabs` | To see a tab's real address (to enforce the allowed / hidden lists), list tabs, open a tab, focus Nori's tab when you click a notification, and take a screenshot of the tab you chose. |
| `scripting` | To read the page text *when you press a button* (Send with page text, Ask), and, in a session you started, to read/click/type. Nothing is injected otherwise. |
| `activeTab` | Declared for the explicit "you pressed the button on this tab" actions. With `<all_urls>` it grants nothing extra today; it is there so those actions keep working if the broad host permission is ever narrowed. |
| `storage` | Keeps the token, your switches, the notification ids already shown (so a restart doesn't repeat them) and the browsing-time clock. Stays in this browser. |
| `alarms` | A 30-second keep-alive: Chrome stops idle extension workers, this restarts the connection loop and clears the toolbar badge. |
| `notifications` | Desktop notifications from Nori, including Approve / Deny buttons. |
| `contextMenus` | The "Send to Nori" right-click menu. |
| `sidePanel` | The "Ask about this page" panel. |
| `idle` | So browsing time only counts while you are actually at the computer (idle after 2 minutes, or screen locked, stops the clock). |

Not requested: cookies, history, bookmarks, downloads, web requests, debugger, native messaging, clipboard.

## What is shared, and what never is

**Never, in any mode:** passwords or the value of any form field (password fields are never read or typed into;
card, one-time-code and similar fields are treated the same way), cookies, history, bookmarks, downloads, the
clipboard, anything from a **hidden site** (Nori's hidden-hosts list for the browser; wildcards such as
`*.mybank.com` work), anything from a **private (incognito) window** unless you turn on "Include private windows"
in the options (Chrome also has to allow the extension in incognito), and anything at all while you are **paused**.

**Notifications:** the text Nori sends is shown on your screen; nothing about your browsing goes back except which
notification you have seen (a number) and whether a Nori tab is in front (so Nori doesn't ping you about
something you're already looking at). Quiet hours are applied by the server.

**Send to Nori:** exactly what you chose, once. A page sends its title and address (the address keeps its query
string, loses the `#fragment`; refused if it contains a username/password). "Send this page and its text" adds the
page's visible text (up to 8,000 characters). A selection sends up to 2,000 characters of what you selected.
A link sends the link. A screenshot sends one JPEG of the visible part of the tab (under about 4 MB; refused if
you switch tabs while it is taken). The destination is the chat (default), a note or a task.

**Ask about this page:** nothing is read until you press **Ask**. Then Nori gets the page's title, address and up
to 30,000 characters of its visible text (scripts, styles, hidden elements, password fields and everything you
typed into forms are skipped). The panel shows the title, the host and the number of characters before and while
it sends, and shows Nori's answer as plain text. The conversation is not saved by the extension.

**Browsing time:** only if Nori's setting is on (and the extension has heard that in the last 3 minutes). Every
minute it sends *host names and seconds*, for example `docs.example.com: 40`, at most 120 seconds per host per
report. Never titles, addresses, paths or content. It counts only the active tab of the focused window while you
are not idle; hidden sites, private windows, non-web pages (`chrome://`, files) are skipped. A report that
cannot be sent is thrown away after 5 minutes. The popup shows "browsing time sharing: on/off" and the last
report's hosts.

**In a session / standing look:** Nori can list the tabs on your allowed sites (other tabs are only counted), read
a page's text and its buttons/fields (sensitive fields appear as "(sensitive field)"), screenshot a tab, and open a
tab. Risky clicks (send, pay, delete, submit, ...) stop and ask you first, in the chat and as a notification with
Approve / Deny. With **no session** (standing look) the extension itself refuses to click, type or scroll
("a session isn't running; I can only look"), opens only *new* tabs and only screenshots the tab already in
front. The extension checks every command itself; it does not rely on the server being right.

## Popup

Shows the connection status, a **Pause** switch (stops everything locally at once), what is being shared right
now (notifications, browsing time, standing look, private windows: each derived from the real stored state), the
last command's outcome, **Send this page** (with destination, "include the page's text" and an optional note), and
**Open side panel**.

## Tests

From the repo root (Node 18+; no browser needed, the real service worker runs against a fake `chrome` object):

```
node tests/test_browser_extension.js              # allowlist / approval rules (shared vectors)
node tests/test_browser_extension_core.js         # pairing, 401/403/429, pause, standing look, manifest
node tests/test_browser_extension_notifications.js
node tests/test_browser_extension_share.js
node tests/test_browser_extension_panel.js        # side panel, popup, options
node tests/test_browser_extension_activity.js
```

These do **not** prove behaviour inside a real Chrome (real notification rendering, the side panel UI, real
service-worker shutdown timing, OS focus/idle events). Do a manual pass after loading it: pair, send a test
message from Nori, press Approve on an approval notification, send a page, ask about a page, and check the popup.
