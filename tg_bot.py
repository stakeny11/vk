"""
tg_bot.py — Telegram-бот для Railway.

Архитектура:
  Telegram <—> tg_bot.py (Railway) <— HTTP —> pc_agent.py (твой ПК)

Возможности:
  - Управление заливкой (start/stop/status/now)
  - Видео и скрин экрана ПК
  - Управление питанием ПК (shutdown/sleep/lock/reboot) с inline-подтверждением
  - "После залива" — авто-действие когда скрипт завершится
  - Прием mp4 от пользователя в TG -> ПК сохраняет в input/<gid>/
  - /input, /retry, /stats, /log, /log_file
  - Ошибки не засоряют чат — копятся в буфере, шлются файлом
"""

import os
import re
import sys
import json
import time
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
MAX_VIDEO_SIZE_BYTES = 20 * 1024 * 1024  # лимит TG bot API на download

if not BOT_TOKEN:
    print("FATAL: BOT_TOKEN env var не задан", flush=True); sys.exit(1)
if not PC_AGENT_TOKEN:
    print("FATAL: PC_AGENT_TOKEN env var не задан", flush=True); sys.exit(1)

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
FILE_URL = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

# ---------- STATE ----------
state_lock = threading.Lock()

authed_users: set[int] = set()
pending_code: set[int] = set()
admins: set[int] = {OWNER_USER_ID}

# Подписчики уведомлений
all_log_watchers: set[int] = set()    # каждая строка stdout
smart_watchers: set[int] = set()      # только важные события (без ошибочных строк)

task_queue: deque = deque()

last_pc_ping: float = 0.0
last_pc_running: bool = False
last_pc_meta: dict = {}

# Все строки лога подряд — для /log и /now
last_lines: deque = deque(maxlen=500)
# Только "ошибочные" строки — для /errors и авто-отправки файлом
error_lines: deque = deque(maxlen=5000)

update_offset = 0

# Временное хранилище входящих видеофайлов
pending_files: dict[str, dict] = {}
PENDING_FILES_TTL_SEC = 600

# Действие после завершения залива (None | "shutdown" | "sleep" | "lock")
power_after_finish: str | None = None

# FSM для диалога ручного ввода параметров статистики клипов.
# pending_stats_dialog[chat_id] = {"step": "from"/"to"/"views", "date_from": ..., "date_to": ..., "started_at": ts}
# Состояние теряется через 5 минут бездействия.
pending_stats_dialog: dict[int, dict] = {}
STATS_DIALOG_TTL_SEC = 300

# Какой режим уведомлений у конкретного chat_id (для toggle)
def watcher_mode_label(chat_id: int) -> str:
    in_smart = chat_id in smart_watchers
    in_all = chat_id in all_log_watchers
    if in_all:
        return "👁 ALL"
    if in_smart:
        return "🔔 SMART"
    return "🚫 OFF"

# ---------- TELEGRAM HELPERS ----------
def tg(method: str, **data):
    try:
        return requests.post(f"{API_URL}/{method}", data=data, timeout=30).json()
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
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption[:1000]
    try:
        requests.post(f"{API_URL}/sendPhoto", data=data,
                      files={"photo": ("screen.png", photo_bytes, "image/png")},
                      timeout=60)
    except Exception as e:
        print(f"sendPhoto error: {e}", flush=True)

def tg_send_video_bytes(chat_id, video_bytes: bytes, filename: str = "screen.mp4", caption: str = ""):
    data = {"chat_id": str(chat_id), "supports_streaming": "true"}
    if caption:
        data["caption"] = caption[:1000]
    try:
        requests.post(f"{API_URL}/sendVideo", data=data,
                      files={"video": (filename, video_bytes, "video/mp4")},
                      timeout=180)
    except Exception as e:
        print(f"sendVideo error: {e}", flush=True)

def tg_send_document_bytes(chat_id, file_bytes: bytes, filename: str, caption: str = ""):
    data = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption[:1000]
    try:
        requests.post(f"{API_URL}/sendDocument", data=data,
                      files={"document": (filename, file_bytes, "application/octet-stream")},
                      timeout=180)
    except Exception as e:
        print(f"sendDocument error: {e}", flush=True)

def tg_answer_callback(callback_id: str, text: str = "", show_alert: bool = False):
    try:
        tg("answerCallbackQuery", callback_query_id=callback_id, text=text[:200],
           show_alert="true" if show_alert else "false")
    except Exception:
        pass

def tg_edit_message(chat_id, message_id, text: str, reply_markup=None):
    data = {"chat_id": chat_id, "message_id": message_id, "text": text[:3900]}
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    tg("editMessageText", **data)

send = tg_send_text  # обратная совместимость

# ---------- KEYBOARDS ----------
def main_menu():
    return {
        "keyboard": [
            [{"text": "▶️ Запустить заливку"}, {"text": "▶️ Продолжить заливку"}],
            [{"text": "⏹ Стоп"}, {"text": "🔍 Что сейчас"}, {"text": "📊 Статус"}],
            [{"text": "📂 Input"}, {"text": "📈 Статистика"}],
            [{"text": "📁 Сбор групп"}, {"text": "🎯 Стата клипов"}],
            [{"text": "🔁 Retry"}, {"text": "⚖️ Раскидать input"}, {"text": "🎲 Перемешать видео"}],
            [{"text": "🧹 Очистить очередь"}],
            [{"text": "📸 Скрин"}, {"text": "🎬 Видео ПК"}],
            [{"text": "📜 Лог"}, {"text": "📋 Ошибки"}, {"text": "📤 Лог-файлы"}],
            [{"text": "🔔 Smart"}, {"text": "👁 All logs"}],
            [{"text": "⚙️ Управление ПК"}, {"text": "⏏️ После залива"}],
        ],
        "resize_keyboard": True,
    }


