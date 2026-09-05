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
NODE=${NODE_NAME:-Амстердам}; FLAG=${NODE_FLAG:-🇳🇱}; EXIT2_NAME=${EXIT2_NAME:-Запасной}
# genitive forms, so an alert reads "не открывается с Амстердама" and not "с Амстердам"
NODE_GEN=${NODE_GEN:-Амстердама}; EXIT2_GEN=${EXIT2_GEN:-Запасного}
# Exactly one node speaks for the Dutch pair. Two exits running the same probes sent two of every alert
# and two morning digests, and both would try to bind the chat through getUpdates and collide (409).
# The standby runs with HEALTH_TG=0: it still probes, still self-heals, still writes health.json, and
# this node reads that file over ssh and speaks for both. Moscow keeps its own voice on purpose — it is
# the only place that can see the exit blocked FROM Russia, which is a different fact, not a copy.
HEALTH_TG=${HEALTH_TG:-1}
declare -A ALSO; X2_DOWN=""; X2_DISK=""; PREVIEW=""
[ "${1:-}" = "--preview" ] && { PREVIEW=${2:-}; [ -n "$PREVIEW" ] || { echo "usage: $0 --preview <chat_id>" >&2; exit 2; }; }
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
verb() { local m=$(( $1 % 10 )) h=$(( $1 % 100 )); if [ "$m" = 1 ] && [ "$h" != 11 ]; then echo "$2"; else echo "$3"; fi; }
plural() { local n=$1 one=$2 few=$3 many=$4 m=$(( $1 % 10 )) h=$(( $1 % 100 ))
  if [ "$m" = 1 ] && [ "$h" != 11 ]; then echo "$n $one"; elif [ "$m" -ge 2 ] && [ "$m" -le 4 ] && { [ "$h" -lt 12 ] || [ "$h" -gt 14 ]; }; then echo "$n $few"; else echo "$n $many"; fi; }

