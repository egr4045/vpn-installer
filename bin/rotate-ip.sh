#!/usr/bin/env bash
# rotate-ip.sh — give this exit node a fresh public IPv4 (when the current one gets blocked) and
# re-issue everything that mentions it: DNS A record, RU relay config, people's links/keys/subscriptions.
# Needs TW_API_TOKEN and TW_SERVER_ID in /etc/safechill/vpn.env. The old IP stays attached (management,
# ~200 rub/month) until you run drop-ip.sh <old-ip> — Timeweb reboots the VM when an IP is removed.
set -euo pipefail
ETC=/etc/safechill; set -a; . "$ETC/vpn.env"; set +a
: "${TW_API_TOKEN:?TW_API_TOKEN missing in $ETC/vpn.env}"; : "${TW_SERVER_ID:?TW_SERVER_ID missing in $ETC/vpn.env}"
API=https://api.timeweb.cloud/api/v1; H="Authorization: Bearer $TW_API_TOKEN"
OLD="$SERVER_IP"
NEW=$(curl -s -X POST -H "$H" -H "Content-Type: application/json" --data '{"type":"ipv4"}' "$API/servers/$TW_SERVER_ID/ips" \
      | jq -r '.server_ip.ip // empty')
[ -n "$NEW" ] || { echo "Timeweb refused to add an IPv4 (balance must cover a month of all resources + 200 rub)"; exit 1; }
echo "new ip: $NEW (old $OLD stays attached until drop-ip.sh $OLD)"
# bring it up on this box (extra IPs are not handed out by DHCP)
F=/etc/netplan/60-safechill-extra-ip.yaml
if [ -f "$F" ]; then sed -i "/^\s*addresses:/a\\        - $NEW/32" "$F"
else printf 'network:\n  version: 2\n  ethernets:\n    %s:\n      addresses:\n        - %s/32\n' "$WAN_IF" "$NEW" > "$F"; fi
chmod 600 "$F"; netplan apply 2>/dev/null || true; sleep 3
curl -4 -s -m 8 --interface "$NEW" https://www.cloudflare.com/cdn-cgi/trace | grep -q "^ip=$NEW" || echo "! $NEW does not answer yet, continuing anyway"
# DNS: A record of the REALITY domain -> new ip
ZONE="${DOMAIN#*.}"; SUB="${DOMAIN%%.*}"
for rid in $(curl -s -H "$H" "$API/domains/$DOMAIN/dns-records" | jq -r '.dns_records[] | select(.type=="A") | .id'); do
  curl -s -o /dev/null -X DELETE -H "$H" "$API/domains/$DOMAIN/dns-records/$rid"; done
curl -s -o /dev/null -X POST -H "$H" -H "Content-Type: application/json" --data "{\"type\":\"A\",\"subdomain\":\"$SUB\",\"value\":\"$NEW\"}" "$API/domains/$ZONE/dns-records"
# switch config and re-issue
sed -i "s/^SERVER_IP=.*/SERVER_IP=$NEW/" "$ETC/vpn.env"
render.sh
systemctl restart xray
for n in $(jq -r '.[].name' "$ETC/users.json"); do add-client.sh "$n" >/dev/null 2>&1 || echo "! add-client $n failed"; done
echo "done: $OLD -> $NEW. Happ subscriptions update themselves; AmneziaVPN users need a new key: /qr Имя amnezia"