def shuffle_confirm_inline():
    """Выбор: dry-run или реальное перемешивание."""
    return {
        "inline_keyboard": [
            [
                {"text": "🔍 Dry-run (только план)", "callback_data": "shuffle:dry"},
                {"text": "🎲 Перемешать!",           "callback_data": "shuffle:do"},
            ],
            [
                {"text": "❌ Отмена",                 "callback_data": "shuffle:cancel"},
            ],
        ]
    }


def start_vk_count_inline():
    """Выбор скольких сообществ заливать за один запуск."""
    return {
        "inline_keyboard": [
            [
                {"text": "5 сообществ",   "callback_data": "startvk:5"},
                {"text": "10 сообществ",  "callback_data": "startvk:10"},
            ],
            [
                {"text": "20 сообществ",  "callback_data": "startvk:20"},
                {"text": "40 сообществ",  "callback_data": "startvk:40"},
            ],
            [
                {"text": "🚀 Все сразу",    "callback_data": "startvk:0"},
                {"text": "❌ Отмена",        "callback_data": "startvk:cancel"},
            ],
        ]
    }


def clips_stats_period_inline():
    """Быстрые варианты периода для статистики клипов."""
    return {
        "inline_keyboard": [
            [
                {"text": "📅 7 дней",   "callback_data": "stclip:7"},
                {"text": "📅 30 дней",  "callback_data": "stclip:30"},
            ],
            [
                {"text": "📅 90 дней",  "callback_data": "stclip:90"},
                {"text": "📅 Год",       "callback_data": "stclip:365"},
            ],
            [
                {"text": "⌨️ Ввести вручную", "callback_data": "stclip:custom"},
                {"text": "❌ Отмена",          "callback_data": "stclip:cancel"},
            ],
        ]
    }


def clips_stats_threshold_inline():
    """Быстрый выбор порога просмотров (после выбора периода)."""
    return {
        "inline_keyboard": [
            [
                {"text": "≥ 100",   "callback_data": "stclipmv:100"},
                {"text": "≥ 500",   "callback_data": "stclipmv:500"},
            ],
            [
                {"text": "≥ 1 000",  "callback_data": "stclipmv:1000"},
                {"text": "≥ 5 000",  "callback_data": "stclipmv:5000"},
            ],
            [
                {"text": "≥ 10 000", "callback_data": "stclipmv:10000"},
                {"text": "❌ Отмена", "callback_data": "stclip:cancel"},
            ],
        ]
    }

def power_menu():
    return {
        "keyboard": [
            [{"text": "🔌 Выключить"}, {"text": "😴 Сон"}],
            [{"text": "🔒 Заблокировать"}, {"text": "🔄 Перезагрузка"}],
            [{"text": "← Главное меню"}],
        ],
        "resize_keyboard": True,
    }

def after_finish_menu():
    return {
        "keyboard": [
            [{"text": "🔌 Выкл после"}, {"text": "😴 Сон после"}],
            [{"text": "🔒 Блок после"}, {"text": "❌ Не делать ничего"}],
            [{"text": "← Главное меню"}],
        ],
        "resize_keyboard": True,
    }

def confirm_inline(action: str, label: str):
    """inline-кнопки подтверждения. Callback data: 'confirm:<action>' / 'cancel:<action>'"""
    return {
        "inline_keyboard": [[
            {"text": f"✅ {label}", "callback_data": f"confirm:{action}"},
            {"text": "❌ Отмена",   "callback_data": f"cancel:{action}"},
        ]]
    }

def video_seconds_inline():
    return {
        "inline_keyboard": [[
            {"text": "5 сек",  "callback_data": "rec:5"},
            {"text": "10 сек", "callback_data": "rec:10"},
            {"text": "15 сек", "callback_data": "rec:15"},
            {"text": "20 сек", "callback_data": "rec:20"},
        ]]
    }

# ---------- AUTH ----------
def is_authed(uid: int) -> bool:
    # Доступ ТОЛЬКО у владельца.
    return uid == OWNER_USER_ID

def is_admin(uid: int) -> bool:
    return uid in admins

# ---------- TASK ----------
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

def clear_queue() -> int:
    with state_lock:
        n = len(task_queue)
        task_queue.clear()
        return n

def pc_is_online() -> bool:
    return (time.time() - last_pc_ping) < PC_OFFLINE_AFTER_SEC

# ---------- LOG CLASSIFIER ----------
SMART_MARKERS = (
    "▶️ Запуск", "▶️ запуск", "▶️ ",
    "🏁", "🟢 ПК", "🔴 ПК", "🔌",
    "✅ ", "🟡 ", "🚫 ", "🚨", "⚠️",
    "📊 Прогресс", "📦 Группа",
    "🔄 Повтор", "RETRY",
    "ИТОГ", "FINISH", "GROUP_DONE",
    "Запустил vk_clips", "ПК-агент в сети", "ПК-агент",
)

ERROR_MARKERS = (
    "Traceback", "Exception", "ERROR:", "[ERROR]", "[FAIL]",
    "FAIL |", "ATTEMPT_FAIL", "❌", "🚨",
    "капча", "captcha",
)

def is_error_line(line: str) -> bool:
    if not line:
        return False
    for m in ERROR_MARKERS:
        if m in line:
            return True
    return False

def is_smart_line(line: str) -> bool:
    if not line:
        return False
    for m in SMART_MARKERS:
        if m in line:
            return True
    return False

