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

## What version is this?

The `VERSION` file in the repo root (for example `0.10.0-beta`), baked into the image by the build (`.git` isn't). `NORI_VERSION` overrides it. Versions are compared as [semver](https://semver.org): a release outranks its own pre-releases, and `0.10.0` is later than `0.9.0`. An instance running something newer than the latest release (a maintainer on `main`) is simply "up to date".

## Cutting a release (maintainers)

1. Bump `VERSION` (bare number, no `v`), add the changelog entry (`docs/changelog.md`), commit.
2. Tag and publish. One command does both and writes the notes people will read in the Updates tab:

   ```
   gh release create v0.11.0 --title "Nori 0.11.0" --generate-notes
   gh release create v0.11.0-beta.1 --title "Nori 0.11.0 beta 1" --generate-notes --prerelease    # a pre-release
   ```

   (`git tag` + `git push --tags` alone is **not** enough: a tag without a Release isn't announced.)
3. Within a day every instance with the check on shows the pill. Use **check now** on your own instance to see it immediately.

Pre-release identifiers compare numerically (`beta.2` < `beta.10`), so a numbered beta series sorts correctly.

## Tests

`tests/test_nori_updates.py`: version ordering, release cleaning (drafts, odd tags, foreign links, note caps), the request itself (conditional, nothing identifying), 304 / error / garbage handling, the switches and schedule, status and skip, the commands (a bad tag never reaches a command), the admin page (notes escaped), the header pill (admins only), the settings tab (admins only).
