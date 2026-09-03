"""Traffic accounting + limit/expiry enforcement (replaces Remnawave's).

Runs on a timer. Each tick:
  0. roll the monthly traffic period over if its boundary has passed;
  1. pull per-user traffic deltas from xray + sing-box, accumulate into the store;
  2. refresh last_seen for users with traffic this tick (or reported online);
  3. disable users that crossed their traffic limit (LIMITED) or expired (EXPIRED),
     hot-removing them from xray (rmu) and rewriting/reloading sing-box;
  4. re-enable users whose limit was raised / topped up / expiry extended.

DISABLED (manual) is never auto-touched; only LIMITED/EXPIRED auto-recover.
"""
from __future__ import annotations

import calendar
from datetime import datetime, timezone

from . import config, render, singbox_ctl, store, xray_ctl

# meta key holding the ISO start of the traffic period we last rolled over into
META_PERIOD = "traffic_period_start"


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


# ── monthly traffic period ────────────────────────────────────────────────────
def _reset_day() -> int | None:
    """Day of month the traffic period rolls over on, or None if undeterminable."""
    cfg = config.get_settings()
    try:
        fixed = int(cfg.get("traffic_reset_day", 0) or 0)
    except (TypeError, ValueError):
        fixed = 0
    if fixed:
        return max(1, min(31, fixed))
    # 0 -> mirror the hosting provider's billing reset (e.g. HOSTKEY "2026-09-04").
    # PROVIDER is rehydrated from /data/provider.json at import, so this survives
    # restarts and short API outages.
    from . import provider
    try:
        return int(str(provider.PROVIDER.get("reset") or "")[8:10])
    except (TypeError, ValueError):
        return None


def _period_start(now: datetime, day: int) -> datetime:
    """Most recent occurrence of `day` at or before `now`, midnight UTC.

    Clamped to the month's length, so day=31 lands on the 28th/30th in short
    months instead of raising or skipping the rollover entirely.
    """
    midnight = {"hour": 0, "minute": 0, "second": 0, "microsecond": 0}
    d = min(day, calendar.monthrange(now.year, now.month)[1])
    start = now.replace(day=d, **midnight)
    if start <= now:
        return start
    y, m = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    d = min(day, calendar.monthrange(y, m)[1])
    return now.replace(year=y, month=m, day=d, **midnight)


def roll_period() -> bool:
    """Zero every counter if we've crossed into a new traffic period.

    Idempotent: the period we last rolled into is recorded in `meta`, so the
    120 s tick resets once per month, not once per tick. Also self-healing —
    if the box was down on the boundary, the next tick after boot still sees a
    newer period start and rolls over.

    On a store that has never recorded a period (fresh deploy) it only seeds the
    marker and leaves counters alone: a code update must never silently wipe
    accounting. Use the admin reset for a deliberate catch-up.
    """
    day = _reset_day()
    if not day:
        return False
    start = _period_start(_now(), day).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    last = store.meta_get(META_PERIOD)
    if last == start:
        return False
    store.meta_set(META_PERIOD, start)
    if last is None:
        return False
    store.reset_all_traffic()
    return True


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
    # roll first: enforce() below then re-activates anyone the rollover freed
    # from LIMITED in the same pass, so nobody stays cut off for a full tick.
    roll_period()
    collect_traffic()
    if enforce():
        render.write_singbox_config()
        singbox_ctl.reload()