# ---------- STATUS / NOW ----------
def build_status_text() -> str:
    online = pc_is_online()
    last_ago = int(time.time() - last_pc_ping) if last_pc_ping > 0 else -1
    last_line = last_lines[-1] if last_lines else "(пусто)"
    meta_str = ""
    if last_pc_meta:
        meta_str = "\n".join(f"  {k}: {v}" for k, v in last_pc_meta.items())
    return (
        f"💻 ПК: {'🟢 онлайн' if online else '🔴 офлайн'}\n"
        f"⏱ Последний пинг: {last_ago}s назад\n"
        f"⚙️ Скрипт работает: {'✅ ДА' if last_pc_running else '❌ нет'}\n"
        f"📋 Задач в очереди: {len(task_queue)}\n"
        f"⏏️ После залива: {power_after_finish or '— ничего —'}\n"
        + (f"\nИнфо от ПК:\n{meta_str}\n" if meta_str else "")
        + f"\n📜 Последняя строка лога:\n{last_line}"
    )

def build_now_text() -> str:
    if not pc_is_online():
        return "🔴 ПК офлайн."
    if not last_pc_running:
        last = last_lines[-1] if last_lines else "(пусто)"
        return (
            "😴 Скрипт не запущен — простаивает.\n\n"
            f"📜 Последняя строка лога:\n{last}"
        )

    lines = list(last_lines)[-60:]

    progress = next((l for l in reversed(lines) if "📊 Прогресс" in l), None)
    group_done = next((l for l in reversed(lines) if "📦 Группа" in l and "завершен" in l.lower()), None)
    cur_group = next((l for l in reversed(lines)
                      if any(s in l for s in ("Группа ", "group=", "Заливаю", "▶️ Запуск"))), None)
    last_action = lines[-1] if lines else "(нет)"

    out = ["⚙️ Скрипт работает прямо сейчас."]
    if progress:
        out.append(f"📊 {progress}")
    if cur_group and (not progress or cur_group != progress):
        out.append(f"🎯 {cur_group}")
    if group_done:
        out.append(f"📦 {group_done}")
    out.append(f"💬 Последнее действие:\n{last_action}")
    return "\n\n".join(out)

# ---------- INCOMING VIDEO ----------
def _parse_group_id_from_caption(caption: str | None) -> int | None:
    if not caption:
        return None
    s = caption.strip().replace("club", "").replace("/group", "").strip()
    digits = "".join(c for c in s if c.isdigit())
    try:
        return int(digits) if digits else None
    except Exception:
        return None

def _download_tg_file(file_id: str) -> tuple[bytes | None, str | None, str | None]:
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

    final_name = suggested if suggested.lower().endswith(".mp4") else (fname or "video.mp4")
    if not final_name.lower().endswith(".mp4"):
        final_name = final_name + ".mp4"

    task = push_task("download_to_input", uid, chat_id=chat_id,
                     args={"group_id": int(gid), "filename": final_name})
    with state_lock:
        pending_files[task["id"]] = {"bytes": data, "filename": final_name, "ts": time.time()}

    if pc_is_online():
        send(chat_id, f"✅ Видео в очереди: input/{gid}/{final_name}\nПК заберёт в течение 5 сек.", reply_markup=main_menu())
    else:
        send(chat_id, f"🔌 ПК офлайн. Видео ждёт в очереди (TTL 10 мин).\ninput/{gid}/{final_name}", reply_markup=main_menu())

# ---------- ERRORS BUFFER ----------
def errors_text_blob() -> bytes:
    if not error_lines:
        return b""
    return ("\n".join(list(error_lines))).encode("utf-8")

def auto_send_errors_after_finish():
    """Если в буфере есть ошибки — шлём всем admin'ам файлом и чистим."""
    with state_lock:
        blob = errors_text_blob()
        n = len(error_lines)
        targets = list(admins | smart_watchers | all_log_watchers)
        error_lines.clear()
    if not blob:
        return
    fname = f"errors_{time.strftime('%Y%m%d_%H%M%S')}.log"
    for w in targets:
        try:
            tg_send_document_bytes(w, blob, filename=fname,
                                   caption=f"📋 Ошибки за прогон ({n} строк)")
        except Exception:
            pass

