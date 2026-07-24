"""
Vest Mini App — Flask backend for Telegram Mini App.

The bot (`bot.py`) already runs a `task_queue_worker` that polls the
`task_queue` table for new tasks. This Mini App is a thin Flask layer
that:

  * Validates Telegram WebApp `initData` (HMAC-SHA256 with bot token).
  * Serves a dashboard (accounts, cached chats, recent activity).
  * Pushes `autolike` tasks into `task_queue`; the bot picks them up.
  * Lets the user cancel a running autolike by flipping its status to
    `cancel_requested` — the bot's `queue_cancelled()` reads that and
    stops the loop.

Run locally:
    pip install -r requirements.txt
    python app.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from flask import (
    Flask, jsonify, render_template, request, session
)

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SECRET_KEY = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
INIT_DATA_TTL_SECONDS = int(os.getenv("INIT_DATA_TTL_SECONDS", "3600"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("vest-mini")

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["SECRET_KEY"] = SECRET_KEY
app.config["JSON_AS_ASCII"] = False
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 7  # 7 days


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
@contextmanager
def db_cursor(commit: bool = False):
    """Yield a RealDictCursor and close cleanly. Autocommit-friendly."""
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_user(tg_user: Dict[str, Any]) -> Dict[str, Any]:
    """Upsert user row from Telegram initData payload.

    New users get joined_at = NOW(); existing users keep their original
    joined_at but get username/first_name refreshed.
    """
    user_id = int(tg_user["id"])
    username = tg_user.get("username")
    first_name = tg_user.get("first_name") or ""
    last_name = tg_user.get("last_name")
    full_name = " ".join(
        [p for p in [first_name, last_name] if p]
    ).strip() or (username or f"user_{user_id}")

    with db_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO users (user_id, username, first_name, joined_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (user_id) DO UPDATE
            SET username = EXCLUDED.username,
                first_name = EXCLUDED.first_name
            RETURNING user_id, username, first_name, joined_at
            """,
            (user_id, username, first_name),
        )
        row = cur.fetchone()
    row["display_name"] = full_name
    return row


