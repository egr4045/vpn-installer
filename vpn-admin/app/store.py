"""SQLite user store + traffic accounting.

Replaces the Remnawave database. Single source of truth for users; xray and
sing-box configs are rendered from here. Traffic is accumulated from
`xray api statsquery -reset` deltas (reset zeroes the counter each poll, so we
just add) and from sing-box clash_api deltas.
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import uuid as _uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import config

DB_PATH = os.path.join(config.DATA_DIR, "users.db")
FAR_FUTURE = "2099-01-01T00:00:00.000Z"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    vless_uuid    TEXT NOT NULL UNIQUE,
    short_uuid    TEXT NOT NULL UNIQUE,
    trojan_pass   TEXT NOT NULL,
    traffic_limit INTEGER NOT NULL DEFAULT 0,   -- bytes, 0 = unlimited
    used_up       INTEGER NOT NULL DEFAULT 0,
    used_down     INTEGER NOT NULL DEFAULT 0,
    expire_at     TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'ACTIVE', -- ACTIVE | DISABLED | LIMITED | EXPIRED
    created_at    TEXT NOT NULL,
    last_seen     TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sla_samples (
    ts   TEXT NOT NULL,
    kind TEXT NOT NULL          -- up | our_down | provider_down
);
CREATE INDEX IF NOT EXISTS idx_sla_ts ON sla_samples(ts);
CREATE TABLE IF NOT EXISTS metrics (
    ts  TEXT NOT NULL,
    key TEXT NOT NULL,           -- cpu_pct | mem_pct | disk_pct | net_rx_mbps | ...
    val REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metrics_key_ts ON metrics(key, ts);
"""


@contextmanager
def _conn():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["used_total"] = (d.get("used_up") or 0) + (d.get("used_down") or 0)
    return d


# ── reads ─────────────────────────────────────────────────────────────────────
def list_users() -> list[dict]:
    with _conn() as c:
        return [_row(r) for r in c.execute("SELECT * FROM users ORDER BY id")]


def _get(field: str, value) -> dict | None:
    with _conn() as c:
        r = c.execute(f"SELECT * FROM users WHERE {field}=?", (value,)).fetchone()
        return _row(r) if r else None


def get_by_uuid(u: str):      return _get("vless_uuid", u)
def get_by_short(s: str):     return _get("short_uuid", s)
def get_by_username(n: str):  return _get("username", n)
def get_by_id(i: int):        return _get("id", i)


# ── writes ──────────────────────────────────────────────────────────────────--
def _gen_short() -> str:
    # url-safe, ~16 chars, alphanumeric only (some clients dislike -/_)
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(16))


def create_user(username: str, traffic_limit: int = 0, expire_at: str = "",
                vless_uuid: str | None = None, short_uuid: str | None = None,
                trojan_pass: str | None = None, status: str = "ACTIVE",
                used_up: int = 0, used_down: int = 0,
                created_at: str | None = None) -> dict:
    vless_uuid = vless_uuid or str(_uuid.uuid4())
    short_uuid = short_uuid or _gen_short()
    trojan_pass = trojan_pass or secrets.token_urlsafe(24)
    with _conn() as c:
        c.execute(
            "INSERT INTO users (username, vless_uuid, short_uuid, trojan_pass, "
            "traffic_limit, used_up, used_down, expire_at, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (username, vless_uuid, short_uuid, trojan_pass, traffic_limit,
             used_up, used_down, expire_at or FAR_FUTURE, status,
             created_at or _now()),
        )
    return get_by_username(username)


def update_user(uid: int, *, traffic_limit: int | None = None,
                expire_at: str | None = None, status: str | None = None) -> None:
    sets, vals = [], []
    if traffic_limit is not None:
        sets.append("traffic_limit=?"); vals.append(traffic_limit)
    if expire_at is not None:
        sets.append("expire_at=?"); vals.append(expire_at or FAR_FUTURE)
    if status is not None:
        sets.append("status=?"); vals.append(status)
    if not sets:
        return
    vals.append(uid)
    with _conn() as c:
        c.execute(f"UPDATE users SET {','.join(sets)} WHERE id=?", vals)


