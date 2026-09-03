#!/usr/bin/env bash
# add-client.sh <name> — create a person: xray UUID (all inbounds, both nodes) + AmneziaWG peer.
# Writes /root/clients/<name>/ (links, awg.conf, QR pngs) and prints the links.
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

mkdir -p "$OUT/$NAME"; chmod 700 "$OUT"
EXTRA=$(python3 - <<'PY'
import urllib.parse, json
x = {"xmux": {"maxConcurrency": "16-32", "maxConnections": 0, "cMaxReuseTimes": "64-128",
              "hMaxRequestTimes": "800-900", "hMaxReusableSecs": "1800-3000"}}
print(urllib.parse.quote(json.dumps(x, separators=(",", ":")), safe=""))
PY
)
XHTTP="vless://$UUID@$SERVER_IP:443?encryption=none&security=reality&type=xhttp&path=%2F$XHTTP_PATH&mode=auto&sni=$DOMAIN&fp=firefox&pbk=$REALITY_PUB&sid=$SHORT_ID&extra=$EXTRA#$BRAND-NL-XHTTP-$NAME"
TCP="vless://$UUID@$SERVER_IP:$TCP_PORT?encryption=none&security=reality&type=tcp&flow=xtls-rprx-vision&sni=$FALLBACK_SNI&fp=chrome&pbk=$REALITY_PUB&sid=$SHORT_ID#$BRAND-NL-TCP-$NAME"
echo "$XHTTP" > "$OUT/$NAME/nl-xhttp.txt"; echo "$TCP" > "$OUT/$NAME/nl-tcp.txt"
qrencode -o "$OUT/$NAME/nl-xhttp.png" -s 6 "$XHTTP"; qrencode -o "$OUT/$NAME/nl-tcp.png" -s 6 "$TCP"
envsubst < "$TPL/awg-client.conf.tpl" > "$OUT/$NAME/awg.conf"
qrencode -o "$OUT/$NAME/awg.png" -s 5 < "$OUT/$NAME/awg.conf"
if [ -n "${RU_HOST:-}" ]; then
  RU_SNI=${RU_SNI:-yandex.ru}
  RUX="vless://$UUID@$RU_HOST:443?encryption=none&security=reality&type=xhttp&path=%2F$XHTTP_PATH&mode=auto&sni=$RU_SNI&fp=chrome&pbk=$REALITY_PUB&sid=$SHORT_ID&extra=$EXTRA#$BRAND-RU-XHTTP-$NAME"
  RUT="vless://$UUID@$RU_HOST:$TCP_PORT?encryption=none&security=reality&type=tcp&flow=xtls-rprx-vision&sni=$RU_SNI&fp=chrome&pbk=$REALITY_PUB&sid=$SHORT_ID#$BRAND-RU-TCP-$NAME"
  echo "$RUX" > "$OUT/$NAME/ru-xhttp.txt"; echo "$RUT" > "$OUT/$NAME/ru-tcp.txt"
  qrencode -o "$OUT/$NAME/ru-xhttp.png" -s 6 "$RUX"; qrencode -o "$OUT/$NAME/ru-tcp.png" -s 6 "$RUT"
fi
chmod 600 "$OUT/$NAME"/*
echo; echo "== $NAME =="
echo "NL XHTTP (main):     $XHTTP"; echo
echo "NL TCP (fallback):   $TCP"; echo
[ -n "${RU_HOST:-}" ] && { echo "RU XHTTP (mobile whitelists): $RUX"; echo; echo "RU TCP:              $RUT"; echo; }
echo "AmneziaWG 3.1 conf:  $OUT/$NAME/awg.conf"
echo "QR codes:            $OUT/$NAME/*.png"
