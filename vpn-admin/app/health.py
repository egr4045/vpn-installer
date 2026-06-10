"""Server health checks + client healthcheck ingest. Ported from the original
admin; protocol ports and cert SNIs derive from settings, and the old
Remnawave node-status line is replaced by xray/sing-box core status."""
from __future__ import annotations

import datetime as _dt
import json
import os
import shutil as _shutil
import socket as _socket
import ssl as _ssl
import time as _time
from collections import deque

from . import config

HEALTH_FILE = os.path.join(config.DATA_DIR, "health.json")
HEALTH_REPORTS = deque(maxlen=60)


def _load_health():
    try:
        for r in json.load(open(HEALTH_FILE)):
            HEALTH_REPORTS.append(r)
    except Exception:
        pass


def save_health():
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        json.dump(list(HEALTH_REPORTS), open(HEALTH_FILE, "w"))
    except Exception:
        pass


_load_health()


def proto_ports() -> list[tuple[str, int, str]]:
    cfg = config.get_settings()
    return [
        ("Reality / xHTTP / httpUpgrade (xray)", 443, "tcp"),
        ("Reality steal 2 (xray)", cfg.reality_port, "tcp"),
        ("VLESS-TCP", cfg.tcp_port, "tcp"),
        ("Hysteria2 #1 (sing-box)", cfg.hy2_port, "udp"),
        ("Hysteria2 #2 (xray)", cfg.hy2_port_x, "udp"),
        ("TUIC (sing-box)", cfg.tuic_port, "udp"),
    ]


def _port_listening(port: int, proto: str) -> bool:
    files = {"tcp": ["/proc/net/tcp", "/proc/net/tcp6"],
             "udp": ["/proc/net/udp", "/proc/net/udp6"]}[proto]
    hexport = f"{port:04X}"
    for f in files:
        try:
            for line in open(f).read().splitlines()[1:]:
                p = line.split()
                if p[1].split(":")[1] == hexport and (proto == "udp" or p[3] == "0A"):
                    return True
        except Exception:
            pass
    return False


def _tcp_probe(host: str, port: int, timeout: float = 3):
    t = _time.time()
    try:
        s = _socket.create_connection((host, port), timeout)
        s.close()
        return round((_time.time() - t) * 1000)
    except Exception:
        return None


def _cert_days(sni: str):
    ctx = _ssl.create_default_context()
    try:
        with _socket.create_connection(("127.0.0.1", 443), 5) as s:
            with ctx.wrap_socket(s, server_hostname=sni) as ss:
                na = ss.getpeercert()["notAfter"]
        exp = _dt.datetime.strptime(na, "%b %d %H:%M:%S %Y %Z")
        return (exp - _dt.datetime.utcnow()).days
    except Exception:
        return None


def _meminfo() -> dict:
    d = {}
    try:
        for line in open("/proc/meminfo"):
            k, v = line.split(":")
            d[k.strip()] = int(v.strip().split()[0])
    except Exception:
        pass
    mt, ma = d.get("MemTotal", 0), d.get("MemAvailable", 0)
    st, sf = d.get("SwapTotal", 0), d.get("SwapFree", 0)
    return {
        "mem_total_mb": mt // 1024, "mem_avail_mb": ma // 1024,
        "mem_used_pct": round((mt - ma) / mt * 100) if mt else 0,
        "swap_total_mb": st // 1024, "swap_used_mb": (st - sf) // 1024,
        "swap_used_pct": round((st - sf) / st * 100) if st else 0,
    }


