#!/usr/bin/env python3
"""safechill-bot — Telegram admin bot for the exit node (stdlib only, long polling).

Commands (admins only, TG_ADMIN_IDS in /etc/safechill/vpn.env):
  /users                    list people, with last AmneziaWG handshake
  /add <name>               create a person (add-client.sh) and post their one-QR subscription
  /qr <name>                ONE QR for everything: personal subscription for Happ (all xray servers)
  /qr <name> amnezia        set for AmneziaVPN: main server + AmneziaWG (2 messages)
  /qr <name> awg|ru|tcp|nl6|ru6   a single profile with an explanation of when to use it
  /qr <name> all            everything above
  /del <name> yes           revoke a person everywhere
  /status                   vpn-status.sh
CLI: safechill-bot.py --send-qr <name> <chat_id> [what]
"""
import json, os, re, subprocess, sys, time, urllib.request, urllib.parse, uuid, html, pathlib

ETC = pathlib.Path("/etc/safechill"); CLIENTS = pathlib.Path("/root/clients")
STATE = pathlib.Path("/var/lib/safechill"); STATE.mkdir(parents=True, exist_ok=True)
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
AMNEZIA_LATEST = "https://github.com/amnezia-vpn/amnezia-client/releases/latest"
HAPP_IOS = "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215"
HAPP_ANDROID = "https://play.google.com/store/apps/details?id=com.happproxy"
HAPP_SITE = "https://www.happ.su/main/ru"

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

def log(*a): print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)

# ── Telegram API ──────────────────────────────────────────────────────────────
def api(method, **params):
    req = urllib.request.Request(API + method, data=json.dumps(params).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=70) as r: return json.load(r)

def api_multipart(method, fields, files):
    boundary = uuid.uuid4().hex; body = b""
    for k, v in fields.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    for k, (fname, blob, ctype) in files.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fname}\"\r\nContent-Type: {ctype}\r\n\r\n".encode() + blob + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(API + method, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=70) as r: return json.load(r)

def send(chat, text):
    for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]:
        api("sendMessage", chat_id=chat, text=chunk, parse_mode="HTML", disable_web_page_preview=True)

def send_photo(chat, path, caption):
    return api_multipart("sendPhoto", {"chat_id": str(chat), "caption": caption[:1024], "parse_mode": "HTML"},
                         {"photo": (pathlib.Path(path).name, pathlib.Path(path).read_bytes(), "image/png")})

def send_doc(chat, path, caption, as_name=None):
    return api_multipart("sendDocument", {"chat_id": str(chat), "caption": caption[:1024], "parse_mode": "HTML"},
                         {"document": (as_name or pathlib.Path(path).name, pathlib.Path(path).read_bytes(), "text/plain")})

# ── data ──────────────────────────────────────────────────────────────────────
def users(): return json.loads((ETC / "users.json").read_text())

def find_user(name):
    for u in users():
        if u["name"].lower() == name.lower(): return u["name"]
    return None

def awg_last_seen():
    pub2name = {}
    for f in (ETC / "peers").glob("*.env"):
        e = load_env(f); pub2name[e.get("PEER_PUB", "")] = e.get("PEER_NAME", f.stem)
    out = {}
    try:
        for line in subprocess.run(["awg", "show", "awg0", "latest-handshakes"], capture_output=True, text=True, timeout=10).stdout.splitlines():
            pub, ts = line.split(); n = pub2name.get(pub); ts = int(ts)
            if n: out[n] = (int(time.time()) - ts) if ts else None
    except Exception: pass
    return out

def ago(sec):
    if sec is None: return "не подключался"
    if sec < 180: return "🟢 онлайн"
    if sec < 3600: return f"{sec // 60} мин назад"
    if sec < 86400: return f"{sec // 3600} ч назад"
    return f"{sec // 86400} дн назад"

