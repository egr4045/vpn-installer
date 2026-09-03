#!/usr/bin/env bash
# drop-ip.sh <ip> — release an extra IPv4 of this server at Timeweb (stops the ~200 rub/month for it).
# Refuses to drop the IP currently in use by clients (SERVER_IP). Timeweb usually REBOOTS the VM
# when an IP is removed: expect ~5 minutes of downtime, run it at night.
set -euo pipefail
ETC=/etc/safechill; set -a; . "$ETC/vpn.env"; set +a
IP="${1:?usage: drop-ip.sh <ip>}"
[ "$IP" != "$SERVER_IP" ] || { echo "refusing: $IP is the IP people connect to"; exit 1; }
: "${TW_API_TOKEN:?TW_API_TOKEN missing}"; : "${TW_SERVER_ID:?TW_SERVER_ID missing}"
API=https://api.timeweb.cloud/api/v1; H="Authorization: Bearer $TW_API_TOKEN"
sed -i "\#- $IP/32#d" /etc/netplan/60-safechill-extra-ip.yaml 2>/dev/null || true
code=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE -H "$H" -H "Content-Type: application/json" --data "{\"ip\":\"$IP\"}" "$API/servers/$TW_SERVER_ID/ips")
echo "Timeweb: delete $IP -> HTTP $code (the VM may reboot now)"
for fid in $(curl -s -H "$H" "$API/floating-ips" | jq -r '.ips[] | select(.resource_id==null) | .id'); do
  curl -s -o /dev/null -X DELETE -H "$H" "$API/floating-ips/$fid" && echo "released floating ip $fid"; done
