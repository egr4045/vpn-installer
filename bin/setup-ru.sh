#!/usr/bin/env bash
# setup-ru.sh <ru-ip> [sni] — turn a fresh Ubuntu 24.04 box in Russia into an ENTRY node that
# relays into this exit node over XHTTP+REALITY, and make it watch the exit node in return.
# Run on the exit node; needs root ssh to <ru-ip> (offers its own key if not installed yet).
#   sni = a whitelisted Russian site the RU node pretends to be (default yandex.ru).
set -euo pipefail
ETC=/etc/safechill; TPL=/usr/local/share/safechill/templates
RU="${1:?usage: setup-ru.sh <ru-ip> [sni]}"; SNI="${2:-yandex.ru}"
set -a; . "$ETC/vpn.env"; set +a
[ -f /root/.ssh/id_ed25519 ] || ssh-keygen -q -t ed25519 -N "" -f /root/.ssh/id_ed25519
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new "root@$RU")
if ! "${SSH[@]}" true 2>/dev/null; then
  echo "cannot ssh root@$RU non-interactively. Add this key to its /root/.ssh/authorized_keys and re-run:"; cat /root/.ssh/id_ed25519.pub; exit 1
fi
echo "> checking $SNI supports TLS 1.3 from the RU node"
"${SSH[@]}" "timeout 10 openssl s_client -connect $SNI:443 -servername $SNI -tls1_3 </dev/null 2>/dev/null | grep -q 'TLSv1.3'" \
  || { echo "x $SNI does not complete a TLS 1.3 handshake from $RU; pick another sni"; exit 1; }
echo "> installing xray + firewall + health timer on $RU"
scp -o BatchMode=yes -q "$TPL/install-ru.sh" "$TPL/vpn-health-ru.sh" "$TPL/vpn-health-ru.service" "$TPL/vpn-health-ru.timer" "root@$RU:/root/"
"${SSH[@]}" "TCP_PORT=${TCP_PORT:-8443} bash /root/install-ru.sh && mkdir -p /etc/safechill && install -m755 /root/vpn-health-ru.sh /usr/local/bin/vpn-health-ru.sh && install -m644 /root/vpn-health-ru.service /root/vpn-health-ru.timer /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now vpn-health-ru.timer >/dev/null 2>&1; rm -f /root/vpn-health-ru.*"
RU6=$("${SSH[@]}" "ip -6 addr show scope global | awk '/inet6/{print \$2}' | cut -d/ -f1 | grep -v '^fd' | head -1" || true)
sed -i '/^RU_HOST=/d;/^RU_SNI=/d;/^RU_HOST6=/d' "$ETC/vpn.env"
{ echo "RU_HOST=$RU"; echo "RU_SNI=$SNI"; [ -n "$RU6" ] && echo "RU_HOST6=$RU6"; } >> "$ETC/vpn.env"
echo "> rendering + pushing relay config"
render.sh
sleep 2
code=$(curl -sk -o /dev/null -w '%{http_code}' -m 8 --resolve "$SNI:443:$RU" "https://$SNI/" || true)
echo "+ RU entry node $RU${RU6:+ / $RU6} up: REALITY steal of $SNI answers HTTP $code; it now also watches this node"
echo "  re-run add-client.sh <name> for existing people to get their RU links"