def amnezia_latest():
    """'5.0.1.5' from GitHub, cached 6h; '' on failure."""
    cache = STATE / "amnezia.latest"
    try:
        if cache.exists() and time.time() - cache.stat().st_mtime < 6 * 3600: return cache.read_text().strip()
        req = urllib.request.Request("https://api.github.com/repos/amnezia-vpn/amnezia-client/releases/latest", headers={"User-Agent": "safechill-bot"})
        with urllib.request.urlopen(req, timeout=10) as r: tag = json.load(r).get("tag_name", "")
        cache.write_text(tag); return tag
    except Exception:
        return cache.read_text().strip() if cache.exists() else ""

def sh(cmd, timeout=180):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).strip()

def esc(s): return html.escape(str(s))

# ── messages ──────────────────────────────────────────────────────────────────
def amnezia_line():
    v = amnezia_latest()
    return f"📥 AmneziaVPN {esc(v) + ' ' if v else ''}(<a href=\"{AMNEZIA_LATEST}\">скачать последнюю</a>), нужна 5.0.1.5 или новее."

def msg_sub(chat, name):
    d = CLIENTS / name; link = d / "sub.txt"
    if not link.exists(): return send(chat, f"У {esc(name)} ещё нет подписки, запусти /add {esc(name)} повторно")
    cap = (f"🔐 <b>{BRAND} · {esc(name)}</b>\n"
           f"<b>Один QR на всё.</b> Телефон, планшет, ПК. Внутри все наши серверы, приложение само выбирает рабочий.\n\n"
           f"1. Установи <b>Happ</b>: <a href=\"{HAPP_IOS}\">iPhone</a> · <a href=\"{HAPP_ANDROID}\">Android</a> · <a href=\"{HAPP_SITE}\">Windows и Mac</a>\n"
           f"2. В Happ нажми «+» → «Сканировать QR» (или вставь ссылку ниже)\n"
           f"3. Включи. Готово.\n\n<code>{esc(link.read_text().strip())}</code>\n\n"
           f"Пользуешься AmneziaVPN? Тогда: /qr {esc(name)} amnezia — тоже один QR на всё")
    send_photo(chat, d / "sub.png", cap)

def msg_profile(chat, name, what):
    d = CLIENTS / name
    if what == "awg":
        conf = d / "awg.conf"
        if not conf.exists(): return send(chat, f"У {esc(name)} нет awg.conf")
        cap = (f"⚡ <b>{BRAND} · {esc(name)} · AmneziaWG</b>\n"
               f"<b>Не обязательно.</b> Быстрая полоса для домашнего Wi-Fi и ПК. На мобильном интернете часто не работает.\n"
               f"Как: в AmneziaVPN «+» → «Импорт» → выбрать этот файл awg.conf (или отсканировать QR).\n{amnezia_line()}")
        send_doc(chat, conf, cap, as_name=f"{BRAND}-{name}-awg.conf")
        send_photo(chat, d / "awg.png", f"⚡ <b>{BRAND} · {esc(name)} · AmneziaWG</b>\nQR к файлу выше, для сканирования с другого экрана.")
        return
    titles = {
        "nl":  ("nl-xhttp",  "🟢 <b>Основной сервер</b> (Амстердам). Для AmneziaVPN на телефоне и ПК. Подключай этот."),
        "tcp": ("nl-tcp",    "🟡 <b>Запасной сервер</b> (Амстердам, другой протокол). Только если основной перестал подключаться."),
        "ru":  ("ru-xhttp",  "🇷🇺 <b>Вход через Россию.</b> Только для мобильного интернета в дни, когда не открывается ничего иностранного (белые списки). Выход всё равно Амстердам."),
        "nl6": ("nl6-xhttp", "🔵 <b>Основной по IPv6.</b> Редкий случай: сеть с IPv6, где IPv4 режут сильнее."),
        "ru6": ("ru6-xhttp", "🔵 <b>Вход через Россию по IPv6.</b> Редкий случай."),
    }
    stem, title = titles[what]; link = d / f"{stem}.txt"; png = d / f"{stem}.png"
    if not link.exists(): return send(chat, f"У {esc(name)} нет профиля «{what}»")
    cap = (f"🔐 <b>{BRAND} · {esc(name)}</b>\n{title}\n"
           f"Как: в AmneziaVPN «+» → «Импорт» → вставить ссылку или отсканировать QR.\n{amnezia_line()}\n\n"
           f"<code>{esc(link.read_text().strip())}</code>")
    send_photo(chat, png, cap)

