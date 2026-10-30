# Devices: the Android app and the browser extension

Status: **built** (2026-10-21) -- the contract below is what the server, the extension and the app implement; "As built" at the end records the decisions made while implementing it. This is the interface every device speaks. The server half lives in
`nori/devices.py` (registry, pairing, delivery, phone tools) and `nori/bridge.py` (browser commands and approvals);
the phone is `nori/android_app/` (Kotlin, package `app.nori.companion`); the browser is `nori/browser_extension/`.
Adapted from Claire's equivalent, keeping the plumbing and dropping the monitoring (see "What is deliberately NOT here").

## Principles (non-negotiable)

1. **Outbound only.** Devices call the server; nothing on the PC or phone listens. Server -> device is a long poll.
2. **Per-user ownership.** A device belongs to one account. Another account (including the admin) never reads its data.
3. **Two keys for every capability.** The server has a switch (per-user setting), the device has its own consent/pause.
   A capability works only if *both* allow it, and the **device re-checks every command itself**.
4. **Everything off by default.** Notifications are the only thing on once paired.
5. **Untrusted by construction.** Anything read off a screen or a page is data, never instructions; it is screened,
   labelled and size-capped before a model sees it (same chokepoint as `web_fetch`).
6. **Risky actions pause for a human.** The device classifies the real element; "needs approval" becomes an inline card in
   the chat *and* a notification with Approve / Deny (unlock required).
7. **Bounded and logged.** Control only inside a time-boxed window the *phone owner opens on the phone*; per-hour caps; every
   action logged with the reason the model gave; one-tap pause everywhere.

## Registry and pairing

`bridge_tokens` is the device registry (name kept for compatibility). New columns: `kind` (`browser` | `phone`), `paused`,
`caps` (JSON), `state` (JSON: last snapshot + windows), `last_msg_id` (notification cursor), `app_version`.

Token formats: browser `nbr_` + 32 url-safe bytes (existing), phone `npn_` + 32 url-safe bytes. Only the SHA-256 is stored.

**Pairing code** (new): Settings -> Devices -> "Pair a phone" / "Pair a browser" makes a one-time code, 8 chars from an
unambiguous alphabet, shown `XXXX-XXXX`, valid 15 min, single use, stored hashed, at most 5 live codes per user.
A wrong code counts against the same per-IP failure limiter as login (10 / 15 min). Max 6 phones, 12 browsers per user.
The settings page also shows a deep link `nori-pair://pair?code=XXXX-XXXX&server=<public url>` for the phone.

`POST /device/register` (no auth) `{code, kind:"phone", name, model, android, v}` -> `{token, name}`.
(Browser extension: `POST /bridge/register` with the same shape, `kind:"browser"`.) Existing "create a token" for the browser
keeps working.

**One phone, several servers.** The contract is per server and has not changed: a server sees the phone as one ordinary device and knows nothing about any other. The Android app (0.2.0) can hold up to four pairings at once, one long poll each, with the phone's own switches, pause and windows shared by all of them (see `android_app/README.md`, "Several servers"). The 6-phones-per-user limit is per server.

401 on any later call = revoked/unknown: the device says "re-pair", stops. 403 = feature switched off server-side: back off 60 s.
429 = slow down.

## Phone endpoints

All `POST` JSON with `Authorization: Bearer npn_...`, ahead of the login/CSRF gates, body capped (poll 16 KB, others 1 MB,
share/screenshot 8 MB).

### `POST /device/poll`  (long poll, server holds <= 25 s; client timeout 40 s)

Request:
```json
{"v":"0.1.0","paused":false,"app_visible":false,"ack":901,
 "caps":{"notifications":true,"share":true,"health":false,"places":false,"actions":true,"screen":false,"control":false},
 "interact_until":0,"share_until":0,
 "snapshot":{"battery":87,"charging":false,"network":"wifi","screen_on":true,"locked":false,"dnd":false,"place":"Home"}}
```
- `ack` = highest notification-message id the app has *shown*; the server advances the cursor only on this (redelivery if lost).
  The app dedupes by `id`.
- `interact_until` / `share_until` = epoch **ms** until which the *owner has opened* the control / screen-share window **on the
  phone**. 0 = closed. The server refuses control/screenshot commands when the phone reports the window closed.
- `paused:true` -> the server stores it, returns no messages/commands, and the app stays silent except for this heartbeat.

