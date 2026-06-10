"""Drive xray at runtime via its CLI API (`xray api ...`) over `docker exec`.

The admin container has the docker socket mounted; xray runs in its own
container. We avoid generating protobuf stubs by shelling out to the xray
binary inside that container:
    adu  -> hot add a user to all inbounds      (no restart)
    rmu  -> hot remove a user by email
    statsquery -> per-user traffic deltas (with -reset)
"""
from __future__ import annotations

import json
import os
import subprocess
import uuid as _uuid

from . import config, render

XRAY_BIN = "xray"   # binary name inside the xray container
# shared dir: admin mounts /etc/xray rw, xray mounts it ro — used to hand adu
# JSON files to the (distroless, shell-less) xray container.
SHARED_DIR = "/etc/xray"


def _cfg():
    return config.get_settings()


def _exec(args: list[str], stdin: str | None = None, timeout: int = 15) -> tuple[int, str]:
    cfg = _cfg()
    base = ["docker", "exec"]
    if stdin is not None:
        base.append("-i")
    base.append(cfg.xray_container)
    cmd = base + args
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:  # docker missing / container down
        return 1, f"exec error: {e}"


def _api(subcmd: str, *args: str) -> tuple[int, str]:
    """Run `xray api <subcmd> -s <server> <args...>`.

    NOTE: `-s` MUST precede any positional argument — Go's flag parser stops at
    the first positional, so a trailing `-s` is silently ignored (defaults to
    :8080). Hence we inject it right after the subcommand.
    """
    cfg = _cfg()
    return _exec([XRAY_BIN, "api", subcmd, "-s", cfg.xray_api, *args])


# ── user lifecycle (hot) ──────────────────────────────────────────────────────
def add_user(user: dict) -> tuple[bool, str]:
    """Hot-add one user to every client inbound via `adu`.

    The xray image is distroless (no shell), so we drop the adu JSON into the
    shared /etc/xray dir (admin rw / xray ro) and point adu at the file.
    """
    name = f".adu-{_uuid.uuid4().hex}.json"
    host_path = os.path.join(SHARED_DIR, name)
    try:
        with open(host_path, "w") as f:
            json.dump(render.build_adu_config(user), f)
        rc, out = _api("adu", os.path.join(SHARED_DIR, name))
    finally:
        try:
            os.remove(host_path)
        except OSError:
            pass
    return rc == 0 and "result: ok" in out, out.strip()


def remove_user(username: str) -> tuple[bool, str]:
    """Hot-remove a user (by email == username) from every client inbound."""
    logs = []
    for tag in render.CLIENT_TAGS:
        _rc, out = _api("rmu", "-tag", tag, username)
        logs.append(f"{tag}: {out.strip()}")
        # rmu returns non-zero if the user wasn't present; tolerate that
    return True, "\n".join(logs)


# ── stats ─────────────────────────────────────────────────────────────────────
def query_user_stats(reset: bool = True) -> dict[str, dict[str, int]]:
    """Return {username: {'up': bytes, 'down': bytes}} since last reset."""
    args = ["-pattern", "user>>>"]
    if reset:
        args.append("-reset")
    rc, out = _api("statsquery", *args)
    result: dict[str, dict[str, int]] = {}
    if rc != 0:
        return result
    try:
        data = json.loads(out)
    except Exception:
        return result
    for stat in data.get("stat", []) or []:
        name = stat.get("name", "")
        val = int(stat.get("value", 0) or 0)
        parts = name.split(">>>")
        if len(parts) == 4 and parts[0] == "user" and parts[2] == "traffic":
            email, direction = parts[1], parts[3]
            slot = result.setdefault(email, {"up": 0, "down": 0})
            if direction == "uplink":
                slot["up"] += val
            elif direction == "downlink":
                slot["down"] += val
    return result


def online_users() -> set[str]:
    """Best-effort set of currently-online usernames (xray statsUserOnline)."""
    rc, out = _api("statsgetallonlineusers")
    if rc != 0:
        return set()
    try:
        data = json.loads(out)
    except Exception:
        return set()
    users = data.get("users") or data.get("onlineUsers") or {}
    if isinstance(users, dict):
        return set(users.keys())
    if isinstance(users, list):
        return set(users)
    return set()


# ── lifecycle ─────────────────────────────────────────────────────────────────
def restart() -> tuple[bool, str]:
    cfg = _cfg()
    try:
        p = subprocess.run(["docker", "restart", cfg.xray_container],
                           capture_output=True, text=True, timeout=60)
        return p.returncode == 0, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return False, str(e)


def test_config(path: str | None = None) -> tuple[bool, str]:
    cfg = _cfg()
    path = path or cfg.xray_cfg
    rc, out = _exec([XRAY_BIN, "-test", "-config", path])
    return rc == 0 and "Configuration OK" in out, out.strip()


def api_ok() -> bool:
    rc, _ = _api("statsquery", "-pattern", "____none____")
    return rc == 0
