# Nori Android app

A sideloaded Android app (not on any store) that is Nori's companion on your phone. It speaks the contract in `docs/devices.md` and nothing else.

1. **Shows Nori full screen** in a web view: the chat and every page, signed in the way the website is (the website's own login; the app never sees your password).
2. **Delivers Nori's messages** as Android notifications (replies, reminders, approvals with Approve / Deny, finished jobs), and lets you **send things to Nori** from any app's Share sheet.
3. **Optionally, and only on the switches you turn on**: reads Health Connect, watches your saved places, opens links/apps and sets timers, and (inside windows you open yourself, on the phone) lets Nori read the screen, tap and type, or take a screenshot.

Everything is **off by default after pairing, except notifications**. Every capability needs two keys (the server's switch and the one in this app) and the phone re-checks every command itself.

## Build

Tools (JDK 17, Android SDK, Gradle) live in `D:\Dev\android-tools` (override with `NORI_ANDROID_TOOLS`); nothing is installed system-wide.

```powershell
.\build.ps1            # release APK: prints path, size, version
.\build.ps1 -Test      # unit tests first, then the build
.\build.ps1 -Upload    # build, then docker compose cp the APK + version.txt into the nori container (see below)
```

Or by hand (after pointing `JAVA_HOME`, `ANDROID_HOME`, `GRADLE_USER_HOME` at the tools):

```powershell
.\gradlew.bat testDebugUnitTest assembleDebug assembleRelease
```

The release APK is signed with the **debug key** (`~/.android/debug.keystore`) on purpose: a personal sideload that updates in place without a keystore to look after. Keep that file; Android only updates in place when the signature matches and `versionCode` goes up. **Bump `version.properties` (`versionName`, `versionCode`) for every build you install.**

`-Upload` copies the APK to `/app/data/phone_app/nori-companion.apk` and the version string to `/app/data/phone_app/version.txt` in the `nori` container. The server's `GET /device/app.apk` serves that file and `app.latest` in the poll answer carries `version.txt`.

## Install and pair

1. Install the APK (allow "install unknown apps" for whatever opened it).
2. In Nori: **Settings -> Devices -> Pair a phone**. It shows a one-time code `XXXX-XXXX` (valid 15 minutes) and a link `nori-pair://pair?code=...&server=...`.
3. Open that link on the phone (one tap; the app asks you to confirm the server name first, because a link can come from anywhere), **or** open the app, type the server address (`https://...`, HTTPS only) and the code, press **Pair**.
4. Allow notifications when Android asks. The app also asks **once** to be exempted from battery optimisation (so replies and reminders are not delayed). Both are repeated under *Battery & permissions help* in the app's settings.

If the server ever answers 401 the app says "pair again" and stops until you do; 403 makes it wait a minute; 429 makes it slow down.

Deep links: `nori-pair://pair?code=XXXX-XXXX&server=https://...` and `nori-app://settings` (opens the settings screen). Widgets, launcher shortcuts and notifications use the same app routes (`nori-app://talk`, `task`, `chat`, `prefill?text=`) through explicit intents only.

## Several servers (Nori and Iron)

One phone can be paired with up to four Nori servers at once. Each is a **connection**, shown as a tab along the top of the app once there are two or more (with one, there is no bar). The last tab you looked at is where widgets, shortcuts and the Share sheet start.

**Adding a second one:** make a pairing code in the *other* Nori's Settings -> Devices and open its link on the phone (the app asks you to confirm the address and says it is adding a connection), or open the app's settings -> *Add another connection* and type the address and code. Pairing a server the phone already has replaces that connection's pairing; it does not add a duplicate. Two servers on the same host name cannot be kept apart (the web view's login cookies are per host), so give each its own host name. In the app's settings you can rename a connection (the tab's title; the default is the first part of its host name, `iron.example.com` -> Iron), unpair it, or remove it.

**What is per connection:** the address and token, which of its messages were already shown, what that server last said the phone may do, its place list, the health readings it was sent, the update it offers.

