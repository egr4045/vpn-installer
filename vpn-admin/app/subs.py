"""Subscription link generation (raw base64 / Clash YAML / sing-box JSON).

Ported verbatim from the original admin, but parametrised by settings instead
of module-level constants so domains/ports/keys are configurable. Output is
byte-identical to the old links when settings match the previous box, so
existing clients keep working after the Remnawave drop.
"""
from __future__ import annotations

import base64
import json
from urllib.parse import quote

from . import config


def gen_sub_links(uuid: str, trojan_pass: str, name: str, cfg=None) -> list[str]:
    cfg = cfg or config.get_settings()
    ip = cfg.server_ip
    return [
        # Reality / Vision — real-site steal on dedicated port (anti-DPI)
        f"vless://{uuid}@{ip}:{cfg.reality_port}?encryption=none&security=reality"
        f"&flow=xtls-rprx-vision&pbk={cfg.reality_public_key}&sid={cfg.reality_short_id}"
        f"&sni={cfg.reality_sni}&type=tcp&fp=chrome#{quote('Reality-' + name)}",

        # VLESS httpUpgrade through CDN — works in Hiddify (sing-box) AND xray
        f"vless://{uuid}@{cfg.cdn_domain}:443?encryption=none&security=tls"
        f"&sni={cfg.cdn_domain}&host={cfg.cdn_domain}&type=httpupgrade&path={quote(cfg.hu_path, safe='')}"
        f"&fp=chrome#{quote('httpUpgrade-CDN-' + name)}",

        # Hysteria2 #1 — sing-box on :443
        f"hysteria2://{uuid}@{cfg.direct_domain}:{cfg.hy2_port}"
        f"?sni={cfg.hy2_sni}&insecure=0&alpn=h3#{quote('Hy2-' + name)}",

        # Hysteria2 #2 — xray on :8444 (fallback)
        f"hysteria2://{uuid}@{ip}:{cfg.hy2_port_x}"
        f"?sni={cfg.hy2_sni}&insecure=0&alpn=h3#{quote('Hy2-2-' + name)}",

        # TUIC — sing-box on :9443
        f"tuic://{uuid}:{trojan_pass}@{cfg.direct_domain}:{cfg.tuic_port}"
        f"?sni={cfg.tuic_sni}&congestion_control=bbr&udp_relay_mode=native"
        f"&alpn=h3#{quote('TUIC-' + name)}",

        # Raw VLESS/TCP
        f"vless://{uuid}@{ip}:{cfg.tcp_port}"
        f"?encryption=none&type=tcp#{quote('TCP-' + name)}",
    ]


def gen_sub_b64(uuid: str, trojan_pass: str, name: str, cfg=None) -> str:
    links = gen_sub_links(uuid, trojan_pass, name, cfg)
    return base64.b64encode("\n".join(links).encode()).decode()


def gen_clash_yaml(uuid: str, trojan_pass: str, name: str, cfg=None) -> str:
    cfg = cfg or config.get_settings()
    ip = cfg.server_ip
    n = name.replace('"', "")
    return f"""proxies:
  - name: "Reality-{n}"
    type: vless
    server: {ip}
    port: {cfg.reality_port}
    uuid: {uuid}
    flow: xtls-rprx-vision
    tls: true
    servername: {cfg.reality_sni}
    reality-opts:
      public-key: {cfg.reality_public_key}
      short-id: {cfg.reality_short_id}
    client-fingerprint: chrome
    network: tcp

  - name: "httpUpgrade-{n}"
    type: vless
    server: {cfg.cdn_domain}
    port: 443
    uuid: {uuid}
    tls: true
    servername: {cfg.cdn_domain}
    client-fingerprint: chrome
    network: httpupgrade
    http-upgrade-opts:
      path: {cfg.hu_path}
      headers:
        Host: {cfg.cdn_domain}

  - name: "Hy2-{n}"
    type: hysteria2
    server: {cfg.direct_domain}
    port: {cfg.hy2_port}
    password: {uuid}
    sni: {cfg.hy2_sni}
    up: "{cfg.hy2_up_mbps} Mbps"
    down: "{cfg.hy2_down_mbps} Mbps"
    alpn:
      - h3

  - name: "Hy2-2-{n}"
    type: hysteria2
    server: {ip}
    port: {cfg.hy2_port_x}
    password: {uuid}
    sni: {cfg.hy2_sni}
    up: "{cfg.hy2_up_mbps} Mbps"
    down: "{cfg.hy2_down_mbps} Mbps"
    alpn:
      - h3

  - name: "TUIC-{n}"
    type: tuic
    server: {cfg.direct_domain}
    port: {cfg.tuic_port}
    uuid: {uuid}
    password: {trojan_pass}
    alpn:
      - h3
    sni: {cfg.tuic_sni}
    congestion-controller: bbr
    udp-relay-mode: native

  - name: "TCP-{n}"
    type: vless
    server: {ip}
    port: {cfg.tcp_port}
    uuid: {uuid}
    network: tcp

proxy-groups:
  - name: PROXY
    type: url-test
    proxies:
      - "Reality-{n}"
      - "httpUpgrade-{n}"
      - "Hy2-{n}"
      - "Hy2-2-{n}"
      - "TUIC-{n}"
      - "TCP-{n}"
    url: https://www.gstatic.com/generate_204
    interval: 300
    tolerance: 50

rules:
  - MATCH,PROXY
"""


