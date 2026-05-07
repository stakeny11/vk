"""
tg_bot.py — Telegram-бот для деплоя на Railway.

Архитектура:
  Telegram <—> tg_bot.py (Railway, всегда онлайн) <—HTTP—> pc_agent.py (твой ПК)

Бот:
  - принимает команды из TG (long-poll getUpdates)
  - держит in-memory очередь задач
  - предоставляет HTTP API для ПК-агента
  - пушит логи от ПК в Telegram подписчикам (watchers)
  - умеет принимать видео из TG, временно держать байты, отдавать ПК

Состояние in-memory: при перезапуске Railway-инстанса очередь и watchers
обнуляются. Это сознательно — данные не критичны.
"""

import os
import sys
import io
import json
import time
import base64
import threading
from collections import deque

import requests
from flask import Flask, request, jsonify, abort, Response

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

# ---------- CONFIG ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ACCESS_CODE = os.getenv("ACCESS_CODE", "КириллШaрапов").strip()
OWNER_USER_ID = int(os.getenv("OWNER_USER_ID", "872428450"))
PC_AGENT_TOKEN = os.getenv("PC_AGENT_TOKEN", "").strip()
HTTP_PORT = int(os.getenv("PORT", "8080"))
PC_OFFLINE_AFTER_SEC = float(os.getenv("PC_OFFLINE_AFTER_SEC", "20"))

# Лимит TG bot API на скачивание входящих файлов = 20 МБ. Не повышаем.
MAX_VIDEO_SIZE_BYTES = 20 * 1024 * 1024

if not BOT_TOKEN:
    print("FATAL: BOT_TOKEN env var не задан", flush=True)
    sys.exit(1)
if not PC_AGENT_TOKEN:
    print("FATAL: PC_AGENT_TOKEN env var не задан", flush=True)
    sys.exit(1)

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_URL = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

# ---------- STATE ----------
state_lock = threading.Lock()

authed_users: set[int] = set()
pending_code: set[int] = set()
admins: set[int] = {OWNER_USER_ID}

# Подписчики на live-логи (все строки)
watchers: set[int] = set()
# Подписчики на smart-уведомления (только важные события)
smart_watchers: set[int] = set()

task_queue: deque = deque()

last_pc_ping: float = 0.0
last_pc_running: bool = False
last_pc_meta: dict = {}

last_lines: deque = deque(maxlen=300)

update_offset = 0

# Временное хранилище входящих видеофайлов: {task_id: {"bytes": bytes, "filename": str, "ts": float}}
pending_files: dict[str, dict] = {}
PENDING_FILES_TTL_SEC = 600  # 10 минут — потом удалим

# ---------- TELEGRAM HELPERS ----------
def tg(method: str, **data):
    try:
        r = requests.post(f"{API_URL}/{method}", data=data, timeout=30)
        return r.json()
    except Exception:
        return {"ok": False}

def tg_send_text(chat_id, text: str, reply_markup=None, disable_notification=False):
    text = text or ""
    chunks = [text[i:i + 3900] for i in range(0, max(1, len(text)), 3900)] or [""]
    for i, ch in enumerate(chunks):
        data = {"chat_id": chat_id, "text": ch}
        if reply_markup is not None and i == len(chunks) - 1:
            data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        if disable_notification:
            data["disable_notification"] = "true"
        tg("sendMessage", **data)

def tg_send_photo_bytes(chat_id, photo_bytes: bytes, caption: str = ""):
    files = {"photo": ("screen.png", photo_bytes, "image/png")}
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption[:1000]
    try:
        requests.post(f"{API_URL}/sendPhoto", data=data, files=files, timeout=60)
    except Exception as e:
        print(f"sendPhoto error: {e}", flush=True)

def tg_send_document_bytes(chat_id, file_bytes: bytes, filename: str, caption: str = ""):
    files = {"document": (filename, file_bytes, "application/octet-stream")}
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption[:1000]
    try:
        requests.post(f"{API_URL}/sendDocument", data=data, files=files, timeout=120)
    except Exception as e:
        print(f"sendDocument error: {e}", flush=True)

# Обратная совместимость с прежним кодом
send = tg_send_text

