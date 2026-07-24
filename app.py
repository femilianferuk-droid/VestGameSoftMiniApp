"""
Vest Mini App — Flask, single-file edition.
HTML / CSS / JS зашиты внутрь как константы и отдаются через роуты.

Серверная часть бота (bot.py) уже крутит task_queue_worker — мы
только пишем задачи в task_queue и ставим status='cancel_requested'
для остановки.

Запуск:
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
from functools import wraps
from typing import Any, Dict, Optional

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from flask import (
    Flask, Response, jsonify, render_template_string, request, session,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://bothost_db_c7b70c49a8ed:QyhslYwQU7g1hT4OD69RP9jcV3EkzmXRLj4VH703ahQ"
    "@node1.pghost.ru:15761/bothost_db_c7b70c49a8ed",
)
BOT_TOKEN = os.getenv(
    "BOT_TOKEN",
    "8805400400:AAGAX6L8ohYpciEABCzPq5iJx-N8psw_Zx0",
)
SECRET_KEY = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
INIT_DATA_TTL_SECONDS = int(os.getenv("INIT_DATA_TTL_SECONDS", "3600"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("vest-mini")

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
app.config["JSON_AS_ASCII"] = False
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 7


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
@contextmanager
def db_cursor(commit: bool = False):
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
    user_id = int(tg_user["id"])
    username = tg_user.get("username")
    first_name = tg_user.get("first_name") or ""
    last_name = tg_user.get("last_name")
    full_name = " ".join(p for p in [first_name, last_name] if p).strip() \
        or (username or f"user_{user_id}")
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
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def validate_init_data(init_data: str) -> Optional[Dict[str, Any]]:
    if not init_data:
        return None
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    except ValueError:
        return None

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret = _build_secret_key(BOT_TOKEN)
    computed = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, received_hash):
        logger.warning("initData hash mismatch")
        return None

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
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user_id = session.get("user_id")
        if not user_id:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        return fn(user_id, *args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Pages & static assets (всё inline)
# ---------------------------------------------------------------------------
INDEX_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, user-scalable=no" />
  <meta name="theme-color" content="#0b0b14" />
  <title>Vest Game — Mini App</title>
  <link rel="stylesheet" href="/static/app.css" />
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
</head>
<body data-bot-username="{{ bot_username }}">
  <div class="bg-orbs" aria-hidden="true">
    <span class="orb orb-a"></span>
    <span class="orb orb-b"></span>
    <span class="orb orb-c"></span>
  </div>

  <!-- Login screen -->
  <section id="login-screen" class="screen">
    <div class="card auth-card">
      <div class="brand">
        <div class="logo">
          <svg viewBox="0 0 32 32" width="42" height="42" aria-hidden="true">
            <defs>
              <linearGradient id="g1" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0%" stop-color="#7c5cff"/>
                <stop offset="100%" stop-color="#22d3ee"/>
              </linearGradient>
            </defs>
            <path d="M16 2 L29 9 V23 L16 30 L3 23 V9 Z" fill="url(#g1)"/>
            <path d="M16 8 L23 12 V20 L16 24 L9 20 V12 Z" fill="#0b0b14" opacity=".55"/>
          </svg>
        </div>
        <h1>Vest Game</h1>
        <p class="muted">Мини-приложение для автолайкинга</p>
      </div>
      <div class="auth-status" id="auth-status">
        <div class="spinner"></div>
        <span>Подключаем Telegram…</span>
      </div>
      <button class="btn primary" id="auth-retry" hidden>Повторить вход</button>
    </div>
  </section>

  <!-- Dashboard -->
  <section id="dashboard" class="screen" hidden>
    <header class="topbar">
      <div class="me">
        <div class="avatar" id="me-avatar">–</div>
        <div class="me-text">
          <div class="me-name" id="me-name">…</div>
          <div class="me-sub" id="me-sub">…</div>
        </div>
      </div>
      <button class="icon-btn" id="btn-refresh" title="Обновить">
        <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-3.5-7.1"/><path d="M21 4v5h-5"/></svg>
      </button>
    </header>

    <div class="stat-strip">
      <div class="stat"><div class="stat-label">Аккаунтов</div><div class="stat-value" id="stat-accounts">0</div></div>
      <div class="stat"><div class="stat-label">Активных задач</div><div class="stat-value" id="stat-active">0</div></div>
      <div class="stat"><div class="stat-label">Лайков 24ч</div><div class="stat-value" id="stat-likes">0</div></div>
      <div class="stat"><div class="stat-label">Чатов</div><div class="stat-value" id="stat-chats">0</div></div>
    </div>

    <nav class="tabs" role="tablist">
      <button class="tab active" data-tab="autolike" role="tab">Автолайкинг</button>
      <button class="tab" data-tab="accounts" role="tab">Аккаунты</button>
      <button class="tab" data-tab="tasks" role="tab">Задачи</button>
      <button class="tab" data-tab="logs" role="tab">Лог</button>
    </nav>

    <!-- AUTOLIKE -->
    <div class="tab-panel active" data-panel="autolike">
      <div class="card">
        <div class="card-head">
          <h2>Запустить автолайкинг</h2>
          <span class="pill" id="pill-bot-status">Проверка…</span>
        </div>
        <label class="field">
          <span class="field-label">Аккаунт</span>
          <select id="f-account" class="select"></select>
        </label>
        <label class="field">
          <span class="field-label">Реакция</span>
          <div class="emoji-grid" id="emoji-grid"></div>
        </label>
        <label class="field">
          <span class="field-label">Чаты (по одному на строку, можно @username или ID)</span>
          <textarea id="f-chats" class="textarea" rows="5" placeholder="@durov&#10;@telegram&#10;-1001234567890"></textarea>
        </label>
        <div class="row-2">
          <label class="field">
            <span class="field-label">Задержка (сек)</span>
            <input id="f-delay" type="number" class="input" min="10" max="3600" value="60"/>
          </label>
          <label class="field">
            <span class="field-label">Режим</span>
            <select id="f-mode" class="select">
              <option value="loop">Бесконечно</option>
              <option value="once" disabled>Один круг (скоро)</option>
            </select>
          </label>
        </div>
        <div class="actions">
          <button class="btn primary" id="btn-start">Запустить</button>
          <button class="btn ghost" id="btn-fill-chats" title="Подтянуть чаты аккаунта">Чаты аккаунта</button>
        </div>
      </div>

      <div class="card" id="chats-cache-card" hidden>
        <div class="card-head">
          <h3>Кэш чатов</h3>
          <span class="muted" id="chats-cache-meta">…</span>
        </div>
        <div class="chats-list" id="chats-cache-list"></div>
      </div>
    </div>

    <!-- ACCOUNTS -->
    <div class="tab-panel" data-panel="accounts">
      <div class="card">
        <div class="card-head"><h2>Аккаунты</h2><span class="muted" id="accounts-meta">…</span></div>
        <div id="accounts-list" class="grid-cards"></div>
      </div>
    </div>

    <!-- TASKS -->
    <div class="tab-panel" data-panel="tasks">
      <div class="card">
        <div class="card-head"><h2>Очередь задач</h2><span class="muted">последние 50</span></div>
        <div id="tasks-list" class="tasks-list"></div>
      </div>
    </div>

    <!-- LOGS -->
    <div class="tab-panel" data-panel="logs">
      <div class="card">
        <div class="card-head"><h2>Активность</h2><span class="muted">последние 40</span></div>
        <div id="logs-list" class="logs-list"></div>
      </div>
    </div>

    <footer class="foot muted">
      <span>v1.1 · Mini App</span>
      <span id="last-update">—</span>
    </footer>
  </section>

  <div id="toast" class="toast" hidden></div>
  <script src="/static/app.js"></script>
</body>
</html>
"""

