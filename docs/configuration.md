# Configuration

All settings live in `/etc/securecam/config.yaml`. Secrets live in `/etc/securecam/securecam.env`.

```bash
sudo nano /etc/securecam/config.yaml
sudo -u securecam securecam-admin check-config
sudo systemctl restart securecam
```

**Always run `check-config` before restarting.** It validates every value, rejects impossible combinations, and tells you exactly which key is wrong. The service refuses to start on a bad config rather than starting in a half-broken state.

Every key is optional. Anything you leave out uses the default in [config/config.example.yaml](../config/config.example.yaml).

- [Secrets](#secrets)
- [device](#device)
- [camera](#camera)
- [mediamtx](#mediamtx)
- [motion](#motion)
- [storage](#storage)
- [ai](#ai)
- [notifications](#notifications)
- [api](#api)
- [network](#network)
- [health](#health)
- [logging](#logging)
- [Common recipes](#common-recipes)

---

## Secrets

No password, token or API key is ever written to `config.yaml`. The config refers to them by **environment variable name** (`*_env` keys), and the values live in a root-owned 0600 file loaded by systemd:

```bash
sudo nano /etc/securecam/securecam.env
sudo systemctl restart securecam
```

```ini
# Signs session cookies and stream tickets. Generated at install time.
# Changing it logs everyone out.
SECURECAM_SECRET_KEY=...

# Credentials SecureCam uses to talk to the MediaMTX control API.
SECURECAM_MEDIAMTX_USER=...
SECURECAM_MEDIAMTX_PASSWORD=...

# Optional
SECURECAM_AI_API_KEY=sk-...
SECURECAM_NTFY_TOKEN=tk_...
SECURECAM_PUSHOVER_TOKEN=...
```

These values are redacted from logs and never returned by the API.

## device

```yaml
device:
  id: ""
  name: "Front Door"
  timezone: ""
```

| Key        | Notes                                                                                                                                                                                                                                                                               |
| ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id`       | Stable identity, generated once at install and stored in `/etc/securecam/device_id`. **Must be unique across your cameras** — user accounts are bound to it, so an account for one camera cannot log into another. Changing it invalidates every session and every account binding. |
| `name`     | Shown in the UI, in notification titles and in the video overlay.                                                                                                                                                                                                                   |
| `timezone` | Empty uses the system timezone. Set an IANA name like `Europe/Amsterdam` if the system clock is UTC but you want local timestamps.                                                                                                                                                  |

## camera

```yaml
camera:
  enabled: true
  width: 1920
  height: 1080
  fps: 15
  bitrate: 3000000
  idr_period: 30
  codec: auto
```

| Key                           | Range                                  | Notes                                                                                                                                                                                |
| ----------------------------- | -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `enabled`                     |                                        | `false` disables the camera path entirely. Only useful for testing.                                                                                                                  |
| `width` / `height`            | 160-4096 / 120-2464                    | 1920x1080 is the sweet spot. Use 1280x720 if you are thermally limited.                                                                                                              |
| `fps`                         | 1-60                                   | **15 is plenty for security footage** and roughly halves both bitrate and encoder load versus 30.                                                                                    |
| `bitrate`                     | 100 kbit - 25 Mbit                     | 3 Mbit/s at 1080p15 looks good and costs about 32 GB/day of disk writes.                                                                                                             |
| `idr_period`                  | 1-600                                  | Keyframe interval **in frames**. Keep it at or below `fps * 2` so the buffer can be cut at any second without re-encoding. Larger values save space but make clip boundaries sloppy. |
| `codec`                       | `auto`, `hardwareH264`, `softwareH264` | `auto` picks the Pi 4's hardware encoder. **Never set `softwareH264` on a fanless Pi.**                                                                                              |
| `h264_profile` / `h264_level` |                                        | Leave on `auto` / `4.1` unless a specific client demands otherwise.                                                                                                                  |
| `camera_id`                   | 0-3                                    | Which CSI camera, if you have more than one.                                                                                                                                         |
| `hflip` / `vflip`             |                                        | Set both `true` if the camera is mounted upside down.                                                                                                                                |
| `denoise`                     | `off`, `cdn_off`, `cdn_fast`, `cdn_hq` | `off` is cheapest. `cdn_fast` helps a lot in low light at a small CPU cost.                                                                                                          |
| `tuning_file`                 |                                        | Absolute path to a libcamera tuning JSON. **Required for NoIR modules**, e.g. `/usr/share/libcamera/ipa/rpi/vc4/imx219_noir.json`.                                                   |
| `text_overlay`                |                                        | Burns a timestamp into the video.                                                                                                                                                    |
| `text_overlay_format`         |                                        | `strftime` format.                                                                                                                                                                   |

Changing anything here rewrites `mediamtx.yml` and restarts MediaMTX, which briefly interrupts the stream and the buffer.

## mediamtx

```yaml
mediamtx:
  path_name: cam
  api_address: "127.0.0.1:9997"
  playback_address: "127.0.0.1:9996"
  rtsp_address: "127.0.0.1:8554"
  hls_address: "127.0.0.1:8888"
  webrtc_address: "0.0.0.0:8889"
  webrtc_local_udp_address: ":8189"
  webrtc_additional_hosts: []
```

| Key                        | Notes                                                                                                                                                                    |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `path_name`                | Stream name. Live URLs become `.../cam`.                                                                                                                                 |
| `api_address`              | MediaMTX control API. **Keep it on 127.0.0.1.**                                                                                                                          |
| `playback_address`         | The playback API SecureCam cuts clips from. **Keep it on 127.0.0.1.**                                                                                                    |
| `rtsp_address`             | Localhost by default. Bind to `0.0.0.0:8554` only if you want VLC/Frigate/Home Assistant to pull the stream, and understand that this is a second authenticated surface. |
| `hls_address`              | Localhost by default. HLS has multi-second latency; WebRTC is used instead.                                                                                              |
| `webrtc_address`           | **The one port meant to be reachable from your LAN.** Still authenticated.                                                                                               |
| `webrtc_local_udp_address` | UDP port for media. Must also be reachable or video falls back to a slower path.                                                                                         |
| `webrtc_additional_hosts`  | Extra IPs/hostnames advertised to browsers. Add your Tailscale IP here for remote live view — `scripts/setup-remote-access.sh` does it for you.                          |
| `binary`, `config_path`    | Where the binary and the **generated** config live. `mediamtx.yml` is rewritten from the template on every start; edits to it are lost.                                  |
| `log_level`                | `error`, `warn`, `info`, `debug`.                                                                                                                                        |

## motion

The section you will actually tune.

```yaml
motion:
  source: pir
  gpio: 17
  pull: down
  active_state: high
  poll_interval_seconds: 0.05
  debounce_seconds: 0.2
  min_active_seconds: 1.0
  warmup_seconds: 60
  pre_event_seconds: 60
  post_motion_seconds: 300
  max_event_seconds: 1800
  cooldown_seconds: 5
```

| Key                     | Range                | Notes                                                                                                                              |
| ----------------------- | -------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `source`                | `pir`, `disabled`    | `disabled` keeps live view and the buffer but never creates events.                                                                |
| `gpio`                  | 0-27                 | **BCM** numbering, not physical pin numbering. GPIO17 is physical pin 11.                                                          |
| `gpio_chip`             |                      | Empty lets gpiozero choose. Set `/dev/gpiochip0` to pin it down.                                                                   |
| `pull`                  | `down`, `up`, `none` | `down` is right for a push-pull PIR output and gives a defined level if the wire falls off. Use `up` for an open-collector module. |
| `active_state`          | `high`, `low`        | The level the sensor outputs **while it sees motion**.                                                                             |
| `poll_interval_seconds` | 0.005-1.0            | 0.05 (20 Hz) is far faster than any PIR responds. Raising it saves negligible CPU.                                                 |
| `debounce_seconds`      | 0-10                 | The level must be stable this long to be believed. **Raise this first if you get spurious events.**                                |
| `min_active_seconds`    | 0-120                | After motion is accepted, ignore "no motion" for at least this long. Tames modules that briefly drop low mid-movement.             |
| `warmup_seconds`        | 0-600                | No events until this long after the service starts. A cold HC-SR501 outputs garbage for the first minute.                          |
| `pre_event_seconds`     | 0-900                | **Seconds of video kept from before the trigger.** This is the whole point of the rolling buffer.                                  |
| `post_motion_seconds`   | 1-3600               | The event closes this long after the last motion.                                                                                  |
| `max_event_seconds`     | 10-21600             | Hard cap, so a stuck sensor cannot record forever.                                                                                 |
| `cooldown_seconds`      | 0-600                | Minimum gap between one event ending and the next starting.                                                                        |

> `storage.buffer_retain_minutes` must comfortably exceed `(pre_event_seconds + max_event_seconds) / 60`. `check-config` enforces this — if the buffer is too short, the oldest part of a long event would be deleted before it could be extracted.

### Tuning by symptom

| Symptom                                          | Change                                                                                                              |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------- |
| Events every few minutes with nobody there       | Turn the sensitivity pot down; raise `debounce_seconds` to 0.5; move the sensor away from heat sources and sunlight |
| One long event instead of several                | Lower `post_motion_seconds`                                                                                         |
| Several events for one visit                     | Raise `post_motion_seconds`; raise `min_active_seconds`                                                             |
| Person already in frame at the start of the clip | Raise `pre_event_seconds`                                                                                           |
| Nothing at all after a restart                   | You are inside `warmup_seconds`                                                                                     |

## storage

```yaml
storage:
  base_path: /var/lib/securecam
  events_path: /var/lib/securecam/events
  buffer_path: /var/lib/securecam/buffer
  buffer_retain_minutes: 60
  buffer_segment_seconds: 60
  retention_days: 7
  max_usage_percent: 80
  min_free_gb: 5
  check_interval_seconds: 300
```

| Key                      | Notes                                                                                                                                  |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------- |
| `buffer_retain_minutes`  | How much rolling video MediaMTX keeps. 60 minutes is about 1.4 GB at 3 Mbit/s. This is the ceiling on how far back an event can reach. |
| `buffer_segment_seconds` | Segment file length. 60 is fine; smaller means more files without any benefit.                                                         |
| `retention_days`         | Saved events older than this are deleted.                                                                                              |
| `max_usage_percent`      | If the filesystem goes above this, the oldest events are deleted until it does not.                                                    |
| `min_free_gb`            | Same, expressed as absolute free space. Whichever limit bites first wins.                                                              |
| `check_interval_seconds` | How often the cleanup pass runs.                                                                                                       |

Cleanup order: retention first, then oldest-first until both limits are satisfied. **An event that is currently recording is never deleted.** If it still cannot free enough, it logs a critical message and the health status turns red rather than filling the card silently.

Estimating: at 3 Mbit/s, one hour of video is about 1.4 GB. Seven days of buffer plus a handful of events per day fits comfortably in 20 GB.

## ai

Optional. Off by default. When off, **no image ever leaves the Pi**.

```yaml
ai:
  enabled: true
  provider: openai_vision
  timeout_seconds: 20
  snapshot_count: 1
  snapshot_interval_seconds: 2
  min_confidence: 0.6
  max_retry_age_hours: 24
  openai_vision:
    base_url: https://api.openai.com/v1
    model: gpt-4o-mini
    api_key_env: SECURECAM_AI_API_KEY
    detail: low
    max_tokens: 200
```

| Key                   | Notes                                                                                                                   |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `provider`            | `openai_vision` for anything speaking the OpenAI chat-completions API. `generic_http` for a custom detector.            |
| `snapshot_count`      | Images sent per event. 1 is usually enough and costs the least.                                                         |
| `min_confidence`      | Below this, the result is treated as "no person".                                                                       |
| `max_retry_age_hours` | Give up retrying an event's AI call after this long. The recording is unaffected.                                       |
| `base_url`            | Point this at **any** compatible endpoint — Ollama, vLLM, LM Studio, an OpenRouter proxy, or a machine on your own LAN. |
| `detail`              | `low` keeps token cost and latency down and is plenty for "is there a person".                                          |

```bash
sudo -u securecam securecam-admin test-ai
sudo -u securecam securecam-admin test-ai --image /path/to/photo.jpg
```

A local model on the LAN, for example:

```yaml
ai:
  enabled: true
  provider: openai_vision
  openai_vision:
    base_url: http://192.168.1.50:11434/v1
    model: llava:13b
    api_key_env: SECURECAM_AI_API_KEY # Ollama ignores it; set it to anything
```

### generic_http

For a detector with its own API:

```yaml
ai:
  provider: generic_http
  generic_http:
    endpoint: "http://192.168.1.50:8000/detect"
    api_key_env: SECURECAM_AI_API_KEY
    auth_header: Authorization
    auth_scheme: Bearer
    encoding: multipart # or json_base64
    field_name: image
    person_field: person_detected
    confidence_field: confidence
    label_field: label
```

The `*_field` keys accept dotted paths into the JSON response, e.g. `result.person`.

**AI is never load-bearing.** If the endpoint is down, the event is still recorded and, unless `notifications.only_if_person` is set, the notification is still sent.

## notifications

```yaml
notifications:
  enabled: true
  provider: ntfy
  alarm: true
  include_snapshot: true
  only_if_person: false
  cooldown_seconds: 0
  max_retry_age_hours: 24
  recipients:
    - id: my-phone
      enabled: true
      provider: ""
      target: "long-random-topic-name"
      alarm: true
```

| Key                     | Notes                                                                                                                                               |
| ----------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `alarm`                 | Ask the provider for its loudest, most persistent delivery — the setting that gets through Do Not Disturb.                                          |
| `include_snapshot`      | Attach the event snapshot.                                                                                                                          |
| `only_if_person`        | Notify only when AI reports a person. Requires `ai.enabled`. **If AI fails, the notification is sent anyway** — fail open, never miss a real alarm. |
| `cooldown_seconds`      | Suppress repeats within this window. `0` never suppresses.                                                                                          |
| `recipients[].target`   | ntfy: the topic name. Pushover: the user or group key.                                                                                              |
| `recipients[].provider` | Per-recipient override, so one person can get ntfy and another Pushover.                                                                            |

At least one enabled recipient with a real target is required when `enabled: true`. `CHANGE-ME` is rejected.

### ntfy

```yaml
ntfy:
  server: https://ntfy.sh
  token_env: SECURECAM_NTFY_TOKEN
  priority: 5
  tags: ["rotating_light"]
  call: ""
```

1. Install the ntfy app on your phone.
2. Subscribe to a **long random topic name** — treat it like a password.
3. Put that name in `recipients[].target`.
4. In the app, open the topic's settings and allow it to override Do Not Disturb / use the alarm sound.

Priority 5 is what makes the phone actually ring rather than buzz once. `call` (a phone number, or `yes`) makes ntfy.sh phone you; it needs a paid account with a verified number.

> **Anyone who knows the topic name can read your notifications**, including the snapshot. Self-host ntfy and set `token_env` if that matters to you.

### Pushover

```yaml
pushover:
  api_url: https://api.pushover.net/1/messages.json
  token_env: SECURECAM_PUSHOVER_TOKEN
  priority: 2
  retry_seconds: 30
  expire_seconds: 600
  sound: persistent
```

1. Create an application at pushover.net to get an **API token** — that goes in `securecam.env`.
2. Your **user key** goes in `recipients[].target`.

Priority 2 is _emergency_: Pushover repeats the alert every `retry_seconds` until someone acknowledges it, for up to `expire_seconds`, ignoring quiet hours.

```bash
sudo -u securecam securecam-admin test-notify
```

## api

```yaml
api:
  enabled: true
  address: "0.0.0.0"
  port: 8080
  internal_auth_port: 9095
  web_ui: true
  session_ttl_hours: 12
  stream_ticket_ttl_seconds: 120
  public_base_url: ""
  tls:
    enabled: false
    cert_file: ""
    key_file: ""
  rate_limit:
    login_attempts: 10
    window_seconds: 300
```

| Key                         | Notes                                                                                                                                                           |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `enabled`                   | **Disabling this also denies live viewing**, because MediaMTX authenticates against it. PIR recording keeps working.                                            |
| `address`                   | `0.0.0.0` for LAN access, `127.0.0.1` to only allow SSH tunnels.                                                                                                |
| `internal_auth_port`        | Loopback-only listener that answers MediaMTX auth callbacks. Always plain HTTP, even when `tls.enabled` — that is why it is a separate listener. Never exposed. |
| `web_ui`                    | `false` serves the JSON API only.                                                                                                                               |
| `session_ttl_hours`         | How long a login lasts.                                                                                                                                         |
| `stream_ticket_ttl_seconds` | Lifetime of the one-shot credential the browser uses for the WebRTC handshake. Short on purpose.                                                                |
| `public_base_url`           | Absolute URL used in notification links, e.g. `https://frontdoor.tailnet.ts.net:8080`.                                                                          |
| `rate_limit`                | Failed logins per client address per window.                                                                                                                    |

### TLS

```yaml
api:
  tls:
    enabled: true
    cert_file: /etc/securecam/cert.pem
    key_file: /etc/securecam/key.pem
```

The key file must be readable by the `securecam` user and by nobody else:

```bash
sudo chown securecam:securecam /etc/securecam/key.pem
sudo chmod 600 /etc/securecam/key.pem
```

A self-signed certificate produces a browser warning every time. Over Tailscale you do not need TLS at all — the tunnel already encrypts everything. See [security.md](security.md).

## network

```yaml
network:
  check_interval_seconds: 60
  retry_initial_seconds: 10
  retry_max_seconds: 300
  internet_probe_hosts: ["1.1.1.1:443", "8.8.8.8:443"]
  dns_probe_host: cloudflare.com
  probe_timeout_seconds: 4
```

Connectivity is probed with plain **TCP connects**, not ICMP, so no raw sockets and no root are needed. When the network comes back, queued AI calls and notifications are retried with exponential backoff.

Change `internet_probe_hosts` if your network blocks those addresses. An empty list disables internet probing, and connectivity is then reported as unknown.

## health

```yaml
health:
  interval_seconds: 30
  state_file: /run/securecam/health.json
  cpu_temp_warn_celsius: 75
  cpu_temp_critical_celsius: 80
  disk_warn_percent: 80
```

Every check produces a status (`ok`, `degraded`, `critical`, `unknown`), a message, and a **remedy** — visible in the Status tab, in `GET /api/health`, and in `scripts/health-check.sh`. The state file lets external monitoring read the status without an HTTP request.

The Pi 4 hard-throttles at 80 °C, which is why the critical threshold is there and not higher.

## logging

```yaml
logging:
  level: INFO
  format: text
  file: ""
  max_bytes: 5242880
  backup_count: 3
```

`file: ""` logs to stdout only, which systemd captures into the journal:

```bash
sudo journalctl -u securecam -f
sudo journalctl -u securecam --since "1 hour ago" -p warning
```

Set `format: json` if you ship logs somewhere that parses them. `level: DEBUG` is very chatty — useful for diagnosing motion timing, not for leaving on.

## Common recipes

**Quieter camera, cooler Pi**

```yaml
camera: { width: 1280, height: 720, fps: 10, bitrate: 1500000, idr_period: 20 }
```

**Longer memory: 30 days of events, 2 hours of buffer**

```yaml
storage:
  { retention_days: 30, buffer_retain_minutes: 120, max_usage_percent: 90 }
motion: { pre_event_seconds: 120 }
```

Check you have the disk for it first: `df -h /var/lib/securecam`.

**Only tell me about people**

```yaml
ai: { enabled: true }
notifications: { enabled: true, only_if_person: true }
```

**Recording only, no network services**

```yaml
api: { enabled: false }
notifications: { enabled: false }
ai: { enabled: false }
```

Events still record. You retrieve them over SSH.

**Indoor camera that only watches at night** — SecureCam has no schedule feature. Use systemd timers:

```bash
sudo systemctl edit --force --full securecam-night.timer
```

...or simply `systemctl stop securecam` from a cron job during the day.