# ---------- MESSAGE HANDLER ----------
def handle_message(msg: dict):
    global power_after_finish
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    user = msg.get("from") or {}
    uid = int(user.get("id", 0))
    text = (msg.get("text") or "").strip()

    if not chat_id or not uid:
        return

    # ---- авторизация: только владелец ----
    if not is_authed(uid):
        print(f"[auth] блок: uid={uid} @{user.get('username','')} text={text[:60]!r}", flush=True)
        return

    # ---- FSM: ручной ввод параметров статистики клипов ----
    # Должен идти ПЕРЕД основным меню, чтобы перехватывать ответы пользователя.
    if _maybe_handle_stats_dialog(chat_id, uid, text):
        return

    # ---- видео-документы ----
    if any(k in msg for k in ("video", "video_note")) or (
        "document" in msg and "video" in (msg["document"].get("mime_type") or "")
    ):
        handle_incoming_video(msg, chat_id, uid); return
    if "document" in msg:
        fname = (msg["document"].get("file_name") or "").lower()
        if fname.endswith((".mp4", ".mov", ".avi", ".mkv")):
            handle_incoming_video(msg, chat_id, uid); return

    # ---- команды/кнопки ----
    low = text.lower()
    if text in ("/start", "/menu") or "меню" in low or text == "← Главное меню":
        send(chat_id,
             f"Главное меню.\n💻 ПК: {'🟢 онлайн' if pc_is_online() else '🔴 офлайн'}\n"
             f"🔔 Режим уведомлений: {watcher_mode_label(int(chat_id))}\n"
             f"⏏️ После залива: {power_after_finish or '—'}",
             reply_markup=main_menu())
        return

    # --- основные ---
    if text == "▶️ Запустить заливку" or low == "/run":
        send(chat_id,
             "🎯 Сколько сообществ загружать ЗА ОДИН ЭТАП?\n\n"
             "После каждого этапа скрипт остановится — публикуешь руками,\n"
             "закрываешь вкладки, потом жмёшь «▶️ Продолжить заливку»\n"
             "и идёт следующий этап.",
             reply_markup=start_vk_count_inline())
        return

    if text == "▶️ Продолжить заливку" or low == "/continue":
        push_task("continue_stage", uid, chat_id=chat_id)
        send(chat_id, "▶️ Сигнал отправлен. Скрипт начнёт следующий этап.",
             reply_markup=main_menu())
        return

    if text == "🎲 Перемешать видео" or low == "/shuffle":
        send(chat_id,
             "🎲 Перемешать видео между папками сообществ?\n\n"
             "Каждая папка СОХРАНИТ свой размер, но содержимое будет\n"
             "случайно перетасовано между всеми папками.\n\n"
             "• Dry-run — показать план без перемещения\n"
             "• Перемешать! — реально переместить (необратимо)",
             reply_markup=shuffle_confirm_inline())
        return

    if text == "⏹ Стоп" or low == "/stop":
        # Сначала отправляем мягкий сигнал stage_stop (скрипт завершится
        # между этапами с сохранением state). Если скрипт уже выполняет
        # этап — он закончит его и потом увидит сигнал.
        push_task("stop_stage", uid, chat_id=chat_id)
        send(chat_id,
             "🛑 Команда мягкой остановки в очереди.\n"
             "Скрипт завершится между этапами (state сохранится).\n\n"
             "Если нужно жёстко прибить процесс — /forcestop",
             reply_markup=main_menu()); return

    if low == "/forcestop" or text == "⏹ Force-стоп":
        push_task("stop_vk", uid, chat_id=chat_id)
        send(chat_id, "🛑 Команда жёсткой остановки (kill процесса) в очереди.",
             reply_markup=main_menu()); return

    if text == "🔍 Что сейчас" or low == "/now":
        send(chat_id, build_now_text(), reply_markup=main_menu()); return

    if text == "📊 Статус" or low == "/status":
        send(chat_id, build_status_text(), reply_markup=main_menu()); return

    if text in ("📂 Input", "📂 Что в input") or low == "/input":
        push_task("list_input", uid, chat_id=chat_id)
        send(chat_id, "📂 Запрашиваю состояние input/ у ПК…", reply_markup=main_menu()); return

    if text in ("🔁 Retry", "🔁 Перезалив pending/failed") or low == "/retry":
        push_task("retry_failed_pending", uid, chat_id=chat_id)
        send(chat_id, "🔁 Команда отправлена. Жду отчёт от ПК…", reply_markup=main_menu()); return

    if text in ("⚖️ Раскидать input", "⚖️ Раскидать Input") or low in ("/distribute_input", "/spread_input"):
        push_task("distribute_input_even", uid, chat_id=chat_id)
        send(chat_id, "⚖️ Раскидываю input/*.mp4 по папкам групп…", reply_markup=main_menu()); return

    if text == "📈 Статистика" or low.startswith("/stats"):
        period = "today"
        parts = low.split()
        if len(parts) > 1 and parts[1] in ("today", "week", "all"):
            period = parts[1]
        push_task("stats", uid, chat_id=chat_id, args={"period": period})
        send(chat_id, f"📈 Считаю статистику за {period}…", reply_markup=main_menu()); return

    # --- новые: сбор групп и стата клипов ---
    if text == "📁 Сбор групп" or low == "/collect_groups":
        push_task("collect_groups", uid, chat_id=chat_id)
        send(chat_id,
             "📁 Запустил сбор управляемых сообществ.\n"
             "На ПК откроется Chrome → vk.com/groups?tab=admin.\n"
             "Жди отчёт. Создадутся пустые папки input/<id>/.",
             reply_markup=main_menu()); return

    if text == "🎯 Стата клипов" or low == "/clip_stats":
        send(chat_id,
             "🎯 Статистика клипов VK.\n\n"
             "Выбери период публикации. Сразу после — выберешь порог просмотров.\n"
             "⚠️ В pc_agent.env должен быть VK_TOKEN.",
             reply_markup=clips_stats_period_inline())
        return

    if text == "📜 Лог" or low == "/log":
        if not last_lines:
            send(chat_id, "Лог пустой.", reply_markup=main_menu())
        else:
            tail = "\n".join(list(last_lines)[-80:])
            send(chat_id, "📜 Последние строки:\n\n" + tail, reply_markup=main_menu())
        return

    if text == "📋 Ошибки" or low == "/errors":
        blob = errors_text_blob()
        if not blob:
            send(chat_id, "📋 Ошибок не зафиксировано.", reply_markup=main_menu())
        else:
            tg_send_document_bytes(chat_id, blob,
                                   filename=f"errors_{time.strftime('%Y%m%d_%H%M%S')}.log",
                                   caption=f"📋 Текущий буфер ошибок ({len(error_lines)} строк)")
            send(chat_id, "Готово. Очистить буфер?  /clear_errors",
                 reply_markup=main_menu())
        return

    if low == "/clear_errors":
        with state_lock:
            error_lines.clear()
        send(chat_id, "🧹 Буфер ошибок очищен.", reply_markup=main_menu()); return

    if text in ("📤 Лог-файлы", "📤 Лог-файл") or low == "/log_file":
        push_task("send_log_file", uid, chat_id=chat_id, args={"which": "both"})
        send(chat_id, "📤 Сейчас пришлю лог-файлы…", reply_markup=main_menu()); return

    if text in ("📸 Скрин", "📸 Скрин ПК") or low == "/screen":
        push_task("screenshot", uid, chat_id=chat_id)
        send(chat_id, "📸 Делаю скрин…", reply_markup=main_menu()); return

    if text == "🎬 Видео ПК" or low == "/screenrec":
        send(chat_id, "🎬 Сколько секунд записать?",
             reply_markup=video_seconds_inline())
        return

    if text == "🧹 Очистить очередь" or low == "/clear_queue":
        n = clear_queue()
        send(chat_id, f"🧹 Очистил очередь ({n} задач).", reply_markup=main_menu()); return

    # --- уведомления (toggle) ---
    if text in ("🔔 Smart", "🔔 Smart-уведомления") or low == "/smart":
        with state_lock:
            if int(chat_id) in smart_watchers:
                smart_watchers.discard(int(chat_id))
                msg2 = "🔔 Smart выключены."
            else:
                smart_watchers.add(int(chat_id))
                all_log_watchers.discard(int(chat_id))
                msg2 = "🔔 Smart включены.\nПрисылаю только важные события (запуск/финиш/прогресс/ретраи)."
        send(chat_id, msg2, reply_markup=main_menu()); return

    if text in ("👁 All logs", "👁 Все логи", "👁 Вкл логи") or low in ("/all_logs", "/watch_on"):
        with state_lock:
            if int(chat_id) in all_log_watchers:
                all_log_watchers.discard(int(chat_id))
                msg2 = "👁 Поток всех логов выключен."
            else:
                all_log_watchers.add(int(chat_id))
                smart_watchers.discard(int(chat_id))
                msg2 = "👁 Все логи поедут сюда. Может быть много сообщений."
        send(chat_id, msg2, reply_markup=main_menu()); return

    # legacy кнопка «🙈 Тихо» → отписать от всего
    if text in ("🙈 Тихо", "🙈 Выкл логи") or low in ("/watch_off", "/quiet"):
        with state_lock:
            all_log_watchers.discard(int(chat_id))
            smart_watchers.discard(int(chat_id))
        send(chat_id, "🙈 Уведомления выключены.", reply_markup=main_menu()); return

    # --- меню питания ---
    if text == "⚙️ Управление ПК" or low == "/power":
        send(chat_id, "⚙️ Управление ПК", reply_markup=power_menu()); return

    if text == "🔌 Выключить":
        send(chat_id, "🔌 Выключить ПК сейчас?", reply_markup=confirm_inline("shutdown_now", "Выключить")); return
    if text == "😴 Сон":
        send(chat_id, "😴 Усыпить ПК сейчас?", reply_markup=confirm_inline("sleep_now", "В сон")); return
    if text == "🔒 Заблокировать":
        send(chat_id, "🔒 Заблокировать экран?", reply_markup=confirm_inline("lock_now", "Заблокировать")); return
    if text == "🔄 Перезагрузка":
        send(chat_id, "🔄 Перезагрузить ПК сейчас?", reply_markup=confirm_inline("reboot_now", "Перезагрузить")); return

    # --- меню "после залива" ---
    if text == "⏏️ После залива":
        cur = power_after_finish or "—"
        send(chat_id, f"⏏️ Сейчас после залива: {cur}\n\nЧто сделать когда скрипт закончится?",
             reply_markup=after_finish_menu()); return

    if text == "🔌 Выкл после":
        send(chat_id, "Подтверди: после завершения залива ВЫКЛЮЧИТЬ ПК?",
             reply_markup=confirm_inline("after_shutdown", "Да, выключить после")); return
    if text == "😴 Сон после":
        send(chat_id, "Подтверди: после завершения залива УСЫПИТЬ ПК?",
             reply_markup=confirm_inline("after_sleep", "Да, в сон после")); return
    if text == "🔒 Блок после":
        send(chat_id, "Подтверди: после завершения залива ЗАБЛОКИРОВАТЬ экран?",
             reply_markup=confirm_inline("after_lock", "Да, заблокировать после")); return
    if text == "❌ Не делать ничего":
        with state_lock:
            power_after_finish = None
        send(chat_id, "✅ После залива ничего не делать.", reply_markup=main_menu()); return

    # admin
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

