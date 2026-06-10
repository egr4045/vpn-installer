#!/usr/bin/env bash
# MyVPN — one-command installer / wizard.
#   curl -fsSL https://raw.githubusercontent.com/<you>/myvpn/main/install.sh | bash
# Idempotent-ish: re-running re-renders configs from .env and brings the stack up.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"
say()  { printf '\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
ask()  { local p="$1" d="${2:-}" v; read -rp "$p${d:+ [$d]}: " v || true; echo "${v:-$d}"; }
gen()  { openssl rand "$@"; }

# ── 00 preflight ──────────────────────────────────────────────────────────────
[ "$(id -u)" = 0 ] || die "run as root"
. /etc/os-release 2>/dev/null || true
case "${ID:-}${ID_LIKE:-}" in *debian*|*ubuntu*) ;; *) echo "warning: tested on Debian/Ubuntu only";; esac
[ "$(uname -m)" = x86_64 ] || echo "warning: only x86_64 images are pinned"

# ── 10 packages ───────────────────────────────────────────────────────────────
say "Installing packages"
if ! command -v docker >/dev/null; then curl -fsSL https://get.docker.com | sh; fi
docker compose version >/dev/null 2>&1 || die "docker compose plugin missing"
apt-get update -qq && apt-get install -y -qq openssl qrencode ufw jq curl >/dev/null
ok "packages ready"

# ── 20 host tuning (sysctl + journald cap + RPS/ring) ─────────────────────────
say "Applying network/OS tuning"
install -m644 templates/sysctl-99-vpn-tuning.conf /etc/sysctl.d/99-vpn-tuning.conf 2>/dev/null || true
sysctl --system >/dev/null 2>&1 || true
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=100M\nSystemKeepFree=500M\nMaxRetentionSec=2week\n' > /etc/systemd/journald.conf.d/99-vpn.conf
systemctl restart systemd-journald 2>/dev/null || true
install -m755 templates/vpn-net-tuning.sh /usr/local/sbin/vpn-net-tuning.sh 2>/dev/null || true
install -m644 templates/vpn-net-tuning.service /etc/systemd/system/vpn-net-tuning.service 2>/dev/null || true
systemctl enable --now vpn-net-tuning.service 2>/dev/null || true
ok "tuning applied"

# ── 30 firewall ───────────────────────────────────────────────────────────────
# NOTE: 8080 (admin panel) is deliberately NOT opened to the internet. The panel
# is reached only via nginx-TLS on the admin domain (:443); nginx talks to the
# admin over loopback (127.0.0.1:8080), which ufw does not filter. For direct
# access before DNS/TLS is ready, use an SSH tunnel:  ssh -L 8080:127.0.0.1:8080
say "Configuring ufw"
ufw allow 22/tcp >/dev/null; ufw allow 80/tcp >/dev/null
for p in 443/tcp 443/udp 8443/tcp 8444/udp 9443/udp 2080/tcp; do ufw allow "$p" >/dev/null; done
ufw delete allow 8080/tcp >/dev/null 2>&1 || true   # close it if a previous run opened it
ufw --force enable >/dev/null
ok "firewall up (admin :8080 stays loopback-only)"

# ── 40 wizard → .env ──────────────────────────────────────────────────────────
if [ -f .env ]; then
  say ".env exists — reusing it (delete it to reconfigure)"
else
  say "Configuration wizard"
  IP="$(curl -fsS4 https://api.ipify.org || true)"
  BRAND="$(ask 'Brand name' MyVPN)"
  SERVER_IP="$(ask 'Public IPv4' "$IP")"
  ADMIN_DOMAIN="$(ask 'Admin/panel domain (e.g. admin.example.com)')"
  DIRECT_DOMAIN="$(ask 'Direct domain (Reality/Hy2/TUIC, DNS-only)')"
  CDN_DOMAIN="$(ask 'CDN domain (Cloudflare-proxied, optional)' "$DIRECT_DOMAIN")"
  CERT_DOMAIN="$(ask 'Apex domain for the LE certificate (e.g. example.com)')"
  LE_EMAIL="$(ask 'Email for Let'\''s Encrypt')"
  CF_TOKEN="$(ask 'Cloudflare API token (blank = HTTP-01 instead of DNS-01)')"
  TG_BOT_TOKEN="$(ask 'Telegram bot token (optional)')"
  ADMIN_USER="$(ask 'Admin login' admin)"

  say "Generating secrets (reality keypair, passwords)"
  KP="$(docker run --rm ghcr.io/xtls/xray-core:25.1.30 x25519 2>/dev/null || true)"
  R_PRIV="$(echo "$KP" | awk -F': *' '/Private/{print $2}')"
  R_PUB="$(echo "$KP" | awk -F': *' '/Public/{print $2}')"
  ADMIN_PASS="$(gen -base64 18)"; SECRET_KEY="$(gen -hex 32)"; HEALTH_TOKEN="$(gen -hex 16)"
  R_SHORT="$(gen -hex 8)"

  umask 077
  cat > .env <<EOF
