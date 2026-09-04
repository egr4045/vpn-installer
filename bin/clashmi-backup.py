#!/usr/bin/env python3
"""clashmi-backup.py <name> — a ClashMi restore archive for one person.

ClashMi can restore a backup straight from a URL (restoreBackupFromUrl in the app), so one link
carries both the subscription and the settings that a desktop needs but the app does not ship with:
TUN on, its address out of the 172.16/12 block that Hyper-V hands itself, and no DNS override — the
app's own fake-ip-range collides with WSL's adapter, while the profile already carries 198.18.0.0/16.

Layout copied from an archive the app wrote itself: seven json at the root plus profiles/.
service.json is deliberately absent from ClashMi's own backups — it is machine state (control port,
API secret) — so `secret` and `external-controller` are left out here too rather than handing one
machine's API credentials to every other machine.

Served as https://DOMAIN/z/<token>.zip. Writes /root/clients/<name>/clashmi.txt and clashmi.png.
"""
import json, pathlib, subprocess, sys, zipfile

ETC = pathlib.Path("/etc/safechill"); CL = pathlib.Path("/root/clients")
SUB = pathlib.Path("/var/www/html/c"); OUT = pathlib.Path("/var/www/html/z")
PROBE = "https://www.gstatic.com/generate_204"

def load_env(p):
    env = {}
    for line in pathlib.Path(p).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1); env[k.strip()] = v.strip().strip("'\"")
    return env

CORE = {
    "mixed-port": 7890, "mode": "rule", "unified-delay": True,
    "log-level": "warning",            # the app defaults to "error", which leaves the core log empty
    "ipv6": False, "find-process-mode": "off",
    # no "global-client-fingerprint": removed from mihomo, and it errors at config.go:758 on every
    # start; each proxy in the profile carries its own client-fingerprint anyway
    "keep-alive-idle": 30, "keep-alive-interval": 30, "disable-keep-alive": False,
    "dns": {"overwrite": False},       # take the profile's DNS: DoH and fake-ip in 198.18.0.0/16
    "ntp": {"overwrite": False},
    "tun": {"overwrite": True, "enable": True, "device": "Clash Mi", "stack": "gvisor",
            "dns-hijack": ["0.0.0.0:53"], "auto-route": True, "auto-detect-interface": True,
            "mtu": 1280, "inet4-address": ["198.19.0.1/30"], "auto-redirect": False,
            "disable-icmp-forwarding": True},
    "sniffer": {"overwrite": False},   # the profile turns the sniffer on
    "tls": {"overwrite": False},
    "profile": {"store-selected": True, "store-fake-ip": True},
    "extension": {
        # the RU geoip list has to be fetched through the tunnel: raw.githubusercontent is blocked here
        "geo-rule-set": {"geosite_url": "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/refs/heads/meta/geo/geosite",
                         "geoip_url": "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/refs/heads/meta/geo/geoip",
                         "asn_url": "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/refs/heads/meta/asn",
                         "update-interval": 172800, "enable-proxy": True},
        "tun": {"http_proxy": {"enable": False}, "per_app": {"enable": False}},
    },
}
APP = {
    "language_tag": "ru", "ui": {"theme": "light", "auto_orientation": False, "disable_font_scaler": False,
                                 "hide_after_launch": False, "tv_mode": False, "perapp_hide_system_app": True,
                                 "perapp_hide_app_icon": False, "delay_test_sort": False},
    "webdav": {"url": "", "user": "", "password": ""},
    "alway_on": False, "log_level": "warning", "auto_update_channel": "stable",
    "auto_download_udpate_pkg": True, "auto_connect_after_launch": True, "auto_set_system_proxy": True,
    "system_proxy_bypass_domain": ["<-loopback>", "<local>", "localhost", "*.local", "127.*", "10.*",
                                   "172.16.*", "172.17.*", "172.18.*", "172.19.*", "172.2*", "172.30.*",
                                   "172.31.*", "192.168.*"],
    "user_agent": "", "board_online": False, "delay_test_url": PROBE, "delay_test_url_timeout": 5000,
    "hide_dock_icon": False, "show_tray_traffic": False, "exclude_from_recent": False,
    "wake_lock": False, "auto_connect_at_boot": True, "hide_vpn": False,
}

def main():
    if len(sys.argv) < 2: sys.exit("usage: clashmi-backup.py <name>")
    name = sys.argv[1]
    V = load_env(ETC / "vpn.env")
    users = json.loads((ETC / "users.json").read_text())
    u = next((x for x in users if x["name"] == name), None) or sys.exit(f"unknown user {name}")
    brand, domain, token = V.get("BRAND", "SafeChill"), V["DOMAIN"], u["sub"]
    profile = (SUB / token).read_text(encoding="utf-8")   # clash-sub.py must have run first
    pid = f"{int(token[:8], 16) % 10**9}.yaml"             # stable per person, and never collides
    files = {
        "setting.json": APP,
        "service_core_setting.json": CORE,
        "profiles.json": {"current_id": pid, "profiles": [{
            "id": pid, "remark": brand, "patch": "", "update_interval": 86400,
            "update_interval_by_profile": 43200, "update_interval_prefer_by_profile": True,
            "update": "", "url": f"https://{domain}/c/{token}",
            "user_agent": "ClashMeta/1.19.30; mihomo/1.19.30", "xhwid": False, "decrypt_password": "",
            "upload": 0, "download": 0, "total": 0, "expire": "", "board_provider_id": "",
            "overwrite_rules": False, "overwrite_proxy_groups": False, "proxy_groups": {},
            "rules": {}, "rules_for_proxy_groups": {}, "append_apple_push_rules": False}]},
        "profile_patchs.json": {"current_id": "", "profile_patchs": []},
        "providers.json": [],
        "board_sessions.json": {"headers_and_cookies": {}, "sessions": []},
        "diversion_template.json": {"rule-providers": [], "rule-templates": [], "proxygroup-templates": []},
    }
    OUT.mkdir(parents=True, exist_ok=True)
    z = OUT / f"{token}.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as a:
        for fn, body in files.items():
            a.writestr(fn, json.dumps(body, ensure_ascii=False, indent=2))
        a.writestr(f"profiles/{pid}", profile)
    z.chmod(0o644)
    live = {x["sub"] + ".zip" for x in users}
    for f in OUT.iterdir():
        if f.is_file() and f.name not in live: f.unlink(); print(f"retired stale archive {f.name}")
    url = f"https://{domain}/z/{token}.zip"
    d = CL / name; d.mkdir(parents=True, exist_ok=True)
    (d / "clashmi.txt").write_text(url)
    subprocess.run(["qrencode", "-o", str(d / "clashmi.png"), "-s", "8", "-m", "3", "-l", "M", url], check=True)
    print(f"clashmi backup: {z.stat().st_size} bytes, profile {pid} -> {url}")

if __name__ == "__main__":
    main()