# ---------------------------------------------------------------------------
# Telegram initData validation
# ---------------------------------------------------------------------------
def _build_secret_key(bot_token: str) -> bytes:
    """Telegram WebApp secret key derivation."""
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def validate_init_data(init_data: str) -> Optional[Dict[str, Any]]:
    """
    Validate raw `Telegram.WebApp.initData` string.
    Returns the parsed user dict on success, else None.

    Spec: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data:
        return None
    try:
        # keep_blank_values to preserve `key=` segments; values stay URL-encoded.
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    except ValueError:
        return None

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None

    # Build data-check-string: sorted by key, value is the *raw* URL-encoded form.
    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(parsed.items())
    )

    secret = _build_secret_key(BOT_TOKEN)
    computed = hmac.new(
        secret, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(computed, received_hash):
        logger.warning("initData hash mismatch")
        return None

    # Reject stale init data.
    try:
        auth_date = int(parsed.get("auth_date", "0"))
    except ValueError:
        auth_date = 0
    if auth_date and (time.time() - auth_date) > INIT_DATA_TTL_SECONDS:
        logger.warning("initData expired")
        return None

    user_raw = parsed.get("user")
    if not user_raw:
        return None
    try:
        return json.loads(user_raw)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Auth decorator
# ---------------------------------------------------------------------------
def login_required(fn):
    from functools import wraps

    @wraps(fn)
    def wrapper(*args, **kwargs):
        user_id = session.get("user_id")
        if not user_id:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        return fn(user_id, *args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html", bot_username=os.getenv("BOT_USERNAME", ""))


# ---------------------------------------------------------------------------
# Auth API
# ---------------------------------------------------------------------------
@app.post("/api/auth")
def api_auth():
    payload = request.get_json(silent=True) or {}
    init_data = payload.get("initData", "")
    user = validate_init_data(init_data)
    if not user:
        return jsonify({"ok": False, "error": "invalid initData"}), 403

    row = ensure_user(user)
    session.permanent = True
    session["user_id"] = int(row["user_id"])
    session["username"] = row.get("username")
    session["first_name"] = row.get("first_name")
    return jsonify({
        "ok": True,
        "user": {
            "id": int(row["user_id"]),
            "username": row.get("username"),
            "first_name": row.get("first_name"),
            "display_name": row.get("display_name"),
            "joined_at": row["joined_at"].isoformat() if row.get("joined_at") else None,
        },
    })


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
@login_required
def api_me(user_id: int):
    with db_cursor() as cur:
        cur.execute(
            "SELECT user_id, username, first_name, joined_at "
            "FROM users WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
    if not row:
        return jsonify({"ok": False, "error": "user not found"}), 404
    full = " ".join(
        [p for p in [row.get("first_name"), row.get("last_name")] if p]
    ).strip() or row.get("username") or f"user_{user_id}"
    return jsonify({
        "ok": True,
        "user": {
            "id": int(row["user_id"]),
            "username": row.get("username"),
            "first_name": row.get("first_name"),
            "display_name": full,
            "joined_at": row["joined_at"].isoformat() if row.get("joined_at") else None,
        },
    })


# ---------------------------------------------------------------------------
# Dashboard data
# ---------------------------------------------------------------------------
@app.get("/api/dashboard")
@login_required
def api_dashboard(user_id: int):
    """Aggregated data for the dashboard."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT id, phone, is_active, created_at "
            "FROM accounts WHERE user_id = %s "
            "ORDER BY created_at DESC",
            (user_id,),
        )
        accounts = [dict(r) for r in cur.fetchall()]

        cur.execute(
            """
            SELECT id, task_type, status, payload, result, error,
                   created_at, started_at, finished_at
            FROM task_queue
            WHERE user_id = %s
              AND created_at > NOW() - INTERVAL '7 days'
            ORDER BY id DESC
            LIMIT 50
            """,
            (user_id,),
        )
        tasks = [dict(r) for r in cur.fetchall()]

        cur.execute(
            """
            SELECT l.id, l.account_id, a.phone,
                   l.chat_name, l.chat_id, l.direction,
                   l.message_text, l.created_at
            FROM account_logs l
            LEFT JOIN accounts a ON a.id = l.account_id
            WHERE a.user_id = %s
            ORDER BY l.id DESC
            LIMIT 40
            """,
            (user_id,),
        )
        logs = [dict(r) for r in cur.fetchall()]

    # Serialize datetimes.
    for t in tasks:
        for k in ("created_at", "started_at", "finished_at"):
            if t.get(k):
                t[k] = t[k].isoformat()
        if t.get("payload") and not isinstance(t["payload"], dict):
            t["payload"] = dict(t["payload"])
    for a in accounts:
        if a.get("created_at"):
            a["created_at"] = a["created_at"].isoformat()
    for l_ in logs:
        if l_.get("created_at"):
            l_["created_at"] = l_["created_at"].isoformat()

    active = [t for t in tasks if t["status"] in ("queued", "running")]
    return jsonify({
        "ok": True,
        "accounts": accounts,
        "tasks": tasks,
        "active_tasks": active,
        "logs": logs,
    })


# ---------------------------------------------------------------------------
# Account chats
# ---------------------------------------------------------------------------
@app.get("/api/accounts/<int:account_id>/chats")
@login_required
def api_account_chats(user_id: int, account_id: int):
    with db_cursor() as cur:
        cur.execute(
            "SELECT id FROM accounts WHERE id = %s AND user_id = %s",
            (account_id, user_id),
        )
        if not cur.fetchone():
            return jsonify({"ok": False, "error": "not found"}), 404
        cur.execute(
            "SELECT chat_id, name, chat_type, updated_at "
            "FROM account_chats WHERE account_id = %s "
            "ORDER BY name ASC NULLS LAST",
            (account_id,),
        )
        chats = [dict(r) for r in cur.fetchall()]
    for c in chats:
        if c.get("updated_at"):
            c["updated_at"] = c["updated_at"].isoformat()
    return jsonify({"ok": True, "chats": chats})


