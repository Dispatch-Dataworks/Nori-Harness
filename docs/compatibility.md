# Compatibility

What you can rely on between one version of Nori and the next. Scope is
narrow, on purpose: two things an operator's own data and configuration
actually depend on — the `nori.db` schema, and config keys (the settings
this app reads via `config.get`/`config.set`, declared in `config._SPEC`).
Nothing else (module internals, tool names, HTML/CSS on a settings page)
carries a promise; only these two do, because only these two hold *your*
state across an upgrade.

There is deliberately no third category for a stable external API: nothing
here documents an HTTP endpoint as a contract for a third-party integration
to build against. The endpoints that exist serve this app's own UI, and can
change between any two versions without notice. If that changes, this page
changes with it.

This follows the same versioning shape the PACI protocol's own spec uses
(see [Peer agents](peer-agents.md) for what PACI is; its specification is
maintained in its own separate repository, not this one — see
[NOTICE](../NOTICE)), so the two read as one house style rather than two
inconsistent policies: patch and minor are additive-only, and a real break
lives at a major version, stated explicitly, never implied.

## `nori.db`'s schema

- **Patch:** unchanged. A patch release never touches the schema.
- **Minor:** additive only. A new table or column may be added (via
  `_MIGRATIONS`, applied automatically on every startup — see
  [Deployment & supervision](deployment-and-watchdog.md)'s "Upgrading"
  section); an existing column's name, type, and meaning never change,
  and nothing existing is ever dropped.
- **Major:** may remove, rename, or restructure something existing. Called
  out explicitly in [the changelog](changelog.md), with what changed and
  what (if anything) an operator needs to do about it — never a silent
  breaking migration.

## Config keys

- **Patch:** unchanged.
- **Minor:** may add a new key, with a default that preserves today's
  behavior for anyone who never touches it. An existing key keeps its
  name, its scope (`user` or `workspace`), and what it actually controls —
  a minor version never quietly repurposes what a key you already set
  means.
- **Major:** may remove or repurpose an existing key. Same rule as the
  schema: called out explicitly in the changelog, not left for a stranger
  to discover mid-troubleshooting.

## What this means in practice

An upgrade within the same major version should never require you to
touch a config value or hand-edit the database to keep working the way
you already were — the whole point of the two rules above. If an upgrade
ever does require that without a major version bump and an explicit
changelog entry describing it, that's a bug in the release, not something
you did wrong.
