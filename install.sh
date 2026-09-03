#!/usr/bin/env bash
# SafeChill VPN — installer for the EXIT node (Ubuntu 24.04, run as root). Idempotent:
# re-running re-renders everything from /etc/safechill/{vpn.env,secrets.env,users.json,peers/}.
#
#   443/tcp   VLESS + XHTTP + REALITY  (SNI = your domain, steals the local nginx site)   <- primary
#   8443/tcp  VLESS + TCP + REALITY + xtls-rprx-vision (SNI = FALLBACK_SNI)               <- fallback
#   UDP       AmneziaWG 3.1 (kernel module from ppa:amnezia/ppa)                          <- fast lane
#   optional  RU entry node (setup-ru.sh) that relays into this node over XHTTP+REALITY
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ETC=/etc/safechill
SHARE=/usr/local/share/safechill
say(){ printf '\033[1;36m> %s\033[0m\n' "$*"; }; ok(){ printf '\033[1;32m+ %s\033[0m\n' "$*"; }
die(){ printf '\033[1;31mx %s\033[0m\n' "$*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || die "run as root"

# -- 0. config -----------------------------------------------------------------
mkdir -p "$ETC/peers" /var/lib/safechill /root/clients /var/www/html "$SHARE"
if [ ! -f "$ETC/vpn.env" ]; then
  if [ -f "$REPO/vpn.env" ]; then install -m600 "$REPO/vpn.env" "$ETC/vpn.env"
  else install -m600 "$REPO/vpn.env.example" "$ETC/vpn.env"; die "fill in $ETC/vpn.env (DOMAIN at least) and re-run"; fi
fi
set -a; . "$ETC/vpn.env"; set +a
: "${DOMAIN:?DOMAIN missing in $ETC/vpn.env}"
AWG_PORT=${AWG_PORT:-39217}; TCP_PORT=${TCP_PORT:-8443}; FALLBACK_SNI=${FALLBACK_SNI:-gateway.icloud.com}
AWG_NET4=${AWG_NET4:-10.8.0}; AWG_NET6=${AWG_NET6:-fd08:5afe:c411}; BLOCK_TORRENT=${BLOCK_TORRENT:-1}
WAN_IF="$(ip route show default | awk '{print $5}' | head -1)"
SERVER_IP="${SERVER_IP:-$(ip -4 addr show "$WAN_IF" | awk '/inet /{print $2}' | cut -d/ -f1 | head -1)}"
SERVER_IP6="${SERVER_IP6:-$(ip -6 addr show scope global dev "$WAN_IF" 2>/dev/null | awk '/inet6/{print $2}' | cut -d/ -f1 | grep -v '^fd' | head -1)}"
grep -q '^WAN_IF=' "$ETC/vpn.env" || echo "WAN_IF=$WAN_IF" >> "$ETC/vpn.env"
grep -q '^SERVER_IP=' "$ETC/vpn.env" || echo "SERVER_IP=$SERVER_IP" >> "$ETC/vpn.env"
grep -q '^SERVER_IP6=' "$ETC/vpn.env" || echo "SERVER_IP6=$SERVER_IP6" >> "$ETC/vpn.env"
export WAN_IF SERVER_IP SERVER_IP6 AWG_PORT TCP_PORT FALLBACK_SNI AWG_NET4 AWG_NET6 BLOCK_TORRENT
ok "config: domain=$DOMAIN ip=$SERVER_IP${SERVER_IP6:+ / $SERVER_IP6} wan=$WAN_IF awg=udp/$AWG_PORT"

# -- 1. packages ---------------------------------------------------------------
say "Packages"
export DEBIAN_FRONTEND=noninteractive
for i in $(seq 1 60); do fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || break; [ "$i" = 1 ] && echo "  waiting for apt/dpkg lock (unattended-upgrades?)"; sleep 5; done
apt-get update -qq
apt-get install -y -qq software-properties-common nginx certbot ufw qrencode jq curl iptables dkms zstd \
  gettext-base python3 openssl "linux-headers-$(uname -r)" >/dev/null   # zstd: without it dkms writes an EMPTY .ko.zst
grep -rqs "amnezia" /etc/apt/sources.list.d/ || add-apt-repository -y ppa:amnezia/ppa >/dev/null
apt-get install -y -qq amneziawg amneziawg-tools >/dev/null
modprobe amneziawg || die "amneziawg kernel module did not load (dkms build failed?)"
if ! command -v xray >/dev/null; then
  bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install >/tmp/xray-install.log 2>&1 \
    || die "xray install failed, see /tmp/xray-install.log"
fi
ok "xray $(xray version | head -1 | awk '{print $2}'), awg-tools $(awg --version 2>&1 | awk '{print $2}')"

# -- 2. secrets (generated once) -----------------------------------------------
if [ ! -f "$ETC/secrets.env" ]; then
  say "Generating secrets"
  KP="$(xray x25519)"
  R_PRIV="$(sed -n 1p <<<"$KP" | awk '{print $NF}')"; R_PUB="$(sed -n 2p <<<"$KP" | awk '{print $NF}')"
  rnd(){ shuf -i "$1-$2" -n1; }
  S1=$(rnd 15 120); S2=$(rnd 15 120); while [ $((S1+56)) -eq "$S2" ]; do S2=$(rnd 15 120); done
  B=$(rnd 100000000 1500000000)
  AWG_PRIV="$(awg genkey)"
  umask 077
  {
    echo "REALITY_PRIV=$R_PRIV"
    echo "REALITY_PUB=$R_PUB"
    echo "SHORT_ID=$(openssl rand -hex 8)"
    echo "XHTTP_PATH=$(openssl rand -hex 6)"
    echo "RELAY_UUID=$(xray uuid)"
    echo "AWG_PRIV=$AWG_PRIV"
    echo "AWG_PUB=$(awg pubkey <<<"$AWG_PRIV")"
    echo "AWG_HPK=$(awg genpsk)"
    echo "AWG_JC=4"
    echo "AWG_JMIN=40"
    echo "AWG_JMAX=70"
    echo "AWG_S1=$S1"
    echo "AWG_S2=$S2"
    echo "AWG_S3=$(rnd 12 30)"
    echo "AWG_S4=$(rnd 12 30)"
    echo "AWG_H1=$B-$((B+99999))"
    echo "AWG_H2=$((B+10000000))-$((B+10099999))"
    echo "AWG_H3=$((B+20000000))-$((B+20099999))"
    echo "AWG_H4=$((B+30000000))-$((B+30099999))"
    echo "AWG_I1='<b 0xc30000000108><r 8><b 0x000044d0><r 1232>'"
    echo "AWG_I2='<r 2><b 0x01000001000000000000><b 0x0377777706676f6f676c6503636f6d0000010001>'"
    echo "AWG_CPA=0-64"
    echo "AWG_RAT=100-140"
    echo "AWG_KT=8-12"
  } > "$ETC/secrets.env"
  umask 022
  ok "secrets written to $ETC/secrets.env"
fi
[ -f "$ETC/users.json" ] || echo '[]' > "$ETC/users.json"

# -- 3. kernel tuning ----------------------------------------------------------
say "Kernel tuning (BBR, buffers, forwarding)"
install -m644 "$REPO/templates/sysctl-99-vpn.conf" /etc/sysctl.d/99-vpn.conf
sysctl --system >/dev/null
ok "sysctl applied"

# -- 4. firewall ---------------------------------------------------------------
say "Firewall"
sed -i 's/^DEFAULT_FORWARD_POLICY=.*/DEFAULT_FORWARD_POLICY="ACCEPT"/' /etc/default/ufw
ufw default deny incoming >/dev/null; ufw default allow outgoing >/dev/null
for p in 22/tcp 80/tcp 443/tcp "$TCP_PORT/tcp" "$AWG_PORT/udp"; do ufw allow "$p" >/dev/null; done
ufw --force enable >/dev/null
ok "ufw: 22,80,443,$TCP_PORT tcp + $AWG_PORT udp"

# -- 5. nginx site + Let's Encrypt (the REALITY steal target) ------------------
say "nginx + certificate for $DOMAIN"
grep -qs SafeChill /var/www/html/index.html || install -m644 "$REPO/templates/index.html" /var/www/html/index.html
rm -f /etc/nginx/sites-enabled/default
envsubst '${DOMAIN}' < "$REPO/templates/nginx-http.conf.tpl" > /etc/nginx/sites-available/safechill
ln -sf /etc/nginx/sites-available/safechill /etc/nginx/sites-enabled/safechill
nginx -t >/dev/null && systemctl enable --now nginx >/dev/null && systemctl reload nginx
CERT_DIR="/etc/letsencrypt/live/$DOMAIN"
if [ ! -f "$CERT_DIR/fullchain.pem" ]; then
  if [ -n "${EMAIL:-}" ]; then MAILARG=(-m "$EMAIL"); else MAILARG=(--register-unsafely-without-email); fi
  if ! certbot certonly --webroot -w /var/www/html -d "$DOMAIN" -n --agree-tos "${MAILARG[@]}"; then
    echo "! certbot failed: $DOMAIN must resolve to $SERVER_IP (DNS-only, no proxy) with port 80 reachable."
    echo "! Using a SELF-SIGNED certificate for now — REALITY/AWG work, probes just see a self-signed site."
    echo "! Fix DNS and re-run ./install.sh to switch to Let's Encrypt."
    CERT_DIR=/etc/safechill/selfsigned; mkdir -p "$CERT_DIR"
    [ -f "$CERT_DIR/fullchain.pem" ] || openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
      -days 3650 -subj "/CN=$DOMAIN" -addext "subjectAltName=DNS:$DOMAIN" \
      -keyout "$CERT_DIR/privkey.pem" -out "$CERT_DIR/fullchain.pem" 2>/dev/null
  fi
fi
export CERT_DIR
sed -i '/^CERT_DIR=/d' "$ETC/vpn.env"; echo "CERT_DIR=$CERT_DIR" >> "$ETC/vpn.env"
mkdir -p /etc/letsencrypt/renewal-hooks/deploy
printf '#!/bin/sh\nsystemctl reload nginx\n' > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
envsubst '${DOMAIN} ${CERT_DIR}' < "$REPO/templates/nginx-site.conf.tpl" > /etc/nginx/sites-available/safechill
nginx -t >/dev/null && systemctl reload nginx
case "$CERT_DIR" in *selfsigned*) ok "nginx up on 127.0.0.1:8444 with a TEMPORARY self-signed cert";;
  *) ok "nginx serves https://$DOMAIN on 127.0.0.1:8444 (REALITY target), Let's Encrypt auto-renews";; esac

