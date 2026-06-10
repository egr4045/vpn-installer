<h1 align="center">MyVPN</h1>

<p align="center">
  Self-hosted, censorship-resistant VPN for a single VPS — one command to install,
  one web panel to run it. xray (Reality/VLESS/XHTTP) + sing-box (Hysteria2/TUIC)
  behind an SNI-routing nginx, with users, subscriptions, health dashboards,
  Telegram alerts and self-healing built in.
</p>

<p align="center">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-blue">
  <img alt="status" src="https://img.shields.io/badge/status-beta-orange">
  <img alt="platform" src="https://img.shields.io/badge/platform-Debian%2FUbuntu%20x86__64-lightgrey">
</p>

---

## Why

Most self-hosted VPN stacks are either a pile of manual configs or a heavy
multi-container control plane. **MyVPN** is one small FastAPI admin that *is* the
control plane: it renders every core config, hot-adds users with no restart,
generates subscriptions, watches its own health, and heals a wedged core — on a
1.8 GB box. No database server, no telemetry, no secrets in the repo.

## Features

- **Protocols that survive DPI:** VLESS-Reality (self-steal + real-site steal),
  XHTTP & httpupgrade over a CDN, Hysteria2 (×2 engines) and TUIC. Clients pick
  the fastest working transport via `urltest`.
- **One-command install** — interactive wizard generates all keys/secrets,
  obtains a Let's Encrypt cert (Cloudflare DNS-01 or HTTP-01), renders configs,
  brings the stack up.
- **Web admin** — users with traffic/expiry limits & live per-user accounting,
  subscription pages (Hiddify/Clash/sing-box/raw), and a settings area.
- **Super health dashboard** — protocols, certs, CPU/RAM/disk/network charts,
  per-month **SLA** (provider/internet outages excluded), an outbound-uplink
  watchdog, and a one-click **disk cleanup**.
- **Self-healing & alerts** — restarts a dead core automatically; Telegram alerts
  on protocol down, uplink loss, traffic thresholds and cert expiry.
- **Tuned out of the box** — BBR+fq, big socket buffers, RPS/RFS, journald cap.

## Quick start

```bash
# on a fresh Debian/Ubuntu x86_64 VPS, as root
git clone https://github.com/<you>/myvpn && cd myvpn
./install.sh
```

The wizard asks for your domain(s), email, optional Cloudflare token and Telegram
bot token — everything else (Reality keypair, admin password, session/health
tokens) is generated for you and written to a **gitignored `.env`**. When it
finishes it prints the admin URL, login and a first-user subscription QR.

> **DNS:** point your `direct.` domain straight at the server (DNS-only) and your
> `cdn.` domain through Cloudflare (orange-cloud) if you want the CDN transport.

## Architecture

```
            TCP 443 ─ xray Reality ─┬─ owned SNIs ─► nginx (unix sock) ─► XHTTP / httpupgrade
client ──►  TCP 8443 ─ xray Reality (real-site steal)                    └─► admin panel (SNI)
            UDP 443/9443 ─ sing-box Hysteria2 / TUIC
            UDP 8444 ─ xray Hysteria2
                         vpn-admin (FastAPI) ── docker.sock ──► renders configs, restarts cores
```

| Service     | Role                                                        |
|-------------|-------------------------------------------------------------|
| `xray`      | Reality (443/8443), VLESS-TCP, XHTTP `/store`, httpupgrade   |
| `sing-box`  | Hysteria2 (UDP 443), TUIC (UDP 9443)                        |
| `nginx`     | SNI routing + TLS over a `/dev/shm` unix socket             |
| `vpn-admin` | single control plane: users, subs, health, settings, alerts |

## Configuration & secrets

Precedence: **built-in defaults → `.env` → admin Settings (`/data/settings.json`)**.

- The repo ships **no secrets**. `config.py` defaults are generic placeholders;
  real values live in `.env` (gitignored). See [`.env.example`](.env.example).
- A pre-commit hook (`scripts/pre-commit-secretscan.sh`) and `.gitignore` keep
  `.env`, `/data`, certs and the rendered `docker-compose.yml`/`nginx.conf` out
  of git. See [SECURITY.md](SECURITY.md).

## Operating it

```bash
docker compose ps                 # status
docker compose logs -f vpn-admin  # admin logs
docker compose restart sing-box   # restart a core (configs are re-read)
```

Health, SLA and disk cleanup live on the **/health** page of the admin.

## Status

Beta. The live reference deployment is fully functional; the one-command
installer is best-effort and should be smoke-tested on a throwaway VM before you
rely on it. Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE).
