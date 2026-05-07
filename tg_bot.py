"""
tg_bot.py — Telegram-бот для деплоя на Railway.

Архитектура:
  Telegram <—> tg_bot.py (Railway, всегда онлайн) <—HTTP—> pc_agent.py (твой ПК)

Бот:
  - принимает команды из TG (long-poll getUpdates)
  - держит in-memory очередь задач
  - предоставляет HTTP API для ПК-агента (/pc/poll, /pc/log, /pc/result)
  - пушит логи от ПК в Telegram подписчикам (watchers)

Состояние in-memory: при перезапуске Railway-инстанса очередь и watchers
обнуляются. Это сознательно — данные не критичны, проще задачу заново поставить.
"""

import os
import sys
import json
import time
import threading
from collections import deque
from datetime import datetime

import requests
from flask import Flask, request, jsonify

# ---------- UTF-8 ----------
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("PYTHONUTF8", "1")
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------- CONFIG (env vars on Railway) ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ACCESS_CODE = os.getenv("ACCESS_CODE", "КириллШaрапов").strip()
OWNER_USER_ID = int(os.getenv("OWNER_USER_ID", "872428450"))
PC_AGENT_TOKEN = os.getenv("PC_AGENT_TOKEN", "").strip()
HTTP_PORT = int(os.getenv("PORT", "8080"))

# Через сколько секунд без пинга считать ПК офлайн
PC_OFFLINE_AFTER_SEC = float(os.getenv("PC_OFFLINE_AFTER_SEC", "20"))

if not BOT_TOKEN:
    print("FATAL: BOT_TOKEN env var не задан", flush=True)
    sys.exit(1)
if not PC_AGENT_TOKEN:
    print("FATAL: PC_AGENT_TOKEN env var не задан (любая длинная случайная строка)", flush=True)
    sys.exit(1)

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ---------- STATE ----------
state_lock = threading.Lock()

authed_users: set[int] = set()
pending_code: set[int] = set()
admins: set[int] = {OWNER_USER_ID}

watchers: set[int] = set()

# очередь задач: list[dict {id, type, ts, requested_by}]
task_queue: deque = deque()

# последняя информация от ПК
last_pc_ping: float = 0.0
last_pc_running: bool = False
last_pc_meta: dict = {}

# буфер последних строк лога
last_lines: deque = deque(maxlen=300)

# Telegram polling offset
update_offset = 0

# ---------- TELEGRAM API ----------
def tg(method: str, **data):
    try:
        r = requests.post(f"{API_URL}/{method}", data=data, timeout=30)
        return r.json()
    except Exception:
        return {"ok": False}

def send(chat_id: int, text: str, reply_markup=None):
    text = text or ""
    # делим длинные сообщения
    chunks = [text[i:i + 3900] for i in range(0, max(1, len(text)), 3900)] or [""]
    for i, ch in enumerate(chunks):
        data = {"chat_id": chat_id, "text": ch}
        if reply_markup is not None and i == len(chunks) - 1:
            data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        tg("sendMessage", **data)

# ---------- KEYBOARDS ----------
def main_menu():
    return {
        "keyboard": [
            [{"text": "▶️ Запустить заливку"}, {"text": "⏹ Стоп"}],
            [{"text": "📊 Статус"}, {"text": "📜 Лог"}],
            [{"text": "👁 Вкл логи"}, {"text": "🙈 Выкл логи"}],
            [{"text": "🧹 Очистить очередь"}],
        ],
        "resize_keyboard": True,
    }

# ---------- AUTH ----------
def is_authed(uid: int) -> bool:
    return uid == OWNER_USER_ID or uid in authed_users

def is_admin(uid: int) -> bool:
    return uid in admins

# ---------- HELPERS ----------
def pc_is_online() -> bool:
    return (time.time() - last_pc_ping) < PC_OFFLINE_AFTER_SEC

def queue_snapshot() -> list[dict]:
    with state_lock:
        return list(task_queue)

def push_task(ttype: str, requested_by: int) -> dict:
    task = {
        "id": f"t{int(time.time() * 1000)}",
        "type": ttype,
        "ts": time.time(),
        "requested_by": int(requested_by),
    }
    with state_lock:
        task_queue.append(task)
    return task

