#!/usr/bin/env bash
# vpn-health.sh — runs every minute from vpn-health.timer on the exit node.
# Probes every path, restarts what died and re-probes; alerts Telegram once per incident (after TWO failed
# runs, so a single dropped probe stays quiet). On recovery the SAME alert message is edited into the
# resolved state (started / ended / downtime), so one incident = one message. Writes
# /var/lib/safechill/health.json for the bot's /status and sends a daily digest at 09:00 MSK.
#
# Alert grammar (HTML, ≤5 lines):   <emoji> <b>Node · what happened, in plain words</b>
#                                    technical detail (port, HTTP code, minutes)
#                                    what was done automatically and whether it helped
#                                    👥 who of the people is affected and what they should do
#                                    → what the admin does (🔴/🟠 only)
# Resolved (edited in place):        🟢 <b>Node · what works again</b>
#                                    Началось 14:02, закончилось 14:08, простой 6 мин.
#                                    <i>Было: HTTP 000 · Перезапустил xray — не помогло.</i>
# Levels: 🔴 people can't use it (sound) · 🟠 degraded (sound) · 🟡 heads-up (silent) · 🟢 recovered · 🔵 info (silent)
# Vocabulary shared with the bot and the RU script: 🇳🇱 Амстердам · 🇷🇺 Москва · 🛟 Запасной.
set -u
ETC=/etc/safechill; LIB=/var/lib/safechill; STATE=$LIB/health.state; LOG=/var/log/vpn-health.log
set -a; . "$ETC/vpn.env" 2>/dev/null; set +a
TCP_PORT=${TCP_PORT:-8443}; FALLBACK_SNI=${FALLBACK_SNI:-gateway.icloud.com}; BRAND=${BRAND:-SafeChill}
NODE=${NODE_NAME:-Амстердам}; FLAG=${NODE_FLAG:-🇳🇱}
SERVICES=${SERVICES:-"www.youtube.com www.google.com www.instagram.com web.telegram.org chatgpt.com x.com www.facebook.com discord.com www.tiktok.com web.whatsapp.com github.com www.netflix.com"}
mkdir -p "$LIB"; touch "$STATE"
NOW=$(date +%s); TODAY=$(TZ=Europe/Moscow date +%F); TG="https://api.telegram.org/bot${TG_BOT_TOKEN:-}"
MSK()  { TZ=Europe/Moscow date -d "@${1:-$NOW}" +%H:%M; }
MON=(янв фев мар апр мая июн июл авг сен окт ноя дек)
when() { local t=${1:-$NOW}; if [ "$(TZ=Europe/Moscow date -d "@$t" +%F)" = "$TODAY" ]; then MSK "$t"; else echo "$(TZ=Europe/Moscow date -d "@$t" +%-d) ${MON[$(TZ=Europe/Moscow date -d "@$t" +%-m)-1]} $(MSK "$t")"; fi; }
MONTH=(января февраля марта апреля мая июня июля августа сентября октября ноября декабря)
rudate() { local d m; d=$(date -d "$1" +%-d 2>/dev/null) || { echo "$1"; return; }; m=$(date -d "$1" +%-m); echo "$d ${MONTH[m-1]}"; }
dur()  { local s=$1; if [ "$s" -lt 3600 ]; then echo "$(( (s+59)/60 )) мин"; elif [ "$s" -lt 86400 ]; then echo "$((s/3600)) ч $(( (s%3600)/60 )) мин"; else echo "$((s/86400)) дн. $(( (s%86400)/3600 )) ч"; fi; }
timeline() { echo "Началось $(when "$1"), закончилось $(when), простой $(dur $((NOW-$1)))."; }
logl() { echo "$(date +%FT%T) $*" >> "$LOG"; }
msg()  { printf '%s\n' "$@" | sed '/^$/d'; }            # join non-empty lines
emo()  { case $1 in crit) echo 🔴;; warn) echo 🟠;; *) echo 🟡;; esac; }
loud() { case $1 in crit|warn) echo 0;; *) echo 1;; esac; }   # 0 = with sound
plural() { local n=$1 one=$2 few=$3 many=$4 m=$(( $1 % 10 )) h=$(( $1 % 100 ))
  if [ "$m" = 1 ] && [ "$h" != 11 ]; then echo "$n $one"; elif [ "$m" -ge 2 ] && [ "$m" -le 4 ] && { [ "$h" -lt 12 ] || [ "$h" -gt 14 ]; }; then echo "$n $few"; else echo "$n $many"; fi; }

