#!/usr/bin/env bash
# vpn-status.sh — one screen: services, listeners, cert, AWG peers, users, RU node, last health lines.
set -a; . /etc/safechill/vpn.env 2>/dev/null; set +a
TCP_PORT=${TCP_PORT:-8443}; AWG_PORT=${AWG_PORT:-39217}
echo "--- services"
for u in xray nginx awg-quick@awg0 vpn-health.timer; do printf '  %-18s %s\n' "$u" "$(systemctl is-active "$u")"; done
echo "--- listeners"; ss -tulnp | grep -E ":(443|$TCP_PORT|$AWG_PORT|8444|80)\b" | awk '{print "  "$1" "$5" "$7}'
CERT_DIR=${CERT_DIR:-/etc/letsencrypt/live/$DOMAIN}
echo "--- cert ($CERT_DIR)"; openssl x509 -enddate -issuer -noout -in "$CERT_DIR/fullchain.pem" 2>/dev/null | sed 's/^/  /'
echo "--- awg peers"; awg show awg0 2>/dev/null | grep -E '^peer|latest handshake|transfer' | sed 's/^/  /'
echo "--- users"; jq -r '.[]|"  \(.name)  \(.uuid)"' /etc/safechill/users.json
if [ -n "${RU_HOST:-}" ]; then
  code=$(curl -sk -o /dev/null -w '%{http_code}' -m 6 --resolve "${RU_SNI:-yandex.ru}:443:$RU_HOST" "https://${RU_SNI:-yandex.ru}/" || true)
  echo "--- RU entry $RU_HOST: reality steal answers HTTP $code (000 = down)"
fi
echo "--- health log"; tail -3 /var/log/vpn-health.log 2>/dev/null | sed 's/^/  /'