# -- 6. install scripts + templates, render, start -----------------------------
say "Rendering xray + AmneziaWG"
install -m755 "$REPO/bin/"*.sh "$REPO/bin/"*.py /usr/local/bin/
rm -rf "$SHARE/templates"; cp -r "$REPO/templates" "$SHARE/templates"
render.sh
systemctl enable xray >/dev/null 2>&1 || true; systemctl restart xray
systemctl enable "awg-quick@awg0" >/dev/null 2>&1 || true
if systemctl restart "awg-quick@awg0"; then ok "xray + awg0 up"
else echo "! awg0 failed to start — see: journalctl -u awg-quick@awg0 -n 30"; journalctl -u awg-quick@awg0 -n 15 --no-pager | tail -8; fi

# -- 7. health timer -----------------------------------------------------------
say "Health checks (every minute, Telegram alerts)"
install -m755 "$REPO/templates/vpn-health.sh" /usr/local/bin/vpn-health.sh
install -m644 "$REPO/templates/vpn-health.service" /etc/systemd/system/vpn-health.service
install -m644 "$REPO/templates/vpn-health.timer" /etc/systemd/system/vpn-health.timer
systemctl daemon-reload; systemctl enable --now vpn-health.timer >/dev/null
ok "vpn-health.timer enabled"

