#!/usr/bin/env bash
# vpn-acceptance.sh — приёмочный тест кандидата под VPN-выход.
#
# Запускать НА проверяемом сервере до того, как платить за него / переносить юзеров:
#   ssh root@<new-ip> 'bash -s' < scripts/vpn-acceptance.sh
#
# Проверяет то, что реально ломается на «грязных» IP: не пинг до 8.8.8.8,
# а полный HTTPS-запрос к сервисам, которыми пользуются клиенты.
# Контрольные цели нужны, чтобы отличить «забанен этот IP» от «сеть лежит».

set -u
LC_ALL=C

RED=$'\033[1;31m'; GRN=$'\033[1;32m'; YEL=$'\033[1;33m'; DIM=$'\033[2m'; RST=$'\033[0m'

command -v curl >/dev/null || {
  DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl ca-certificates >/dev/null 2>&1
}

# группа|домен  — группы: META / GOOGLE / OTHER / CONTROL
TARGETS='
META|www.instagram.com
META|i.instagram.com
META|graph.instagram.com
META|scontent.cdninstagram.com
META|www.facebook.com
META|web.whatsapp.com
GOOGLE|www.youtube.com
GOOGLE|www.google.com
GOOGLE|youtubei.googleapis.com
GOOGLE|play.googleapis.com
OTHER|api.telegram.org
OTHER|x.com
OTHER|www.tiktok.com
OTHER|chatgpt.com
OTHER|discord.com
CONTROL|cloudflare.com
CONTROL|example.com
'

declare -A OK_N FAIL_N
FAILED_LIST=""

printf '%s══ ПРИЁМОЧНЫЙ ТЕСТ ══%s  %s  (%s)\n\n' "$GRN" "$RST" "$(hostname)" "$(date -u +%FT%TZ)"

MYIP=$(curl -s --max-time 8 https://api.ipify.org 2>/dev/null || echo '?')
printf 'Внешний IPv4 : %s\n' "$MYIP"

# IPv6: есть ли глобальный адрес и уходит ли трафик
V6ADDR=$(ip -6 addr show scope global 2>/dev/null | grep -oP 'inet6 \K[0-9a-f:]+' | head -1)
if [ -n "$V6ADDR" ]; then
  if curl -6 -s --max-time 8 -o /dev/null https://ipv6.google.com 2>/dev/null; then
    printf 'IPv6         : %sЕСТЬ и работает%s (%s)\n' "$GRN" "$RST" "$V6ADDR"
  else
    printf 'IPv6         : %sадрес есть, но наружу не ходит%s\n' "$YEL" "$RST"
  fi
else
  printf 'IPv6         : %sнет%s (нужно запрашивать у хостера)\n' "$YEL" "$RST"
fi
printf '\n%-34s %-8s %-9s %s\n' 'ЦЕЛЬ' 'HTTP' 'ВРЕМЯ' 'IP'
printf '%s\n' '────────────────────────────────────────────────────────────────'

while IFS='|' read -r grp host; do
  [ -z "${host:-}" ] && continue
  read -r code ttime ipaddr < <(
    curl -sS -o /dev/null --max-time 12 --connect-timeout 6 \
         -w '%{http_code} %{time_total} %{remote_ip}\n' \
         "https://$host/" 2>/dev/null || echo '000 0 -'
  )
  # 2xx/3xx/4xx = соединение состоялось (403/404 нас устраивает: сеть жива).
  # 000 = не достучались вообще — вот это и есть блокировка.
  if [ "$code" != "000" ] && [ -n "$code" ]; then
    OK_N[$grp]=$(( ${OK_N[$grp]:-0} + 1 ))
    printf '%-34s %s%-8s%s %-9s %s%s%s\n' "$host" "$GRN" "$code" "$RST" "${ttime}s" "$DIM" "$ipaddr" "$RST"
  else
    FAIL_N[$grp]=$(( ${FAIL_N[$grp]:-0} + 1 ))
    FAILED_LIST="$FAILED_LIST $grp:$host"
    printf '%-34s %s%-8s%s %-9s %s\n' "$host" "$RED" "БЛОК" "$RST" '-' "$DIM-$RST"
  fi
done <<< "$(echo "$TARGETS" | grep -v '^$')"

printf '\n%s── скорость ──%s\n' "$GRN" "$RST"
SPD=$(curl -o /dev/null -s --max-time 30 -w '%{speed_download}' \
      'https://speed.cloudflare.com/__down?bytes=50000000' 2>/dev/null || echo 0)
printf 'Cloudflare 50MB : %.0f Мбит/с\n' "$(echo "$SPD" | awk '{print $1*8/1000000}')"

printf '\n%s── ИТОГ ──%s\n' "$GRN" "$RST"
verdict_ok=1
for g in META GOOGLE OTHER CONTROL; do
  o=${OK_N[$g]:-0}; f=${FAIL_N[$g]:-0}; t=$((o+f))
  [ "$t" -eq 0 ] && continue
  if [ "$f" -eq 0 ]; then c=$GRN; else c=$RED; verdict_ok=0; fi
  printf '  %-8s %s%d/%d доступно%s\n' "$g" "$c" "$o" "$t" "$RST"
done

CTRL_FAIL=${FAIL_N[CONTROL]:-0}
echo
if [ "$CTRL_FAIL" -gt 0 ]; then
  printf '%sНЕВАЛИДНЫЙ ТЕСТ%s — упали контрольные цели, проблема в сети/DNS сервера,\n' "$RED" "$RST"
  printf 'а не в репутации IP. Чинить сеть и перезапустить.\n'
elif [ "$verdict_ok" -eq 1 ]; then
  printf '%s✓ IP ЧИСТЫЙ — сервер можно брать.%s\n' "$GRN" "$RST"
else
  printf '%s✗ IP ГРЯЗНЫЙ — НЕ брать / требовать замену адреса.%s\n' "$RED" "$RST"
  printf 'Недоступно:%s\n' "$FAILED_LIST"
  printf '%sКонтрольные цели живы → сеть в порядке, забанен именно этот адрес.%s\n' "$DIM" "$RST"
fi
