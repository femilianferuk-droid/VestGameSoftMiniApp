/* Vest Mini App — UI logic.
 * Talks to /api/* endpoints; relies on Telegram.WebApp for initData.
 */
(() => {
  'use strict';

  const tg = window.Telegram ? window.Telegram.WebApp : null;
  if (tg) {
    try { tg.ready(); tg.expand(); } catch (_) {}
  }

  // ---------- State ----------
  const state = {
    user: null,
    reactions: [],
    selectedReaction: '👍',
    dashboard: { accounts: [], tasks: [], active_tasks: [], logs: [] },
    pollTimer: null,
  };

  // ---------- DOM ----------
  const $ = (id) => document.getElementById(id);
  const els = {
    login: $('login-screen'),
    authStatus: $('auth-status'),
    authRetry: $('auth-retry'),
    dash: $('dashboard'),
    meName: $('me-name'),
    meSub: $('me-sub'),
    meAvatar: $('me-avatar'),
    statAccounts: $('stat-accounts'),
    statActive: $('stat-active'),
    statLikes: $('stat-likes'),
    statChats: $('stat-chats'),
    pillBot: $('pill-bot-status'),
    fAccount: $('f-account'),
    fChats: $('f-chats'),
    fDelay: $('f-delay'),
    fMode: $('f-mode'),
    emojiGrid: $('emoji-grid'),
    btnStart: $('btn-start'),
    btnFillChats: $('btn-fill-chats'),
    chatsCacheCard: $('chats-cache-card'),
    chatsCacheList: $('chats-cache-list'),
    chatsCacheMeta: $('chats-cache-meta'),
    accountsList: $('accounts-list'),
    accountsMeta: $('accounts-meta'),
    tasksList: $('tasks-list'),
    logsList: $('logs-list'),
    lastUpdate: $('last-update'),
    btnRefresh: $('btn-refresh'),
    toast: $('toast'),
  };

  // ---------- Toast ----------
  let toastTimer = null;
  function toast(msg, type = '') {
    const t = els.toast;
    t.className = 'toast' + (type ? ' ' + type : '');
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 2600);
  }

  // ---------- API ----------
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
      const err = (data && data.error) || ('HTTP ' + res.status);
      throw new Error(err);
    }
    return data;
  }

  // ---------- Auth ----------
  async function authenticate() {
    const initData = tg ? (tg.initData || '') : '';
    if (!initData) {
      // Allow opening in a regular browser for development: prompt user to paste.
      const pasted = window.prompt(
        'Открой мини-приложение из Telegram.\n' +
        'Для отладки в браузере — вставь initData (опционально):'
      );
      if (!pasted) {
        showAuthError('Не удалось получить initData от Telegram.');
        return;
      }
      try {
        const data = await api('/api/auth', { method: 'POST', body: { initData: pasted } });
        onAuthed(data.user);
      } catch (ex) {
        showAuthError(ex.message || 'Ошибка входа');
      }
      return;
    }
    try {
      const data = await api('/api/auth', { method: 'POST', body: { initData } });
      onAuthed(data.user);
    } catch (ex) {
      showAuthError(ex.message || 'Ошибка входа');
    }
  }

  function showAuthError(msg) {
    els.authStatus.innerHTML =
      '<div style="text-align:center; color: var(--err);">' +
      '<b>Не удалось войти</b><br><span class="muted">' + escapeHtml(msg) + '</span></div>';
    els.authRetry.hidden = false;
  }

  function onAuthed(user) {
    state.user = user;
    els.login.hidden = true;
    els.dash.hidden = false;
    renderMe();
    loadAll();
    startPolling();
  }

  // ---------- Renderers ----------
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
    // (bot username is read from server-rendered index.html via #bot-username data-attr)

    // Populate account select in autolike form.
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
          '<div class="t-right">' +
            status +
            (isActive
              ? '<button class="btn danger" data-stop="' + t.id + '" style="padding:6px 10px;font-size:12px;">Стоп</button>'
              : '') +
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
      els.tasksList; // noop
      els.logsList.innerHTML = '<div class="muted" style="padding:14px; text-align:center;">Лог пуст.</div>';
      return;
    }
    els.logsList.innerHTML = logs.map(l => {
      const iconCls = l.direction === 'liked' ? 'like'
        : l.direction === 'joined' ? 'join' : 'msg';
      const icon = l.direction === 'liked' ? '❤'
        : l.direction === 'joined' ? '↗' : '✉';
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

  function renderStatusPill(status) {
    const map = {
      queued:           ['pill run',  'в очереди'],
      running:          ['pill run',  'выполняется'],
      completed:        ['pill ok',   'готово'],
      cancelled:        ['pill warn', 'отменено'],
      cancel_requested: ['pill warn', 'отмена…'],
      stopped:          ['pill warn', 'остановлено'],
      failed:           ['pill err',  'ошибка'],
    };
    const [cls, txt] = map[status] || ['pill dim', status || '—'];
    return '<span class="' + cls + '">' + txt + '</span>';
  }

  // ---------- Actions ----------
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
        body: {
          account_id: parseInt(accountId, 10),
          chat_ids: chatIds,
          reaction: state.selectedReaction,
          delay,
        },
      });
      toast('Задача #' + data.task_id + ' поставлена в очередь', 'ok');
      loadAll();
    } catch (ex) {
      toast('Ошибка: ' + ex.message, 'err');
    }
  }

  async function stopTask(taskId) {
    try {
      await api('/api/autolike/stop/' + taskId, { method: 'POST' });
      toast('Запрошена остановка #' + taskId, 'ok');
      loadAll();
    } catch (ex) {
      toast('Ошибка: ' + ex.message, 'err');
    }
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
    } catch (ex) {
      toast('Ошибка: ' + ex.message, 'err');
    }
  }

  // ---------- Loaders ----------
  async function loadAll() {
    try {
      const d = await api('/api/dashboard');
      state.dashboard = d;
      renderAccounts();
      renderTasks();
      renderLogs();
      renderStats();
      els.lastUpdate.textContent = 'обновлено ' + fmtTime(new Date().toISOString());
      // Set bot status pill.
      els.pillBot.className = 'pill ' + (accHasActive() ? 'ok' : 'dim');
      els.pillBot.textContent = accHasActive() ? 'аккаунты активны' : 'нет активных';
    } catch (ex) {
      if (String(ex.message).includes('unauthorized')) {
        clearInterval(state.pollTimer);
        els.dash.hidden = true;
        els.login.hidden = false;
        showAuthError('Сессия истекла. Войдите заново.');
      } else {
        toast('Ошибка загрузки: ' + ex.message, 'err');
      }
    }
  }

  function accHasActive() {
    return (state.dashboard.accounts || []).some(a => a.is_active);
  }

  async function loadReactions() {
    try {
      const data = await api('/api/reactions');
      state.reactions = data.reactions || [];
      renderReactions();
    } catch (_) { /* ignore */ }
  }

  function startPolling() {
    clearInterval(state.pollTimer);
    state.pollTimer = setInterval(() => {
      if (document.hidden) return;
      loadAll();
    }, 4000);
  }

  // ---------- Tabs ----------
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

  // ---------- Helpers ----------
  function escapeHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
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

  // ---------- Boot ----------
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