# ── Telegram ─────────────────────────────────────────────────────────────────────────────────────
tg_chat() { # chat id: TG_CHAT_ID, else /etc/safechill/tg_chat_id (the bot writes it on the first admin message)
  local id="${TG_CHAT_ID:-}"; [ -n "$id" ] || id=$(cat "$ETC/tg_chat_id" 2>/dev/null || true)
  if [ -z "$id" ]; then
    [ "${BOT_ENABLED:-1}" = 1 ] && return 1          # the bot owns getUpdates; it will bind the chat itself
    id=$(curl -s -m 8 "$TG/getUpdates" | jq -r '[.result[]|.message.chat.id|select(.!=null)][0] // empty')
    [ -n "$id" ] || return 1
    echo "$id" > "$ETC/tg_chat_id"
    tg_raw "$id" 1 "$(msg "🔵 <b>$BRAND · алерты подключены</b>" "Сюда будут приходить сообщения с ноды $NODE.")" >/dev/null
  fi
  echo "$id"
}
tg_raw() { # <chat> <silent 0|1> <html> -> prints message_id
  local sil=false out mid; [ "$2" = 1 ] && sil=true
  out=$(curl -s -m 8 -f -X POST "$TG/sendMessage" -d chat_id="$1" -d parse_mode=HTML -d disable_web_page_preview=true \
        -d disable_notification=$sil --data-urlencode text="$3") || return 1
  mid=$(jq -r '.result.message_id // empty' <<<"$out"); [ -n "$mid" ] || return 1; echo "$mid"
}
tg() { # tg <silent 0|1> <html> [state-key] -> prints message_id; spooled (re-sent later) when Telegram is unreachable
  [ -n "${TG_BOT_TOKEN:-}" ] && [ -n "$2" ] || return 0
  local id mid; id=$(tg_chat) || return 0
  if mid=$(tg_raw "$id" "$1" "$2"); then echo "$mid"
  else printf '%s\t%s\t%s\t%s\n' "$NOW" "$1" "$(printf %s "$2" | base64 -w0)" "${3:-}" >> "$LIB/tg.spool"; fi
}
tg_edit() { # <message_id> <html> — turn the alert into its resolved state; fails if the message is gone/too old
  [ -n "${TG_BOT_TOKEN:-}" ] && [ -n "$1" ] || return 1
  local id; id=$(tg_chat) || return 1
  curl -s -m 8 -f -o /dev/null -X POST "$TG/editMessageText" -d chat_id="$id" -d message_id="$1" -d parse_mode=HTML \
       -d disable_web_page_preview=true --data-urlencode text="$2"
}
flush_spool() {
  [ -s "$LIB/tg.spool" ] && [ -n "${TG_BOT_TOKEN:-}" ] || return 0
  local id; id=$(tg_chat) || return 0
  local tmp ts sil b64 key text mid; tmp=$(mktemp); mv "$LIB/tg.spool" "$tmp"
  while IFS=$'\t' read -r ts sil b64 key; do
    text="$(base64 -d <<<"$b64")"$'\n'"<i>задержано, событие в $(when "$ts")</i>"
    if mid=$(tg_raw "$id" "$sil" "$text"); then [ -n "$key" ] && st_setmsg "$key" "$mid"
    else printf '%s\t%s\t%s\t%s\n' "$ts" "$sil" "$b64" "$key" >> "$LIB/tg.spool"; cat >> "$LIB/tg.spool"; break; fi
  done < "$tmp"; rm -f "$tmp"
}