# ---------- STATS-CLIPS FSM ----------
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _gc_stats_dialog():
    """Чистим протухшие FSM-сессии."""
    now = time.time()
    with state_lock:
        dead = [cid for cid, st in pending_stats_dialog.items()
                if now - st.get("started_at", now) > STATS_DIALOG_TTL_SEC]
        for cid in dead:
            pending_stats_dialog.pop(cid, None)


def _maybe_handle_stats_dialog(chat_id, uid, text: str) -> bool:
    """
    Если для chat_id идёт FSM-диалог ручного ввода — обрабатывает шаг.
    Возвращает True если перехватил сообщение (дальше handle_message не идёт).
    """
    _gc_stats_dialog()
    with state_lock:
        state = pending_stats_dialog.get(int(chat_id))
    if not state:
        return False

    low = (text or "").lower().strip()
    if low in ("/cancel", "отмена", "cancel", "❌", "стоп"):
        with state_lock:
            pending_stats_dialog.pop(int(chat_id), None)
        send(chat_id, "❌ Диалог статистики отменён.", reply_markup=main_menu())
        return True

    step = state.get("step")
    if step == "from":
        if not DATE_RE.match(text):
            send(chat_id, "⚠️ Формат YYYY-MM-DD (например 2026-01-15). Или /cancel.")
            return True
        state["date_from"] = text
        state["step"] = "to"
        send(chat_id, f"📅 Дата ОТ = {text}.\n\nТеперь введи дату ДО (YYYY-MM-DD):")
        return True

    if step == "to":
        if not DATE_RE.match(text):
            send(chat_id, "⚠️ Формат YYYY-MM-DD. Или /cancel.")
            return True
        state["date_to"] = text
        state["step"] = "views"
        send(chat_id, f"📅 Дата ДО = {text}.\n\nТеперь выбери мин. просмотры:",
             reply_markup=clips_stats_threshold_inline())
        return True

    if step == "views":
        # Юзер мог ввести число руками вместо нажатия inline-кнопки
        try:
            mv = int(text.replace(" ", "").replace("_", ""))
            if mv < 0:
                raise ValueError
        except ValueError:
            send(chat_id, "⚠️ Введи целое число (например 1000) или жми кнопку.")
            return True
        # Запускаем задачу
        df = state.get("date_from"); dt = state.get("date_to")
        with state_lock:
            pending_stats_dialog.pop(int(chat_id), None)
        push_task("clips_stats", uid, chat_id=chat_id,
                  args={"date_from": df, "date_to": dt, "min_views": mv})
        send(chat_id,
             f"🚀 Задача отправлена.\n"
             f"Период: {df} … {dt}\n"
             f"Мин. просмотры: {mv}\n"
             f"Жди — xlsx придёт сюда.",
             reply_markup=main_menu())
        return True

    # Неизвестный шаг — сбрасываем
    with state_lock:
        pending_stats_dialog.pop(int(chat_id), None)
    return False


