# Remote access

How to reach the camera from outside your house without opening a single router port.

- [Why not port forwarding](#why-not-port-forwarding)
- [Tailscale](#tailscale-recommended)
- [What the script changes](#what-the-script-changes)
- [Using it](#using-it)
- [Notification links](#notification-links)
- [Tailscale troubleshooting](#tailscale-troubleshooting)
- [Alternatives](#alternatives)
- [If you insist on port forwarding](#if-you-insist-on-port-forwarding)

---

## Why not port forwarding

Forwarding port 8080 to the Pi puts your camera on the public internet, where it will be found by automated scanners within hours. From there, every weakness in every component — SecureCam, MediaMTX, the kernel — is directly reachable by anyone.

The internet is full of security cameras that were installed exactly this way and are now streaming to strangers.

A mesh VPN removes the entire class of problem: nothing is exposed, and only devices you have explicitly authorized can even attempt to connect.

## Tailscale (recommended)

[Tailscale](https://tailscale.com) is WireGuard with the key exchange handled for you. It gives every device a stable private IP that works from anywhere, punches through NAT without configuration, and encrypts everything end to end. The free tier covers up to 100 devices, which is far more than a home needs.

```bash
sudo ./scripts/setup-remote-access.sh
```

The script:

1. Installs Tailscale from the official repository.
2. Runs `tailscale up` and prints the login URL — open it and sign in.
3. Reads the assigned IP with `tailscale ip -4`.
4. Adds that IP to `mediamtx.webrtc_additional_hosts` so live view works over the tunnel.
5. Sets `api.public_base_url` so notification links are clickable from your phone.
6. Restarts both services.
7. Prints exactly what to do next.

Then install the Tailscale app on your phone and laptop and sign into the **same account**.

## What the script changes

```yaml
mediamtx:
  webrtc_additional_hosts: ["100.x.y.z"]

api:
  public_base_url: "http://100.x.y.z:8080"
```

The first is the important one. WebRTC works by the server telling the browser which addresses to connect to. Without the Tailscale IP in that list, MediaMTX only advertises its LAN address — which your phone on mobile data cannot reach — and live view hangs while the API works fine.

You can make the URL nicer by enabling MagicDNS in the Tailscale admin console, then using the hostname:

```yaml
api:
  public_base_url: "http://frontdoor.tailnet-name.ts.net:8080"
```

## Using it

```bash
tailscale ip -4       # on the Pi, e.g. 100.101.102.103
```

From any device signed into your tailnet:

```
http://100.101.102.103:8080
```

Live view, events, status and user management all work exactly as they do on the LAN — the traffic is just inside a WireGuard tunnel.

You do not need TLS on top of this. Tailscale already encrypts and authenticates every packet.

### Locking it down further

In the Tailscale admin console you can:

- **Disable key expiry** for the Pi, so it does not need re-authentication every 180 days.
- Write an **ACL** so only your phone and laptop can reach the Pi, not every device on the tailnet:

```json
{
  "acls": [
    {
      "action": "accept",
      "src": ["user@example.com"],
      "dst": ["tag:camera:8080,8889,8189"]
    }
  ]
}
```

- Enable **MagicDNS** so `frontdoor` resolves without remembering the IP.

Do **not** use Tailscale Funnel with SecureCam. Funnel publishes a service to the public internet, which defeats the entire point.

## Notification links

With `api.public_base_url` set, notifications contain a link that opens the event directly. Tapping it on your phone works anywhere as long as Tailscale is connected on that phone.

If the link opens but the page does not load, Tailscale is disconnected on the phone. The notification itself still arrived, because notifications go out over the normal internet and do not depend on the tunnel.

## Tailscale troubleshooting

```bash
tailscale status
tailscale ip -4
tailscale netcheck
```

| Symptom                            | Fix                                                                                                                                                                |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `Logged out`                       | `sudo tailscale up` and complete the browser login                                                                                                                 |
| Pi not listed on other devices     | Devices are in different tailnets — check you signed in with the same account                                                                                      |
| Reachable but the UI does not load | Include the port: `http://100.x.y.z:8080`                                                                                                                          |
| UI works, live view spins          | Tailscale IP missing from `webrtc_additional_hosts`; re-run the setup script                                                                                       |
| Worked, then stopped after months  | Key expiry. Disable it for this machine in the admin console                                                                                                       |
| Very slow video                    | `tailscale netcheck` says it is relaying via DERP rather than connecting directly. Usually a restrictive CGNAT. Enable UPnP on the router, or accept lower quality |

After changing the IP or the config manually:

```bash
sudo -u securecam securecam-admin check-config
sudo systemctl restart securecam securecam-mediamtx
```

## Alternatives

### Plain WireGuard

More work, no third-party coordination server, no account. Good if you want zero external dependencies and have a static IP or dynamic DNS. You will need to open one UDP port on the router for the WireGuard endpoint — which is a much smaller attack surface than an HTTP service, since WireGuard does not respond at all to unauthenticated packets.

### ZeroTier / Nebula / Netbird

Same shape as Tailscale. If you already run one, use it: install it, find the Pi's address on that network, and put it in `webrtc_additional_hosts` and `public_base_url`.

### SSH tunnel

Zero extra software, good for occasional access:

```bash
ssh -L 8080:localhost:8080 -L 8889:localhost:8889 pi@frontdoor.local
```

Then browse to `http://localhost:8080`. **Live view will not work** — WebRTC needs UDP, which a TCP tunnel cannot carry. Events and everything else work fine.

### Reverse proxy with a real certificate

If you have a domain and want browser-trusted HTTPS, run Caddy or nginx in front of SecureCam with a Let's Encrypt certificate. This still means exposing a port to the internet, so at minimum:

- put the proxy on a different machine, not the Pi;
- enforce HTTPS only;
- add IP allow-listing or an auth layer in the proxy as well;
- keep the WebRTC ports **off** the public internet, which means live view will not work remotely.

Tailscale gives you more security for less work. This option exists because some people need a URL that works without installing anything on the client.

## If you insist on port forwarding

Don't. But if you do, the absolute minimum:

1. **Enable TLS** — see [security.md](security.md#tls). Never expose plain HTTP.
2. Forward **only** 8080. Not 8889, not 8189, not SSH, not the MediaMTX APIs.
3. Use a very long, unique admin password.
4. Restrict source IPs on the router if it supports it.
5. Enable `unattended-upgrades` and update SecureCam promptly.
6. Watch the log for denied attempts: `sudo journalctl -u securecam -f | grep -i denied`.
7. Accept that live view will not work remotely without also exposing WebRTC, and that exposing WebRTC widens the surface further.

Every one of these steps is unnecessary with Tailscale, which takes two minutes to set up.
