# Vest Game — Telegram Mini App

Flask Mini App с Telegram-авторизацией, красивым дашбордом и автолайкингом.
Серверная часть бота (`bot.py` из репозитория `Vest-game-soft`) уже
запущена и сама подхватывает задачи из таблицы `task_queue` — это
приложение только пишет туда команды.

## Архитектура

```
┌────────────────────┐  initData (HMAC)   ┌────────────────────┐
│ Telegram Mini App  │ ─────────────────► │  Flask app.py      │
│  (HTML/CSS/JS)     │                    │   /api/*           │
└────────────────────┘                    └─────────┬──────────┘
                                                    │ INSERT task_queue
                                                    ▼
                                          ┌────────────────────┐
                                          │   PostgreSQL       │
                                          │   task_queue       │
                                          └─────────┬──────────┘
                                                    │ SKIP LOCKED
                                                    ▼
                                          ┌────────────────────┐
                                          │   bot.py           │
                                          │   task_queue_worker│
                                          │   → execute_autolike│
                                          └────────────────────┘
```

## Что есть

* Telegram WebApp `initData` валидация (HMAC-SHA256 + проверка `auth_date`).
* Сессия пользователя (Flask cookie).
* Дашборд: профиль, аккаунты, активные задачи, последние 40 логов, статистика.
* Запуск автолайкинга: аккаунт + список чатов + реакция + задержка.
* Остановка задачи: перевод `task_queue.status` → `cancel_requested`,
  бот увидит это в `queue_cancelled()` и остановит цикл.
* Кэш чатов из таблицы `account_chats` с быстрым добавлением в форму.
* Адаптивная тёмная тема, градиенты, glassmorphism, real-time polling.

## Структура

```
vest-mini-app/
├── app.py                  # Flask backend
├── requirements.txt
├── .env.example
├── templates/
│   └── index.html
└── static/
    ├── css/app.css
    └── js/app.js
```

## Запуск

```bash
cd vest-mini-app
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# опционально — переменные окружения
export DATABASE_URL="postgresql://user:pass@host:port/db"
export BOT_TOKEN="123:abc"
export FLASK_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"

python app.py
# → http://0.0.0.0:8080
```

## Подключение Mini App в Telegram

1. Создай бота через `@BotFather`.
2. `/newapp` → выбери бота → укажи URL мини-приложения
   (например `https://your-domain/`).
3. В `app.py` `BOT_TOKEN` должен совпадать с токеном бота —
   иначе HMAC-проверка `initData` не пройдёт.

## Переменные окружения

| Имя                  | Назначение                                 | По умолчанию |
|----------------------|--------------------------------------------|--------------|
| `DATABASE_URL`       | PostgreSQL DSN                             | см. `app.py` |
| `BOT_TOKEN`          | Токен Telegram-бота (для HMAC initData)    | см. `app.py` |
| `FLASK_SECRET_KEY`   | Подпись cookie-сессии                      | random       |
| `INIT_DATA_TTL_SECONDS` | Макс. возраст initData (default 3600)    | 3600         |
| `PORT`               | Порт Flask                                 | 8080         |

## API

| Метод | Путь                                  | Описание                              |
|-------|---------------------------------------|---------------------------------------|
| POST  | `/api/auth`                           | Войти через `initData`                |
| POST  | `/api/logout`                         | Сбросить сессию                       |
| GET   | `/api/me`                             | Текущий пользователь                  |
| GET   | `/api/dashboard`                      | Все данные для дашборда               |
| GET   | `/api/accounts/<id>/chats`            | Кэш чатов аккаунта                    |
| GET   | `/api/reactions`                      | Список доступных реакций              |
| POST  | `/api/autolike/start`                 | Поставить задачу автолайкинга         |
| POST  | `/api/autolike/stop/<task_id>`        | Запросить остановку                   |
| GET   | `/api/tasks/<task_id>`                | Статус одной задачи                   |
| GET   | `/healthz`                            | Liveness + проверка БД                |

## Что Mini App **не делает** сам

* Не отправляет реакции. Этим занимается `bot.py` → `execute_autolike()`.
* Не подключается к Telegram-аккаунтам. Сессии Telethon живут в `bot.py`.
* Не валидирует «живость» аккаунта. Это тоже в зоне ответственности бота.

## Деплой

Для прод-кейса: `gunicorn -w 2 -b 0.0.0.0:8080 app:app` + reverse-proxy с TLS.
initData чувствителен к домену — Telegram подписывает его под `WebAppData`
с токеном бота, так что единственный способ подделать — утечка токена.
