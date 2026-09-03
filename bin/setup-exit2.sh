#!/usr/bin/env bash
# setup-exit2.sh <ip> <domain> — turn a fresh Ubuntu 24.04 box into a STANDBY EXIT: same secrets, users
# and AmneziaWG peers as this node, its own domain for the REALITY steal site. The RU entry node fails
# over to it automatically; people get x2-* links / the amnezia-x2 key. Run on the primary exit node.
set -euo pipefail
ETC=/etc/safechill
IP="${1:?usage: setup-exit2.sh <ip> <domain>}"; DOM="${2:?usage: setup-exit2.sh <ip> <domain>}"
set -a; . "$ETC/vpn.env"; set +a
[ -f /root/.ssh/id_ed25519 ] || ssh-keygen -q -t ed25519 -N "" -f /root/.ssh/id_ed25519
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new "root@$IP")
if ! "${SSH[@]}" true 2>/dev/null; then
  echo "cannot ssh root@$IP non-interactively. Add this key to its /root/.ssh/authorized_keys and re-run:"; cat /root/.ssh/id_ed25519.pub; exit 1
fi
echo "> copying repo, secrets, users and peers to $IP"
"${SSH[@]}" "rm -rf /root/safechill && mkdir -p /root/safechill /etc/safechill/peers && chmod 700 /etc/safechill"
scp -o BatchMode=yes -q -r /root/safechill/. "root@$IP:/root/safechill/"
scp -o BatchMode=yes -q "$ETC/secrets.env" "$ETC/users.json" "root@$IP:/etc/safechill/"
scp -o BatchMode=yes -q -r "$ETC/peers" "root@$IP:/etc/safechill/"
{ echo "BRAND=${BRAND:-SafeChill}"; echo "DOMAIN=$DOM"; echo "EMAIL=${EMAIL:-}"; echo "TG_BOT_TOKEN=${TG_BOT_TOKEN:-}"
  echo "TG_CHAT_ID=${TG_CHAT_ID:-$(cat "$ETC/tg_chat_id" 2>/dev/null || true)}"; echo "BOT_ENABLED=0"
  echo "AWG_PORT=${AWG_PORT:-39217}"; echo "AWG_NET4=${AWG_NET4:-10.8.0}"; echo "AWG_NET6=${AWG_NET6:-fd08:5afe:c411}"
  echo "TCP_PORT=${TCP_PORT:-8443}"; echo "FALLBACK_SNI=${FALLBACK_SNI:-gateway.icloud.com}"
  echo "BLOCK_TORRENT=${BLOCK_TORRENT:-1}"; echo "EGRESS_PREFER=${EGRESS_PREFER:-ipv4}"; } > /tmp/exit2.env
scp -o BatchMode=yes -q /tmp/exit2.env "root@$IP:/etc/safechill/vpn.env"; rm -f /tmp/exit2.env
echo "> installing the stack on $IP (a few minutes)"
"${SSH[@]}" "chmod 600 /etc/safechill/*.env /etc/safechill/users.json; cd /root/safechill && chmod +x install.sh bin/*.sh bin/*.py templates/*.sh && ./install.sh 2>&1 | tail -5"
sed -i '/^EXIT2_HOST=/d;/^EXIT2_DOMAIN=/d' "$ETC/vpn.env"
printf 'EXIT2_HOST=%s\nEXIT2_DOMAIN=%s\n' "$IP" "$DOM" >> "$ETC/vpn.env"
echo "> rendering: sync standby, RU balancer"
render.sh
sleep 2
code=$(curl -sk -o /dev/null -w '%{http_code}' -m 8 --resolve "$DOM:443:$IP" "https://$DOM/" || true)
echo "+ standby exit $IP ($DOM) up: REALITY steal answers HTTP $code"
echo "  re-run add-client.sh <name> for existing people to get their x2-* links and amnezia-x2 key"