# ---------- KEYBOARD ----------
def main_menu():
    return {
        "keyboard": [
            [{"text": "▶️ Запустить заливку"}, {"text": "⏹ Стоп"}],
            [{"text": "📊 Статус"}, {"text": "📂 Что в input"}],
            [{"text": "🔁 Перезалив pending/failed"}, {"text": "📈 Статистика"}],
            [{"text": "📜 Лог"}, {"text": "📤 Лог-файлы"}],
            [{"text": "📸 Скрин ПК"}, {"text": "🧹 Очистить очередь"}],
            [{"text": "🔔 Smart-уведомления"}, {"text": "👁 Все логи"}],
            [{"text": "🙈 Тихо"}],
        ],
        "resize_keyboard": True,
    }

# ---------- AUTH ----------
def is_authed(uid: int) -> bool:
    return uid == OWNER_USER_ID or uid in authed_users

def is_admin(uid: int) -> bool:
    return uid in admins

# ---------- SMART NOTIFICATIONS ----------
# Подстроки/префиксы строк лога которые считаем "важными" — их получают smart_watchers.
SMART_MARKERS = (
    "✅", "🚫", "🟡", "🚨", "📊 Прогресс", "📦 Группа", "🏁", "▶️", "⏹",
    "🔄 Повтор", "🔌", "🟢 ПК", "🔴 ПК", "❌", "⚠️",
    "ИТОГ",
    "ATTEMPT_FAIL", "RETRY", "FINISH", "GROUP_DONE",
)

def _is_smart_line(line: str) -> bool:
    if not line:
        return False
    for m in SMART_MARKERS:
        if m in line:
            return True
    return False

# ---------- TASK HELPERS ----------
def push_task(ttype: str, requested_by: int, *, chat_id=None, args: dict | None = None) -> dict:
    task = {
        "id": f"t{int(time.time() * 1000)}",
        "type": ttype,
        "ts": time.time(),
        "requested_by": int(requested_by),
        "chat_id": int(chat_id) if chat_id is not None else None,
        "args": args or {},
    }
    with state_lock:
        task_queue.append(task)
    return task

def pop_task() -> dict | None:
    with state_lock:
        if task_queue:
            return task_queue.popleft()
        return None

def queue_snapshot() -> list[dict]:
    with state_lock:
        return list(task_queue)

def clear_queue() -> int:
    with state_lock:
        n = len(task_queue)
        task_queue.clear()
        return n

def pc_is_online() -> bool:
    return (time.time() - last_pc_ping) < PC_OFFLINE_AFTER_SEC

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

# ---------- INCOMING VIDEO HANDLING ----------
def _parse_group_id_from_caption(caption: str | None) -> int | None:
    if not caption:
        return None
    s = caption.strip()
    # допускаем "235245024" или "/group 235245024" или "club235245024"
    s = s.replace("club", "").replace("/group", "").strip()
    digits = "".join(c for c in s if c.isdigit())
    if not digits:
        return None
    try:
        return int(digits)
    except Exception:
        return None

def _download_tg_file(file_id: str) -> tuple[bytes | None, str | None, str | None]:
    """
    Скачиваем файл от Telegram. Возвращает (bytes, filename, error).
    """
    try:
        r = requests.get(f"{API_URL}/getFile", params={"file_id": file_id}, timeout=30).json()
    except Exception as e:
        return None, None, f"getFile error: {e}"
    if not r.get("ok"):
        return None, None, f"getFile failed: {r}"
    file_path = r["result"].get("file_path", "")
    file_size = r["result"].get("file_size", 0)
    if file_size and file_size > MAX_VIDEO_SIZE_BYTES:
        mb = file_size / 1024 / 1024
        return None, None, f"Файл слишком большой ({mb:.1f} МБ). TG bot API лимит = 20 МБ."
    name = file_path.split("/")[-1] if file_path else f"video_{int(time.time())}.mp4"
    try:
        rb = requests.get(f"{FILE_URL}/{file_path}", timeout=120)
        if rb.status_code != 200:
            return None, None, f"download status {rb.status_code}"
        return rb.content, name, None
    except Exception as e:
        return None, None, f"download error: {e}"

