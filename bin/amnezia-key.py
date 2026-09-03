#!/usr/bin/env python3
"""amnezia-key.py <name> [nl|ru|x2] [xray|awg] — build an AmneziaVPN native key (vpn://…).

Format (as the client itself does it): vpn:// + base64url( 4-byte big-endian length + zlib(JSON) ).
Containers are marked isThirdPartyConfig so the client uses our configs verbatim.
One protocol per key on purpose: a key with both xray and AWG is ~2000 chars and its QR is too dense
for phone cameras; a single-protocol key is ~900 chars and scans instantly.

Files in /root/clients/<name>/: amnezia (nl, xray), amnezia-awg (nl, AmneziaWG), amnezia-ru (RU entry),
amnezia-x2 (standby exit, xray). Each as .txt (the key) and .png (QR).
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

def xray_container(host, sni, fp, uuid, S):
    cfg = {"inbounds": [{"tag": "socks-in", "listen": "127.0.0.1", "port": 10808, "protocol": "socks", "settings": {"udp": True}}],
           "outbounds": [{"tag": "proxy", "protocol": "vless",
                          "settings": {"vnext": [{"address": host, "port": 443, "users": [{"id": uuid, "encryption": "none"}]}]},
                          "streamSettings": {"network": "xhttp", "security": "reality",
                                             "xhttpSettings": {"path": "/" + S["XHTTP_PATH"], "mode": "auto", "extra": {"xmux": XMUX}},
                                             "realitySettings": {"serverName": sni, "fingerprint": fp, "publicKey": S["REALITY_PUB"], "shortId": S["SHORT_ID"]}}}]}
    return {"container": "amnezia-xray", "xray": {"last_config": json.dumps(cfg, separators=(",", ":")), "isThirdPartyConfig": True}}

def awg_container(conf_path, host, port):
    conf = conf_path.read_text()
    m = dict(re.findall(r"^(\w+)\s*=\s*(.+?)\s*$", conf, re.M))
    last = {"config": conf, "hostName": host, "port": int(port),
            "client_priv_key": m["PrivateKey"], "client_ip": m["Address"], "psk_key": m.get("PresharedKey", ""),
            "server_pub_key": m["PublicKey"], "mtu": m.get("MTU", "1280"),
            "persistent_keep_alive": m.get("PersistentKeepalive", "25"),
            "allowed_ips": [x.strip() for x in m.get("AllowedIPs", "0.0.0.0/0, ::/0").split(",")]}
    for k in AWG_KEYS:
        if k in m: last[k] = m[k]
    return {"container": "amnezia-awg",
            "awg": {"last_config": json.dumps(last, separators=(",", ":")), "isThirdPartyConfig": True,
                    "port": str(port), "transport_proto": "udp"}}

def main():
    if len(sys.argv) < 2: sys.exit("usage: amnezia-key.py <name> [nl|ru|x2] [xray|awg]")
    name = sys.argv[1]; node = sys.argv[2] if len(sys.argv) > 2 else "nl"; proto = sys.argv[3] if len(sys.argv) > 3 else "xray"
    V = load_env(ETC / "vpn.env"); S = load_env(ETC / "secrets.env")
    users = json.loads((ETC / "users.json").read_text())
    u = next((x for x in users if x["name"] == name), None) or sys.exit(f"unknown user {name}")
    d = CL / name; brand = V.get("BRAND", "SafeChill"); awg_port = V.get("AWG_PORT", "39217")
    if node == "nl":
        host, sni, fp, label, awg_conf = V["SERVER_IP"], V["DOMAIN"], "firefox", "Амстердам", d / "awg.conf"
    elif node == "x2":
        if not V.get("EXIT2_HOST"): sys.exit("no standby exit configured")
        host, sni, fp, label, awg_conf = V["EXIT2_HOST"], V["EXIT2_DOMAIN"], "firefox", "запасной выход", d / "awg-x2.conf"
    else:
        if not V.get("RU_HOST"): sys.exit("no RU node configured")
        host, sni, fp, label, awg_conf = V["RU_HOST"], V.get("RU_SNI", "yandex.ru"), "chrome", "вход через Россию", None
    if proto == "awg":
        if awg_conf is None or not awg_conf.exists(): sys.exit(f"no AWG on node {node}")
        containers = [awg_container(awg_conf, host, awg_port)]; default = "amnezia-awg"; suffix = " · AWG"
    else:
        containers = [xray_container(host, sni, fp, u["uuid"], S)]; default = "amnezia-xray"; suffix = ""
    cfg = {"containers": containers, "defaultContainer": default,
           "description": f"{brand} · {label}{suffix} · {name}", "hostName": host, "dns1": "1.1.1.1", "dns2": "8.8.8.8"}
    js = json.dumps(cfg, ensure_ascii=False, separators=(",", ":")).encode()
    key = "vpn://" + base64.urlsafe_b64encode(len(js).to_bytes(4, "big") + zlib.compress(js, 9)).decode().rstrip("=")
    stem = {"nl": "amnezia", "ru": "amnezia-ru", "x2": "amnezia-x2"}[node] + ("-awg" if proto == "awg" else "")
    (d / f"{stem}.txt").write_text(key)
    subprocess.run(["qrencode", "-o", str(d / f"{stem}.png"), "-s", "8", "-m", "3", "-l", "L", "-r", str(d / f"{stem}.txt")], check=True)
    print(f"{stem}: {len(key)} chars, default {default}")

if __name__ == "__main__":
    main()
