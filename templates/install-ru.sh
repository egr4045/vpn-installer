#!/usr/bin/env bash
# install-ru.sh — runs ON the RU entry node (pushed by setup-ru.sh). xray + ufw + BBR only;
# its config is rendered and pushed by the exit node (render.sh).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
TCP_PORT=${TCP_PORT:-8443}
apt-get update -qq
apt-get install -y -qq ufw curl jq openssl >/dev/null
command -v xray >/dev/null || bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install >/tmp/xray-install.log 2>&1
printf 'net.core.default_qdisc = fq\nnet.ipv4.tcp_congestion_control = bbr\nnet.ipv4.tcp_fastopen = 3\nnet.core.rmem_max = 16777216\nnet.core.wmem_max = 16777216\n' > /etc/sysctl.d/99-vpn.conf
sysctl --system >/dev/null
install -d /etc/systemd/journald.conf.d
printf '[Journal]
Storage=persistent
MaxRetentionSec=8day
SystemMaxUse=4G
SystemMaxFileSize=128M
' > /etc/systemd/journald.conf.d/99-safechill.conf
systemctl restart systemd-journald
ufw default deny incoming >/dev/null; ufw default allow outgoing >/dev/null
for p in 22/tcp 443/tcp "$TCP_PORT/tcp"; do ufw allow "$p" >/dev/null; done
ufw --force enable >/dev/null
systemctl enable xray >/dev/null 2>&1 || true
echo "RU node ready: $(xray version | head -1)"
