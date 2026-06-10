"""Render xray / sing-box configs from the user store + settings.

Single source of truth shared by the installer and the web admin. The xray
base template (`templates/xray.base.json`) is the proven-valid inbound layout
extracted from the live box; here we overlay settings-derived values (reality
keys, SNIs, ports, cert paths) and inject clients from the store.
"""
from __future__ import annotations

import json
import os

from . import config, store

_TEMPLATE_DIR = os.environ.get(
    "VPN_TEMPLATE_DIR",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "templates"),
)
XRAY_BASE = os.path.abspath(os.path.join(_TEMPLATE_DIR, "xray.base.json"))

# inbound tags that carry per-user clients
VLESS_TAGS = ("Steal", "VLESS_XHTTP", "VLESS_HTTPUPGRADE", "VLESS_TCP_2080", "RealitySteal2")
HY2_TAG = "Hysteria2"
CLIENT_TAGS = VLESS_TAGS + (HY2_TAG,)


def _vless_client(u: dict) -> dict:
    return {"id": u["vless_uuid"], "email": u["username"]}


def _hy2_client(u: dict) -> dict:
    return {"id": u["vless_uuid"], "auth": u["vless_uuid"], "email": u["username"]}


def _client_for(tag: str, u: dict) -> dict:
    return _hy2_client(u) if tag == HY2_TAG else _vless_client(u)


def _overlay_settings(inbounds: list[dict], cfg) -> None:
    """Apply settings-derived values onto the base inbounds, keyed by tag."""
    cert_dir = cfg.cert_dir
    for inb in inbounds:
        tag = inb.get("tag")
        ss = inb.get("streamSettings", {}) or {}
        if tag == "Steal":
            rs = ss.get("realitySettings", {})
            rs["privateKey"] = cfg.reality_private_key
            rs["shortIds"] = [cfg.reality_short_id]
            rs["serverNames"] = cfg.reality_server_names()
        elif tag == "RealitySteal2":
            inb["port"] = cfg.reality_port
            rs = ss.get("realitySettings", {})
            rs["privateKey"] = cfg.reality_private_key
            rs["shortIds"] = [cfg.reality_short_id]
            rs["serverNames"] = [cfg.reality_sni]
        elif tag == "VLESS_HTTPUPGRADE":
            hu = ss.get("httpupgradeSettings", {})
            hu["host"] = cfg.cdn_domain
            hu["path"] = cfg.hu_path
        elif tag == "VLESS_TCP_2080":
            inb["port"] = cfg.tcp_port
        elif tag == HY2_TAG:
            inb["port"] = cfg.hy2_port_x
            tls = ss.get("tlsSettings", {})
            tls["serverName"] = cfg.hy2_sni
            tls["certificates"] = [{
                "keyFile": f"{cert_dir}/privkey.pem",
                "certificateFile": f"{cert_dir}/fullchain.pem",
            }]
            # enable Hysteria2 "brutal" fixed-rate CC so throughput doesn't
            # crawl through BBR slow-start (xray's hysteria defaults to BBR)
            hs = ss.setdefault("hysteriaSettings", {})
            hs["brutalUp"] = cfg.hy2_up_mbps
            hs["brutalDown"] = cfg.hy2_down_mbps


def _load_base() -> dict:
    with open(XRAY_BASE) as f:
        return json.load(f)


def build_xray_config(users: list[dict] | None = None, cfg=None) -> dict:
    """Full xray config with ACTIVE users injected into every client inbound."""
    cfg = cfg or config.get_settings()
    if users is None:
        users = store.list_users()
    active = [u for u in users if u.get("status") == "ACTIVE"]
    doc = _load_base()
    _overlay_settings(doc["inbounds"], cfg)
    for inb in doc["inbounds"]:
        if inb.get("tag") in CLIENT_TAGS:
            inb.setdefault("settings", {})["clients"] = [
                _client_for(inb["tag"], u) for u in active
            ]
    return doc


def build_adu_config(user: dict, cfg=None) -> dict:
    """Minimal config used by `xray api adu` to hot-add ONE user to all inbounds.

    Reuses the proven inbound layout (adu rebuilds each inbound to extract its
    clients, so the inbound def must be valid) but with a single client.
    """
    cfg = cfg or config.get_settings()
    doc = _load_base()
    _overlay_settings(doc["inbounds"], cfg)
    inbounds = []
    for inb in doc["inbounds"]:
        if inb.get("tag") in CLIENT_TAGS:
            inb.setdefault("settings", {})["clients"] = [_client_for(inb["tag"], user)]
            inbounds.append(inb)
    return {"inbounds": inbounds}


def write_xray_config(users: list[dict] | None = None, cfg=None) -> str:
    cfg = cfg or config.get_settings()
    doc = build_xray_config(users, cfg)
    path = cfg.xray_cfg
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return path


# ── sing-box (Hy2 :443 + TUIC :9443) ──────────────────────────────────────────
def write_singbox_config(users: list[dict] | None = None, cfg=None) -> str | None:
    """Sync sing-box inbound user lists from the store. Returns path or None."""
    cfg = cfg or config.get_settings()
    path = cfg.sing_box_cfg
    if not os.path.exists(path):
        return None
    if users is None:
        users = store.list_users()
    active = [u for u in users if u.get("status") == "ACTIVE"]
    with open(path) as f:
        doc = json.load(f)
    # `name` lets sing-box attribute clash_api connections to a user for stats
    tuic_users = [{"name": u["username"], "uuid": u["vless_uuid"],
                   "password": u["trojan_pass"]} for u in active]
    hy2_users = [{"name": u["username"], "password": u["vless_uuid"]}  # Hy2 auth = vlessUuid
                 for u in active]
    for inb in doc.get("inbounds", []):
        if inb.get("tag") == "tuic-in":
            inb["users"] = tuic_users
        elif inb.get("tag") == "hy2-in":
            inb["users"] = hy2_users
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=2)
    os.replace(tmp, path)
    return path