def _start_clips_stats_task(chat_id, uid, days: int, min_views: int = 1000):
    """Хелпер: посчитать дату ОТ как (сегодня - N дней) и запушить задачу."""
    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone.utc)
    df = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    dt = today.strftime("%Y-%m-%d")
    push_task("clips_stats", uid, chat_id=chat_id,
              args={"date_from": df, "date_to": dt, "min_views": min_views})
    send(chat_id,
         f"🚀 Задача отправлена.\n"
         f"Период: {df} … {dt} ({days} дней)\n"
         f"Мин. просмотры: {min_views}\n"
         f"Жди — xlsx придёт сюда.",
         reply_markup=main_menu())


# ---------- CALLBACK QUERY ----------
def handle_callback_query(cb: dict):
    global power_after_finish
    cb_id = cb.get("id")
    data = cb.get("data", "")
    user = cb.get("from") or {}
    uid = int(user.get("id", 0))
    msg = cb.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    msg_id = msg.get("message_id")

    if not is_authed(uid):
        tg_answer_callback(cb_id, "❌ Нет доступа", show_alert=True); return

    # --- Перемешать видео между папками ---
    if data.startswith("shuffle:"):
        choice = data.split(":", 1)[1]
        if choice == "cancel":
            tg_answer_callback(cb_id, "Отменено")
            tg_edit_message(chat_id, msg_id, "❌ Shuffle отменён.")
            return
        if choice == "dry":
            push_task("shuffle_input", uid, chat_id=chat_id,
                      args={"dry_run": True})
            tg_answer_callback(cb_id, "Dry-run запущен")
            tg_edit_message(chat_id, msg_id,
                            "🔍 Dry-run shuffle запущен.\n"
                            "Скрипт покажет план перемещений — реально файлы не двигаются.")
            return
        if choice == "do":
            push_task("shuffle_input", uid, chat_id=chat_id, args={})
            tg_answer_callback(cb_id, "Перемешиваю!")
            tg_edit_message(chat_id, msg_id,
                            "🎲 Shuffle запущен.\n"
                            "Видео перемешиваются между папками. Отчёт придёт по окончании.")
            return
        tg_answer_callback(cb_id, "Неизвестно")
        return

    # --- Запуск заливки: выбор количества сообществ ---
    if data.startswith("startvk:"):
        choice = data.split(":", 1)[1]
        if choice == "cancel":
            tg_answer_callback(cb_id, "Отменено")
            tg_edit_message(chat_id, msg_id, "❌ Запуск отменён.")
            return
        try:
            n = int(choice)
            if n < 0: n = 0
        except ValueError:
            tg_answer_callback(cb_id, "Ошибка"); return

        # Запускаем заливку с размером этапа stage_size
        push_task("start_vk", uid, chat_id=chat_id,
                  args={"stage_size": n})
        label = "все сразу (один этап)" if n == 0 else f"{n} за этап"
        tg_answer_callback(cb_id, f"Запускаю: {label}")
        online = pc_is_online()
        tail = ("ПК онлайн — заберёт в течение 5 сек."
                if online
                else "ПК офлайн — выполнится как только включится.")
        tg_edit_message(chat_id, msg_id,
                        f"✅ Задача в очереди.\n"
                        f"🎯 Размер этапа: {label}\n"
                        f"💻 {tail}\n\n"
                        f"После каждого этапа жми «▶️ Продолжить заливку».")
        return

    # --- Stats-clips: выбор периода ---
    if data.startswith("stclip:"):
        choice = data.split(":", 1)[1]
        if choice == "cancel":
            with state_lock:
                pending_stats_dialog.pop(int(chat_id), None)
            tg_answer_callback(cb_id, "Отменено")
            tg_edit_message(chat_id, msg_id, "❌ Сбор статистики отменён.")
            return
        if choice == "custom":
            with state_lock:
                pending_stats_dialog[int(chat_id)] = {
                    "step": "from",
                    "started_at": time.time(),
                }
            tg_answer_callback(cb_id, "Введи даты")
            tg_edit_message(chat_id, msg_id,
                            "⌨️ Ручной ввод.\n\n"
                            "Введи дату ОТ в формате YYYY-MM-DD (например 2026-01-15).\n"
                            "Любое сообщение /cancel — отмена.")
            return
        # Быстрый период: число дней
        try:
            days = int(choice)
        except ValueError:
            tg_answer_callback(cb_id, "Ошибка"); return
        # Сразу спрашиваем порог через inline
        with state_lock:
            pending_stats_dialog[int(chat_id)] = {
                "step": "views_quick",
                "days": days,
                "started_at": time.time(),
            }
        tg_answer_callback(cb_id, f"Период {days} дней")
        tg_edit_message(chat_id, msg_id,
                        f"📅 Период: последние {days} дней.\n\nВыбери мин. просмотры:",
                        reply_markup=clips_stats_threshold_inline())
        return

    # --- Stats-clips: выбор порога просмотров ---
    if data.startswith("stclipmv:"):
        try:
            mv = int(data.split(":", 1)[1])
        except ValueError:
            tg_answer_callback(cb_id, "Ошибка"); return
        with state_lock:
            state = pending_stats_dialog.pop(int(chat_id), None)
        if not state:
            tg_answer_callback(cb_id, "Сессия истекла", show_alert=True)
            tg_edit_message(chat_id, msg_id, "⚠️ Сессия истекла. Нажми «🎯 Стата клипов» снова.")
            return

        if state.get("step") == "views_quick":
            # Быстрый период
            days = int(state.get("days", 30))
            tg_answer_callback(cb_id, f"Запускаю ({days} дней, ≥ {mv})")
            tg_edit_message(chat_id, msg_id,
                            f"🚀 Запускаю: последние {days} дней, ≥ {mv} просмотров.")
            _start_clips_stats_task(chat_id, uid, days=days, min_views=mv)
            return
        elif state.get("step") == "views":
            # Из ручного FSM
            df = state.get("date_from"); dt = state.get("date_to")
            tg_answer_callback(cb_id, f"Запускаю (≥ {mv})")
            tg_edit_message(chat_id, msg_id,
                            f"🚀 Запускаю: {df} … {dt}, ≥ {mv} просмотров.")
            push_task("clips_stats", uid, chat_id=chat_id,
                      args={"date_from": df, "date_to": dt, "min_views": mv})
            send(chat_id, "Жди — xlsx придёт сюда.", reply_markup=main_menu())
            return
        else:
            tg_answer_callback(cb_id, "Неожиданное состояние")
            return

    # Видеозапись N сек
    if data.startswith("rec:"):
        try:
            sec = int(data.split(":")[1])
        except Exception:
            sec = 10
        if sec not in (5, 10, 15, 20):
            sec = 10
        push_task("screenrecord", uid, chat_id=chat_id, args={"seconds": sec})
        tg_answer_callback(cb_id, f"Записываю {sec} сек…")
        tg_edit_message(chat_id, msg_id, f"🎬 Записываю экран {sec} сек… Видео придёт следом.")
        return

    # Подтверждения power-команд
    if data.startswith("confirm:") or data.startswith("cancel:"):
        verdict, action = data.split(":", 1)
        if verdict == "cancel":
            tg_answer_callback(cb_id, "Отменено")
            tg_edit_message(chat_id, msg_id, "❌ Отменено.")
            return

        # одноразовые действия
        if action == "shutdown_now":
            push_task("power_action", uid, chat_id=chat_id, args={"action": "shutdown"})
            tg_answer_callback(cb_id, "🔌 Выключаю...")
            tg_edit_message(chat_id, msg_id, "🔌 Команда отправлена ПК.")
            return
        if action == "sleep_now":
            push_task("power_action", uid, chat_id=chat_id, args={"action": "sleep"})
            tg_answer_callback(cb_id, "😴 Усыпляю...")
            tg_edit_message(chat_id, msg_id, "😴 Команда отправлена ПК.")
            return
        if action == "lock_now":
            push_task("power_action", uid, chat_id=chat_id, args={"action": "lock"})
            tg_answer_callback(cb_id, "🔒 Блокирую...")
            tg_edit_message(chat_id, msg_id, "🔒 Команда отправлена ПК.")
            return
        if action == "reboot_now":
            push_task("power_action", uid, chat_id=chat_id, args={"action": "reboot"})
            tg_answer_callback(cb_id, "🔄 Перезагружаю...")
            tg_edit_message(chat_id, msg_id, "🔄 Команда отправлена ПК.")
            return

        # отложенные действия (после залива)
        if action == "after_shutdown":
            with state_lock:
                power_after_finish = "shutdown"
            tg_answer_callback(cb_id, "Запомнил")
            tg_edit_message(chat_id, msg_id, "✅ После залива ВЫКЛЮЧУ ПК.")
            return
        if action == "after_sleep":
            with state_lock:
                power_after_finish = "sleep"
            tg_answer_callback(cb_id, "Запомнил")
            tg_edit_message(chat_id, msg_id, "✅ После залива УСЫПЛЮ ПК.")
            return
        if action == "after_lock":
            with state_lock:
                power_after_finish = "lock"
            tg_answer_callback(cb_id, "Запомнил")
            tg_edit_message(chat_id, msg_id, "✅ После залива ЗАБЛОКИРУЮ экран.")
            return

    tg_answer_callback(cb_id, "?")

