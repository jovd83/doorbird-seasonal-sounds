# DoorBird Seasonal Sounds — Home Assistant integration

A custom Home Assistant integration with the same scope as the Docker
sibling at `../` (FastAPI webapp): manage multiple DoorBirds, configure
seasonal MP3 schedules, and have them auto-applied daily — but living
entirely inside Home Assistant.

```
home_assistant/
├── README.md                                ← you are here
├── example_configuration.yaml               ← copy chunks of this into your HA config
└── custom_components/doorbird_seasonal/
    ├── manifest.json
    ├── __init__.py            ← setup, scheduler, services, reconcile engine
    ├── const.py
    ├── client.py              ← async DoorBird cloud API client (api.doorbird.io)
    ├── date_logic.py          ← annual / one-off / year-wrap window matching
    ├── sensor.py              ← hub + per-device state sensors
    ├── services.yaml          ← service descriptions for HA UI
    └── translations/en.json
```

## Install

### Manual (HACS-free)

1. Copy `custom_components/doorbird_seasonal/` to your HA config directory:
   ```
   /config/custom_components/doorbird_seasonal/
   ```
2. Create the MP3 folder and drop your files in (default path
   `/config/doorbird_seasonal/mp3/`):
   ```
   mkdir -p /config/doorbird_seasonal/mp3
   cp default.mp3 christmas.mp3 easter.mp3 summer.mp3 \
      /config/doorbird_seasonal/mp3/
   ```
3. Add the configuration block (see below) to your `configuration.yaml`,
   store the DoorBird admin passwords in `secrets.yaml`, and **restart
   Home Assistant**.

### HACS (optional)

If you publish this repo under your GitHub account, HACS users can add it
as a custom repository pointing at the `home_assistant/` subdir as the
integration root.

## Configuration

```yaml
# configuration.yaml
doorbird_seasonal:
  mp3_dir: /config/doorbird_seasonal/mp3
  default_mp3: default.mp3
  daily_run_time: "03:15"

  devices:
    - name: Front door
      host: 192.168.1.50                # LAN IP/hostname of the device
      username: ggaaaa0000              # the device ADMIN user (ends in 0000)
      password: !secret doorbird_front
    - name: Garage
      host: 192.168.1.51
      username: ggaaaa0001
      password: !secret doorbird_garage

  schedules:
    - name: Christmas
      from: "12-20"
      to:   "01-06"                     # window wraps year-end automatically
      mp3:  christmas.mp3
      priority: 200

    - name: Easter 2026
      from: "04-03"
      to:   "04-06"
      year: 2026                        # one-off, only this year
      mp3:  easter.mp3
      priority: 200

    - name: Summer
      from: "06-21"
      to:   "09-22"
      mp3:  summer.mp3
      priority: 50

    - name: Birthday
      from: "07-14"                     # single day (no `to:`)
      mp3:  birthday.mp3
      priority: 300
```

## What it gives you

### Sensors

Hub sensors:
- `sensor.doorbird_seasonal_active_sound` — today's resolved MP3 filename
- `sensor.doorbird_seasonal_active_reason` — *which* schedule won, or "default"
- `sensor.doorbird_seasonal_next_change` — next date + label
- `sensor.doorbird_seasonal_last_reconcile` — UTC timestamp

Per-device sensors (one each per DoorBird):
- `sensor.<device>_last_applied_sound`
- `sensor.<device>_last_applied_at`
- `sensor.<device>_status` — `ok` or `error: ...`

Each DoorBird also appears as a **device** in Home Assistant's device registry,
so you can group them in dashboards.

### Services

- `doorbird_seasonal.apply_now` — resolve today, push to all enabled devices.
  Set `force: true` to re-upload even if unchanged.
- `doorbird_seasonal.set_button_sound` — one-off upload + activate. Args:
  `mp3` (filename inside `mp3_dir`), `devices` (optional list of device names).
- `doorbird_seasonal.activate_builtin` — switch to a stock DoorBird sound
  without uploading. Args: `sound`, `devices` (optional).
- `doorbird_seasonal.test_connection` — verify each device credential.

All services are visible in **Developer Tools → Services** with auto-complete.

### Scheduler

A `homeassistant.helpers.event.async_track_time_change` fires once per day at
your `daily_run_time` (default 03:15 local) and runs the same reconcile that
`apply_now` does. The reconcile is also fired once at integration startup so
the state sensors populate immediately.

## Tying it into your existing automations

The sensors above are first-class HA state — use them anywhere:

```yaml
# Example: send a notification the morning of a change
automation:
  - alias: Heads up — DoorBird sound changing today
    trigger:
      - platform: time
        at: "08:00:00"
    condition:
      - condition: template
        value_template: >-
          {{ states('sensor.doorbird_seasonal_next_change').startswith(
               now().date().isoformat()) }}
    action:
      - service: notify.mobile_app_your_phone
        data:
          message: "DoorBird will swap to {{ states('sensor.doorbird_seasonal_next_change') }}"
```

```yaml
# Example: a button on a Lovelace dashboard that forces a reconcile
action:
  - service: doorbird_seasonal.apply_now
    data:
      force: true
```

## How it talks to your DoorBirds

Same as the Docker sibling: HTTPS POSTs to `https://api.doorbird.io/` using
HTTP Basic Auth with each device's administration credentials. Endpoints
`POST /other/buttonsound/file` (raw MP3 body) followed by
`POST /other/buttonsound` (`{"buttonSound":"custom"}`). Discovery trail is
in `../discovery/README.md`.

The HA host needs outbound HTTPS to `api.doorbird.io`. No LAN access to the
DoorBirds is required.

## Differences vs the Docker sibling

| Capability | Docker app | HA integration |
|---|---|---|
| Web UI | yes, built-in | uses HA's Developer Tools + sensors + automations |
| MP3 library | uploaded via web | drop files in `mp3_dir` |
| Schedules | added via web | YAML in `configuration.yaml` |
| Storage | SQLite under `data/` | in-memory + `mp3_dir` |
| Audit log | dedicated DB table + page | HA's logbook + log entries (search for `doorbird_seasonal`) |
| Auth | session login | HA's own user/permissions model |
| Backup | bind-mounted `data/` | HA's normal config backup |

Pick whichever fits your habits. They're functionally equivalent.
