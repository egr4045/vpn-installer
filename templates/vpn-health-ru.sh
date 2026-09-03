#!/usr/bin/env bash
# vpn-health-ru.sh — runs every minute ON the RU entry node (vpn-health-ru.timer).
# Its one job for Telegram: say how the exit node looks FROM RUSSIA — blocked IPv4, blocked SNI, or the exit
# being down. Nobody else can see that. Its own xray it restarts quietly (the exit node alerts about it), so
# there are no duplicate messages. Env: /etc/safechill/ru.env, pushed by the exit node's render.sh.
# Same grammar, levels and vocabulary as vpn-health.sh; alerts after two failed runs, recovery with downtime.
set -u
ENV=/etc/safechill/ru.env; LIB=/var/lib/safechill; STATE=$LIB/health.state; LOG=/var/log/vpn-health.log
[ -f "$ENV" ] || exit 0
set -a; . "$ENV"; set +a
mkdir -p "$LIB"; touch "$STATE"
BRAND=${BRAND:-SafeChill}; NODE=${NODE_NAME:-Москва}; EXIT=${EXIT_NAME:-Амстердам}; TCP_PORT=${TCP_PORT:-8443}
NOW=$(date +%s)
MSK()  { TZ=Europe/Moscow date -d "@${1:-$NOW}" +%H:%M; }
dur()  { local s=$1; if [ "$s" -lt 3600 ]; then echo "$(( (s+59)/60 )) мин"; elif [ "$s" -lt 86400 ]; then echo "$((s/3600)) ч $(( (s%3600)/60 )) мин"; else echo "$((s/86400)) дн. $(( (s%86400)/3600 )) ч"; fi; }
span() { echo "$(dur $((NOW-$1))) ($(MSK "$1")–$(MSK))"; }
logl() { echo "$(date +%FT%T) $*" >> "$LOG"; }
msg()  { printf '%s\n' "$@" | sed '/^$/d'; }
loud() { case $1 in crit|warn) echo 0;; *) echo 1;; esac; }

tg_raw() { local sil=false; [ "$2" = 1 ] && sil=true
  curl -s -m 8 -o /dev/null -f -X POST "https://api.telegram.org/bot$TG_BOT_TOKEN/sendMessage" -d chat_id="$1" -d parse_mode=HTML \
       -d disable_web_page_preview=true -d disable_notification=$sil --data-urlencode text="$3"; }
tg() { [ -n "${TG_BOT_TOKEN:-}" ] && [ -n "${TG_CHAT_ID:-}" ] && [ -n "$2" ] || return 0
  tg_raw "$TG_CHAT_ID" "$1" "$2" || printf '%s\t%s\t%s\n' "$NOW" "$1" "$(printf %s "$2" | base64 -w0)" >> "$LIB/tg.spool"; }
flush_spool() {
  [ -s "$LIB/tg.spool" ] && [ -n "${TG_BOT_TOKEN:-}" ] && [ -n "${TG_CHAT_ID:-}" ] || return 0
  local tmp ts sil b64 text; tmp=$(mktemp); mv "$LIB/tg.spool" "$tmp"
  while IFS=$'\t' read -r ts sil b64; do
    text="$(base64 -d <<<"$b64")"$'\n'"<i>задержано, событие в $(MSK "$ts")</i>"
    if ! tg_raw "$TG_CHAT_ID" "$sil" "$text"; then printf '%s\t%s\t%s\n' "$ts" "$sil" "$b64" >> "$LIB/tg.spool"; cat >> "$LIB/tg.spool"; break; fi
  done < "$tmp"; rm -f "$tmp"
}

