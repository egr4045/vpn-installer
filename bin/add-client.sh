#!/usr/bin/env bash
# add-client.sh <name> — create (or refresh) a person: xray UUID on every inbound of every node +
# an AmneziaWG peer + a personal subscription (one QR for every xray server) + AmneziaVPN native keys.
# Writes /root/clients/<name>/ and prints the links.
#
#   sub          https://DOMAIN/s/<token>  — all xray servers in one link (Happ, v2rayTun, Streisand…)
#   amnezia      vpn:// key for AmneziaVPN: this exit, xray XHTTP+REALITY + AmneziaWG   (-awg: AWG default)
#   amnezia-ru   vpn:// key via the RU entry (mobile white lists)     amnezia-x2  via the standby exit
#   nl-xhttp     main: XHTTP+REALITY to this exit over IPv4          nl6-xhttp   same over IPv6
#   x2-xhttp     standby exit                                       ru-xhttp / ru6-xhttp  RU entry
#   *-tcp        fallback TCP+REALITY+vision                        awg.conf / awg6.conf / awg-x2.conf
set -euo pipefail
ETC=/etc/safechill; OUT=/root/clients; TPL=/usr/local/share/safechill/templates; SUBDIR=/var/www/html/s
NAME="${1:-}"; [[ "$NAME" =~ ^[A-Za-z0-9_-]{1,32}$ ]] || { echo "usage: add-client.sh <name>   (letters, digits, - _)"; exit 1; }
set -a; . "$ETC/vpn.env"; . "$ETC/secrets.env"; set +a
BRAND=${BRAND:-SafeChill}; TCP_PORT=${TCP_PORT:-8443}; FALLBACK_SNI=${FALLBACK_SNI:-gateway.icloud.com}
AWG_NET4=${AWG_NET4:-10.8.0}; AWG_NET6=${AWG_NET6:-fd08:5afe:c411}; AWG_PORT=${AWG_PORT:-39217}

# -- identity: uuid + subscription token in users.json, awg keys in peers/<name>.env ----------------
if jq -e --arg n "$NAME" '.[]|select(.name==$n)' "$ETC/users.json" >/dev/null; then
  echo "xray user '$NAME' exists, refreshing files"
else
  tmp=$(mktemp); jq --arg n "$NAME" --arg u "$(xray uuid)" '. + [{name:$n, uuid:$u}]' "$ETC/users.json" > "$tmp" && install -m600 "$tmp" "$ETC/users.json" && rm -f "$tmp"
fi
if [ "$(jq -r --arg n "$NAME" '.[]|select(.name==$n)|.sub // empty' "$ETC/users.json")" = "" ]; then
  tmp=$(mktemp); jq --arg n "$NAME" --arg s "$(openssl rand -hex 12)" 'map(if .name==$n then .sub=$s else . end)' "$ETC/users.json" > "$tmp" && install -m600 "$tmp" "$ETC/users.json" && rm -f "$tmp"
fi
UUID=$(jq -r --arg n "$NAME" '.[]|select(.name==$n)|.uuid' "$ETC/users.json")
SUB=$(jq -r --arg n "$NAME" '.[]|select(.name==$n)|.sub' "$ETC/users.json")
if [ ! -f "$ETC/peers/$NAME.env" ]; then
  used=$(find "$ETC/peers" -name '*.env' -exec awk -F= '/^PEER_N=/{print $2}' {} + | sort -n | tail -1)
  N=$(( ${used:-1} + 1 )); [ "$N" -le 250 ] || { echo "no free AWG addresses"; exit 1; }
  PRIV=$(awg genkey)
  umask 077
  { echo "PEER_NAME=$NAME"; echo "PEER_N=$N"; echo "PEER_PRIV=$PRIV"; echo "PEER_PUB=$(awg pubkey <<<"$PRIV")"; echo "PEER_PSK=$(awg genpsk)"; } > "$ETC/peers/$NAME.env"
  umask 022
fi
set -a; . "$ETC/peers/$NAME.env"; set +a

render.sh
systemctl restart xray
awg syncconf awg0 <(awg-quick strip awg0) 2>/dev/null || systemctl restart awg-quick@awg0

