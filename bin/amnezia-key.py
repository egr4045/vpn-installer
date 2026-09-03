#!/usr/bin/env python3
"""amnezia-key.py <name> [nl|ru] — build an AmneziaVPN native key (vpn://…) for one node carrying
every protocol we run there, so one QR imports xray (XHTTP+REALITY) and AmneziaWG together.

Format (as the client itself does it): vpn:// + base64url( 4-byte big-endian length + zlib(JSON) ).
Containers are marked isThirdPartyConfig so the client uses our configs verbatim.
Writes /root/clients/<name>/amnezia.txt + amnezia.png (nl) or amnezia-ru.txt/.png (ru).
"""
import base64, json, pathlib, re, subprocess, sys, zlib

ETC = pathlib.Path("/etc/safechill"); CL = pathlib.Path("/root/clients")
AWG_KEYS = ("Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4", "I1", "I2", "I3", "I4", "I5",
            "HeaderProtectionKey", "ContentPaddingAddition", "RekeyAfterTime", "RekeyTimeout", "RejectAfterTime",
            "KeepaliveTimeout", "MaxHandshakeAttempts", "RandomTrailers")
XMUX = {"maxConcurrency": "16-32", "maxConnections": 0, "cMaxReuseTimes": "64-128",
        "hMaxRequestTimes": "800-900", "hMaxReusableSecs": "1800-3000"}

def load_env(p):
    env = {}
    for line in pathlib.Path(p).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1); env[k.strip()] = v.strip().strip("'\"")
    return env

def main():
    if len(sys.argv) < 2: sys.exit("usage: amnezia-key.py <name> [nl|ru]")
    name, node = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "nl")
    V = load_env(ETC / "vpn.env"); S = load_env(ETC / "secrets.env")
    users = json.loads((ETC / "users.json").read_text())
    u = next((x for x in users if x["name"] == name), None) or sys.exit(f"unknown user {name}")
    d = CL / name; brand = V.get("BRAND", "SafeChill")
    if node == "nl":
        host, sni, fp, label = V["SERVER_IP"], V["DOMAIN"], "firefox", "Амстердам"
    else:
        if not V.get("RU_HOST"): sys.exit("no RU node configured")
        host, sni, fp, label = V["RU_HOST"], V.get("RU_SNI", "yandex.ru"), "chrome", "вход через Россию"

    xray_cfg = {
        "log": {"loglevel": "warning"},
        "inbounds": [{"tag": "socks-in", "listen": "127.0.0.1", "port": 10808, "protocol": "socks", "settings": {"udp": True}}],
        "outbounds": [
            {"tag": "proxy", "protocol": "vless",
             "settings": {"vnext": [{"address": host, "port": 443, "users": [{"id": u["uuid"], "encryption": "none"}]}]},
             "streamSettings": {"network": "xhttp", "security": "reality",
                                "xhttpSettings": {"path": "/" + S["XHTTP_PATH"], "mode": "auto", "extra": {"xmux": XMUX}},
                                "realitySettings": {"serverName": sni, "fingerprint": fp, "publicKey": S["REALITY_PUB"], "shortId": S["SHORT_ID"]}}},
            {"tag": "direct", "protocol": "freedom"},
        ],
    }
    containers = [{"container": "amnezia-xray",
                   "xray": {"last_config": json.dumps(xray_cfg, separators=(",", ":")), "isThirdPartyConfig": True}}]

    if node == "nl" and (d / "awg.conf").exists():
        conf = (d / "awg.conf").read_text()
        m = dict(re.findall(r"^(\w+)\s*=\s*(.+?)\s*$", conf, re.M))
        last = {"config": conf, "hostName": host, "port": int(V.get("AWG_PORT", "39217")),
                "client_priv_key": m["PrivateKey"], "client_ip": m["Address"], "psk_key": m.get("PresharedKey", ""),
                "server_pub_key": m["PublicKey"], "mtu": m.get("MTU", "1280"),
                "persistent_keep_alive": m.get("PersistentKeepalive", "25"),
                "allowed_ips": [x.strip() for x in m.get("AllowedIPs", "0.0.0.0/0, ::/0").split(",")]}
        for k in AWG_KEYS:
            if k in m: last[k] = m[k]
        containers.append({"container": "amnezia-awg",
                           "awg": {"last_config": json.dumps(last, separators=(",", ":")), "isThirdPartyConfig": True,
                                   "port": str(V.get("AWG_PORT", "39217")), "transport_proto": "udp"}})

    cfg = {"containers": containers, "defaultContainer": "amnezia-xray",
           "description": f"{brand} · {label} · {name}", "hostName": host, "dns1": "1.1.1.1", "dns2": "8.8.8.8"}
    js = json.dumps(cfg, ensure_ascii=False, separators=(",", ":")).encode()
    key = "vpn://" + base64.urlsafe_b64encode(len(js).to_bytes(4, "big") + zlib.compress(js, 9)).decode().rstrip("=")
    stem = "amnezia" if node == "nl" else "amnezia-ru"
    (d / f"{stem}.txt").write_text(key)
    subprocess.run(["qrencode", "-o", str(d / f"{stem}.png"), "-s", "4", "-l", "L", "-r", str(d / f"{stem}.txt")], check=True)
    print(f"{stem}: {len(key)} chars, {len(containers)} containers")

if __name__ == "__main__":
    main()
