#!/usr/bin/env python3
"""Seed the SQLite store from a Remnawave users export (GET /api/users).

Preserves username / vlessUuid / shortUuid / trojanPassword / traffic limit /
used bytes / expiry / status so existing subscription URLs and client configs
keep working after the cutover.

Usage:
    VPN_DATA_DIR=/data python migrate-from-remnawave.py users.json
"""
import json
import sys

sys.path.insert(0, "/app")          # in-container path
sys.path.insert(0, ".")             # local run from vpn-admin/
from app import store               # noqa: E402

STATUS_MAP = {"ACTIVE": "ACTIVE", "DISABLED": "DISABLED",
              "LIMITED": "LIMITED", "EXPIRED": "EXPIRED"}


def main():
    data = json.load(open(sys.argv[1]))
    users = data.get("response", {}).get("users", data if isinstance(data, list) else [])
    store.init_db()
    existing = {u["username"] for u in store.list_users()}
    added = 0
    for u in users:
        name = u["username"]
        if name in existing:
            print(f"  skip (exists): {name}")
            continue
        used = int(u.get("userTraffic", {}).get("usedTrafficBytes")
                   or u.get("usedTrafficBytes") or 0)
        store.create_user(
            username=name,
            vless_uuid=u["vlessUuid"],
            short_uuid=u["shortUuid"],
            trojan_pass=u["trojanPassword"],
            traffic_limit=int(u.get("trafficLimitBytes") or 0),
            used_down=used,            # historical total -> downlink bucket
            used_up=0,
            expire_at=u.get("expireAt") or store.FAR_FUTURE,
            status=STATUS_MAP.get(u.get("status", "ACTIVE"), "ACTIVE"),
            created_at=u.get("createdAt"),
        )
        added += 1
        print(f"  + {name}  {u['vlessUuid'][:8]}  short={u['shortUuid']}")
    print(f"seeded {added} user(s); total now {len(store.list_users())}")


if __name__ == "__main__":
    main()