def msg_amnezia(chat, name, node="nl"):
    """One QR for AmneziaVPN: native vpn:// key with xray (XHTTP+REALITY) and AmneziaWG inside."""
    d = CLIENTS / name; stem = "amnezia" if node == "nl" else "amnezia-ru"
    key = d / f"{stem}.txt"
    if not key.exists(): return send(chat, f"У {esc(name)} нет ключа {stem}, запусти /add {esc(name)} повторно")
    where = "Амстердам, основной" if node == "nl" else "вход через Россию, для мобильного в дни белых списков"
    cap = (f"🔐 <b>{BRAND} · {esc(name)} · AmneziaVPN</b>\n"
           f"<b>Один QR на всё для AmneziaVPN</b> ({where}). Внутри сразу два протокола: обычный и AmneziaWG, "
           f"переключаются в самом приложении.\n\n"
           f"1. {amnezia_line()}\n"
           f"2. В Amnezia: «+» → «Подключиться по ключу» → отсканировать QR (или вставить ключ из следующего сообщения)\n"
           f"3. Включить. Готово.")
    send_photo(chat, d / f"{stem}.png", cap)
    send(chat, f"Ключ текстом, если QR не читается (нажми, чтобы скопировать):\n<code>{esc(key.read_text().strip())}</code>")

def cmd_qr(chat, args):
    if not args: return send(chat, "Так: /qr Имя — один QR на всё (Happ)\n/qr Имя amnezia — один QR на всё для AmneziaVPN\n/qr Имя amneziaru — то же через RU-вход\n/qr Имя awg|ru|tcp|nl6|ru6|all")
    name = find_user(args[0])
    if not name: return send(chat, f"Не знаю «{esc(args[0])}». Список: /users")
    what = args[1].lower() if len(args) > 1 else "sub"
    if what in ("sub", "happ"): return msg_sub(chat, name)
    if what == "amnezia": return msg_amnezia(chat, name, "nl")
    if what in ("amneziaru", "amnezia-ru"): return msg_amnezia(chat, name, "ru")
    if what == "all":
        msg_sub(chat, name); time.sleep(1)
        msg_amnezia(chat, name, "nl"); time.sleep(1)
        if (CLIENTS / name / "amnezia-ru.txt").exists(): msg_amnezia(chat, name, "ru"); time.sleep(1)
        for w in ("nl", "awg", "ru", "tcp", "nl6", "ru6"):
            f = CLIENTS / name / ("awg.conf" if w == "awg" else {"nl": "nl-xhttp", "ru": "ru-xhttp", "tcp": "nl-tcp", "nl6": "nl6-xhttp", "ru6": "ru6-xhttp"}[w] + ".txt")
            if f.exists(): msg_profile(chat, name, w); time.sleep(1)
        return
    if what in ("nl", "tcp", "ru", "nl6", "ru6", "awg"): return msg_profile(chat, name, what)
    send(chat, "Варианты: (ничего) · amnezia · amneziaru · awg · ru · tcp · nl6 · ru6 · all")

def cmd_users(chat):
    us = users(); seen = awg_last_seen()
    if not us: return send(chat, "Пока никого нет. /add Имя")
    lines = [f"👥 <b>{BRAND}</b> · {len(us)} чел."]
    for u in sorted(us, key=lambda x: x["name"].lower()):
        lines.append(f"• <b>{esc(u['name'])}</b> — AWG: {ago(seen.get(u['name']))}")
    lines.append("\n/qr Имя — QR на всё · /qr Имя amnezia — для AmneziaVPN · /add Имя — новый человек")
    send(chat, "\n".join(lines))