# ---------- POLLING ----------
def telegram_polling_loop():
    global update_offset
    print("📡 Стартую Telegram long-polling", flush=True)
    while True:
        try:
            r = requests.get(
                f"{API_URL}/getUpdates",
                params={"offset": update_offset, "timeout": 25,
                        "allowed_updates": json.dumps(["message", "callback_query"])},
                timeout=35,
            ).json()
            if not r.get("ok"):
                time.sleep(2); continue
            for upd in r.get("result", []):
                update_offset = upd["update_id"] + 1
                try:
                    if "message" in upd:
                        handle_message(upd["message"])
                    elif "callback_query" in upd:
                        handle_callback_query(upd["callback_query"])
                except Exception as e:
                    print(f"handler error: {e}", flush=True)
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
    if not _check_token(): return jsonify({"error": "unauth"}), 401
    global last_pc_ping, last_pc_running, last_pc_meta
    last_pc_ping = time.time()
    last_pc_running = (request.args.get("running", "") == "1")
    meta = request.args.get("meta")
    if meta:
        try: last_pc_meta = json.loads(meta)
        except Exception: last_pc_meta = {"raw": meta[:200]}
    return jsonify({"task": pop_task()})

@app.route("/pc/log", methods=["POST"])
def pc_log():
    if not _check_token(): return jsonify({"error": "unauth"}), 401
    global last_pc_ping
    last_pc_ping = time.time()

    payload = request.get_json(silent=True) or {}
    line = (payload.get("line") or "").rstrip()
    if not line:
        return jsonify({"ok": True})

    is_err = is_error_line(line)
    is_smt = is_smart_line(line)

    with state_lock:
        last_lines.append(line)
        if is_err:
            error_lines.append(line)
        all_targets = list(all_log_watchers)
        smart_targets = list(smart_watchers) if (is_smt and not is_err) else []

    text_to_send = line[:3900]
    for w in all_targets:
        try: tg("sendMessage", chat_id=w, text=text_to_send, disable_notification="true")
        except Exception: pass
    for w in smart_targets:
        try: tg("sendMessage", chat_id=w, text=text_to_send, disable_notification="true")
        except Exception: pass

    return jsonify({"ok": True})

