#!/usr/bin/env python3
"""safechill-bot — Telegram admin bot for the exit node (stdlib only, long polling).

Commands (admins only, TG_ADMIN_IDS in /etc/safechill/vpn.env):
  /users               list people, with last AmneziaWG handshake
  /add <name>          create a person (add-client.sh) and post their main QR
  /qr <name> [what]    QR + link as one forwardable photo; what = nl (default) | tcp | ru | awg | nl6 | ru6 | all
  /del <name> yes      revoke a person everywhere
  /status              vpn-status.sh
Also: safechill-bot.py --send-qr <name> <chat_id> [what]   (one-shot, for tests)
"""
import json, os, re, subprocess, sys, time, urllib.request, urllib.parse, uuid, html, pathlib

ETC = pathlib.Path("/etc/safechill"); CLIENTS = pathlib.Path("/root/clients")
STATE = pathlib.Path("/var/lib/safechill"); STATE.mkdir(parents=True, exist_ok=True)
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

def load_env(p):
    env = {}
    for line in pathlib.Path(p).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1); env[k.strip()] = v.strip().strip("'\"")
    return env

ENV = load_env(ETC / "vpn.env")
TOKEN = ENV.get("TG_BOT_TOKEN", "")
BRAND = ENV.get("BRAND", "SafeChill")
ADMINS = {int(x) for x in re.split(r"[ ,]+", ENV.get("TG_ADMIN_IDS", "")) if x.strip().lstrip("-").isdigit()}
API = f"https://api.telegram.org/bot{TOKEN}/"

PROFILES = {  # what -> (file stem, title, note)
    "nl":  ("nl-xhttp",  "Основной · Амстердам · XHTTP+Reality", "Подключай этот первым."),
    "tcp": ("nl-tcp",    "Резерв · Амстердам · TCP+Reality", "Если основной не подключается."),
    "ru":  ("ru-xhttp",  "Вход через Россию · для мобильного в режиме белых списков", "Когда с мобильного не открывается ничего иностранного."),
    "nl6": ("nl6-xhttp", "Основной по IPv6 · Амстердам", "Для сетей, где IPv6 фильтруют слабее."),
    "ru6": ("ru6-xhttp", "Вход через Россию по IPv6", "То же, что ru, но по IPv6."),
    "awg": ("awg",       "AmneziaWG 3.1 · быстрая полоса для домашнего интернета", "Импортируй файл awg.conf или отсканируй QR в AmneziaVPN."),
}
HOWTO = "📱 AmneziaVPN 5.0.1.5+ → «+» → Импорт → вставь ссылку или отсканируй QR."

# ── Telegram API ──────────────────────────────────────────────────────────────
def api(method, **params):
    data = json.dumps(params).encode()
    req = urllib.request.Request(API + method, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=70) as r:
        return json.load(r)

def api_multipart(method, fields, files):
    boundary = uuid.uuid4().hex; body = b""
    for k, v in fields.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    for k, (fname, blob, ctype) in files.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fname}\"\r\nContent-Type: {ctype}\r\n\r\n".encode() + blob + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(API + method, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=70) as r:
        return json.load(r)

def send(chat, text):
    for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]:
        api("sendMessage", chat_id=chat, text=chunk, parse_mode="HTML", disable_web_page_preview=True)

def send_photo(chat, path, caption):
    return api_multipart("sendPhoto", {"chat_id": str(chat), "caption": caption[:1024], "parse_mode": "HTML"},
                         {"photo": (pathlib.Path(path).name, pathlib.Path(path).read_bytes(), "image/png")})

def send_doc(chat, path, caption):
    return api_multipart("sendDocument", {"chat_id": str(chat), "caption": caption[:1024], "parse_mode": "HTML"},
                         {"document": (pathlib.Path(path).name, pathlib.Path(path).read_bytes(), "text/plain")})

# ── data ──────────────────────────────────────────────────────────────────────
def users():
    return json.loads((ETC / "users.json").read_text())

def find_user(name):
    for u in users():
        if u["name"].lower() == name.lower(): return u["name"]
    return None

def awg_last_seen():
    """name -> seconds since last handshake (None if never)."""
    pub2name = {}
    for f in (ETC / "peers").glob("*.env"):
        e = load_env(f); pub2name[e.get("PEER_PUB", "")] = e.get("PEER_NAME", f.stem)
    out = {}
    try:
        for line in subprocess.run(["awg", "show", "awg0", "latest-handshakes"], capture_output=True, text=True, timeout=10).stdout.splitlines():
            pub, ts = line.split()
            n = pub2name.get(pub); ts = int(ts)
            if n: out[n] = (int(time.time()) - ts) if ts else None
    except Exception:
        pass
    return out

def ago(sec):
    if sec is None: return "не подключался"
    if sec < 180: return "🟢 онлайн"
    if sec < 3600: return f"{sec // 60} мин назад"
    if sec < 86400: return f"{sec // 3600} ч назад"
    return f"{sec // 86400} дн назад"

def sh(cmd, timeout=180):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).strip()

# ── commands ──────────────────────────────────────────────────────────────────
def cmd_users(chat):
    us = users(); seen = awg_last_seen()
    if not us: return send(chat, "Пока никого нет. /add имя")
    lines = [f"👥 <b>{BRAND}</b> · {len(us)} чел."]
    for u in sorted(us, key=lambda x: x["name"].lower()):
        lines.append(f"• <b>{html.escape(u['name'])}</b> — AWG: {ago(seen.get(u['name']))}")
    lines.append("\n/qr имя — QR и ссылка · /add имя — новый человек")
    send(chat, "\n".join(lines))

