# Updates

Nori is self-hosted, so a fix pushed to GitHub only helps someone once they run it. Nori tells the admin when a newer **release** exists; the admin updates it.

## What the admin sees

A small **↑ Update** pill in the header (admins only) that opens **Settings → Updates**:

- what's running and what's available, with the release notes and a link to the release on GitHub;
- the exact commands for the two ways people run Nori (below);
- the switches: check once a day (on by default), include pre-releases (on by default while Nori is in beta), **check now**, and **skip this release** (hides the pill until a newer release appears).

Members never see any of it.

## Updating

Nori **never updates or restarts itself**. There is no "update now" button, on purpose: it would need git and the Docker socket (effectively root on the host) inside the container, which is a lot of attack surface for a convenience. The Updates tab shows the commands with the real tag filled in:

```
# Docker (from the folder you cloned Nori into)
git fetch --tags
git checkout v0.11.0
docker compose up -d --build

# Without Docker
git fetch --tags
git checkout v0.11.0
pip install -r requirements.txt
pwsh nori_ctl.ps1 restart       # or restart it however you run it
```

Your data (chats, memory, settings, files) lives in its own volume and is kept; the database upgrades itself on start (see [Compatibility](compatibility.md) for what is promised to survive). Take a backup first if you like (Settings → Backups). `git checkout <tag>` leaves you on that exact version; `git checkout main` later follows the latest again (`git pull` if you track `main`).

## What the check does, and what it sends

Once a day (`updates.maybe_check`, from the scheduler tick) Nori makes **one HTTPS GET** to `https://api.github.com/repos/Dispatch-Dataworks/Nori-Harness/releases`, conditional on the last `ETag` so an unchanged answer is a bodiless `304`. GitHub sees the server's IP address and the user agent `Nori-update-check`. Nothing about the instance, its version, its users or its data is sent. A failed check is recorded, shown on the Updates tab, and retried after six hours.

- Only a **published GitHub Release** counts. A bare tag, a draft, or a push to `main` never prompts anyone.
- Release notes are text from GitHub and are treated as untrusted: stored capped at 4000 characters, shown escaped, never as HTML. The "view on GitHub" link is shown only if it points at the project's own `github.com` page.
- A fork can watch its own releases by setting `NORI_UPDATE_REPO=owner/name` in `.env`.
- The check is a workspace setting (`update_check_enabled`, `update_include_prereleases`); switch it off and no request is made.

## The Android app comes with it

A server that is updated to a release should also be able to offer that release's phone app, so the app is fetched the same way (`app_fetch.py`, from the same scheduler tick, right after the check). Each release that has `nori-companion-<version>.apk` attached (`scripts/release.ps1` does that) has the file's name, size and GitHub's SHA-256 stored with the release list. When the newest app among them is newer than the one this server offers (or it offers none), the server downloads it and puts it where paired phones get it (`data/phone_app/`), so the app's own updater offers it on its next poll.

- It is **automatic**, with a switch: Settings -> Updates -> *also fetch the Android app from new releases* (`phone_app_auto_fetch`, on by default, and it only runs while the update check is on). *fetch the app now* does it on the spot, whatever the switches say.
- What leaves the server: one HTTPS GET to the asset on `github.com/<repo>/releases/download/<tag>/`, following redirects only to `github.com` and `*.githubusercontent.com`. Nothing identifying is sent.
- What it accepts: never more than the size GitHub listed (and 60 MB at most), the SHA-256 GitHub published for it, and a real Android package (a zip with `AndroidManifest.xml` and a `classes*.dex` that passes the CRC test). Anything else is thrown away and the app already on offer stays. It never downgrades and never fetches a version it already has. A failure is shown on the Updates tab and retried after six hours (a newer version is tried at once).
- It cannot push anything onto a phone: the phone still asks before installing, and Android only updates an app in place from an APK signed with the same key as the one installed.
- A release list stored before releases carried their files is asked for again in full on the next cycle, so the first update to a server with this feature finds the app without waiting a day.

## What version is this?

The `VERSION` file in the repo root (for example `0.10.0-beta`), baked into the image by the build (`.git` isn't). `NORI_VERSION` overrides it. Versions are compared as [semver](https://semver.org): a release outranks its own pre-releases, and `0.10.0` is later than `0.9.0`. An instance running something newer than the latest release (a maintainer on `main`) is simply "up to date".

## Cutting a release (maintainers)

1. Bump `VERSION` (bare number, no `v`), add the changelog entry (`docs/changelog.md`), commit.
2. Push, then tag and publish with the script, which also builds the Android app and attaches it to the release:

   ```
   .\scripts\release.ps1 -Notes "what changed"       # the notes are what people read in the Updates tab
   .\scripts\release.ps1 -NotesFile notes.md         # or from a file; with neither, GitHub generates them
   ```

   It reads `VERSION` (a version with a dash is published as a pre-release), refuses to run unless everything is committed and pushed and the tag is new, runs the Android unit tests, builds the APK, and publishes `v<VERSION>` with `nori-companion-<versionName>.apk` attached (`versionName` is `android_app\version.properties`: bump it, and `versionCode`, whenever the app changed). Options: `-SkipTests`, `-NoApk` (a server-only release), `-DryRun` (everything except publishing). It runs on your machine, not in CI, because the APK is signed with your local debug key and must be the same key every time.

   To do it by hand instead: `gh release create v0.11.0 --title v0.11.0 --generate-notes` (add `--prerelease` for a beta). `git tag` + `git push --tags` alone is **not** enough: a tag without a Release isn't announced.
3. Within a day every instance with the check on shows the pill. Use **check now** on your own instance to see it immediately.

Pre-release identifiers compare numerically (`beta.2` < `beta.10`), so a numbered beta series sorts correctly.

## Tests

`tests/test_nori_app_fetch.py`: which release files are ever stored (only this repo's own `nori-companion-<version>.apk` download), newest wins and no downgrade or repeat, the pre-release and automatic switches, a failed / wrong-size / bad-checksum / not-an-app download leaving the old app in place, the retry cooldown, GitHub-only hosts and redirects, the scheduler hook, and the Updates tab and its button.

`tests/test_nori_updates.py`: version ordering, release cleaning (drafts, odd tags, foreign links, note caps), the request itself (conditional, nothing identifying), 304 / error / garbage handling, the switches and schedule, status and skip, the commands (a bad tag never reaches a command), the admin page (notes escaped), the header pill (admins only), the settings tab (admins only).
