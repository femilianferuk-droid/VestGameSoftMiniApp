"""
Vest Game Soft — Telegram Mini App
==================================
Single-file Flask Mini App для управления аккаунтами и рассылками.

Запуск:
    pip install -r requirements.txt
    export BOT_TOKEN=...
    export DATABASE_URL=...
    python app.py

Mini App читает initData из Telegram.WebApp и подписывает HMAC
по BOT_TOKEN. Все операции пишут задачи в таблицу task_queue —
тот самый бот (bot.py) их подхватывает через task_queue_worker.
"""

import os
import hmac
import hashlib
import json
import secrets
import time
import urllib.parse
import logging
from functools import wraps
from pathlib import Path

import jinja2
from flask import (
    Flask, request, jsonify,
    render_template_string, send_from_directory,
)
from psycopg.rows import dict_row

# psycopg_pool опционален — без него работаем с одним соединением на запрос
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
SECRET_KEY = os.getenv("SECRET_KEY", "vest-miniapp-" + secrets.token_hex(8))
APP_NAME = "Vest Game Soft"
SUPPORT_URL = "https://t.me/VestGameSupport"
MAIN_BOT_URL = "https://t.me/VestGamebot"
CASINO_URL = "https://t.me/VestGamebot"
DEBUG = os.getenv("DEBUG", "0") == "1"

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [miniapp] %(levelname)s %(message)s",
)
logger = logging.getLogger("vest.miniapp")

app = Flask(__name__, static_folder=None)
app.secret_key = SECRET_KEY
app.config["JSON_AS_ASCII"] = False

# --- БД ---
if USE_POOL:
    pool = ConnectionPool(
        DATABASE_URL,
        min_size=1,
        max_size=8,
        kwargs={"row_factory": dict_row, "autocommit": True},
        open=True,
    )
else:
    pool = None

    def _connect():
        import psycopg
        return psycopg.connect(
            DATABASE_URL, row_factory=dict_row, autocommit=True
        )


def db_query(sql: str, params: tuple = ()):
    """SELECT — возвращает list[dict]."""
    if USE_POOL:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
    else:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()


def db_one(sql: str, params: tuple = ()):
    rows = db_query(sql, params)
    return rows[0] if rows else None


def db_exec(sql: str, params: tuple = ()):
    if USE_POOL:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.rowcount
    else:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.rowcount


# --- Аутентификация через Telegram initData ---
def validate_init_data(init_data: str) -> dict | None:
    """Проверяет подпись initData от Telegram Mini App."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        parsed = urllib.parse.parse_qs(init_data, keep_blank_values=True)
        received_hash = parsed.pop("hash", [None])[0]
        if not received_hash:
            return None

        data_check_arr = sorted(
            f"{k}={v[0]}" for k, v in parsed.items()
        )
        data_check_string = "\n".join(data_check_arr)

        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        calculated = hmac.new(
            secret_key,
            data_check_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(calculated, received_hash):
            return None

        auth_date = int(parsed.get("auth_date", ["0"])[0] or 0)
        if auth_date and (time.time() - auth_date) > 3600:
            return None

        user_raw = parsed.get("user", [None])[0]
        if not user_raw:
            return None
        return json.loads(user_raw)
    except Exception as e:
        logger.warning("initData validation error: %s", e)
        return None


def get_user_from_request() -> dict | None:
    """Достаёт пользователя из initData."""
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
                user = {
                    "id": int(dev_id),
                    "first_name": "Dev",
                    "username": "dev",
                }
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


# --- Хелперы ---
def enqueue_task(user_id: int, task_type: str, payload: dict) -> int:
    row = db_one(
        """INSERT INTO task_queue (user_id, task_type, payload, status)
           VALUES (%s, %s, %s::jsonb, 'queued')
           RETURNING id""",
        (user_id, task_type, json.dumps(payload, ensure_ascii=False)),
    )
    return row["id"]


def get_stats(user_id: int) -> dict:
    row = db_one(
        """
        SELECT
            (SELECT COUNT(*) FROM accounts
             WHERE user_id = %s AND is_active) AS accounts,
            (SELECT COUNT(*) FROM accounts
             WHERE user_id = %s) AS accounts_total,
            (SELECT COUNT(*) FROM broadcasts
             WHERE user_id = %s AND status = 'active') AS active_broadcasts,
            (SELECT COUNT(*) FROM broadcasts
             WHERE user_id = %s) AS broadcasts_total,
            (SELECT COUNT(*) FROM dm_broadcasts
             WHERE user_id = %s) AS dm_total,
            (SELECT COUNT(*) FROM auto_responders
             WHERE user_id = %s AND is_active) AS responders_active,
            (SELECT COUNT(*) FROM auto_responders
             WHERE user_id = %s) AS responders_total,
            (SELECT COUNT(*) FROM proxies
             WHERE user_id = %s AND is_active) AS proxies,
            (SELECT COALESCE(SUM(progress), 0) FROM broadcasts
             WHERE user_id = %s) AS sent_total
        """,
        (user_id,) * 9,
    ) or {}
    return row


def fmt_dt(dt) -> str:
    if not dt:
        return "—"
    if isinstance(dt, str):
        return dt[:16]
    return dt.strftime("%d.%m.%Y %H:%M")


def fmt_phone(p: str) -> str:
    if not p:
        return "—"
    p = p.strip()
    if p.startswith("+") and len(p) >= 7:
        return f"{p[:4]}***{p[-4:]}"
    return p


# =========================================================================
#  ROUTES
# =========================================================================

@app.route("/healthz")
def healthz():
    try:
        db_one("SELECT 1 AS ok")
        return jsonify(status="ok", db="up", time=int(time.time()))
    except Exception as e:
        return jsonify(status="degraded", db="down", error=str(e)), 500


@app.route("/static/<path:filename>")
def static_files(filename: str):
    return send_from_directory(STATIC_DIR, filename)


@app.route("/")
@require_auth
def dashboard(user):
    stats = get_stats(user["id"])
    recent_tasks = db_query(
        """SELECT id, task_type, status, error, created_at, finished_at
           FROM task_queue
           WHERE user_id = %s
           ORDER BY id DESC LIMIT 8""",
        (user["id"],),
    )
    return _render(
        "dashboard",
        user=user,
        stats=stats,
        recent_tasks=recent_tasks,
    )


@app.route("/accounts")
@require_auth
def accounts_page(user):
    accounts = db_query(
        """SELECT a.id, a.phone, a.is_active, a.warming_enabled,
                  a.created_at, p.label AS proxy_label,
                  p.host AS proxy_host, p.proxy_type
           FROM accounts a
           LEFT JOIN proxies p ON p.id = a.proxy_id
           WHERE a.user_id = %s
           ORDER BY a.id DESC""",
        (user["id"],),
    )
    return _render("accounts", user=user, accounts=accounts)


@app.route("/proxies", methods=["GET", "POST"])
@require_auth
def proxies_page(user):
    if request.method == "POST":
        proxy_type = (request.form.get("proxy_type") or "socks5").lower()
        host = (request.form.get("host") or "").strip()
        try:
            port = int(request.form.get("port") or "0")
        except ValueError:
            port = 0
        username = (request.form.get("username") or "").strip() or None
        password = (request.form.get("password") or "").strip() or None
        label = (request.form.get("label") or "").strip() or None
        if not host or not (1 <= port <= 65535):
            return render_template_string(
                ALERT_HTML, message="Некорректный host или port",
                back_url="/proxies",
            ), 400
        if proxy_type not in ("socks5", "socks4", "http"):
            proxy_type = "socks5"
        db_exec(
            """INSERT INTO proxies
               (user_id, proxy_type, host, port, username, password, label)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (user["id"], proxy_type, host, port, username, password, label),
        )
        return _redirect("/proxies")
    proxies = db_query(
        "SELECT id, label, host, port, proxy_type FROM proxies "
        "WHERE user_id = %s ORDER BY id DESC",
        (user["id"],),
    )
    return _render("proxies", user=user, proxies=proxies)