def pop_task() -> dict | None:
    with state_lock:
        if task_queue:
            return task_queue.popleft()
        return None

def clear_queue() -> int:
    with state_lock:
        n = len(task_queue)
        task_queue.clear()
        return n

def build_status_text() -> str:
    online = pc_is_online()
    last_ago = int(time.time() - last_pc_ping) if last_pc_ping > 0 else -1
    queue = queue_snapshot()
    last_line = last_lines[-1] if last_lines else "(пусто)"
    meta_str = ""
    if last_pc_meta:
        meta_str = "\n".join(f"  {k}: {v}" for k, v in last_pc_meta.items())
    return (
        f"💻 ПК: {'🟢 онлайн' if online else '🔴 офлайн'}\n"
        f"⏱ Последний пинг: {last_ago}s назад\n"
        f"⚙️ Скрипт работает: {'✅ ДА' if last_pc_running else '❌ нет'}\n"
        f"📋 Задач в очереди: {len(queue)}\n"
        + (f"\nИнфо от ПК:\n{meta_str}\n" if meta_str else "")
        + f"\n📜 Последняя строка лога:\n{last_line}"
    )

# ---------- MESSAGE HANDLER ----------
def handle_message(msg: dict):
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    user = msg.get("from") or {}
    uid = int(user.get("id", 0))
    text = (msg.get("text") or "").strip()

    if not chat_id or not uid:
        return

    # ---- авторизация по коду ----
    if not is_authed(uid):
        if uid in pending_code:
            if text == ACCESS_CODE:
                with state_lock:
                    authed_users.add(uid)
                    pending_code.discard(uid)
                send(chat_id, "✅ Доступ выдан.", reply_markup=main_menu())
            else:
                send(chat_id, "❌ Неверный код.")
            return
        if text in ("/start", "/menu") or "меню" in text.lower():
            with state_lock:
                pending_code.add(uid)
            send(chat_id, "🔒 Введи код доступа.")
            return
        return

    # ---- меню/команды ----
    low = text.lower()
    if text in ("/start", "/menu") or "меню" in low:
        send(
            chat_id,
            f"Главное меню.\n💻 ПК: {'🟢 онлайн' if pc_is_online() else '🔴 офлайн'}",
            reply_markup=main_menu(),
        )
        return

    if text == "▶️ Запустить заливку" or low == "/run":
        push_task("start_vk", uid)
        if pc_is_online():
            send(
                chat_id,
                "✅ Задача в очереди. ПК онлайн — заберёт в течение 5 секунд.",
                reply_markup=main_menu(),
            )
        else:
            send(
                chat_id,
                "🔌 Задача в очереди. ПК сейчас офлайн — выполнится как только включится.",
                reply_markup=main_menu(),
            )
        return

    if text == "⏹ Стоп" or low == "/stop":
        push_task("stop_vk", uid)
        send(chat_id, "🛑 Команда остановки в очереди.", reply_markup=main_menu())
        return

    if text == "📊 Статус" or low == "/status":
        send(chat_id, build_status_text(), reply_markup=main_menu())
        return

    if text == "📜 Лог" or low == "/log":
        if not last_lines:
            send(chat_id, "Лог пустой.", reply_markup=main_menu())
        else:
            tail = "\n".join(list(last_lines)[-80:])
            send(chat_id, "📜 Последние строки:\n\n" + tail, reply_markup=main_menu())
        return

    if text == "👁 Вкл логи" or low == "/watch_on":
        with state_lock:
            watchers.add(int(chat_id))
        send(chat_id, "👁 Логи будут приходить сюда.", reply_markup=main_menu())
        return

    if text == "🙈 Выкл логи" or low == "/watch_off":
        with state_lock:
            watchers.discard(int(chat_id))
        send(chat_id, "🙈 Логи отключены.", reply_markup=main_menu())
        return

    if text == "🧹 Очистить очередь" or low == "/clear_queue":
        n = clear_queue()
        send(chat_id, f"🧹 Очистил очередь ({n} задач).", reply_markup=main_menu())
        return

    # admin only
    if is_admin(uid) and low.startswith("/grant "):
        try:
            target = int(text.split()[1])
            with state_lock:
                authed_users.add(target)
            send(chat_id, f"✅ Дал доступ {target}.", reply_markup=main_menu())
        except Exception:
            send(chat_id, "Использование: /grant <user_id>", reply_markup=main_menu())
        return

    send(chat_id, "Не понял. /menu", reply_markup=main_menu())