def _gc_pending_files():
    now = time.time()
    with state_lock:
        dead = [k for k, v in pending_files.items() if now - v["ts"] > PENDING_FILES_TTL_SEC]
        for k in dead:
            pending_files.pop(k, None)

def handle_incoming_video(msg: dict, chat_id: int, uid: int):
    """Юзер прислал видео/документ с подписью group_id → кладём задачу download_to_input."""
    caption = msg.get("caption") or ""
    gid = _parse_group_id_from_caption(caption)
    if gid is None:
        send(chat_id, "❓ Чтобы залить видео — пришли его с подписью = ID группы.\nПример подписи: 235245024", reply_markup=main_menu())
        return

    if "video" in msg:
        f = msg["video"]
        suggested = f"video_{f.get('file_unique_id','x')}.mp4"
    elif "document" in msg:
        f = msg["document"]
        suggested = f.get("file_name") or f"file_{f.get('file_unique_id','x')}.mp4"
    elif "video_note" in msg:
        f = msg["video_note"]
        suggested = f"note_{f.get('file_unique_id','x')}.mp4"
    else:
        return

    file_id = f.get("file_id")
    file_size = f.get("file_size", 0)
    if file_size and file_size > MAX_VIDEO_SIZE_BYTES:
        mb = file_size / 1024 / 1024
        send(chat_id, f"🚫 Файл {mb:.1f} МБ — больше 20 МБ. TG bot API не отдаст. Положи на ПК руками.", reply_markup=main_menu())
        return

    send(chat_id, "⬇️ Скачиваю…", reply_markup=main_menu())
    data, fname, err = _download_tg_file(file_id)
    if err or not data:
        send(chat_id, f"🚫 Не смог скачать: {err}", reply_markup=main_menu())
        return

    # сохраняем в очередь
    final_name = suggested if (suggested and suggested.lower().endswith(".mp4")) else (fname or "video.mp4")
    if not final_name.lower().endswith(".mp4"):
        final_name = final_name + ".mp4"

    task = push_task("download_to_input", uid, chat_id=chat_id, args={"group_id": int(gid), "filename": final_name})
    with state_lock:
        pending_files[task["id"]] = {"bytes": data, "filename": final_name, "ts": time.time()}

    if pc_is_online():
        send(chat_id, f"✅ Видео в очереди: input/{gid}/{final_name}\nПК заберёт в течение 5 секунд.", reply_markup=main_menu())
    else:
        send(chat_id, f"🔌 ПК офлайн. Видео ждёт в очереди (TTL 10 мин).\ninput/{gid}/{final_name}", reply_markup=main_menu())