# ── state: one line per failing check  key ⇥ since ⇥ fails ⇥ alerted ⇥ msgid ⇥ what-was-wrong ────
if [ -s "$STATE" ] && ! grep -q $'\t' "$STATE"; then awk -v n="$NOW" '{print $1"\t"n"\t2\t1\t-\t-"}' "$STATE" > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"; fi
st_get() { awk -F'\t' -v k="$1" '$1==k{print $2"\t"$3"\t"$4"\t"$5"\t"$6; exit}' "$STATE"; }
# "-" stands for an empty msgid / description: `read` with a tab IFS collapses empty fields and would shift them
st_put() { awk -F'\t' -v k="$1" '$1!=k' "$STATE" > "$STATE.tmp"; printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "${5:--}" "${6:--}" >> "$STATE.tmp"; mv "$STATE.tmp" "$STATE"; }
st_del() { awk -F'\t' -v k="$1" '$1!=k' "$STATE" > "$STATE.tmp"; mv "$STATE.tmp" "$STATE"; }
st_setmsg() { awk -F'\t' -v OFS='\t' -v k="$1" -v m="$2" '$1==k{$5=m}1' "$STATE" > "$STATE.tmp"; mv "$STATE.tmp" "$STATE"; }
failing_keys() { awk -F'\t' '$4==1 && $1!~/^flap_/{print $1}' "$STATE"; }
incident() { printf '%s\t%s\t%s\t%s\t%s\n' "$3" "$NOW" "$1" "$2" "${TITLE[$2]:-$2}" >> "$LIB/incidents.log"; }

declare -A OK DETAILS
declare -A TITLE=([xhttp]="основной путь" [tcp]="резервный путь" [awg]="AmneziaWG" [nginx]="сайт-прикрытие" [egress4]="нет интернета"
  [egress6]="IPv6" [cert14]="сертификат" [cert3]="сертификат" [selfsigned]="самоподписанный сертификат" [disk]="диск" [disk95]="диск"
  [mem]="память" [ru]="вход через Россию" [exit2]="запасной выход" [sites]="сайты")

resolve() { # <key> <since> <msgid> <was> <sev>: edit the alert into "resolved"; send a new message if editing is impossible
  local text; text="$("ok_$1")"$'\n'"$(timeline "$2")"; [ -n "$4" ] && text+=$'\n'"<i>Было: $4</i>"
  tg_edit "$3" "$text" || tg 1 "$text" >/dev/null
  logl "OK   $1 after $(dur $((NOW-$2)))"; incident "$5" "$1" "$2"
}

selfheal() { # <key> <label>: a restart fixed it — quiet, unless it keeps happening
  logl "HEAL $1 ($2 restarted)"; echo "$NOW $1" >> "$LIB/selfheal.log"
  awk -v t=$((NOW-86400)) '$1>t' "$LIB/selfheal.log" > "$LIB/selfheal.tmp" && mv "$LIB/selfheal.tmp" "$LIB/selfheal.log"
  local n since _; n=$(awk -v t=$((NOW-3600)) -v k="$1" '$1>t && $2==k' "$LIB/selfheal.log" | wc -l)
  IFS=$'\t' read -r since _ _ _ _ <<<"$(st_get "flap_$1")"
  if [ "$n" -ge 3 ] && { [ -z "${since:-}" ] || [ $((NOW-since)) -gt 3600 ]; }; then
    tg 0 "$(msg "🟠 <b>$NODE · $2 перезапускался $n раза за час</b>" "Каждый раз оживал сам после перезапуска." "→ <code>journalctl -u $2 -n 100</code>")" >/dev/null
    st_put "flap_$1" "$NOW" 0 1
  fi
}

check() { # check <key> <crit|warn|info> <probe-fn> [fix-cmd] [fix-label]
  # probe-fn sets DETAIL and returns 0/1; texts come from fail_<key> (uses $DETAIL $FIXNOTE $MIN) and ok_<key> (one line)
  local key=$1 sev=$2 probe=$3 fix=${4:-} label=${5:-} since fails alerted msgid was healed=0
  IFS=$'\t' read -r since fails alerted msgid was <<<"$(st_get "$key")"; since=${since:-0}; fails=${fails:-0}; alerted=${alerted:-0}
  [ "${msgid:-}" = "-" ] && msgid=""; [ "${was:-}" = "-" ] && was=""
  DETAIL=""; FIXNOTE=""
  if $probe; then healed=2
  elif [ -n "$fix" ]; then eval "$fix" >/dev/null 2>&1; sleep 5; $probe && healed=1; fi
  if [ "$healed" -gt 0 ]; then
    OK[$key]=1; DETAILS[$key]=$DETAIL
    [ "$healed" = 1 ] && [ "$fails" = 0 ] && selfheal "$key" "$label"
    if [ "$fails" -gt 0 ]; then
      [ "$alerted" = 1 ] && resolve "$key" "$since" "${msgid:-}" "${was:-}" "$sev"
      st_del "$key"
    fi
    return 0
  fi
  [ -n "$fix" ] && FIXNOTE="Перезапустил $label — не помогло."
  OK[$key]=0; DETAILS[$key]=$DETAIL
  fails=$((fails+1)); [ "$since" -gt 0 ] || since=$NOW
  if [ "$alerted" = 0 ] && [ "$fails" -ge 2 ]; then
    MIN=$(( (NOW-since)/60 )); [ "$MIN" -ge 1 ] || MIN=1
    msgid=$(tg "$(loud "$sev")" "$("fail_$key")" "$key"); alerted=1; was="$DETAIL${FIXNOTE:+ · $FIXNOTE}"; logl "FAIL $key $DETAIL"
  fi
  st_put "$key" "$since" "$fails" "$alerted" "${msgid:-}" "${was:-}"
}

