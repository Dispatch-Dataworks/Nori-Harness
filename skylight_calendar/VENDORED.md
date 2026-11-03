# Vendored: skylight-calendar

A copy of the `skylight_calendar` package from
https://github.com/benjaminarthurt/Skylight-Calendar (MIT, see `LICENSE` in this folder),
unchanged, at commit `c0820afe3a5ac66a7bc30ad2ed573db88604b0d0` (0.2.0).

Why a copy and not a dependency: Nori keeps to the standard library plus the few packages in
`requirements.txt` (docs/setup.md#python-dependencies). From 0.2.0 the library's sync client runs on
the standard library (`StdlibClient`), so nothing new is installed. `httpx` is imported only if it is
present and only `AsyncSkylightClient` needs it; Nori uses neither.

Nori's own code is `skylight.py` at the repository root; nothing here knows about Nori.

To update: copy `src/skylight_calendar/` from a newer commit over this folder, keep this file and
`LICENSE`, change the commit above, and run `tests/test_nori_skylight.py`.
