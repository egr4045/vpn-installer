#!/usr/bin/env bash
# update-xray.sh — upgrade xray-core to the latest release on the exit node (and the RU node if any).
set -euo pipefail
set -a; . /etc/safechill/vpn.env; set +a
bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
render.sh && systemctl restart xray && echo "exit node: $(xray version | head -1)"
if [ -n "${RU_HOST:-}" ]; then
  ssh -o BatchMode=yes root@"$RU_HOST" 'bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install >/dev/null && systemctl restart xray && xray version | head -1' \
    && echo "RU node updated" || echo "RU node update failed"
fi