APP_CSS = r""":root {
  --bg-0: #07070d;
  --bg-1: #0b0b14;
  --bg-2: #11111d;
  --line: rgba(255, 255, 255, 0.07);
  --line-strong: rgba(255, 255, 255, 0.14);
  --text: #e9ecf1;
  --text-dim: #8b91a3;
  --text-mute: #5a6072;
  --accent: #7c5cff;
  --accent-2: #22d3ee;
  --accent-3: #ff6ec7;
  --ok: #34d399;
  --warn: #fbbf24;
  --err: #f87171;
  --card: rgba(20, 22, 36, 0.65);
  --card-strong: rgba(28, 30, 48, 0.85);
  --shadow: 0 12px 40px rgba(0, 0, 0, 0.45);
  --radius: 18px;
  --radius-sm: 12px;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
html, body, #root { min-height: 100%; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Segoe UI", "Inter", Roboto, "Helvetica Neue", Arial, sans-serif;
  color: var(--text);
  background: radial-gradient(ellipse at top, #14142b 0%, var(--bg-0) 60%) fixed, var(--bg-0);
  -webkit-font-smoothing: antialiased;
  -webkit-tap-highlight-color: transparent;
  overflow-x: hidden;
}
button { font-family: inherit; }
input, select, textarea { font-family: inherit; color: inherit; }
a { color: var(--accent-2); text-decoration: none; }

.bg-orbs { position: fixed; inset: 0; z-index: -1; pointer-events: none; overflow: hidden; }
.orb { position: absolute; border-radius: 50%; filter: blur(80px); opacity: 0.55; animation: float 22s ease-in-out infinite; }
.orb-a { width: 480px; height: 480px; left: -120px; top: -160px; background: radial-gradient(circle, #7c5cff 0%, transparent 60%); }
.orb-b { width: 420px; height: 420px; right: -140px; top: 80px; background: radial-gradient(circle, #22d3ee 0%, transparent 60%); animation-delay: -7s; }
.orb-c { width: 520px; height: 520px; left: 30%; bottom: -200px; background: radial-gradient(circle, #ff6ec7 0%, transparent 60%); animation-delay: -14s; }
@keyframes float { 0%, 100% { transform: translate(0, 0) scale(1); } 33% { transform: translate(40px, -30px) scale(1.06); } 66% { transform: translate(-30px, 40px) scale(0.96); } }

.screen { max-width: 720px; margin: 0 auto; padding: 18px 16px 32px; animation: fade-in .35s ease; }
@keyframes fade-in { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }

.auth-card { margin-top: 14vh; padding: 28px 22px; text-align: center; }
.brand { display: flex; flex-direction: column; align-items: center; gap: 10px; }
.brand h1 { margin: 4px 0 0; font-size: 26px; letter-spacing: -.01em; }
.logo { display: flex; }
.auth-status { display: flex; align-items: center; gap: 10px; justify-content: center; color: var(--text-dim); margin: 18px 0 8px; }

.card { background: var(--card); border: 1px solid var(--line); border-radius: var(--radius); padding: 18px; margin-bottom: 14px; backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px); box-shadow: var(--shadow); }
.card-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
.card-head h2, .card-head h3 { margin: 0; font-size: 16px; letter-spacing: -.005em; }
.muted { color: var(--text-dim); font-size: 12.5px; }

.topbar { display: flex; align-items: center; justify-content: space-between; padding: 6px 2px 16px; }
.me { display: flex; align-items: center; gap: 12px; }
.avatar { width: 44px; height: 44px; border-radius: 50%; background: linear-gradient(135deg, var(--accent), var(--accent-2)); display: flex; align-items: center; justify-content: center; font-weight: 700; color: white; font-size: 17px; box-shadow: 0 6px 18px rgba(124, 92, 255, .35); }
.me-text .me-name { font-weight: 600; font-size: 15px; }
.me-text .me-sub { color: var(--text-dim); font-size: 12px; }
.icon-btn { width: 38px; height: 38px; border-radius: 12px; background: var(--card); color: var(--text); border: 1px solid var(--line); display: flex; align-items: center; justify-content: center; cursor: pointer; transition: transform .15s ease, background .2s ease; }
.icon-btn:hover { background: var(--card-strong); }
.icon-btn:active { transform: scale(.96); }

.stat-strip { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 14px; }
.stat { background: var(--card); border: 1px solid var(--line); border-radius: var(--radius-sm); padding: 12px; text-align: center; backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); }
.stat-label { color: var(--text-dim); font-size: 11px; text-transform: uppercase; letter-spacing: .06em; }
.stat-value { font-size: 22px; font-weight: 700; margin-top: 4px; background: linear-gradient(135deg, #fff, #c5c8ff); -webkit-background-clip: text; background-clip: text; color: transparent; }

.tabs { display: flex; gap: 6px; padding: 4px; margin: 4px 0 14px; background: rgba(255, 255, 255, .03); border: 1px solid var(--line); border-radius: 14px; overflow-x: auto; -webkit-overflow-scrolling: touch; }
.tab { flex: 1; min-width: max-content; padding: 9px 14px; border-radius: 10px; background: transparent; color: var(--text-dim); border: 0; cursor: pointer; font-weight: 500; font-size: 13.5px; white-space: nowrap; transition: background .2s ease, color .2s ease; }
.tab.active { background: linear-gradient(135deg, rgba(124,92,255,.28), rgba(34,211,238,.22)); color: white; box-shadow: 0 4px 14px rgba(124, 92, 255, .25); }
.tab-panel { display: none; }
.tab-panel.active { display: block; animation: fade-in .25s ease; }

.field { display: block; margin-bottom: 12px; }
.field-label { display: block; font-size: 12px; color: var(--text-dim); margin-bottom: 6px; }
.input, .select, .textarea { width: 100%; background: rgba(0, 0, 0, .28); border: 1px solid var(--line); border-radius: 10px; padding: 11px 12px; font-size: 14px; color: var(--text); transition: border-color .2s ease, background .2s ease; outline: none; }
.input:focus, .select:focus, .textarea:focus { border-color: var(--accent); background: rgba(0, 0, 0, .38); }
.textarea { resize: vertical; min-height: 90px; font-family: "SF Mono", Menlo, Consolas, monospace; }
.select { appearance: none; -webkit-appearance: none; background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%238b91a3' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><polyline points='6 9 12 15 18 9'/></svg>"); background-repeat: no-repeat; background-position: right 12px center; background-size: 16px; padding-right: 36px; }
.row-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }

.btn { border: 0; border-radius: 12px; padding: 12px 16px; font-size: 14px; font-weight: 600; cursor: pointer; transition: transform .12s ease, box-shadow .2s ease, opacity .2s ease; }
.btn:active { transform: scale(.97); }
.btn:disabled { opacity: .55; cursor: not-allowed; }
.btn.primary { color: white; background: linear-gradient(135deg, #7c5cff, #22d3ee); box-shadow: 0 10px 30px rgba(124, 92, 255, .35); }
.btn.primary:hover { box-shadow: 0 14px 38px rgba(124, 92, 255, .5); }
.btn.ghost { background: rgba(255, 255, 255, .06); color: var(--text); border: 1px solid var(--line); }
.btn.danger { background: linear-gradient(135deg, #f87171, #fb923c); color: white; box-shadow: 0 8px 24px rgba(248, 113, 113, .35); }
.actions { display: flex; gap: 10px; margin-top: 8px; }
.actions .btn { flex: 1; }

.pill { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 999px; font-size: 11.5px; font-weight: 600; background: rgba(255, 255, 255, .06); color: var(--text-dim); border: 1px solid var(--line); }
.pill.ok    { background: rgba(52, 211, 153, .15); color: #6ee7b7; border-color: rgba(52, 211, 153, .3); }
.pill.run   { background: rgba(124, 92, 255, .18); color: #c4b5fd; border-color: rgba(124, 92, 255, .35); }
.pill.warn  { background: rgba(251, 191, 36, .15); color: #fde68a; border-color: rgba(251, 191, 36, .3); }
.pill.err   { background: rgba(248, 113, 113, .15); color: #fca5a5; border-color: rgba(248, 113, 113, .3); }
.pill.dim   { background: rgba(255, 255, 255, .05); color: var(--text-dim); }

.emoji-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 6px; }
.emoji-cell { background: rgba(255, 255, 255, .04); border: 1px solid var(--line); border-radius: 10px; padding: 9px 0; font-size: 22px; text-align: center; cursor: pointer; user-select: none; transition: transform .12s ease, border-color .2s ease, background .2s ease; }
.emoji-cell:hover { background: rgba(255, 255, 255, .08); }
.emoji-cell:active { transform: scale(.95); }
.emoji-cell.active { border-color: var(--accent); background: rgba(124, 92, 255, .22); box-shadow: 0 0 0 3px rgba(124, 92, 255, .15); }

.grid-cards { display: grid; gap: 10px; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); }
.account-card { background: rgba(0, 0, 0, .28); border: 1px solid var(--line); border-radius: 12px; padding: 14px; display: flex; flex-direction: column; gap: 8px; }
.account-card .ac-phone { font-weight: 600; font-size: 15px; }
.account-card .ac-meta { color: var(--text-dim); font-size: 12px; }

.tasks-list { display: flex; flex-direction: column; gap: 8px; }
.task-row { display: grid; grid-template-columns: 1fr auto; gap: 10px; align-items: center; background: rgba(0, 0, 0, .22); border: 1px solid var(--line); border-radius: 12px; padding: 12px 14px; }
.task-row .t-id { color: var(--text-dim); font-size: 11.5px; font-family: "SF Mono", Menlo, monospace; }
.task-row .t-type { font-weight: 600; font-size: 14px; }
.task-row .t-meta { color: var(--text-dim); font-size: 12px; margin-top: 2px; }
.task-row .t-right { display: flex; align-items: center; gap: 8px; }
.task-row .t-payload { color: var(--text-mute); font-size: 11.5px; margin-top: 4px; word-break: break-all; }

.logs-list { display: flex; flex-direction: column; gap: 6px; }
.log-row { display: grid; grid-template-columns: auto 1fr auto; gap: 10px; align-items: center; background: rgba(0, 0, 0, .18); border: 1px solid var(--line); border-radius: 10px; padding: 9px 12px; font-size: 13px; }
.log-row .l-icon { width: 26px; height: 26px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 14px; background: rgba(124, 92, 255, .15); }
.log-row .l-icon.like { background: rgba(248, 113, 113, .18); }
.log-row .l-icon.join { background: rgba(34, 211, 238, .18); }
.log-row .l-icon.msg  { background: rgba(124, 92, 255, .18); }
.log-row .l-text { color: var(--text); }
.log-row .l-time { color: var(--text-mute); font-size: 11.5px; }

.chats-list { display: flex; flex-direction: column; gap: 6px; max-height: 260px; overflow: auto; }
.chat-row { display: flex; align-items: center; justify-content: space-between; padding: 8px 10px; background: rgba(0, 0, 0, .18); border: 1px solid var(--line); border-radius: 8px; font-size: 13px; }
.chat-row .ch-name { font-weight: 500; }
.chat-row .ch-type { color: var(--text-mute); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }

.toast { position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%); background: rgba(20, 22, 36, .95); color: white; border: 1px solid var(--line-strong); padding: 10px 14px; border-radius: 12px; font-size: 13.5px; box-shadow: 0 12px 30px rgba(0, 0, 0, .5); z-index: 100; max-width: 90vw; animation: toast-in .25s ease; }
.toast.err  { border-color: rgba(248, 113, 113, .55); }
.toast.ok   { border-color: rgba(52, 211, 153, .55); }
@keyframes toast-in { from { opacity: 0; transform: translate(-50%, 8px); } to { opacity: 1; transform: translate(-50%, 0); } }

.spinner { width: 18px; height: 18px; border-radius: 50%; border: 2px solid rgba(255, 255, 255, .15); border-top-color: var(--accent); animation: spin .9s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

.foot { display: flex; justify-content: space-between; align-items: center; margin-top: 12px; padding: 0 4px; font-size: 11.5px; }

@media (max-width: 480px) {
  .stat-strip { grid-template-columns: repeat(2, 1fr); }
  .emoji-grid { grid-template-columns: repeat(6, 1fr); }
  .row-2 { grid-template-columns: 1fr; }
}
"""

