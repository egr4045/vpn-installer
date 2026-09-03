#!/usr/bin/env bash
# add-client.sh <name> — create (or refresh) a person: xray UUID on every inbound of both nodes +
# an AmneziaWG peer. Writes /root/clients/<name>/ and prints the links.
#
#   nl-xhttp   main: XHTTP+REALITY to the exit node over IPv4        nl6-xhttp  same over IPv6
#   nl-tcp     fallback: TCP+REALITY+vision, SNI icloud               nl6-tcp
#   ru-xhttp   entry node in Russia (mobile "white lists")            ru6-xhttp
#   ru-tcp                                                             ru6-tcp
#   awg.conf   AmneziaWG 3.1 over IPv4                                awg6.conf  over IPv6
set -euo pipefail
ETC=/etc/safechill; OUT=/root/clients; TPL=/usr/local/share/safechill/templates
NAME="${1:-}"; [[ "$NAME" =~ ^[A-Za-z0-9_-]{1,32}$ ]] || { echo "usage: add-client.sh <name>   (letters, digits, - _)"; exit 1; }
set -a; . "$ETC/vpn.env"; . "$ETC/secrets.env"; set +a
BRAND=${BRAND:-SafeChill}; TCP_PORT=${TCP_PORT:-8443}; FALLBACK_SNI=${FALLBACK_SNI:-gateway.icloud.com}
AWG_NET4=${AWG_NET4:-10.8.0}; AWG_NET6=${AWG_NET6:-fd08:5afe:c411}; AWG_PORT=${AWG_PORT:-39217}

if jq -e --arg n "$NAME" '.[]|select(.name==$n)' "$ETC/users.json" >/dev/null; then
  UUID=$(jq -r --arg n "$NAME" '.[]|select(.name==$n)|.uuid' "$ETC/users.json"); echo "xray user '$NAME' exists, reusing"
else
  UUID=$(xray uuid); tmp=$(mktemp)
  jq --arg n "$NAME" --arg u "$UUID" '. + [{name:$n, uuid:$u}]' "$ETC/users.json" > "$tmp" && install -m600 "$tmp" "$ETC/users.json" && rm -f "$tmp"
fi
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

D="$OUT/$NAME"; mkdir -p "$D"; chmod 700 "$OUT"; rm -f "$D"/*.txt "$D"/*.png "$D"/*.conf
EXTRA=$(python3 - <<'PY'
import urllib.parse, json
x = {"xmux": {"maxConcurrency": "16-32", "maxConnections": 0, "cMaxReuseTimes": "64-128",
              "hMaxRequestTimes": "800-900", "hMaxReusableSecs": "1800-3000"}}
print(urllib.parse.quote(json.dumps(x, separators=(",", ":")), safe=""))
PY
)
# link <file-stem> <host> <sni> <fp> <label>
xhttp_link() { echo "vless://$UUID@$2:443?encryption=none&security=reality&type=xhttp&path=%2F$XHTTP_PATH&mode=auto&sni=$3&fp=$4&pbk=$REALITY_PUB&sid=$SHORT_ID&extra=$EXTRA#$BRAND-$5-XHTTP-$NAME" > "$D/$1.txt"; qrencode -o "$D/$1.png" -s 6 < "$D/$1.txt"; }
tcp_link()   { echo "vless://$UUID@$2:$TCP_PORT?encryption=none&security=reality&type=tcp&flow=xtls-rprx-vision&sni=$3&fp=$4&pbk=$REALITY_PUB&sid=$SHORT_ID#$BRAND-$5-TCP-$NAME" > "$D/$1.txt"; qrencode -o "$D/$1.png" -s 6 < "$D/$1.txt"; }
awg_conf()   { ENDPOINT="$2" envsubst < "$TPL/awg-client.conf.tpl" > "$D/$1.conf"; qrencode -o "$D/$1.png" -s 5 < "$D/$1.conf"; }

xhttp_link nl-xhttp "$SERVER_IP" "$DOMAIN" firefox NL
tcp_link   nl-tcp   "$SERVER_IP" "$FALLBACK_SNI" chrome NL
awg_conf   awg      "$SERVER_IP:$AWG_PORT"
if [ -n "${SERVER_IP6:-}" ]; then
  xhttp_link nl6-xhttp "[$SERVER_IP6]" "$DOMAIN" firefox NL6
  tcp_link   nl6-tcp   "[$SERVER_IP6]" "$FALLBACK_SNI" chrome NL6
  awg_conf   awg6      "[$SERVER_IP6]:$AWG_PORT"
fi
if [ -n "${RU_HOST:-}" ]; then
  RU_SNI=${RU_SNI:-yandex.ru}
  xhttp_link ru-xhttp "$RU_HOST" "$RU_SNI" chrome RU
  tcp_link   ru-tcp   "$RU_HOST" "$RU_SNI" chrome RU
  if [ -n "${RU_HOST6:-}" ]; then
    xhttp_link ru6-xhttp "[$RU_HOST6]" "$RU_SNI" chrome RU6
    tcp_link   ru6-tcp   "[$RU_HOST6]" "$RU_SNI" chrome RU6
  fi
fi
chmod 600 "$D"/*
echo; echo "== $NAME =="
echo "NL XHTTP (main):   $(cat "$D/nl-xhttp.txt")"; echo
echo "NL TCP (fallback): $(cat "$D/nl-tcp.txt")"; echo
[ -f "$D/ru-xhttp.txt" ] && { echo "RU XHTTP (mobile whitelists): $(cat "$D/ru-xhttp.txt")"; echo; }
echo "AmneziaWG 3.1:     $D/awg.conf $([ -f "$D/awg6.conf" ] && echo "+ awg6.conf (IPv6)")"
echo "all files:         $(ls "$D" | tr '\n' ' ')"
