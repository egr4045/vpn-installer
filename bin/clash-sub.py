#!/usr/bin/env python3
"""clash-sub.py <name> — a complete mihomo (Clash Meta) profile for one person: every xray server
(XHTTP+REALITY and TCP fallbacks on all nodes, IPv4 and IPv6), AmneziaWG 3.1 on both exits, an
auto group that prefers the RU entry and falls back to Amsterdam, Russian sites direct, DNS over HTTPS.
Works in ClashMi, Clash Verge Rev, FlClash, Clash Meta for Android (mihomo >= 1.19.30 for AWG 3.1).

Served by nginx as https://DOMAIN/c/<token> (behind the REALITY steal on :443).
Writes /root/clients/<name>/clash.txt (URL) and clash.png (QR).
"""
import json, pathlib, subprocess, sys
try:
    import yaml
except ImportError:  # JSON is valid YAML; mihomo reads it fine
    yaml = None

ETC = pathlib.Path("/etc/safechill"); CL = pathlib.Path("/root/clients"); OUT = pathlib.Path("/var/www/html/c")
PROBE = "https://www.gstatic.com/generate_204"

def load_env(p):
    env = {}
    for line in pathlib.Path(p).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1); env[k.strip()] = v.strip().strip("'\"")
    return env

def main():
    if len(sys.argv) < 2: sys.exit("usage: clash-sub.py <name>")
    name = sys.argv[1]
    V = load_env(ETC / "vpn.env"); S = load_env(ETC / "secrets.env")
    users = json.loads((ETC / "users.json").read_text())
    u = next((x for x in users if x["name"] == name), None) or sys.exit(f"unknown user {name}")
    P = load_env(ETC / "peers" / f"{name}.env") if (ETC / "peers" / f"{name}.env").exists() else {}
    brand = V.get("BRAND", "SafeChill"); tcp_port = int(V.get("TCP_PORT", "8443")); awg_port = int(V.get("AWG_PORT", "39217"))
    fallback_sni = V.get("FALLBACK_SNI", "gateway.icloud.com"); net4 = V.get("AWG_NET4", "10.8.0")
    xmux = {"max-concurrency": "16-32", "max-connections": "0", "c-max-reuse-times": "64-128",
            "h-max-request-times": "800-900", "h-max-reusable-secs": "1800-3000"}

    def xhttp(label, server, sni, fp):
        return {"name": label, "type": "vless", "server": server, "port": 443, "uuid": u["uuid"], "udp": True, "tls": True,
                "network": "xhttp", "servername": sni, "client-fingerprint": fp, "encryption": "",
                "reality-opts": {"public-key": S["REALITY_PUB"], "short-id": S["SHORT_ID"]},
                "xhttp-opts": {"path": "/" + S["XHTTP_PATH"], "mode": "auto", "reuse-settings": xmux}}

    def tcp(label, server, sni, fp):
        return {"name": label, "type": "vless", "server": server, "port": tcp_port, "uuid": u["uuid"], "udp": True, "tls": True,
                "network": "tcp", "flow": "xtls-rprx-vision", "servername": sni, "client-fingerprint": fp, "encryption": "",
                "reality-opts": {"public-key": S["REALITY_PUB"], "short-id": S["SHORT_ID"]}}

    def awg(label, server):
        def rng(k, default):  # "a-b" ranges are what the kernel module accepts; mihomo v3 takes the same strings
            return S.get(k, default)
        return {"name": label, "type": "wireguard", "server": server, "port": awg_port, "ip": f"{net4}.{P['PEER_N']}",
                "private-key": P["PEER_PRIV"], "public-key": S["AWG_PUB"], "pre-shared-key": P["PEER_PSK"],
                "allowed-ips": ["0.0.0.0/0"], "udp": True, "mtu": 1280, "persistent-keepalive": 25,
                "remote-dns-resolve": True, "dns": ["1.1.1.1", "8.8.8.8"],
                "amnezia-wg-option": {"version": 3, "jc": int(S["AWG_JC"]), "jmin": int(S["AWG_JMIN"]), "jmax": int(S["AWG_JMAX"]),
                                      "s1": int(S["AWG_S1"]), "s2": int(S["AWG_S2"]), "s3": int(S["AWG_S3"]), "s4": int(S["AWG_S4"]),
                                      "h1": S["AWG_H1"], "h2": S["AWG_H2"], "h3": S["AWG_H3"], "h4": S["AWG_H4"],
                                      "i1": S["AWG_I1"], "i2": S["AWG_I2"],
                                      "header-protection-key": S["AWG_HPK"], "content-padding-addition": rng("AWG_CPA", "0-64"),
                                      "rekey-after-time": rng("AWG_RAT", "100-140"), "keepalive-timeout": rng("AWG_KT", "8-12"),
                                      "random-trailers": True}}

    proxies, main_order, tcp_order, awg_order = [], [], [], []
    def add(p, bucket): proxies.append(p); bucket.append(p["name"])
    if V.get("RU_HOST"):
        add(xhttp("🇷🇺 Россия → Амстердам", V["RU_HOST"], V.get("RU_SNI", "yandex.ru"), "chrome"), main_order)
        if V.get("RU_HOST6"): add(xhttp("🇷🇺 Россия → Амстердам (IPv6)", V["RU_HOST6"], V.get("RU_SNI", "yandex.ru"), "chrome"), main_order)
    add(xhttp("🟢 Амстердам", V["SERVER_IP"], V["DOMAIN"], "firefox"), main_order)
    if V.get("SERVER_IP6"): add(xhttp("🟢 Амстердам (IPv6)", V["SERVER_IP6"], V["DOMAIN"], "firefox"), main_order)
    if V.get("EXIT2_HOST"): add(xhttp("🛟 Запасной выход", V["EXIT2_HOST"], V["EXIT2_DOMAIN"], "firefox"), main_order)
    if V.get("RU_HOST"): add(tcp("🇷🇺 Россия TCP", V["RU_HOST"], V.get("RU_SNI", "yandex.ru"), "chrome"), tcp_order)
    add(tcp("🟢 Амстердам TCP", V["SERVER_IP"], fallback_sni, "chrome"), tcp_order)
    if V.get("EXIT2_HOST"): add(tcp("🛟 Запасной TCP", V["EXIT2_HOST"], fallback_sni, "chrome"), tcp_order)
    if P:
        add(awg("⚡ AWG Амстердам", V["SERVER_IP"]), awg_order)
        if V.get("EXIT2_HOST"): add(awg("⚡ AWG Запасной", V["EXIT2_HOST"]), awg_order)
        if V.get("SERVER_IP6"): add(awg("⚡ AWG Амстердам (IPv6)", V["SERVER_IP6"]), awg_order)

    groups = [
        {"name": f"🔐 {brand}", "type": "select",
         "proxies": ["⚡ Авто (Россия → Амстердам)"] + (["⚡ AWG (дом, макс. скорость)"] if awg_order else []) + main_order + tcp_order + ["DIRECT"]},
        {"name": "⚡ Авто (Россия → Амстердам)", "type": "fallback", "proxies": main_order + tcp_order,
         "url": PROBE, "interval": 120, "timeout": 3000, "lazy": False},
        {"name": "🇷🇺 Сайты РФ", "type": "select", "proxies": ["DIRECT", f"🔐 {brand}"]},
    ]
    if awg_order:
        groups.insert(2, {"name": "⚡ AWG (дом, макс. скорость)", "type": "url-test", "proxies": awg_order,
                          "url": PROBE, "interval": 300, "tolerance": 80})

    cfg = {
        "mixed-port": 7890, "allow-lan": False, "mode": "rule", "log-level": "warning", "ipv6": True,
        "unified-delay": True, "tcp-concurrent": True, "find-process-mode": "off",
        "geodata-mode": False, "geo-auto-update": True, "geo-update-interval": 168,
        "profile": {"store-selected": True, "store-fake-ip": True},
        "sniffer": {"enable": True, "sniff": {"HTTP": {"ports": [80, 8080]}, "TLS": {"ports": [443, 8443]}}},
        "dns": {"enable": True, "ipv6": True, "enhanced-mode": "fake-ip", "fake-ip-range": "198.18.0.1/16",
                "fake-ip-filter": ["*.lan", "*.local", "localhost.ptlogin2.qq.com", "+.msftconnecttest.com", "+.msftncsi.com", "+.pool.ntp.org"],
                "default-nameserver": ["1.1.1.1", "8.8.8.8"],
                "nameserver": ["https://1.1.1.1/dns-query", "https://dns.google/dns-query"],
                "proxy-server-nameserver": ["https://1.1.1.1/dns-query", "https://dns.google/dns-query"]},
        "proxies": proxies, "proxy-groups": groups,
        "rules": [
            "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve", "IP-CIDR,172.16.0.0/12,DIRECT,no-resolve", "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve",
            "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve", "IP-CIDR,100.64.0.0/10,DIRECT,no-resolve", "IP-CIDR6,fc00::/7,DIRECT,no-resolve", "IP-CIDR6,fe80::/10,DIRECT,no-resolve",
            "GEOIP,RU,🇷🇺 Сайты РФ", f"MATCH,🔐 {brand}",
        ],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, width=1000) if yaml else json.dumps(cfg, ensure_ascii=False, indent=1)
    (OUT / u["sub"]).write_text(text); (OUT / u["sub"]).chmod(0o644)
    url = f"https://{V['DOMAIN']}/c/{u['sub']}"
    d = CL / name; (d / "clash.txt").write_text(url)
    subprocess.run(["qrencode", "-o", str(d / "clash.png"), "-s", "8", "-m", "3", "-l", "M", url], check=True)
    print(f"clash profile: {len(proxies)} proxies, {len(groups)} groups -> {url}")

if __name__ == "__main__":
    main()
