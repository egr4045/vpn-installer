#!/usr/bin/env python3
"""clash-sub.py <name> — a complete mihomo (Clash Meta) profile for one person: every xray server
(XHTTP+REALITY and TCP fallbacks on all nodes, IPv4 and IPv6), AmneziaWG 3.1 on both exits, an
auto group that prefers the RU entry and falls back to Amsterdam, Russian sites direct except the services
whose CDN lives inside Russian ISPs (FORCE_PROXY below), DNS over HTTPS.
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

# Services whose CDN answers with caches inside Russian ISPs, which are exactly the throttled or blocked
# path: measured from Moscow, www.tiktok.com -> 164.215.74.x (AS29076 RU), tiktokcdn/ttwstatic ->
# 93.191.15.x (AS51369 RU), googlevideo -> the provider's own GGC. A bare GEOIP,RU rule therefore sends
# them DIRECT and the app hangs half-loaded, so they are matched by domain before GEOIP ever runs.
FORCE_PROXY = [
    "tiktok.com", "tiktokv.com", "tiktokv.us", "tiktokcdn.com", "tiktokcdn-us.com", "tiktokcdn-eu.com",
    "tiktokcdn-in.com", "ttwstatic.com", "ttlivecdn.com", "byteoversea.com", "ibytedtos.com", "byteimg.com",
    "bytedapm.com", "isnssdk.com", "snssdk.com", "muscdn.com", "musical.ly", "capcut.com",
    "youtube.com", "youtu.be", "googlevideo.com", "ytimg.com", "ggpht.com", "youtubei.googleapis.com",
    "instagram.com", "cdninstagram.com", "facebook.com", "fbcdn.net", "whatsapp.com", "whatsapp.net",
    "twitter.com", "x.com", "twimg.com", "t.co",
    "discord.com", "discord.gg", "discord.media", "discordapp.com", "discordapp.net",
    "openai.com", "chatgpt.com", "oaistatic.com", "oaiusercontent.com", "claude.ai", "anthropic.com",
    "linkedin.com", "medium.com", "signal.org", "spotify.com", "scdn.co", "soundcloud.com",
]

# The mirror image: Russian sites leave from the user's own provider. Through the tunnel they take a
# Moscow -> Amsterdam -> Moscow hairpin (+80 ms measured), and a bank or gosuslugi that sees a Dutch
# address tends to refuse the session outright. GEOIP,RU below is only a backstop: under fake-ip it
# fires after a resolution that may never geolocate right, so the names are matched first. Anything
# here that is blocked INSIDE Russia belongs in FORCE_PROXY instead — that list is matched earlier.
RU_DIRECT = [
    "ru", "xn--p1ai", "su",                       # .ru, .рф, .su wholesale
    "vk.com", "vk.me", "vk-cdn.net", "vkuser.net", "vkuservideo.net", "userapi.com", "mycdn.me",
    "yandex.net", "yandex.com", "ya.cc", "avito.st", "ozonusercontent.com", "2gis.com", "sberbank.com",
]

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
    apex = ".".join(V["DOMAIN"].split(".")[-2:])
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

    proxies, auto_order, spare_order, awg_order = [], [], [], []
    def add(pr, *buckets):
        proxies.append(pr)
        for b in buckets: b.append(pr["name"])
    # Short names on purpose: ClashMi prints the live member next to its group and truncates hard.
    # auto_order is the failover order of "Авто"; spare_order is the same servers without the Moscow
    # hop. IPv6 goes last — desktop ClashMi ships with ipv6 off, so there it only costs one probe.
    ru, ru_sni = V.get("RU_HOST"), V.get("RU_SNI", "yandex.ru")
    if ru: add(xhttp("🇷🇺 Россия", ru, ru_sni, "chrome"), auto_order)
    add(xhttp("🇳🇱 Амстердам", V["SERVER_IP"], V["DOMAIN"], "firefox"), auto_order, spare_order)
    if V.get("EXIT2_HOST"): add(xhttp("🇳🇱 Амстердам-2", V["EXIT2_HOST"], V["EXIT2_DOMAIN"], "firefox"), auto_order, spare_order)
    if ru: add(tcp("🇷🇺 Россия TCP", ru, ru_sni, "chrome"), auto_order)
    add(tcp("🇳🇱 Амстердам TCP", V["SERVER_IP"], fallback_sni, "chrome"), auto_order, spare_order)
    if V.get("EXIT2_HOST"): add(tcp("🇳🇱 Амстердам-2 TCP", V["EXIT2_HOST"], fallback_sni, "chrome"), auto_order, spare_order)
    if ru and V.get("RU_HOST6"): add(xhttp("🇷🇺 Россия IPv6", V["RU_HOST6"], ru_sni, "chrome"), auto_order)
    if V.get("SERVER_IP6"): add(xhttp("🇳🇱 Амстердам IPv6", V["SERVER_IP6"], V["DOMAIN"], "firefox"), auto_order, spare_order)
    if P:
        add(awg("⚡ AWG Амстердам", V["SERVER_IP"]), awg_order)
        if V.get("EXIT2_HOST"): add(awg("⚡ AWG Амстердам-2", V["EXIT2_HOST"]), awg_order)
        # no AWG over IPv6: UDP to the exit's IPv6 does not pass from Russian networks in tests

    AUTO, FAST, SPARE = "⚡ Авто", "🚀 Макс. скорость", "🛟 Резерв"
    probe = {"url": PROBE, "interval": 120, "timeout": 3000, "lazy": False}
    # Three choices and nothing else on the Proxy screen. Each one is a fallback chain, so no pick is
    # a dead end: Макс. скорость ends in Авто because UDP to the exit is filtered on some networks,
    # and Резерв drops the Moscow entry for when that entry is the thing that broke.
    groups = [
        {"name": f"🔐 {brand}", "type": "select", "proxies": [AUTO] + ([FAST] if awg_order else []) + [SPARE]},
        {"name": AUTO, "type": "fallback", "proxies": auto_order, **probe},
        {"name": SPARE, "type": "fallback", "proxies": spare_order, **probe},
    ]
    if awg_order:
        groups.insert(2, {"name": FAST, "type": "fallback", "proxies": awg_order + [AUTO], **probe})

    cfg = {
        "mixed-port": 7890, "allow-lan": False, "mode": "rule", "log-level": "warning", "ipv6": True,
        "unified-delay": True, "tcp-concurrent": True, "find-process-mode": "off",
        "geodata-mode": False, "geo-auto-update": True, "geo-update-interval": 168,
        "profile": {"store-selected": True, "store-fake-ip": True},
        "sniffer": {"enable": True, "sniff": {"HTTP": {"ports": [80, 8080]}, "TLS": {"ports": [443, 8443]}}},
        # respect-rules puts every lookup through the same rules as the traffic it is for. Without it the
        # DoH servers are dialled straight from the user's Russian provider, where both 1.1.1.1 and
        # dns.google are throttled, and each first visit to a domain waits out that timeout. With it they
        # ride the tunnel, while a .ru name is answered DIRECT by a Russian resolver — so Russian CDNs
        # still return their nearest cache and not one chosen for Amsterdam.
        # 198.18 and not 198.19: ClashMi puts its own TUN interface on 198.19.0.1/30, and a fake IP handed
        # out from inside that subnet is answered by the interface instead of being routed anywhere. The
        # two must not overlap, and it is the pool that has to move — the interface address is the app's.
        "dns": {"enable": True, "ipv6": True, "enhanced-mode": "fake-ip", "fake-ip-range": "198.18.0.1/16",
                "fake-ip-filter": ["*.lan", "*.local", "localhost.ptlogin2.qq.com", "+.msftconnecttest.com", "+.msftncsi.com", "+.pool.ntp.org"],
                "respect-rules": True,
                "default-nameserver": ["77.88.8.8", "1.1.1.1", "8.8.8.8"],
                "nameserver": ["https://1.1.1.1/dns-query", "https://dns.google/dns-query"],
                "nameserver-policy": {"+.ru,+.xn--p1ai,+.su": ["77.88.8.8", "77.88.8.1"]},
                "proxy-server-nameserver": ["https://1.1.1.1/dns-query", "https://dns.google/dns-query"]},
        "proxies": proxies, "proxy-groups": groups,
        "rules": [
            # our own domain direct, so the profile can always be refreshed even when every tunnel is down
            f"DOMAIN-SUFFIX,{apex},DIRECT",
        ] + [f"IP-CIDR{'6' if ':' in h else ''},{h}/{128 if ':' in h else 32},DIRECT,no-resolve"
             # anything else aimed at a node itself — ssh, the subscription, a health probe — would
             # otherwise hairpin: into the tunnel, out at the exit, back across Europe to that node
             for h in dict.fromkeys(filter(None, [V.get("SERVER_IP"), V.get("SERVER_IP6"), V.get("EXIT2_HOST"),
                                                  V.get("RU_HOST"), V.get("RU_HOST6")]))] + [
            # the Russian resolver that nameserver-policy sends .ru lookups to, pinned direct: answered
            # from Amsterdam it would hand back the CDN node nearest Amsterdam for every Russian site
            "IP-CIDR,77.88.8.0/24,DIRECT,no-resolve",
            "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve", "IP-CIDR,172.16.0.0/12,DIRECT,no-resolve", "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve",
            "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve", "IP-CIDR,100.64.0.0/10,DIRECT,no-resolve", "IP-CIDR6,fc00::/7,DIRECT,no-resolve", "IP-CIDR6,fe80::/10,DIRECT,no-resolve",
        ] + [f"DOMAIN-SUFFIX,{d},🔐 {brand}" for d in FORCE_PROXY]
          + [f"DOMAIN-SUFFIX,{d},DIRECT" for d in RU_DIRECT] + [
            "GEOIP,RU,DIRECT", f"MATCH,🔐 {brand}",
        ],
    }
    OUT.mkdir(parents=True, exist_ok=True)
    # Tokens change when users.json is regenerated, and a left-over file keeps serving a UUID xray no
    # longer knows: the client then fails REALITY silently on every server. Better a 404 than that.
    live = {x["sub"] for x in users}
    attic = pathlib.Path("/root/attic/old-clash-subs")
    for f in OUT.iterdir():
        if f.is_file() and f.name not in live:
            attic.mkdir(parents=True, exist_ok=True); f.rename(attic / f.name)
            print(f"retired stale profile {f.name}")
    text = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, width=1000) if yaml else json.dumps(cfg, ensure_ascii=False, indent=1)
    (OUT / u["sub"]).write_text(text); (OUT / u["sub"]).chmod(0o644)
    url = f"https://{V['DOMAIN']}/c/{u['sub']}"
    d = CL / name; (d / "clash.txt").write_text(url)
    subprocess.run(["qrencode", "-o", str(d / "clash.png"), "-s", "8", "-m", "3", "-l", "M", url], check=True)
    print(f"clash profile: {len(proxies)} proxies, {len(groups)} groups -> {url}")

if __name__ == "__main__":
    main()