# ── Telegram ─────────────────────────────────────────────────────────────────────────────────────
tg_chat() { # chat id: TG_CHAT_ID, else /etc/safechill/tg_chat_id (the bot writes it on the first admin message)
  [ -n "$PREVIEW" ] && { echo "$PREVIEW"; return 0; }
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
  [ "$HEALTH_TG" = 1 ] || return 0                        # the standby is mute; this node speaks for it
  [ -n "${TG_BOT_TOKEN:-}" ] && [ -n "$2" ] || return 0
  local id mid; id=$(tg_chat) || return 0
  if mid=$(tg_raw "$id" "$1" "$2"); then echo "$mid"
  else printf '%s\t%s\t%s\t%s\n' "$NOW" "$1" "$(printf %s "$2" | base64 -w0)" "${3:-}" >> "$LIB/tg.spool"; fi
}
tg_edit() { # <message_id> <html> — rewrite an OPEN alert in place: resolved, or a second node joining it
  [ "$HEALTH_TG" = 1 ] || return 0
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

# ── state: key ⇥ since ⇥ fails ⇥ alerted ⇥ msgid ⇥ what-was-wrong ⇥ nodes named in the open alert ──
if [ -s "$STATE" ] && ! grep -q $'\t' "$STATE"; then awk -v n="$NOW" '{print $1"\t"n"\t2\t1\t-\t-\t-"}' "$STATE" > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"; fi
st_get() { awk -F'\t' -v k="$1" '$1==k{print $2"\t"$3"\t"$4"\t"$5"\t"$6"\t"$7; exit}' "$STATE"; }
# "-" stands for an empty msgid / description: `read` with a tab IFS collapses empty fields and would shift them
st_put() { awk -F'\t' -v k="$1" '$1!=k' "$STATE" > "$STATE.tmp"; printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "${5:--}" "${6:--}" "${7:--}" >> "$STATE.tmp"; mv "$STATE.tmp" "$STATE"; }
st_del() { awk -F'\t' -v k="$1" '$1!=k' "$STATE" > "$STATE.tmp"; mv "$STATE.tmp" "$STATE"; }
st_setmsg() { awk -F'\t' -v OFS='\t' -v k="$1" -v m="$2" '$1==k{$5=m}1' "$STATE" > "$STATE.tmp"; mv "$STATE.tmp" "$STATE"; }
failing_keys() { awk -F'\t' '$4==1 && $1!~/^flap_/{print $1}' "$STATE"; }
incident() { printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$3" "$NOW" "$1" "$2" "${DIGEST_T[$2]:-${TITLE[$2]:-$2}}" "${4:-}" "${5:-}" >> "$LIB/incidents.log"; }

declare -A OK DETAILS
# TITLE is what /status calls a check; DIGEST_T is how the morning summary says it happened
declare -A DIGEST_T=([xhttp]="не работал основной путь" [tcp]="не работал резервный путь" [awg]="не работал AmneziaWG"
  [nginx]="лежало сайт-прикрытие" [egress4]="не было интернета" [egress6]="не было IPv6" [mem]="заканчивалась память"
  [cert14]="сертификат не продлевался" [cert3]="сертификат не продлевался" [selfsigned]="самоподписанный сертификат"
  [disk]="заканчивался диск" [disk95]="заканчивался диск" [ru]="не работал вход через Россию"
  [exit2]="не отвечал запасной выход" [sites]="не открывались сайты")
declare -A TITLE=([xhttp]="основной путь" [tcp]="резервный путь" [awg]="AmneziaWG" [nginx]="сайт-прикрытие" [egress4]="нет интернета"
  [egress6]="IPv6" [cert14]="сертификат" [cert3]="сертификат" [selfsigned]="самоподписанный сертификат" [disk]="диск" [disk95]="диск"
  [mem]="память" [ru]="вход через Россию" [exit2]="запасной выход" [sites]="сайты")

resolve() { # <key> <since> <msgid> <was> <sev> <nodes>: the SAME message becomes the resolved one
  WHO=${6:-$NODE}
  local text; text="$("ok_$1")"$'\n'"$(timeline "$2")"; [ -n "$4" ] && text+=$'\n'"<i>Было: $4</i>"
  tg_edit "$3" "$text" || tg 1 "$text" >/dev/null
  logl "OK   $1 after $(dur $((NOW-$2)))"; incident "$5" "$1" "$2" "$WHO"
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
  # probe-fn sets DETAIL and returns 0/1; texts come from fail_<key> (uses $WHO $DETAIL $FIXNOTE $MIN) and ok_<key>.
  # An incident is one PROBLEM, not one server: it stays open while EITHER exit has this key failing, $WHO names
  # whoever has it right now, and when the second exit joins or leaves, the message already sent is edited in
  # place — never a second one. OK[] stays this node's own answer, because health.json describes this node.
  local key=$1 sev=$2 probe=$3 fix=${4:-} label=${5:-} since fails alerted msgid was nodes healed=0 who
  IFS=$'\t' read -r since fails alerted msgid was nodes <<<"$(st_get "$key")"
  since=${since:-0}; fails=${fails:-0}; alerted=${alerted:-0}
  [ "${msgid:-}" = "-" ] && msgid=""; [ "${was:-}" = "-" ] && was=""; [ "${nodes:-}" = "-" ] && nodes=""
  DETAIL=""; FIXNOTE=""
  if $probe; then healed=2
  elif [ -n "$fix" ]; then eval "$fix" >/dev/null 2>&1; sleep 5; $probe && healed=1; fi
  if [ "$healed" -gt 0 ]; then OK[$key]=1; else OK[$key]=0; fi
  DETAILS[$key]=$DETAIL
  [ "$healed" = 1 ] && [ "$fails" = 0 ] && selfheal "$key" "$label"
  who=""; [ "$healed" = 0 ] && who="$NODE"
  [ -n "${ALSO[$key]:-}" ] && who="${who:+$who и }${ALSO[$key]}"
  if [ -z "$who" ]; then
    if [ "$fails" -gt 0 ]; then
      [ "$alerted" = 1 ] && resolve "$key" "$since" "${msgid:-}" "${was:-}" "$sev" "${nodes:-$NODE}"
      st_del "$key"
    fi
    return 0
  fi
  [ "$healed" = 0 ] && [ -n "$fix" ] && FIXNOTE="Перезапустил $label — не помогло."
  fails=$((fails+1)); [ "$since" -gt 0 ] || since=$NOW
  WHO=$who; MIN=$(( (NOW-since)/60 )); [ "$MIN" -ge 1 ] || MIN=1
  if [ "$alerted" = 0 ] && [ "$fails" -ge 2 ]; then
    msgid=$(tg "$(loud "$sev")" "$("fail_$key")"$'\n'"$RES" "$key"); alerted=1; was="$DETAIL${FIXNOTE:+ · $FIXNOTE}"
    logl "FAIL $key ($who) $DETAIL"
  elif [ "$alerted" = 1 ] && [ "$who" != "$nodes" ]; then
    tg_edit "${msgid:-}" "$("fail_$key")"$'\n'"$RES" && logl "EDIT $key -> $who"
  fi
  st_put "$key" "$since" "$fails" "$alerted" "${msgid:-}" "${was:-}" "$who"
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
# every alert carries this line: the first question about a red message is always "was the box busy?"
CPU_PCT=$(awk -v l="$(cut -d' ' -f1 /proc/loadavg)" -v n="$(nproc)" 'BEGIN{printf "%d", l*100/n}')
RES="⚙️ CPU $CPU_PCT% · память $MEM_PCT% · диск $DISK_PCT% · аптайм $(dur "$UP")"
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
fail_xhttp()   { msg "🔴 <b>$WHO · основной путь не работает</b>" "🔌 XHTTP :443 не отвечает ($DETAIL) уже $MIN мин." "${FIXNOTE:+🔧 $FIXNOTE}" "👥 Amnezia у всех не подключится. ClashMi сам уйдёт на резервный путь." "→ <code>journalctl -u xray -n 50</code>"; }
ok_xhttp()     { echo "🟢 <b>$WHO · основной путь снова работает</b>"; }
fail_tcp()     { msg "🟠 <b>$WHO · резервный путь не работает</b>" "🔌 TCP :$TCP_PORT не отдаёт сертификат $FALLBACK_SNI уже $MIN мин." "${FIXNOTE:+🔧 $FIXNOTE}" "👥 Основной путь работает, люди не заметят."; }
ok_tcp()       { echo "🟢 <b>$WHO · резервный путь снова работает</b>"; }
fail_awg()     { msg "🟠 <b>$WHO · AmneziaWG не работает</b>" "🔌 Интерфейс awg0 не поднят уже $MIN мин." "${FIXNOTE:+🔧 $FIXNOTE}" "👥 Кто на AmneziaWG — переключись на основной ключ или ClashMi."; }
ok_awg()       { echo "🟢 <b>$WHO · AmneziaWG снова работает</b>"; }
fail_nginx()   { msg "🟠 <b>$WHO · сайт-прикрытие не работает</b>" "🔌 nginx остановлен уже $MIN мин, REALITY на :443 начнёт отваливаться." "${FIXNOTE:+🔧 $FIXNOTE}"; }
ok_nginx()     { echo "🟢 <b>$WHO · сайт-прикрытие снова работает</b>"; }
fail_egress4() { msg "🔴 <b>$WHO · нет интернета с сервера</b>" "🌐 IPv4 не выходит в сеть уже $MIN мин." "👥 Не работает ничего. Вход через Россию уйдёт на запасной выход, если он есть." "→ панель Timeweb: состояние сервера и сети."; }
ok_egress4()   { echo "🟢 <b>$WHO · интернет вернулся</b>"; }
fail_egress6() { msg "🟡 <b>$WHO · IPv6 не выходит в сеть</b>" "🌐 Не критично: люди ходят по IPv4."; }
ok_egress6()   { echo "🟢 <b>$WHO · IPv6 снова работает</b>"; }
fail_cert14()  { msg "🟡 <b>$WHO · сертификат истекает через $CERT_DAYS дн.</b>" "🔒 certbot не продлил. Проверь: $DOMAIN → ${SERVER_IP:-этот сервер} без прокси, порт 80 открыт."; }
ok_cert14()    { echo "🟢 <b>$WHO · сертификат продлён до $(rudate "$CERT_END")</b>"; }
fail_cert3()   { msg "🔴 <b>$WHO · основной путь сломается через $CERT_DAYS дн.</b>" "🔒 Сертификат $DOMAIN не продлевается." "→ <code>certbot renew</code> руками и смотреть ошибку."; }
ok_cert3()     { echo "🟢 <b>$WHO · сертификат продлён до $(rudate "$CERT_END")</b>"; }
fail_selfsigned() { msg "🟡 <b>$WHO · работаем на самоподписанном сертификате</b>" "🔒 Подписка ClashMi и часть XHTTP-клиентов не заработают." "→ направь DNS $DOMAIN на ${SERVER_IP:-этот сервер}; install.sh повторяет попытку сам каждые 10 мин."; }
ok_selfsigned() { echo "🟢 <b>$WHO · получен сертификат Let's Encrypt</b>"; }
fail_disk()    { msg "🟡 <b>$WHO · диск заполнен на $DISK_PCT%</b>" "💾 Свободно $DISK_FREE." "→ <code>journalctl --vacuum-size=200M</code>, <code>apt clean</code>"; }
ok_disk()      { echo "🟢 <b>$WHO · диск освободился, занято $DISK_PCT%</b>"; }
fail_disk95()  { msg "🔴 <b>$WHO · диск почти полон: $DISK_PCT%</b>" "💾 Свободно $DISK_FREE, скоро перестанут писаться логи и подписки." "→ <code>journalctl --vacuum-size=200M</code>, <code>apt clean</code>, <code>du -xsh /var/* | sort -h</code>"; }
ok_disk95()    { echo "🟢 <b>$WHO · диск освободился, занято $DISK_PCT%</b>"; }
fail_mem()     { msg "🟠 <b>$WHO · память почти закончилась</b>" "🧠 Свободно $MEM_FREE из $MEM_TOTAL, xray может убить OOM." "→ <code>systemctl restart xray</code>, если станет хуже."; }
ok_mem()       { echo "🟢 <b>$WHO · память в норме, занято $MEM_PCT%</b>"; }
fail_ru()      { msg "🟠 <b>Москва · вход через Россию не работает</b>" "🔌 :443 на $RU_HOST не отвечает ($DETAIL) уже $MIN мин." "${FIXNOTE:+🔧 $FIXNOTE}" "👥 Кто на ключе «Россия» — переключись на Амстердам напрямую. ClashMi переключится сам."; }
ok_ru()        { echo "🟢 <b>Москва · вход через Россию снова работает</b>"; }
fail_exit2()   { msg "🟡 <b>Запасной выход не отвечает</b>" "🔌 $EXIT2_DOMAIN :443 ($DETAIL) уже $MIN мин." "${FIXNOTE:+🔧 $FIXNOTE}" "Основной работает, резерва сейчас нет."; }
ok_exit2()     { echo "🟢 <b>Запасной выход снова в строю</b>"; }

# ── what the standby found about itself ─────────────────────────────────────────────────────────
# It is mute, so its health.json is how it reports. Anything failing there joins this node's incident for
# the same key. "ru" and "exit2" are skipped: those name a specific OTHER node, not the pair, and the
# standby's copy of them would put the wrong name in the text.
if [ -n "${EXIT2_HOST:-}" ] && [ "$HEALTH_TG" = 1 ] && [ -z "$PREVIEW" ]; then
  x2=$(ssh -o BatchMode=yes -o ConnectTimeout=8 "root@$EXIT2_HOST" cat "$LIB/health.json" 2>/dev/null || true)
  if [ -n "$x2" ] && jq -e . >/dev/null 2>&1 <<<"$x2"; then
    x2ts=$(jq -r '.ts // 0' <<<"$x2")
    # a stale file means the standby stopped running; the exit2 probe is what reports that, not this
    if [ $((NOW - x2ts)) -lt 600 ]; then
      X2_DISK=$(jq -r '.disk_pct // empty' <<<"$x2")
      X2_DOWN=$(jq -r '(.sites.down // []) | join(" ")' <<<"$x2")
      for k in $(jq -r '.checks | to_entries[] | select(.value.ok == false) | .key' <<<"$x2"); do
        case $k in ru|exit2) continue;; esac; ALSO[$k]="$EXIT2_NAME"
      done
    fi
  fi
fi

# ── renderers, defined above the run so --preview can reach them ─────────────────────────────────
site_name() { case $1 in www.youtube.com) echo YouTube;; www.google.com) echo Google;; www.instagram.com) echo Instagram;; web.telegram.org) echo Telegram;;
  chatgpt.com) echo ChatGPT;; x.com) echo X;; www.facebook.com) echo Facebook;; discord.com) echo Discord;; www.tiktok.com) echo TikTok;;
  web.whatsapp.com) echo WhatsApp;; github.com) echo GitHub;; www.netflix.com) echo Netflix;; *) echo "$1";; esac; }
