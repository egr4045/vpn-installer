#!/usr/bin/env bash
# vpn-health-ru.sh — runs every minute ON the RU entry node (vpn-health-ru.timer).
# Its one job for Telegram: say how the exit node looks FROM RUSSIA — blocked IPv4, blocked SNI, or the exit
# being down. Nobody else can see that. Its own xray it restarts quietly (the exit node alerts about it), so
# there are no duplicate messages. Env: /etc/safechill/ru.env, pushed by the exit node's render.sh.
# Same grammar as vpn-health.sh: alert after two failed runs; on recovery the alert is EDITED into the
# resolved state (started / ended / downtime), so one incident stays one message.
set -u
ENV=/etc/safechill/ru.env; LIB=/var/lib/safechill; STATE=$LIB/health.state; LOG=/var/log/vpn-health.log
[ -f "$ENV" ] || exit 0
set -a; . "$ENV"; set +a
mkdir -p "$LIB"; touch "$STATE"
BRAND=${BRAND:-SafeChill}; NODE=${NODE_NAME:-Москва}; EXIT=${EXIT_NAME:-Амстердам}; TCP_PORT=${TCP_PORT:-8443}
NOW=$(date +%s); TODAY=$(TZ=Europe/Moscow date +%F); TG="https://api.telegram.org/bot${TG_BOT_TOKEN:-}"
MSK()  { TZ=Europe/Moscow date -d "@${1:-$NOW}" +%H:%M; }
MON=(янв фев мар апр мая июн июл авг сен окт ноя дек)
when() { local t=${1:-$NOW}; if [ "$(TZ=Europe/Moscow date -d "@$t" +%F)" = "$TODAY" ]; then MSK "$t"; else echo "$(TZ=Europe/Moscow date -d "@$t" +%-d) ${MON[$(TZ=Europe/Moscow date -d "@$t" +%-m)-1]} $(MSK "$t")"; fi; }
dur()  { local s=$1; if [ "$s" -lt 3600 ]; then echo "$(( (s+59)/60 )) мин"; elif [ "$s" -lt 86400 ]; then echo "$((s/3600)) ч $(( (s%3600)/60 )) мин"; else echo "$((s/86400)) дн. $(( (s%86400)/3600 )) ч"; fi; }
timeline() { echo "Началось $(when "$1"), закончилось $(when), простой $(dur $((NOW-$1)))."; }
logl() { echo "$(date +%FT%T) $*" >> "$LOG"; }
msg()  { printf '%s\n' "$@" | sed '/^$/d'; }
loud() { case $1 in crit|warn) echo 0;; *) echo 1;; esac; }

tg_raw() { local sil=false out mid; [ "$2" = 1 ] && sil=true
  out=$(curl -s -m 8 -f -X POST "$TG/sendMessage" -d chat_id="$1" -d parse_mode=HTML -d disable_web_page_preview=true \
        -d disable_notification=$sil --data-urlencode text="$3") || return 1
  mid=$(sed -n 's/.*"message_id":\([0-9]*\).*/\1/p' <<<"$out" | head -1); [ -n "$mid" ] || return 1; echo "$mid"; }
tg() { # tg <silent> <html> [state-key] -> prints message_id; spooled when Telegram is unreachable
  [ -n "${TG_BOT_TOKEN:-}" ] && [ -n "${TG_CHAT_ID:-}" ] && [ -n "$2" ] || return 0
  local mid; if mid=$(tg_raw "$TG_CHAT_ID" "$1" "$2"); then echo "$mid"
  else printf '%s\t%s\t%s\t%s\n' "$NOW" "$1" "$(printf %s "$2" | base64 -w0)" "${3:-}" >> "$LIB/tg.spool"; fi; }
tg_edit() { [ -n "${TG_BOT_TOKEN:-}" ] && [ -n "${TG_CHAT_ID:-}" ] && [ -n "$1" ] || return 1
  curl -s -m 8 -f -o /dev/null -X POST "$TG/editMessageText" -d chat_id="$TG_CHAT_ID" -d message_id="$1" -d parse_mode=HTML \
       -d disable_web_page_preview=true --data-urlencode text="$2"; }