# keep trying Let's Encrypt in the background while DNS propagates (transient unit, idempotent)
if [[ "$CERT_DIR" == *selfsigned* ]] && ! systemctl is-active --quiet cert-retry.service; then
  install -m755 "$REPO/templates/cert-retry.sh" /usr/local/bin/cert-retry.sh
  systemd-run --unit=cert-retry --description="SafeChill: retry Let's Encrypt until DNS propagates" /usr/local/bin/cert-retry.sh >/dev/null 2>&1 || true
fi

# -- 8. Telegram admin bot (/users, /add, /qr, /status) — only on the control node ----
if [ -n "${TG_BOT_TOKEN:-}" ] && [ "${BOT_ENABLED:-1}" = 1 ]; then
  install -m755 "$REPO/bin/safechill-bot.py" /usr/local/bin/safechill-bot.py
  install -m644 "$REPO/templates/safechill-bot.service" /etc/systemd/system/safechill-bot.service
  systemctl daemon-reload; systemctl enable safechill-bot.service >/dev/null 2>&1 || true; systemctl restart safechill-bot.service
  [ -n "${TG_ADMIN_IDS:-}" ] || echo "! TG_ADMIN_IDS is empty in $ETC/vpn.env — nobody can use the bot's commands yet"
  ok "telegram bot running (admins: ${TG_ADMIN_IDS:-none})"
fi

say "Done."
echo "  add a person:    add-client.sh <name>      (prints links, writes /root/clients/<name>/)"
echo "  RU entry node:   setup-ru.sh <ru-ip> [sni]"
echo "  status:          vpn-status.sh"
sleep 1; ss -tulnp | grep -E ":(443|$TCP_PORT|$AWG_PORT|8444|80)\b" | awk '{print "  "$1" "$5}' || true
