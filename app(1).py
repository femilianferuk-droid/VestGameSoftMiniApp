"""
Vest Game Soft — Telegram Mini App dashboard.

Read-only admin dashboard for the existing bot database.

Required environment variables:
    DATABASE_URL  PostgreSQL connection string
    BOT_TOKEN     Telegram bot token used to validate Web App initData
    ADMIN_IDS     comma-separated Telegram user IDs allowed to open the dashboard

For Vercel, expose this module as the Python entrypoint (for example
api/app.py) or use the WSGI object named ``app``/``application``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import wraps
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Set
from urllib.parse import parse_qsl

import psycopg2
import psycopg2.extras
from flask import Flask, jsonify, render_template_string, request
from psycopg2 import sql


app = Flask(__name__)
application = app  # WSGI/serverless aliases
app.config["JSON_SORT_KEYS"] = False

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
AUTH_MAX_AGE = int(os.getenv("TELEGRAM_AUTH_MAX_AGE", "86400"))


def _admin_ids() -> Set[int]:
    raw = os.getenv("ADMIN_IDS", "7973988177")
    result: Set[int] = set()
    for value in raw.split(","):
        value = value.strip()
        if value.lstrip("-").isdigit():
            result.add(int(value))
    return result


ADMIN_IDS = _admin_ids()


def _json_value(value: Any) -> Any:
    """Convert PostgreSQL/Python values into JSON-safe values."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    return value


@contextmanager
def readonly_connection() -> Iterator[psycopg2.extensions.connection]:
    """
    Open a connection whose transaction is explicitly read-only.

    Every dashboard query uses this context manager. No write statement is
    present in this application, and PostgreSQL itself rejects writes in the
    transaction as a second safety barrier.
    """
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=8)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
        yield conn
        conn.rollback()
    finally:
        conn.close()


