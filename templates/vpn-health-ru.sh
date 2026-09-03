#!/usr/bin/env bash
# vpn-health-ru.sh — runs every minute ON the RU entry node. Watches its own xray and the exit node,
# restarts local xray when needed, alerts Telegram once per state change. Env: /etc/safechill/ru.env
# (pushed by the exit node's render.sh).
set -u
ENV=/etc/safechill/ru.env; STATE=/var/lib/safechill/health.state; LOG=/var/log/vpn-health.log
[ -f "$ENV" ] || exit 0
set -a; . "$ENV"; set +a
mkdir -p /var/lib/safechill; touch "$STATE"; BRAND=${BRAND:-SafeChill}
tg() { [ -n "${TG_BOT_TOKEN:-}" ] && [ -n "${TG_CHAT_ID:-}" ] || return 0
  curl -s -m 8 -X POST "https://api.telegram.org/bot$TG_BOT_TOKEN/sendMessage" -d chat_id="$TG_CHAT_ID" --data-urlencode text="$1" >/dev/null; }
was()    { grep -qx "$1" "$STATE"; }
mark()   { grep -qx "$1" "$STATE" || echo "$1" >> "$STATE"; }
unmark() { grep -vx "$1" "$STATE" > "$STATE.tmp" || true; mv "$STATE.tmp" "$STATE"; }
report() { local key=$1 rc=$2 msg=$3 fix=${4:-}
  if [ "$rc" = 0 ]; then
    if was "$key"; then echo "$(date +%FT%T) OK   $msg" >> "$LOG"; tg "🟢 $BRAND (RU-нода): $msg — снова работает"; unmark "$key"; fi
  else
    if [ -n "$fix" ]; then eval "$fix" >/dev/null 2>&1; sleep 3; fi
    if ! was "$key"; then echo "$(date +%FT%T) FAIL $msg" >> "$LOG"; tg "🔴 $BRAND (RU-нода): $msg${fix:+ (перезапустил: $fix)}"; mark "$key"; fi
  fi
}
# own xray: REALITY steal must answer like the pretended site
code=$(curl -sk -o /dev/null -w '%{http_code}' -m 8 --resolve "$RU_SNI:443:127.0.0.1" "https://$RU_SNI/" || true)
[ "$code" != 000 ] && [ -n "$code" ]; report self $? "xray RU-входа :443 (HTTP $code)" "systemctl restart xray"
# exit node from Russia: 443 steal site and 8443 icloud certificate
code=$(curl -sk -o /dev/null -w '%{http_code}' -m 8 --resolve "$DOMAIN:443:$NL_IP" "https://$DOMAIN/" || true)
[ "$code" = 200 ]; report nl443 $? "NL-выход $NL_IP :443 недоступен из России (HTTP $code)"
timeout 8 openssl s_client -connect "$NL_IP:$TCP_PORT" -servername "$FALLBACK_SNI" </dev/null 2>/dev/null | grep -q "CN *= *$FALLBACK_SNI"
report nl8443 $? "NL-выход $NL_IP :$TCP_PORT недоступен из России"
echo "$(date +%FT%T) checked, failing=$(wc -l < "$STATE")" >> "$LOG"
tail -n 1000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