names() { local o="" s; for s in "$@"; do o="$o, $(site_name "$s")"; done; echo "${o#, }"; }
sites_who() {  # which exit could not open what: one incident naming both addresses, never two alerts
  if [ -n "$DOWN" ] && [ "$DOWN" = "$X2_DOWN" ]; then echo "🌐 Не открывается ни с $NODE_GEN, ни с $EXIT2_GEN: $(names $DOWN)."
  elif [ -n "$DOWN" ] && [ -n "$X2_DOWN" ]; then echo "🌐 С $NODE_GEN: $(names $DOWN). С $EXIT2_GEN: $(names $X2_DOWN)."
  elif [ -n "$DOWN" ]; then echo "🌐 Не открывается с $NODE_GEN: $(names $DOWN)."
  else echo "🌐 Не открывается с $EXIT2_GEN: $(names $X2_DOWN)."; fi
}
sites_nodes() { local w=""; [ -n "$DOWN" ] && w="$NODE"; [ -n "$X2_DOWN" ] && w="${w:+$w и }$EXIT2_NAME"; echo "$w"; }
sites_text() { msg "🟠 <b>$(verb "$NDOWN" "не открывается" "не открываются") $(plural "$NDOWN" сайт сайта сайтов) из $TOTAL</b>" \
                   "$(sites_who)" "Держится два прогона подряд. Если часами — адрес в чёрном списке сервиса: /newip."; }