@app.route("/proxies/delete/<int:proxy_id>", methods=["POST"])
@require_auth
def proxy_delete(user, proxy_id: int):
    db_exec(
        "DELETE FROM proxies WHERE id = %s AND user_id = %s",
        (proxy_id, user["id"]),
    )
    return _redirect("/proxies")


@app.route("/broadcasts")
@require_auth
def broadcasts_page(user):
    rows = db_query(
        """SELECT b.*, a.phone
           FROM broadcasts b
           LEFT JOIN accounts a ON a.id = b.account_id
           WHERE b.user_id = %s
           ORDER BY b.id DESC LIMIT 50""",
        (user["id"],),
    )
    accounts = db_query(
        "SELECT id, phone FROM accounts WHERE user_id = %s AND is_active "
        "ORDER BY id DESC",
        (user["id"],),
    )
    return _render("broadcasts", user=user, broadcasts=rows, accounts=accounts)


@app.route("/dm")
@require_auth
def dm_page(user):
    rows = db_query(
        """SELECT d.*, a.phone
           FROM dm_broadcasts d
           LEFT JOIN accounts a ON a.id = d.account_id
           WHERE d.user_id = %s
           ORDER BY d.id DESC LIMIT 50""",
        (user["id"],),
    )
    accounts = db_query(
        "SELECT id, phone FROM accounts WHERE user_id = %s AND is_active "
        "ORDER BY id DESC",
        (user["id"],),
    )
    return _render("dm", user=user, dm=rows, accounts=accounts)


@app.route("/responders")
@require_auth
def responders_page(user):
    rows = db_query(
        """SELECT r.*, a.phone
           FROM auto_responders r
           LEFT JOIN accounts a ON a.id = r.account_id
           WHERE r.user_id = %s
           ORDER BY r.id DESC""",
        (user["id"],),
    )
    return _render("responders", user=user, responders=rows)


@app.route("/parsing")
@require_auth
def parsing_page(user):
    accounts = db_query(
        "SELECT id, phone FROM accounts WHERE user_id = %s AND is_active "
        "ORDER BY id DESC",
        (user["id"],),
    )
    last_results = db_query(
        "SELECT * FROM parsed_contacts WHERE user_id = %s "
        "ORDER BY id DESC LIMIT 30",
        (user["id"],),
    )
    return _render("parsing", user=user, accounts=accounts, last_results=last_results)


@app.route("/tasks")
@require_auth
def tasks_page(user):
    tasks = db_query(
        """SELECT id, task_type, status, entity_id, error,
                  result, created_at, started_at, finished_at
           FROM task_queue
           WHERE user_id = %s
           ORDER BY id DESC LIMIT 50""",
        (user["id"],),
    )
    return _render("tasks", user=user, tasks=tasks)


# --- API: создание задач ---

@app.route("/api/broadcast", methods=["POST"])
@require_auth
def api_broadcast(user):
    data = request.json or {}
    try:
        account_id = int(data["account_id"])
    except (KeyError, ValueError, TypeError):
        return jsonify(ok=False, error="account_id required"), 400
    chat_ids = data.get("chat_ids") or []
    message_text = (data.get("message_text") or "").strip()
    if not chat_ids or not message_text:
        return jsonify(ok=False, error="chat_ids и message_text обязательны"), 400
    payload = {
        "account_id": account_id,
        "chat_ids": [str(c).strip() for c in chat_ids if str(c).strip()],
        "delay": int(data.get("delay", 30)),
        "message_count": int(data.get("message_count", 1)),
        "message_text": message_text,
        "message_media": data.get("message_media") or [],
        "mode": data.get("mode", "simultaneous"),
    }
    scheduled_at = data.get("scheduled_at")
    task_type = "schedule_broadcast" if scheduled_at else "broadcast"
    if scheduled_at:
        payload["scheduled_at"] = scheduled_at
    task_id = enqueue_task(user["id"], task_type, payload)
    return jsonify(ok=True, task_id=task_id, task_type=task_type)