def qr_message(chat, name, what="nl"):
    stem, title, note = PROFILES[what]
    d = CLIENTS / name
    if what == "awg":
        conf = d / "awg.conf"
        if not conf.exists(): return send(chat, f"У {html.escape(name)} нет awg.conf")
        cap = f"🔐 <b>{BRAND} · {html.escape(name)}</b>\n{title}\n{note}"
        send_doc(chat, conf, cap)
        send_photo(chat, d / "awg.png", cap)
        return
    link = d / f"{stem}.txt"; png = d / f"{stem}.png"
    if not link.exists(): return send(chat, f"У {html.escape(name)} нет профиля «{what}» (нет IPv6 или RU-ноды?)")
    cap = (f"🔐 <b>{BRAND} · {html.escape(name)}</b>\n{title}\n{note}\n\n"
           f"<code>{html.escape(link.read_text().strip())}</code>\n\n{HOWTO}")
    send_photo(chat, png, cap)

def cmd_qr(chat, args):
    if not args: return send(chat, "Так: /qr имя [nl|tcp|ru|awg|nl6|ru6|all]")
    name = find_user(args[0])
    if not name: return send(chat, f"Не знаю «{html.escape(args[0])}». Список: /users")
    what = (args[1].lower() if len(args) > 1 else "nl")
    if what == "all":
        for w in ("nl", "tcp", "ru", "nl6", "ru6", "awg"):
            if (CLIENTS / name / f"{PROFILES[w][0]}.{'conf' if w == 'awg' else 'txt'}").exists(): qr_message(chat, name, w)
        return
    if what not in PROFILES: return send(chat, "Варианты: nl, tcp, ru, awg, nl6, ru6, all")
    qr_message(chat, name, what)

def cmd_add(chat, args):
    if not args or not NAME_RE.match(args[0]): return send(chat, "Так: /add Имя (латиница, цифры, - _; до 32 символов)")
    name = args[0]
    if find_user(name): return send(chat, f"{html.escape(name)} уже есть. /qr {html.escape(name)}")
    send(chat, f"⏳ Создаю {html.escape(name)}…")
    rc, out = sh(["/usr/local/bin/add-client.sh", name])
    if rc != 0: return send(chat, f"❌ add-client.sh упал:\n<pre>{html.escape(out[-1500:])}</pre>")
    send(chat, f"✅ {html.escape(name)} создан на обеих нодах. Основной QR ниже; ещё: /qr {html.escape(name)} awg · ru · all")
    qr_message(chat, name, "nl")

def cmd_del(chat, args):
    if len(args) < 2 or args[1].lower() not in ("yes", "да"): return send(chat, "Так: /del имя yes — удалит человека везде, без отката")
    name = find_user(args[0])
    if not name: return send(chat, f"Не знаю «{html.escape(args[0])}»")
    rc, out = sh(["/usr/local/bin/del-client.sh", name])
    send(chat, f"🗑 {html.escape(name)} удалён" if rc == 0 else f"❌ ошибка:\n<pre>{html.escape(out[-1500:])}</pre>")

def cmd_status(chat):
    rc, out = sh(["/usr/local/bin/vpn-status.sh"], timeout=60)
    send(chat, f"<pre>{html.escape(out[-3800:])}</pre>")

HELP = ("Команды:\n/users — кто заведён\n/add Имя — новый человек (+ QR)\n/qr Имя [nl|tcp|ru|awg|nl6|ru6|all] — QR и ссылка\n"
        "/del Имя yes — удалить\n/status — состояние нод")

def handle(msg):
    chat = msg["chat"]["id"]; frm = (msg.get("from") or {}).get("id"); text = (msg.get("text") or "").strip()
    if not text.startswith("/"): return
    parts = text.split(); cmd = parts[0].split("@")[0].lower(); args = parts[1:]
    if frm not in ADMINS:
        return send(chat, "⛔ Команды бота доступны только администратору.")
    try:
        if cmd in ("/users", "/list"): cmd_users(chat)
        elif cmd == "/add": cmd_add(chat, args)
        elif cmd == "/qr": cmd_qr(chat, args)
        elif cmd == "/del": cmd_del(chat, args)
        elif cmd == "/status": cmd_status(chat)
        elif cmd in ("/start", "/help"): send(chat, f"🔐 <b>{BRAND}</b>\n{HELP}")
    except Exception as e:  # noqa: BLE001
        send(chat, f"❌ {html.escape(str(e)[:500])}")

def main():
    if not TOKEN: sys.exit("TG_BOT_TOKEN missing in /etc/safechill/vpn.env")
    if len(sys.argv) >= 4 and sys.argv[1] == "--send-qr":
        name = find_user(sys.argv[2]) or sys.exit(f"unknown user {sys.argv[2]}")
        qr_message(int(sys.argv[3]), name, sys.argv[4] if len(sys.argv) > 4 else "nl"); return
    try:
        api("setMyCommands", commands=[{"command": "users", "description": "кто заведён"}, {"command": "add", "description": "добавить человека: /add Имя"},
                                       {"command": "qr", "description": "QR и ссылка: /qr Имя [awg|ru|all]"}, {"command": "status", "description": "состояние нод"},
                                       {"command": "help", "description": "справка"}])
    except Exception:
        pass
    off_file = STATE / "bot.offset"; offset = int(off_file.read_text()) if off_file.exists() else 0
    while True:
        try:
            res = api("getUpdates", offset=offset, timeout=50, allowed_updates=["message"])
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1; off_file.write_text(str(offset))
                if "message" in upd: handle(upd["message"])
        except KeyboardInterrupt:
            return
        except Exception as e:  # noqa: BLE001
            print("loop error:", e, file=sys.stderr, flush=True); time.sleep(5)

if __name__ == "__main__":
    main()