online24() {
  { journalctl -u xray --since -24h -o cat 2>/dev/null | grep -oP 'email: \K\S+'
    local -A m; local f pub name; for f in "$ETC"/peers/*.env; do [ -f "$f" ] || continue; pub=$(sed -n 's/^PEER_PUB=//p' "$f"); name=$(sed -n 's/^PEER_NAME=//p' "$f"); m[$pub]=$name; done
    awg show awg0 latest-handshakes 2>/dev/null | while read -r pub ts; do [ "${ts:-0}" -gt $((NOW-86400)) ] 2>/dev/null && echo "${m[$pub]:-}"; done
  } | grep . | grep -vx relay-ru | sort -u | paste -sd, | sed 's/,/, /g'
}

digest() {  # ONE silent message for every node: whatever did not deserve a sound during the day lands here
  local inc n lines=() s e sev key title inodes f a _ nodes online facts
  inc=$(awk -F'\t' -v t=$((NOW-86400)) '$2>t' "$LIB/incidents.log" 2>/dev/null || true); n=$(printf '%s' "$inc" | grep -c . || true)
  if [ "$n" = 0 ] && [ -z "$(failing_keys)" ]; then lines+=("☀️ <b>$BRAND</b> · сутки без происшествий")
  else
    lines+=("☀️ <b>$BRAND</b> · $(plural "$n" инцидент инцидента инцидентов) за сутки")
    while IFS=$'\t' read -r s e sev key title inodes idet; do [ -n "$s" ] || continue
      lines+=("$(emo "$sev") $(when "$s") · ${DIGEST_T[$key]:-$title}${idet:+ ($idet)}, $(dur $((e-s)))${inodes:+ · $inodes}"); done <<<"$inc"
    while IFS=$'\t' read -r key s f a _ _ nodes; do [ "$a" = 1 ] || continue; case $key in flap_*) continue;; esac
      lines+=("⏳ ${TITLE[$key]:-$key} не работает с $(when "$s")${nodes:+ · $nodes}"); done < "$STATE"
  fi
  nodes="$FLAG $NODE"; [ -n "${EXIT2_HOST:-}" ] && nodes="$nodes · 🛟 $EXIT2_NAME"; [ -n "${RU_HOST:-}" ] && nodes="$nodes · 🇷🇺 Москва"
  lines+=("$nodes — $( [ -z "$(failing_keys)" ] && echo 'в порядке' || echo 'см. /status')")
  facts="🌐 $((TOTAL-NDOWN))/$TOTAL · 🔒 $CERT_DAYS дн. · 💾 $DISK_PCT%"; [ -n "$X2_DISK" ] && facts="$facts и $X2_DISK%"
  lines+=("$facts")
  online=$(online24); [ -n "$online" ] && lines+=("👥 $online")
  tg 1 "$(printf '%s\n' "${lines[@]}")" >/dev/null; logl "DIGEST sent"
}

# ── --preview <chat>: every shape a message can take, with made-up data, into one chat ───────────
# There is no way to rehearse an outage on a live server, and wording is the whole point of these
# messages, so this renders them on demand instead.
preview_send() {
  # Sends TWO messages and then rewrites each of them three times, which is what an incident actually does:
  # one message appears, gains the second node, loses it, and finally turns into the resolved line. Showing
  # each shape as its own message (the first version of this) read as if the alerts were duplicating.
  local t=$'\n'"<i>— тест, ничего не сломалось</i>" a b pause=7
  TOTAL=$(echo $SERVICES | wc -w); MIN=2; DETAIL="HTTP 000"; FIXNOTE=""
  WHO="$NODE"; a=$(tg 1 "$(fail_xhttp)"$'\n'"$RES$t")
  DOWN="www.netflix.com"; X2_DOWN=""; NDOWN=1; b=$(tg 1 "$(sites_text)$t")
  sleep $pause                                   # the standby joins both incidents
  MIN=4; FIXNOTE="Перезапустил xray — не помогло."; WHO="$NODE и $EXIT2_NAME"
  tg_edit "$a" "$(fail_xhttp)"$'\n'"$RES$t"
  X2_DOWN="www.netflix.com"; tg_edit "$b" "$(sites_text)$t"
  sleep $pause                                   # the two exits now differ
  X2_DOWN="x.com"; NDOWN=2; tg_edit "$b" "$(sites_text)$t"
  sleep $pause                                   # both recover: the same two messages become the report
  tg_edit "$a" "$(ok_xhttp)"$'\n'"$(timeline $((NOW-360)))"$'\n'"<i>Было: HTTP 000 · Перезапустил xray — не помогло.</i>$t"
  tg_edit "$b" "$(msg "🟢 <b>все $TOTAL сайтов снова открываются</b>" "$(timeline $((NOW-360)))" "<i>Не открывались: Netflix, X</i>")$t"
  DOWN=""; X2_DOWN=""; NDOWN=0; X2_DISK=${X2_DISK:-20}; digest
  echo "preview -> $PREVIEW: два сообщения, каждое правится на месте, плюс дайджест"
}

# ── run ──────────────────────────────────────────────────────────────────────────────────────────
# ── AmneziaWG log ────────────────────────────────────────────────────────────────────────────────
# The kernel keeps no record of who connected over awg0 — only "latest handshake" per key, and the next
# handshake overwrites it. Each run diffs that against the previous run and writes what changed to the
# journal (journalctl -t safechill-awg): every handshake at info; a new session (5+ min of silence before
# it) or a changed address at notice; a key that no peers/*.env explains at warning — a person removed
# here who can still connect, which is exactly how three deleted people were found still valid on the standby.
awg_log() {
  local dump prev="$LIB/awg.last" next="$LIB/awg.last.tmp" pub psk ep ips hs rx tx ka name f ohs orx otx oep line pri
  # tail -n +2: the first dump line is the interface itself and starts with its PRIVATE key. It must be dropped
  # by position — AmneziaWG's interface line carries the obfuscation parameters too, so counting fields does
  # not tell it apart from a peer (that mistake once logged the first characters of the key).
  dump=$(awg show awg0 dump 2>/dev/null | tail -n +2); [ -n "$dump" ] || return 0
  local -A PNAME=(); for f in "$ETC"/peers/*.env; do [ -f "$f" ] || continue; PNAME[$(sed -n 's/^PEER_PUB=//p' "$f")]=$(sed -n 's/^PEER_NAME=//p' "$f"); done
  ( umask 077; : > "$next" )
  while read -r pub psk ep ips hs rx tx ka; do
    [ -n "$ka" ] && [ "$ka" != "off" -o "$ka" = "off" ] || continue   # a peer line has exactly 8 fields
    printf '%s\t%s\t%s\t%s\t%s\n' "$pub" "$hs" "$rx" "$tx" "$ep" >> "$next"
    [ "${hs:-0}" -gt 0 ] 2>/dev/null || continue   # never connected
    IFS=$'\t' read -r ohs orx otx oep <<<"$(awk -F'\t' -v k="$pub" '$1==k{print $2"\t"$3"\t"$4"\t"$5; exit}' "$prev" 2>/dev/null)"
    [ "$hs" != "${ohs:-}" ] || continue            # no handshake since the last run
    name=${PNAME[$pub]:-}; pri=info
    line="${name:-UNKNOWN KEY ${pub:0:12}…} handshake $(TZ=Europe/Moscow date -d "@$hs" +%H:%M:%S) from $ep"
    # the address is compared without the port: one person's several AWG clients (ClashMi keeps one per
    # server in a group) share the key and hop between source ports every minute — only a new IP is news
    if [ -z "${ohs:-}" ] || [ $((hs - ohs)) -gt 300 ]; then line="$line · new session"; pri=notice
    elif [ "${ep%:*}" != "${oep%:*}" ]; then line="$line · address changed from ${oep:-?}"; pri=notice; fi
    [ -n "${orx:-}" ] && line="$line · +$(( rx >= orx ? (rx - orx) / 1024 : rx / 1024 )) KB in, +$(( tx >= otx ? (tx - otx) / 1024 : tx / 1024 )) KB out"
    [ -n "$name" ] || pri=warning
    logger -t safechill-awg -p "daemon.$pri" -- "$line"
  done <<<"$dump"
  mv "$next" "$prev"
}

[ -n "$PREVIEW" ] && { preview_send; exit 0; }
flush_spool
check xhttp   crit p_xhttp   "systemctl restart xray" xray
check tcp     warn p_tcp     "systemctl restart xray" xray
check awg     warn p_awg     "systemctl restart awg-quick@awg0" awg-quick@awg0
awg_log
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

# popular sites from BOTH exits — one incident for the pair: alert once the set is stable for two runs,
# edit that same message when the other exit joins or drops out, edit it again into the resolved state
tmpd=$(mktemp -d)
for s in $SERVICES; do ( c=$(curl -s -o /dev/null -m 8 -w '%{http_code}' "https://$s/" || true); [ "$c" = 000 ] && echo "$s" > "$tmpd/$s" ) & done; wait
DOWN=$(ls "$tmpd" 2>/dev/null | sort | tr '\n' ' ' | sed 's/ $//'); rm -rf "$tmpd"
TOTAL=$(echo $SERVICES | wc -w)
BOTH=$(printf '%s %s' "$DOWN" "$X2_DOWN" | tr ' ' '
' | grep . | sort -u | tr '
' ' ' | sed 's/ $//')
NDOWN=$(echo $BOTH | wc -w)
last=$(cat "$LIB/services.last" 2>/dev/null || true); alerted=$(cat "$LIB/services.alerted" 2>/dev/null || true)
echo "$BOTH" > "$LIB/services.last"
if [ "$BOTH" = "$last" ] && [ "$BOTH" != "$alerted" ]; then
  if [ -n "$BOTH" ]; then
    [ -n "$alerted" ] || echo "$NOW" > "$LIB/services.since"
    if [ -n "$alerted" ] && [ -s "$LIB/services.msgid" ] && tg_edit "$(cat "$LIB/services.msgid")" "$(sites_text)"; then
      logl "SITES EDIT $BOTH"
    else
      mid=$(tg 0 "$(sites_text)"); [ -n "$mid" ] && echo "$mid" > "$LIB/services.msgid"; logl "SITES DOWN $BOTH"
    fi
    sites_nodes > "$LIB/services.who"
  else
    since=$(cat "$LIB/services.since" 2>/dev/null || echo "$NOW")
    text=$(msg "🟢 <b>все $TOTAL сайтов снова открываются</b>" "$(timeline "$since")" "<i>Не открывались: $(names $alerted)</i>")
    tg_edit "$(cat "$LIB/services.msgid" 2>/dev/null)" "$text" || tg 1 "$text" >/dev/null
    incident warn sites "$since" "$(cat "$LIB/services.who" 2>/dev/null || true)" "$(names $alerted)"
    rm -f "$LIB/services.msgid" "$LIB/services.who"; logl "SITES OK"
  fi
  echo "$BOTH" > "$LIB/services.alerted"
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
if [ "$(TZ=Europe/Moscow date +%H)" = 09 ] && [ "$(cat "$LIB/digest.date" 2>/dev/null)" != "$TODAY" ]; then echo "$TODAY" > "$LIB/digest.date"; digest; fi

logl "checked, failing=$(failing_keys | wc -l)${DOWN:+, sites down: $DOWN}"
tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
