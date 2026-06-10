"""Control sing-box (Hy2 :443 + TUIC :9443) and read per-user traffic.

sing-box has no per-user cumulative counter like xray's StatsService, so we
poll the Clash API `/connections` endpoint: each live connection reports
cumulative upload/download and `metadata.user` (set from the user's `name`).
We accumulate per-connection deltas into per-user totals. Hy2/TUIC tunnels are
long-lived, so the only loss is connections that open AND close entirely
between two polls.
"""
from __future__ import annotations

import re
import subprocess
import time

import httpx

from . import config

# in-memory per-connection cumulative baseline: {conn_id: (upload, download)}
_LAST: dict[str, tuple[int, int]] = {}
# persistent "srcip:srcport" -> (username, last_seen_ts); sing-box tags neither
# the clash /connections nor a stats API with the user, but its INFO log does:
#   inbound/hysteria2[hy2-in]: inbound connection from <ip:port>
#   inbound/hysteria2[hy2-in]: [User] inbound connection to <dest>
# (same numeric log-id). One client UDP port == one user tunnel, so srcip:port
# is a stable key we can join against /connections.metadata.{sourceIP,sourcePort}.
_PORT_USER: dict[str, tuple[str, float]] = {}
_PORT_USER_TTL = 3600
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_RE_FROM = re.compile(r"\[(\d+)\b.*?inbound connection from (\d+\.\d+\.\d+\.\d+):(\d+)")
_RE_USER = re.compile(r"\[(\d+)\b.*?\[([A-Za-z0-9_.-]+)\] inbound connection to")


def _cfg():
    return config.get_settings()


def _clash_base() -> str:
    return f"http://{_cfg().singbox_clash_api}"


# ── reload ────────────────────────────────────────────────────────────────────
def reload() -> tuple[bool, str]:
    """Apply config changes. sing-box has no in-place hot reload (SIGHUP just
    terminates it), so we restart the container — Hy2/TUIC clients reconnect."""
    return restart()


def restart() -> tuple[bool, str]:
    cfg = _cfg()
    try:
        p = subprocess.run(["docker", "restart", cfg.singbox_container],
                           capture_output=True, text=True, timeout=60)
        return p.returncode == 0, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return False, str(e)


def _refresh_port_user(window: str = "5m") -> None:
    """Update the srcip:port -> user map from recent sing-box logs."""
    try:
        p = subprocess.run(["docker", "logs", "--since", window, _cfg().singbox_container],
                           capture_output=True, text=True, timeout=15)
        logs = _ANSI.sub("", (p.stdout or "") + (p.stderr or ""))
    except Exception:
        return
    id_src, id_user = {}, {}
    for line in logs.splitlines():
        m = _RE_FROM.search(line)
        if m:
            id_src[m.group(1)] = f"{m.group(2)}:{m.group(3)}"
        m2 = _RE_USER.search(line)
        if m2:
            id_user[m2.group(1)] = m2.group(2)
    now = time.time()
    for lid, key in id_src.items():
        if lid in id_user:
            _PORT_USER[key] = (id_user[lid], now)
    # prune stale entries
    for k in [k for k, (_u, ts) in _PORT_USER.items() if now - ts > _PORT_USER_TTL]:
        _PORT_USER.pop(k, None)


# ── per-user traffic ──────────────────────────────────────────────────────────
def query_user_stats() -> dict[str, dict[str, int]]:
    """Return {username: {'up','down'}} deltas since last poll for Hy2/TUIC.

    Bytes come from clash /connections; the user is resolved by joining
    metadata.sourceIP:sourcePort against the log-derived port->user map.
    """
    out: dict[str, dict[str, int]] = {}
    _refresh_port_user()
    try:
        r = httpx.get(f"{_clash_base()}/connections", timeout=5)
        conns = r.json().get("connections") or []
    except Exception:
        return out
    seen = set()
    for c in conns:
        cid = c.get("id")
        if not cid:
            continue
        seen.add(cid)
        up = int(c.get("upload", 0) or 0)
        down = int(c.get("download", 0) or 0)
        last_up, last_down = _LAST.get(cid, (0, 0))
        d_up, d_down = max(0, up - last_up), max(0, down - last_down)
        _LAST[cid] = (up, down)
        md = c.get("metadata") or {}
        key = f"{md.get('sourceIP','')}:{md.get('sourcePort','')}"
        user = md.get("user") or (_PORT_USER.get(key, (None,))[0])
        if not user:
            continue
        slot = out.setdefault(user, {"up": 0, "down": 0})
        slot["up"] += d_up
        slot["down"] += d_down
    for cid in list(_LAST.keys()):
        if cid not in seen:
            _LAST.pop(cid, None)
    return out


def api_ok() -> bool:
    try:
        httpx.get(f"{_clash_base()}/version", timeout=3)
        return True
    except Exception:
        return False
