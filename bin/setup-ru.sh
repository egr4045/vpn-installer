#!/usr/bin/env bash
# setup-ru.sh <ru-ip> [sni] — turn a fresh Ubuntu 24.04 box in Russia into an ENTRY node that
# relays into this exit node over XHTTP+REALITY. Run on the exit node; needs root ssh to <ru-ip>
# (this script offers its own /root/.ssh/id_ed25519.pub if the key is not installed yet).
#   sni = a whitelisted Russian site the RU node pretends to be (default yandex.ru).
set -euo pipefail
ETC=/etc/safechill; TPL=/usr/local/share/safechill/templates
RU="${1:?usage: setup-ru.sh <ru-ip> [sni]}"; SNI="${2:-yandex.ru}"
[ -f /root/.ssh/id_ed25519 ] || ssh-keygen -q -t ed25519 -N "" -f /root/.ssh/id_ed25519
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new root@$RU"
if ! $SSH true 2>/dev/null; then
  echo "cannot ssh root@$RU non-interactively. Add this key to its /root/.ssh/authorized_keys and re-run:"; cat /root/.ssh/id_ed25519.pub; exit 1
fi
echo "> checking $SNI supports TLS 1.3 from the RU node"
$SSH "timeout 10 openssl s_client -connect $SNI:443 -servername $SNI -tls1_3 </dev/null 2>/dev/null | grep -q 'TLSv1.3'" \
  || { echo "x $SNI does not complete a TLS 1.3 handshake from $RU; pick another sni"; exit 1; }
echo "> installing xray + firewall on $RU"
scp -o BatchMode=yes -q "$TPL/install-ru.sh" "root@$RU:/root/install-ru.sh"
$SSH "TCP_PORT=${TCP_PORT:-8443} bash /root/install-ru.sh"
sed -i '/^RU_HOST=/d;/^RU_SNI=/d' "$ETC/vpn.env"; printf 'RU_HOST=%s\nRU_SNI=%s\n' "$RU" "$SNI" >> "$ETC/vpn.env"
echo "> rendering + pushing relay config"
render.sh
sleep 2
code=$(curl -sk -o /dev/null -w '%{http_code}' -m 8 --resolve "$SNI:443:$RU" "https://$SNI/" || true)
echo "+ RU entry node $RU up: REALITY steal of $SNI answers HTTP $code"
echo "  re-run add-client.sh <name> for existing people to get their RU links"
