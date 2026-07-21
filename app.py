"""
Vest Chat — Telegram Mini App
=============================

Telegram-клиент в вебе. Список аккаунтов → список чатов → переписка
с автообновлением (polling 2 сек) и реальной отправкой.

Стек: Flask + Telethon (sync) + psycopg + Jinja2
"""

import os
import json
import hmac
import hashlib
import time
import secrets
import urllib.parse
import logging
import threading
from datetime import datetime
from functools import wraps
from pathlib import Path

import jinja2
from flask import Flask, request, jsonify, send_from_directory
from telethon.sync import TelegramClient
from telethon.errors import (
    FloodWaitError, ChatWriteForbiddenError, UserBannedInChannelError,
    AuthKeyUnregisteredError, SessionPasswordNeededError,
    ChatAdminRequiredError, ChannelPrivateError, UserNotParticipantError,
)
from telethon.sessions import StringSession
from telethon.tl.types import (
    User, Chat, Channel, UserEmpty, ChatEmpty, ChannelForbidden,
)
from psycopg.rows import dict_row

try:
    from psycopg_pool import ConnectionPool
    USE_POOL = True
except ImportError:
    USE_POOL = False

# --- Конфигурация ---
BOT_TOKEN = os.getenv(
    "BOT_TOKEN",
    "8805400400:AAGAX6L8ohYpciEABCzPq5iJx-N8psw_Zx0",
)
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://bothost_db_c7b70c49a8ed:QyhslYwQU7g1hT4OD69RP9jcV3EkzmXRLj4VH703ahQ@node1.pghost.ru:15761/bothost_db_c7b70c49a8ed",
)
API_ID = int(os.getenv("API_ID", "32480523") or 0)
API_HASH = os.getenv("API_HASH", "147839735c9fa4e83451209e9b55cfc5")
SECRET_KEY = os.getenv("SECRET_KEY", "vest-chat-" + secrets.token_hex(8))
APP_NAME = "Vest Chat"
DEBUG = os.getenv("DEBUG", "0") == "1"

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [chat] %(levelname)s %(message)s",
)
logger = logging.getLogger("vest.chat")

app = Flask(__name__, static_folder=None)
app.secret_key = SECRET_KEY
app.config["JSON_AS_ASCII"] = False

# --- БД ---
if USE_POOL:
    pool = ConnectionPool(
        DATABASE_URL, min_size=1, max_size=8,
        kwargs={"row_factory": dict_row, "autocommit": True}, open=True,
    )
else:
    pool = None

    def _connect():
        import psycopg
        return psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True)


def db_query(sql, params=()):
    if USE_POOL:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params); return cur.fetchall()
    else:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params); return cur.fetchall()


def db_one(sql, params=()):
    rows = db_query(sql, params)
    return rows[0] if rows else None


def db_exec(sql, params=()):
    if USE_POOL:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params); return cur.rowcount
    else:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params); return cur.rowcount