def server_health() -> dict:
    cfg = config.get_settings()
    protos = []
    for label, port, proto in proto_ports():
        up = _port_listening(port, proto)
        lat = _tcp_probe("127.0.0.1", port) if proto == "tcp" else None
        protos.append({"label": label, "port": port, "proto": proto, "up": up, "lat_ms": lat})
    mem = _meminfo()
    cert_snis = [d["domain"] for d in cfg.domains if d.get("role") in ("direct", "cdn")]
    certs = {sni: _cert_days(sni) for sni in cert_snis}

    alerts = []
    for p in protos:
        if not p["up"]:
            alerts.append(("crit", f"proto:{p['port']}:{p['proto']}",
                           f"{p['label']} — порт {p['port']}/{p['proto']} не слушается"))
    if mem.get("swap_used_pct", 0) >= 80:
        alerts.append(("warn", "swap", f"Своп забит на {mem['swap_used_pct']}% ({mem['swap_used_mb']}МБ)"))
    if mem.get("mem_avail_mb", 9999) < 120:
        alerts.append(("warn", "ram", f"Мало свободной RAM: {mem['mem_avail_mb']}МБ"))
    disk = disk_usage()
    if disk.get("used_pct", 0) >= 90:
        alerts.append(("crit", "disk", f"Диск заполнен на {disk['used_pct']}% (свободно {disk['free_gb']}ГБ)"))
    elif disk.get("used_pct", 0) >= 80:
        alerts.append(("warn", "disk", f"Диск заполнен на {disk['used_pct']}% (свободно {disk['free_gb']}ГБ)"))
    for sni, days in certs.items():
        if days is not None and days < 14:
            alerts.append(("warn", f"cert:{sni}", f"Сертификат {sni} истекает через {days} дн."))
        elif days is None:
            alerts.append(("warn", f"certfail:{sni}", f"Не удалось проверить сертификат {sni}"))
    return {"ts": _dt.datetime.utcnow().isoformat() + "Z", "protocols": protos,
            "mem": mem, "certs": certs, "disk": disk, "alerts": alerts}


_LAST_OUTBOUND = {"ts": None, "reachable": None, "total": 0, "targets": []}


def _parse_hostport(s: str, default_port: int = 443) -> tuple[str, int]:
    host, sep, port = s.rpartition(":")
    if not sep:
        return s, default_port
    try:
        return host, int(port)
    except ValueError:
        return s, default_port


def outbound_probe(retry: bool = True) -> dict:
    """Active outbound reachability check: TCP-connect to a few external anycast
    IPs. All-fail => uplink down / packet-loss — the core symptom of a provider
    blip that drops every protocol at once (QUIC/Hy2/TUIC first). On a full miss
    we re-check once after 2s to debounce a single transient failure."""
    cfg = config.get_settings()
    targets = cfg.get("outbound_probe_targets") or ["1.1.1.1:443", "8.8.8.8:443"]
    res = []
    for t in targets:
        host, port = _parse_hostport(t)
        ms = _tcp_probe(host, port, timeout=2)
        res.append({"target": t, "ok": ms is not None, "lat_ms": ms})
    reachable = sum(1 for r in res if r["ok"])
    if reachable == 0 and retry:
        _time.sleep(2)
        return outbound_probe(retry=False)
    out = {"ts": _dt.datetime.utcnow().isoformat() + "Z",
           "reachable": reachable, "total": len(res), "targets": res}
    _LAST_OUTBOUND.update(out)
    return out


def last_outbound() -> dict:
    return dict(_LAST_OUTBOUND)


_LAST_REACH: dict = {"ts": None, "reachable": None, "total": 0, "pct": None, "targets": []}


def reachability_probe() -> dict:
    """Partial-outage check: TCP-connect to several REAL, network-diverse service
    endpoints (Twitch/AWS, Google, Discord, Steam, MS, Cloudflare) in parallel.
    A provider transit fault can blackhole some of these while anycast (1.1.1.1
    etc.) stays up — that's invisible to outbound_probe but very visible to users.
    Returns reachable/total + pct + per-target detail. Parallel, so total wall
    time ≈ one timeout even if everything is down."""
    from concurrent.futures import ThreadPoolExecutor
    cfg = config.get_settings()
    targets = cfg.get("reachability_targets") or []
    timeout = cfg.get("reachability_timeout_sec", 4)
    if not targets:
        return dict(_LAST_REACH)

    def _one(t):
        host, port = _parse_hostport(t, 443)
        ms = _tcp_probe(host, port, timeout=timeout)
        return {"target": host, "ok": ms is not None, "lat_ms": ms}

    with ThreadPoolExecutor(max_workers=len(targets)) as ex:
        res = list(ex.map(_one, targets))
    reachable = sum(1 for r in res if r["ok"])
    out = {"ts": _dt.datetime.utcnow().isoformat() + "Z", "reachable": reachable,
           "total": len(res), "pct": round(reachable / len(res) * 100) if res else None,
           "targets": res}
    _LAST_REACH.update(out)
    return out


