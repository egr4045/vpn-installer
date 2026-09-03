#!/usr/bin/env bash
# render.sh — render every config from /etc/safechill and push the RU relay config (if any).
#   xray (exit node)  : templates/xray.json.tpl    + users.json + relay user
#   AmneziaWG         : templates/awg0.conf.tpl    + peers/*.env
#   xray (RU entry)   : templates/xray-ru.json.tpl + users.json -> scp to RU_HOST, restart there
#   RU health env     : /etc/safechill/ru.env on the RU node (what to watch, where to alert)
# Does NOT restart local services; callers decide. Safe to run any time.
set -euo pipefail
ETC=/etc/safechill
TPL="${REPO_TPL:-/usr/local/share/safechill/templates}"
set -a; . "$ETC/vpn.env"; . "$ETC/secrets.env"; set +a
export TORRENT_OUT=$([ "${BLOCK_TORRENT:-1}" = 1 ] && echo block || echo direct)
export EGRESS_STRATEGY=$([ "${EGRESS_PREFER:-ipv6}" = ipv6 ] && echo UseIPv6v4 || echo UseIPv4v6)
export TCP_PORT=${TCP_PORT:-8443} FALLBACK_SNI=${FALLBACK_SNI:-gateway.icloud.com}
export AWG_NET4=${AWG_NET4:-10.8.0} AWG_NET6=${AWG_NET6:-fd08:5afe:c411} AWG_PORT=${AWG_PORT:-39217}

inject_users() { # inject_users <rendered.json> <out.json> [relay-uuid]
  jq --slurpfile users "$ETC/users.json" --arg relay "${3:-}" '
    ($users[0] + (if $relay != "" then [{name:"relay-ru", uuid:$relay}] else [] end)) as $u |
    .inbounds |= map(
      if .tag=="xhttp-reality" then .settings.clients = ($u | map({id:.uuid, email:.name}))
      elif .tag=="tcp-reality"  then .settings.clients = ($u | map({id:.uuid, email:.name, flow:"xtls-rprx-vision"}))
      else . end)' "$1" > "$2"
}

t1=$(mktemp --suffix=.json); t2=$(mktemp --suffix=.json)   # xray detects config format by extension

# -- exit node xray ------------------------------------------------------------
envsubst < "$TPL/xray.json.tpl" > "$t1"
inject_users "$t1" "$t2" "$RELAY_UUID"
xray run -test -c "$t2" >/dev/null || { echo "xray config test FAILED:"; xray run -test -c "$t2"; exit 1; }
install -m644 "$t2" /usr/local/etc/xray/config.json
n_users=$(jq 'length' "$ETC/users.json")

# -- AmneziaWG -----------------------------------------------------------------
mkdir -p /etc/amnezia/amneziawg
{
  envsubst < "$TPL/awg0.conf.tpl"
  for f in "$ETC"/peers/*.env; do
    [ -f "$f" ] || continue
    ( set -a; . "$f"; set +a
      printf '\n[Peer]\n# %s\nPublicKey = %s\nPresharedKey = %s\nAllowedIPs = %s.%s/32, %s::%s/128\n' \
        "$PEER_NAME" "$PEER_PUB" "$PEER_PSK" "$AWG_NET4" "$PEER_N" "$AWG_NET6" "$PEER_N" )
  done
} > "$t1"
install -m600 "$t1" /etc/amnezia/amneziawg/awg0.conf
n_peers=$(find "$ETC/peers" -name '*.env' | wc -l)
echo "rendered exit node: $n_users users (+relay), $n_peers awg peers, egress $EGRESS_STRATEGY"

# -- RU entry node (optional) --------------------------------------------------
if [ -n "${RU_HOST:-}" ]; then
  export RU_SNI=${RU_SNI:-yandex.ru}
  envsubst < "$TPL/xray-ru.json.tpl" > "$t1"
  inject_users "$t1" "$t2" ""
  xray run -test -c "$t2" >/dev/null || { echo "RU xray config test FAILED:"; xray run -test -c "$t2"; exit 1; }
  SSHOPT=(-o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new)
  t3=$(mktemp)
  { echo "BRAND=${BRAND:-SafeChill}"; echo "NL_IP=$SERVER_IP"; echo "DOMAIN=$DOMAIN"; echo "TCP_PORT=$TCP_PORT"
    echo "FALLBACK_SNI=$FALLBACK_SNI"; echo "RU_SNI=$RU_SNI"; echo "TG_BOT_TOKEN=${TG_BOT_TOKEN:-}"
    echo "TG_CHAT_ID=${TG_CHAT_ID:-$(cat "$ETC/tg_chat_id" 2>/dev/null || true)}"; } > "$t3"
  if scp "${SSHOPT[@]}" -q "$t2" "root@$RU_HOST:/usr/local/etc/xray/config.json" \
     && scp "${SSHOPT[@]}" -q "$t3" "root@$RU_HOST:/etc/safechill/ru.env" \
     && ssh "${SSHOPT[@]}" "root@$RU_HOST" "chmod 644 /usr/local/etc/xray/config.json; chmod 600 /etc/safechill/ru.env; systemctl restart xray && systemctl is-active xray" >/dev/null; then
    echo "pushed RU relay config to $RU_HOST ($n_users users), xray restarted there"
  else
    echo "WARNING: could not push config to RU node $RU_HOST (unreachable?) — exit node is fine" >&2
  fi
  rm -f "$t3"
fi
rm -f "$t1" "$t2"
