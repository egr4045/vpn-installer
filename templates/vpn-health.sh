#!/usr/bin/env bash
# vpn-health.sh — runs every minute from vpn-health.timer on the exit node. Probes every protocol
# path, restarts what died, checks that popular sites are reachable from here, and tells Telegram
# once per state change (no spam; site outages are grouped into one message).
set -u
ETC=/etc/safechill; STATE=/var/lib/safechill/health.state; LOG=/var/log/vpn-health.log
set -a; . "$ETC/vpn.env" 2>/dev/null; set +a
TCP_PORT=${TCP_PORT:-8443}; FALLBACK_SNI=${FALLBACK_SNI:-gateway.icloud.com}; BRAND=${BRAND:-SafeChill}
SERVICES=${SERVICES:-"www.youtube.com www.google.com www.instagram.com web.telegram.org chatgpt.com x.com www.facebook.com discord.com www.tiktok.com web.whatsapp.com github.com www.netflix.com"}
mkdir -p /var/lib/safechill; touch "$STATE"

tg() { # tg <text> — TG_CHAT_ID from vpn.env, else /etc/safechill/tg_chat_id, else the first person who wrote to the bot
  [ -n "${TG_BOT_TOKEN:-}" ] || return 0
  local id="${TG_CHAT_ID:-}"; [ -n "$id" ] || id=$(cat "$ETC/tg_chat_id" 2>/dev/null || true)
  if [ -z "$id" ]; then
    id=$(curl -s -m 8 "https://api.telegram.org/bot$TG_BOT_TOKEN/getUpdates" | jq -r '[.result[]|.message.chat.id|select(.!=null)][0] // empty')
    [ -n "$id" ] || return 0
    echo "$id" > "$ETC/tg_chat_id"
    curl -s -m 8 -X POST "https://api.telegram.org/bot$TG_BOT_TOKEN/sendMessage" -d chat_id="$id" \
      --data-urlencode text="$BRAND: бот подключён, сюда будут приходить алерты с $(hostname)" >/dev/null
  fi
  curl -s -m 8 -X POST "https://api.telegram.org/bot$TG_BOT_TOKEN/sendMessage" -d chat_id="$id" --data-urlencode text="$1" >/dev/null
}
was()    { grep -qx "$1" "$STATE"; }
mark()   { grep -qx "$1" "$STATE" || echo "$1" >> "$STATE"; }
unmark() { grep -vx "$1" "$STATE" > "$STATE.tmp" || true; mv "$STATE.tmp" "$STATE"; }
report() { # report <key> <exit-code> <human text> [fix command]
  local key=$1 rc=$2 msg=$3 fix=${4:-}
  if [ "$rc" = 0 ]; then
    if was "$key"; then echo "$(date +%FT%T) OK   $msg" >> "$LOG"; tg "🟢 $BRAND: $msg — снова работает"; unmark "$key"; fi
  else
    if [ -n "$fix" ]; then eval "$fix" >/dev/null 2>&1; sleep 3; fi
    if ! was "$key"; then echo "$(date +%FT%T) FAIL $msg" >> "$LOG"; tg "🔴 $BRAND: $msg${fix:+ (перезапустил: $fix)}"; mark "$key"; fi
  fi
}

# 1. :443 XHTTP+REALITY — the steal target must answer like a real site
code=$(curl -sk -o /dev/null -w '%{http_code}' -m 6 --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/" || true)
[ "$code" = 200 ]; report xhttp $? "xray :443 XHTTP+Reality (HTTP $code)" "systemctl restart xray"
# 2. :8443 TCP+REALITY — must present the fallback site's certificate
timeout 8 openssl s_client -connect "127.0.0.1:$TCP_PORT" -servername "$FALLBACK_SNI" </dev/null 2>/dev/null | grep -q "CN *= *$FALLBACK_SNI"
report tcp $? "xray :$TCP_PORT TCP+Reality" "systemctl restart xray"
# 3. AmneziaWG
awg show awg0 >/dev/null 2>&1 && ip link show awg0 2>/dev/null | grep -q ',UP'
report awg $? "AmneziaWG awg0" "systemctl restart awg-quick@awg0"
# 4. nginx
systemctl is-active --quiet nginx; report nginx $? "nginx" "systemctl restart nginx"
# 5. egress, both families
curl -4 -s -o /dev/null -m 6 https://www.cloudflare.com/cdn-cgi/trace; report egress4 $? "внешний интернет IPv4 с сервера"
if [ -n "${SERVER_IP6:-}" ]; then curl -6 -s -o /dev/null -m 6 https://www.cloudflare.com/cdn-cgi/trace; report egress6 $? "внешний интернет IPv6 с сервера"; fi
# 6. certificate (Let's Encrypt, or the temporary self-signed one install.sh falls back to)
CERT_DIR=${CERT_DIR:-/etc/letsencrypt/live/$DOMAIN}
end=$(openssl x509 -enddate -noout -in "$CERT_DIR/fullchain.pem" 2>/dev/null | cut -d= -f2)
days=$(( ( $(date -d "$end" +%s 2>/dev/null || echo 0) - $(date +%s) ) / 86400 ))
[ "$days" -gt 10 ]; report cert $? "сертификат $DOMAIN истекает через $days дн." "certbot renew -q"
case "$CERT_DIR" in *selfsigned*) false;; *) true;; esac
report selfsigned $? "работаем на самоподписанном сертификате: направь $DOMAIN на этот сервер и запусти install.sh"
# 7. disk
use=$(df --output=pcent / | tail -1 | tr -dc 0-9); [ "$use" -lt 90 ]; report disk $? "диск заполнен на $use%"
# 8. RU entry node (optional)
if [ -n "${RU_HOST:-}" ]; then
  rcode=$(curl -sk -o /dev/null -w '%{http_code}' -m 8 --resolve "${RU_SNI:-yandex.ru}:443:$RU_HOST" "https://${RU_SNI:-yandex.ru}/" || true)
  [ "$rcode" != 000 ] && [ -n "$rcode" ]
  report ru $? "RU-вход $RU_HOST :443 (HTTP $rcode)" "ssh -o BatchMode=yes -o ConnectTimeout=10 root@$RU_HOST systemctl restart xray"
fi
# 9. popular sites reachable from the exit node — grouped, alert only when the set is stable for 2 runs
tmpd=$(mktemp -d)
for s in $SERVICES; do
  ( c=$(curl -s -o /dev/null -m 8 -w '%{http_code}' "https://$s/" || true); [ "$c" = 000 ] && echo "$s" > "$tmpd/$s" ) &
done; wait
now=$(ls "$tmpd" 2>/dev/null | sort | tr '\n' ' ' | sed 's/ $//'); rm -rf "$tmpd"
last=$(cat /var/lib/safechill/services.last 2>/dev/null || true); alerted=$(cat /var/lib/safechill/services.alerted 2>/dev/null || true)
echo "$now" > /var/lib/safechill/services.last
if [ "$now" = "$last" ] && [ "$now" != "$alerted" ]; then
  if [ -n "$now" ]; then tg "🟠 $BRAND: с NL-ноды недоступны: $now"; echo "$(date +%FT%T) SITES DOWN $now" >> "$LOG"
  else tg "🟢 $BRAND: все сервисы снова доступны с NL-ноды"; echo "$(date +%FT%T) SITES OK" >> "$LOG"; fi
  echo "$now" > /var/lib/safechill/services.alerted
fi
echo "$(date +%FT%T) checked, failing=$(wc -l < "$STATE")${now:+, sites down: $now}" >> "$LOG"
tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
