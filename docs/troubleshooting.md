# Troubleshooting

Start here, in this order:

```bash
sudo ./scripts/diagnose.sh
systemctl status securecam securecam-mediamtx
sudo journalctl -u securecam -n 50 --no-pager
```

Every SecureCam error message tells you what failed, why it probably failed, whether the rest still works, and the next command to run. If one does not, that is a bug — please report it.

- [The service will not start](#the-service-will-not-start)
- [No video](#no-video)
- [Live view does not load](#live-view-does-not-load)
- [Motion problems](#motion-problems)
- [Recording problems](#recording-problems)
- [Storage problems](#storage-problems)
- [Notification problems](#notification-problems)
- [AI problems](#ai-problems)
- [Login problems](#login-problems)
- [Remote access problems](#remote-access-problems)
- [Performance problems](#performance-problems)
- [Getting help](#getting-help)

---

## The service will not start

```bash
sudo journalctl -u securecam -n 50 --no-pager
```

### `ConfigError: N problem(s) found`

The config is invalid and the service refuses to start half-configured. The message names each bad key.

```bash
sudo -u securecam securecam-admin check-config
sudo nano /etc/securecam/config.yaml
```

### `Permission denied` on `/var/lib/securecam` or `/etc/securecam`

Something was created as root that should belong to the service user.

```bash
sudo chown -R securecam:securecam /var/lib/securecam
sudo chown root:securecam /etc/securecam/config.yaml
sudo chmod 640 /etc/securecam/config.yaml
sudo systemctl restart securecam
```

This is almost always caused by running `securecam-admin` as root instead of `sudo -u securecam securecam-admin`.

### `SECURECAM_SECRET_KEY is not set`

The env file is missing or not being loaded.

```bash
sudo ls -l /etc/securecam/securecam.env      # should be -rw------- root root
sudo grep -c SECRET_KEY /etc/securecam/securecam.env
```

If it is gone, generate a new one (this logs everyone out but loses nothing else):

```bash
printf 'SECURECAM_SECRET_KEY=%s\n' "$(openssl rand -hex 32)" | sudo tee -a /etc/securecam/securecam.env
sudo systemctl restart securecam
```

### `Address already in use`

Another process holds port 8080.

```bash
sudo ss -tlnp | grep 8080
```

Stop it, or change `api.port`.

### The unit restarts in a loop

```bash
sudo systemctl status securecam
sudo journalctl -u securecam --since "10 minutes ago" --no-pager
```

`Watchdog timeout` means the main loop stopped pinging systemd — a genuine hang. Capture the log and open an issue.

## No video

```bash
sudo ./scripts/diagnose-camera.sh
```

### The camera is not detected

```bash
rpicam-hello --list-cameras
```

Empty output means the OS cannot see the camera at all — SecureCam is not involved yet.

1. **Power off completely** and reseat the ribbon cable at both ends. Silver contacts face the HDMI ports on the Pi.
2. Check you used the **CSI** port, not the DSI display port.
3. Check the ribbon is not cracked at the fold.
4. `sudo apt update && sudo apt full-upgrade` — a Camera Module 3 needs a recent `libcamera`.
5. Test the camera on a second Pi if you have one.

### The camera is detected but MediaMTX gets nothing

```bash
sudo journalctl -u securecam-mediamtx -n 50 --no-pager
```

`Device or resource busy` means something else has the camera:

```bash
sudo fuser -v /dev/video0
sudo systemctl stop securecam-mediamtx
rpicam-jpeg -o /tmp/t.jpg -t 1000      # works now? then it was a leftover process
sudo systemctl start securecam-mediamtx
```

`permission denied` means the service user lost its group membership:

```bash
groups securecam                       # expect video and render
sudo usermod -aG video,render securecam
sudo systemctl restart securecam-mediamtx
```

### The image is upside down or mirrored

```yaml
camera:
  hflip: true
  vflip: true
```

### The image is dark, purple or washed out

A NoIR module without a tuning file:

```yaml
camera:
  tuning_file: /usr/share/libcamera/ipa/rpi/vc4/imx219_noir.json
```

```bash
ls /usr/share/libcamera/ipa/rpi/vc4/
```

## Live view does not load

The **Events** tab working while **Live** does not means the API is fine and the problem is WebRTC.

### Check the ports

```bash
sudo ss -tlnp | grep 8889          # TCP, WebRTC signalling
sudo ss -ulnp | grep 8189          # UDP, WebRTC media
```

Both must be open between your browser and the Pi. If you run a firewall:

```bash
sudo ufw allow 8080/tcp
sudo ufw allow 8889/tcp
sudo ufw allow 8189/udp
```

### It connects and then freezes

The browser got the signalling but not the media — almost always the UDP port. Test from your laptop:

```bash
nc -vzu <pi-ip> 8189
```

### It works on the LAN but not over Tailscale

MediaMTX is advertising only its LAN address. Add the Tailscale IP:

```yaml
mediamtx:
  webrtc_additional_hosts: ["100.x.y.z"]
```

Or just re-run `sudo ./scripts/setup-remote-access.sh`, which does this for you.

### `401` on the WebRTC request

The stream ticket expired or the device id changed. Reload the page. If it persists:

```bash
sudo journalctl -u securecam -n 30 | grep -i denied
```

The denial log line says exactly which rule rejected it.

## Motion problems

### No events ever

```bash
sudo ./scripts/diagnose-pir.sh
sudo ./scripts/pir-test.py --gpio 17
```

Wave at the sensor while `pir-test.py` runs. If the level never changes:

| Check          | Fix                                                                    |
| -------------- | ---------------------------------------------------------------------- |
| Warmup         | Wait 60 seconds after power-on. A cold HC-SR501 is unreliable.         |
| Trigger jumper | Must be on **H** (repeat trigger), not L.                              |
| Wiring         | VCC to a 5 V pin, GND to a ground pin, OUT to the GPIO you configured. |
| Pin number     | `motion.gpio` is **BCM** numbering. GPIO17 is _physical pin 11_.       |
| Polarity       | Try `--active-low`, and `motion.active_state: low`.                    |
| Pull           | Try `--pull up` for an open-collector module.                          |
| The sensor     | They are $2. Try another one.                                          |

If `pir-test.py` sees transitions but SecureCam does not create events:

```bash
sudo journalctl -u securecam -f | grep -i motion
```

Check `motion.source` is `pir` and not `disabled`, and that you are past `warmup_seconds`.

### Constant false events

In order of effectiveness:

1. Turn the **sensitivity** potentiometer down (counter-clockwise).
2. Raise `motion.debounce_seconds` to `0.5`.
3. Move the sensor away from sunlight, heating vents, radiators and windows.
4. Physically mask part of the lens dome with tape to narrow the field of view.
5. Turn on AI with `notifications.only_if_person: true` so at least you are only _told_ about people.

### One giant event instead of several

Lower `motion.post_motion_seconds`.

### Several events for one visitor

Raise `motion.post_motion_seconds` and `motion.min_active_seconds`.

## Recording problems

### Events exist but `recording.mp4` is missing or zero bytes

```bash
sudo journalctl -u securecam -n 50 | grep -i recording
```

Check MediaMTX is up and the buffer is being written:

```bash
systemctl is-active securecam-mediamtx
ls -lh /var/lib/securecam/buffer/ | tail
```

An empty buffer directory means MediaMTX is not recording — see [No video](#no-video).

If the buffer has files but extraction fails, the requested range is probably older than the buffer:

```yaml
storage:
  buffer_retain_minutes: 120 # must exceed pre_event + max_event, with slack
```

### The clip does not include the time before the trigger

`motion.pre_event_seconds` cannot reach further back than the buffer holds. Both `pre_event_seconds` and `buffer_retain_minutes` need to be large enough. `check-config` warns about this combination.

### Events stuck in `interrupted`

The service was killed mid-event, most likely by a power cut. On the next start it recovers what is still in the buffer. Events older than the buffer window cannot be recovered — the video is genuinely gone.

## Storage problems

```bash
sudo ./scripts/diagnose-storage.sh
df -h /var/lib/securecam
du -sh /var/lib/securecam/*
```

### The disk keeps filling up

Cleanup runs every `storage.check_interval_seconds`. If it cannot keep up:

```yaml
storage:
  retention_days: 3
  max_usage_percent: 70
  buffer_retain_minutes: 30
camera:
  bitrate: 1500000
```

Something else may be the real consumer:

```bash
sudo du -xh / --max-depth=2 2>/dev/null | sort -rh | head -20
sudo journalctl --vacuum-size=100M
```

### `read-only file system`

The SD card has failed or the filesystem was remounted read-only after errors.

```bash
dmesg | grep -iE "ext4|mmc|I/O error"
```

Back up what you can immediately and replace the card. Cards used for continuous recording do wear out — see [hardware.md](hardware.md#storage) for moving to an SSD.

### Deleting an event fails

An event that is currently recording cannot be deleted. Wait for it to finish, or restart the service to close it.

## Notification problems

```bash
sudo -u securecam securecam-admin test-notify
```

The output names the recipient and the exact provider response.

| Symptom                        | Cause                                                                                          |
| ------------------------------ | ---------------------------------------------------------------------------------------------- |
| `no recipients enabled`        | `recipients[].enabled` is false, or `target` is still `CHANGE-ME`                              |
| `401` / `403`                  | Wrong or missing token in `/etc/securecam/securecam.env`                                       |
| `name resolution failed`       | No DNS. `sudo ./scripts/diagnose-network.sh`                                                   |
| Test works, real events do not | `notifications.only_if_person: true` and AI is reporting no person                             |
| Arrives but silently           | Phone-side. Allow the topic/app to override Do Not Disturb; check `alarm: true` and `priority` |
| Nothing at all, no errors      | Check `notifications.cooldown_seconds` is not suppressing repeats                              |

Notifications are retried with backoff for `max_retry_age_hours`. A pending one is visible in the event's `event.json`.

## AI problems

```bash
sudo -u securecam securecam-admin test-ai
```

| Symptom                         | Cause                                                                                  |
| ------------------------------- | -------------------------------------------------------------------------------------- |
| `API key ... is not set`        | Add it to `/etc/securecam/securecam.env` and restart                                   |
| `401`                           | Bad key, or the key does not have access to that model                                 |
| `404` on the model              | Wrong `model` name for that `base_url`                                                 |
| `the model did not return JSON` | The model is ignoring the instruction. Use a better one — `gpt-4o-mini` works reliably |
| Timeouts                        | Raise `ai.timeout_seconds`; a local model on modest hardware can take a while          |
| Everything says "no person"     | Lower `ai.min_confidence`                                                              |

AI failures never affect recording. The event keeps its video and its snapshot, and the AI task is retried until `max_retry_age_hours`.

### `Could not capture a snapshot: Server returned 401 Unauthorized`

The AI never sees an image, so nothing is analysed and notifications go out without a picture. The snapshot is pulled over RTSP from the local MediaMTX, and that read is authorized by the `securecam` service on `127.0.0.1:9095`. A 401 means the credentials were refused, not that the stream is down.

```bash
systemctl status securecam.service
journalctl -u securecam -n 50 | grep "Denied MediaMTX"
```

The log line names the exact reason. Two causes account for almost all of them:

- **The `securecam` service is not running.** MediaMTX then has nobody to ask, so it denies every read. Live view fails at the same time.
- **`rtspAuthMethods` is not `[basic]`.** Digest authentication never sends the password in a recoverable form, so the HTTP authorizer cannot validate it. Regenerate the file and restart the stream:

  ```bash
  sudo securecam-admin render-mediamtx
  sudo systemctl restart securecam-mediamtx.service
  ```

## Login problems

### Forgotten password

```bash
sudo -u securecam securecam-admin user passwd alice
```

### No admin account exists

```bash
sudo -u securecam securecam-admin user add admin --role admin
```

### `too many attempts`

The rate limiter tripped. Wait for `api.rate_limit.window_seconds` (5 minutes by default) or restart the service to clear it.

### Logged out constantly

`SECURECAM_SECRET_KEY` is changing between restarts, which means the env file is not being read.

```bash
sudo systemctl show securecam -p EnvironmentFile
sudo ls -l /etc/securecam/securecam.env
```

### The login page loads but the API 404s

`api.web_ui: true` with a broken install. Reinstall the app:

```bash
cd ~/SecureCam && sudo ./install.sh
```

## Remote access problems

```bash
tailscale status
tailscale ip -4
```

| Symptom                            | Fix                                                                   |
| ---------------------------------- | --------------------------------------------------------------------- |
| `Logged out`                       | `sudo tailscale up`                                                   |
| Reachable but the UI does not load | Use the Tailscale IP with the port: `http://100.x.y.z:8080`           |
| UI loads, live view does not       | Add the Tailscale IP to `mediamtx.webrtc_additional_hosts`            |
| Nothing reachable                  | Both devices must be signed into the same tailnet, and check the ACLs |

See [remote-access.md](remote-access.md).

## Performance problems

```bash
vcgencmd measure_temp
vcgencmd get_throttled
top -b -n1 | head -15
```

| Reading         | Meaning                                         |
| --------------- | ----------------------------------------------- |
| `throttled=0x0` | Healthy                                         |
| bit 0 set       | Undervoltage now — **replace the power supply** |
| bit 16 set      | Undervoltage happened since boot                |
| bit 1 / 17      | Thermal capping                                 |

### High CPU

Expected: 3-6 % idle, 8-15 % during an event. If it is much higher:

```bash
systemctl show securecam-mediamtx -p MainPID
top -b -n1 | grep -E "mediamtx|securecam"
```

`mediamtx` above 50 % means it is software-encoding. Check `camera.codec` is `auto` or `hardwareH264`, never `softwareH264`.

`python` high means an AI or notification retry loop. Check the log.

### High temperature

Above 75 °C consistently: add a heatsink or better venting, drop `camera.fps` to 10, drop to 1280x720, and move the Pi out of direct sun. The Pi 4 throttles at 80 °C.

### Out of memory

```bash
free -h
journalctl -k | grep -i "out of memory"
```

Both units have `MemoryMax` set, so a leak gets the offending service killed and restarted rather than taking the whole Pi down. If it recurs, capture `journalctl -u securecam --since today` and open an issue.

## Getting help

Collect this before opening an issue:

```bash
sudo ./scripts/diagnose.sh > /tmp/diag.txt 2>&1
sudo journalctl -u securecam --since "1 hour ago" --no-pager >> /tmp/diag.txt
sudo journalctl -u securecam-mediamtx --since "1 hour ago" --no-pager >> /tmp/diag.txt
sudo -u securecam securecam-admin check-config --show >> /tmp/diag.txt
```

**Read `/tmp/diag.txt` before posting it.** It contains your local IP addresses and hostnames. It does not contain passwords, tokens or API keys — those are redacted — but check anyway.
