#!/usr/bin/env bash
# del-client.sh <name> — revoke a person everywhere (xray on both nodes + awg) and delete their files.
set -euo pipefail
ETC=/etc/safechill; NAME="${1:?usage: del-client.sh <name>}"
tmp=$(mktemp); jq --arg n "$NAME" 'map(select(.name!=$n))' "$ETC/users.json" > "$tmp" && install -m600 "$tmp" "$ETC/users.json" && rm -f "$tmp"
rm -f "$ETC/peers/$NAME.env"; rm -rf "/root/clients/$NAME"
render.sh
systemctl restart xray
awg syncconf awg0 <(awg-quick strip awg0) 2>/dev/null || systemctl restart awg-quick@awg0
echo "removed $NAME"
