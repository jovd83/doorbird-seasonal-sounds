# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] — Unreleased

### Added

### Changed

- **The day rule is now `All` / `Custom` with a dialog behind it.** Seven
  weekday chips and a holiday disclosure in every table row was more furniture
  than a column that usually just says "all days" deserves. The row now carries
  two radios and a one-line summary — *Mo–Fr · 2 holidays · skipping* — and
  everything else moved into a modal that opens when Custom is picked, or by
  clicking the summary. The modal is driven by a checkbox, so it still opens
  with scripting off, the same way the mobile "More" sheet does.
- The form field is `day_mode` (`all` / `custom`) rather than the previous
  hidden marker. Anything that is not `custom` — including the field being
  absent, which is what a client written before this sends — means every day.
- The inline-edit table is ~190px narrower again, since the Days column no
  longer has to hold seven toggles.

### Fixed

### Removed

## [0.2.0] — 2026-08-30

### Added

- **Day-of-week rules on a schedule.** Seven weekday toggles with Mo–Fr,
  Sa–Su and every-day presets, editable inline in the schedule row alongside
  every other control on it.
- **Belgian holidays.** Nineteen entries in three groups — the ten federal
  public holidays, three community days, six observances. A day matches when it
  is a ticked weekday *or* a ticked holiday, so "Mo–Fr plus Christmas" fires on
  Christmas even when it falls on a Sunday.
- **A "skip public holidays" switch**, the one subtraction in the rule: it
  drops a day that matched only by its weekday when it is one of the ten public
  holidays. It never drops a holiday that was ticked explicitly, and it is
  disabled when no weekday is ticked for it to act on.
- **A Holidays page** listing the catalogue, each entry's rule, the date it next
  falls on, and the schedules using it.
- **A stored century of moveable dates.** The five holidays that move with
  Easter are computed once and written to `holiday_dates` — 500 rows, a hundred
  years ahead — so resolving a ring is a lookup rather than a calculation. The
  horizon is topped up at every start, and nothing is ever deleted.

### Changed

- **Migration `0003_days_holidays` runs on the first start after upgrading.**
  It adds two columns to `schedules`, creates `schedule_holidays` and
  `holiday_dates`, and the store fills on the same boot. Existing schedules are
  set to every day with no holidays — exactly what they did before — so the
  upgrade changes nothing about when a doorbell sounds. Take a copy of
  `doorbird.db` first, as with any schema change.
- **The manual now says when an auto response actually reaches the speaker.**
  The wait interval is counted from the end of the chime rather than the button
  press, and the door station stops playing transmitted audio once the ring
  session closes — which is why a message can be logged `OK` and never heard.
- **Specificity tie-breaking now considers the day rule.** The order is
  priority, then the narrowest time window, then the fewest weekdays, then the
  narrowest date range. Every schedule that predates this covers all seven days
  and so scores identically, which is why nothing that already existed is
  reordered.
- The Home Assistant component's vendored `date_logic` learned the same rule,
  and now carries a byte-identical copy of `app/holidays.py`. A test fails if
  the two files drift apart.

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