def init_db():
    """Создаёт новые таблицы для чат-клиента, если их ещё нет."""
    db_exec("""
        CREATE TABLE IF NOT EXISTS account_dialogs (
            id BIGSERIAL PRIMARY KEY,
            account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
            chat_id BIGINT NOT NULL,
            chat_type TEXT,
            title TEXT,
            username TEXT,
            last_message_text TEXT,
            last_message_date TIMESTAMP,
            unread_count INTEGER DEFAULT 0,
            is_pinned BOOLEAN DEFAULT FALSE,
            updated_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(account_id, chat_id)
        )
    """)
    db_exec("""
        CREATE INDEX IF NOT EXISTS idx_dialogs_account
        ON account_dialogs(account_id, last_message_date DESC NULLS LAST)
    """)
    db_exec("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id BIGSERIAL PRIMARY KEY,
            account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
            chat_id BIGINT NOT NULL,
            message_id BIGINT NOT NULL,
            sender_id BIGINT,
            sender_name TEXT,
            text TEXT,
            is_outgoing BOOLEAN DEFAULT FALSE,
            sent_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(account_id, chat_id, message_id)
        )
    """)
    db_exec("""
        CREATE INDEX IF NOT EXISTS idx_messages_recent
        ON chat_messages(account_id, chat_id, message_id DESC)
    """)


# --- Аутентификация через Telegram initData ---

def validate_init_data(init_data: str) -> dict | None:
    if not init_data or not BOT_TOKEN:
        return None
    try:
        parsed = urllib.parse.parse_qs(init_data, keep_blank_values=True)
        received_hash = parsed.pop("hash", [None])[0]
        if not received_hash:
            return None
        data_check_arr = sorted(f"{k}={v[0]}" for k, v in parsed.items())
        data_check_string = "\n".join(data_check_arr)
        secret_key = hmac.new(
            b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256
        ).digest()
        calculated = hmac.new(
            secret_key, data_check_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(calculated, received_hash):
            return None
        auth_date = int(parsed.get("auth_date", ["0"])[0] or 0)
        if auth_date and (time.time() - auth_date) > 3600:
            return None
        user_raw = parsed.get("user", [None])[0]
        return json.loads(user_raw) if user_raw else None
    except Exception as e:
        logger.warning("initData validation error: %s", e)
        return None


def get_user_from_request() -> dict | None:
    init_data = ""
    if request.method == "POST" and request.is_json:
        init_data = (request.json or {}).get("initData", "")
    if not init_data:
        init_data = request.headers.get("X-Telegram-Init-Data", "")
    if not init_data:
        init_data = request.cookies.get("tg_init", "")
    if not init_data:
        init_data = request.args.get("_init", "")
    if not init_data:
        return None
    return validate_init_data(init_data)


def require_auth(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        user = get_user_from_request()
        if not user and DEBUG:
            dev_id = request.args.get("dev_user_id")
            if dev_id:
                user = {"id": int(dev_id), "first_name": "Dev", "username": "dev"}
        if not user:
            return render_template_string(LOGIN_PAGE, app_name=APP_NAME), 401
        try:
            db_exec(
                """INSERT INTO users (user_id, username, first_name)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (user_id) DO UPDATE
                   SET username = EXCLUDED.username,
                       first_name = EXCLUDED.first_name""",
                (user["id"], user.get("username"), user.get("first_name")),
            )
        except Exception as e:
            logger.warning("user upsert failed: %s", e)
        return view(user=user, *args, **kwargs)
    return wrapper


# --- Менеджер Telethon-клиентов ---

class ClientManager:
    """Ленивое подключение Telethon-клиентов с per-account lock."""

    def __init__(self):
        self._clients: dict = {}    # account_id -> TelegramClient
        self._locks: dict = {}      # account_id -> Lock
        self._meta_lock = threading.Lock()
        self._creds_missing = False

    def _check_creds(self):
        if not API_ID or not API_HASH:
            if not self._creds_missing:
                logger.error("API_ID / API_HASH not set!")
            self._creds_missing = True
            return False
        return True

    def get_client(self, account: dict):
        """Возвращает (client, lock). Lock нужно использовать при вызовах."""
        if not self._check_creds():
            raise RuntimeError("API_ID/API_HASH не настроены")
        acc_id = account["id"]
        with self._meta_lock:
            if acc_id not in self._clients:
                proxy = None
                if account.get("proxy_id"):
                    proxy = db_one(
                        "SELECT * FROM proxies WHERE id = %s",
                        (account["proxy_id"],),
                    )
                proxy_arg = None
                if proxy:
                    type_map = {"socks5": 2, "socks4": 1, "http": 3}
                    ptype = type_map.get(proxy["proxy_type"].lower(), 2)
                    proxy_arg = (
                        ptype, proxy["host"], int(proxy["port"]),
                        True,
                        proxy.get("username") or None,
                        proxy.get("password") or None,
                    )
                client = TelegramClient(
                    StringSession(account["session_string"]),
                    API_ID, API_HASH, proxy=proxy_arg,
                )
                client.connect()
                self._clients[acc_id] = client
                self._locks[acc_id] = threading.Lock()
            return self._clients[acc_id], self._locks[acc_id]

    def disconnect_all(self):
        with self._meta_lock:
            for c in self._clients.values():
                try: c.disconnect()
                except Exception: pass
            self._clients.clear()
            self._locks.clear()


cm = ClientManager()


def get_account_for_user(account_id: int, user_id: int) -> dict | None:
    return db_one(
        "SELECT * FROM accounts WHERE id = %s AND user_id = %s",
        (account_id, user_id),
    )


def get_proxy_for_account(account: dict) -> dict | None:
    if not account.get("proxy_id"):
        return None
    return db_one("SELECT * FROM proxies WHERE id = %s", (account["proxy_id"],))


def fmt_phone(p: str) -> str:
    if not p:
        return "—"
    p = p.strip()
    if p.startswith("+") and len(p) >= 7:
        return f"{p[:5]}***{p[-3:]}"
    return p


def avatar_color(seed: str) -> str:
    """Стабильный цвет по строке."""
    palette = [
        ("#FF6B6B", "#FF8E53"), ("#5B86E5", "#36D1DC"),
        ("#11998E", "#38EF7D"), ("#FC466B", "#3F5EFB"),
        ("#F7971E", "#FFD200"), ("#834D9B", "#D04ED6"),
        ("#4ECDC4", "#556270"), ("#FF9966", "#FF5E62"),
    ]
    h = sum(ord(c) for c in (seed or "")) % len(palette)
    return f"linear-gradient(135deg, {palette[h][0]} 0%, {palette[h][1]} 100%)"


def fmt_time(dt) -> str:
    if not dt:
        return ""
    if isinstance(dt, str):
        return dt
    now = datetime.now()
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    if (now.date() - dt.date()).days == 1:
        return "вчера"
    if (now.date() - dt.date()).days < 7:
        return dt.strftime("%a")  # Mon, Tue
    return dt.strftime("%d.%m.%y")


# =========================================================================
#  ROUTES
# =========================================================================

@app.route("/healthz")
def healthz():
    try:
        db_one("SELECT 1 AS ok")
        return jsonify(status="ok", db="up", api=bool(API_ID and API_HASH))
    except Exception as e:
        return jsonify(status="degraded", error=str(e)), 500


@app.route("/static/<path:filename>")
def static_files(filename: str):
    return send_from_directory(STATIC_DIR, filename)


@app.route("/")
@require_auth
def accounts_page(user):
    accounts = db_query(
        """SELECT a.id, a.phone, a.is_active, a.created_at, a.warming_enabled,
                  p.label AS proxy_label, p.host AS proxy_host
           FROM accounts a
           LEFT JOIN proxies p ON p.id = a.proxy_id
           WHERE a.user_id = %s
           ORDER BY a.id DESC""",
        (user["id"],),
    )
    return _render("accounts", user=user, accounts=accounts)


@app.route("/account/<int:account_id>")
@require_auth
def chats_page(user, account_id: int):
    account = get_account_for_user(account_id, user["id"])
    if not account:
        return render_template_string(
            ALERT_HTML, message="Аккаунт не найден", back_url="/",
        ), 404
    return _render("chats", user=user, account=account)


@app.route("/chat/<int:account_id>/<int:chat_id>")
@require_auth
def chat_page(user, account_id: int, chat_id: int):
    account = get_account_for_user(account_id, user["id"])
    if not account:
        return render_template_string(
            ALERT_HTML, message="Аккаунт не найден", back_url="/",
        ), 404
    dlg = db_one(
        """SELECT title, username, chat_type FROM account_dialogs
           WHERE account_id = %s AND chat_id = %s""",
        (account_id, chat_id),
    )
    title = (dlg or {}).get("title") if dlg else None
    if not title:
        title = f"Chat {chat_id}"
    return _render("chat", user=user, account=account, chat_id=chat_id, title=title)


# --- API ---

@app.route("/api/account/<int:account_id>/info")
@require_auth
def api_account_info(user, account_id: int):
    account = get_account_for_user(account_id, user["id"])
    if not account:
        return jsonify(ok=False, error="not found"), 404
    try:
        client, lock = cm.get_client(account)
        with lock:
            me = client.get_me()
            return jsonify(
                ok=True,
                me={
                    "id": me.id,
                    "first_name": me.first_name or "",
                    "username": me.username or "",
                    "phone": me.phone or "",
                },
            )
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/account/<int:account_id>/chats", methods=["GET"])
@require_auth
def api_chats(user, account_id: int):
    account = get_account_for_user(account_id, user["id"])
    if not account:
        return jsonify(ok=False, error="not found"), 404
    force = request.args.get("refresh") == "1"
    try:
        client, lock = cm.get_client(account)
        with lock:
            dialogs = client.get_dialogs(limit=100)
        now = datetime.now()
        # Сохраняем в БД (upsert)
        for d in dialogs:
            entity = d.entity
            chat_id = d.id
            chat_type = (
                "private" if isinstance(entity, User)
                else "group" if isinstance(entity, Chat)
                else "channel" if isinstance(entity, Channel)
                else "unknown"
            )
            title = (
                (entity.first_name or "") + (" " + entity.last_name if getattr(entity, "last_name", None) else "")
                if isinstance(entity, User) else getattr(entity, "title", "") or ""
            ).strip() or "(без имени)"
            username = getattr(entity, "username", None)
            last_text = (d.message.message or "")[:200] if d.message else ""
            last_date = d.message.date if d.message else None
            unread = d.unread_count or 0
            db_exec(
                """INSERT INTO account_dialogs
                   (account_id, chat_id, chat_type, title, username,
                    last_message_text, last_message_date, unread_count, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                   ON CONFLICT (account_id, chat_id) DO UPDATE SET
                     chat_type = EXCLUDED.chat_type,
                     title = EXCLUDED.title,
                     username = EXCLUDED.username,
                     last_message_text = EXCLUDED.last_message_text,
                     last_message_date = EXCLUDED.last_message_date,
                     unread_count = EXCLUDED.unread_count,
                     updated_at = NOW()
                """,
                (account_id, chat_id, chat_type, title, username,
                 last_text, last_date, unread),
            )
    except Exception as e:
        logger.exception("get_dialogs failed")
        return jsonify(ok=False, error=str(e)), 500

    rows = db_query(
        """SELECT chat_id, chat_type, title, username,
                  last_message_text, last_message_date, unread_count
           FROM account_dialogs
           WHERE account_id = %s
           ORDER BY last_message_date DESC NULLS LAST, chat_id DESC""",
        (account_id,),
    )
    return jsonify(ok=True, chats=[{
        "chat_id": r["chat_id"],
        "chat_type": r["chat_type"],
        "title": r["title"] or "—",
        "username": r["username"] or "",
        "last_message_text": r["last_message_text"] or "",
        "last_message_date": r["last_message_date"].isoformat() if r["last_message_date"] else "",
        "unread_count": r["unread_count"] or 0,
    } for r in rows])


@app.route("/api/chat/<int:account_id>/<int:chat_id>/messages", methods=["GET"])
@require_auth
def api_messages(user, account_id: int, chat_id: int):
    """Получить сообщения. ?since=<message_id> для инкрементальных обновлений."""
    account = get_account_for_user(account_id, user["id"])
    if not account:
        return jsonify(ok=False, error="not found"), 404
    since = request.args.get("since", 0, type=int)
    limit = min(request.args.get("limit", 50, type=int), 200)
    try:
        client, lock = cm.get_client(account)
        with lock:
            if since > 0:
                # Инкрементальный запрос
                msgs = client.get_messages(chat_id, min_id=since, limit=limit)
            else:
                msgs = client.get_messages(chat_id, limit=limit)
        # Кэшируем
        for m in msgs:
            try:
                sender = await_get_sender(client, lock, m)
            except Exception:
                sender = None
            sender_id = m.sender_id
            sender_name = ""
            if sender:
                if isinstance(sender, User):
                    sender_name = (
                        (sender.first_name or "") +
                        (" " + sender.last_name if getattr(sender, "last_name", None) else "")
                    ).strip()
                    if not sender_name and sender.username:
                        sender_name = "@" + sender.username
                else:
                    sender_name = getattr(sender, "title", "") or ""
            if not sender_name:
                sender_name = f"id{sender_id}" if sender_id else "—"
            db_exec(
                """INSERT INTO chat_messages
                   (account_id, chat_id, message_id, sender_id, sender_name,
                    text, is_outgoing, sent_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (account_id, chat_id, message_id) DO NOTHING""",
                (account_id, chat_id, m.id, sender_id, sender_name,
                 (m.text or "")[:4000], bool(m.outgoing),
                 m.date),
            )
        # Сортируем по id ASC (старые -> новые, для UI)
        msgs_sorted = sorted(msgs, key=lambda x: x.id)
        result = [{
            "id": m.id,
            "text": m.text or "",
            "outgoing": bool(m.outgoing),
            "sender_id": m.sender_id,
            "sender_name": get_cached_sender_name(account_id, chat_id, m.id),
            "date": m.date.isoformat() if m.date else "",
        } for m in msgs_sorted]
        return jsonify(ok=True, messages=result,
                       max_id=max((m.id for m in msgs), default=since))
    except Exception as e:
        logger.exception("get_messages failed")
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/chat/<int:account_id>/<int:chat_id>/send", methods=["POST"])
@require_auth
def api_send(user, account_id: int, chat_id: int):
    account = get_account_for_user(account_id, user["id"])
    if not account:
        return jsonify(ok=False, error="not found"), 404
    data = request.json or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify(ok=False, error="Пустое сообщение"), 400
    if len(text) > 4096:
        return jsonify(ok=False, error="Слишком длинное (макс 4096)"), 400
    try:
        client, lock = cm.get_client(account)
        with lock:
            me = client.get_me()
            sent = client.send_message(chat_id, text)
        # Сохраняем в кэш
        try:
            db_exec(
                """INSERT INTO chat_messages
                   (account_id, chat_id, message_id, sender_id, sender_name,
                    text, is_outgoing, sent_at)
                   VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s)
                   ON CONFLICT (account_id, chat_id, message_id) DO NOTHING""",
                (account_id, chat_id, sent.id, me.id,
                 me.first_name or "me", text, sent.date),
            )
        except Exception as e:
            logger.warning("cache save failed: %s", e)
        return jsonify(ok=True, message={
            "id": sent.id,
            "text": sent.text or "",
            "outgoing": True,
            "sender_id": me.id,
            "sender_name": me.first_name or "me",
            "date": sent.date.isoformat() if sent.date else "",
        })
    except FloodWaitError as e:
        return jsonify(ok=False, error=f"Flood wait: {e.seconds}s", flood=e.seconds), 429
    except ChatWriteForbiddenError:
        return jsonify(ok=False, error="Нет прав писать в этот чат"), 403
    except UserBannedInChannelError:
        return jsonify(ok=False, error="Забанен в канале"), 403
    except (AuthKeyUnregisteredError, SessionPasswordNeededError):
        return jsonify(ok=False, error="Сессия невалидна (переавторизуйтесь)"), 401
    except (ChannelPrivateError, UserNotParticipantError, ChatAdminRequiredError) as e:
        return jsonify(ok=False, error=f"Нет доступа: {type(e).__name__}"), 403
    except Exception as e:
        logger.exception("send_message failed")
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/chat/<int:account_id>/<int:chat_id>/read", methods=["POST"])
@require_auth
def api_mark_read(user, account_id: int, chat_id: int):
    """Помечает чат прочитанным (сбрасывает unread)."""
    account = get_account_for_user(account_id, user["id"])
    if not account:
        return jsonify(ok=False, error="not found"), 404
    try:
        client, lock = cm.get_client(account)
        with lock:
            client.send_read_acknowledge(chat_id)
        db_exec(
            "UPDATE account_dialogs SET unread_count = 0 "
            "WHERE account_id = %s AND chat_id = %s",
            (account_id, chat_id),
        )
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


# --- Хелперы для sender ---

def await_get_sender(client, lock, message):
    """Синхронно получить sender — Telethon может потребовать resolve."""
    with lock:
        return message.get_sender()


def get_cached_sender_name(account_id, chat_id, message_id) -> str:
    row = db_one(
        """SELECT sender_name FROM chat_messages
           WHERE account_id = %s AND chat_id = %s AND message_id = %s""",
        (account_id, chat_id, message_id),
    )
    return (row or {}).get("sender_name", "—") if row else "—"


# =========================================================================
#  TEMPLATES
# =========================================================================

BASE_CSS = """
:root{
  --bg: var(--tg-theme-bg-color, #ffffff);
  --bg-2: var(--tg-theme-secondary-bg-color, #f4f7fb);
  --text: var(--tg-theme-text-color, #0f172a);
  --hint: var(--tg-theme-hint-color, #6b7c93);
  --link: var(--tg-theme-link-color, #2481cc);
  --accent: var(--tg-theme-button-color, #2481cc);
  --header-bg: var(--tg-theme-header-bg-color, var(--bg));
  --bottom-bar-bg: var(--tg-theme-bottom-bar-bg-color, var(--bg));
  --section-bg: var(--tg-theme-section-bg-color, var(--bg-2));
  --accent-grad: linear-gradient(135deg, #5fb3f0 0%, #2481cc 100%);
  --shadow: 0 4px 24px rgba(36,129,204,.08);
  --shadow-sm: 0 1px 4px rgba(0,0,0,.06);
  --radius: 18px;
}
@media (prefers-color-scheme: dark){
  :root{ --bg: #17212b; --bg-2: #0e1621; --text: #ffffff; --hint: #708499; }
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{
  background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",Roboto,sans-serif;
  font-size:15px;line-height:1.4;min-height:100vh;
  -webkit-font-smoothing:antialiased;overscroll-behavior:none
}
body{display:flex;flex-direction:column;height:100vh;overflow:hidden}
a{color:var(--link);text-decoration:none}

/* Header */
.hdr{position:sticky;top:0;z-index:20;background:var(--header-bg);
  border-bottom:1px solid rgba(0,0,0,.06);
  display:flex;align-items:center;gap:10px;padding:10px 14px;
  backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
  flex-shrink:0}
.hdr .back{color:var(--link);font-size:24px;line-height:1;width:30px;cursor:pointer;
  display:flex;align-items:center;justify-content:center;user-select:none}
.hdr .title{font-weight:600;font-size:16px;flex:1;min-width:0;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hdr .sub{font-size:11px;color:var(--hint);font-weight:400;margin-top:1px}
.hdr .actions{display:flex;gap:6px}
.hdr .icon-btn{width:36px;height:36px;border-radius:10px;background:var(--bg-2);
  color:var(--text);display:flex;align-items:center;justify-content:center;
  border:none;cursor:pointer;font-size:16px}
.hdr .icon-btn:active{background:rgba(0,0,0,.05)}

/* Account list */
.scroll{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;
  padding-bottom:env(safe-area-inset-bottom)}
.account{padding:14px;background:var(--bg-2);
  border-radius:14px;margin:10px 14px;display:flex;align-items:center;gap:12px;
  cursor:pointer;transition:transform .1s}
.account:active{transform:scale(.98)}
.account .av{width:48px;height:48px;border-radius:50%;
  display:flex;align-items:center;justify-content:center;
  color:#fff;font-weight:700;font-size:18px;flex-shrink:0}
.account .meta{flex:1;min-width:0}
.account .name{font-weight:600;font-size:15px}
.account .sub{font-size:12px;color:var(--hint);margin-top:2px}
.account .arrow{color:var(--hint);font-size:20px}

/* Chat list */
.section-title{font-size:12px;font-weight:700;color:var(--hint);
  text-transform:uppercase;letter-spacing:.06em;margin:14px 14px 6px}
.chat-row{padding:10px 14px;display:flex;align-items:center;gap:12px;
  cursor:pointer;border-bottom:1px solid rgba(0,0,0,.04)}
.chat-row:active{background:rgba(0,0,0,.03)}
.chat-row .av{width:50px;height:50px;border-radius:50%;
  display:flex;align-items:center;justify-content:center;
  color:#fff;font-weight:700;font-size:20px;flex-shrink:0;
  position:relative}
.chat-row .badge{position:absolute;top:-2px;right:-2px;
  background:#ff3b30;color:#fff;border-radius:10px;
  font-size:10px;font-weight:700;padding:2px 6px;min-width:18px;text-align:center;
  border:2px solid var(--bg)}
.chat-row .meta{flex:1;min-width:0}
.chat-row .top{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.chat-row .name{font-weight:600;font-size:15px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chat-row .time{font-size:11px;color:var(--hint);flex-shrink:0}
.chat-row .preview{font-size:13px;color:var(--hint);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}
.chat-row .preview.outgoing-prefix::before{content:"Вы: ";color:var(--hint);font-weight:600}
.empty{text-align:center;padding:60px 20px;color:var(--hint)}
.empty .big{font-size:48px;opacity:.4;margin-bottom:10px}

/* Chat detail */
.chat-wrap{flex:1;display:flex;flex-direction:column;overflow:hidden;
  background:var(--bg)}
.messages{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;
  padding:14px 12px;display:flex;flex-direction:column;gap:4px}
.bubble{max-width:78%;padding:7px 12px 6px;border-radius:14px;
  position:relative;word-wrap:break-word;animation:pop .2s}
@keyframes pop{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}
.bubble.in{background:var(--bg-2);align-self:flex-start;
  border-bottom-left-radius:4px}
.bubble.out{background:var(--accent-grad);color:#fff;align-self:flex-end;
  border-bottom-right-radius:4px;box-shadow:0 1px 2px rgba(0,0,0,.1)}
.bubble.out.pending{opacity:.6}
.bubble.out.failed{background:linear-gradient(135deg,#ff6b6b,#ee5a5a)}
.bubble .sender{font-size:12px;font-weight:600;color:var(--accent);margin-bottom:2px}
.bubble.out .sender{color:rgba(255,255,255,.85)}
.bubble .text{font-size:15px;white-space:pre-wrap;word-break:break-word}
.bubble .time{font-size:10px;margin-top:2px;text-align:right;opacity:.65}
.bubble.in .time{color:var(--hint)}
.bubble.out .time{color:rgba(255,255,255,.85)}
.system-msg{align-self:center;background:rgba(0,0,0,.05);
  color:var(--hint);font-size:12px;padding:4px 10px;border-radius:10px;margin:6px 0}

.composer{flex-shrink:0;display:flex;gap:8px;align-items:flex-end;
  padding:8px 10px;background:var(--bg);
  border-top:1px solid rgba(0,0,0,.06);
  padding-bottom:calc(8px + env(safe-area-inset-bottom))}
.composer textarea{flex:1;background:var(--bg-2);
  border:1.5px solid transparent;border-radius:18px;
  padding:9px 14px;font-size:15px;resize:none;
  max-height:120px;min-height:38px;font-family:inherit;
  color:var(--text);outline:none}
.composer textarea:focus{border-color:var(--accent);background:var(--bg)}
.composer .send{width:38px;height:38px;border-radius:50%;
  background:var(--accent-grad);border:none;color:#fff;
  font-size:18px;cursor:pointer;display:flex;align-items:center;
  justify-content:center;flex-shrink:0;font-weight:700;
  box-shadow:0 2px 8px rgba(36,129,204,.3)}
.composer .send:disabled{opacity:.4;cursor:not-allowed;box-shadow:none}

/* Refresh button */
.refresh-btn{padding:8px 14px;background:var(--accent-grad);color:#fff;
  border:none;border-radius:10px;font-size:13px;font-weight:600;cursor:pointer}
.refresh-btn:disabled{opacity:.5}

/* Loading dots */
.loading{display:inline-flex;gap:3px;padding:4px 10px}
.loading span{width:6px;height:6px;border-radius:50%;background:var(--hint);animation:dot 1.4s infinite}
.loading span:nth-child(2){animation-delay:.2s}
.loading span:nth-child(3){animation-delay:.4s}
@keyframes dot{0%,60%,100%{opacity:.2;transform:translateY(0)}30%{opacity:1;transform:translateY(-3px)}}

.toast{position:fixed;bottom:80px;left:50%;transform:translateX(-50%);
  background:var(--text);color:var(--bg);padding:11px 18px;
  border-radius:12px;font-size:14px;font-weight:500;z-index:100;
  box-shadow:0 8px 24px rgba(0,0,0,.25);max-width:90%;text-align:center;
  animation:slideUp .25s}
@keyframes slideUp{from{transform:translate(-50%,10px);opacity:0}to{transform:translate(-50%,0);opacity:1}}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid rgba(255,255,255,.3);
  border-top-color:#fff;border-radius:50%;animation:sp .8s linear infinite;vertical-align:middle}
@keyframes sp{to{transform:rotate(360deg)}}
"""


BASE_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,user-scalable=no">
<meta name="theme-color" content="#2481cc">
<title>{% block title %}{{ app_name }}{% endblock %}</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>{{ css|safe }}</style>
</head>
<body>
{% block body %}{% endblock %}
<script>
(function(){
  const wa = window.Telegram && Telegram.WebApp;
  if(wa){
    wa.ready();
    wa.expand();
    if(wa.themeParams){
      const r=document.documentElement.style;
      const m={'bg_color':'--bg','secondary_bg_color':'--bg-2',
        'text_color':'--text','hint_color':'--hint',
        'button_color':'--accent','link_color':'--link',
        'header_bg_color':'--header-bg','bottom_bar_bg_color':'--bottom-bar-bg',
        'section_bg_color':'--section-bg','section_separator_color':'--separator'};
      for(const k in m){ if(wa.themeParams[k]) r.setProperty(m[k], wa.themeParams[k]); }
    }
  }
  window.__initData = wa ? wa.initData : '';
  window.__post = (url, body) => fetch(url, {
    method:'POST',
    headers:{'Content-Type':'application/json','X-Telegram-Init-Data': window.__initData || ''},
    body: JSON.stringify(body || {})
  });
  window.__get = (url) => fetch(url, {
    headers:{'X-Telegram-Init-Data': window.__initData || ''}
  });
  window.__toast = (msg, ms=2200) => {
    const t=document.createElement('div');
    t.className='toast';t.textContent=msg;document.body.appendChild(t);
    setTimeout(()=>t.remove(),ms);
  };
})();
</script>
{% block scripts %}{% endblock %}
</body>
</html>
"""


LOGIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<title>Войдите через Telegram</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;
background:#17212b;color:#fff;margin:0;padding:20px}
.box{max-width:380px;background:#0e1621;padding:32px;border-radius:18px;
box-shadow:0 8px 32px rgba(0,0,0,.3);text-align:center}
h1{font-size:20px;margin-bottom:12px}
p{color:#708499;line-height:1.5;font-size:14px}</style></head>
<body><div class="box">
<h1>🔒 {{ app_name }}</h1>
<p>Это приложение доступно только из Telegram.<br>Открой его через
<strong>@VestGamebot</strong> → кнопка с приложением.</p>
</div></body></html>"""


ALERT_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Ошибка</title>
<style>body{font-family:-apple-system,sans-serif;display:flex;align-items:center;justify-content:center;
min-height:100vh;background:#17212b;color:#fff;margin:0;padding:20px}
.box{max-width:380px;background:#0e1621;padding:32px;border-radius:18px;
box-shadow:0 8px 32px rgba(0,0,0,.3);text-align:center}
.btn{display:inline-block;margin-top:16px;padding:10px 20px;background:#2481cc;
color:#fff;border-radius:10px;text-decoration:none;font-weight:600}</style></head>
<body><div class="box">
<p style="color:#ff6b6b;font-weight:600;font-size:16px">⚠ {{ message }}</p>
<a class="btn" href="{{ back_url }}">← Назад</a>
</div></body></html>"""


ACCOUNTS_HTML = """{% extends "base" %}
{% block title %}Аккаунты — {{ app_name }}{% endblock %}
{% block body %}
<div class="hdr">
  <div class="title">{{ app_name }}</div>
  <div class="actions"><div class="icon-btn" title="Помощь" onclick="__toast('Кликни на аккаунт → список чатов → открой чат')">?</div></div>
</div>
<div class="scroll">
  {% if accounts %}
    <div class="section-title">Твои аккаунты ({{ accounts|length }})</div>
    {% for a in accounts %}
    <a class="account" href="/account/{{ a.id }}" style="text-decoration:none;color:inherit">
      <div class="av" style="background:{{ avatar_color(a.phone) }}">
        {{ a.phone[-2:][:2] if a.phone else "?" }}
      </div>
      <div class="meta">
        <div class="name">{{ fmt_phone(a.phone) }}</div>
        <div class="sub">
          {% if a.is_active %}<span style="color:#1ba35b">● online</span>{% else %}<span style="color:#d63838">● offline</span>{% endif %}
          {% if a.proxy_label %} · 🔗 {{ a.proxy_label }}{% endif %}
          {% if a.warming_enabled %} · 🔥 прогрев{% endif %}
        </div>
      </div>
      <div class="arrow">›</div>
    </a>
    {% endfor %}
  {% else %}
    <div class="empty">
      <div class="big">👤</div>
      <p>Нет аккаунтов</p>
      <p style="margin-top:6px;font-size:12px">Добавь аккаунт в @VestGamebot</p>
    </div>
  {% endif %}
</div>
{% endblock %}
"""


CHATS_HTML = """{% extends "base" %}
{% block title %}Чаты — {{ fmt_phone(account.phone) }}{% endblock %}
{% block body %}
<div class="hdr">
  <a class="back" href="/">‹</a>
  <div>
    <div class="title">{{ fmt_phone(account.phone) }}</div>
    <div class="sub" id="meSub">подключаюсь…</div>
  </div>
  <div class="actions">
    <button class="icon-btn" id="refreshBtn" onclick="loadChats(true)" title="Обновить">↻</button>
  </div>
</div>
<div class="scroll" id="chatList">
  <div class="empty"><div class="big">💬</div><p>Загружаю чаты…</p></div>
</div>
{% endblock %}
{% block scripts %}
<script>
const ACCOUNT_ID = {{ account.id }};
const FALLBACK_AVATAR = (name) => {
  const colors = [
    ['#FF6B6B','#FF8E53'],['#5B86E5','#36D1DC'],['#11998E','#38EF7D'],
    ['#FC466B','#3F5EFB'],['#F7971E','#FFD200'],['#834D9B','#D04ED6'],
    ['#4ECDC4','#556270'],['#FF9966','#FF5E62']
  ];
  let h=0; for(const c of (name||'')) h=(h*31+c.charCodeAt(0))&0xffffffff;
  const [a,b] = colors[Math.abs(h)%colors.length];
  return `linear-gradient(135deg, ${a} 0%, ${b} 100%)`;
};
const escapeHtml = (s) => (s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
const fmtTime = (iso) => {
  if(!iso) return '';
  const d = new Date(iso);
  const now = new Date();
  if(d.toDateString()===now.toDateString()) return d.toTimeString().slice(0,5);
  const diff = (now-d)/86400000;
  if(diff<1) return 'вчера';
  if(diff<7) return ['Вс','Пн','Вт','Ср','Чт','Пт','Сб'][d.getDay()];
  return d.toLocaleDateString('ru',{day:'2-digit',month:'2-digit'});
};

async function loadInfo(){
  try {
    const r = await __get('/api/account/'+ACCOUNT_ID+'/info');
    const j = await r.json();
    if(j.ok){
      const me = j.me;
      const name = me.username ? '@'+me.username : (me.first_name || 'id'+me.id);
      document.getElementById('meSub').innerHTML = '<span style="color:#1ba35b">●</span> '+escapeHtml(name);
    } else {
      document.getElementById('meSub').innerHTML = '<span style="color:#d63838">● '+escapeHtml(j.error||'ошибка')+'</span>';
    }
  } catch(e){
    document.getElementById('meSub').textContent = '● offline';
  }
}

async function loadChats(force){
  const btn = document.getElementById('refreshBtn');
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>';
  try {
    const url = '/api/account/'+ACCOUNT_ID+'/chats'+(force?'?refresh=1':'');
    const r = await __get(url);
    const j = await r.json();
    btn.disabled = false; btn.textContent = '↻';
    if(!j.ok){
      document.getElementById('chatList').innerHTML =
        '<div class="empty"><div class="big">⚠</div><p>'+escapeHtml(j.error||'Ошибка')+'</p></div>';
      return;
    }
    if(!j.chats.length){
      document.getElementById('chatList').innerHTML =
        '<div class="empty"><div class="big">💬</div><p>Нет диалогов</p></div>';
      return;
    }
    let html = '';
    let currentType = '';
    for(const c of j.chats){
      const typeLabel = c.chat_type==='private'?'Личные':
                        c.chat_type==='group'?'Группы':
                        c.chat_type==='channel'?'Каналы':'Другое';
      if(typeLabel!==currentType){
        html += '<div class="section-title">'+typeLabel+'</div>';
        currentType = typeLabel;
      }
      const initial = (c.title||'?').trim().charAt(0).toUpperCase();
      const username = c.username ? '<span style="color:var(--hint);font-weight:400">@'+escapeHtml(c.username)+'</span>' : '';
      html += `
        <a class="chat-row" href="/chat/${ACCOUNT_ID}/${c.chat_id}" style="text-decoration:none;color:inherit">
          <div class="av" style="background:${FALLBACK_AVATAR(c.title)}">${escapeHtml(initial)}</div>
          <div class="meta">
            <div class="top">
              <div class="name">${escapeHtml(c.title||'—')}</div>
              <div class="time">${fmtTime(c.last_message_date)}</div>
            </div>
            <div class="preview">${username?'@'+escapeHtml(c.username)+': ':''}${escapeHtml(c.last_message_text||'')}</div>
          </div>
          ${c.unread_count>0?`<div class="badge">${c.unread_count}</div>`:''}
        </a>`;
    }
    document.getElementById('chatList').innerHTML = html;
  } catch(e){
    btn.disabled=false; btn.textContent='↻';
    __toast('Сеть: '+e.message);
  }
}

loadInfo();
loadChats(false);
</script>
{% endblock %}
"""


CHAT_HTML = """{% extends "base" %}
{% block title %}{{ title }} — {{ fmt_phone(account.phone) }}{% endblock %}
{% block body %}
<div class="hdr">
  <a class="back" href="/account/{{ account.id }}">‹</a>
  <div style="flex:1;min-width:0">
    <div class="title" id="chatTitle">{{ title }}</div>
    <div class="sub" id="chatSub">подключаюсь…</div>
  </div>
  <div class="actions">
    <button class="icon-btn" id="markRead" title="Прочитано" onclick="markRead()">✓✓</button>
    <button class="icon-btn" id="reload" title="Перезагрузить" onclick="reload()">↻</button>
  </div>
</div>
<div class="chat-wrap">
  <div class="messages" id="messages">
    <div class="empty" id="loadingMsg">
      <div class="big">💬</div>
      <p>Загружаю сообщения…</p>
    </div>
  </div>
  <form class="composer" id="composer" onsubmit="return sendMessage(event)">
    <textarea id="input" placeholder="Сообщение…" rows="1"
      oninput="autoResize(this)" onkeydown="onKey(event)"></textarea>
    <button class="send" type="submit" id="sendBtn">➤</button>
  </form>
</div>
{% endblock %}
{% block scripts %}
<script>
const ACCOUNT_ID = {{ account.id }};
const CHAT_ID = {{ chat_id }};
let lastId = 0;
let pollTimer = null;
let isPolling = false;
let pendingMsg = null;

const escapeHtml = (s) => (s||'').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
const fmtTime = (iso) => {
  if(!iso) return '';
  const d = new Date(iso);
  return d.toTimeString().slice(0,5);
};

function autoResize(el){
  el.style.height = 'auto';
  el.style.height = Math.min(120, el.scrollHeight) + 'px';
}

function onKey(e){
  if(e.key === 'Enter' && !e.shiftKey){
    e.preventDefault();
    sendMessage(e);
  }
}

function renderMessage(m, isPending=false){
  const wrap = document.createElement('div');
  wrap.className = 'bubble ' + (m.outgoing ? 'out'+(isPending?' pending':'') : 'in');
  wrap.dataset.id = m.id;
  let senderHtml = '';
  if(!m.outgoing && m.sender_name){
    senderHtml = '<div class="sender">'+escapeHtml(m.sender_name)+'</div>';
  }
  const time = m.date ? fmtTime(m.date) : '';
  wrap.innerHTML = `
    ${senderHtml}
    <div class="text">${escapeHtml(m.text || '')}</div>
    <div class="time">${time}${isPending?' · ⏱':''}</div>
  `;
  return wrap;
}

function appendMessage(m, isPending=false){
  const list = document.getElementById('messages');
  // удалить loading
  const ld = document.getElementById('loadingMsg');
  if(ld) ld.remove();
  const el = renderMessage(m, isPending);
  list.appendChild(el);
  scrollToBottom();
  return el;
}

function updateMessage(id, patch){
  const el = document.querySelector(`.bubble[data-id="${id}"]`);
  if(!el) return;
  for(const k in patch){
    if(k==='status'){
      el.classList.remove('pending');
      if(patch[k]==='failed') el.classList.add('failed');
    }
    if(k==='id' && patch.id){
      el.dataset.id = patch.id;
    }
  }
}

function scrollToBottom(){
  const list = document.getElementById('messages');
  list.scrollTop = list.scrollHeight;
}

async function loadInitial(){
  try {
    const r = await __get('/api/chat/'+ACCOUNT_ID+'/'+CHAT_ID+'/messages?limit=50');
    const j = await r.json();
    if(!j.ok){
      document.getElementById('messages').innerHTML =
        '<div class="empty"><div class="big">⚠</div><p>'+escapeHtml(j.error||'Ошибка')+'</p></div>';
      return;
    }
    document.getElementById('chatSub').innerHTML = '<span style="color:#1ba35b">● online</span> · '+j.messages.length+' сообщений';
    if(!j.messages.length){
      document.getElementById('messages').innerHTML =
        '<div class="empty" id="loadingMsg"><div class="big">👋</div><p>Нет сообщений. Напиши первым!</p></div>';
      return;
    }
    const list = document.getElementById('messages');
    list.innerHTML = '';
    for(const m of j.messages){
      list.appendChild(renderMessage(m));
      if(m.id > lastId) lastId = m.id;
    }
    scrollToBottom();
  } catch(e){
    document.getElementById('messages').innerHTML =
      '<div class="empty"><div class="big">⚠</div><p>'+escapeHtml(e.message)+'</p></div>';
  }
}

async function poll(){
  if(isPolling || document.hidden) return;
  isPolling = true;
  try {
    const r = await __get('/api/chat/'+ACCOUNT_ID+'/'+CHAT_ID+'/messages?since='+lastId+'&limit=100');
    const j = await r.json();
    if(j.ok && j.messages && j.messages.length){
      let added = 0;
      for(const m of j.messages){
        // dedup: если уже есть такой id, пропускаем
        if(document.querySelector(`.bubble[data-id="${m.id}"]`)) continue;
        appendMessage(m);
        if(m.id > lastId) lastId = m.id;
        added++;
      }
      if(added>0) scrollToBottom();
    }
  } catch(e){
    console.warn('poll err', e);
  } finally {
    isPolling = false;
  }
}

function startPolling(){
  stopPolling();
  pollTimer = setInterval(poll, 2000);
}

function stopPolling(){
  if(pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

async function sendMessage(e){
  e.preventDefault();
  const input = document.getElementById('input');
  const btn = document.getElementById('sendBtn');
  const text = input.value.trim();
  if(!text) return false;
  btn.disabled = true; input.disabled = true;

  // Оптимистичное добавление
  const tempId = 'tmp-' + Date.now();
  const optimistic = {
    id: tempId,
    text: text,
    outgoing: true,
    sender_name: 'Вы',
    date: new Date().toISOString(),
  };
  appendMessage(optimistic, true);
  input.value = ''; autoResize(input);
  scrollToBottom();

  try {
    const r = await __post('/api/chat/'+ACCOUNT_ID+'/'+CHAT_ID+'/send', {text});
    const j = await r.json();
    if(j.ok){
      // заменить оптимистичное на реальное
      const el = document.querySelector(`.bubble[data-id="${tempId}"]`);
      if(el){
        el.dataset.id = j.message.id;
        el.classList.remove('pending');
        const t = el.querySelector('.time');
        if(t) t.textContent = fmtTime(j.message.date) + ' · ✓✓';
        if(j.message.id > lastId) lastId = j.message.id;
      }
    } else {
      updateMessage(tempId, {status: 'failed'});
      __toast('Ошибка: ' + (j.error || 'неизвестно'));
      input.value = text; // вернуть в инпут
    }
  } catch(e){
    updateMessage(tempId, {status: 'failed'});
    __toast('Сеть: ' + e.message);
    input.value = text;
  } finally {
    btn.disabled = false; input.disabled = false;
    input.focus();
  }
  return false;
}

async function markRead(){
  try {
    await __post('/api/chat/'+ACCOUNT_ID+'/'+CHAT_ID+'/read', {});
  } catch(e){}
}

function reload(){
  lastId = 0;
  document.getElementById('messages').innerHTML = '<div class="empty"><div class="big">⏳</div><p>Загружаю…</p></div>';
  loadInitial();
}

// Старт
loadInitial().then(() => startPolling());

// Пауза polling когда вкладка скрыта
document.addEventListener('visibilitychange', () => {
  if(document.hidden) stopPolling();
  else { loadInitial(); startPolling(); }
});

// Фокус на инпут при загрузке (на мобильных это спорно, поэтому через секунду)
setTimeout(() => {
  // не автофокус, чтобы не вылетала клавиатура
}, 1000);
</script>
{% endblock %}
"""


# =========================================================================
#  JINJA ENV
# =========================================================================

_TEMPLATES = {
    "base": BASE_HTML,
    "accounts": ACCOUNTS_HTML,
    "chats": CHATS_HTML,
    "chat": CHAT_HTML,
}
_JINJA = jinja2.Environment(
    loader=jinja2.DictLoader(_TEMPLATES),
    autoescape=True, trim_blocks=True, lstrip_blocks=True,
)
_JINJA.globals.update(
    css=BASE_CSS, app_name=APP_NAME,
    fmt_phone=fmt_phone, avatar_color=avatar_color,
)


def _render(name: str, **ctx) -> str:
    return _JINJA.get_template(name).render(**ctx)


# =========================================================================
#  MAIN
# =========================================================================

if __name__ == "__main__":
    init_db()
    port = int(os.getenv("PORT", "8080"))
    logger.info("Starting %s on :%s", APP_NAME, port)
    logger.info("DEBUG=%s  USE_POOL=%s  API_ID set: %s", DEBUG, USE_POOL, bool(API_ID and API_HASH))
    if not (API_ID and API_HASH):
        logger.warning("API_ID / API_HASH not set — Telethon will fail. "
                       "Set them via env: export API_ID=... API_HASH=...")
    try:
        app.run(host="0.0.0.0", port=port, debug=DEBUG, threaded=True)
    finally:
        cm.disconnect_all()