APP_JS = r"""/* Vest Mini App — UI logic. */
(() => {
  'use strict';

  const tg = window.Telegram ? window.Telegram.WebApp : null;
  if (tg) { try { tg.ready(); tg.expand(); } catch (_) {} }

  const state = {
    user: null,
    reactions: [],
    selectedReaction: '👍',
    dashboard: { accounts: [], tasks: [], active_tasks: [], logs: [] },
    cachedChats: [],
    pollTimer: null,
  };

  const $ = (id) => document.getElementById(id);
  const els = {
    login: $('login-screen'), authStatus: $('auth-status'), authRetry: $('auth-retry'),
    dash: $('dashboard'),
    meName: $('me-name'), meSub: $('me-sub'), meAvatar: $('me-avatar'),
    statAccounts: $('stat-accounts'), statActive: $('stat-active'),
    statLikes: $('stat-likes'), statChats: $('stat-chats'),
    pillBot: $('pill-bot-status'),
    fAccount: $('f-account'), fChats: $('f-chats'), fDelay: $('f-delay'), fMode: $('f-mode'),
    emojiGrid: $('emoji-grid'),
    btnStart: $('btn-start'), btnFillChats: $('btn-fill-chats'),
    chatsCacheCard: $('chats-cache-card'), chatsCacheList: $('chats-cache-list'),
    chatsCacheMeta: $('chats-cache-meta'),
    accountsList: $('accounts-list'), accountsMeta: $('accounts-meta'),
    tasksList: $('tasks-list'), logsList: $('logs-list'),
    lastUpdate: $('last-update'), btnRefresh: $('btn-refresh'),
    toast: $('toast'),
  };

  let toastTimer = null;
  function toast(msg, type = '') {
    const t = els.toast;
    t.className = 'toast' + (type ? ' ' + type : '');
    t.textContent = msg; t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 2600);
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      method: opts.method || 'GET',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch (_) {}
    if (!res.ok || (data && data.ok === false)) {
      throw new Error((data && data.error) || ('HTTP ' + res.status));
    }
    return data;
  }

  async function authenticate() {
    const initData = tg ? (tg.initData || '') : '';
    if (!initData) {
      const pasted = window.prompt(
        'Открой мини-приложение из Telegram.\n' +
        'Для отладки в браузере — вставь initData (опционально):'
      );
      if (!pasted) { showAuthError('Не удалось получить initData от Telegram.'); return; }
      try {
        const data = await api('/api/auth', { method: 'POST', body: { initData: pasted } });
        onAuthed(data.user);
      } catch (ex) { showAuthError(ex.message || 'Ошибка входа'); }
      return;
    }
    try {
      const data = await api('/api/auth', { method: 'POST', body: { initData } });
      onAuthed(data.user);
    } catch (ex) { showAuthError(ex.message || 'Ошибка входа'); }
  }

  function showAuthError(msg) {
    els.authStatus.innerHTML =
      '<div style="text-align:center; color: var(--err);">' +
      '<b>Не удалось войти</b><br><span class="muted">' + escapeHtml(msg) + '</span></div>';
    els.authRetry.hidden = false;
  }

  function onAuthed(user) {
    state.user = user;
    els.login.hidden = true; els.dash.hidden = false;
    renderMe(); loadAll(); startPolling();
  }

  function renderMe() {
    const u = state.user || {};
    const name = u.display_name || u.first_name || u.username || 'user';
    els.meName.textContent = name;
    els.meSub.textContent = u.username ? '@' + u.username : ('id ' + u.id);
    els.meAvatar.textContent = (name[0] || 'U').toUpperCase();
  }

  function renderStats() {
    const d = state.dashboard;
    const acc = d.accounts || [];
    const active = d.active_tasks || [];
    const likes = (d.logs || []).filter(l => l.direction === 'liked' &&
      Date.now() - new Date(l.created_at).getTime() < 86400 * 1000).length;
    const chats = (state.cachedChats || []).length;
    els.statAccounts.textContent = acc.length;
    els.statActive.textContent = active.length;
    els.statLikes.textContent = likes;
    els.statChats.textContent = chats;
  }

  function renderAccounts() {
    const acc = state.dashboard.accounts || [];
    els.accountsMeta.textContent = acc.length + ' шт.';
    if (acc.length === 0) {
      const bu = (document.body.getAttribute('data-bot-username') || '').replace(/^@/, '');
      const support = bu ? '@' + escapeHtml(bu) : 'Vest Game Bot';
      els.accountsList.innerHTML =
        '<div class="muted" style="padding: 18px; text-align:center;">' +
        'Аккаунтов пока нет. Добавьте аккаунт в боте <b>' + support + '</b>.' +
        '</div>';
    } else {
      els.accountsList.innerHTML = acc.map(a => {
        const status = a.is_active ? '<span class="pill ok">active</span>' : '<span class="pill dim">off</span>';
        return (
          '<div class="account-card">' +
            '<div class="ac-phone">' + escapeHtml(a.phone || '—') + '</div>' +
            '<div>' + status + '</div>' +
            '<div class="ac-meta">id #' + a.id + ' · ' + fmtDate(a.created_at) + '</div>' +
          '</div>'
        );
      }).join('');
    }
    const opts = acc.length
      ? acc.map(a => '<option value="' + a.id + '">' + escapeHtml(a.phone || ('acc ' + a.id)) + '</option>').join('')
      : '<option value="">— нет аккаунтов —</option>';
    els.fAccount.innerHTML = opts;
  }

  function renderReactions() {
    if (!state.reactions.length) return;
    els.emojiGrid.innerHTML = state.reactions.map(r => {
      const active = r.emoji === state.selectedReaction ? ' active' : '';
      return '<div class="emoji-cell' + active + '" data-emoji="' + r.emoji + '" title="' + escapeHtml(r.name) + '">' + r.emoji + '</div>';
    }).join('');
    Array.from(els.emojiGrid.querySelectorAll('.emoji-cell')).forEach(el => {
      el.addEventListener('click', () => {
        state.selectedReaction = el.getAttribute('data-emoji');
        Array.from(els.emojiGrid.children).forEach(c => c.classList.remove('active'));
        el.classList.add('active');
      });
    });
  }

  function renderStatusPill(status) {
    const map = {
      queued: ['pill run', 'в очереди'], running: ['pill run', 'выполняется'],
      completed: ['pill ok', 'готово'], cancelled: ['pill warn', 'отменено'],
      cancel_requested: ['pill warn', 'отмена…'], stopped: ['pill warn', 'остановлено'],
      failed: ['pill err', 'ошибка'],
    };
    const [cls, txt] = map[status] || ['pill dim', status || '—'];
    return '<span class="' + cls + '">' + txt + '</span>';
  }

  function renderTasks() {
    const tasks = state.dashboard.tasks || [];
    if (tasks.length === 0) {
      els.tasksList.innerHTML = '<div class="muted" style="padding:14px; text-align:center;">Задач пока нет.</div>';
      return;
    }
    els.tasksList.innerHTML = tasks.map(t => {
      const status = renderStatusPill(t.status);
      const isActive = t.status === 'queued' || t.status === 'running';
      const payload = t.payload || {};
      const detail = payload.chat_ids
        ? payload.chat_ids.length + ' чатов · ' + (payload.reaction || '') + ' · ' + (payload.delay || '') + 's'
        : '';
      const result = t.result || null;
      const liked = result && result.liked != null ? result.liked + ' ❤️' : '';
      const err = t.error ? '<div class="t-payload" style="color:var(--err)">' + escapeHtml(t.error) + '</div>' : '';
      return (
        '<div class="task-row">' +
          '<div>' +
            '<div><span class="t-id">#' + t.id + '</span> · <span class="t-type">' + escapeHtml(t.task_type) + '</span></div>' +
            '<div class="t-meta">' + detail + ' ' + liked + '</div>' +
            '<div class="t-payload">' + escapeHtml(JSON.stringify(payload)) + '</div>' +
            err +
          '</div>' +
          '<div class="t-right">' + status +
            (isActive ? '<button class="btn danger" data-stop="' + t.id + '" style="padding:6px 10px;font-size:12px;">Стоп</button>' : '') +
          '</div>' +
        '</div>'
      );
    }).join('');
    Array.from(els.tasksList.querySelectorAll('[data-stop]')).forEach(btn => {
      btn.addEventListener('click', () => stopTask(btn.getAttribute('data-stop')));
    });
  }

  function renderLogs() {
    const logs = state.dashboard.logs || [];
    if (logs.length === 0) {
      els.logsList.innerHTML = '<div class="muted" style="padding:14px; text-align:center;">Лог пуст.</div>';
      return;
    }
    els.logsList.innerHTML = logs.map(l => {
      const iconCls = l.direction === 'liked' ? 'like' : l.direction === 'joined' ? 'join' : 'msg';
      const icon = l.direction === 'liked' ? '❤' : l.direction === 'joined' ? '↗' : '✉';
      const chat = l.chat_name || l.chat_id || '';
      const txt = l.message_text || l.direction;
      return (
        '<div class="log-row">' +
          '<div class="l-icon ' + iconCls + '">' + icon + '</div>' +
          '<div class="l-text"><b>' + escapeHtml(l.phone || '') + '</b> · ' + escapeHtml(chat) + ' · <span class="muted">' + escapeHtml(txt) + '</span></div>' +
          '<div class="l-time">' + fmtTime(l.created_at) + '</div>' +
        '</div>'
      );
    }).join('');
  }

  async function startAutolike() {
    const accountId = els.fAccount.value;
    if (!accountId) { toast('Выбери аккаунт', 'err'); return; }
    const chatsRaw = els.fChats.value.trim();
    if (!chatsRaw) { toast('Укажи хотя бы один чат', 'err'); return; }
    const chatIds = chatsRaw.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
    if (chatIds.length === 0) { toast('Список чатов пуст', 'err'); return; }
    const delay = Math.max(10, parseInt(els.fDelay.value, 10) || 60);
    try {
      const data = await api('/api/autolike/start', {
        method: 'POST',
        body: { account_id: parseInt(accountId, 10), chat_ids: chatIds, reaction: state.selectedReaction, delay },
      });
      toast('Задача #' + data.task_id + ' поставлена в очередь', 'ok');
      loadAll();
    } catch (ex) { toast('Ошибка: ' + ex.message, 'err'); }
  }

  async function stopTask(taskId) {
    try {
      await api('/api/autolike/stop/' + taskId, { method: 'POST' });
      toast('Запрошена остановка #' + taskId, 'ok');
      loadAll();
    } catch (ex) { toast('Ошибка: ' + ex.message, 'err'); }
  }

  async function loadAccountChats() {
    const accountId = els.fAccount.value;
    if (!accountId) { toast('Сначала выбери аккаунт', 'err'); return; }
    try {
      const data = await api('/api/accounts/' + accountId + '/chats');
      const chats = data.chats || [];
      state.cachedChats = chats;
      if (chats.length === 0) {
        els.chatsCacheCard.hidden = false;
        els.chatsCacheMeta.textContent = 'кэш пуст';
        els.chatsCacheList.innerHTML = '<div class="muted" style="padding:10px;">Кэш пуст. В боте нажми «Синхронизировать чаты» для аккаунта.</div>';
      } else {
        els.chatsCacheCard.hidden = false;
        els.chatsCacheMeta.textContent = chats.length + ' шт.';
        els.chatsCacheList.innerHTML = chats.map(c =>
          '<div class="chat-row" data-id="' + escapeHtml(c.chat_id) + '">' +
            '<span class="ch-name">' + escapeHtml(c.name || c.chat_id) + '</span>' +
            '<span class="ch-type">' + escapeHtml(c.chat_type || '') + '</span>' +
          '</div>'
        ).join('');
        Array.from(els.chatsCacheList.querySelectorAll('.chat-row')).forEach(row => {
          row.addEventListener('click', () => {
            const id = row.getAttribute('data-id');
            const cur = els.fChats.value.trim();
            if (cur && !cur.split(/\r?\n/).includes(id)) {
              els.fChats.value = (cur + '\n' + id).trim();
            } else if (!cur) {
              els.fChats.value = id;
            }
            toast('Добавлено: ' + id, 'ok');
          });
        });
      }
      renderStats();
    } catch (ex) { toast('Ошибка: ' + ex.message, 'err'); }
  }

  async function loadAll() {
    try {
      const d = await api('/api/dashboard');
      state.dashboard = d;
      renderAccounts(); renderTasks(); renderLogs(); renderStats();
      els.lastUpdate.textContent = 'обновлено ' + fmtTime(new Date().toISOString());
      els.pillBot.className = 'pill ' + (accHasActive() ? 'ok' : 'dim');
      els.pillBot.textContent = accHasActive() ? 'аккаунты активны' : 'нет активных';
    } catch (ex) {
      if (String(ex.message).includes('unauthorized')) {
        clearInterval(state.pollTimer);
        els.dash.hidden = true; els.login.hidden = false;
        showAuthError('Сессия истекла. Войдите заново.');
      } else {
        toast('Ошибка загрузки: ' + ex.message, 'err');
      }
    }
  }

  function accHasActive() { return (state.dashboard.accounts || []).some(a => a.is_active); }

  async function loadReactions() {
    try {
      const data = await api('/api/reactions');
      state.reactions = data.reactions || [];
      renderReactions();
    } catch (_) {}
  }

  function startPolling() {
    clearInterval(state.pollTimer);
    state.pollTimer = setInterval(() => { if (!document.hidden) loadAll(); }, 4000);
  }

  function setupTabs() {
    Array.from(document.querySelectorAll('.tab')).forEach(tab => {
      tab.addEventListener('click', () => {
        Array.from(document.querySelectorAll('.tab')).forEach(t => t.classList.remove('active'));
        Array.from(document.querySelectorAll('.tab-panel')).forEach(p => p.classList.remove('active'));
        tab.classList.add('active');
        const name = tab.getAttribute('data-tab');
        const panel = document.querySelector('.tab-panel[data-panel="' + name + '"]');
        if (panel) panel.classList.add('active');
      });
    });
  }

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function fmtDate(iso) {
    if (!iso) return '';
    const d = new Date(iso); if (isNaN(d)) return '';
    return d.toLocaleDateString('ru-RU');
  }
  function fmtTime(iso) {
    if (!iso) return '';
    const d = new Date(iso); if (isNaN(d)) return '';
    const today = new Date();
    if (d.toDateString() === today.toDateString()) {
      return d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    }
    return d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' }) + ' ' +
      d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  }

  document.addEventListener('DOMContentLoaded', () => {
    setupTabs();
    els.btnStart.addEventListener('click', startAutolike);
    els.btnFillChats.addEventListener('click', loadAccountChats);
    els.btnRefresh.addEventListener('click', loadAll);
    els.authRetry.addEventListener('click', authenticate);
    loadReactions();
    authenticate();
  });
})();
"""


@app.route("/")
def index():
    return render_template_string(
        INDEX_HTML,
        bot_username=os.getenv("BOT_USERNAME", ""),
    )


@app.route("/static/app.css")
def static_css():
    return Response(APP_CSS, mimetype="text/css")


@app.route("/static/app.js")
def static_js():
    return Response(APP_JS, mimetype="application/javascript")


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
    full = " ".join(p for p in [row.get("first_name"), row.get("last_name")] if p).strip() \
        or row.get("username") or f"user_{user_id}"
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
    with db_cursor() as cur:
        cur.execute(
            "SELECT id, phone, is_active, created_at "
            "FROM accounts WHERE user_id = %s ORDER BY created_at DESC",
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
            ORDER BY id DESC LIMIT 50
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
            ORDER BY l.id DESC LIMIT 40
            """,
            (user_id,),
        )
        logs = [dict(r) for r in cur.fetchall()]

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
# Autolike
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
            INSERT INTO task_queue (user_id, task_type, payload, status)
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