# ---------- MESSAGE HANDLER ----------
def handle_message(msg: dict):
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    user = msg.get("from") or {}
    uid = int(user.get("id", 0))
    text = (msg.get("text") or "").strip()

    if not chat_id or not uid:
        return

    # ---- авторизация ----
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

    # ---- видео ----
    if any(k in msg for k in ("video", "video_note")) or (
        "document" in msg and "video" in (msg["document"].get("mime_type") or "")
    ):
        handle_incoming_video(msg, chat_id, uid)
        return
    # документ-mp4 без mime
    if "document" in msg:
        fname = (msg["document"].get("file_name") or "").lower()
        if fname.endswith(".mp4") or fname.endswith(".mov"):
            handle_incoming_video(msg, chat_id, uid)
            return

    # ---- команды ----
    low = text.lower()
    if text in ("/start", "/menu") or "меню" in low:
        send(
            chat_id,
            f"Главное меню.\n💻 ПК: {'🟢 онлайн' if pc_is_online() else '🔴 офлайн'}",
            reply_markup=main_menu(),
        )
        return

    if text == "▶️ Запустить заливку" or low == "/run":
        push_task("start_vk", uid, chat_id=chat_id)
        if pc_is_online():
            send(chat_id, "✅ Задача в очереди. ПК онлайн — заберёт в течение 5 секунд.", reply_markup=main_menu())
        else:
            send(chat_id, "🔌 Задача в очереди. ПК сейчас офлайн — выполнится как только включится.", reply_markup=main_menu())
        return

    if text == "⏹ Стоп" or low == "/stop":
        push_task("stop_vk", uid, chat_id=chat_id)
        send(chat_id, "🛑 Команда остановки в очереди.", reply_markup=main_menu())
        return

    if text == "📊 Статус" or low == "/status":
        send(chat_id, build_status_text(), reply_markup=main_menu())
        return

    if text == "📂 Что в input" or low == "/input":
        push_task("list_input", uid, chat_id=chat_id)
        send(chat_id, "📂 Запрашиваю состояние input/ у ПК…", reply_markup=main_menu())
        return

    if text == "🔁 Перезалив pending/failed" or low == "/retry":
        push_task("retry_failed_pending", uid, chat_id=chat_id)
        send(chat_id, "🔁 Команда отправлена. Жду отчёт от ПК…", reply_markup=main_menu())
        return

    if text == "📈 Статистика" or low.startswith("/stats"):
        period = "today"
        parts = low.split()
        if len(parts) > 1 and parts[1] in ("today", "week", "all"):
            period = parts[1]
        push_task("stats", uid, chat_id=chat_id, args={"period": period})
        send(chat_id, f"📈 Считаю статистику за {period}…", reply_markup=main_menu())
        return

    if text == "📜 Лог" or low == "/log":
        if not last_lines:
            send(chat_id, "Лог пустой.", reply_markup=main_menu())
        else:
            tail = "\n".join(list(last_lines)[-80:])
            send(chat_id, "📜 Последние строки:\n\n" + tail, reply_markup=main_menu())
        return

    if text == "📤 Лог-файлы" or low == "/log_file":
        push_task("send_log_file", uid, chat_id=chat_id, args={"which": "both"})
        send(chat_id, "📤 Сейчас пришлю лог-файлы…", reply_markup=main_menu())
        return

    if text == "📸 Скрин ПК" or low == "/screen":
        push_task("screenshot", uid, chat_id=chat_id)
        send(chat_id, "📸 Делаю скрин… (придёт фото)", reply_markup=main_menu())
        return

    if text == "🧹 Очистить очередь" or low == "/clear_queue":
        n = clear_queue()
        send(chat_id, f"🧹 Очистил очередь ({n} задач).", reply_markup=main_menu())
        return

    if text == "🔔 Smart-уведомления" or low == "/smart_on":
        with state_lock:
            smart_watchers.add(int(chat_id))
            watchers.discard(int(chat_id))
        send(chat_id, "🔔 Smart-уведомления включены.\nПрисылаю только важные события (запуск/финиш/ошибки/ретраи/прогресс).", reply_markup=main_menu())
        return

    if text == "👁 Все логи" or low == "/watch_on":
        with state_lock:
            watchers.add(int(chat_id))
            smart_watchers.discard(int(chat_id))
        send(chat_id, "👁 Все логи будут приходить сюда (полный поток).", reply_markup=main_menu())
        return

    if text == "🙈 Тихо" or low in ("/watch_off", "/quiet"):
        with state_lock:
            watchers.discard(int(chat_id))
            smart_watchers.discard(int(chat_id))
        send(chat_id, "🙈 Уведомления отключены.", reply_markup=main_menu())
        return

    if is_admin(uid) and low.startswith("/grant "):
        try:
            target = int(text.split()[1])
            with state_lock:
                authed_users.add(target)
            send(chat_id, f"✅ Дал доступ {target}.", reply_markup=main_menu())
        except Exception:
            send(chat_id, "Использование: /grant <user_id>", reply_markup=main_menu())
        return

    send(chat_id, "Не понял команду. /menu", reply_markup=main_menu())

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
            _gc_pending_files()
        except Exception as e:
            print(f"polling error: {e}", flush=True)
            time.sleep(3)

# ---------- HTTP API ----------
app = Flask(__name__)

def _check_token() -> bool:
    tok = request.args.get("token") or request.headers.get("X-Auth-Token", "")
    return tok == PC_AGENT_TOKEN

