#!/usr/bin/env python3
"""vpn-log.py — answer "did it break for X, and where" from the xray journals of every node.

Run it on the exit node; it reads its own journal and the entry/standby ones over ssh. Identity and
destination only ever appear together on the ENTRY node — the exit sees relayed traffic as the single
relay user — so "who" comes from there and "what the exit got back" from here.

  vpn-log.py --host tiktok --since -3h            every connection to a matching host, and what failed
  vpn-log.py --user Alina --since "2 days ago"    what one person reached, and where it broke
  vpn-log.py --errors --since -1h                 only the connections that failed
  vpn-log.py --user Egor --host youtube --raw     the raw lines behind the summary

What the log can and cannot say: xray forwards TLS without opening it, so there are no HTTP status
codes here. A result is connection-level — established, refused, timed out, DNS failed.
"""
import argparse, collections, re, subprocess, sys, pathlib

ETC = pathlib.Path("/etc/safechill")
# 2026/09/04 14:13:10.777921 from 1.2.3.4:5678 accepted tcp:host:443 [xhttp-reality >> direct] email: Egor
ACCESS = re.compile(r"^(?P<ts>\S+ \S+) from (?P<src>\S+) accepted \w+:(?P<host>[^\s:]+):(?P<port>\d+)"
                    r" \[(?P<in>\S+) (?:>>|->) (?P<out>\S+)\](?: email: (?P<user>\S+))?")
# 2026/09/04 14:22:53.209755 [Info] [528254312] app/proxyman/outbound: ... failed ... tcp:host:80 > ...
TAGGED = re.compile(r"^(?P<ts>\S+ \S+) \[\w+\] \[(?P<cid>\d+)\] (?P<text>.*)")
IN_TEXT = re.compile(r"\w+:(?P<host>[a-zA-Z0-9._-]+):(?P<port>\d+)")
# A real failure, not a normal end of stream. XHTTP rides HTTP/2, so every closed tab logs
# "stream error … CANCEL" and every finished download logs "client disconnected" — counting those as
# breakage buried the actual ones 20:1 the first time this ran.
FAIL = re.compile(r"failed to open connection|no such host|i/o timeout|connection refused"
                  r"|network is unreachable|invalid request user id|context deadline exceeded"
                  r"|REALITY: processed invalid|dns: |timeout waiting", re.I)
BENIGN = re.compile(r"client disconnected|CANCEL|connection ends|EOF|use of closed", re.I)
SKIP_HOST = {"v1.mux.cool"}        # xray's internal mux pseudo-destination, never a real site


def load_env(p):
    env = {}
    try: lines = pathlib.Path(p).read_text(encoding="utf-8").splitlines()
    except OSError: return env
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1); env[k.strip()] = v.strip().strip("'\"")
    return env


def nodes():
    e = load_env(ETC / "vpn.env")
    out = [("выход", None)]                                    # this box
    if e.get("RU_HOST"): out.append(("вход", e["RU_HOST"]))    # where the user's name lives
    if e.get("EXIT2_HOST"): out.append(("запасной", e["EXIT2_HOST"]))
    return out


CAP = 300_000   # a day of one node is ~500k lines; never pull an unbounded journal into 896 MB of RAM


def journal(host, since, until, needle, errors_only):
    """Raw xray journal lines, always filtered ON THE FAR NODE — an unfiltered week is millions of lines
    and pulling it across killed the box's ssh once already."""
    cmd = ["journalctl", "-u", "xray", "--no-pager", "-o", "cat", "--since", since]
    if until: cmd += ["--until", until]
    if needle:
        flt = f" | grep -F -i -- {needle!r}"
    elif errors_only:   # "accepted" comes too, or a failure has no name and no source to attach it to
        cmd += ["-n", str(CAP)]
        flt = " | grep -E -- 'accepted|failed|rejected|refused|timeout|unreachable|no such host|received request for'"
    else:
        cmd += ["-n", str(CAP)]; flt = ""
    remote = " ".join(cmd) + flt
    argv = remote if host is None else None
    try:
        p = subprocess.run(argv or ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", f"root@{host}", remote],
                           shell=argv is not None, capture_output=True, text=True, timeout=180)
        return p.stdout.splitlines()
    except subprocess.SubprocessError as ex:
        print(f"  ! {host or 'локально'}: {ex}", file=sys.stderr); return []