def delete_user(uid: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM users WHERE id=?", (uid,))


def reset_traffic(uid: int) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET used_up=0, used_down=0 WHERE id=?", (uid,))


def reset_all_traffic() -> int:
    """Zero every user's counters (monthly billing-period rollover). Returns rows hit."""
    with _conn() as c:
        cur = c.execute("UPDATE users SET used_up=0, used_down=0")
        return cur.rowcount


# ── traffic accounting ──────────────────────────────────────────────────────--
def add_traffic(username: str, up: int = 0, down: int = 0) -> None:
    if not (up or down):
        return
    with _conn() as c:
        c.execute("UPDATE users SET used_up=used_up+?, used_down=used_down+? "
                  "WHERE username=?", (up, down, username))


def set_last_seen(username: str, ts: str | None = None) -> None:
    with _conn() as c:
        c.execute("UPDATE users SET last_seen=? WHERE username=?",
                  (ts or _now(), username))


# ── meta kv ───────────────────────────────────────────────────────────────────
def meta_get(key: str, default=None):
    with _conn() as c:
        r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def meta_set(key: str, value: str) -> None:
    with _conn() as c:
        c.execute("INSERT INTO meta (key,value) VALUES (?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# ── SLA samples ───────────────────────────────────────────────────────────────
# One sample per watchdog tick. kind: "up" | "our_down" (a core we own is broken
# while the internet is reachable) | "provider_down" (no external host reachable —
# NOT our fault, excluded from SLA). See nested-yawning-penguin.md Часть 3.1.
def record_sla(kind: str, retain_days: int = 60) -> None:
    with _conn() as c:
        c.execute("INSERT INTO sla_samples (ts,kind) VALUES (?,?)", (_now(), kind))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retain_days)
                  ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        c.execute("DELETE FROM sla_samples WHERE ts < ?", (cutoff,))


_metric_writes = 0


def record_metrics(samples: dict, retain_days: int = 30) -> None:
    """Append a row per non-None metric; prune occasionally (not every call)."""
    global _metric_writes
    rows = [(_now(), k, float(v)) for k, v in samples.items() if v is not None]
    if not rows:
        return
    with _conn() as c:
        c.executemany("INSERT INTO metrics (ts,key,val) VALUES (?,?,?)", rows)
        _metric_writes += 1
        if _metric_writes % 120 == 0:   # ~hourly at 30s cadence
            cutoff = (datetime.now(timezone.utc) - timedelta(days=retain_days)
                      ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            c.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))


def metric_series(key: str, since_iso: str, points: int = 120) -> list:
    """Return [(ts, val), …] for a metric, downsampled to ~`points`."""
    with _conn() as c:
        rows = c.execute("SELECT ts,val FROM metrics WHERE key=? AND ts>=? ORDER BY ts",
                         (key, since_iso)).fetchall()
    data = [(r["ts"], r["val"]) for r in rows]
    if len(data) > points:
        step = len(data) // points
        data = data[::step]
    return data


def sla_stats(since_iso: str) -> dict:
    """Availability since `since_iso`, excluding provider/internet outages.
    sla = 1 − our_down / (total − provider_down)."""
    with _conn() as c:
        rows = c.execute("SELECT kind, COUNT(*) n FROM sla_samples WHERE ts>=? "
                         "GROUP BY kind", (since_iso,)).fetchall()
    counts = {r["kind"]: r["n"] for r in rows}
    total = sum(counts.values())
    prov = counts.get("provider_down", 0)
    our = counts.get("our_down", 0)
    denom = total - prov
    sla = (1 - our / denom) if denom > 0 else 1.0
    return {"total": total, "up": counts.get("up", 0), "our_down": our,
            "provider_down": prov, "denom": denom, "sla": sla}