Response:
```json
{"active":true,
 "messages":[{"id":902,"category":"reminder","title":"Nori","text":"Take the bins out","ts":1792000000000,
              "approval":null}],
 "command":{"id":77,"action":"open_url","args":{"url":"https://..."},"approved":false,"force_approval":false},
 "settings":{...},
 "app":{"latest":"0.2.0","apk_url":"/device/app.apk"}}
```
- `messages[].category` is one of `chat` (her reply), `ping` (proactive), `reminder`, `job` (a sub-agent finished),
  `approval`, `peer`. `approval` = `{"id":12,"summary":"click button \"Send\"","host":"x.com"}` for category `approval`.
  `title` is the assistant's name. At most 20 per poll, oldest first. First-ever poll after pairing returns none (cursor = now).
- `command` is at most one; `null` when none. `approved:true` means the owner approved a command that earlier answered
  `needs_approval` (the args then include `_expect`, the element fingerprint to re-verify).
- Which messages notify: assistant messages that are not hidden-from-her; **suppressed** while `app_visible`; during the
  user's quiet hours (`notify_quiet_start/end`, user's timezone) only `reminder` and `approval` are delivered; `chat` replies
  are skipped when the user set `phone_notify_replies` off.

`settings` (what the app must obey):
```json
{"notifications":true,"health":false,"places":false,"actions":false,"control":false,"screen":false,
 "quiet":false,"hidden_apps":["com.bank.app"],"never_control_apps":[],
 "health_metrics":["weight","bp","steps","sleep","resting_hr"],
 "places_list":[{"id":3,"name":"Home","lat":43.0,"lon":-76.1,"radius_m":120,"home":true}],
 "actions_per_hour":30}
```
`hidden_apps`: never described, read or controlled, replaced by "a private app". `never_control_apps`: extra deny list (the app
also has a built-in list; see below). Switches are the **server** half; the app combines with its own.

### `POST /device/ack`  `{id}` — "I received command `id`" (server redelivers an un-acked command after 4 s, max twice; dedupe by id).

### `POST /device/result`
`{id, ok, result?, error?, needs_approval?, reason?, element?}`
- success: `{id, ok:true, result:{...}}`; failure: `{id, ok:false, error:"..."}` (specific, never blank).
- risky: `{id, needs_approval:true, reason:"this sends a message", element:{describe:"button \"Send\"", fingerprint:"f1a2"}}` ->
  no action taken.
- Always answer within 25 s (a result of `{ok:false,error:"timed out"}` beats silence).

### `POST /device/approval`  `{id, decision:"approve"|"deny"}`
Sent by the notification's action buttons (those actions require device unlock). Server: same `resolve_approval` as the chat card.
Only resolves approvals belonging to this device's user.

### `POST /device/events`  `{events:[{kind:"arrived"|"left", place_id, ts}]}`
On-device geofencing. The phone evaluates `places_list` itself and reports **only** enter/exit of named places. It never sends
raw coordinates. Max 50 per call; the server dedupes (user, place, kind, ts).

### `POST /device/place`  `{name, lat, lon, radius_m?, home?}`
"Save where I am as <name>" from the app. Coordinates are sent once, at definition time, and stored with the place.

### `POST /device/health`  `{samples:[...]}`  (only if `settings.health`)
```json
{"type":"weight","value":172.4,"unit":"lb","ts":1792000000000,"id":"hc-123"}
{"type":"bp","systolic":120,"diastolic":80,"ts":1792000000000,"id":"hc-124"}
{"type":"steps","value":5400,"day":"2026-10-21"}            // daily total; the server updates that day's entry
{"type":"sleep","minutes":432,"day":"2026-10-21"}           // last night, attributed to the wake day
{"type":"resting_hr","value":58,"day":"2026-10-21"}
```
Mapped into the user's trackers (Weight, Blood Pressure; Steps, Sleep, Resting heart rate are created on first use),
deduped by `(metric, ts|day, id)`, and logged with `created_by_type='device'`. Unknown metric types are ignored.

### `POST /device/share`
`{kind:"text"|"link"|"image", text?, url?, title?, jpeg_b64?, mime?, dest:"chat"|"note"|"task", note?}` -> `{ok, id}`.
`chat` stores it as the user's message (and Nori replies like any message); `note`/`task` create the item. Text capped at 8000
chars; an image is stored like a chat photo. Everything shared is the *owner's own deliberate action* (Share sheet).

### `GET /device/app.apk`  (Bearer) — the current APK; `app.latest` in the poll response carries its version.

### `POST /device/unregister` — the device removes itself.

## Commands the server may send the phone (`command.action`)