def parse(lines):
    """-> (access records, failures). A failure names its host directly when xray put it in the text,
    otherwise it carries only a connection id, which the id->host map resolves."""
    acc, fails, cid_host = [], [], {}
    for ln in lines:
        m = ACCESS.match(ln)
        if m:
            acc.append(m.groupdict()); continue
        m = TAGGED.match(ln)
        if not m: continue
        text, cid = m.group("text"), m.group("cid")
        if "received request for" in text or "default route for" in text:
            h = IN_TEXT.search(text)
            if h: cid_host.setdefault(cid, h.group("host"))
        if FAIL.search(text) and not BENIGN.search(text):
            h = IN_TEXT.search(text)
            fails.append({"ts": m.group("ts"), "cid": cid, "host": h.group("host") if h else None, "text": text})
    for f in fails:
        if not f["host"]: f["host"] = cid_host.get(f["cid"])
    return acc, fails


def fold_negatives(argv):
    """`--since -6h` reads as a flag to argparse, and journalctl's own syntax is exactly that. Join it."""
    out, i = [], 0
    while i < len(argv):
        a = argv[i]
        if a in ("--since", "--until") and i + 1 < len(argv) and re.fullmatch(r"-\d+\w*", argv[i + 1]):
            out.append(f"{a}={argv[i + 1]}"); i += 2
        else:
            out.append(a); i += 1
    return out


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--user"); ap.add_argument("--host")
    ap.add_argument("--since", default="-1h"); ap.add_argument("--until")
    ap.add_argument("--errors", action="store_true", help="только то, что не сработало")
    ap.add_argument("--raw", action="store_true", help="сырые строки вместо сводки")
    ap.add_argument("--limit", type=int, default=40)
    a = ap.parse_args(fold_negatives(sys.argv[1:]))

    needle = a.host or (f"email: {a.user}" if a.user else None)
    print(f"окно: {a.since} … {a.until or 'сейчас'}"
          + (f" | человек: {a.user}" if a.user else "") + (f" | хост ~ {a.host}" if a.host else ""))

    rows = collections.defaultdict(lambda: {"n": 0, "outs": collections.Counter(), "fails": 0,
                                            "first": None, "last": None, "srcs": set(), "why": collections.Counter()})
    raw = []
    for label, host in nodes():
        lines = journal(host, a.since, a.until, needle, a.errors)
        acc, fails = parse(lines)
        if a.raw: raw += [f"[{label}] {l}" for l in lines]
        for r in acc:
            if a.user and r.get("user") != a.user: continue
            if a.host and a.host.lower() not in r["host"].lower(): continue
            if r["host"] in SKIP_HOST: continue
            k = (label, r.get("user") or "—", r["host"])
            d = rows[k]; d["n"] += 1; d["outs"][r["out"]] += 1; d["srcs"].add(r["src"].rsplit(":", 1)[0])
            d["first"] = min(d["first"] or r["ts"], r["ts"]); d["last"] = max(d["last"] or r["ts"], r["ts"])
        for f in fails:
            if not f["host"] or f["host"] in SKIP_HOST: continue
            if a.host and a.host.lower() not in f["host"].lower(): continue
            cands = [k for k in rows if k[0] == label and k[2] == f["host"]] or [(label, "—", f["host"])]
            d = rows[cands[0]]; d["fails"] += 1
            d["why"][re.sub(r".*?> ", "", f["text"])[:60] or f["text"][:60]] += 1
        print(f"  {label}: {len(lines)} строк, {len(acc)} соединений, {len(fails)} сбоев")

    if a.raw:
        print(); [print(l) for l in raw[-a.limit:]]
        print(f"\n({len(raw)} строк всего, показаны последние {min(a.limit, len(raw))})"); return

    items = [(k, v) for k, v in rows.items() if not a.errors or v["fails"]]
    items.sort(key=lambda kv: (-kv[1]["fails"], -kv[1]["n"]))
    if not items: print("\nничего не найдено"); return
    print(f"\n{'нода':9} {'кто':9} {'откуда':17} {'куда':34} {'всего':>5} {'сбоев':>5}  выход / когда")
    for (label, user, host), v in items[:a.limit]:
        outs = ",".join(f"{o}×{c}" for o, c in v["outs"].most_common())
        when = f"{(v['first'] or '')[-15:-7]}–{(v['last'] or '')[-15:-7]}"
        src = sorted(v["srcs"]); frm = (src[0] if src else "—") + (f" +{len(src) - 1}" if len(src) > 1 else "")
        print(f"{label:9} {user:9} {frm:17} {host[:34]:34} {v['n']:5} {v['fails']:5}  {outs} {when}")
        for why, c in v["why"].most_common(2): print(f"{'':28}└ ×{c} {why}")
    if len(items) > a.limit: print(f"\n(+{len(items) - a.limit} строк, поднимите --limit)")


if __name__ == "__main__":
    main()
