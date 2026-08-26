# Installation

From a blank microSD card to a running camera.

- [1. Prepare the SD card](#1-prepare-the-sd-card)
- [2. First boot](#2-first-boot)
- [3. Check the hardware](#3-check-the-hardware)
- [4. Install SecureCam](#4-install-securecam)
- [5. What the installer did](#5-what-the-installer-did)
- [6. Verify](#6-verify)
- [7. Optional next steps](#7-optional-next-steps)
- [Unattended installation](#unattended-installation)
- [Offline installation](#offline-installation)
- [Reinstalling and updating](#reinstalling-and-updating)
- [If installation fails](#if-installation-fails)

---

## 1. Prepare the SD card

Use **Raspberry Pi Imager** on your desktop machine.

1. Choose device: **Raspberry Pi 4**.
2. Choose OS: **Raspberry Pi OS Lite (64-bit)**. Not the desktop version — you do not need a GUI and it costs you 300 MB of RAM.
3. Choose storage: your card.
4. Click the gear / **Edit settings** before writing:
   - Set a hostname, e.g. `frontdoor`.
   - Set a username and a strong password. **Do not use `pi`/`raspberry`.**
   - Configure your Wi-Fi SSID, password and country.
   - Enable **SSH**, with password or (better) a public key.
5. Write, then eject.

> 64-bit is required. The MediaMTX build SecureCam downloads is `linux_arm64`. If you install the 32-bit OS the installer will stop and tell you.

## 2. First boot

Insert the card, connect the camera and the PIR (see [hardware.md](hardware.md)), then power on. Give it a minute, then find it:

```bash
ping frontdoor.local
ssh youruser@frontdoor.local
```

Update everything before you start:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

## 3. Check the hardware

Save yourself an hour of debugging later by verifying both sensors first.

```bash
rpicam-hello --list-cameras          # should list exactly one camera
rpicam-jpeg -o /tmp/test.jpg -t 2000 # should produce a non-empty file
ls -l /tmp/test.jpg
```

```bash
sudo apt install -y git
git clone https://github.com/YOUR-USERNAME/SecureCam.git
cd SecureCam
sudo ./scripts/pir-test.py --gpio 17
```

Wave at the PIR. If nothing happens, fix that now — see [hardware.md](hardware.md#testing-before-you-install).

## 4. Install SecureCam

```bash
cd ~/SecureCam
sudo ./install.sh
```

It will ask you three things:

1. **Which BCM GPIO pin is the PIR on?** Default 17.
2. **A username for the first admin account.**
3. **A password for it**, twice. Minimum 12 characters, entered without echo.

Everything else is automatic. It takes a couple of minutes, mostly `apt`.

## 5. What the installer did

| Step          | Result                                                                                                                                                                                                                      |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Preflight     | Verified `aarch64`, a Raspberry Pi, enough RAM and at least a few GB free. Refused to continue otherwise.                                                                                                                   |
| Packages      | Installed `python3`, `python3-yaml`, `python3-gpiozero`, `python3-lgpio`, `ffmpeg`, `curl`, `ca-certificates`.                                                                                                              |
| User          | Created system user `securecam` (no login shell, no home directory) in groups `video`, `render`, `gpio`.                                                                                                                    |
| Application   | Copied the repo to `/opt/securecam/app`, created a venv at `/opt/securecam/venv` with `--system-site-packages`, installed the package, symlinked `securecam` and `securecam-admin` into `/usr/local/bin`.                   |
| MediaMTX      | Downloaded `mediamtx_v1.20.1_linux_arm64.tar.gz`, **verified its SHA-256**, unpacked the binary to `/usr/local/bin/mediamtx`.                                                                                               |
| Configuration | Copied `config.example.yaml` to `/etc/securecam/config.yaml` (only if it did not already exist) and wrote your GPIO choice into it.                                                                                         |
| Secrets       | Generated `/etc/securecam/securecam.env`, mode 0600, owner root, containing a random `SECURECAM_SECRET_KEY` and MediaMTX service credentials.                                                                               |
| System files  | Installed both systemd units, a narrowly-scoped `/etc/sudoers.d/securecam` validated with `visudo -c`, `/etc/sysctl.d/99-securecam.conf` (larger UDP receive buffers for WebRTC), and a tmpfiles rule for `/run/securecam`. |
| Admin         | Created your first account in `/var/lib/securecam/users.json`.                                                                                                                                                              |
| Services      | Enabled and started `securecam-mediamtx.service` and `securecam.service`.                                                                                                                                                   |

The final block of output tells you the URL, the account you created, and anything still needing attention.

## 6. Verify

```bash
systemctl status securecam securecam-mediamtx
sudo ./scripts/diagnose.sh
```

Then open `http://<pi-ip>:8080` in a browser and log in.

```bash
hostname -I
```

- **Live** tab should show video within a couple of seconds.
- Walk in front of the PIR, wait for `motion.post_motion_seconds` (5 minutes by default), then check the **Events** tab. The clip should start a minute before you appeared.

Impatient? Shorten the wait temporarily:

```bash
sudo nano /etc/securecam/config.yaml    # motion.post_motion_seconds: 20
sudo -u securecam securecam-admin check-config
sudo systemctl restart securecam
```

## 7. Optional next steps

| Want                          | Do                                                                                |
| ----------------------------- | --------------------------------------------------------------------------------- |
| Push notifications            | [configuration.md](configuration.md#notifications)                                |
| Person detection              | [configuration.md](configuration.md#ai)                                           |
| Access from outside the house | `sudo ./scripts/setup-remote-access.sh`, see [remote-access.md](remote-access.md) |
| HTTPS on the LAN              | [security.md](security.md#tls)                                                    |
| Extra user accounts           | `sudo -u securecam securecam-admin user add bob --role viewer`                    |
| Recordings on an SSD          | [hardware.md](hardware.md#storage)                                                |
| A daily health email          | Cron `scripts/health-check.sh --quiet` and mail on non-zero exit                  |

## Unattended installation

For imaging several Pis, or for Ansible:

```bash
sudo ./install.sh --unattended --gpio 17
```

`--unattended` never prompts. It skips admin account creation, so create one afterwards:

```bash
sudo -u securecam securecam-admin user add admin --role admin
```

Until an admin exists, the web UI has nothing to log in with. Recording works regardless.

## Offline installation

If the Pi has no internet:

1. On a machine that does, download the MediaMTX release for `linux_arm64` and copy it over.
2. Unpack `mediamtx` to `/usr/local/bin/mediamtx` and `chmod 755` it.
3. Pre-install the apt packages from a local mirror or a `.deb` bundle.
4. Run:

```bash
sudo ./install.sh --skip-apt --skip-mediamtx
```

The installer verifies the binary exists and is executable before continuing.

## Reinstalling and updating

`install.sh` is idempotent. Running it again:

- **replaces** the application code and the venv;
- **replaces** the systemd units, sudoers rule and sysctl file;
- **keeps** `/etc/securecam/config.yaml`, `/etc/securecam/securecam.env`, `/var/lib/securecam/users.json` and every recording.

```bash
cd ~/SecureCam && git pull && sudo ./install.sh
```

The generated `/etc/securecam/mediamtx.yml` is rewritten from the template every time the service starts, so you never need to touch it — and edits to it are lost. Change `config.yaml` instead.

## If installation fails

The installer stops at the first real problem and prints what failed, why, and what to do. Common ones:

| Message                      | Cause                                | Fix                                                                |
| ---------------------------- | ------------------------------------ | ------------------------------------------------------------------ |
| `this system is not aarch64` | 32-bit OS image                      | Reflash with Raspberry Pi OS Lite **64-bit**                       |
| `not a Raspberry Pi`         | Running on a VM or a different SBC   | Unsupported                                                        |
| `only N MB of disk free`     | Card nearly full                     | `sudo apt clean`, remove old files, or use a bigger card           |
| `checksum mismatch`          | Corrupted or tampered download       | Re-run; if it repeats, download manually and use `--skip-mediamtx` |
| `apt failed`                 | No network, or a stale package index | `sudo apt update` and read the error                               |
| `visudo -c rejected`         | Bug or a modified sudoers template   | Report it; nothing was installed                                   |

Nothing is left half-installed: each step either completes or aborts the run. If you want a clean slate:

```bash
sudo ./uninstall.sh --purge
```
