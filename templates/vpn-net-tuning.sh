#!/usr/bin/env bash
# VPN NIC tuning — RPS/RFS + ring buffers for the primary uplink.
# Runtime-applied + persisted; see /root/.claude/plans/nested-yawning-penguin.md.
set -u
IF="${VPN_NIC:-ens1}"

# fall back to the default-route iface if ens1 is renamed on this host
if [ ! -d "/sys/class/net/$IF" ]; then
  IF="$(ip -o route get 1.1.1.1 2>/dev/null | grep -oE 'dev [^ ]+' | awk '{print $2}')"
fi
[ -d "/sys/class/net/$IF" ] || exit 0

# RPS: spread receive softirq across all CPUs (mask = all cores)
ncpu="$(nproc)"
mask=$(( (1 << ncpu) - 1 ))
hexmask=$(printf '%x' "$mask")
for q in /sys/class/net/"$IF"/queues/rx-*; do
  echo "$hexmask" > "$q/rps_cpus" 2>/dev/null || true
  echo 32768      > "$q/rps_flow_cnt" 2>/dev/null || true
done

# Ring buffers: raise toward hardware max to cut burst drops
ethtool -G "$IF" rx 4096 tx 4096 2>/dev/null || true

exit 0