def last_reach() -> dict:
    return dict(_LAST_REACH)


def core_status() -> dict:
    """Are the xray / sing-box cores alive? (api reachability)."""
    from . import singbox_ctl, xray_ctl
    return {"xray": xray_ctl.api_ok(), "singbox": singbox_ctl.api_ok()}


# ── extended metrics (host-network container reads host /proc, overlay = host disk) ──
_RATE_PREV: dict = {}   # state for delta/rate calculations


def cpu_pct():
    """System-wide CPU utilisation % since the previous call (None on first call)."""
    try:
        parts = list(map(int, open("/proc/stat").readline().split()[1:]))
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        total = sum(parts)
    except Exception:
        return None
    prev = _RATE_PREV.get("cpu")
    _RATE_PREV["cpu"] = (idle, total)
    if not prev:
        return None
    di, dt = idle - prev[0], total - prev[1]
    return round((1 - di / dt) * 100, 1) if dt > 0 else None


def loadavg() -> list[float]:
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except Exception:
        return [0, 0, 0]


def disk_usage() -> dict:
    try:
        u = _shutil.disk_usage("/")
        g = 1024 ** 3
        return {"total_gb": round(u.total / g, 1), "used_gb": round(u.used / g, 1),
                "free_gb": round(u.free / g, 1), "used_pct": round(u.used / u.total * 100)}
    except Exception:
        return {"total_gb": 0, "used_gb": 0, "free_gb": 0, "used_pct": 0}


def _net_dev_totals() -> tuple[int, int, int, int]:
    rx = tx = rxp = txp = 0
    try:
        for line in open("/proc/net/dev").read().splitlines()[2:]:
            iface, _, rest = line.partition(":")
            if iface.strip() in ("lo",):
                continue
            f = rest.split()
            rx += int(f[0]); rxp += int(f[1]); tx += int(f[8]); txp += int(f[9])
    except Exception:
        pass
    return rx, tx, rxp, txp


def net_throughput() -> dict:
    """rx/tx Mbit/s + packets/s since previous call (None-ish on first)."""
    rx, tx, rxp, txp = _net_dev_totals()
    now = _time.time()
    prev = _RATE_PREV.get("net")
    _RATE_PREV["net"] = (rx, tx, rxp, txp, now)
    if not prev:
        return {"rx_mbps": None, "tx_mbps": None, "pps": None}
    dt = now - prev[4]
    if dt <= 0:
        return {"rx_mbps": None, "tx_mbps": None, "pps": None}
    return {"rx_mbps": round((rx - prev[0]) * 8 / 1e6 / dt, 2),
            "tx_mbps": round((tx - prev[1]) * 8 / 1e6 / dt, 2),
            "pps": round(((rxp - prev[2]) + (txp - prev[3])) / dt)}


def _snmp_udp() -> dict:
    try:
        for line in open("/proc/net/snmp"):
            if line.startswith("Udp: ") and line.split()[1].isdigit():
                f = line.split()
                return {"InErrors": int(f[3]), "RcvbufErrors": int(f[5])}
    except Exception:
        pass
    return {"InErrors": 0, "RcvbufErrors": 0}


def udp_drop_rate() -> float | None:
    """UDP RcvbufErrors/sec since previous call (QUIC/Hy2/TUIC drop pressure)."""
    cur = _snmp_udp().get("RcvbufErrors", 0)
    now = _time.time()
    prev = _RATE_PREV.get("udp")
    _RATE_PREV["udp"] = (cur, now)
    if not prev:
        return None
    dt = now - prev[1]
    return round((cur - prev[0]) / dt, 2) if dt > 0 else None


def collect_metrics() -> dict:
    """Snapshot of rate/gauge metrics for the time-series sampler."""
    mem = _meminfo()
    net = net_throughput()
    return {
        "cpu_pct": cpu_pct(),
        "mem_pct": mem.get("mem_used_pct"),
        "swap_pct": mem.get("swap_used_pct"),
        "disk_pct": disk_usage().get("used_pct"),
        "net_rx_mbps": net.get("rx_mbps"),
        "net_tx_mbps": net.get("tx_mbps"),
        "udp_drop_rate": udp_drop_rate(),
    }