def cmd_add(chat, args):
    if not args or not NAME_RE.match(args[0]): return send(chat, "Так: /add Имя (латиница, цифры, - _; до 32 символов)")
    name = args[0]
    if find_user(name): return send(chat, f"{esc(name)} уже есть: /qr {esc(name)}")
    send(chat, f"⏳ Создаю {esc(name)}…")
    rc, out = sh(["/usr/local/bin/add-client.sh", name])
    if rc != 0: return send(chat, f"❌ add-client.sh упал:\n<pre>{esc(out[-1500:])}</pre>")
    msg_sub(chat, name)

def cmd_del(chat, args):
    if len(args) < 2 or args[1].lower() not in ("yes", "да"): return send(chat, "Так: /del Имя yes — удалит человека везде, без отката")
    name = find_user(args[0])
    if not name: return send(chat, f"Не знаю «{esc(args[0])}»")
    rc, out = sh(["/usr/local/bin/del-client.sh", name])
    send(chat, f"🗑 {esc(name)} удалён" if rc == 0 else f"❌ ошибка:\n<pre>{esc(out[-1500:])}</pre>")

def cmd_status(chat):
    rc, out = sh(["/usr/local/bin/vpn-status.sh"], timeout=60)
    send(chat, f"<pre>{esc(out[-3800:])}</pre>")

HELP = ("Команды:\n/users — кто заведён\n/add Имя — новый человек (+ QR на всё)\n/qr Имя — один QR на всё (Happ)\n"
        "/qr Имя amnezia — один QR на всё для AmneziaVPN (xray + AmneziaWG)\n/qr Имя amneziaru — то же через RU-вход\n"
        "/qr Имя awg|ru|tcp|nl6|ru6|all — отдельные профили\n/del Имя yes — удалить\n/status — состояние нод")

def handle(msg):
    chat = msg["chat"]["id"]; frm = (msg.get("from") or {}).get("id"); text = (msg.get("text") or "").strip()
    if not text.startswith("/"): return
    parts = text.split(); cmd = parts[0].split("@")[0].lower(); args = parts[1:]
    log("cmd", cmd, args, "from", frm, "chat", chat)
    if frm not in ADMINS: return send(chat, "⛔ Команды бота доступны только администратору.")
    try:
        if cmd in ("/users", "/list"): cmd_users(chat)
        elif cmd == "/add": cmd_add(chat, args)
        elif cmd == "/qr": cmd_qr(chat, args)
        elif cmd == "/del": cmd_del(chat, args)
        elif cmd == "/status": cmd_status(chat)
        elif cmd in ("/start", "/help"): send(chat, f"🔐 <b>{BRAND}</b>\n{HELP}")
    except Exception as e:  # noqa: BLE001
        log("error", repr(e)); send(chat, f"❌ {esc(str(e)[:500])}")

def main():
    if not TOKEN: sys.exit("TG_BOT_TOKEN missing in /etc/safechill/vpn.env")
    if len(sys.argv) >= 4 and sys.argv[1] == "--send-qr":
        name = find_user(sys.argv[2]) or sys.exit(f"unknown user {sys.argv[2]}")
        cmd_qr(int(sys.argv[3]), [name] + sys.argv[4:5]); return
    try:
        api("setMyCommands", commands=[{"command": "users", "description": "кто заведён"}, {"command": "add", "description": "добавить человека: /add Имя"},
                                       {"command": "qr", "description": "QR на всё: /qr Имя  (amnezia | awg | ru | all)"}, {"command": "status", "description": "состояние нод"},
                                       {"command": "help", "description": "справка"}])
    except Exception: pass
    off_file = STATE / "bot.offset"; offset = int(off_file.read_text()) if off_file.exists() else 0
    log("bot started, admins", ADMINS)
    while True:
        try:
            res = api("getUpdates", offset=offset, timeout=50, allowed_updates=["message"])
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1; off_file.write_text(str(offset))
                if "message" in upd: handle(upd["message"])
        except KeyboardInterrupt: return
        except Exception as e:  # noqa: BLE001
            log("loop error:", e); time.sleep(5)

if __name__ == "__main__":
    main()