@app.route("/api/dm", methods=["POST"])
@require_auth
def api_dm(user):
    data = request.json or {}
    try:
        account_id = int(data["account_id"])
    except (KeyError, ValueError, TypeError):
        return jsonify(ok=False, error="account_id required"), 400
    usernames = data.get("usernames") or []
    message_text = (data.get("message_text") or "").strip()
    if not usernames or not message_text:
        return jsonify(ok=False, error="usernames и message_text обязательны"), 400
    payload = {
        "account_id": account_id,
        "usernames": [str(u).strip().lstrip("@") for u in usernames if str(u).strip()],
        "delay": int(data.get("delay", 60)),
        "message_text": message_text,
        "message_media": data.get("message_media") or [],
    }
    task_id = enqueue_task(user["id"], "dm_broadcast", payload)
    return jsonify(ok=True, task_id=task_id, task_type="dm_broadcast")


@app.route("/api/join", methods=["POST"])
@require_auth
def api_join(user):
    data = request.json or {}
    try:
        account_id = int(data["account_id"])
    except (KeyError, ValueError, TypeError):
        return jsonify(ok=False, error="account_id required"), 400
    links = data.get("links") or []
    if not links:
        return jsonify(ok=False, error="links обязательны"), 400
    payload = {
        "account_id": account_id,
        "links": [str(l).strip() for l in links if str(l).strip()],
        "delay": int(data.get("delay", 30)),
    }
    task_id = enqueue_task(user["id"], "join", payload)
    return jsonify(ok=True, task_id=task_id, task_type="join")


@app.route("/api/autolike", methods=["POST"])
@require_auth
def api_autolike(user):
    data = request.json or {}
    try:
        account_id = int(data["account_id"])
    except (KeyError, ValueError, TypeError):
        return jsonify(ok=False, error="account_id required"), 400
    chat_ids = data.get("chat_ids") or []
    if not chat_ids:
        return jsonify(ok=False, error="chat_ids обязательны"), 400
    payload = {
        "account_id": account_id,
        "chat_ids": [str(c).strip() for c in chat_ids if str(c).strip()],
        "reaction": data.get("reaction", "👍"),
        "delay": int(data.get("delay", 60)),
    }
    task_id = enqueue_task(user["id"], "autolike", payload)
    return jsonify(ok=True, task_id=task_id, task_type="autolike")


@app.route("/api/delete_messages", methods=["POST"])
@require_auth
def api_delete_messages(user):
    data = request.json or {}
    try:
        account_id = int(data["account_id"])
    except (KeyError, ValueError, TypeError):
        return jsonify(ok=False, error="account_id required"), 400
    chat_ids = data.get("chat_ids") or []
    if not chat_ids:
        return jsonify(ok=False, error="chat_ids обязательны"), 400
    payload = {
        "account_id": account_id,
        "chat_ids": [str(c).strip() for c in chat_ids if str(c).strip()],
        "hours": int(data.get("hours", 24)),
    }
    task_id = enqueue_task(user["id"], "delete_messages", payload)
    return jsonify(ok=True, task_id=task_id, task_type="delete_messages")


@app.route("/api/parsing", methods=["POST"])
@require_auth
def api_parsing(user):
    data = request.json or {}
    try:
        account_id = int(data["account_id"])
    except (KeyError, ValueError, TypeError):
        return jsonify(ok=False, error="account_id required"), 400
    chat = (data.get("chat") or "").strip()
    mode = data.get("mode", "all")
    if not chat:
        return jsonify(ok=False, error="chat обязателен"), 400
    payload = {"account_id": account_id, "chat": chat, "mode": mode}
    task_id = enqueue_task(user["id"], "parsing", payload)
    return jsonify(
        ok=True,
        task_id=task_id,
        task_type="parsing",
        note="Задача поставлена в очередь. Если бот её не подхватил — "
             "используй парсинг через @VestGamebot (раздел Функции → Парсинг).",
    )


# --- API: статусы и стоп ---

@app.route("/api/task/<int:task_id>")
@require_auth
def api_task_status(user, task_id: int):
    row = db_one(
        """SELECT id, task_type, status, entity_id, error,
                  result, created_at, started_at, finished_at
           FROM task_queue
           WHERE id = %s AND user_id = %s""",
        (task_id, user["id"]),
    )
    if not row:
        return jsonify(ok=False, error="not found"), 404
    return jsonify(ok=True, task=row)


@app.route("/api/task/<int:task_id>/cancel", methods=["POST"])
@require_auth
def api_task_cancel(user, task_id: int):
    db_exec(
        """UPDATE task_queue SET status = 'cancel_requested'
           WHERE id = %s AND user_id = %s AND status IN ('queued', 'running')""",
        (task_id, user["id"]),
    )
    return jsonify(ok=True)


@app.route("/api/broadcast/<int:broadcast_id>/stop", methods=["POST"])
@require_auth
def api_broadcast_stop(user, broadcast_id: int):
    db_exec(
        """UPDATE broadcasts SET status = 'stopped', stopped_at = NOW()
           WHERE id = %s AND user_id = %s AND status = 'active'""",
        (broadcast_id, user["id"]),
    )
    return jsonify(ok=True)


@app.route("/api/dm/<int:dm_id>/stop", methods=["POST"])
@require_auth
def api_dm_stop(user, dm_id: int):
    db_exec(
        """UPDATE dm_broadcasts SET status = 'stopped', stopped_at = NOW()
           WHERE id = %s AND user_id = %s AND status = 'active'""",
        (dm_id, user["id"]),
    )
    return jsonify(ok=True)


@app.route("/api/stats")
@require_auth
def api_stats(user):
    return jsonify(stats=get_stats(user["id"]))


# =========================================================================
#  TEMPLATES
# =========================================================================

