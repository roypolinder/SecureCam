# Security

What SecureCam protects, how, and what it does not protect against. Read this before putting the camera on a network you do not control.

- [Threat model](#threat-model)
- [Authentication](#authentication)
- [Authorization](#authorization)
- [Stream authentication](#stream-authentication)
- [Network exposure](#network-exposure)
- [TLS](#tls)
- [Secrets](#secrets)
- [Process hardening](#process-hardening)
- [File permissions](#file-permissions)
- [Privacy](#privacy)
- [Hardening checklist](#hardening-checklist)
- [Reporting a vulnerability](#reporting-a-vulnerability)

---

## Threat model

**Defended against**

- Anyone on your LAN reading footage or watching the live stream without an account.
- Password guessing over the network.
- Someone injecting a fake video stream into the camera path.
- A stolen session cookie being replayed after it expires.
- An account for one camera being used on another camera.
- Secrets leaking into logs, API responses or the git repository.
- A compromised SecureCam process escalating to root.

**Not defended against**

- **Physical access to the Pi.** Anyone who can pull the SD card has every recording and every secret on it. Use full-disk encryption if that matters, or accept it.
- **Root on the Pi.** Root reads everything.
- **A malicious LAN with HTTP enabled.** Without TLS or Tailscale, credentials and video cross the LAN unencrypted. See [TLS](#tls).
- **A compromised notification provider.** ntfy topics are readable by anyone who knows the name.
- **A compromised AI provider.** If you enable AI, the snapshot is sent to whatever endpoint you configured.
- **Denial of service.** The rate limiter slows password guessing; it does not stop someone flooding your network.

## Authentication

Passwords are hashed with **PBKDF2-HMAC-SHA256** with a per-user random salt and a high iteration count. The plaintext password:

- is never written to disk;
- is never written to a log;
- is never returned by the API;
- is compared in constant time.

The user database is `/var/lib/securecam/users.json`, mode 0600, owned by `securecam`.

Sessions are **HMAC-SHA256-signed tokens** containing the username, the role, the device id, the token kind and an expiry. They are:

- stored in a cookie with `HttpOnly` (no JavaScript access), `SameSite=Strict` (no cross-site sending) and `Secure` when TLS is on;
- verified in constant time;
- rejected if expired, tampered with, signed by a different key, or of the wrong kind.

The signing key is `SECURECAM_SECRET_KEY`, generated at install with 32 bytes of `os.urandom`. **Changing it invalidates every session**, which is the emergency "log everyone out" switch:

```bash
sudo sed -i "/^SECURECAM_SECRET_KEY=/d" /etc/securecam/securecam.env
printf 'SECURECAM_SECRET_KEY=%s\n' "$(openssl rand -hex 32)" | sudo tee -a /etc/securecam/securecam.env
sudo systemctl restart securecam
```

Failed logins are **rate limited per client address** (10 attempts per 5 minutes by default). Password minimum length is 12 characters, enforced by the CLI and the API alike.

State-changing requests require an `X-Requested-With` header, which a cross-site form post cannot set — a second layer behind `SameSite=Strict`.

## Authorization

Two roles:

| Permission               | viewer | admin |
| ------------------------ | :----: | :---: |
| View live stream         |  yes   |  yes  |
| View and download events |  yes   |  yes  |
| Delete events            |   no   |  yes  |
| View health              |  yes   |  yes  |
| View full diagnostics    |   no   |  yes  |
| View configuration       |   no   |  yes  |
| Manage users             |   no   |  yes  |

Every route declares the permission it needs. There is no route that skips the check except `GET /api/info` and `POST /api/login`.

**The last admin account cannot be deleted, demoted or disabled.** This is enforced in `UserStore`, so the API and the CLI both obey it, and you cannot lock yourself out.

Accounts are bound to `device.id`. An account created on one camera will not authenticate on another even if the file is copied, which limits the blast radius of a stolen `users.json`.

## Stream authentication

MediaMTX is configured with `authMethod: http`. **It cannot serve a single frame without asking SecureCam first.** Any non-2xx response denies the request.

The callback listener:

- binds to `127.0.0.1` only;
- is a **separate listener from the public API**, always plain HTTP, so that turning on TLS for the browser does not break MediaMTX's callback;
- fails closed — if SecureCam is down, nothing can be watched.

Decisions it makes:

| Request                               | Rule                                                                          |
| ------------------------------------- | ----------------------------------------------------------------------------- |
| `publish`                             | **Always denied.** The camera is the only source; nobody can inject a stream. |
| `api`, `metrics`, `pprof`             | Service credentials **and** a loopback source address.                        |
| `read`, `playback`                    | A valid stream ticket, or valid user credentials with the right role.         |
| Any other action                      | Denied.                                                                       |
| A path that is not the configured one | Denied.                                                                       |

Stream tickets are short-lived (120 seconds), single-purpose (`kind: stream`), bound to the device id and the path. A session cookie cannot be used as a stream ticket and vice versa — the kind is checked. Every denial is logged with the action, the user and the reason.

## Network exposure

Default listening ports:

| Port     | Bind        | Purpose                | Exposure                 |
| -------- | ----------- | ---------------------- | ------------------------ |
| 8080/tcp | `0.0.0.0`   | HTTP API + web UI      | **LAN**, authenticated   |
| 8889/tcp | `0.0.0.0`   | WebRTC signalling      | **LAN**, authenticated   |
| 8189/udp | all         | WebRTC media           | **LAN**, only after auth |
| 9095/tcp | `127.0.0.1` | MediaMTX auth callback | loopback only            |
| 9996/tcp | `127.0.0.1` | MediaMTX playback API  | loopback only            |
| 9997/tcp | `127.0.0.1` | MediaMTX control API   | loopback only            |
| 8554/tcp | `127.0.0.1` | RTSP                   | loopback only            |
| 8888/tcp | `127.0.0.1` | HLS                    | loopback only            |

**Do not port-forward any of these from the internet.** Use [Tailscale](remote-access.md) instead. Verify what is actually listening:

```bash
sudo ss -tlnp
sudo ss -ulnp
```

Restrict to the LAN only:

```bash
sudo ufw default deny incoming
sudo ufw allow from 192.168.1.0/24 to any port 8080 proto tcp
sudo ufw allow from 192.168.1.0/24 to any port 8889 proto tcp
sudo ufw allow from 192.168.1.0/24 to any port 8189 proto udp
sudo ufw allow ssh
sudo ufw enable
```

For an API-only deployment, bind to loopback and reach it through an SSH tunnel:

```yaml
api:
  address: "127.0.0.1"
```

```bash
ssh -L 8080:localhost:8080 pi@frontdoor.local
```

## TLS

**HTTP is the default**, deliberately: on a home LAN behind NAT, requiring certificate setup before the camera works would push people towards not using authentication at all.

You should enable TLS, or use Tailscale, if:

- the LAN is shared (a flat office network, student housing, a guest network);
- you access the camera over Wi-Fi you do not control;
- you expose it beyond the LAN in any way.

Self-signed:

```bash
sudo openssl req -x509 -newkey rsa:4096 -nodes \
  -keyout /etc/securecam/key.pem -out /etc/securecam/cert.pem \
  -days 3650 -subj "/CN=frontdoor.local"
sudo chown securecam:securecam /etc/securecam/key.pem /etc/securecam/cert.pem
sudo chmod 600 /etc/securecam/key.pem
```

```yaml
api:
  tls:
    enabled: true
    cert_file: /etc/securecam/cert.pem
    key_file: /etc/securecam/key.pem
```

Browsers will warn every time. A real certificate from Let's Encrypt requires a public DNS name, which brings its own exposure — Tailscale is simpler and gives you WireGuard encryption end to end with no certificates at all.

> Enabling TLS does **not** encrypt the WebRTC signalling port. WebRTC media itself is always DTLS-encrypted, but the signalling on 8889 is plain HTTP. Use Tailscale if that matters.

## Secrets

Secrets never appear in `config.yaml`. The config refers to them by environment variable name; the values live in `/etc/securecam/securecam.env`:

```
-rw------- 1 root root /etc/securecam/securecam.env
```

Loaded by systemd via `EnvironmentFile`, so the values are in the service's environment and nowhere else.

- Secret values are registered with the logging layer and **redacted from log output**.
- `GET /api/config` returns the configuration with every secret-bearing field removed.
- `.gitignore` excludes `*.env`, `config.yaml`, `users.json`, `device_id` and `*.pem`.

Check nothing sensitive is tracked before you push:

```bash
git ls-files | grep -Ei "\.env$|users\.json|\.pem$|config\.yaml$"   # should print nothing
```

## Process hardening

Both units run as the dedicated system user `securecam`, which has no login shell and no home directory.

```ini
NoNewPrivileges=yes        # mediamtx unit
ProtectSystem=full
ProtectHome=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
RestrictNamespaces=yes
LockPersonality=yes
MemoryMax=...
```

`securecam.service` deliberately does **not** set `NoNewPrivileges`, because it needs one sudo call. That call is scoped to exactly one command in `/etc/sudoers.d/securecam`:

```
securecam ALL=(root) NOPASSWD: /usr/bin/systemctl restart securecam-mediamtx.service
```

No wildcards. The file is validated with `visudo -c` during installation and the install aborts if it does not pass.

Group membership is the minimum needed: `video` and `render` for the camera, `gpio` for the PIR.

`MemoryMax` on both units means a memory leak gets one service killed and restarted rather than the whole Pi going down.

## File permissions

| Path                            | Mode | Owner               |
| ------------------------------- | ---- | ------------------- |
| `/etc/securecam/securecam.env`  | 0600 | root:root           |
| `/etc/securecam/config.yaml`    | 0640 | root:securecam      |
| `/etc/securecam/device_id`      | 0644 | securecam:securecam |
| `/var/lib/securecam/users.json` | 0600 | securecam:securecam |
| `/var/lib/securecam/events/`    | 0750 | securecam:securecam |
| `/opt/securecam/`               | 0755 | root:root           |

Verify:

```bash
sudo ls -la /etc/securecam/ /var/lib/securecam/
```

> Run admin commands as the service user — `sudo -u securecam securecam-admin ...` — otherwise root-owned files appear in `/var/lib/securecam` and the service can no longer write there.

## Privacy

- **Video never leaves the Pi.** There is no upload path for recordings.
- **Snapshots leave only if you enable AI**, and only to the endpoint you configured. Point `base_url` at a machine on your own LAN if you want detection without a third party.
- **Notifications carry a title, a message and optionally a snapshot** to ntfy or Pushover.
- **No telemetry, no analytics, no phone-home.** The only outbound requests are network probes (TCP connects to `1.1.1.1:443` and `8.8.8.8:443`), AI, and notifications. Disable probing by emptying `network.internet_probe_hosts`.
- The web UI loads **no external resources** — no CDN fonts, no analytics, no remote scripts.

Recording people has legal consequences that vary by country: signage requirements, restrictions on capturing public space or a neighbour's property, and retention limits. **That is your responsibility, not the software's.** `storage.retention_days` exists partly so you can comply with a legally required maximum.

## Hardening checklist

Before you consider this deployed:

- [ ] Changed the default OS password, or better, use SSH keys and set `PasswordAuthentication no`.
- [ ] SecureCam admin password is long and unique.
- [ ] `sudo ls -l /etc/securecam/securecam.env` shows `-rw------- root root`.
- [ ] `git ls-files` shows no secret files.
- [ ] `sudo ss -tlnp` shows nothing unexpected, and 9996/9997/8554/8888 are on 127.0.0.1.
- [ ] No router ports forwarded to the Pi.
- [ ] Remote access is via Tailscale, not port forwarding.
- [ ] TLS enabled, or the LAN is genuinely trusted.
- [ ] `unattended-upgrades` installed so the OS gets security patches.
- [ ] ntfy topic is a long random string, or you self-host with a token.
- [ ] Extra accounts are `viewer`, not `admin`.
- [ ] Recordings are backed up somewhere a thief cannot reach.
- [ ] `sudo ./scripts/diagnose.sh` is clean.

```bash
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure --priority=low unattended-upgrades
```

## Reporting a vulnerability

Please do **not** open a public issue for a security problem. Email the maintainer listed in the repository, or use GitHub's private security advisory feature. Include the version, the configuration (with secrets removed) and reproduction steps.