BRAND=$BRAND
SERVER_IP=$SERVER_IP
SUB_HTTPS_BASE=https://$ADMIN_DOMAIN/sub
REALITY_PUBLIC_KEY=$R_PUB
REALITY_PRIVATE_KEY=$R_PRIV
REALITY_SHORT_ID=$R_SHORT
REALITY_SNI=www.microsoft.com
REALITY_PORT=8443
CDN_DOMAIN=$CDN_DOMAIN
DIRECT_DOMAIN=$DIRECT_DOMAIN
HY2_SNI=$DIRECT_DOMAIN
TUIC_SNI=$DIRECT_DOMAIN
HU_PATH=/probe
HY2_PORT=443
HY2_PORT_X=8444
TUIC_PORT=9443
TCP_PORT=2080
ADMIN_USER=$ADMIN_USER
ADMIN_PASS=$ADMIN_PASS
SECRET_KEY=$SECRET_KEY
HEALTH_TOKEN=$HEALTH_TOKEN
TG_BOT_TOKEN=$TG_BOT_TOKEN
NGINX_CONTAINER=nginx
CERT_DIR=/etc/letsencrypt/live/$CERT_DOMAIN
ADMIN_DOMAIN=$ADMIN_DOMAIN
CERT_DOMAIN=$CERT_DOMAIN
LE_EMAIL=$LE_EMAIL
CF_TOKEN=$CF_TOKEN
EOF
  ok ".env written (gitignored)"
fi
set -a; . ./.env; set +a

# ── 50 certificates ───────────────────────────────────────────────────────────
say "Obtaining certificate for $CERT_DOMAIN"
if [ ! -f "/etc/letsencrypt/live/$CERT_DOMAIN/fullchain.pem" ]; then
  if [ -n "${CF_TOKEN:-}" ]; then
    mkdir -p /root/.secrets; printf 'dns_cloudflare_api_token = %s\n' "$CF_TOKEN" > /root/.secrets/cf.ini; chmod 600 /root/.secrets/cf.ini
    docker run --rm -v /etc/letsencrypt:/etc/letsencrypt -v /root/.secrets:/secrets \
      certbot/dns-cloudflare certonly --dns-cloudflare --dns-cloudflare-credentials /secrets/cf.ini \
      -d "$CERT_DOMAIN" -d "*.$CERT_DOMAIN" --email "$LE_EMAIL" --agree-tos -n || die "certbot (DNS-01) failed"
  else
    docker run --rm -p 80:80 -v /etc/letsencrypt:/etc/letsencrypt certbot/certbot certonly --standalone \
      -d "$CERT_DOMAIN" -d "$ADMIN_DOMAIN" -d "$DIRECT_DOMAIN" --email "$LE_EMAIL" --agree-tos -n || die "certbot (HTTP-01) failed"
  fi
fi
ok "certificate present"

# ── 60 render configs from templates ──────────────────────────────────────────
say "Rendering compose + nginx"
render() { sed -e "s|__XRAY_VER__|26.3.27|g" -e "s|__SINGBOX_VER__|v1.13.13|g" \
              -e "s|__ADMIN_DOMAIN__|$ADMIN_DOMAIN|g" -e "s|__DIRECT_DOMAIN__|$DIRECT_DOMAIN|g" \
              -e "s|__CDN_DOMAIN__|$CDN_DOMAIN|g" -e "s|__APEX_DOMAIN__|$CERT_DOMAIN|g" \
              -e "s|__CERT_DOMAIN__|$CERT_DOMAIN|g" "$1"; }
render templates/docker-compose.yml.tpl > docker-compose.yml
render templates/nginx.conf.tpl          > nginx.conf
mkdir -p data /etc/xray /etc/sing-box
ok "compose + nginx rendered"

# ── 70 build admin + render core configs (reuse app.render) + bring up ────────
say "Building admin and rendering xray/sing-box configs"
docker compose build vpn-admin
docker compose run --rm -T vpn-admin python -c \
  "from app import render; render.write_xray_config([]); render.write_singbox_config([])" || die "core config render failed"
docker compose up -d
ok "stack is up"

# ── 80 first user + 90 verify ────────────────────────────────────────────────
say "Done. Admin: https://$ADMIN_DOMAIN"
echo "  login: $ADMIN_USER   password: $ADMIN_PASS"
echo "  (port 8080 is firewalled; for direct access use: ssh -L 8080:127.0.0.1:8080 root@$SERVER_IP )"
echo "  create your first user in the admin → Users, then scan the subscription QR."
say "Verifying listeners"
sleep 3; ss -tulnp 2>/dev/null | grep -E ':(443|8443|9443|8444|2080|8080)\b' | awk '{print "  "$1" "$5}' || true
ok "install complete"