BASE_CSS = """
:root{
  --tg-bg: var(--tg-theme-bg-color, #ffffff);
  --tg-secondary-bg: var(--tg-theme-secondary-bg-color, #f4f7fb);
  --tg-text: var(--tg-theme-text-color, #0f172a);
  --tg-hint: var(--tg-theme-hint-color, #6b7c93);
  --tg-link: var(--tg-theme-link-color, #2481cc);
  --tg-button: var(--tg-theme-button-color, #2481cc);
  --tg-button-text: var(--tg-theme-button-text-color, #ffffff);
  --accent: #2481cc;
  --accent-2: #5fb3f0;
  --gradient: linear-gradient(135deg, #5fb3f0 0%, #2481cc 100%);
  --card-shadow: 0 4px 24px rgba(36, 129, 204, 0.08);
  --radius: 18px;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{background:var(--tg-bg);color:var(--tg-text);
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",Roboto,sans-serif;
  font-size:15px;line-height:1.45;min-height:100vh;-webkit-font-smoothing:antialiased}
body{padding-bottom:80px;overflow-x:hidden}
a{color:var(--tg-link);text-decoration:none}
.container{max-width:680px;margin:0 auto;padding:0 16px}
.header{position:sticky;top:0;z-index:10;background:var(--tg-bg);
  border-bottom:1px solid rgba(36,129,204,.08);
  padding:12px 16px;display:flex;align-items:center;gap:12px;
  backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px)}
.header .title{font-weight:700;font-size:17px;flex:1}
.header .back{color:var(--tg-link);font-size:24px;text-decoration:none;line-height:1;width:24px;text-align:center}
.hero{position:relative;border-radius:var(--radius);overflow:hidden;
  margin:16px 0;box-shadow:var(--card-shadow)}
.hero img{width:100%;display:block}
.hero .overlay{position:absolute;inset:0;
  background:linear-gradient(180deg,transparent 30%,rgba(15,23,42,.55) 100%);
  display:flex;align-items:flex-end;padding:18px;color:#fff}
.hero .overlay h1{font-size:22px;font-weight:800;margin-bottom:4px}
.hero .overlay p{opacity:.85;font-size:13px}
.section-title{font-size:13px;font-weight:700;color:var(--tg-hint);
  text-transform:uppercase;letter-spacing:.06em;margin:18px 0 10px}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.grid-4{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.card{background:var(--tg-secondary-bg);border-radius:var(--radius);
  padding:14px;box-shadow:var(--card-shadow)}
.card .num{font-size:22px;font-weight:800;color:var(--accent)}
.card .lbl{font-size:11px;color:var(--tg-hint);margin-top:2px}
.stat-tile{padding:12px}
.menu{display:flex;flex-direction:column;gap:8px}
.menu a,.menu button{display:flex;align-items:center;gap:12px;padding:14px;
  background:var(--tg-secondary-bg);border-radius:14px;
  text-decoration:none;color:var(--tg-text);font-weight:600;
  border:1px solid transparent;transition:all .15s;cursor:pointer;font-size:15px;width:100%;
  font-family:inherit}
.menu a:active,.menu button:active{transform:scale(.98)}
.menu a .icon,.menu button .icon{width:40px;height:40px;border-radius:12px;
  background:var(--gradient);color:#fff;display:flex;align-items:center;justify-content:center;
  font-size:18px;flex-shrink:0}
.menu a .arrow{margin-left:auto;color:var(--tg-hint);font-size:20px}
.form{display:flex;flex-direction:column;gap:12px}
.field{display:flex;flex-direction:column;gap:6px}
.field label{font-size:12px;font-weight:600;color:var(--tg-hint);text-transform:uppercase;letter-spacing:.05em}
.field input,.field textarea,.field select{padding:12px 14px;border-radius:12px;
  border:1.5px solid rgba(36,129,204,.18);background:var(--tg-bg);
  color:var(--tg-text);font-size:15px;font-family:inherit;outline:none;
  transition:border-color .15s;width:100%}
.field input:focus,.field textarea:focus,.field select:focus{border-color:var(--accent)}
.field textarea{min-height:120px;resize:vertical}
.field .hint{font-size:11px;color:var(--tg-hint);margin-top:2px}
.btn{padding:14px 18px;border-radius:14px;border:none;
  background:var(--gradient);color:#fff;font-weight:700;font-size:15px;cursor:pointer;
  font-family:inherit;box-shadow:0 4px 16px rgba(36,129,204,.3);transition:transform .1s;width:100%}
.btn:active{transform:scale(.98)}
.btn.secondary{background:var(--tg-secondary-bg);color:var(--tg-text);
  box-shadow:none;border:1px solid rgba(36,129,204,.15)}
.btn.danger{background:linear-gradient(135deg,#ff6b6b 0%,#ee5a5a 100%);
  box-shadow:0 4px 16px rgba(238,90,90,.3);width:auto;padding:8px 14px;font-size:13px}
.badge{display:inline-block;padding:3px 10px;border-radius:999px;
  font-size:11px;font-weight:700;letter-spacing:.03em;text-transform:uppercase}
.badge.active{background:rgba(36,204,109,.15);color:#1ba35b}
.badge.stopped{background:rgba(255,107,107,.15);color:#d63838}
.badge.completed{background:rgba(36,129,204,.15);color:#2481cc}
.badge.scheduled{background:rgba(255,184,28,.18);color:#c98300}
.badge.queued{background:rgba(155,162,170,.18);color:#6b7c93}
.badge.running{background:rgba(95,179,240,.18);color:#2481cc}
.badge.cancelled{background:rgba(155,162,170,.18);color:#6b7c93}
.row{display:flex;align-items:center;gap:10px;padding:12px;
  background:var(--tg-secondary-bg);border-radius:14px;margin-bottom:8px}
.row .grow{flex:1;min-width:0}
.row .title{font-weight:600;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row .sub{font-size:12px;color:var(--tg-hint);margin-top:2px}
.row .meta{font-size:11px;color:var(--tg-hint);margin-top:4px}
.empty{padding:40px 20px;text-align:center;color:var(--tg-hint);background:var(--tg-secondary-bg);border-radius:var(--radius)}
.empty .big{font-size:42px;margin-bottom:10px;opacity:.5}
.bar{height:6px;background:rgba(36,129,204,.12);border-radius:3px;overflow:hidden;margin-top:6px}
.bar .fill{height:100%;background:var(--gradient);border-radius:3px;transition:width .4s}
.footer-link{display:block;text-align:center;padding:14px;margin:18px 0;
  background:linear-gradient(135deg,#ff8a3d 0%,#ff5b3d 100%);
  color:#fff;border-radius:14px;font-weight:700;text-decoration:none}
.muted{color:var(--tg-hint);font-weight:400;font-size:12px;margin-top:2px}
.divider{height:1px;background:rgba(36,129,204,.1);margin:14px 0}
.toast{position:fixed;bottom:80px;left:50%;transform:translateX(-50%);
  background:var(--tg-text);color:var(--tg-bg);padding:12px 20px;
  border-radius:12px;font-size:14px;font-weight:600;z-index:100;box-shadow:0 8px 24px rgba(0,0,0,.2);
  animation:slideUp .3s;max-width:90%;text-align:center}
@keyframes slideUp{from{transform:translate(-50%,20px);opacity:0}to{transform:translate(-50%,0);opacity:1}}
.row.row-center .grow .sub{font-weight:500}
.list-form{margin-bottom:14px}
.action-row{display:flex;gap:8px;margin-top:8px}
.action-row .btn{flex:1}
.inline-form{display:inline-block;margin:0}
"""