# -- files --------------------------------------------------------------------------------------
D="$OUT/$NAME"; mkdir -p "$D" "$SUBDIR"; chmod 700 "$OUT"; rm -f "$D"/*.txt "$D"/*.png "$D"/*.conf
EXTRA=$(python3 - <<'PY'
import urllib.parse, json
x = {"xmux": {"maxConcurrency": "16-32", "maxConnections": 0, "cMaxReuseTimes": "64-128",
              "hMaxRequestTimes": "800-900", "hMaxReusableSecs": "1800-3000"}}
print(urllib.parse.quote(json.dumps(x, separators=(",", ":")), safe=""))
PY
)
ORDER=()   # subscription order = client preference
xhttp_link() { # <stem> <host> <sni> <fp> <label>
  echo "vless://$UUID@$2:443?encryption=none&security=reality&type=xhttp&path=%2F$XHTTP_PATH&mode=auto&sni=$3&fp=$4&pbk=$REALITY_PUB&sid=$SHORT_ID&extra=$EXTRA#$BRAND-$5-XHTTP-$NAME" > "$D/$1.txt"
  qrencode -o "$D/$1.png" -s 6 < "$D/$1.txt"; ORDER+=("$1"); }
tcp_link() {
  echo "vless://$UUID@$2:$TCP_PORT?encryption=none&security=reality&type=tcp&flow=xtls-rprx-vision&sni=$3&fp=$4&pbk=$REALITY_PUB&sid=$SHORT_ID#$BRAND-$5-TCP-$NAME" > "$D/$1.txt"
  qrencode -o "$D/$1.png" -s 6 < "$D/$1.txt"; ORDER+=("$1"); }
awg_conf() { ENDPOINT="$2" envsubst < "$TPL/awg-client.conf.tpl" > "$D/$1.conf"; qrencode -o "$D/$1.png" -s 5 < "$D/$1.conf"; }

xhttp_link nl-xhttp "$SERVER_IP" "$DOMAIN" firefox NL
[ -n "${SERVER_IP6:-}" ] && xhttp_link nl6-xhttp "[$SERVER_IP6]" "$DOMAIN" firefox NL6
[ -n "${EXIT2_HOST:-}" ] && xhttp_link x2-xhttp "$EXIT2_HOST" "$EXIT2_DOMAIN" firefox X2
if [ -n "${RU_HOST:-}" ]; then
  RU_SNI=${RU_SNI:-yandex.ru}
  xhttp_link ru-xhttp "$RU_HOST" "$RU_SNI" chrome RU
  [ -n "${RU_HOST6:-}" ] && xhttp_link ru6-xhttp "[$RU_HOST6]" "$RU_SNI" chrome RU6
fi
tcp_link nl-tcp "$SERVER_IP" "$FALLBACK_SNI" chrome NL
[ -n "${SERVER_IP6:-}" ] && tcp_link nl6-tcp "[$SERVER_IP6]" "$FALLBACK_SNI" chrome NL6
[ -n "${EXIT2_HOST:-}" ] && tcp_link x2-tcp "$EXIT2_HOST" "$FALLBACK_SNI" chrome X2
if [ -n "${RU_HOST:-}" ]; then
  tcp_link ru-tcp "$RU_HOST" "$RU_SNI" chrome RU
  [ -n "${RU_HOST6:-}" ] && tcp_link ru6-tcp "[$RU_HOST6]" "$RU_SNI" chrome RU6
fi
awg_conf awg "$SERVER_IP:$AWG_PORT"
[ -n "${SERVER_IP6:-}" ] && awg_conf awg6 "[$SERVER_IP6]:$AWG_PORT"
[ -n "${EXIT2_HOST:-}" ] && awg_conf awg-x2 "$EXIT2_HOST:$AWG_PORT"

# subscription: every xray link, base64, served by nginx behind the REALITY steal on :443
for s in "${ORDER[@]}"; do cat "$D/$s.txt"; done | base64 -w0 > "$SUBDIR/$SUB"; chmod 644 "$SUBDIR/$SUB"
echo "https://$DOMAIN/s/$SUB" > "$D/sub.txt"; qrencode -o "$D/sub.png" -s 6 < "$D/sub.txt"
# AmneziaVPN native keys (one QR = xray + AWG): this exit (xray default / AWG default), RU entry, standby exit
amnezia-key.py "$NAME" nl >/dev/null
amnezia-key.py "$NAME" nl awg >/dev/null
[ -n "${RU_HOST:-}" ] && amnezia-key.py "$NAME" ru >/dev/null
[ -n "${EXIT2_HOST:-}" ] && amnezia-key.py "$NAME" x2 >/dev/null
chmod 600 "$D"/*
echo; echo "== $NAME =="
echo "ONE-QR subscription (Happ etc.): $(cat "$D/sub.txt")"; echo
echo "AmneziaVPN one-QR key (xray+AWG): $D/amnezia.txt ($(wc -c < "$D/amnezia.txt") chars)"; echo
echo "NL XHTTP (main, AmneziaVPN):     $(cat "$D/nl-xhttp.txt")"; echo
echo "all files:                       $(ls "$D" | tr '\n' ' ')"
