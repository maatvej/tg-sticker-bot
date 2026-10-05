# Бот-конвертер стикеров Telegram

Присылаете боту стикер — он отвечает файлом:

| Стикер | Формат в Telegram | Что пришлёт бот |
|---|---|---|
| Обычный (статичный) | WEBP | PNG |
| Анимированный | TGS (Lottie) | GIF |
| Видеостикер | WEBM (VP9) | GIF |

Прозрачность сохраняется. Файлы приходят документом: так Telegram не пережимает PNG в JPEG
и не перекодирует GIF в MP4.

## Запуск

1. Создайте бота у [@BotFather](https://t.me/BotFather) командой `/newbot` и скопируйте выданный токен.
2. Вставьте токен в файл `.env` вместо заглушки `PASTE_YOUR_BOT_TOKEN_FROM_BOTFATHER`.
3. Запустите бота:

   ```powershell
   .venv\Scripts\python -m sticker_bot
   ```

Виртуальное окружение `.venv` (Python 3.12) уже создано, зависимости установлены.
Ставить в систему ffmpeg или rlottie не нужно — они входят в pip-пакеты `imageio-ffmpeg` и `rlottie-python`.

### Установка с нуля (например, на сервере)

Windows:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env   # затем впишите токен в .env
.venv\Scripts\python -m sticker_bot
```

Linux / macOS:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env          # затем впишите токен в .env
.venv/bin/python -m sticker_bot
```

## Настройки

Читаются из `.env`, переменные окружения имеют приоритет.

| Переменная | Описание |
|---|---|
| `BOT_TOKEN` | токен от @BotFather, обязательно |
| `MAX_CONCURRENT_CONVERSIONS` | сколько стикеров конвертировать одновременно, по умолчанию `2` |

## Тесты

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

Тесты генерируют стикеры всех трёх типов на лету и прогоняют бота целиком
(апдейт → конвертация → ответ) с подменённым API Telegram, без сети.

## Как это работает

- **WEBP → PNG** — Pillow.
- **TGS → GIF** — кадры рендерит rlottie (тот же движок, что в приложениях Telegram), GIF собирает ffmpeg
  с общей палитрой на всю анимацию, чтобы цвета не мерцали между кадрами.
- **WEBM → GIF** — ffmpeg с декодером `libvpx-vp9`: встроенный декодер VP9 теряет альфа-канал,
  и фон получился бы чёрным.

Ограничения формата GIF: не больше 256 цветов и нет полупрозрачности — пиксели прозрачнее 50%
становятся прозрачными, остальные непрозрачными, поэтому на мягких краях возможна лёгкая «лесенка».
Анимации 60 fps сохраняются в 30 fps: задержка кадра в GIF кратна 10 мс, а задержки короче 20 мс
браузеры и мессенджеры не соблюдают.

В группах бот по умолчанию видит только команды и ответы на свои сообщения (privacy mode),
поэтому пользоваться им удобнее в личных сообщениях.

## Структура

```
sticker_bot/
  __main__.py    точка входа: настройки и запуск long polling
  config.py      настройки из .env
  handlers.py    обработчики сообщений
  converter.py   конвертация WEBP → PNG, TGS/WEBM → GIF
tests/           тесты
```
