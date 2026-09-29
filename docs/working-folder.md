# The working folder

Source: `nori/workfiles.py` (files she can read/write/download into)
and `nori/scanner.py` (malware scanning for anything downloaded from
outside).

## Shape

Each user has their own root under `workfiles/<user_id>/` (see
[Setup](setup.md#what-lives-where-on-disk)) — files are per-person, not
shared workspace state the way [tasks/notes](tasks-notes-reminders-trackers.md)
are. Tools: `list_files` (optionally recursive), `read_file` (with the
same [content-screening](content-screening.md) `preserve_content`
option used elsewhere for untrusted text), `search_files`, and
`read_image_bytes`/vision captioning for images. A per-user quota is
checked before any write completes, not after.

## Downloading a file from a URL

`download_image(session, url, filename)` doesn't write straight into
the working folder. It fetches into a **quarantine directory**
(`QUARANTINE_DIR`), deliberately **outside** `WORKFILES_DIR` entirely —
structurally, not by a flag that could be forgotten — scans it there,
and only *moves* it into the real working folder on a clean verdict.

## Three verdicts, not two

`scanner.py` is a thin, swappable interface in front of one antivirus
engine (Windows Defender by default, selected via
`NORI_SCANNER_BACKEND`, one function + one line to add another) — but
its own real contribution is the vocabulary: **clean** (scanned,
nothing matched — promote), **infected** (a real match — delete,
never promote), and, critically, **unavailable** (the backend
couldn't produce a real verdict at all: the binary is missing, it
crashed, it timed out, or the configured backend name doesn't exist).

`unavailable` is **not** treated as clean — this is the actual point of
having three verdicts instead of two. The failure mode this exists to
prevent is exactly the one where the safety check silently didn't run
and nobody noticed, on a machine or a moment when the antivirus product
happened to be unavailable. A caller that collapsed "unavailable" into
"proceed anyway" would reintroduce the exact risk scanning exists to
close. See [The enforcement model](enforcement-model.md) — this is a
real, code-enforced deny-by-default, the same posture this project's
own `check_secrets.py` pre-commit scan takes for the identical reason.

## What this means for you if you're extending it

Any new way of getting external content into the working folder — a
different download tool, an MCP server that can write files, a new
connector — needs to go through the same quarantine-then-scan path, not
a direct write. See [Contributing](contributing.md).