BASE_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{% block title %}{{ app_name }}{% endblock %}</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>{{ css|safe }}</style>
</head>
<body>
<div class="header">
  <a href="/" class="back" onclick="if(window.history.length>1){history.back();return false}">‹</a>
  <div class="title">{% block header %}{{ app_name }}{% endblock %}</div>
</div>
<div class="container" style="padding-top:14px">
{% block content %}{% endblock %}
</div>
<script>
(function(){
  if(!window.Telegram||!Telegram.WebApp) return;
  const wa = Telegram.WebApp;
  wa.ready();
  wa.expand();
  const btn = wa.BackButton;
  btn.show();
  btn.onClick(()=>history.length>1?history.back():wa.close());

  if(wa.themeParams){
    const r=document.documentElement.style;
    if(wa.themeParams.bg_color) r.setProperty('--tg-bg', wa.themeParams.bg_color);
    if(wa.themeParams.secondary_bg_color) r.setProperty('--tg-secondary-bg', wa.themeParams.secondary_bg_color);
    if(wa.themeParams.text_color) r.setProperty('--tg-text', wa.themeParams.text_color);
    if(wa.themeParams.hint_color) r.setProperty('--tg-hint', wa.themeParams.hint_color);
    if(wa.themeParams.button_color) r.setProperty('--accent', wa.themeParams.button_color);
  }

  window.__postJson = (url, body) => fetch(url, {
    method:'POST',
    headers:{
      'Content-Type':'application/json',
      'X-Telegram-Init-Data': wa.initData || ''
    },
    body: JSON.stringify(body || {})
  });
  window.__toast = (msg) => {
    const t = document.createElement('div');
    t.className='toast';
    t.textContent=msg;
    document.body.appendChild(t);
    setTimeout(()=>t.remove(), 2200);
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
background:#f4f7fb;margin:0;padding:20px}
.box{max-width:380px;background:#fff;padding:32px;border-radius:18px;
box-shadow:0 8px 32px rgba(0,0,0,.08);text-align:center}
h1{font-size:20px;margin-bottom:12px;color:#0f172a}
p{color:#6b7c93;line-height:1.5;font-size:14px}</style></head>
<body><div class="box">
<h1>🔒 {{ app_name }}</h1>
<p>Это приложение доступно только из Telegram.<br>Открой его через
<strong>@VestGamebot</strong> → кнопка с приложением.</p>
</div></body></html>"""


ALERT_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Ошибка</title>
<style>body{font-family:-apple-system,sans-serif;display:flex;align-items:center;justify-content:center;
min-height:100vh;background:#f4f7fb;margin:0;padding:20px}
.box{max-width:380px;background:#fff;padding:32px;border-radius:18px;
box-shadow:0 8px 32px rgba(0,0,0,.08);text-align:center}
.btn{display:inline-block;margin-top:16px;padding:10px 20px;background:#2481cc;
color:#fff;border-radius:10px;text-decoration:none;font-weight:600}</style></head>
<body><div class="box">
<p style="color:#d63838;font-weight:600;font-size:16px">⚠ {{ message }}</p>
<a class="btn" href="{{ back_url }}">← Назад</a>
</div></body></html>"""


DASHBOARD_HTML = """{% extends "base" %}
{% block title %}Главная — {{ app_name }}{% endblock %}
{% block content %}
<div class="hero">
  <img src="/static/img/vest-hero.webp" alt="Vest Game Soft">
  <div class="overlay">
    <div>
      <h1>Vest Game Soft</h1>
      <p>Управление аккаунтами и рассылками</p>
    </div>
  </div>
</div>

<div class="section-title">Сводка</div>
<div class="grid-2">
  <div class="card stat-tile">
    <div class="num">{{ stats.accounts or 0 }}</div>
    <div class="lbl">Активных аккаунтов</div>
  </div>
  <div class="card stat-tile">
    <div class="num">{{ stats.active_broadcasts or 0 }}</div>
    <div class="lbl">Активных рассылок</div>
  </div>
  <div class="card stat-tile">
    <div class="num">{{ stats.responders_active or 0 }}</div>
    <div class="lbl">Автоответчиков</div>
  </div>
  <div class="card stat-tile">
    <div class="num">{{ stats.sent_total or 0 }}</div>
    <div class="lbl">Сообщений отправлено</div>
  </div>
</div>

<div class="section-title">Функции</div>
<div class="menu">
  <a href="/accounts">
    <div class="icon">👥</div>
    <div>Аккаунты<div class="muted">{{ stats.accounts_total or 0 }} всего</div></div>
    <div class="arrow">›</div>
  </a>
  <a href="/proxies">
    <div class="icon">🔗</div>
    <div>Прокси<div class="muted">{{ stats.proxies or 0 }} настроено</div></div>
    <div class="arrow">›</div>
  </a>
  <a href="/broadcasts">
    <div class="icon">📣</div>
    <div>Рассылки в чаты<div class="muted">{{ stats.broadcasts_total or 0 }} всего, {{ stats.active_broadcasts or 0 }} активных</div></div>
    <div class="arrow">›</div>
  </a>
  <a href="/dm">
    <div class="icon">✉️</div>
    <div>Рассылки в ЛС<div class="muted">{{ stats.dm_total or 0 }} запущено</div></div>
    <div class="arrow">›</div>
  </a>
  <a href="/responders">
    <div class="icon">🔔</div>
    <div>Автоответчики<div class="muted">{{ stats.responders_total or 0 }} настроено</div></div>
    <div class="arrow">›</div>
  </a>
  <a href="/parsing">
    <div class="icon">🔍</div>
    <div>Парсинг чатов</div>
    <div class="arrow">›</div>
  </a>
  <a href="/tasks">
    <div class="icon">📊</div>
    <div>История задач</div>
    <div class="arrow">›</div>
  </a>
</div>

<div class="section-title">Последние задачи</div>
{% if recent_tasks %}
  {% for t in recent_tasks %}
  <div class="row">
    <div class="grow">
      <div class="title">{{ t.task_type }}</div>
      <div class="sub">#{{ t.id }} · {{ fmt_dt(t.created_at) }}</div>
      {% if t.error %}<div class="meta" style="color:#d63838">{{ t.error[:80] }}</div>{% endif %}
    </div>
    <span class="badge {{ t.status }}">{{ t.status }}</span>
  </div>
  {% endfor %}
{% else %}
  <div class="empty">
    <div class="big">📭</div>
    Пока нет задач
  </div>
{% endif %}

<a class="footer-link" href="{{ casino_url }}">🔥 КАЗИНО В ТЕЛЕГРАМ</a>

<div style="text-align:center;padding:20px 0;color:var(--tg-hint);font-size:12px">
  Поддержка: <a href="{{ support_url }}">{{ support_url.replace('https://','') }}</a>
</div>
{% endblock %}
"""


ACCOUNTS_HTML = """{% extends "base" %}
{% block title %}Аккаунты — {{ app_name }}{% endblock %}
{% block content %}
<div class="menu" style="margin-bottom:14px">
  <a href="https://t.me/VestGamebot?start=addacc" target="_blank">
    <div class="icon">＋</div>
    <div>Добавить аккаунт<div class="muted">Через @VestGamebot</div></div>
    <div class="arrow">↗</div>
  </a>
</div>

<div class="section-title">Мои аккаунты ({{ accounts|length }})</div>
{% if accounts %}
  {% for a in accounts %}
  <div class="row">
    <div class="grow">
      <div class="title">{{ fmt_phone(a.phone) }}</div>
      <div class="sub">
        {% if a.is_active %}Активен{% else %}Отключён{% endif %}
        {% if a.warming_enabled %} · 🟢 прогрев{% endif %}
        {% if a.proxy_label %} · 🔗 {{ a.proxy_label }}{% elif a.proxy_host %} · 🔗 {{ a.proxy_host }}{% endif %}
      </div>
      <div class="meta">Добавлен {{ fmt_dt(a.created_at) }}</div>
    </div>
    <span class="badge {{ 'active' if a.is_active else 'stopped' }}">
      {{ 'online' if a.is_active else 'off' }}
    </span>
  </div>
  {% endfor %}
{% else %}
  <div class="empty">
    <div class="big">👥</div>
    У тебя пока нет аккаунтов.<br>Нажми «Добавить аккаунт» выше.
  </div>
{% endif %}
{% endblock %}
"""


PROXIES_HTML = """{% extends "base" %}
{% block title %}Прокси — {{ app_name }}{% endblock %}
{% block content %}
<div class="card" style="margin-bottom:14px">
  <div class="section-title" style="margin-top:0">Добавить прокси</div>
  <form class="form" method="POST" action="/proxies">
    <div class="field">
      <label>Тип</label>
      <select name="proxy_type">
        <option value="socks5">SOCKS5</option>
        <option value="socks4">SOCKS4</option>
        <option value="http">HTTP</option>
      </select>
    </div>
    <div class="field">
      <label>Хост</label>
      <input type="text" name="host" placeholder="1.2.3.4" required>
    </div>
    <div class="field">
      <label>Порт</label>
      <input type="number" name="port" placeholder="1080" required>
    </div>
    <div class="field">
      <label>Логин (опц.)</label>
      <input type="text" name="username">
    </div>
    <div class="field">
      <label>Пароль (опц.)</label>
      <input type="password" name="password">
    </div>
    <div class="field">
      <label>Подпись (опц.)</label>
      <input type="text" name="label" placeholder="Например, DE-1">
    </div>
    <button class="btn" type="submit">Сохранить</button>
  </form>
</div>

<div class="section-title">Мои прокси ({{ proxies|length }})</div>
{% if proxies %}
  {% for p in proxies %}
  <div class="row">
    <div class="grow">
      <div class="title">{{ p.label or p.host }}</div>
      <div class="sub">{{ p.proxy_type }}://{{ p.host }}:{{ p.port }}</div>
      {% if p.username %}<div class="meta">👤 {{ p.username }}</div>{% endif %}
    </div>
    <form class="inline-form" method="POST" action="/proxies/delete/{{ p.id }}"
          onsubmit="return confirm('Удалить прокси?')">
      <button class="btn danger" type="submit">×</button>
    </form>
  </div>
  {% endfor %}
{% else %}
  <div class="empty"><div class="big">🔗</div>Прокси пока нет</div>
{% endif %}
{% endblock %}
"""


BROADCASTS_HTML = """{% extends "base" %}
{% block title %}Рассылки — {{ app_name }}{% endblock %}
{% block content %}
<div class="card" style="margin-bottom:14px">
  <div class="section-title" style="margin-top:0">Новая рассылка</div>
  {% if not accounts %}
  <p style="color:var(--tg-hint);font-size:13px">
    Сначала добавь аккаунт в @VestGamebot
  </p>
  {% else %}
  <form id="bcForm" class="form">
    <div class="field">
      <label>Аккаунт</label>
      <select name="account_id" required>
        <option value="">— выбери аккаунт —</option>
        {% for a in accounts %}
        <option value="{{ a.id }}">{{ a.phone }}</option>
        {% endfor %}
      </select>
    </div>
    <div class="field">
      <label>Чаты (юзернеймы или ID, через запятую)</label>
      <textarea name="chat_ids" placeholder="@chat1, @chat2, -1001234567890" required></textarea>
      <div class="hint">Можно указывать публичные @username или числовые ID</div>
    </div>
    <div class="field">
      <label>Текст сообщения</label>
      <textarea name="message_text" placeholder="Привет! 👋" required></textarea>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
      <div class="field">
        <label>Задержка (сек)</label>
        <input type="number" name="delay" value="30" min="0">
      </div>
      <div class="field">
        <label>Повторов</label>
        <input type="number" name="message_count" value="1" min="1" max="50">
      </div>
    </div>
    <div class="field">
      <label>Режим</label>
      <select name="mode">
        <option value="simultaneous">Одновременный</option>
        <option value="random">Случайный (по одному)</option>
      </select>
    </div>
    <div class="field">
      <label>Запланировать (опц.)</label>
      <input type="datetime-local" name="scheduled_at">
    </div>
    <button class="btn" type="submit">🚀 Запустить</button>
  </form>
  {% endif %}
</div>

<div class="section-title">История ({{ broadcasts|length }})</div>
{% if broadcasts %}
  {% for b in broadcasts %}
  <div class="row">
    <div class="grow">
      <div class="title">
        #{{ b.id }} · {{ b.chat_ids|length }} чатов
        <span style="color:var(--tg-hint);font-weight:400">×{{ b.message_count or 1 }}</span>
      </div>
      <div class="sub">{{ b.phone or '—' }} · {{ b.mode }}</div>
      <div class="meta">
        {{ b.progress or 0 }}/{{ b.total_count or 0 }} · {{ fmt_dt(b.created_at) }}
      </div>
      {% if b.total_count and b.total_count > 0 %}
      <div class="bar"><div class="fill" style="width:{{ ((b.progress or 0) * 100 / b.total_count)|round(0) }}%"></div></div>
      {% endif %}
    </div>
    <span class="badge {{ b.status }}">{{ b.status }}</span>
  </div>
  {% endfor %}
{% else %}
  <div class="empty"><div class="big">📣</div>Пока нет рассылок</div>
{% endif %}
{% endblock %}

{% block scripts %}
<script>
document.getElementById('bcForm') && document.getElementById('bcForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = {
    account_id: fd.get('account_id'),
    chat_ids: (fd.get('chat_ids') || '').split(',').map(s => s.trim()).filter(Boolean),
    message_text: fd.get('message_text'),
    delay: parseInt(fd.get('delay') || '30'),
    message_count: parseInt(fd.get('message_count') || '1'),
    mode: fd.get('mode'),
  };
  const sched = fd.get('scheduled_at');
  if (sched) body.scheduled_at = new Date(sched).toISOString();
  const r = await __postJson('/api/broadcast', body);
  const j = await r.json();
  if (j.ok) { __toast('Задача #' + j.task_id + ' создана ✓'); e.target.reset(); }
  else __toast('Ошибка: ' + j.error);
});
</script>
{% endblock %}
"""


DM_HTML = """{% extends "base" %}
{% block title %}Рассылки в ЛС — {{ app_name }}{% endblock %}
{% block content %}
<div class="card" style="margin-bottom:14px">
  <div class="section-title" style="margin-top:0">Новая DM рассылка</div>
  {% if not accounts %}
  <p style="color:var(--tg-hint);font-size:13px">Сначала добавь аккаунт в @VestGamebot</p>
  {% else %}
  <form id="dmForm" class="form">
    <div class="field">
      <label>Аккаунт</label>
      <select name="account_id" required>
        <option value="">— выбери —</option>
        {% for a in accounts %}<option value="{{ a.id }}">{{ a.phone }}</option>{% endfor %}
      </select>
    </div>
    <div class="field">
      <label>Получатели (юзернеймы через запятую)</label>
      <textarea name="usernames" placeholder="user1, user2, user3" required></textarea>
    </div>
    <div class="field">
      <label>Сообщение</label>
      <textarea name="message_text" placeholder="Привет!" required></textarea>
    </div>
    <div class="field">
      <label>Задержка (сек)</label>
      <input type="number" name="delay" value="60" min="5">
    </div>
    <button class="btn" type="submit">✉️ Запустить</button>
  </form>
  {% endif %}
</div>

<div class="section-title">История ({{ dm|length }})</div>
{% if dm %}
  {% for d in dm %}
  <div class="row">
    <div class="grow">
      <div class="title">#{{ d.id }} · {{ d.usernames|length }} получателей</div>
      <div class="sub">{{ d.phone or '—' }}</div>
      <div class="meta">
        {{ d.progress or 0 }}/{{ d.total_count or 0 }} · {{ fmt_dt(d.created_at) }}
      </div>
    </div>
    <span class="badge {{ d.status }}">{{ d.status }}</span>
  </div>
  {% endfor %}
{% else %}
  <div class="empty"><div class="big">✉️</div>Нет DM рассылок</div>
{% endif %}
{% endblock %}

{% block scripts %}
<script>
document.getElementById('dmForm') && document.getElementById('dmForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = {
    account_id: fd.get('account_id'),
    usernames: (fd.get('usernames') || '').split(',').map(s => s.trim().replace(/^@/, '')).filter(Boolean),
    message_text: fd.get('message_text'),
    delay: parseInt(fd.get('delay') || '60'),
  };
  const r = await __postJson('/api/dm', body);
  const j = await r.json();
  if (j.ok) { __toast('Задача #' + j.task_id + ' создана ✓'); e.target.reset(); }
  else __toast('Ошибка: ' + j.error);
});
</script>
{% endblock %}
"""


RESPONDERS_HTML = """{% extends "base" %}
{% block title %}Автоответчики — {{ app_name }}{% endblock %}
{% block content %}
<div class="card" style="margin-bottom:14px">
  <div class="section-title" style="margin-top:0">Как это работает</div>
  <p style="font-size:13px;color:var(--tg-hint);line-height:1.5">
    Бот автоматически отвечает в личке, когда сообщение содержит
    <strong>триггер</strong>. Создавать автоответчики удобнее через
    <a href="https://t.me/VestGamebot" target="_blank">@VestGamebot</a> —
    там есть прикрепление медиа и подробные настройки.
  </p>
</div>

<div class="section-title">Мои автоответчики ({{ responders|length }})</div>
{% if responders %}
  {% for r in responders %}
  <div class="row">
    <div class="grow">
      <div class="title">«{{ r.trigger }}»</div>
      <div class="sub">
        {{ r.phone or '—' }} · {{ (r.response_text[:50] if r.response_text else 'медиа') }}{% if r.response_text and r.response_text|length > 50 %}…{% endif %}
      </div>
      <div class="meta">Создан {{ fmt_dt(r.created_at) }}</div>
    </div>
    <span class="badge {{ 'active' if r.is_active else 'stopped' }}">
      {{ 'on' if r.is_active else 'off' }}
    </span>
  </div>
  {% endfor %}
{% else %}
  <div class="empty">
    <div class="big">🔔</div>
    Нет автоответчиков
    <div style="margin-top:14px">
      <a class="btn" href="https://t.me/VestGamebot" target="_blank" style="text-decoration:none;display:inline-block;width:auto;padding:10px 20px">
        Создать в боте →
      </a>
    </div>
  </div>
{% endif %}
{% endblock %}
"""


PARSING_HTML = """{% extends "base" %}
{% block title %}Парсинг — {{ app_name }}{% endblock %}
{% block content %}
<div class="card" style="margin-bottom:14px">
  <div class="section-title" style="margin-top:0">Запустить парсинг</div>
  {% if not accounts %}
  <p style="color:var(--tg-hint);font-size:13px">Сначала добавь аккаунт в @VestGamebot</p>
  {% else %}
  <form id="pForm" class="form">
    <div class="field">
      <label>Аккаунт</label>
      <select name="account_id" required>
        <option value="">— выбери —</option>
        {% for a in accounts %}<option value="{{ a.id }}">{{ a.phone }}</option>{% endfor %}
      </select>
    </div>
    <div class="field">
      <label>Чат (юзернейм или ссылка)</label>
      <input type="text" name="chat" placeholder="@chatname или https://t.me/chatname" required>
    </div>
    <div class="field">
      <label>Что собирать</label>
      <select name="mode">
        <option value="all">Все данные</option>
        <option value="usernames">Только юзернеймы</option>
        <option value="names">Только имена</option>
        <option value="names_usernames">Имена + юзернеймы</option>
      </select>
    </div>
    <button class="btn" type="submit">🔍 Запустить</button>
  </form>
  {% endif %}
</div>

<div class="section-title">Последние результаты</div>
{% if last_results %}
  {% for r in last_results %}
  <div class="row">
    <div class="grow">
      <div class="title">{{ r.username or r.first_name or ('user_' + (r.user_id_telegram|string)) }}</div>
      <div class="sub">{{ r.chat }} · {{ r.parse_mode }}</div>
    </div>
  </div>
  {% endfor %}
{% else %}
  <div class="empty">
    <div class="big">🔍</div>
    Пока ничего не спарсили
    <div style="margin-top:8px;font-size:12px">
      Результаты появятся после парсинга через @VestGamebot
    </div>
  </div>
{% endif %}
{% endblock %}

{% block scripts %}
<script>
document.getElementById('pForm') && document.getElementById('pForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = {
    account_id: fd.get('account_id'),
    chat: fd.get('chat'),
    mode: fd.get('mode'),
  };
  const r = await __postJson('/api/parsing', body);
  const j = await r.json();
  if (j.ok) {
    __toast('Задача #' + j.task_id + ' в очереди ✓');
    if (j.note) setTimeout(() => __toast(j.note), 2500);
  } else __toast('Ошибка: ' + j.error);
});
</script>
{% endblock %}
"""


TASKS_HTML = """{% extends "base" %}
{% block title %}Задачи — {{ app_name }}{% endblock %}
{% block content %}
<div class="section-title">Очередь задач (последние 50)</div>
{% if tasks %}
  {% for t in tasks %}
  <div class="row">
    <div class="grow">
      <div class="title">
        #{{ t.id }} · {{ t.task_type }}
        {% if t.entity_id %}<span style="color:var(--tg-hint);font-weight:400">→ #{{ t.entity_id }}</span>{% endif %}
      </div>
      <div class="sub">{{ fmt_dt(t.created_at) }}{% if t.finished_at %} → {{ fmt_dt(t.finished_at) }}{% endif %}</div>
      {% if t.error %}<div class="meta" style="color:#d63838">⚠ {{ t.error[:120] }}</div>{% endif %}
    </div>
    <span class="badge {{ t.status }}">{{ t.status }}</span>
  </div>
  {% endfor %}
{% else %}
  <div class="empty"><div class="big">📊</div>Пусто</div>
{% endif %}
{% endblock %}
"""


# =========================================================================
#  JINJA ENV + RENDER
# =========================================================================

_TEMPLATES = {
    "base": BASE_HTML,
    "dashboard": DASHBOARD_HTML,
    "accounts": ACCOUNTS_HTML,
    "proxies": PROXIES_HTML,
    "broadcasts": BROADCASTS_HTML,
    "dm": DM_HTML,
    "responders": RESPONDERS_HTML,
    "parsing": PARSING_HTML,
    "tasks": TASKS_HTML,
}

_JINJA = jinja2.Environment(
    loader=jinja2.DictLoader(_TEMPLATES),
    autoescape=True,
    trim_blocks=True,
    lstrip_blocks=True,
)
_JINJA.globals.update(
    css=BASE_CSS,
    app_name=APP_NAME,
    support_url=SUPPORT_URL,
    main_bot_url=MAIN_BOT_URL,
    casino_url=CASINO_URL,
    fmt_dt=fmt_dt,
    fmt_phone=fmt_phone,
)


def _render(name: str, **ctx) -> str:
    return _JINJA.get_template(name).render(**ctx)


def _redirect(url: str, code: int = 302):
    from flask import redirect
    return redirect(url, code=code)


# =========================================================================
#  MAIN
# =========================================================================

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    logger.info("Starting Vest Game Soft Mini App on :%s", port)
    logger.info("DEBUG=%s  USE_POOL=%s", DEBUG, USE_POOL)
    app.run(host="0.0.0.0", port=port, debug=DEBUG)