def _catalog(cur: psycopg2.extensions.cursor) -> Dict[str, Set[str]]:
    """Return public table -> columns metadata for schema-tolerant queries."""
    cur.execute(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position
        """
    )
    catalog: Dict[str, Set[str]] = {}
    for table_name, column_name in cur.fetchall():
        catalog.setdefault(table_name, set()).add(column_name)
    return catalog


def _has(catalog: Mapping[str, Set[str]], table: str, *columns: str) -> bool:
    return table in catalog and all(col in catalog[table] for col in columns)


def _count(
    cur: psycopg2.extensions.cursor,
    catalog: Mapping[str, Set[str]],
    table: str,
    where: Optional[str] = None,
    params: Iterable[Any] = (),
) -> int:
    if table not in catalog:
        return 0
    query = sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table))
    if where:
        query += sql.SQL(" WHERE ") + sql.SQL(where)
    cur.execute(query, tuple(params))
    return int(cur.fetchone()[0] or 0)


def _rows(
    cur: psycopg2.extensions.cursor,
    query: sql.Composed,
    params: Iterable[Any] = (),
) -> List[Dict[str, Any]]:
    cur.execute(query, tuple(params))
    return [{k: _json_value(v) for k, v in row.items()} for row in cur.fetchall()]


def _optional_table_rows(
    cur: psycopg2.extensions.cursor,
    catalog: Mapping[str, Set[str]],
    table: str,
    fields: List[str],
    limit: int = 100,
) -> List[Dict[str, Any]]:
    if table not in catalog or not all(field in catalog[table] for field in fields):
        return []
    selected = sql.SQL(", ").join(sql.Identifier(field) for field in fields)
    query = sql.SQL("SELECT {} FROM {} ORDER BY {} DESC LIMIT %s").format(
        selected,
        sql.Identifier(table),
        sql.Identifier("created_at" if "created_at" in catalog[table] else fields[0]),
    )
    return _rows(cur, query, (limit,))


def _verify_telegram_init_data(init_data: str) -> Optional[Dict[str, Any]]:
    """Validate Telegram Web App initData according to Telegram's HMAC scheme."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", "")
        if not received_hash:
            return None
        data_check_string = "\n".join(
            f"{key}={pairs[key]}" for key in sorted(pairs)
        )
        secret_key = hmac.new(
            b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256
        ).digest()
        expected_hash = hmac.new(
            secret_key, data_check_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected_hash, received_hash):
            return None

        auth_date = int(pairs.get("auth_date", "0"))
        if auth_date <= 0 or time.time() - auth_date > AUTH_MAX_AGE:
            return None
        user = json.loads(pairs.get("user", "{}"))
        user_id = int(user.get("id", 0))
        if user_id not in ADMIN_IDS:
            return None
        return {
            "id": user_id,
            "username": user.get("username") or "",
            "first_name": user.get("first_name") or "",
            "last_name": user.get("last_name") or "",
        }
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def current_telegram_user() -> Optional[Dict[str, Any]]:
    init_data = request.headers.get("X-Telegram-Init-Data", "").strip()
    if not init_data:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("tma "):
            init_data = auth[4:].strip()
    return _verify_telegram_init_data(init_data)


def telegram_admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_telegram_user()
        if not user:
            return jsonify(
                {
                    "ok": False,
                    "error": "Telegram authorization required",
                    "hint": "Open this page from the bot's Telegram Web App button.",
                }
            ), 401
        return view(user, *args, **kwargs)

    return wrapped


def _date_series(
    cur: psycopg2.extensions.cursor,
    catalog: Mapping[str, Set[str]],
    table: str,
    days: int,
    extra_where: Optional[str] = None,
) -> Dict[str, int]:
    if not _has(catalog, table, "created_at"):
        return {}
    clauses = ["created_at >= NOW() - %s * INTERVAL '1 day'"]
    params: List[Any] = [days]
    if extra_where:
        clauses.append(extra_where)
    query = sql.SQL(
        "SELECT created_at::date AS day, COUNT(*) AS total "
        "FROM {} WHERE {} GROUP BY 1 ORDER BY 1"
    ).format(sql.Identifier(table), sql.SQL(" AND ").join(map(sql.SQL, clauses)))
    cur.execute(query, tuple(params))
    return {str(row[0]): int(row[1]) for row in cur.fetchall()}


@app.get("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.get("/api/me")
@telegram_admin_required
def api_me(user: Dict[str, Any]):
    return jsonify({"ok": True, "user": user, "read_only": True})


@app.get("/api/overview")
@telegram_admin_required
def api_overview(user: Dict[str, Any]):
    days = max(7, min(int(request.args.get("days", "30")), 90))
    with readonly_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            catalog = _catalog(cur)
            overview = {
                "users": _count(cur, catalog, "users"),
                "accounts": _count(cur, catalog, "accounts"),
                "active_accounts": _count(
                    cur, catalog, "accounts", "is_active = TRUE"
                )
                if _has(catalog, "accounts", "is_active")
                else 0,
                "broadcasts": _count(cur, catalog, "broadcasts"),
                "active_broadcasts": _count(
                    cur, catalog, "broadcasts", "status = %s", ("active",)
                )
                if _has(catalog, "broadcasts", "status")
                else 0,
                "dm_broadcasts": _count(cur, catalog, "dm_broadcasts"),
                "sent_messages": _count(
                    cur, catalog, "account_logs", "direction = %s", ("sent",)
                )
                if _has(catalog, "account_logs", "direction")
                else 0,
                "errors": _count(
                    cur, catalog, "account_logs", "direction = %s", ("error",)
                )
                if _has(catalog, "account_logs", "direction")
                else 0,
                "queued_tasks": _count(
                    cur, catalog, "task_queue", "status = %s", ("queued",)
                )
                if _has(catalog, "task_queue", "status")
                else 0,
                "running_tasks": _count(
                    cur, catalog, "task_queue", "status = %s", ("running",)
                )
                if _has(catalog, "task_queue", "status")
                else 0,
                "failed_tasks": _count(
                    cur, catalog, "task_queue", "status = %s", ("failed",)
                )
                if _has(catalog, "task_queue", "status")
                else 0,
                "active_auto_responders": _count(
                    cur, catalog, "auto_responders", "is_active = TRUE"
                )
                if _has(catalog, "auto_responders", "is_active")
                else 0,
                "active_warming": _count(
                    cur, catalog, "accounts", "warming_enabled = TRUE"
                )
                if _has(catalog, "accounts", "warming_enabled")
                else 0,
            }
            series = {
                "broadcasts": _date_series(cur, catalog, "broadcasts", days),
                "dm_broadcasts": _date_series(cur, catalog, "dm_broadcasts", days),
                "sent_messages": _date_series(
                    cur, catalog, "account_logs", days, "direction = 'sent'"
                ),
                "new_users": _date_series(cur, catalog, "users", days),
            }
            tables = sorted(catalog)
    return jsonify(
        {
            "ok": True,
            "read_only": True,
            "days": days,
            "overview": overview,
            "series": series,
            "tables": tables,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    )


@app.get("/api/broadcasts")
@telegram_admin_required
def api_broadcasts(user: Dict[str, Any]):
    limit = max(1, min(int(request.args.get("limit", "100")), 250))
    results: List[Dict[str, Any]] = []
    with readonly_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            catalog = _catalog(cur)
            if _has(catalog, "broadcasts", "id", "status", "created_at"):
                fields = [
                    "id",
                    "user_id",
                    "account_id",
                    "status",
                    "progress",
                    "total_count",
                    "message_count",
                    "broadcast_type",
                    "created_at",
                    "started_at",
                    "stopped_at",
                ]
                present = [f for f in fields if f in catalog["broadcasts"]]
                selected = sql.SQL(", ").join(
                    sql.Identifier(f) for f in present
                )
                query = sql.SQL(
                    "SELECT {}, 'chat' AS kind FROM {} "
                    "ORDER BY created_at DESC LIMIT %s"
                ).format(selected, sql.Identifier("broadcasts"))
                results.extend(_rows(cur, query, (limit,)))
            if _has(catalog, "dm_broadcasts", "id", "status", "created_at"):
                fields = [
                    "id",
                    "user_id",
                    "account_id",
                    "status",
                    "progress",
                    "total_count",
                    "created_at",
                    "started_at",
                    "stopped_at",
                ]
                present = [f for f in fields if f in catalog["dm_broadcasts"]]
                selected = sql.SQL(", ").join(
                    sql.Identifier(f) for f in present
                )
                query = sql.SQL(
                    "SELECT {}, 'dm' AS kind FROM {} "
                    "ORDER BY created_at DESC LIMIT %s"
                ).format(selected, sql.Identifier("dm_broadcasts"))
                results.extend(_rows(cur, query, (limit,)))
    results.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    return jsonify({"ok": True, "items": results[:limit], "read_only": True})


@app.get("/api/accounts")
@telegram_admin_required
def api_accounts(user: Dict[str, Any]):
    limit = max(1, min(int(request.args.get("limit", "200")), 500))
    items: List[Dict[str, Any]] = []
    with readonly_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            catalog = _catalog(cur)
            if "accounts" in catalog:
                fields = [
                    "id",
                    "user_id",
                    "phone",
                    "is_active",
                    "warming_enabled",
                    "warming_cycles",
                    "warming_last_active",
                    "created_at",
                ]
                present = [f for f in fields if f in catalog["accounts"]]
                selected = sql.SQL(", ").join(
                    sql.Identifier(f) for f in present
                )
                order_col = "created_at" if "created_at" in present else "id"
                query = sql.SQL(
                    "SELECT {} FROM {} ORDER BY {} DESC LIMIT %s"
                ).format(
                    selected,
                    sql.Identifier("accounts"),
                    sql.Identifier(order_col),
                )
                items = _rows(cur, query, (limit,))
    for item in items:
        phone = str(item.get("phone") or "")
        if len(phone) > 7:
            item["phone"] = phone[:3] + "…" + phone[-3:]
    return jsonify({"ok": True, "items": items, "read_only": True})


@app.get("/api/tasks")
@telegram_admin_required
def api_tasks(user: Dict[str, Any]):
    limit = max(1, min(int(request.args.get("limit", "100")), 250))
    items: List[Dict[str, Any]] = []
    with readonly_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            catalog = _catalog(cur)
            fields = [
                f
                for f in [
                    "id",
                    "user_id",
                    "task_type",
                    "status",
                    "entity_id",
                    "error",
                    "created_at",
                    "started_at",
                    "finished_at",
                ]
                if f in catalog.get("task_queue", set())
            ]
            if fields:
                selected = sql.SQL(", ").join(sql.Identifier(f) for f in fields)
                order_col = "created_at" if "created_at" in fields else fields[0]
                query = sql.SQL(
                    "SELECT {} FROM {} ORDER BY {} DESC LIMIT %s"
                ).format(
                    selected,
                    sql.Identifier("task_queue"),
                    sql.Identifier(order_col),
                )
                items = _rows(cur, query, (limit,))
    return jsonify({"ok": True, "items": items, "read_only": True})


@app.get("/api/logs")
@telegram_admin_required
def api_logs(user: Dict[str, Any]):
    limit = max(1, min(int(request.args.get("limit", "100")), 250))
    items: List[Dict[str, Any]] = []
    with readonly_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            catalog = _catalog(cur)
            fields = [
                f
                for f in [
                    "id",
                    "account_id",
                    "chat_name",
                    "chat_id",
                    "direction",
                    "message_text",
                    "created_at",
                ]
                if f in catalog.get("account_logs", set())
            ]
            if fields:
                selected = sql.SQL(", ").join(sql.Identifier(f) for f in fields)
                order_col = "created_at" if "created_at" in fields else fields[0]
                query = sql.SQL(
                    "SELECT {} FROM {} ORDER BY {} DESC LIMIT %s"
                ).format(
                    selected,
                    sql.Identifier("account_logs"),
                    sql.Identifier(order_col),
                )
                items = _rows(cur, query, (limit,))
    return jsonify({"ok": True, "items": items, "read_only": True})


@app.errorhandler(Exception)
def handle_error(error: Exception):
    app.logger.exception("dashboard request failed")
    return jsonify({"ok": False, "error": "Dashboard data is temporarily unavailable"}), 500


DASHBOARD_HTML = r"""
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <title>Vest Game Soft · Dashboard</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root{--bg:#f4f7fc;--card:#fff;--ink:#13233f;--muted:#71809a;--line:#e6edf7;--blue:#2f6df6;--cyan:#27b7f5;--green:#18a879;--orange:#ee9b42;--red:#e45757;--shadow:0 14px 40px rgba(39,76,133,.08)}
    *{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#f8fbff,#edf4ff);color:var(--ink);font:14px/1.45 Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif}
    .shell{max-width:1180px;margin:auto;padding:22px 18px 42px}.top{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:22px}.brand{display:flex;gap:12px;align-items:center}.mark{height:42px;width:42px;border-radius:14px;background:linear-gradient(135deg,var(--blue),var(--cyan));display:grid;place-items:center;color:white;font-size:20px;font-weight:800;box-shadow:0 10px 24px #2f6df633}.brand h1{font-size:20px;margin:0}.brand p{margin:2px 0 0;color:var(--muted);font-size:12px}.pill{border:1px solid #d6e3fa;background:#fff;color:var(--blue);border-radius:999px;padding:7px 11px;font-size:12px;display:flex;align-items:center;gap:6px}.dot{height:7px;width:7px;background:var(--green);border-radius:50%}
    .hero{background:linear-gradient(120deg,#163c88,#2f6df6 60%,#29bdf4);border-radius:24px;padding:24px;color:#fff;box-shadow:0 20px 45px #2f6df633;margin-bottom:18px;display:flex;justify-content:space-between;gap:16px;align-items:flex-end}.hero h2{margin:0 0 5px;font-size:25px}.hero p{margin:0;color:#dce9ff}.refresh{background:#ffffff1f;border:1px solid #ffffff45;color:#fff;border-radius:12px;padding:9px 12px;cursor:pointer}
    .grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px}.card{background:var(--card);border:1px solid #e5ecf7;border-radius:18px;padding:16px;box-shadow:var(--shadow)}.metric{min-height:112px}.metric small{color:var(--muted);display:block;font-size:12px}.metric b{font-size:29px;display:block;margin-top:10px}.metric span{font-size:11px;color:var(--green)}.layout{display:grid;grid-template-columns:1.4fr .9fr;gap:16px}.section-title{display:flex;justify-content:space-between;align-items:center;margin:0 0 14px}.section-title h3{font-size:15px;margin:0}.section-title span{font-size:11px;color:var(--muted)}.chart{height:220px;display:flex;align-items:stretch;gap:5px;padding-top:10px}.bar-wrap{flex:1;display:flex;flex-direction:column;justify-content:flex-end;gap:6px;min-width:0}.bar{min-height:3px;border-radius:7px 7px 2px 2px;background:linear-gradient(180deg,var(--cyan),var(--blue));transition:height .35s}.bar-label{font-size:9px;color:var(--muted);text-align:center;overflow:hidden}.legend{display:flex;gap:14px;color:var(--muted);font-size:11px;margin-top:8px}.legend i{display:inline-block;height:7px;width:7px;border-radius:50%;background:var(--blue);margin-right:4px}.list{display:grid;gap:9px;max-height:300px;overflow:auto}.row{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:11px 0;border-bottom:1px solid var(--line)}.row:last-child{border-bottom:0}.row strong{display:block;font-size:12px}.row small{display:block;color:var(--muted);font-size:11px;margin-top:2px}.badge{border-radius:8px;padding:4px 7px;font-size:10px;background:#edf3ff;color:var(--blue);white-space:nowrap}.badge.ok{color:var(--green);background:#eaf9f3}.badge.warn{color:var(--orange);background:#fff4e7}.badge.err{color:var(--red);background:#ffeded}.tabs{display:flex;gap:8px;overflow:auto;margin:18px 0 12px}.tab{border:0;background:#e8f0ff;color:#54709b;padding:9px 13px;border-radius:10px;cursor:pointer;white-space:nowrap}.tab.active{background:var(--blue);color:#fff}.table-wrap{overflow:auto}.table{width:100%;border-collapse:collapse;font-size:12px}.table th{text-align:left;color:var(--muted);font-weight:500;font-size:11px;padding:10px 8px;border-bottom:1px solid var(--line)}.table td{padding:11px 8px;border-bottom:1px solid var(--line);white-space:nowrap}.empty{text-align:center;color:var(--muted);padding:28px 10px}.notice{display:none;background:#fff1f1;color:#ad3535;border:1px solid #ffd0d0;padding:12px;border-radius:12px;margin-bottom:14px}.foot{color:var(--muted);font-size:11px;text-align:center;margin-top:18px}@media(max-width:820px){.grid{grid-template-columns:repeat(2,1fr)}.layout{grid-template-columns:1fr}.hero{align-items:flex-start;flex-direction:column}}@media(max-width:430px){.shell{padding:15px 12px}.grid{gap:8px}.metric{padding:13px}.metric b{font-size:24px}}
  </style>
</head>
<body>
  <main class="shell">
    <header class="top">
      <div class="brand"><div class="mark">V</div><div><h1>Vest Game Soft</h1><p>Центр статистики рассылок</p></div></div>
      <div class="pill"><i class="dot"></i><span id="userName">Проверка Telegram…</span></div>
    </header>
    <div id="notice" class="notice"></div>
    <section class="hero"><div><h2>Панель управления</h2><p>Только просмотр · данные бота не изменяются</p></div><button class="refresh" onclick="loadAll()">Обновить данные</button></section>
    <section class="grid" id="metrics"></section>
    <section class="layout">
      <div class="card"><div class="section-title"><h3>Динамика за <span id="rangeLabel">30 дней</span></h3><span id="generated"></span></div><div id="chart" class="chart"></div><div class="legend"><span><i></i>события по дням</span></div></div>
      <div class="card"><div class="section-title"><h3>Очередь задач</h3><span>task_queue</span></div><div id="queue" class="list"></div></div>
    </section>
    <nav class="tabs">
      <button class="tab active" data-tab="broadcasts">Рассылки</button><button class="tab" data-tab="accounts">Аккаунты</button><button class="tab" data-tab="tasks">Задачи</button><button class="tab" data-tab="logs">Логи</button>
    </nav>
    <section class="card"><div id="tableContent" class="table-wrap"></div></section>
    <div class="foot">Read-only dashboard · Telegram authorization · PostgreSQL</div>
  </main>
  <script>
    const tg=window.Telegram?.WebApp; tg?.ready(); tg?.expand();
    let data={overview:{},series:{}}, currentTab='broadcasts';
    const headers=()=>({'X-Telegram-Init-Data':tg?.initData||''});
    const esc=v=>String(v??'—').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));
    const fmtDate=v=>v?new Date(v).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';
    async function api(path){const r=await fetch(path,{headers:headers()});const j=await r.json();if(!r.ok||!j.ok)throw Error(j.error||'Ошибка загрузки');return j}
    function showError(e){const n=document.getElementById('notice');n.textContent=e.message||e;n.style.display='block'}
    function metric(label,key,sub,cls=''){const v=data.overview[key]??0;return `<article class="card metric"><small>${label}</small><b>${Number(v).toLocaleString('ru-RU')}</b><span class="${cls}">${sub}</span></article>`}
    function renderMetrics(){document.getElementById('metrics').innerHTML=[metric('Пользователи','users','в базе'),metric('Аккаунты','accounts',`${data.overview.active_accounts||0} активных`),metric('Рассылки','broadcasts',`${data.overview.active_broadcasts||0} активных`),metric('Сообщения отправлены','sent_messages',`${data.overview.errors||0} ошибок`,data.overview.errors?'warn':'')].join('')}
    function renderChart(){const s=data.series||{}, days=[...new Set(Object.values(s).flatMap(x=>Object.keys(x||{})))].sort().slice(-30), vals=days.map(d=>(s.broadcasts?.[d]||0)+(s.dm_broadcasts?.[d]||0)+(s.sent_messages?.[d]||0));const max=Math.max(1,...vals);document.getElementById('chart').innerHTML=days.length?days.map((d,i)=>`<div class="bar-wrap"><div class="bar" style="height:${Math.max(3,vals[i]/max*175)}px" title="${d}: ${vals[i]}"></div><div class="bar-label">${d.slice(5)}</div></div>`).join(''):'<div class="empty">Нет данных за выбранный период</div>'}
    function renderQueue(){const o=data.overview||{};document.getElementById('queue').innerHTML=[['В очереди',o.queued_tasks,''],['Выполняются',o.running_tasks,'warn'],['Ошибки',o.failed_tasks,'err'],['Автоответчики',o.active_auto_responders,'ok'],['Прогрев активен',o.active_warming,'ok']].map(x=>`<div class="row"><div><strong>${x[0]}</strong><small>сейчас</small></div><span class="badge ${x[2]}">${Number(x[1]||0).toLocaleString('ru-RU')}</span></div>`).join('')}
    function statusBadge(v){const s=String(v||'—').toLowerCase();const c=['failed','error','stopped'].includes(s)?'err':['active','running','completed','done','success'].includes(s)?'ok':'warn';return `<span class="badge ${c}">${esc(v)}</span>`}
    function renderTable(tab,items){const c=document.getElementById('tableContent');if(!items?.length){c.innerHTML='<div class="empty">Нет данных</div>';return}let head=[],body=[];if(tab==='broadcasts'){head=['Тип','ID','Статус','Прогресс','Создано'];body=items.map(x=>`<tr><td>${esc(x.kind)}</td><td>#${esc(x.id)}</td><td>${statusBadge(x.status)}</td><td>${esc(x.progress??'—')}${x.total_count?' / '+esc(x.total_count):''}</td><td>${fmtDate(x.created_at)}</td></tr>`)}else if(tab==='accounts'){head=['ID','Пользователь','Телефон','Статус','Прогрев','Создан'];body=items.map(x=>`<tr><td>#${esc(x.id)}</td><td>${esc(x.user_id)}</td><td>${esc(x.phone)}</td><td>${statusBadge(x.is_active?'active':'inactive')}</td><td>${x.warming_enabled?'да':'нет'}</td><td>${fmtDate(x.created_at)}</td></tr>`)}else if(tab==='tasks'){head=['Тип','ID','Статус','Ошибка','Создано'];body=items.map(x=>`<tr><td>${esc(x.task_type)}</td><td>#${esc(x.id)}</td><td>${statusBadge(x.status)}</td><td>${esc(x.error||'—').slice(0,60)}</td><td>${fmtDate(x.created_at)}</td></tr>`)}else{head=['Направление','Чат','Аккаунт','Сообщение','Время'];body=items.map(x=>`<tr><td>${statusBadge(x.direction)}</td><td>${esc(x.chat_name||x.chat_id)}</td><td>#${esc(x.account_id)}</td><td>${esc(x.message_text||'—').slice(0,55)}</td><td>${fmtDate(x.created_at)}</td></tr>`)}c.innerHTML=`<table class="table"><thead><tr>${head.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${body.join('')}</tbody></table>`}
    async function loadTab(tab){currentTab=tab;document.querySelectorAll('.tab').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab));const j=await api('/api/'+tab);renderTable(tab,j.items)}
    async function loadAll(){document.getElementById('notice').style.display='none';try{data=await api('/api/overview?days=30');renderMetrics();renderChart();renderQueue();document.getElementById('generated').textContent=new Date(data.generated_at).toLocaleTimeString('ru-RU');const m=await api('/api/me');document.getElementById('userName').textContent=(m.user.first_name||m.user.username||'Администратор');await loadTab(currentTab)}catch(e){showError(e)}}
    document.querySelectorAll('.tab').forEach(b=>b.addEventListener('click',()=>loadTab(b.dataset.tab).catch(showError)));loadAll();
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