**What is shared (the phone's own):** pause, every switch under *What I allow*, the control and screen windows, and the hourly limits. Notifications and access are therefore the same for every connection. Every capability still needs the server's switch too, so turning health data on here sends it only to a server that allows it, and a place event goes only to the server that owns that place. While a control or screen window is open, any connection whose server allows it may use it, one request at a time.

**Notifications:** a notification opens the tab it came from, and Approve / Deny go to the server that asked. The notification channels are shared (their title already carries the assistant's name). Only the tab in front counts as "the chat is open" for that server; the other server keeps sending notifications.

**Updates:** the app offers the newest version any connection has, downloaded from that server. `build.ps1 -Upload` copies the APK into the `nori` container only: put the same APK and `version.txt` on each server you want to update from.

An existing pairing is the first connection and carries over with no steps (it keeps its old storage names). A phone that already has its connections keeps them across updates.

## What it does

| Feature | Where |
|---|---|
| Web view, microphone for the chat's push-to-talk (only the paired server's pages, audio only) | `MainActivity` |
| Long poll `POST /device/poll` (25 s hold, 40 s timeout, backoff 2 s doubling to 60 s), ack-after-shown, dedupe by id, command ack/result, 22 s answer watchdog | `SyncService`, `core/Inbox.kt` |
| Notification channels (chat, reminder, approval HIGH; job DEFAULT; peer, sync, windows LOW; updates), Approve/Deny that require device unlock | `Notifier`, `Approvals`, `core/Channels.kt` |
| Share sheet "Send to Nori" (text, link, image; chat / note / task) | `ShareActivity`, `Share.kt` |
| Quick Settings tiles (Pause, "Nori may act", "Nori may look"), launcher shortcuts, home-screen widget | `Control.kt`, `Widget.kt`, `res/xml/shortcuts.xml` |
| Health Connect (weight, blood pressure, steps, sleep, resting heart rate) every 15 min and on app open, never sent twice | `Health.kt`, `core/Health.kt` |
| Places: on-device geofences, reports only `arrived`/`left` | `Places.kt`, `core/Geo.kt` |
| Phone actions: `open_url`, `open_app`, `set_timer`, `set_alarm`, `navigate`, `list_apps` | `Commands.kt`, `core/UrlGuard.kt`, `core/ActionArgs.kt` |
| Screen reading / control with approvals | `NoriAccessibilityService`, `core/Approval.kt`, `core/Sensitive.kt`, `core/AppGuard.kt` |
| Screenshot (one picture per request, never stored) | `ScreenShare.kt` |
| Everything-sent log (last 120), crash log, status line | `SentLog`, `NoriApp`, `core/Status.kt` |
| Updates via `GET /device/app.apk` (Bearer token) | `Updates.kt` |

All decisions live in plain Kotlin under `app/src/main/java/app/nori/companion/core` (no Android types) and are covered by JVM tests in `app/src/test`.

## The windows

Control and screen sharing work only inside a **window you open on the phone** (in the app, or with the Quick Settings tiles): 15 minutes, 1 hour or 3 hours. A countdown notification shows while a window is open; it closes itself. The server cannot open one. The window end times are sent in every poll (`interact_until`, `share_until`); an expired one is reported as 0, and a window never survives a phone restart.

* **Control** (`read_screen`, `interact`): needs the owner switch, the server switch, the Nori accessibility service and an open control window. Every step needs a non-empty `why`, passes the hourly cap, and is refused in Settings, permission screens, installers, the Play Store, wallets, password managers and authenticators, system UI, this app, plus the server's `hidden_apps` and `never_control_apps`. Password, card and one-time-code fields are reported as `(sensitive field)` and are never read or typed into. Risky controls (send, pay, delete, confirm, install, allow, ..., or typing that ends in Enter) answer `needs_approval` and **nothing is done**; the approved re-send must carry the element fingerprint, and the element is re-found and re-verified before acting.
* **Screen share** (`screenshot`): needs the owner switch, the server switch, an open share window and Android's own capture consent (which shows Android's capture indicator; this app never hides it). Frames are discarded unread; only when a `screenshot` command arrives is one picture taken, compressed (JPEG, quality stepping down to fit) and returned. It is never stored. It also needs the accessibility service, only so the phone can tell which app is in front and refuse private or deny-listed apps (checked before and after the capture).

## Permissions, and why

See the comment block at the top of `AndroidManifest.xml`. Notably **absent**: `QUERY_ALL_PACKAGES` (a launcher-intent `<queries>` entry is used instead), `PACKAGE_USAGE_STATS`, camera, notification-listener access, contacts, SMS, call log.

* `SYSTEM_ALERT_WINDOW` ("Display over other apps"): Android blocks starting an app or link from the background; an app with a visible overlay window may. It is used for exactly that (phone actions while Nori is closed) and to keep the screen on while a window is open. Not needed if you use neither.
* `ACCESS_BACKGROUND_LOCATION`: Android requires "Allow all the time" for place fences to fire while the app is closed. The fences are evaluated by Google Play services; this app does not collect a location trail, and coordinates are sent only once, by you, with "Save where I am now as a place".
* Foreground service type is `specialUse` only (no location type), so it can start at boot and never needs restarting after a location grant.

### Allowing the accessibility service on a sideloaded app (Android 13+)

Android blocks the accessibility switch for apps installed from a file ("Restricted setting"). To allow it:

1. Settings -> Apps -> Nori (or the **App info** button in the app's *Battery & permissions help*).
2. Tap the three-dot menu in the top right.
3. Choose **Allow restricted settings** and confirm with your PIN or fingerprint.
4. Go back and turn on **Accessibility -> Nori**.

If the menu is missing, try to turn the service on once (it is refused), then look again.

## What the app relies on from the server

* `/device/register`, `/device/poll`, `/device/ack`, `/device/result`, `/device/approval`, `/device/events`, `/device/place`, `/device/health`, `/device/share`, `/device/unregister`, `GET /device/app.apk` exactly as in `docs/devices.md`.
* `GET /?prefill=<url-encoded text>` puts that text in the chat box (used by the "Log water" / "Log weight" widget buttons and the `nori-app://prefill` link). **Not in the contract**: the server's chat page must read it.
* `GET /?talk=1` opens the chat ready for push-to-talk (used by the "Talk to Nori" widget button and shortcut). **Not in the contract** either; without server support it simply opens the chat.
* The chat page lives at `/` and the website's own session cookie keeps the web view signed in.