@app.route("/pc/result", methods=["POST"])
def pc_result():
    if not _check_token(): return jsonify({"error": "unauth"}), 401
    global last_pc_running, power_after_finish
    payload = request.get_json(silent=True) or {}
    last_pc_running = False
    text = payload.get("text") or "Задача завершена."

    with state_lock:
        targets = set(all_log_watchers) | set(smart_watchers) | set(admins)

    for w in targets:
        try:
            tg("sendMessage", chat_id=w, text=f"🏁 {text}"[:3900])
        except Exception:
            pass

    # авто-отправка ошибок файлом
    auto_send_errors_after_finish()

    # отложенное действие
    with state_lock:
        action = power_after_finish
        power_after_finish = None
    if action:
        push_task("power_action", OWNER_USER_ID, chat_id=OWNER_USER_ID, args={"action": action})
        for w in targets:
            try:
                tg("sendMessage", chat_id=w,
                   text=f"⏏️ Залив завершён — выполняю {action}.")
            except Exception: pass

    return jsonify({"ok": True})

@app.route("/pc/reply_text", methods=["POST"])
def pc_reply_text():
    if not _check_token(): return jsonify({"error": "unauth"}), 401
    payload = request.get_json(silent=True) or {}
    chat_id = payload.get("chat_id"); text = payload.get("text") or ""
    if not chat_id or not text:
        return jsonify({"error": "chat_id and text required"}), 400
    tg_send_text(chat_id, text, reply_markup=main_menu())
    return jsonify({"ok": True})

@app.route("/pc/reply_photo", methods=["POST"])
def pc_reply_photo():
    if not _check_token(): return jsonify({"error": "unauth"}), 401
    chat_id = request.form.get("chat_id"); caption = request.form.get("caption", "")
    if not chat_id: return jsonify({"error": "chat_id required"}), 400
    f = request.files.get("photo")
    if not f: return jsonify({"error": "photo file required"}), 400
    tg_send_photo_bytes(chat_id, f.read(), caption=caption)
    return jsonify({"ok": True})

@app.route("/pc/reply_video", methods=["POST"])
def pc_reply_video():
    if not _check_token(): return jsonify({"error": "unauth"}), 401
    chat_id = request.form.get("chat_id"); caption = request.form.get("caption", "")
    filename = request.form.get("filename", "video.mp4")
    if not chat_id: return jsonify({"error": "chat_id required"}), 400
    f = request.files.get("video")
    if not f: return jsonify({"error": "video required"}), 400
    tg_send_video_bytes(chat_id, f.read(), filename=filename, caption=caption)
    return jsonify({"ok": True})

@app.route("/pc/reply_document", methods=["POST"])
def pc_reply_document():
    if not _check_token(): return jsonify({"error": "unauth"}), 401
    chat_id = request.form.get("chat_id"); caption = request.form.get("caption", "")
    filename = request.form.get("filename", "file.txt")
    if not chat_id: return jsonify({"error": "chat_id required"}), 400
    f = request.files.get("document")
    if not f: return jsonify({"error": "document required"}), 400
    tg_send_document_bytes(chat_id, f.read(), filename=filename, caption=caption)
    return jsonify({"ok": True})

@app.route("/pc/file/<task_id>", methods=["GET"])
def pc_get_file(task_id):
    if not _check_token(): abort(401)
    with state_lock:
        entry = pending_files.get(task_id)
    if not entry: abort(404)
    return Response(entry["bytes"], mimetype="application/octet-stream",
                    headers={"X-Filename": entry["filename"],
                             "Content-Disposition": f'attachment; filename="{entry["filename"]}"'})

@app.route("/pc/file/<task_id>", methods=["DELETE"])
def pc_del_file(task_id):
    if not _check_token(): abort(401)
    with state_lock:
        pending_files.pop(task_id, None)
    return jsonify({"ok": True})

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True, "pc_online": pc_is_online(), "queue_len": len(task_queue),
        "running": last_pc_running, "pending_files": len(pending_files),
        "power_after": power_after_finish,
    })

@app.route("/", methods=["GET"])
def root():
    return "VK Clips Bot — Railway Edge ✅"

# ---------- MAIN ----------
def main():
    threading.Thread(target=telegram_polling_loop, daemon=True).start()
    print(f"🚀 Стартую HTTP API на порту {HTTP_PORT}", flush=True)
    app.run(host="0.0.0.0", port=HTTP_PORT, threaded=True, use_reloader=False)

if __name__ == "__main__":
    main()