flush_spool() {
  [ -s "$LIB/tg.spool" ] && [ -n "${TG_BOT_TOKEN:-}" ] && [ -n "${TG_CHAT_ID:-}" ] || return 0
  local tmp ts sil b64 key text mid; tmp=$(mktemp); mv "$LIB/tg.spool" "$tmp"
  while IFS=$'\t' read -r ts sil b64 key; do
    text="$(base64 -d <<<"$b64")"$'\n'"<i>задержано, событие в $(when "$ts")</i>"
    if mid=$(tg_raw "$TG_CHAT_ID" "$sil" "$text"); then [ -n "$key" ] && st_setmsg "$key" "$mid"
    else printf '%s\t%s\t%s\t%s\n' "$ts" "$sil" "$b64" "$key" >> "$LIB/tg.spool"; cat >> "$LIB/tg.spool"; break; fi
  done < "$tmp"; rm -f "$tmp"
}

# state: key ⇥ since ⇥ fails ⇥ alerted ⇥ msgid ⇥ what-was-wrong
if [ -s "$STATE" ] && ! grep -q $'\t' "$STATE"; then awk -v n="$NOW" '{print $1"\t"n"\t2\t1\t-\t-"}' "$STATE" > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"; fi
st_get() { awk -F'\t' -v k="$1" '$1==k{print $2"\t"$3"\t"$4"\t"$5"\t"$6; exit}' "$STATE"; }
# "-" stands for an empty msgid / description: `read` with a tab IFS collapses empty fields and would shift them
st_put() { awk -F'\t' -v k="$1" '$1!=k' "$STATE" > "$STATE.tmp"; printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "${5:--}" "${6:--}" >> "$STATE.tmp"; mv "$STATE.tmp" "$STATE"; }
st_del() { awk -F'\t' -v k="$1" '$1!=k' "$STATE" > "$STATE.tmp"; mv "$STATE.tmp" "$STATE"; }
st_setmsg() { awk -F'\t' -v OFS='\t' -v k="$1" -v m="$2" '$1==k{$5=m}1' "$STATE" > "$STATE.tmp"; mv "$STATE.tmp" "$STATE"; }

check() { # check <key> <crit|warn|info> <probe-fn>; $WAS = how to describe the outage in the resolved message
  local key=$1 sev=$2 probe=$3 since fails alerted msgid was text
  IFS=$'\t' read -r since fails alerted msgid was <<<"$(st_get "$key")"; since=${since:-0}; fails=${fails:-0}; alerted=${alerted:-0}
  [ "${msgid:-}" = "-" ] && msgid=""; [ "${was:-}" = "-" ] && was=""
  if $probe; then
    if [ "$fails" -gt 0 ]; then
      if [ "$alerted" = 1 ]; then
        text="$("ok_$key")"$'\n'"$(timeline "$since")"; [ -n "${was:-}" ] && text+=$'\n'"<i>Было: $was</i>"
        tg_edit "${msgid:-}" "$text" || tg 1 "$text" >/dev/null
        logl "OK   $key after $(dur $((NOW-since)))"
      fi
      st_del "$key"
    fi; return 0
  fi
  fails=$((fails+1)); [ "$since" -gt 0 ] || since=$NOW
  if [ "$alerted" = 0 ] && [ "$fails" -ge 2 ]; then
    MIN=$(( (NOW-since)/60 )); [ "$MIN" -ge 1 ] || MIN=1
    msgid=$(tg "$(loud "$sev")" "$("fail_$key")" "$key"); alerted=1; was="${WAS:-}"; logl "FAIL $key ${WAS:-}"
  fi
  st_put "$key" "$since" "$fails" "$alerted" "${msgid:-}" "${was:-}"
}

flush_spool
# 1. own xray: REALITY steal must answer like the pretended site — restart quietly, the exit node reports it
c=$(curl -sk -o /dev/null -w '%{http_code}' -m 8 --resolve "$RU_SNI:443:127.0.0.1" "https://$RU_SNI/" || true)
if [ -z "$c" ] || [ "$c" = 000 ]; then systemctl restart xray; logl "own xray restarted (HTTP ${c:-000})"; fi

