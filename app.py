"""Telegram Mini App for Vest Game Soft.

The Flask app is deliberately stateless: every write is persisted in PostgreSQL
and long-running Telegram actions are placed in ``task_queue``.  ``bot.py``
polls that queue and executes the existing Telethon workers.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import datetime
from functools import wraps
from typing import Any, Dict, Iterable, Optional
from urllib.parse import parse_qsl

import psycopg
from flask import Flask, jsonify, request, session
from psycopg.rows import dict_row
from psycopg.types.json import Json


app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "change-me-in-production")
app.config["SESSION_COOKIE_SECURE"] = os.getenv("COOKIE_SECURE", "1") != "0"
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://bothost_db_c7b70c49a8ed:QyhslYwQU7g1hT4OD69RP9jcV3EkzmXRLj4VH703ahQ@node1.pghost.ru:15761/bothost_db_c7b70c49a8ed")
BOT_TOKEN = os.getenv("BOT_TOKEN", "8805400400:AAGAX6L8ohYpciEABCzPq5iJx-N8psw_Zx0")
_schema_ready = False


def _connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True)


def _ensure_schema(conn) -> None:
    """Create the small set of tables required by the web app.

    The bot has a more complete migration too; IF NOT EXISTS/ADD COLUMN keeps
    this safe when either process starts first.
    """
    global _schema_ready
    if _schema_ready:
        return
    statements = [
        """CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT,
            joined_at TIMESTAMP DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS accounts (
            id SERIAL PRIMARY KEY, user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
            phone TEXT NOT NULL, session_string TEXT NOT NULL, dc_id INTEGER,
            proxy_id INTEGER, is_active BOOLEAN DEFAULT TRUE,
            warming_enabled BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS proxies (
            id SERIAL PRIMARY KEY, user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
            proxy_type TEXT NOT NULL DEFAULT 'socks5', host TEXT NOT NULL, port INTEGER NOT NULL,
            username TEXT, password TEXT, label TEXT, is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS broadcasts (
            id SERIAL PRIMARY KEY, user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
            account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE, chat_ids TEXT[] NOT NULL,
            delay INTEGER NOT NULL, message_count INTEGER NOT NULL, message_text TEXT,
            message_media TEXT[] DEFAULT '{}', mode TEXT NOT NULL DEFAULT 'simultaneous',
            broadcast_type TEXT NOT NULL DEFAULT 'chat', status TEXT NOT NULL DEFAULT 'active',
            progress INTEGER DEFAULT 0, total_count INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT NOW(),
            started_at TIMESTAMP, stopped_at TIMESTAMP, scheduled_at TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS dm_broadcasts (
            id SERIAL PRIMARY KEY, user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
            account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE, usernames TEXT[] NOT NULL,
            delay INTEGER NOT NULL, message_text TEXT, message_media TEXT[] DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'active', progress INTEGER DEFAULT 0, total_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW(), started_at TIMESTAMP, stopped_at TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS auto_responders (
            id SERIAL PRIMARY KEY, user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
            account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE, trigger TEXT NOT NULL,
            response_text TEXT, response_media TEXT[] DEFAULT '{}', is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS account_logs (
            id SERIAL PRIMARY KEY, account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
            chat_name TEXT, chat_id BIGINT, direction TEXT NOT NULL, message_text TEXT,
            created_at TIMESTAMP DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS account_chats (
            id BIGSERIAL PRIMARY KEY, account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
            chat_id TEXT NOT NULL, name TEXT NOT NULL, chat_type TEXT,
            updated_at TIMESTAMP DEFAULT NOW(), UNIQUE(account_id, chat_id))""",
        """CREATE TABLE IF NOT EXISTS parsed_contacts (
            id BIGSERIAL PRIMARY KEY, user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
            account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE, chat TEXT NOT NULL,
            parse_mode TEXT NOT NULL, user_id_telegram BIGINT, username TEXT,
            first_name TEXT, last_name TEXT, created_at TIMESTAMP DEFAULT NOW())""",
        """CREATE TABLE IF NOT EXISTS task_queue (
            id BIGSERIAL PRIMARY KEY, user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
            task_type TEXT NOT NULL, payload JSONB NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
            entity_id BIGINT, result JSONB, error TEXT, created_at TIMESTAMP DEFAULT NOW(),
            started_at TIMESTAMP, finished_at TIMESTAMP)""",
    ]
    for statement in statements:
        conn.execute(statement)
    # Existing installations may have been created by an older bot revision.
    for statement in (
        "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS warming_enabled BOOLEAN DEFAULT FALSE",
        "ALTER TABLE broadcasts ADD COLUMN IF NOT EXISTS scheduled_at TIMESTAMP",
    ):
        conn.execute(statement)
    _schema_ready = True


def db():
    conn = _connect()
    _ensure_schema(conn)
    return conn


def _json(data: Any):
    return Json(data)


def _telegram_user_from_init_data(init_data: str) -> Optional[Dict[str, Any]]:
    if not BOT_TOKEN or not init_data:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    if not received_hash:
        return None
    check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None
    try:
        if int(pairs.get("auth_date", "0")) < int(time.time()) - 86400:
            return None
        user = json.loads(pairs.get("user", "{}"))
        if not user.get("id"):
            return None
        return user
    except (ValueError, json.JSONDecodeError):
        return None


def _current_user() -> Optional[Dict[str, Any]]:
    return session.get("telegram_user")


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not _current_user():
            return jsonify({"error": "Telegram authorization required"}), 401
        return fn(*args, **kwargs)
    return wrapped


def _user_id() -> int:
    return int(_current_user()["id"])


def _upsert_user(conn, user: Dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO users (user_id, username, first_name) VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username,
        first_name = EXCLUDED.first_name""",
        (int(user["id"]), user.get("username"), user.get("first_name", "")),
    )


def _owned_account(conn, account_id: int) -> Optional[Dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM accounts WHERE id = %s AND user_id = %s",
        (account_id, _user_id()),
    ).fetchone()


def _queue(conn, task_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    row = conn.execute(
        """INSERT INTO task_queue (user_id, task_type, payload) VALUES (%s, %s, %s)
        RETURNING id, task_type, status, created_at""",
        (_user_id(), task_type, _json(payload)),
    ).fetchone()
    return row


def _body() -> Dict[str, Any]:
    return request.get_json(silent=True) or {}


def _positive_int(value, name: str, minimum: int = 1, maximum: int = 2_000_000) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number")
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return result


@app.get("/")
def index():
    return MINI_APP_HTML


@app.post("/api/auth")
def authenticate():
    user = _telegram_user_from_init_data((_body().get("init_data") or "").strip())
    if not user:
        return jsonify({"error": "Invalid or expired Telegram init data"}), 401
    with db() as conn:
        _upsert_user(conn, user)
    session["telegram_user"] = user
    return jsonify({"ok": True, "user": user})


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
def me():
    user = _current_user()
    return jsonify({"authenticated": bool(user), "user": user})


@app.get("/api/dashboard")
@login_required
def dashboard():
    uid = _user_id()
    with db() as conn:
        accounts = conn.execute(
            "SELECT id, phone, is_active, warming_enabled, proxy_id, created_at FROM accounts WHERE user_id=%s ORDER BY id DESC", (uid,)
        ).fetchall()
        proxies = conn.execute(
            "SELECT id, proxy_type, host, port, username, label, is_active, created_at FROM proxies WHERE user_id=%s ORDER BY id DESC", (uid,)
        ).fetchall()
        broadcasts = conn.execute(
            "SELECT id, account_id, chat_ids, delay, message_count, message_text, mode, status, progress, total_count, scheduled_at, created_at FROM broadcasts WHERE user_id=%s ORDER BY created_at DESC LIMIT 50", (uid,)
        ).fetchall()
        dms = conn.execute(
            "SELECT id, account_id, usernames, delay, message_text, status, progress, total_count, created_at FROM dm_broadcasts WHERE user_id=%s ORDER BY created_at DESC LIMIT 50", (uid,)
        ).fetchall()
        responders = conn.execute(
            "SELECT id, account_id, trigger, response_text, is_active, created_at FROM auto_responders WHERE user_id=%s ORDER BY created_at DESC", (uid,)
        ).fetchall()
        tasks = conn.execute(
            "SELECT id, task_type, payload, status, entity_id, result, error, created_at, started_at, finished_at FROM task_queue WHERE user_id=%s ORDER BY created_at DESC LIMIT 100", (uid,)
        ).fetchall()
        stats = {
            "accounts": len(accounts),
            "active_broadcasts": conn.execute("SELECT count(*) AS n FROM broadcasts WHERE user_id=%s AND status='active'", (uid,)).fetchone()["n"],
            "messages_sent": conn.execute("SELECT coalesce(sum(progress),0) AS n FROM broadcasts WHERE user_id=%s", (uid,)).fetchone()["n"],
            "tasks": len(tasks),
        }
    return jsonify({"stats": stats, "accounts": accounts, "proxies": proxies, "broadcasts": broadcasts, "dm_broadcasts": dms, "responders": responders, "tasks": tasks})


@app.get("/api/accounts")
@login_required
def accounts():
    with db() as conn:
        rows = conn.execute("SELECT id, phone, is_active, warming_enabled, proxy_id, created_at FROM accounts WHERE user_id=%s ORDER BY id DESC", (_user_id(),)).fetchall()
    return jsonify(rows)


@app.post("/api/accounts")
@login_required
def add_account():
    data = _body()
    phone, session_string = str(data.get("phone", "")).strip(), str(data.get("session_string", "")).strip()
    if not phone or not session_string:
        return jsonify({"error": "phone and session_string are required"}), 400
    with db() as conn:
        row = conn.execute(
            "INSERT INTO accounts (user_id, phone, session_string, dc_id) VALUES (%s,%s,%s,%s) RETURNING id, phone, is_active, created_at",
            (_user_id(), phone, session_string, data.get("dc_id")),
        ).fetchone()
    return jsonify(row), 201


@app.delete("/api/accounts/<int:account_id>")
@login_required
def remove_account(account_id: int):
    with db() as conn:
        cur = conn.execute("DELETE FROM accounts WHERE id=%s AND user_id=%s", (account_id, _user_id()))
    if cur.rowcount == 0:
        return jsonify({"error": "Account not found"}), 404
    return jsonify({"ok": True})


@app.post("/api/accounts/<int:account_id>/warming")
@login_required
def warming(account_id: int):
    enabled = bool(_body().get("enabled"))
    with db() as conn:
        cur = conn.execute("UPDATE accounts SET warming_enabled=%s WHERE id=%s AND user_id=%s", (enabled, account_id, _user_id()))
    return jsonify({"ok": cur.rowcount > 0, "enabled": enabled})


@app.get("/api/accounts/<int:account_id>/chats")
@login_required
def chats(account_id: int):
    with db() as conn:
        if not _owned_account(conn, account_id):
            return jsonify({"error": "Account not found"}), 404
        rows = conn.execute("SELECT chat_id, name, chat_type, updated_at FROM account_chats WHERE account_id=%s ORDER BY name", (account_id,)).fetchall()
    return jsonify(rows)


@app.post("/api/accounts/<int:account_id>/sync-chats")
@login_required
def sync_chats(account_id: int):
    with db() as conn:
        if not _owned_account(conn, account_id):
            return jsonify({"error": "Account not found"}), 404
        task = _queue(conn, "sync_chats", {"account_id": account_id})
    return jsonify(task), 202


@app.get("/api/proxies")
@login_required
def proxies():
    with db() as conn:
        rows = conn.execute("SELECT id, proxy_type, host, port, username, label, is_active, created_at FROM proxies WHERE user_id=%s ORDER BY id DESC", (_user_id(),)).fetchall()
    return jsonify(rows)


@app.post("/api/proxies")
@login_required
def add_proxy_route():
    data = _body()
    try:
        port = _positive_int(data.get("port"), "port", 1, 65535)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    host = str(data.get("host", "")).strip()
    if not host:
        return jsonify({"error": "host is required"}), 400
    with db() as conn:
        row = conn.execute("INSERT INTO proxies (user_id, proxy_type, host, port, username, password, label) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id, proxy_type, host, port, username, label, is_active", (_user_id(), data.get("proxy_type", "socks5"), host, port, data.get("username"), data.get("password"), data.get("label"))).fetchone()
    return jsonify(row), 201


@app.delete("/api/proxies/<int:proxy_id>")
@login_required
def remove_proxy(proxy_id: int):
    with db() as conn:
        conn.execute("UPDATE accounts SET proxy_id=NULL WHERE proxy_id=%s AND user_id=%s", (proxy_id, _user_id()))
        cur = conn.execute("DELETE FROM proxies WHERE id=%s AND user_id=%s", (proxy_id, _user_id()))
    return jsonify({"ok": cur.rowcount > 0})


@app.post("/api/accounts/<int:account_id>/proxy")
@login_required
def account_proxy(account_id: int):
    proxy_id = _body().get("proxy_id")
    with db() as conn:
        if not _owned_account(conn, account_id):
            return jsonify({"error": "Account not found"}), 404
        if proxy_id is not None and not conn.execute("SELECT 1 FROM proxies WHERE id=%s AND user_id=%s", (proxy_id, _user_id())).fetchone():
            return jsonify({"error": "Proxy not found"}), 404
        conn.execute("UPDATE accounts SET proxy_id=%s WHERE id=%s AND user_id=%s", (proxy_id, account_id, _user_id()))
    return jsonify({"ok": True})


@app.post("/api/broadcasts")
@login_required
def create_broadcast():
    data = _body()
    try:
        account_id = _positive_int(data.get("account_id"), "account_id")
        delay = _positive_int(data.get("delay", 30), "delay", 1, 300000)
        count = _positive_int(data.get("message_count", 1), "message_count", 1, 200000)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    chat_ids = [str(x).strip() for x in data.get("chat_ids", []) if str(x).strip()]
    if not chat_ids or not str(data.get("message_text", "")).strip():
        return jsonify({"error": "chat_ids and message_text are required"}), 400
    payload = {"account_id": account_id, "chat_ids": chat_ids, "delay": delay, "message_count": count, "message_text": str(data["message_text"]), "message_media": data.get("message_media", []), "mode": data.get("mode", "simultaneous"), "scheduled_at": data.get("scheduled_at")}
    with db() as conn:
        if not _owned_account(conn, account_id):
            return jsonify({"error": "Account not found"}), 404
        task = _queue(conn, "schedule_broadcast" if payload["scheduled_at"] else "broadcast", payload)
    return jsonify(task), 202


@app.post("/api/dm-broadcasts")
@login_required
def create_dm_broadcast():
    data = _body()
    try:
        account_id = _positive_int(data.get("account_id"), "account_id")
        delay = _positive_int(data.get("delay", 30), "delay", 1, 300000)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    usernames = [str(x).strip() for x in data.get("usernames", []) if str(x).strip()]
    if not usernames or not str(data.get("message_text", "")).strip():
        return jsonify({"error": "usernames and message_text are required"}), 400
    payload = {"account_id": account_id, "usernames": usernames, "delay": delay, "message_text": str(data["message_text"]), "message_media": data.get("message_media", [])}
    with db() as conn:
        if not _owned_account(conn, account_id):
            return jsonify({"error": "Account not found"}), 404
        task = _queue(conn, "dm_broadcast", payload)
    return jsonify(task), 202


@app.post("/api/operations/<string:operation>")
@login_required
def operation(operation: str):
    allowed = {"join": "join", "autolike": "autolike", "delete_messages": "delete_messages"}
    if operation not in allowed:
        return jsonify({"error": "Unknown operation"}), 404
    data = _body()
    try:
        account_id = _positive_int(data.get("account_id"), "account_id")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    with db() as conn:
        if not _owned_account(conn, account_id):
            return jsonify({"error": "Account not found"}), 404
        if operation == "join":
            links = [str(x).strip() for x in data.get("links", []) if str(x).strip()]
            if not links:
                return jsonify({"error": "links are required"}), 400
            payload = {"account_id": account_id, "links": links, "delay": _positive_int(data.get("delay", 30), "delay", 30, 300000)}
        elif operation == "autolike":
            payload = {"account_id": account_id, "chat_ids": [str(x) for x in data.get("chat_ids", [])], "reaction": data.get("reaction", "👍"), "delay": _positive_int(data.get("delay", 60), "delay", 1, 300000)}
        else:
            payload = {"account_id": account_id, "chat_ids": [str(x) for x in data.get("chat_ids", [])], "hours": _positive_int(data.get("hours", 24), "hours", 1, 8760)}
        task = _queue(conn, allowed[operation], payload)
    return jsonify(task), 202


@app.post("/api/parse")
@login_required
def parse_chat():
    data = _body()
    try:
        account_id = _positive_int(data.get("account_id"), "account_id")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    chat = str(data.get("chat", "")).strip()
    mode = data.get("mode", "usernames")
    if not chat or mode not in {"all", "usernames", "names", "names_usernames"}:
        return jsonify({"error": "chat and a valid mode are required"}), 400
    with db() as conn:
        if not _owned_account(conn, account_id):
            return jsonify({"error": "Account not found"}), 404
        task = _queue(conn, "parse", {"account_id": account_id, "chat": chat, "mode": mode})
    return jsonify(task), 202


@app.get("/api/parsed-contacts")
@login_required
def parsed_contacts():
    account_id = request.args.get("account_id")
    with db() as conn:
        params = [_user_id()]
        query = "SELECT id, account_id, chat, parse_mode, user_id_telegram, username, first_name, last_name, created_at FROM parsed_contacts WHERE user_id=%s"
        if account_id:
            query += " AND account_id=%s"
            params.append(int(account_id))
        query += " ORDER BY created_at DESC LIMIT 5000"
        rows = conn.execute(query, tuple(params)).fetchall()
    return jsonify(rows)


@app.post("/api/responders")
@login_required
def create_responder():
    data = _body()
    account_id, trigger, response_text = data.get("account_id"), str(data.get("trigger", "")).strip(), str(data.get("response_text", ""))
    try:
        account_id = _positive_int(account_id, "account_id")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    with db() as conn:
        if not _owned_account(conn, account_id):
            return jsonify({"error": "Account not found"}), 404
        task = _queue(conn, "create_responder", {"account_id": account_id, "trigger": trigger or "-", "response_text": response_text, "response_media": data.get("response_media", [])})
    return jsonify(task), 202


@app.post("/api/responders/<int:responder_id>/<action>")
@login_required
def responder_action(responder_id: int, action: str):
    if action not in {"start", "stop", "delete"}:
        return jsonify({"error": "Unknown action"}), 404
    with db() as conn:
        row = conn.execute("SELECT id, account_id FROM auto_responders WHERE id=%s AND user_id=%s", (responder_id, _user_id())).fetchone()
        if not row:
            return jsonify({"error": "Responder not found"}), 404
        if action == "delete":
            conn.execute("DELETE FROM auto_responders WHERE id=%s AND user_id=%s", (responder_id, _user_id()))
            task = {"ok": True}
        else:
            conn.execute("UPDATE auto_responders SET is_active=%s WHERE id=%s AND user_id=%s", (action == "start", responder_id, _user_id()))
            task = _queue(conn, "start_responder" if action == "start" else "stop_responder", {"responder_id": responder_id, "account_id": row["account_id"]})
    return jsonify(task)


@app.get("/api/logs/<int:account_id>")
@login_required
def logs(account_id: int):
    limit = min(int(request.args.get("limit", 100)), 500)
    with db() as conn:
        if not _owned_account(conn, account_id):
            return jsonify({"error": "Account not found"}), 404
        rows = conn.execute("SELECT id, chat_name, chat_id, direction, message_text, created_at FROM account_logs WHERE account_id=%s ORDER BY created_at DESC LIMIT %s", (account_id, limit)).fetchall()
    return jsonify(rows)


@app.get("/api/tasks")
@login_required
def tasks():
    with db() as conn:
        rows = conn.execute("SELECT id, task_type, payload, status, entity_id, result, error, created_at, started_at, finished_at FROM task_queue WHERE user_id=%s ORDER BY created_at DESC LIMIT 100", (_user_id(),)).fetchall()
    return jsonify(rows)


@app.post("/api/tasks/<int:task_id>/stop")
@login_required
def stop_task(task_id: int):
    with db() as conn:
        row = conn.execute("SELECT id, task_type, entity_id FROM task_queue WHERE id=%s AND user_id=%s", (task_id, _user_id())).fetchone()
        if not row:
            return jsonify({"error": "Task not found"}), 404
        conn.execute("UPDATE task_queue SET status='cancel_requested' WHERE id=%s AND status IN ('queued','running')", (task_id,))
        if row["task_type"] in {"broadcast", "schedule_broadcast"} and row["entity_id"]:
            conn.execute("UPDATE broadcasts SET status='stopped', stopped_at=NOW() WHERE id=%s AND user_id=%s", (row["entity_id"], _user_id()))
        if row["task_type"] == "dm_broadcast" and row["entity_id"]:
            conn.execute("UPDATE dm_broadcasts SET status='stopped', stopped_at=NOW() WHERE id=%s AND user_id=%s", (row["entity_id"], _user_id()))
    return jsonify({"ok": True})


MINI_APP_HTML = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>Vest Game Soft</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--blue:#2563eb;--navy:#0f2d63;--pale:#eef5ff;--ink:#14213d;--muted:#71809b;--line:#dfe8f7;--white:#fff}*{box-sizing:border-box}body{margin:0;background:#f6f9ff;color:var(--ink);font:15px/1.45 Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}button,input,textarea,select{font:inherit}button{cursor:pointer;border:0}.shell{max-width:1180px;margin:auto;padding:18px 18px 44px}.top{background:linear-gradient(135deg,#fff 0%,#edf4ff 100%);border:1px solid var(--line);border-radius:24px;padding:24px;display:flex;justify-content:space-between;gap:18px;align-items:center;box-shadow:0 16px 45px #204c9b12}.brand{display:flex;gap:13px;align-items:center}.mark{width:47px;height:47px;border-radius:15px;background:linear-gradient(145deg,#2f7bff,#123a93);display:grid;place-items:center;color:white;font-size:22px;box-shadow:0 8px 20px #1c5be344}.eyebrow{color:var(--blue);font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.14em}.title{font-size:27px;font-weight:800;margin:2px 0}.user{display:flex;align-items:center;gap:10px;color:var(--muted)}.avatar{width:36px;height:36px;border-radius:50%;background:var(--pale);display:grid;place-items:center;color:var(--blue);font-weight:800}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 0}.stat,.card{background:var(--white);border:1px solid var(--line);border-radius:18px;padding:17px;box-shadow:0 7px 22px #204c9b0a}.stat b{display:block;font-size:25px;color:var(--navy)}.stat span{color:var(--muted);font-size:13px}.tabs{display:flex;gap:8px;overflow:auto;padding:3px;margin:3px 0 15px}.tab{padding:10px 15px;border-radius:12px;background:white;border:1px solid var(--line);color:var(--muted);white-space:nowrap}.tab.active{background:var(--blue);border-color:var(--blue);color:#fff}.panel{display:none}.panel.active{display:block}.section-title{display:flex;justify-content:space-between;align-items:center;margin:18px 0 10px}.section-title h2{font-size:19px;margin:0}.section-title small{color:var(--muted)}.cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.row{display:flex;justify-content:space-between;gap:12px;align-items:center}.muted{color:var(--muted)}.pill{display:inline-flex;padding:4px 9px;border-radius:99px;background:var(--pale);color:var(--blue);font-size:12px;font-weight:700}.pill.ok{background:#eaf9f0;color:#148346}.pill.warn{background:#fff5df;color:#a86a00}.btn{padding:10px 13px;border-radius:11px;background:var(--blue);color:#fff;font-weight:700}.btn.secondary{background:var(--pale);color:var(--blue)}.btn.danger{background:#fff0f1;color:#c42e43}.btn.ghost{background:transparent;color:var(--blue);padding:6px}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}.form{display:grid;gap:10px}.form.two{grid-template-columns:repeat(2,minmax(0,1fr))}.field{display:grid;gap:5px}.field label{font-size:12px;color:var(--muted);font-weight:700}.field input,.field textarea,.field select{width:100%;padding:11px 12px;border:1px solid var(--line);border-radius:11px;background:#fbfdff;color:var(--ink);outline:0}.field textarea{min-height:90px;resize:vertical}.field input:focus,.field textarea:focus,.field select:focus{border-color:var(--blue);box-shadow:0 0 0 3px #2563eb18}.empty{color:var(--muted);padding:25px;text-align:center;border:1px dashed var(--line);border-radius:14px}.toast{position:fixed;right:18px;bottom:18px;background:var(--navy);color:white;padding:12px 15px;border-radius:12px;box-shadow:0 10px 35px #0f2d6340;display:none;z-index:5}.table{width:100%;border-collapse:collapse}.table th,.table td{text-align:left;padding:9px 7px;border-bottom:1px solid var(--line);font-size:13px}.table th{color:var(--muted);font-weight:700}.notice{background:#fff9e9;border:1px solid #f2df9e;border-radius:14px;padding:12px;color:#805b09;margin-bottom:12px}@media(max-width:760px){.grid{grid-template-columns:repeat(2,1fr)}.cards{grid-template-columns:1fr}.form.two{grid-template-columns:1fr}.top{align-items:flex-start;flex-direction:column}.title{font-size:23px}}
</style></head><body><main class="shell"><header class="top"><div class="brand"><div class="mark">V</div><div><div class="eyebrow">Telegram control center</div><div class="title">Vest Game Soft</div><div class="muted">Белый интерфейс · синие инструменты · PostgreSQL</div></div></div><div class="user"><div class="avatar" id="avatar">?</div><span id="who">Авторизация…</span><button class="btn secondary" onclick="logout()">Выйти</button></div></header>
<div class="grid"><div class="stat"><b id="sAccounts">—</b><span>аккаунтов</span></div><div class="stat"><b id="sActive">—</b><span>активных задач</span></div><div class="stat"><b id="sSent">—</b><span>отправлено сообщений</span></div><div class="stat"><b id="sTasks">—</b><span>задач в истории</span></div></div>
<nav class="tabs"><button class="tab active" data-tab="overview">Обзор</button><button class="tab" data-tab="accounts">Аккаунты</button><button class="tab" data-tab="broadcast">Рассылки</button><button class="tab" data-tab="tools">Инструменты</button><button class="tab" data-tab="settings">Прокси и автоответы</button></nav>
<section id="overview" class="panel active"><div class="section-title"><h2>Последние задачи</h2><button class="btn secondary" onclick="load()">Обновить</button></div><div id="tasks" class="cards"></div><div class="section-title"><h2>Журнал аккаунта</h2></div><div class="card"><div id="logs" class="empty">Выберите аккаунт во вкладке «Аккаунты»</div></div></section>
<section id="accounts" class="panel"><div class="section-title"><h2>Аккаунты Telegram</h2><small>Сессионная строка хранится только в PostgreSQL</small></div><div class="notice">Добавляйте только собственные Telegram-аккаунты. Получить session string можно через авторизацию в боте.</div><div class="card"><form id="accountForm" class="form two"><div class="field"><label>Телефон</label><input name="phone" placeholder="+79990000000" required></div><div class="field"><label>DC ID (необязательно)</label><input name="dc_id" type="number" placeholder="2"></div><div class="field" style="grid-column:1/-1"><label>Telethon session string</label><textarea name="session_string" placeholder="1BVtsOH..." required></textarea></div><div><button class="btn">Добавить аккаунт</button></div></form></div><div id="accountsList" class="cards" style="margin-top:12px"></div></section>
<section id="broadcast" class="panel"><div class="section-title"><h2>Рассылки</h2><small>чаты, личные сообщения и расписание</small></div><div class="cards"><div class="card"><h3>В чаты</h3><form id="broadcastForm" class="form"><div class="field"><label>Аккаунт</label><select name="account_id" class="accountSelect" required></select></div><div class="field"><label>Chat ID, по одному в строке</label><textarea name="chat_ids" placeholder="-100123456789\n@channel"></textarea></div><div class="form two"><div class="field"><label>Задержка, сек</label><input name="delay" type="number" value="30" min="1"></div><div class="field"><label>Сообщений на чат</label><input name="message_count" type="number" value="1" min="1"></div></div><div class="field"><label>Текст</label><textarea name="message_text" required placeholder="Ваше сообщение"></textarea></div><div class="form two"><div class="field"><label>Режим</label><select name="mode"><option value="simultaneous">Одновременно</option><option value="random">Случайный</option></select></div><div class="field"><label>Отложить до (ISO, необязательно)</label><input name="scheduled_at" placeholder="2026-08-01T12:00:00+03:00"></div></div><button class="btn">Запустить рассылку</button></form></div><div class="card"><h3>В личные сообщения</h3><form id="dmForm" class="form"><div class="field"><label>Аккаунт</label><select name="account_id" class="accountSelect" required></select></div><div class="field"><label>Username, по одному в строке</label><textarea name="usernames" placeholder="username1\n@username2"></textarea></div><div class="field"><label>Задержка, сек</label><input name="delay" type="number" value="30" min="1"></div><div class="field"><label>Текст</label><textarea name="message_text" required></textarea></div><button class="btn">Запустить DM-рассылку</button></form></div></div><div class="section-title"><h2>История рассылок</h2></div><div class="card"><div id="broadcasts" class="empty">Пока нет рассылок</div></div></section>
<section id="tools" class="panel"><div class="section-title"><h2>Инструменты</h2><small>каждая операция уходит в очередь бота</small></div><div class="cards"><div class="card"><h3>Вступление в чаты</h3><form class="toolForm form" data-operation="join"><div class="field"><label>Аккаунт</label><select name="account_id" class="accountSelect" required></select></div><div class="field"><label>Ссылки, по одной в строке</label><textarea name="links" placeholder="https://t.me/channel"></textarea></div><div class="field"><label>Задержка, сек (мин. 30)</label><input name="delay" value="30" type="number"></div><button class="btn">Поставить в очередь</button></form></div><div class="card"><h3>Авто-лайкинг</h3><form class="toolForm form" data-operation="autolike"><div class="field"><label>Аккаунт</label><select name="account_id" class="accountSelect" required></select></div><div class="field"><label>Chat ID, по одному в строке</label><textarea name="chat_ids"></textarea></div><div class="form two"><div class="field"><label>Реакция</label><select name="reaction"><option>👍</option><option>❤</option><option>🔥</option><option>🎉</option><option>🤩</option></select></div><div class="field"><label>Задержка, сек</label><input name="delay" value="60" type="number"></div></div><button class="btn">Запустить лайкинг</button></form></div><div class="card"><h3>Удаление своих сообщений</h3><form class="toolForm form" data-operation="delete_messages"><div class="field"><label>Аккаунт</label><select name="account_id" class="accountSelect" required></select></div><div class="field"><label>Chat ID, по одному в строке</label><textarea name="chat_ids"></textarea></div><div class="field"><label>За последние часов</label><input name="hours" value="24" type="number"></div><button class="btn danger">Удалить сообщения</button></form></div></div></section>
<section id="settings" class="panel"><div class="section-title"><h2>Прокси</h2><small>SOCKS5, SOCKS4 или HTTP</small></div><div class="cards"><div class="card"><form id="proxyForm" class="form two"><div class="field"><label>Тип</label><select name="proxy_type"><option>socks5</option><option>socks4</option><option>http</option></select></div><div class="field"><label>Хост</label><input name="host" required></div><div class="field"><label>Порт</label><input name="port" type="number" value="1080" required></div><div class="field"><label>Метка</label><input name="label" placeholder="Резидентский"></div><div class="field"><label>Логин</label><input name="username"></div><div class="field"><label>Пароль</label><input name="password" type="password"></div><div><button class="btn">Добавить прокси</button></div></form></div><div class="card"><h3>Автоответчик</h3><form id="responderForm" class="form"><div class="field"><label>Аккаунт</label><select name="account_id" class="accountSelect" required></select></div><div class="field"><label>Триггер (− = любой входящий)</label><input name="trigger" value="-"></div><div class="field"><label>Ответ</label><textarea name="response_text" required></textarea></div><button class="btn">Создать автоответчик</button></form></div></div><div class="section-title"><h2>Сохранённые прокси и автоответчики</h2></div><div id="settingsList" class="cards"></div></section>
<div id="toast" class="toast"></div></main><script>
const tg=window.Telegram?.WebApp; if(tg){tg.ready();tg.expand()} let state={};
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
function toast(t){const e=$('#toast');e.textContent=t;e.style.display='block';setTimeout(()=>e.style.display='none',3200)}
async function api(url,opt={}){const r=await fetch(url,{headers:{'Content-Type':'application/json',...(opt.headers||{})},...opt});const d=await r.json().catch(()=>({}));if(!r.ok)throw Error(d.error||'Ошибка запроса');return d}
async function auth(){const me=await api('/api/me');if(!me.authenticated){const init=tg?.initData||'';if(!init){toast('Откройте приложение внутри Telegram');return}await api('/api/auth',{method:'POST',body:JSON.stringify({init_data:init})})}load()}
function setUser(u){if(!u)return;$('#who').textContent=u.username?'@'+u.username:(u.first_name||'Пользователь');$('#avatar').textContent=(u.first_name||'V')[0].toUpperCase()}
async function load(){try{const d=await api('/api/dashboard');state=d;setUser((await api('/api/me')).user);$('#sAccounts').textContent=d.stats.accounts;$('#sActive').textContent=d.stats.active_broadcasts;$('#sSent').textContent=d.stats.messages_sent;$('#sTasks').textContent=d.stats.tasks;renderAccounts(d.accounts);renderTasks(d.tasks);renderBroadcasts(d.broadcasts,d.dm_broadcasts);renderSettings(d.proxies,d.responders)}catch(e){toast(e.message)}}
function options(accounts){return accounts.length?accounts.map(a=>`<option value="${a.id}">${a.phone} · #${a.id}</option>`).join(''):'<option value="">Нет аккаунтов</option>'}
function fillSelects(){ $$('.accountSelect').forEach(s=>s.innerHTML=options(state.accounts||[])) }
function renderAccounts(a){fillSelects();$('#accountsList').innerHTML=a.length?a.map(x=>`<div class="card"><div class="row"><div><b>${x.phone}</b><div class="muted">Аккаунт #${x.id} · ${x.is_active?'активен':'выключен'}</div></div><span class="pill ${x.warming_enabled?'warn':'ok'}">${x.warming_enabled?'прогрев':'готов'}</span></div><div class="actions"><button class="btn secondary" onclick="syncChats(${x.id})">Синхронизировать чаты</button><button class="btn ghost" onclick="showLogs(${x.id})">Журнал</button><button class="btn danger" onclick="removeAccount(${x.id})">Удалить</button></div></div>`).join(''):'<div class="empty">Аккаунтов пока нет</div>'}
function renderTasks(t){$('#tasks').innerHTML=t.length?t.slice(0,12).map(x=>`<div class="card"><div class="row"><b>${x.task_type}</b><span class="pill ${x.status==='completed'?'ok':x.status==='failed'?'warn':''}">${x.status}</span></div><div class="muted" style="margin-top:7px">#${x.id} · ${new Date(x.created_at).toLocaleString()}</div>${x.error?`<div style="color:#c42e43;margin-top:5px">${x.error}</div>`:''}${['queued','running','cancel_requested'].includes(x.status)?`<div class="actions"><button class="btn danger" onclick="stopTask(${x.id})">Остановить</button></div>`:''}</div>`).join(''):'<div class="empty">Задач пока нет</div>'}
function renderBroadcasts(b,d){const all=[...b.map(x=>({...x,kind:'Чаты'})),...d.map(x=>({...x,kind:'DM'}))].sort((x,y)=>new Date(y.created_at)-new Date(x.created_at));$('#broadcasts').innerHTML=all.length?`<table class="table"><tr><th>Тип</th><th>ID</th><th>Статус</th><th>Прогресс</th><th></th></tr>${all.map(x=>`<tr><td>${x.kind}</td><td>#${x.id}</td><td><span class="pill">${x.status}</span></td><td>${x.progress||0}/${x.total_count||'—'}</td><td>${['active','scheduled'].includes(x.status)?`<button class="btn ghost" onclick="stopByEntity('${x.kind}',${x.id})">стоп</button>`:''}</td></tr>`).join('')}</table>`:'<div class="empty">Пока нет рассылок</div>'}
function renderSettings(p,r){$('#settingsList').innerHTML=[...p.map(x=>`<div class="card"><div class="row"><b>${x.label||x.host}</b><span class="pill">${x.proxy_type}:${x.port}</span></div><div class="muted">${x.host} · ${x.username||'без логина'}</div><div class="actions"><button class="btn danger" onclick="removeProxy(${x.id})">Удалить</button></div></div>`),...r.map(x=>`<div class="card"><div class="row"><b>Автоответчик #${x.id}</b><span class="pill ${x.is_active?'ok':''}">${x.is_active?'включён':'выключен'}</span></div><div class="muted">${x.trigger} → ${x.response_text||''}</div><div class="actions"><button class="btn secondary" onclick="responderAction(${x.id},'${x.is_active?'stop':'start'}')">${x.is_active?'Остановить':'Запустить'}</button><button class="btn danger" onclick="responderAction(${x.id},'delete')">Удалить</button></div></div>`)].join('')||'<div class="empty">Настроек пока нет</div>'}
async function submitForm(form,url,transform){const data=Object.fromEntries(new FormData(form));const out=transform?transform(data):data;await api(url,{method:'POST',body:JSON.stringify(out)});form.reset();toast('Задача поставлена в очередь');load()}
$('#accountForm').onsubmit=e=>{e.preventDefault();submitForm(e.target,'/api/accounts',d=>({...d,dc_id:d.dc_id?Number(d.dc_id):null})).catch(x=>toast(x.message))};$('#broadcastForm').onsubmit=e=>{e.preventDefault();submitForm(e.target,'/api/broadcasts',d=>({...d,account_id:Number(d.account_id),chat_ids:d.chat_ids.split(/\n|,/).map(x=>x.trim()).filter(Boolean),delay:Number(d.delay),message_count:Number(d.message_count),scheduled_at:d.scheduled_at||null})).catch(x=>toast(x.message))};$('#dmForm').onsubmit=e=>{e.preventDefault();submitForm(e.target,'/api/dm-broadcasts',d=>({...d,account_id:Number(d.account_id),usernames:d.usernames.split(/\n|,/).map(x=>x.trim()).filter(Boolean),delay:Number(d.delay)})).catch(x=>toast(x.message))};$$('.toolForm').forEach(f=>f.onsubmit=e=>{e.preventDefault();const op=f.dataset.operation;submitForm(f,'/api/operations/'+op,d=>({...d,account_id:Number(d.account_id),chat_ids:(d.chat_ids||'').split(/\n|,/).map(x=>x.trim()).filter(Boolean),links:(d.links||'').split(/\n|,/).map(x=>x.trim()).filter(Boolean),delay:Number(d.delay),hours:Number(d.hours)})).catch(x=>toast(x.message))});$('#proxyForm').onsubmit=e=>{e.preventDefault();submitForm(e.target,'/api/proxies',d=>({...d,port:Number(d.port)})).catch(x=>toast(x.message))};$('#responderForm').onsubmit=e=>{e.preventDefault();submitForm(e.target,'/api/responders',d=>({...d,account_id:Number(d.account_id)})).catch(x=>toast(x.message))};
async function removeAccount(id){if(confirm('Удалить аккаунт?')){try{await api('/api/accounts/'+id,{method:'DELETE'});load()}catch(e){toast(e.message)}}}async function removeProxy(id){try{await api('/api/proxies/'+id,{method:'DELETE'});load()}catch(e){toast(e.message)}}async function syncChats(id){try{await api('/api/accounts/'+id+'/sync-chats',{method:'POST'});toast('Синхронизация поставлена в очередь');load()}catch(e){toast(e.message)}}async function stopTask(id){try{await api('/api/tasks/'+id+'/stop',{method:'POST'});load()}catch(e){toast(e.message)}}async function stopByEntity(kind,id){const t=(state.tasks||[]).find(x=>x.entity_id===id);if(t)stopTask(t.id);else toast('Задача ещё запускается') }async function responderAction(id,a){try{await api('/api/responders/'+id+'/'+a,{method:'POST'});load()}catch(e){toast(e.message)}}async function showLogs(id){try{const rows=await api('/api/logs/'+id);$('#logs').innerHTML=rows.length?`<table class="table"><tr><th>Время</th><th>Направление</th><th>Чат</th><th>Текст</th></tr>${rows.map(x=>`<tr><td>${new Date(x.created_at).toLocaleString()}</td><td>${x.direction}</td><td>${x.chat_name||x.chat_id||''}</td><td>${x.message_text||''}</td></tr>`).join('')}</table>`:'<div class="empty">Журнал пуст</div>';document.querySelector('[data-tab="overview"]').click()}catch(e){toast(e.message)}}async function logout(){await api('/api/logout',{method:'POST'});location.reload()}
$$('.tab').forEach(b=>b.onclick=()=>{$$('.tab').forEach(x=>x.classList.toggle('active',x===b));$$('.panel').forEach(x=>x.classList.toggle('active',x.id===b.dataset.tab))});auth();setInterval(load,15000);
</script></body></html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=os.getenv("FLASK_DEBUG") == "1")
