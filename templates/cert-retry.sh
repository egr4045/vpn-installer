#!/bin/bash
# cert-retry.sh — re-run install.sh every 10 minutes (up to 24h) until Let's Encrypt succeeds.
for i in $(seq 1 144); do
  if grep -q "^CERT_DIR=/etc/letsencrypt" /etc/safechill/vpn.env; then exit 0; fi
  cd /root/safechill && ./install.sh >/var/log/cert-retry.log 2>&1 || true
  sleep 600
done