@app.route("/pc/poll", methods=["GET"])
def pc_poll():
    if not _check_token():
        return jsonify({"error": "unauth"}), 401
    global last_pc_ping, last_pc_running, last_pc_meta
    last_pc_ping = time.time()

    last_pc_running = (request.args.get("running", "") == "1")

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
        all_targets = list(watchers)
        smart_targets = list(smart_watchers) if _is_smart_line(line) else []

    text_to_send = line[:3900]
    for w in all_targets:
        try:
            tg("sendMessage", chat_id=w, text=text_to_send, disable_notification="true")
        except Exception:
            pass
    for w in smart_targets:
        try:
            tg("sendMessage", chat_id=w, text=text_to_send, disable_notification="true")
        except Exception:
            pass
    return jsonify({"ok": True})

@app.route("/pc/result", methods=["POST"])
def pc_result():
    if not _check_token():
        return jsonify({"error": "unauth"}), 401
    global last_pc_running
    payload = request.get_json(silent=True) or {}
    last_pc_running = False
    text = payload.get("text") or "Задача завершена."

    with state_lock:
        targets = set(watchers) | set(smart_watchers) | set(admins)

    for w in targets:
        try:
            tg("sendMessage", chat_id=w, text=f"🏁 {text}"[:3900])
        except Exception:
            pass
    return jsonify({"ok": True})

@app.route("/pc/reply_text", methods=["POST"])
def pc_reply_text():
    """ПК отвечает на конкретную задачу — текст в указанный chat_id."""
    if not _check_token():
        return jsonify({"error": "unauth"}), 401
    payload = request.get_json(silent=True) or {}
    chat_id = payload.get("chat_id")
    text = payload.get("text") or ""
    if not chat_id or not text:
        return jsonify({"error": "chat_id and text required"}), 400
    tg_send_text(chat_id, text, reply_markup=main_menu())
    return jsonify({"ok": True})

@app.route("/pc/reply_photo", methods=["POST"])
def pc_reply_photo():
    """multipart: chat_id + caption + photo file."""
    if not _check_token():
        return jsonify({"error": "unauth"}), 401
    chat_id = request.form.get("chat_id")
    caption = request.form.get("caption", "")
    if not chat_id:
        return jsonify({"error": "chat_id required"}), 400
    f = request.files.get("photo")
    if not f:
        return jsonify({"error": "photo file required"}), 400
    tg_send_photo_bytes(chat_id, f.read(), caption=caption)
    return jsonify({"ok": True})

@app.route("/pc/reply_document", methods=["POST"])
def pc_reply_document():
    if not _check_token():
        return jsonify({"error": "unauth"}), 401
    chat_id = request.form.get("chat_id")
    caption = request.form.get("caption", "")
    filename = request.form.get("filename", "file.txt")
    if not chat_id:
        return jsonify({"error": "chat_id required"}), 400
    f = request.files.get("document")
    if not f:
        return jsonify({"error": "document required"}), 400
    tg_send_document_bytes(chat_id, f.read(), filename=filename, caption=caption)
    return jsonify({"ok": True})

@app.route("/pc/file/<task_id>", methods=["GET"])
def pc_get_file(task_id):
    """ПК скачивает входящее видео по task_id."""
    if not _check_token():
        abort(401)
    with state_lock:
        entry = pending_files.get(task_id)
    if not entry:
        abort(404)
    return Response(entry["bytes"], mimetype="application/octet-stream", headers={
        "X-Filename": entry["filename"],
        "Content-Disposition": f'attachment; filename="{entry["filename"]}"',
    })

@app.route("/pc/file/<task_id>", methods=["DELETE"])
def pc_del_file(task_id):
    if not _check_token():
        abort(401)
    with state_lock:
        pending_files.pop(task_id, None)
    return jsonify({"ok": True})

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "pc_online": pc_is_online(),
        "queue_len": len(task_queue),
        "running": last_pc_running,
        "pending_files": len(pending_files),
    })

@app.route("/", methods=["GET"])
def root():
    return "VK Clips Bot — Railway Edge ✅"

# ---------- MAIN ----------
def main():
    t = threading.Thread(target=telegram_polling_loop, daemon=True)
    t.start()
    print(f"🚀 Стартую HTTP API на порту {HTTP_PORT}", flush=True)
    app.run(host="0.0.0.0", port=HTTP_PORT, threaded=True, use_reloader=False)

if __name__ == "__main__":
    main()
