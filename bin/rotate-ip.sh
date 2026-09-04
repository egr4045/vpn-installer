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

tw() { # tw <method> <path> [json] -> body on stdout; fails loudly on a non-2xx, which curl -s swallows
  local m=$1 p=$2 d=${3:-} out code body
  if [ -n "$d" ]; then
    out=$(curl -s -m 20 -w $'\n%{http_code}' -X "$m" -H "$H" -H "Content-Type: application/json" --data "$d" "$API$p" || true)
  else
    out=$(curl -s -m 20 -w $'\n%{http_code}' -X "$m" -H "$H" "$API$p" || true)
  fi
  code=${out##*$'\n'}; body=${out%$'\n'*}
  case "$code" in
    2*) printf '%s' "$body" ;;
    *)  echo "! Timeweb $m $p -> HTTP ${code:-no answer}: $(printf '%s' "$body" | tr -d '\n' | cut -c1-300)" >&2; return 1 ;;
  esac
}

NEW=$(tw POST "/servers/$TW_SERVER_ID/ips" '{"type":"ipv4"}' | jq -r '.server_ip.ip // empty') || true
[ -n "$NEW" ] || { echo "Timeweb refused to add an IPv4 (balance must cover a month of all resources + 200 rub)"; exit 1; }
echo "new ip: $NEW (old $OLD stays attached until drop-ip.sh $OLD)"
# From here on a failure leaves a paid, half-wired extra IP behind, so say which one it is: nothing else
# in the script knows, and the address is billed (~200 rub/month) until it is released by hand.
trap 'rc=$?; echo "! rotate-ip failed after allocating $NEW — release it with: drop-ip.sh $NEW (reboots the VM)" >&2; exit $rc' ERR
# bring it up on this box (extra IPs are not handed out by DHCP)
F=/etc/netplan/60-safechill-extra-ip.yaml
if [ -f "$F" ]; then sed -i "/^\s*addresses:/a\\        - $NEW/32" "$F"
else printf 'network:\n  version: 2\n  ethernets:\n    %s:\n      addresses:\n        - %s/32\n' "$WAN_IF" "$NEW" > "$F"; fi
chmod 600 "$F"; netplan apply 2>/dev/null || true; sleep 3
curl -4 -s -m 8 --interface "$NEW" https://www.cloudflare.com/cdn-cgi/trace | grep -q "^ip=$NEW" || echo "! $NEW does not answer yet, continuing anyway"
# DNS: A record of the REALITY domain -> new ip. The new record is created and read back BEFORE the old
# ones are deleted: the other way round, a create that failed left the domain with no A record at all and
# said nothing (curl -s reports an HTTP error as success), which kills the ClashMi subscriptions — they are
# fetched over https://$DOMAIN — while xray itself keeps looking perfectly healthy.
ZONE="${DOMAIN#*.}"; SUB="${DOMAIN%%.*}"
tw POST "/domains/$ZONE/dns-records" "{\"type\":\"A\",\"subdomain\":\"$SUB\",\"value\":\"$NEW\"}" >/dev/null
RECS=$(tw GET "/domains/$DOMAIN/dns-records")
# the address lives in .data.value; the top-level .value the API also returns is always null
jq -e --arg ip "$NEW" '.dns_records[] | select(.type=="A" and .data.value==$ip)' <<<"$RECS" >/dev/null \
  || { echo "! DNS: $DOMAIN has no A record for $NEW — fix the zone first, $OLD still works"; exit 1; }
STALE=0
for rid in $(jq -r --arg ip "$NEW" --arg d "$DOMAIN" \
             '.dns_records[] | select(.type=="A" and .fqdn==$d and .data.value!=$ip) | .id' <<<"$RECS"); do
  tw DELETE "/domains/$DOMAIN/dns-records/$rid" >/dev/null || { echo "! old A record $rid survived"; STALE=1; }
done   # .fqdn is checked too, so this loop can never reach the zone apex A record of $ZONE
# switch config and re-issue
sed -i "s/^SERVER_IP=.*/SERVER_IP=$NEW/" "$ETC/vpn.env"
render.sh
systemctl restart xray
for n in $(jq -r '.[].name' "$ETC/users.json"); do add-client.sh "$n" >/dev/null 2>&1 || echo "! add-client $n failed"; done
[ "$STALE" = 0 ] || echo "! an old A record is still in the zone: some clients will resolve to $OLD"
echo "done: $OLD -> $NEW. ClashMi subscriptions update themselves; Amnezia keys must be handed out again."
