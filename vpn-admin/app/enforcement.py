"""Traffic accounting + limit/expiry enforcement (replaces Remnawave's).

Runs on a timer. Each tick:
  1. pull per-user traffic deltas from xray + sing-box, accumulate into the store;
  2. refresh last_seen for users with traffic this tick (or reported online);
  3. disable users that crossed their traffic limit (LIMITED) or expired (EXPIRED),
     hot-removing them from xray (rmu) and rewriting/reloading sing-box;
  4. re-enable users whose limit was raised / topped up / expiry extended.

DISABLED (manual) is never auto-touched; only LIMITED/EXPIRED auto-recover.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import render, singbox_ctl, store, xray_ctl


def _now():
    return datetime.now(timezone.utc)


def _parse_iso(s: str):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _is_expired(u: dict) -> bool:
    exp = _parse_iso(u.get("expire_at", ""))
    return exp is not None and _now() > exp


def _over_limit(u: dict) -> bool:
    lim = u.get("traffic_limit") or 0
    return lim > 0 and (u.get("used_total") or 0) >= lim


def collect_traffic() -> None:
    """Accumulate per-user deltas from both cores into the store."""
    for username, d in xray_ctl.query_user_stats(reset=True).items():
        store.add_traffic(username, up=d.get("up", 0), down=d.get("down", 0))
        if d.get("up") or d.get("down"):
            store.set_last_seen(username)
    for username, d in singbox_ctl.query_user_stats().items():
        store.add_traffic(username, up=d.get("up", 0), down=d.get("down", 0))
        if d.get("up") or d.get("down"):
            store.set_last_seen(username)
    for username in xray_ctl.online_users():
        store.set_last_seen(username)


def enforce() -> bool:
    """Apply limit/expiry transitions. Returns True if sing-box needs reload."""
    singbox_dirty = False
    for u in store.list_users():
        st = u.get("status")
        if st == "ACTIVE":
            new = None
            if _is_expired(u):
                new = "EXPIRED"
            elif _over_limit(u):
                new = "LIMITED"
            if new:
                store.update_user(u["id"], status=new)
                xray_ctl.remove_user(u["username"])
                singbox_dirty = True
        elif st in ("LIMITED", "EXPIRED"):
            # auto-recover if the cause is gone
            recovered = (st == "LIMITED" and not _over_limit(u)) or \
                        (st == "EXPIRED" and not _is_expired(u))
            if recovered:
                store.update_user(u["id"], status="ACTIVE")
                xray_ctl.add_user(store.get_by_id(u["id"]))
                singbox_dirty = True
    return singbox_dirty


def tick() -> None:
    collect_traffic()
    if enforce():
        render.write_singbox_config()
        singbox_ctl.reload()