# state: key <TAB> since <TAB> fails <TAB> alerted
if [ -s "$STATE" ] && ! grep -q $'\t' "$STATE"; then awk -v n="$NOW" '{print $1"\t"n"\t2\t1"}' "$STATE" > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"; fi
st_get() { awk -F'\t' -v k="$1" '$1==k{print $2"\t"$3"\t"$4; exit}' "$STATE"; }
st_put() { awk -F'\t' -v k="$1" '$1!=k' "$STATE" > "$STATE.tmp"; printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" >> "$STATE.tmp"; mv "$STATE.tmp" "$STATE"; }
st_del() { awk -F'\t' -v k="$1" '$1!=k' "$STATE" > "$STATE.tmp"; mv "$STATE.tmp" "$STATE"; }

check() { # check <key> <crit|warn|info> <probe-fn>   (no auto-fix here: the exit node is not ours to restart)
  local key=$1 sev=$2 probe=$3 since fails alerted
  IFS=$'\t' read -r since fails alerted <<<"$(st_get "$key")"; since=${since:-0}; fails=${fails:-0}; alerted=${alerted:-0}
  if $probe; then
    if [ "$fails" -gt 0 ]; then
      if [ "$alerted" = 1 ]; then DUR=$(span "$since"); tg 1 "$("ok_$key")"; logl "OK   $key after $(dur $((NOW-since)))"; fi
      st_del "$key"
    fi; return 0
  fi
  fails=$((fails+1)); [ "$since" -gt 0 ] || since=$NOW
  if [ "$alerted" = 0 ] && [ "$fails" -ge 2 ]; then
    MIN=$(( (NOW-since)/60 )); [ "$MIN" -ge 1 ] || MIN=1
    tg "$(loud "$sev")" "$("fail_$key")"; alerted=1; logl "FAIL $key $KIND"
  fi
  st_put "$key" "$since" "$fails" "$alerted"
}

flush_spool
# 1. own xray: REALITY steal must answer like the pretended site — restart quietly, the exit node reports it
c=$(curl -sk -o /dev/null -w '%{http_code}' -m 8 --resolve "$RU_SNI:443:127.0.0.1" "https://$RU_SNI/" || true)
if [ -z "$c" ] || [ "$c" = 000 ]; then systemctl restart xray; logl "own xray restarted (HTTP ${c:-000})"; fi

# 2. do we have internet at all? (otherwise nothing below says anything about the exit node)
INET=0; [ "$(curl -s -o /dev/null -m 6 -w '%{http_code}' https://www.gstatic.com/generate_204 || true)" = 204 ] && INET=1
p_inet() { [ "$INET" = 1 ]; }
fail_inet() { msg "🟠 <b>$NODE · нет интернета с сервера</b>" "gstatic не отвечает уже $MIN мин." "👥 Вход через Россию не работает. Основной путь напрямую в $EXIT не затронут."; }
ok_inet()   { msg "🟢 <b>$NODE · интернет вернулся</b>" "Не было $DUR."; }
KIND=""; check inet warn p_inet
[ "$INET" = 1 ] || { logl "checked, no internet"; exit 0; }

# 3. the exit node as seen from Russia: :443 (domain, REALITY steal), :8443 (icloud certificate), and IPv6 for contrast
c443=$(curl -sk -o /dev/null -w '%{http_code}' -m 8 --resolve "$DOMAIN:443:$NL_IP" "https://$DOMAIN/" || true); OK443=0; [ "$c443" = 200 ] && OK443=1
OK8443=0; timeout 8 openssl s_client -connect "$NL_IP:$TCP_PORT" -servername "$FALLBACK_SNI" </dev/null 2>/dev/null | grep -q "CN *= *$FALLBACK_SNI" && OK8443=1
OK6=""; if [ -n "${NL_IP6:-}" ]; then OK6=0; c6=$(curl -sk -o /dev/null -w '%{http_code}' -m 8 --resolve "$DOMAIN:443:[$NL_IP6]" "https://$DOMAIN/" || true); [ "$c6" = 200 ] && OK6=1; fi
if [ "$OK443" = 1 ] && [ "$OK8443" = 1 ]; then KIND=ok; SEV=info
elif [ "$OK443" = 0 ] && [ "$OK8443" = 0 ]; then if [ "$OK6" = 1 ]; then KIND=blocked4; else KIND=down; fi; SEV=crit
elif [ "$OK443" = 0 ]; then KIND=sni; SEV=warn
else KIND=tcp; SEV=info; fi
if [ -n "${EXIT2_HOST:-}" ]; then FAILOVER="👥 Вход через Россию ушёл на запасной выход автоматически."
else FAILOVER="👥 Запасного выхода нет — вход через Россию не работает. Прямые ключи «Основной» не затронуты."; fi
p_nl() { [ "$KIND" = ok ]; }
fail_nl() { case $KIND in
  blocked4) msg "🔴 <b>$NODE · IPv4 $EXIT не виден из России</b>" ":443 и :$TCP_PORT на $NL_IP молчат уже $MIN мин, по IPv6 $EXIT отвечает." "Похоже на блокировку адреса." "$FAILOVER" "→ /newip";;
  down)     msg "🔴 <b>$NODE · $EXIT недоступен из России</b>" "Ни :443, ни :$TCP_PORT на $NL_IP не отвечают уже $MIN мин${NL_IP6:+, IPv6 тоже}. Интернет на $NODE есть." "Если $EXIT сам не писал — он лежит. Если писал, что жив — заблокирован целиком." "$FAILOVER" "→ /status, затем /newip";;
  sni)      msg "🟠 <b>$NODE · основной путь $EXIT не виден из России</b>" ":$TCP_PORT отвечает, :443 ($DOMAIN) нет уже $MIN мин. Похоже на блокировку по SNI/домену." "👥 Happ переключится на резервный путь сам. Amnezia с ключом «Основной» — переключись на «Россия» или AmneziaWG.";;
  *)        msg "🟡 <b>$NODE · резервный порт $EXIT не виден из России</b>" ":443 отвечает, :$TCP_PORT нет уже $MIN мин." "Основной путь работает.";;
  esac; }
ok_nl() { msg "🟢 <b>$NODE · $EXIT снова виден из России</b>" "Не был виден $DUR."; }
check nl "$SEV" p_nl

logl "checked, exit=$KIND (443=$c443 8443=$OK8443${OK6:+ v6=$OK6})"
tail -n 1000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
