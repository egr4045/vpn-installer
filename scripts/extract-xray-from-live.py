#!/usr/bin/env python3
"""One-time migration helper: turn a Remnawave node's *live* xray config
(fetched from the internal socket, clients already injected) into a
standalone /etc/xray/config.json driven by our own admin.

Transforms:
  * drop Remnawave's internal mTLS api inbound (REMNAWAVE_API_INBOUND:61000)
    and add a plain gRPC api inbound on 127.0.0.1:API_PORT;
  * point api.tag / routing at our "api" tag, keep Handler+Stats services;
  * rename each client email -> username (so `xray api rmu -tag X <username>`
    and per-user stats key `user>>>username>>>traffic>>>...` are readable);
  * repoint the xray Hysteria2 inbound TLS cert from the dropped duckdns
    domain to the primary LE cert + direct SNI;
  * strip the legacy duckdns serverName from the Reality steal inbound.

Usage: extract-xray-from-live.py <live-config.json> <users.json> <out.json>
"""
import json
import sys

API_PORT = 10085
PRIMARY_CERT_DIR = "/etc/letsencrypt/live/example.com"   # parametrised in Phase B
DIRECT_SNI = "direct.example.com"
DROP_SERVERNAMES = {"anti-eblan-node.duckdns.org"}


def uuid_to_username(users_path):
    d = json.load(open(users_path))
    users = d.get("response", {}).get("users", [])
    return {u["vlessUuid"]: u["username"] for u in users}


def main():
    live_path, users_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    cfg = json.load(open(live_path))
    u2n = uuid_to_username(users_path)

    # 1. inbounds: drop Remnawave api inbound, rewrite client emails
    new_inbounds = []
    for inb in cfg.get("inbounds", []):
        if inb.get("tag") == "REMNAWAVE_API_INBOUND":
            continue
        for cl in inb.get("settings", {}).get("clients", []):
            name = u2n.get(cl.get("id"))
            if name:
                cl["email"] = name
        # repoint xray Hysteria2 cert away from duckdns
        if inb.get("tag") == "Hysteria2":
            tls = inb["streamSettings"]["tlsSettings"]
            tls["serverName"] = DIRECT_SNI
            tls["certificates"] = [{
                "keyFile": f"{PRIMARY_CERT_DIR}/privkey.pem",
                "certificateFile": f"{PRIMARY_CERT_DIR}/fullchain.pem",
            }]
        # strip duckdns from reality serverNames
        rs = inb.get("streamSettings", {}).get("realitySettings")
        if rs and rs.get("serverNames"):
            rs["serverNames"] = [s for s in rs["serverNames"] if s not in DROP_SERVERNAMES]
        new_inbounds.append(inb)

    # prepend our own gRPC api inbound
    new_inbounds.insert(0, {
        "tag": "api",
        "listen": "127.0.0.1",
        "port": API_PORT,
        "protocol": "dokodemo-door",
        "settings": {"address": "127.0.0.1"},
    })
    cfg["inbounds"] = new_inbounds

    # 2. api / routing / stats / policy
    cfg["api"] = {"tag": "api", "services": ["HandlerService", "StatsService"]}
    cfg.setdefault("stats", {})
    cfg.setdefault("policy", {})
    cfg["policy"]["levels"] = {"0": {"statsUserUplink": True, "statsUserDownlink": True,
                                     "statsUserOnline": True}}
    cfg["policy"]["system"] = {"statsInboundUplink": True, "statsInboundDownlink": True,
                               "statsOutboundUplink": True, "statsOutboundDownlink": True}
    rules = cfg.setdefault("routing", {}).setdefault("rules", [])
    rules = [r for r in rules if r.get("inboundTag") != ["REMNAWAVE_API_INBOUND"]]
    rules.insert(0, {"type": "field", "inboundTag": ["api"], "outboundTag": "api"})
    cfg["routing"]["rules"] = rules

    json.dump(cfg, open(out_path, "w"), indent=2, ensure_ascii=False)
    print(f"wrote {out_path}")
    for inb in cfg["inbounds"]:
        n = len(inb.get("settings", {}).get("clients", []))
        print(f"  {inb.get('tag'):20} port={inb.get('port','-'):<6} clients={n}")


if __name__ == "__main__":
    main()
