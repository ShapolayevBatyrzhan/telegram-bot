# Telegram-бот "Гражданский Альянс города Астаны"

Бот принимает регистрацию через контакт Telegram, собирает заявление на вступление, уведомляет администраторов, позволяет одобрить или отклонить заявку и автоматически формирует PDF-свидетельство о членстве.

## Возможности

- Регистрация через кнопку `Поделиться номером`.
- Запрет повторной регистрации на один и тот же номер.
- Привязка номера телефона к Telegram ID.
- Заявление на вступление: вид деятельности, наименование организации, БИН организации.
- Админ-панель с кнопками `Одобрить` и `Отказать`.
- После одобрения: официальный текст пользователю, кнопки на сайт/соцсети, PDF-свидетельство с логотипом и QR-кодом.

## Локальный запуск

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python bot.py
```

## Переменные окружения

Основные:

```env
BOT_TOKEN=токен_бота_от_BotFather
ADMIN_IDS=123456789,987654321
DATABASE_PATH=alliance_bot.sqlite3
CERTIFICATES_DIR=certificates
```

Официальные данные для сообщения и PDF:

```env
ALLIANCE_NAME=Гражданский Альянс города Астаны
ALLIANCE_BIN=
ALLIANCE_EMAIL=
ALLIANCE_PHONE=
ALLIANCE_ADDRESS=
ALLIANCE_WEBSITE=
ALLIANCE_SOCIAL_URL=
CERTIFICATE_VERIFY_BASE_URL=
CHAIRMAN_NAME=Утеуова Аяжан Дюсембаевна
CHAIRMAN_POSITION=Председатель Гражданского Альянса города Астаны
```

`CERTIFICATE_VERIFY_BASE_URL` используется для QR-кода. Если указать, например, `https://example.kz`, QR будет вести на `https://example.kz/certificate/GAA-2026-0001`.

## Railway

Проект уже содержит:

- `runtime.txt` с Python `3.11`;
- `railway.json` со стартовой командой `python bot.py`.

В Railway нужно добавить переменные из `.env.example` в раздел `Variables`, затем сделать redeploy.