# ---------------------------------------------------------------------------
# Autolike tasks
# ---------------------------------------------------------------------------
REACTION_EMOJIS = [
    {"emoji": "👍", "name": "Thumbs up"},
    {"emoji": "❤", "name": "Red heart"},
    {"emoji": "🔥", "name": "Fire"},
    {"emoji": "🥰", "name": "Smiling face with hearts"},
    {"emoji": "👏", "name": "Clapping hands"},
    {"emoji": "😁", "name": "Beaming face"},
    {"emoji": "🤯", "name": "Exploding head"},
    {"emoji": "💯", "name": "Hundred points"},
    {"emoji": "🤩", "name": "Star-struck"},
    {"emoji": "🙏", "name": "Folded hands"},
    {"emoji": "👌", "name": "OK hand"},
    {"emoji": "🕊", "name": "Dove"},
    {"emoji": "💩", "name": "Pile of poo"},
]


@app.get("/api/reactions")
@login_required
def api_reactions(user_id: int):  # noqa: ARG001
    return jsonify({"ok": True, "reactions": REACTION_EMOJIS})


@app.post("/api/autolike/start")
@login_required
def api_autolike_start(user_id: int):
    """
    Submit an autolike task. The bot's task_queue_worker will pick it up.

    Body JSON:
        account_id: int
        chat_ids:   list[str | int]
        reaction:   str (emoji)
        delay:      int (seconds between likes; min 10)
    """
    body = request.get_json(silent=True) or {}
    try:
        account_id = int(body["account_id"])
        chat_ids = body.get("chat_ids") or []
        reaction = body.get("reaction", "👍")
        delay = int(body.get("delay", 60))
    except (KeyError, TypeError, ValueError):
        return jsonify({"ok": False, "error": "bad payload"}), 400

    if not chat_ids:
        return jsonify({"ok": False, "error": "chat_ids required"}), 400
    delay = max(10, min(delay, 3600))

    with db_cursor(commit=True) as cur:
        cur.execute(
            "SELECT id FROM accounts WHERE id = %s AND user_id = %s",
            (account_id, user_id),
        )
        if not cur.fetchone():
            return jsonify({"ok": False, "error": "account not found"}), 404

        payload = {
            "account_id": account_id,
            "chat_ids": [str(c) for c in chat_ids],
            "reaction": reaction,
            "delay": delay,
        }
        cur.execute(
            """
            INSERT INTO task_queue
                (user_id, task_type, payload, status)
            VALUES (%s, 'autolike', %s::jsonb, 'queued')
            RETURNING id
            """,
            (user_id, json.dumps(payload)),
        )
        row = cur.fetchone()

    return jsonify({"ok": True, "task_id": int(row["id"])})


@app.post("/api/autolike/stop/<int:task_id>")
@login_required
def api_autolike_stop(user_id: int, task_id: int):
    """Mark the task as cancel_requested. The bot will see it and stop."""
    with db_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE task_queue
            SET status = 'cancel_requested'
            WHERE id = %s
              AND user_id = %s
              AND task_type = 'autolike'
              AND status IN ('queued', 'running')
            RETURNING id, status
            """,
            (task_id, user_id),
        )
        row = cur.fetchone()
    if not row:
        return jsonify({"ok": False, "error": "task not found or not stoppable"}), 404
    return jsonify({"ok": True, "task_id": int(row["id"]), "status": row["status"]})


@app.get("/api/tasks/<int:task_id>")
@login_required
def api_task_status(user_id: int, task_id: int):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT id, task_type, status, payload, result, error,
                   created_at, started_at, finished_at
            FROM task_queue
            WHERE id = %s AND user_id = %s
            """,
            (task_id, user_id),
        )
        row = cur.fetchone()
    if not row:
        return jsonify({"ok": False, "error": "not found"}), 404
    out = dict(row)
    for k in ("created_at", "started_at", "finished_at"):
        if out.get(k):
            out[k] = out[k].isoformat()
    if out.get("payload") and not isinstance(out["payload"], dict):
        out["payload"] = dict(out["payload"])
    return jsonify({"ok": True, "task": out})


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@app.get("/healthz")
def healthz():
    try:
        with db_cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return jsonify({"ok": True, "db": "up"})
    except Exception as ex:
        return jsonify({"ok": False, "db": "down", "error": str(ex)}), 500


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