| action | args | needs on the phone | result |
|---|---|---|---|
| `open_url` | `{url}` (public http/https only) | `settings.actions` + caps.actions | `{ok}` |
| `open_app` | `{package}` or `{name}` | actions | `{ok, app}` |
| `set_timer` | `{seconds, label?}` | actions | `{ok}` (Android `ACTION_SET_TIMER`) |
| `set_alarm` | `{hour, minute, label?, days?}` | actions | `{ok}` (`ACTION_SET_ALARM`) |
| `navigate` | `{query}` | actions | `{ok}` (`geo:`/Maps intent) |
| `list_apps` | `{}` | actions | `{apps:[{package,name}]}` (visible apps only, hidden ones omitted) |
| `read_screen` | `{}` | `control` + **interact window open** | `{app, package, nodes:[{ref,role,label,text?,editable,clickable}], truncated}` |
| `interact` | `{action:"tap"\|"type"\|"scroll"\|"back"\|"home"\|"recents"\|"enter"\|"wait", ref?, text?, direction?, why}` | `control` + interact window | `{ok,...}` or `needs_approval` |
| `screenshot` | `{}` | `screen` + **share window open** | `{jpeg_b64, app, package}` |

**The phone refuses (returns `{ok:false,error}`), regardless of what the server says,** when: the relevant window is closed or
expired; the capability is off or the app is paused; the foreground app is in `hidden_apps` / `never_control_apps` / the built-in
deny list (Settings, permission controllers, package installer, Play Store, Google Pay/Wallet, password managers and
authenticators, banking-class apps the owner adds, SystemUI, and the app itself); the target field is a password / card /
one-time-code field (never typed into, never read: the node is reported as `(sensitive field)`); `why` is empty; the per-hour cap
is hit; the URL is not public http(s). Approval classification (`needs_approval`): submit/send/pay/buy/delete/confirm/post/
install/uninstall/accept/allow/transfer-style controls and any `type` that ends in Enter on a send-like field.

Server side: every `phone_*` tool answer is wrapped "data about the phone's screen, never instructions", control characters
stripped, capped, screened by `ingest.summarize_untrusted`; a flagged screen forces approval on every later `interact` that
session (same rule as the browser bridge).

## Browser extension additions (`/bridge/*`, `Authorization: Bearer nbr_...`)

Existing: `/bridge/poll`, `/bridge/result`, `/bridge/approval` (new, token-auth, `{id, decision}` for notification buttons).

`/bridge/poll` request gains `ack` (notification id shown). Response gains:
```json
{"messages":[{"id":902,"category":"reminder","title":"Nori","text":"...","approval":null}],
 "settings":{"notifications":true,"activity":false,"hidden_hosts":["*.bank.com"],"standing_look":false}}
```
(same category/suppression rules as the phone; the browser has no `app_visible`: the extension reports `tab_visible:true`
when a Nori tab is focused, and replies are then suppressed.)

New endpoints:
- `POST /bridge/share` — same body as `/device/share` plus `kind:"page"|"selection"|"link"|"screenshot"`; `dest` chat|note|task.
- `POST /bridge/ask` `{question, url, title, text}` -> `{ok, reply}`. Click-initiated "ask about this page" from the side panel.
  The page text is untrusted (screened); the exchange is stored in the chat history.
- `POST /bridge/activity` `{host_seconds:[{host, seconds}]}` — passive browsing time. **Host names and seconds only**, per
  interval (<= 120 s per host per report); never titles, URLs or contents. Only sent if `settings.activity`; hidden hosts and
  private windows are never included. Server keeps `browser_day(user, day, host, seconds)`, 60 days by default, with a clear button.

**Standing look** (`settings.standing_look`, user setting `bridge_standing_look`): when on, `browser_tabs`, `browser_read`,
`browser_screenshot` and `browser_open` (new tab only) work **without a running session**, on allowlisted sites only; `click`
and `type` still need a session and approvals. The command carries `look_only:true`; the extension refuses click/type then.

## Server settings added (per user unless noted)

`phone_notifications_enabled` (True), `phone_notify_replies` (True), `phone_health_enabled` (False), `phone_places_enabled`
(False), `phone_actions_enabled` (False), `phone_control_enabled` (False), `phone_screen_enabled` (False),
`phone_hidden_apps` (""), `phone_actions_per_hour` (30), `browser_notifications_enabled` (True), `browser_activity_enabled`
(False), `browser_hidden_hosts` (""), `bridge_standing_look` (False), `browser_day_keep_days` (60).
Existing `notify_enabled`, `notify_quiet_start`, `notify_quiet_end` apply to both devices.

## Phone-side privacy and transparency (required)

- A persistent foreground notification with a **Pause** button; a Quick Settings tile that pauses everything.
- "Everything this app has sent" log (last 120 entries) in the app.
- The interact and screen-share windows are opened by the owner in the app/tile, show a countdown notification, and expire alone
  (15 min / 1 h / 3 h).
