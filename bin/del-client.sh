#!/usr/bin/env bash
# del-client.sh <name> — revoke a person everywhere (xray on both nodes, awg, subscription) and delete their files.
set -euo pipefail
ETC=/etc/safechill; NAME="${1:?usage: del-client.sh <name>}"
SUB=$(jq -r --arg n "$NAME" '.[]|select(.name==$n)|.sub // empty' "$ETC/users.json")
# every artefact keyed by the token: xray subscription, mihomo profiles, ClashMi restore archive
[ -n "$SUB" ] && rm -f "/var/www/html/s/$SUB" "/var/www/html/c/$SUB" "/var/www/html/c/$SUB-game" "/var/www/html/z/$SUB.zip"
tmp=$(mktemp); jq --arg n "$NAME" 'map(select(.name!=$n))' "$ETC/users.json" > "$tmp" && install -m600 "$tmp" "$ETC/users.json" && rm -f "$tmp"
rm -f "$ETC/peers/$NAME.env"; rm -rf "/root/clients/$NAME"
render.sh
systemctl restart xray
awg syncconf awg0 <(awg-quick strip awg0) 2>/dev/null || systemctl restart awg-quick@awg0
echo "removed $NAME"