# ── facts gathered once per run ──────────────────────────────────────────────────────────────────
CERT_DIR=${CERT_DIR:-/etc/letsencrypt/live/$DOMAIN}
read_cert() {
  CERT_END=$(openssl x509 -enddate -noout -in "$CERT_DIR/fullchain.pem" 2>/dev/null | cut -d= -f2)
  CERT_DAYS=$(( ( $(date -d "$CERT_END" +%s 2>/dev/null || echo 0) - NOW ) / 86400 ))
  case "$CERT_DIR" in *selfsigned*) CERT_ISSUER="самоподписанный";;
    *) CERT_ISSUER=$(openssl x509 -issuer -noout -in "$CERT_DIR/fullchain.pem" 2>/dev/null | sed -n 's/.*O *= *\([^,]*\).*/\1/p'); CERT_ISSUER=${CERT_ISSUER:-неизвестный};; esac
}
read_cert
# renew early, but not more often than hourly (Let's Encrypt allows 5 failures/hour)
if [ "$CERT_DAYS" -le 14 ] && [[ "$CERT_DIR" == /etc/letsencrypt/* ]] && [ $((NOW - $(stat -c %Y "$LIB/certbot.last" 2>/dev/null || echo 0))) -gt 3600 ]; then
  touch "$LIB/certbot.last"
  if timeout 120 certbot renew -q >/dev/null 2>&1; then systemctl reload nginx 2>/dev/null; systemctl restart xray 2>/dev/null; read_cert; fi
fi
DISK_PCT=$(df --output=pcent / | tail -1 | tr -dc 0-9); DISK_FREE=$(df -h --output=avail / | tail -1 | tr -d ' ')
MEM_TOTAL_K=$(awk '/MemTotal/{print $2}' /proc/meminfo); MEM_AVAIL_K=$(awk '/MemAvailable/{print $2}' /proc/meminfo)
MEM_PCT=$(( 100 - MEM_AVAIL_K*100/MEM_TOTAL_K )); MEM_FREE="$((MEM_AVAIL_K/1024)) МБ"; MEM_TOTAL="$((MEM_TOTAL_K/1024)) МБ"
UP=$(cut -d. -f1 /proc/uptime)
# money is deliberately NOT monitored or shown: the Timeweb balance is the owner's business, not an alert

# ── probes ───────────────────────────────────────────────────────────────────────────────────────
p_xhttp()   { local c; c=$(curl -sk -o /dev/null -w '%{http_code}' -m 6 --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/" || true); DETAIL="HTTP ${c:-000}"; [ "$c" = 200 ]; }
p_tcp()     { DETAIL="сертификат $FALLBACK_SNI"; timeout 8 openssl s_client -connect "127.0.0.1:$TCP_PORT" -servername "$FALLBACK_SNI" </dev/null 2>/dev/null | grep -q "CN *= *$FALLBACK_SNI"; }
p_awg()     { DETAIL="awg0"; awg show awg0 >/dev/null 2>&1 && ip link show awg0 2>/dev/null | grep -q ',UP'; }
p_nginx()   { DETAIL=$(systemctl is-active nginx); [ "$DETAIL" = active ]; }
p_egress4() { DETAIL="IPv4"; curl -4 -s -o /dev/null -m 6 https://www.cloudflare.com/cdn-cgi/trace; }
p_egress6() { DETAIL="IPv6"; curl -6 -s -o /dev/null -m 6 https://www.cloudflare.com/cdn-cgi/trace; }
p_ru()      { local c; c=$(curl -sk -o /dev/null -w '%{http_code}' -m 8 --resolve "${RU_SNI:-yandex.ru}:443:$RU_HOST" "https://${RU_SNI:-yandex.ru}/" || true); DETAIL="HTTP ${c:-000}"; [ -n "$c" ] && [ "$c" != 000 ]; }
p_exit2()   { local c; c=$(curl -sk -o /dev/null -w '%{http_code}' -m 8 --resolve "$EXIT2_DOMAIN:443:$EXIT2_HOST" "https://$EXIT2_DOMAIN/" || true); DETAIL="HTTP ${c:-000}"; [ "$c" = 200 ]; }
p_cert14()  { DETAIL="$CERT_DAYS дн."; [ "$CERT_DAYS" -gt 14 ]; }
p_cert3()   { DETAIL="$CERT_DAYS дн."; [ "$CERT_DAYS" -gt 3 ]; }
p_selfsigned() { DETAIL=$CERT_ISSUER; [[ "$CERT_DIR" != *selfsigned* ]]; }
p_disk()    { DETAIL="$DISK_PCT%"; [ "$DISK_PCT" -lt 80 ]; }
p_disk95()  { DETAIL="$DISK_PCT%"; [ "$DISK_PCT" -lt 95 ]; }
p_mem()     { DETAIL="$MEM_PCT%"; [ "$MEM_PCT" -lt 92 ]; }

# ── texts: fail_* = the alert, ok_* = one line, the resolved message adds the timeline itself ─────
fail_xhttp()   { msg "🔴 <b>$NODE · основной путь не работает</b>" "XHTTP :443 не отвечает ($DETAIL) уже $MIN мин." "$FIXNOTE" "👥 Amnezia у всех не подключится. ClashMi сам уйдёт на резервный путь." "→ <code>journalctl -u xray -n 50</code>"; }
ok_xhttp()     { echo "🟢 <b>$NODE · основной путь снова работает</b>"; }
fail_tcp()     { msg "🟠 <b>$NODE · резервный путь не работает</b>" "TCP :$TCP_PORT не отдаёт сертификат $FALLBACK_SNI уже $MIN мин." "$FIXNOTE" "👥 Основной путь работает, люди не заметят."; }
ok_tcp()       { echo "🟢 <b>$NODE · резервный путь снова работает</b>"; }
fail_awg()     { msg "🟠 <b>$NODE · AmneziaWG не работает</b>" "Интерфейс awg0 не поднят уже $MIN мин." "$FIXNOTE" "👥 Кто на AmneziaWG — переключись на основной ключ или ClashMi."; }
ok_awg()       { echo "🟢 <b>$NODE · AmneziaWG снова работает</b>"; }
fail_nginx()   { msg "🟠 <b>$NODE · сайт-прикрытие не работает</b>" "nginx остановлен уже $MIN мин, REALITY на :443 начнёт отваливаться." "$FIXNOTE"; }
ok_nginx()     { echo "🟢 <b>$NODE · сайт-прикрытие снова работает</b>"; }
fail_egress4() { msg "🔴 <b>$NODE · нет интернета с сервера</b>" "IPv4 не выходит в сеть уже $MIN мин." "👥 Не работает ничего. Вход через Россию уйдёт на запасной выход, если он есть." "→ панель Timeweb: состояние сервера и сети."; }
ok_egress4()   { echo "🟢 <b>$NODE · интернет вернулся</b>"; }
fail_egress6() { msg "🟡 <b>$NODE · IPv6 не выходит в сеть</b>" "Не критично: люди ходят по IPv4."; }
ok_egress6()   { echo "🟢 <b>$NODE · IPv6 снова работает</b>"; }
fail_cert14()  { msg "🟡 <b>$NODE · сертификат истекает через $CERT_DAYS дн.</b>" "certbot не продлил. Проверь: $DOMAIN → ${SERVER_IP:-этот сервер} без прокси, порт 80 открыт."; }
ok_cert14()    { echo "🟢 <b>$NODE · сертификат продлён до $(rudate "$CERT_END")</b>"; }
fail_cert3()   { msg "🔴 <b>$NODE · основной путь сломается через $CERT_DAYS дн.</b>" "Сертификат $DOMAIN не продлевается." "→ <code>certbot renew</code> руками и смотреть ошибку."; }
ok_cert3()     { echo "🟢 <b>$NODE · сертификат продлён до $(rudate "$CERT_END")</b>"; }
fail_selfsigned() { msg "🟡 <b>$NODE · работаем на самоподписанном сертификате</b>" "Подписка ClashMi и часть XHTTP-клиентов не заработают." "→ направь DNS $DOMAIN на ${SERVER_IP:-этот сервер}; install.sh повторяет попытку сам каждые 10 мин."; }
ok_selfsigned() { echo "🟢 <b>$NODE · получен сертификат Let's Encrypt</b>"; }
fail_disk()    { msg "🟡 <b>$NODE · диск заполнен на $DISK_PCT%</b>" "Свободно $DISK_FREE." "→ <code>journalctl --vacuum-size=200M</code>, <code>apt clean</code>"; }
ok_disk()      { echo "🟢 <b>$NODE · диск освободился, занято $DISK_PCT%</b>"; }
fail_disk95()  { msg "🔴 <b>$NODE · диск почти полон: $DISK_PCT%</b>" "Свободно $DISK_FREE, скоро перестанут писаться логи и подписки." "→ <code>journalctl --vacuum-size=200M</code>, <code>apt clean</code>, <code>du -xsh /var/* | sort -h</code>"; }
ok_disk95()    { echo "🟢 <b>$NODE · диск освободился, занято $DISK_PCT%</b>"; }
fail_mem()     { msg "🟠 <b>$NODE · память почти закончилась</b>" "Свободно $MEM_FREE из $MEM_TOTAL, xray может убить OOM." "→ <code>systemctl restart xray</code>, если станет хуже."; }
ok_mem()       { echo "🟢 <b>$NODE · память в норме, занято $MEM_PCT%</b>"; }
fail_ru()      { msg "🟠 <b>Москва · вход через Россию не работает</b>" ":443 на $RU_HOST не отвечает ($DETAIL) уже $MIN мин." "$FIXNOTE" "👥 Кто на ключе «Россия» — переключись на Амстердам напрямую. ClashMi переключится сам."; }
ok_ru()        { echo "🟢 <b>Москва · вход через Россию снова работает</b>"; }
fail_exit2()   { msg "🟡 <b>Запасной выход не отвечает</b>" "$EXIT2_DOMAIN :443 ($DETAIL) уже $MIN мин." "$FIXNOTE" "Основной работает, резерва сейчас нет."; }
ok_exit2()     { echo "🟢 <b>Запасной выход снова в строю</b>"; }

# ── run ──────────────────────────────────────────────────────────────────────────────────────────
flush_spool
check xhttp   crit p_xhttp   "systemctl restart xray" xray
check tcp     warn p_tcp     "systemctl restart xray" xray
check awg     warn p_awg     "systemctl restart awg-quick@awg0" awg-quick@awg0
check nginx   warn p_nginx   "systemctl restart nginx" nginx
check egress4 crit p_egress4
[ -n "${SERVER_IP6:-}" ] && check egress6 info p_egress6
check cert14  info p_cert14
check cert3   crit p_cert3
check selfsigned info p_selfsigned
check disk    info p_disk
check disk95  crit p_disk95
check mem     warn p_mem
[ -n "${RU_HOST:-}" ]   && check ru    warn p_ru    "ssh -o BatchMode=yes -o ConnectTimeout=10 root@$RU_HOST systemctl restart xray" "xray на Москве"
[ -n "${EXIT2_HOST:-}" ] && check exit2 info p_exit2 "ssh -o BatchMode=yes -o ConnectTimeout=10 root@$EXIT2_HOST systemctl restart xray" "xray на запасном"

# popular sites from here — grouped, alert only when the set is stable for 2 runs; recovery edits the alert
site_name() { case $1 in www.youtube.com) echo YouTube;; www.google.com) echo Google;; www.instagram.com) echo Instagram;; web.telegram.org) echo Telegram;;
  chatgpt.com) echo ChatGPT;; x.com) echo X;; www.facebook.com) echo Facebook;; discord.com) echo Discord;; www.tiktok.com) echo TikTok;;
  web.whatsapp.com) echo WhatsApp;; github.com) echo GitHub;; www.netflix.com) echo Netflix;; *) echo "$1";; esac; }
names() { local o="" s; for s in "$@"; do o="$o, $(site_name "$s")"; done; echo "${o#, }"; }
tmpd=$(mktemp -d)
for s in $SERVICES; do ( c=$(curl -s -o /dev/null -m 8 -w '%{http_code}' "https://$s/" || true); [ "$c" = 000 ] && echo "$s" > "$tmpd/$s" ) & done; wait
DOWN=$(ls "$tmpd" 2>/dev/null | sort | tr '\n' ' ' | sed 's/ $//'); rm -rf "$tmpd"
TOTAL=$(echo $SERVICES | wc -w); NDOWN=$(echo $DOWN | wc -w)
last=$(cat "$LIB/services.last" 2>/dev/null || true); alerted=$(cat "$LIB/services.alerted" 2>/dev/null || true)
echo "$DOWN" > "$LIB/services.last"
if [ "$DOWN" = "$last" ] && [ "$DOWN" != "$alerted" ]; then
  if [ -n "$DOWN" ]; then
    [ -n "$alerted" ] || echo "$NOW" > "$LIB/services.since"
    mid=$(tg 0 "$(msg "🟠 <b>$NODE · не открываются $(plural "$NDOWN" сайт сайта сайтов) из $TOTAL</b>" "$(names $DOWN) не отвечают с нашего IP два прогона подряд." "Если держится часами — IP в чёрном списке сервиса: /newip.")")
    [ -n "$mid" ] && echo "$mid" > "$LIB/services.msgid"; logl "SITES DOWN $DOWN"
  else
    since=$(cat "$LIB/services.since" 2>/dev/null || echo "$NOW")
    text=$(msg "🟢 <b>$NODE · все $TOTAL сайтов снова открываются</b>" "$(timeline "$since")" "<i>Не открывались: $(names $alerted)</i>")
    tg_edit "$(cat "$LIB/services.msgid" 2>/dev/null)" "$text" || tg 1 "$text" >/dev/null
    rm -f "$LIB/services.msgid"; incident warn sites "$since"; logl "SITES OK"
  fi
  echo "$DOWN" > "$LIB/services.alerted"
fi

# reboot notice (🔵, silent) — once per boot, not on the very first run after install
BOOT_ID=$(cat /proc/sys/kernel/random/boot_id)
if [ "$(cat "$LIB/boot.id" 2>/dev/null)" != "$BOOT_ID" ]; then
  [ -f "$LIB/boot.id" ] && REBOOTED=1; echo "$BOOT_ID" > "$LIB/boot.id"
fi
if [ "${REBOOTED:-0}" = 1 ]; then
  up=""; for k in xhttp:основной tcp:резерв awg:AmneziaWG nginx:nginx; do up="$up ${k#*:} $( [ "${OK[${k%%:*}]:-0}" = 1 ] && echo ✓ || echo ✗)"; done
  tg 1 "$(msg "🔵 <b>$NODE · сервер перезагрузился</b>" "Аптайм $(dur "$UP"). Поднялось:$up")" >/dev/null; logl "REBOOT detected"
fi

# snapshot for the bot's /status
[ -f "$LIB/incidents.log" ] && awk -F'\t' -v t=$((NOW-30*86400)) '$2>t' "$LIB/incidents.log" > "$LIB/incidents.tmp" && mv "$LIB/incidents.tmp" "$LIB/incidents.log"
INC24=$(awk -F'\t' -v t=$((NOW-86400)) '$2>t' "$LIB/incidents.log" 2>/dev/null | wc -l)
{ for k in "${!OK[@]}"; do printf '%s\t%s\t%s\t%s\n' "$k" "${OK[$k]}" "${DETAILS[$k]}" "${TITLE[$k]:-$k}"; done; } | jq -R -s \
  --arg ts "$NOW" --arg node "$NODE" --arg flag "$FLAG" --arg ip "${SERVER_IP:-}" --arg ip6 "${SERVER_IP6:-}" --arg domain "${DOMAIN:-}" \
  --arg ru "${RU_HOST:-}" --arg x2 "${EXIT2_HOST:-}" --arg x2dom "${EXIT2_DOMAIN:-}" --arg tcp "$TCP_PORT" \
  --argjson cert_days "$CERT_DAYS" --arg cert_issuer "$CERT_ISSUER" --arg cert_end "$CERT_END" \
  --argjson disk "$DISK_PCT" --argjson mem "$MEM_PCT" --argjson uptime "$UP" --arg down "$DOWN" --argjson total "$TOTAL" \
  --argjson inc "$INC24" --arg failing "$(awk -F'\t' '$4==1 && $1!~/^flap_/{print $1":"$2}' "$STATE" | tr '\n' ' ')" '
  {ts:($ts|tonumber), node:$node, flag:$flag, ip:$ip, ip6:$ip6, domain:$domain, tcp_port:($tcp|tonumber), ru_host:$ru, exit2_host:$x2, exit2_domain:$x2dom,
   checks:(split("\n")|map(select(length>0)|split("\t")|{(.[0]):{ok:(.[1]=="1"),detail:.[2],title:.[3]}})|add // {}),
   failing:($failing|split(" ")|map(select(length>0)|split(":")|{key:.[0],since:(.[1]|tonumber)})),
   cert:{days:$cert_days, issuer:$cert_issuer, end:$cert_end}, disk_pct:$disk, mem_pct:$mem, uptime_s:$uptime,
   sites:{total:$total, down:($down|split(" ")|map(select(length>0)))}, incidents_24h:$inc}' \
  > "$LIB/health.json.tmp" 2>/dev/null && mv "$LIB/health.json.tmp" "$LIB/health.json"

# ☀️ daily digest at 09:00 MSK — everything that did not deserve a sound goes here
online24() {
  { journalctl -u xray --since -24h -o cat 2>/dev/null | grep -oP 'email: \K\S+'
    local -A m; local f pub name; for f in "$ETC"/peers/*.env; do [ -f "$f" ] || continue; pub=$(sed -n 's/^PEER_PUB=//p' "$f"); name=$(sed -n 's/^PEER_NAME=//p' "$f"); m[$pub]=$name; done
    awg show awg0 latest-handshakes 2>/dev/null | while read -r pub ts; do [ "${ts:-0}" -gt $((NOW-86400)) ] 2>/dev/null && echo "${m[$pub]:-}"; done
  } | grep . | sort -u | paste -sd, | sed 's/,/, /g'
}
digest() {
  local inc n lines=() s e sev key title f a _ nodes online
  inc=$(awk -F'\t' -v t=$((NOW-86400)) '$2>t' "$LIB/incidents.log" 2>/dev/null || true); n=$(printf '%s' "$inc" | grep -c . || true)
  if [ "$n" = 0 ] && [ -z "$(failing_keys)" ]; then lines+=("☀️ <b>$BRAND</b> · за сутки без инцидентов")
  else
    lines+=("☀️ <b>$BRAND</b> · $(plural "$n" инцидент инцидента инцидентов) за сутки")
    while IFS=$'\t' read -r s e sev key title; do [ -n "$s" ] || continue; lines+=("$(emo "$sev") $(when "$s") $title, $(dur $((e-s)))"); done <<<"$inc"
    while IFS=$'\t' read -r key s f a _ _; do [ "$a" = 1 ] || continue; case $key in flap_*) continue;; esac; lines+=("⏳ ${TITLE[$key]:-$key} — всё ещё не работает, с $(when "$s")"); done < "$STATE"
  fi
  nodes="$FLAG $NODE"; [ -n "${RU_HOST:-}" ] && nodes="$nodes · 🇷🇺 Москва"; [ -n "${EXIT2_HOST:-}" ] && nodes="$nodes · 🛟 Запасной"
  lines+=("$nodes — $( [ -z "$(failing_keys)" ] && echo 'в порядке' || echo 'есть проблемы, см. /status')")
  lines+=("🌐 $((TOTAL-NDOWN)) из $TOTAL сайтов · 🔒 сертификат $CERT_DAYS дн. · 💾 диск $DISK_PCT%")
  online=$(online24); [ -n "$online" ] && lines+=("👥 Были в сети: $online")
  tg 1 "$(printf '%s\n' "${lines[@]}")" >/dev/null; logl "DIGEST sent"
}
if [ "$(TZ=Europe/Moscow date +%H)" = 09 ] && [ "$(cat "$LIB/digest.date" 2>/dev/null)" != "$TODAY" ]; then echo "$TODAY" > "$LIB/digest.date"; digest; fi

logl "checked, failing=$(failing_keys | wc -l)${DOWN:+, sites down: $DOWN}"
tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