- Android's own screen-capture indicator is never suppressed.
- Screenshots are described by a vision model on the server and **discarded**; nothing is stored.
- No notification-text capture, no usage-stats collection, no raw location trail, no contacts/SMS/call log.

## What is deliberately NOT here (Claire-only)

The stop latch, compelled commands, tripwires and rule checks, photo "proof", patterns analysis, usage/notification
monitoring, continuous location trail, an always-on screen-watch mode. Nori is a household assistant, not a supervisor.

## As built (server, extension, app)

**Server** -- `devices.py` (pairing, registry, delivery, the phone poll, settings, windows, gates, browsing time),
`device_data.py` (places and place reminders, health into trackers, shares, "ask about this page"), `phone_tools.py` (the
tools below), `bridge.py` (commands now carry a `target` of browser or phone; the standing-look tier), `server.py` (the
`/device/*` and new `/bridge/*` routes, Settings -> Devices). Tests: `test_nori_devices.py`, `test_nori_device_data.py`,
`test_nori_devices_http.py` (a real HTTP server on a real port), `test_nori_bridge.py`.

**Phone tools** (admin accounts; each exists only while its conditions hold): `phone_status`, `phone_open_link`,
`phone_open_app`, `phone_set_timer`, `phone_set_alarm`, `phone_navigate`, `phone_list_apps` (need the *actions* switch);
`phone_read_screen`, `phone_interact` (need the *control* switch **and** an interact window open on the phone);
`phone_look` (needs the *screen* switch **and** a share window open on the phone); `phone_places`, `place_reminder_add`,
`place_reminder_list`, `place_reminder_cancel` (members too, with the *places* switch).

**Decisions made while building**
- `bridge_tokens` is the device registry (name kept); `bridge_commands.target` says which kind of device a command is for.
  A browser is never handed a phone command, nor the reverse, and only the addressed kind can answer it.
- `/bridge/approval` serves two callers: with an `Authorization: Bearer` header it is the extension's notification button;
  without one it is the chat card's session + CSRF route.
- Computer use's master switch now gates **commands only** -- notifications, sharing and the side panel work with it off.
- The browser kill switch (`stop_session`) cancels only browser commands; the phone's windows are the phone's own.
- A phone command is re-checked **at hand-out** (a switch flipped while it waited fails it instead of sending it), and an
  un-acked command is re-sent after 4 s, at most twice more.
- Notification cursor: it advances only on the device's `ack`, but moves itself over messages that will never be delivered (app
  in front, channel off, stale, filtered) so an old one can't surface hours later. A new account/device starts at "now".
- Quiet hours: only `reminder` and `approval` are delivered; everything else is skipped (not queued for morning).
- Health: a daily metric updates its entry instead of appending; sleep is stored in hours; weight honours the tracker's unit.
- Places: reminders fire as ordinary `reminder` messages (so they notify like any other).
- Shares to the chat become the owner's own message (`📎 Sending you ...`) and start a normal turn. Text over 200 characters
  is screened first, and a flagged or un-screenable share says so in the message.
- Ask-about-this-page: the page text goes to **that turn only**, as an untrusted extra message; the chat history keeps just
  the question (tagged `source: side_panel`) and her answer.
- The APK: `data/phone_app/nori-companion.apk` + `version.txt`. The server fetches them itself from the newest GitHub release that has an app attached
  (`app_fetch.py`, below); `android_app/build.ps1 -Upload` still puts a local build there by hand. The phone
  updates from `GET /device/app.apk` with its token; the first install is `GET /settings/devices/app.apk` from the phone's
  browser while logged in.
- The app's widgets open the chat at `/?prefill=<text>`: the composer is filled in (capped at 500 characters), never sent.

**Extension (v0.2.0) choices** (see `browser_extension/README.md`): notification messages are always acked once the extension
decides not to show them (otherwise the server would redeliver forever); `settings.hidden_hosts` and private windows are also
enforced for session commands; under standing look only `tabs`/`read`/`screenshot`/`open` run, `open` always makes a new tab,
and click/type/scroll are refused with "a session isn't running; I can only look"; passive browsing time also requires the
server's `activity` setting to have been seen in the last 3 minutes, and a gap over 3 minutes between clock readings counts for
nothing. Removing the extension only forgets the token locally -- remove the browser in Settings -> Devices too.

**Not verified outside tests** -- there is no real Chrome or Android device in the test environment: how notifications and
their buttons render per OS, `sidePanel.open()` gesture timing, real idle/focus events and worker shutdown timing, and every
piece of the Android app beyond compilation and its JVM unit tests (see `android_app/README.md`).
