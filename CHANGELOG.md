# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — Unreleased

### Added

### Changed

### Fixed

### Removed

## [0.1.0] — 2026-08-29

First tagged release. The app had been running for months before this point;
this entry records the state it was published in, including the hardening pass
that preceded it.

### Added

- **Date-based chime schedules** with `MM-DD` ranges, recurring yearly or
  pinned to a year, and year-end wrap-around (`12-20 → 01-06`).
- **Auto responses** — a spoken message played a configurable interval after
  the chime, scheduled like a chime but resolved independently.
- **Collections** — point a schedule at a bag of sounds; a different member
  plays on each ring, never the same one twice running.
- **Time-of-day windows** per schedule, optionally wrapping midnight.
- **Per-device targeting**, priority, and specificity tie-breaking.
- **Two trigger modes** — a passive `monitor.cgi` listener, or a token-guarded
  webhook. Neither writes anything to the door station.
- **Audit log** with CSV export, plus `AUDIT_RETENTION_DAYS` pruning in the
  daily job.
- **Per-device TLS verification** (`verify_tls`), off by default so a stock
  self-signed door station keeps working.
- **Alembic migrations.** A database predating Alembic is detected, brought to
  the baseline shape once, stamped, then upgraded — existing rows are never
  re-created or rewritten.
- **Home Assistant custom component** as an optional side-car, with an
  agreement test suite that fails if its vendored schedule rules ever diverge
  from the app's.

### Security

- **Login redirect** is restricted to same-site paths. `?next=` previously
  accepted an absolute URL and emitted it as a `Location` header.
- **Passwords** are hashed with stdlib `scrypt` instead of compared as
  plaintext, with a constant-time username check and a per-IP lockout.
  `ADMIN_PASSWORD_HASH` is preferred; a plaintext `ADMIN_PASSWORD` still works
  but warns at startup.
- **CSRF tokens** on every mutating form, enforced by a router dependency.
  Logout is a `POST`.
- **Uploads** are streamed to disk in bounded chunks and rejected past
  `MAX_UPLOAD_BYTES`, rather than read into memory whole and merely annotated.
- **Device credentials** no longer reach the process table — the endpoint probe
  passes them to `curl` on stdin instead of `-u` on the command line.
- **Session cookie** `Secure` flag is configurable (`SESSION_HTTPS_ONLY`) with
  an explicit lifetime, instead of hardcoded off.

### Fixed

- **Editing a device now reaches its listener.** Watchers cached the
  credentials they were built with, so a changed host, username or password
  did nothing until the container restarted.
- **Blocking network calls** moved off the event loop. One unreachable door
  station could freeze every request, the ring webhook included.
- **Watchers can be stopped.** The monitor stream carries a read timeout, so a
  watcher notices its own stop flag instead of blocking until the next ring.
- **A lost or rotated `FERNET_KEY` no longer crashes startup.** One device with
  an unreadable password is isolated and reported; the rest carry on.
- **SQLite runs in WAL mode** with a longer busy timeout — the previous
  `delete` journal serialised readers against writers across the app's several
  writer threads.
- **The error page no longer touches the database**, so a database failure
  produces an error page instead of a second exception.
- **The audit CSV genuinely streams** instead of building the whole export in
  memory first.
- **Auto responses are cancellable** rather than parking an OS thread on
  `time.sleep` for up to an hour.
- **The webhook relay task is retained**, closing an `asyncio` weak-reference
  race that could drop it before it ran.

### Changed

- `doorbird_client.py` (558 lines) split into `app/doorbird/` — types, HTTP
  client, raw-socket audio transmit, and diagnostics.
- `ring_watcher.py` split into `app/ring/` — debounce, playback, watcher.
- Shell state resolved once per request instead of queried from inside Jinja,
  cutting database sessions per page render by roughly half.
- The image installs from a pinned `requirements.txt` rather than a dependency
  list restated inline in the Dockerfile.
- Dev-only probe scripts moved from `app/` to `tools/` and excluded from the
  image.
- Dropped the unused `passlib[bcrypt]` dependency, which could not drive
  bcrypt 5.x in any case.

[0.2.0]: https://github.com/jovd83/doorbird-seasonal-sounds/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jovd83/doorbird-seasonal-sounds/releases/tag/v0.1.0