# ---------- TELEGRAM POLLING ----------
def telegram_polling_loop():
    global update_offset
    print("📡 Стартую Telegram long-polling", flush=True)
    while True:
        try:
            r = requests.get(
                f"{API_URL}/getUpdates",
                params={"offset": update_offset, "timeout": 25},
                timeout=35,
            ).json()
            if not r.get("ok"):
                time.sleep(2)
                continue
            for upd in r.get("result", []):
                update_offset = upd["update_id"] + 1
                if "message" in upd:
                    try:
                        handle_message(upd["message"])
                    except Exception as e:
                        print(f"handle_message error: {e}", flush=True)
        except Exception as e:
            print(f"polling error: {e}", flush=True)
            time.sleep(3)

# ---------- HTTP API for PC agent ----------
app = Flask(__name__)

def _check_token() -> bool:
    tok = request.args.get("token") or request.headers.get("X-Auth-Token", "")
    return tok == PC_AGENT_TOKEN

@app.route("/pc/poll", methods=["GET"])
def pc_poll():
    """ПК-агент дёргает это каждые ~4 сек: пинг + забор задачи."""
    if not _check_token():
        return jsonify({"error": "unauth"}), 401
    global last_pc_ping, last_pc_running, last_pc_meta
    last_pc_ping = time.time()

    running_param = request.args.get("running", "")
    last_pc_running = (running_param == "1")

    # необязательная инфа от агента
    meta = request.args.get("meta")
    if meta:
        try:
            last_pc_meta = json.loads(meta)
        except Exception:
            last_pc_meta = {"raw": meta[:200]}

    task = pop_task()
    return jsonify({"task": task})

@app.route("/pc/log", methods=["POST"])
def pc_log():
    """Принимает строку лога с ПК и рассылает watchers'ам в TG."""
    if not _check_token():
        return jsonify({"error": "unauth"}), 401
    global last_pc_ping
    last_pc_ping = time.time()

    payload = request.get_json(silent=True) or {}
    line = (payload.get("line") or "").rstrip()
    if not line:
        return jsonify({"ok": True})

    with state_lock:
        last_lines.append(line)
        targets = list(watchers)

    for w in targets:
        try:
            tg("sendMessage", chat_id=w, text=line[:3900], disable_notification="true")
        except Exception:
            pass
    return jsonify({"ok": True})

@app.route("/pc/result", methods=["POST"])
def pc_result():
    """ПК сообщает что задача завершилась."""
    if not _check_token():
        return jsonify({"error": "unauth"}), 401
    global last_pc_running
    payload = request.get_json(silent=True) or {}
    last_pc_running = False
    text = payload.get("text") or "Задача завершена."

    with state_lock:
        targets = set(watchers) | set(admins)

    for w in targets:
        try:
            tg("sendMessage", chat_id=w, text=f"🏁 {text}"[:3900])
        except Exception:
            pass
    return jsonify({"ok": True})

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "pc_online": pc_is_online(),
        "queue_len": len(task_queue),
        "running": last_pc_running,
    })

@app.route("/", methods=["GET"])
def root():
    return "VK Clips Bot — Railway Edge ✅"

# ---------- MAIN ----------
def main():
    t = threading.Thread(target=telegram_polling_loop, daemon=True)
    t.start()
    print(f"🚀 Стартую HTTP API на порту {HTTP_PORT}", flush=True)
    # Flask dev-server для простоты. Для продакшена Railway проверка
    # покажет что всё работает; при желании можно на gunicorn.
    app.run(host="0.0.0.0", port=HTTP_PORT, threaded=True, use_reloader=False)

if __name__ == "__main__":
    main()