def gen_singbox_json(uuid: str, trojan_pass: str, name: str, cfg=None) -> str:
    cfg = cfg or config.get_settings()
    ip = cfg.server_ip
    n = name.replace('"', "").replace("'", "")
    tags = [f"Reality-{n}", f"httpUpgrade-{n}", f"Hy2-{n}", f"Hy2-2-{n}", f"TUIC-{n}", f"TCP-{n}"]
    cfg_doc = {
        "outbounds": [
            {"type": "urltest", "tag": "auto", "outbounds": tags,
             "url": "https://www.gstatic.com/generate_204", "interval": "10m0s", "tolerance": 50},
            {"type": "vless", "tag": f"Reality-{n}", "server": ip, "server_port": cfg.reality_port,
             "uuid": uuid, "flow": "xtls-rprx-vision",
             "tls": {"enabled": True, "server_name": cfg.reality_sni,
                     "reality": {"enabled": True, "public_key": cfg.reality_public_key,
                                 "short_id": cfg.reality_short_id},
                     "utls": {"enabled": True, "fingerprint": "chrome"}}},
            {"type": "vless", "tag": f"httpUpgrade-{n}", "server": cfg.cdn_domain, "server_port": 443,
             "uuid": uuid,
             "tls": {"enabled": True, "server_name": cfg.cdn_domain,
                     "utls": {"enabled": True, "fingerprint": "chrome"}},
             "transport": {"type": "httpupgrade", "path": cfg.hu_path, "host": cfg.cdn_domain}},
            {"type": "hysteria2", "tag": f"Hy2-{n}", "server": cfg.direct_domain, "server_port": cfg.hy2_port,
             "password": uuid, "up_mbps": cfg.hy2_up_mbps, "down_mbps": cfg.hy2_down_mbps,
             "tls": {"enabled": True, "server_name": cfg.hy2_sni, "alpn": ["h3"]}},
            {"type": "hysteria2", "tag": f"Hy2-2-{n}", "server": ip, "server_port": cfg.hy2_port_x,
             "password": uuid, "up_mbps": cfg.hy2_up_mbps, "down_mbps": cfg.hy2_down_mbps,
             "tls": {"enabled": True, "server_name": cfg.hy2_sni, "alpn": ["h3"]}},
            {"type": "tuic", "tag": f"TUIC-{n}", "server": cfg.direct_domain, "server_port": cfg.tuic_port,
             "uuid": uuid, "password": trojan_pass, "congestion_control": "bbr",
             "tls": {"enabled": True, "server_name": cfg.tuic_sni, "alpn": ["h3"]}},
            {"type": "vless", "tag": f"TCP-{n}", "server": ip, "server_port": cfg.tcp_port, "uuid": uuid},
        ]
    }
    return json.dumps(cfg_doc, ensure_ascii=False, indent=2)


def sub_headers(user: dict, short_uuid: str, cfg=None) -> dict:
    """Subscription-userinfo / profile headers for the sub responses."""
    cfg = cfg or config.get_settings()
    from datetime import datetime
    used = (user.get("used_total") or 0)
    limit = (user.get("traffic_limit") or 0)
    try:
        exp_dt = datetime.fromisoformat((user.get("expire_at") or "").replace("Z", "+00:00"))
        expire_ts = int(exp_dt.timestamp())
    except Exception:
        expire_ts = 0
    title_b64 = base64.b64encode(f"{cfg.brand} VPN - {user['username']}".encode()).decode()
    return {
        "content-disposition": f"attachment; filename={user['username']}",
        "profile-title": f"base64:{title_b64}",
        "profile-update-interval": "12",
        "profile-web-page-url": f"{cfg.sub_https_base}/{short_uuid}",
        "support-url": f"{cfg.sub_https_base}/{short_uuid}",
        "subscription-userinfo": f"upload=0; download={used}; total={limit}; expire={expire_ts}",
        # NOTE: no `access-control-allow-origin: *` — these responses carry the
        # user's VLESS uuid / trojan password. VPN clients fetch them server-side
        # (not via browser CORS), so omitting ACAO costs nothing and stops any
        # web page from reading a known sub URL's credentials cross-origin.
    }
