# Deploying to Synology Container Manager

Container Manager runs plain Docker Compose underneath, so the project file in
this folder works as-is. The parts that catch people out are the bind-mount
ownership and how DSM handles `.env` — both covered below.

## 1. Copy the project to the NAS

Put the repository somewhere under your `docker` shared folder, e.g.
`/volume1/docker/doorbird-seasonal`. Over SMB that is
`\\<nas>\docker\doorbird-seasonal`.

Include `.env` with your secrets filled in. If you are **migrating an existing
install**, also bring `data/` across — and note:

> ⚠️ **`FERNET_KEY` must come across unchanged.** Device passwords are
> encrypted with it. Copying `doorbird.db` without the matching key leaves them
> undecryptable: the app starts, then fails every connection with a decrypt
> error. `SECRET_KEY` only signs cookies — changing it just logs you out.

`data/ulaw/` can be skipped; it is a cache and rebuilds itself.

## 2. Create the project

**Container Manager → Project → Create**

- *Project name*: `doorbird-seasonal`
- *Path*: the folder from step 1
- It detects `docker-compose.yml`; choose **Build and start**

The image is compiled on the NAS, so CPU architecture is never an issue. The
first build takes a few minutes because it installs `ffmpeg`.

Then open `http://<nas>:8088/`, log in, and set the base URL under *Settings*
to the address other machines would use — the NAS's LAN address and the
**published** port, not `localhost`.

## Two traps worth knowing

### Bind-mount ownership → "attempt to write a readonly database"

Files copied over SMB are owned by the DSM user that copied them. A container
with a hardcoded `USER` runs as a different uid, cannot write `/data`, and dies
during the startup migration with:

```
sqlite3.OperationalError: attempt to write a readonly database
```

Container Manager then restarts it in a loop.

This image handles it: `entrypoint.sh` starts as root, reads which uid owns
`/data`, and drops to that uid before running the app. It deliberately does
**not** `chown` your files, which would break your own access to them over SMB.
On every boot it logs:

```
entrypoint: starting as uid=<n> gid=<n> (owner of /data)
```

If that line is missing from the log, you are running a **stale image** —
*Start* and *Restart* reuse the cached one, only **Build** recompiles it.

### DSM does not interpolate a project-folder `.env`

Container Manager does not resolve `${VAR}` from a `.env` beside the compose
file, and the fail-fast `${VAR:?message}` form makes DSM reject the project
outright — every API call against it then returns error **2104** while other
projects behave normally.

The compose file here therefore uses **`env_file:`** instead, which injects
`.env` straight into the container and needs no interpolation.

## Verifying

```bash
docker exec <container> python -m app.cli_diagnose <device-name>
```

Checks 1–3 must pass. Check 3 posts one second of silence, so nothing is
audible at the door.

## Editing files on the NAS from Windows

Write config files as **UTF-8 without a BOM, with LF endings**. PowerShell's
`Set-Content -Encoding utf8` emits a **BOM** on Windows PowerShell 5.1, which
is enough to break compose parsing. Use an editor that lets you choose, or
write the bytes from Python.

## Recovering a broken project

If a project gets stuck — API calls returning **2104**, `containerIds` empty,
and neither start nor delete working — the record itself is damaged, usually
after a crash loop. It can only be removed from the **DSM UI**.

Note that creating a replacement against the *same* folder returns **2103**
(the path is already claimed), so copy the folder to a new path and create the
project there, then delete the old one from the UI.

## Ports

DSM occupies 5000/5001 and 80/443. Pick anything free for the app and keep the
base URL under *Settings* in step with it. To list what your containers already
publish, check Container Manager, or query
`SYNO.Docker.Container` and read each container's `port_bindings`.

## Running two instances

Don't. Two instances pointed at one door station both hold a `monitor.cgi`
connection and both play on a ring — you get the chime twice, and they collide
on the device's single audio channel (the loser gets a `503`). Stop the old
deployment before starting the new one.