# 2. do we have internet at all? (otherwise nothing below says anything about the exit node)
INET=0; [ "$(curl -s -o /dev/null -m 6 -w '%{http_code}' https://www.gstatic.com/generate_204 || true)" = 204 ] && INET=1
p_inet() { [ "$INET" = 1 ]; }
fail_inet() { msg "🟠 <b>$NODE · нет интернета с сервера</b>" "gstatic не отвечает уже $MIN мин." "👥 Вход через Россию не работает. Прямые ключи в $EXIT не затронуты."; }
ok_inet()   { echo "🟢 <b>$NODE · интернет вернулся</b>"; }
WAS="сервер в Москве был без интернета"; check inet warn p_inet
[ "$INET" = 1 ] || { logl "checked, no internet"; exit 0; }

# 3. the exit node as seen from Russia: :443 (domain, REALITY steal), :8443 (icloud certificate), IPv6 for contrast
c443=$(curl -sk -o /dev/null -w '%{http_code}' -m 8 --resolve "$DOMAIN:443:$NL_IP" "https://$DOMAIN/" || true); OK443=0; [ "$c443" = 200 ] && OK443=1
OK8443=0; timeout 8 openssl s_client -connect "$NL_IP:$TCP_PORT" -servername "$FALLBACK_SNI" </dev/null 2>/dev/null | grep -q "CN *= *$FALLBACK_SNI" && OK8443=1
OK6=""; if [ -n "${NL_IP6:-}" ]; then OK6=0; c6=$(curl -sk -o /dev/null -w '%{http_code}' -m 8 --resolve "$DOMAIN:443:[$NL_IP6]" "https://$DOMAIN/" || true); [ "$c6" = 200 ] && OK6=1; fi
if   [ "$OK443" = 1 ] && [ "$OK8443" = 1 ]; then KIND=ok;   SEV=info; WAS=""
elif [ "$OK443" = 0 ] && [ "$OK8443" = 0 ]; then SEV=crit
  if [ "$OK6" = 1 ]; then KIND=blocked4; WAS="IPv4 $NL_IP не отвечал из России, по IPv6 отвечал — похоже на блокировку адреса"
  else KIND=down; WAS="не отвечали ни :443, ни :$TCP_PORT${NL_IP6:+, ни IPv6}"; fi
elif [ "$OK443" = 0 ]; then KIND=sni;  SEV=warn; WAS=":443 ($DOMAIN) не отвечал, :$TCP_PORT работал — похоже на блокировку по SNI"
else                        KIND=tcp;  SEV=info; WAS=":$TCP_PORT не отвечал, :443 работал"; fi
if [ -n "${EXIT2_HOST:-}" ]; then FAILOVER="👥 Вход через Россию ушёл на запасной выход автоматически."
else FAILOVER="👥 Запасного выхода нет — вход через Россию не работает. Прямые ключи «Амстердам» не затронуты."; fi
p_nl() { [ "$KIND" = ok ]; }
fail_nl() { case $KIND in
  blocked4) msg "🔴 <b>$NODE · IPv4 $EXIT не виден из России</b>" ":443 и :$TCP_PORT на $NL_IP молчат уже $MIN мин, по IPv6 $EXIT отвечает." "Похоже на блокировку адреса." "$FAILOVER" "→ смени IP в боте";;
  down)     msg "🔴 <b>$NODE · $EXIT недоступен из России</b>" "Ни :443, ни :$TCP_PORT на $NL_IP не отвечают уже $MIN мин${NL_IP6:+, IPv6 тоже}. Интернет на $NODE есть." "Если $EXIT сам не писал — он лежит. Если писал, что жив — заблокирован целиком." "$FAILOVER" "→ открой бота и посмотри «Подробно»";;
  sni)      msg "🟠 <b>$NODE · основной путь $EXIT не виден из России</b>" ":$TCP_PORT отвечает, :443 ($DOMAIN) нет уже $MIN мин. Похоже на блокировку по SNI/домену." "👥 ClashMi переключится на резервный путь сам. Amnezia с ключом «Амстердам» — переключись на «Россия».";;
  *)        msg "🟡 <b>$NODE · резервный порт $EXIT не виден из России</b>" ":443 отвечает, :$TCP_PORT нет уже $MIN мин." "Основной путь работает.";;
  esac; }
ok_nl() { echo "🟢 <b>$NODE · $EXIT снова виден из России</b>"; }
check nl "$SEV" p_nl

logl "checked, exit=$KIND (443=$c443 8443=$OK8443${OK6:+ v6=$OK6})"
tail -n 1000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
