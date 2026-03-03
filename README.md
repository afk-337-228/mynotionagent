# Notion Telegram Bot

Open-source Telegram-бот для личного использования: принимает текстовые и голосовые заметки, определяет категорию с помощью ИИ (OpenRouter) и записывает в соответствующие базы Notion. Полностью воспроизводим по README без доступа к ключам автора.

---

## Быстрый старт

1. Клонируй репозиторий и перейди в папку проекта.
2. Создай файл `.env` на основе `.env.example` и заполни переменные (см. разделы ниже).
3. **С Docker:**  
   `docker-compose up -d`  
   (перед этим создай папку `data/` при необходимости: `mkdir data`)
4. **Без Docker:**  
   `pip install -r requirements.txt`  
   (для голосовых сообщений дополнительно: `pip install -r requirements-voice.txt`)  
   `python -m bot.main`  
   (запуск из корня репозитория.)

После первого запуска выполни в Telegram команду `/init`, чтобы бот создал все базы в Notion на указанной родительской странице.

---

## Настройка Notion

1. Открой [Notion Integrations](https://www.notion.so/my-integrations), нажми **New integration**.
2. Укажи имя, выбери workspace. Сохрани **Internal Integration Token** (Secret) — это твой `NOTION_API_KEY`.
3. Создай в Notion обычную страницу, внутри которой будут создаваться базы (категории). Открой эту страницу в браузере. URL будет вида:  
   `https://www.notion.so/My-Page-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`
   Идентификатор страницы — последняя часть без дефисов (32 символа): `xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` — это `NOTION_PARENT_PAGE_ID`.
4. На этой странице в правом верхнем углу нажми **…** → **Connections** → добавь созданную интеграцию. Без этого интеграция не сможет создавать базы и страницы на этой странице.
5. Рекомендация: давай интеграции доступ только к этой одной странице (минимальные права).

---

## Настройка Telegram

1. Напиши [@BotFather](https://t.me/BotFather), создай бота командой `/newbot`, следуй инструкциям. Получи **токен** — это `TELEGRAM_BOT_TOKEN`.
2. Узнать свой **User ID**: напиши боту [@userinfobot](https://t.me/userinfobot) — он вернёт твой ID. Это `TELEGRAM_USER_ID`. Только этот пользователь сможет пользоваться ботом; остальные запросы бот молча игнорирует.

---

## OpenRouter

1. Зарегистрируйся на [OpenRouter](https://openrouter.ai/).
2. В профиле создай API Key — это `OPENROUTER_API_KEY`.
3. В проекте используются бесплатные модели (например `google/gemini-2.5-flash` или `meta-llama/llama-3.1-8b-instruct`), чтобы минимизировать затраты.  
   `OPENROUTER_BASE_URL` по умолчанию: `https://openrouter.ai/api/v1`.

---

## Whisper (голосовые сообщения)

- **Локально / Docker:** используется библиотека `faster-whisper` (модель `tiny` на CPU). Бесплатно. Дополнительно: `pip install -r requirements-voice.txt`; в Docker установлен `ffmpeg`.
- **На Vercel:** на сервере нет `faster-whisper` (слишком тяжёлый бандл). Голос можно включить **без отдельного ключа** — используется твой **OpenRouter** (модель с поддержкой аудио, например `google/gemini-2.5-flash`). Достаточно заданных `OPENROUTER_API_KEY` и `OPENROUTER_BASE_URL`; отдельно ничего настраивать не нужно. Альтернатива: задать **`OPENAI_API_KEY`** — тогда для голоса будет использоваться [OpenAI Whisper API](https://platform.openai.com/docs/guides/speech-to-text) (~$0.006 за минуту). Приоритет: OpenRouter → OpenAI → локальный faster-whisper.

---

## Безопасность

- Бот рассчитан на **одного пользователя**: в `.env` задаётся `TELEGRAM_USER_ID`. Запросы от других пользователей **молча игнорируются** (нет ответа, чтобы не раскрывать факт существования бота).
- Все секреты хранятся только в `.env`; в коде и репозитории нет ключей. В репозитории есть только `.env.example` с описанием переменных.
- Логирование: в логах **не пишется содержимое заметок**, только технические события (получен запрос, запись в БД, ошибки).
- Rate limiting: не более 30 запросов в минуту от одного пользователя.
- Состояние диалога (ожидание выбора категории, последние заметки) хранится в SQLite (`state.db`); при Docker можно задать `STATE_DB_PATH=/app/data/state.db` и монтировать том `./data`.

---

## Деплой на Vercel (хостинг бота)

Бот может работать в режиме **webhook**: Telegram шлёт обновления на твой URL, Vercel обрабатывает их в serverless-функции. Репозиторий уже подготовлен для Vercel. **Голос на Vercel** работает через твой OpenRouter (аудио-модель); если задан `OPENROUTER_API_KEY`, отдельный ключ для голоса не нужен (см. раздел Whisper выше).

### Шаг 1. Подключи репозиторий к Vercel

1. Зайди на [vercel.com](https://vercel.com) и войди (через GitHub).
2. Нажми **Add New…** → **Project**.
3. Импортируй репозиторий **afk-337-228/mynotionagent** (если не виден — нажми **Configure GitHub App** и выдай Vercel доступ к этому репо).
4. **Framework Preset** оставь **Other** (или выбери, если Vercel подставит сам).
5. **Root Directory** — оставь пустым (корень репо).
6. Не включай **Build Command** и **Output Directory** — не нужны для serverless.

### Шаг 2. Переменные окружения в Vercel

В настройках проекта открой **Settings** → **Environment Variables** и добавь **все** переменные из `.env.example` (те же имена, свои значения):

| Переменная | Где взять |
|------------|-----------|
| `TELEGRAM_BOT_TOKEN` | @BotFather |
| `TELEGRAM_USER_ID` | @userinfobot |
| `NOTION_API_KEY` | Notion → Integrations |
| `NOTION_PARENT_PAGE_ID` | URL страницы Notion (часть после последнего `-`) |
| `OPENROUTER_API_KEY` | openrouter.ai |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` |
| `WEBHOOK_SECRET` | Придумай длинную случайную строку (например с [randomkeygen](https://randomkeygen.com/)) — понадобится один раз для шага 4. |

Остальные (`LOG_LEVEL`, `WHISPER_MODE`) можно не трогать или выставить по желанию.

Выбери окружения: **Production**, **Preview**, **Development** — хотя бы Production. Сохрани (**Save**).

### Шаг 3. Деплой

Нажми **Deploy**. Дождись окончания сборки. Вверху появится ссылка вида:

`https://mynotionagent-xxxxx.vercel.app`

(или твой кастомный домен, если настроил.)

### Шаг 4. Включить webhook в Telegram (один раз)

Открой в браузере (подставь свой URL и секрет):

```
https://ТВОЙ_VERCEL_URL/api/set_webhook?secret=ТВОЙ_WEBHOOK_SECRET
```

Пример:

```
https://mynotionagent-xxxxx.vercel.app/api/set_webhook?secret=мой_секрет_из_шага_2
```

Должна открыться страница с текстом вроде: **Webhook set: https://.../api/webhook**.  
После этого Telegram будет слать все обновления на этот URL, бот на Vercel начнёт отвечать.

**Если в логах Vercel видишь 403 на `/api/set_webhook`:** значение `secret=` в URL должно **точно** совпадать с переменной `WEBHOOK_SECRET` в Vercel (без пробелов, одна и та же строка). Опечатка или другой секрет дают 403.

### Шаг 5. Проверка

Напиши боту в Telegram `/start`. Должен прийти ответ с приветствием. Отправь текстовую заметку — она должна попасть в Notion (после первого раза можно отправить `/init`, если базы ещё не созданы).

### Ограничения Vercel

- **Таймаут**: бесплатный план — 10 секунд на один запрос. Текст и классификация обычно укладываются. Голосовые (Whisper) на serverless могут не успевать или долго грузиться при холодном старте; для голоса надёжнее использовать [Docker](#запуск-через-docker) или другой всегда включённый хостинг.
- **Состояние**: SQLite в serverless не сохраняется между вызовами (каждый запрос может быть на новом инстансе). Ожидание выбора категории при «неуверенности» ИИ и список «последних заметок» могут сбрасываться. Для полного поведения с состоянием используй локальный запуск или Docker.

---

## Запуск через Docker

```bash
mkdir -p data
docker-compose up -d
```

Просмотр логов: `docker-compose logs -f bot`

---

## Запуск без Docker

```bash
pip install -r requirements.txt
cp .env.example .env
# отредактируй .env
python -m bot.main
```

Рекомендуется использовать Python 3.11+.

---

## Примеры использования

- **Текст:** отправить сообщение «Купить молоко» — бот определит категорию (например, «Задачи на сегодня/завтра») и сохранит в Notion, ответит подтверждением и ссылкой.
- **Явная категория:**  
  `запиши в спорт: план тренировки на неделю`  
  `в крипту: посмотреть BTC dominance`
- **Перенос:**  
  `перенеси последнюю заметку в разное`  
  `перемести Купить молоко в финансы`
- **Голосовое:** отправить голосовое сообщение — бот распознает текст и обработает как обычную заметку.
- **Команды:** `/start`, `/help`, `/categories`, `/last`, `/init`.

---

## Тесты

Из корня проекта:
```bash
python -m unittest discover -s tests -v
```
Проверяются парсинг команд, нормализация категорий, извлечение URL, санитизация текста для классификатора.

---

## Структура проекта

```
notion-telegram-bot/
├── api/
│   ├── webhook.py       # Vercel: приём обновлений от Telegram
│   └── set_webhook.py   # Vercel: один раз включить webhook
├── bot/
│   ├── __init__.py
│   ├── main.py
│   ├── handlers.py
│   ├── classifier.py
│   ├── notion_client.py
│   ├── voice_handler.py
│   └── state.py
├── .env.example
├── .gitignore
├── vercel.json
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── README.md
└── tests/
    ├── test_classifier.py
    ├── test_handlers_parse.py
    └── test_notion_client.py
```

---

## Лицензия

Open-source. Использование на свой страх и риск; автор не несёт ответственности за потерю данных в Notion или Telegram.
