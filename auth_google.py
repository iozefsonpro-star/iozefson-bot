"""
Получение токена Google Calendar для бота Паолы и Паола App.

ВАЖНО: бот и Паола App используют ОДИН И ТОТ ЖЕ токен — переменную
GOOGLE_TOKEN_JSON. Авторизуешься один раз, полученный JSON кладёшь в
переменные ОБОИХ сервисов Railway (бот + paola-app).

Перед запуском убедись, что в Google Cloud Console → OAuth consent screen
приложение в статусе PRODUCTION (не Testing!). Токен, выданный в Testing,
аннулируется через 7 дней.

Способ авторизации — ручной (без устаревшего OOB, который Google отключил).
Работает прямо в Railway Console.

Шаги:
  1. В Google Cloud Console переведи приложение в Production.
  2. Убедись, что GOOGLE_CLIENT_SECRET_JSON задан в Railway Variables.
  3. Запусти в Railway Console: python3 auth_google.py
  4. Открой показанную ссылку в браузере, войди в Google, разреши доступ.
  5. Google перенаправит на http://localhost/?code=... — страница НЕ
     откроется («не удаётся подключиться»), это нормально.
  6. Скопируй из адресной строки браузера значение code (или весь URL
     целиком) и вставь обратно в консоль.
  7. Скопируй напечатанный JSON и вставь его в Railway как
     GOOGLE_TOKEN_JSON — в ОБА сервиса (бот и paola-app), затем передеплой.
"""

import json
import os
import tempfile
from urllib.parse import urlparse, parse_qs

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
REDIRECT_URI = "http://localhost"

# Читаем client secret из переменной окружения или из файла рядом со скриптом
client_secret_json = os.environ.get("GOOGLE_CLIENT_SECRET_JSON")
if client_secret_json:
    SECRET_FILE = tempfile.mktemp(suffix=".json")
    with open(SECRET_FILE, "w") as f:
        f.write(client_secret_json)
    print("Использую учётные данные из GOOGLE_CLIENT_SECRET_JSON.")
else:
    SECRET_FILE = next(
        (f for f in os.listdir(".") if f.startswith("client_secret") and f.endswith(".json")),
        None,
    )
    if not SECRET_FILE:
        raise FileNotFoundError(
            "Учётные данные не найдены.\n"
            "Задай GOOGLE_CLIENT_SECRET_JSON в Railway Variables."
        )
    print(f"Использую файл учётных данных: {SECRET_FILE}")


def extract_code(raw: str) -> str:
    """Достаёт значение code, даже если вставлен весь URL редиректа."""
    raw = raw.strip()
    if "code=" in raw:
        # вставили целиком http://localhost/?code=...&scope=...
        parsed = urlparse(raw if "://" in raw else "http://localhost/?" + raw)
        code = parse_qs(parsed.query).get("code", [None])[0]
        if code:
            return code
    return raw


flow = InstalledAppFlow.from_client_secrets_file(SECRET_FILE, SCOPES)
flow.redirect_uri = REDIRECT_URI

auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
print("\n🔗 Открой эту ссылку в браузере и войди в Google:\n")
print(auth_url)
print(
    "\nПосле входа Google перенаправит на http://localhost/?code=..."
    "\nСтраница НЕ откроется («не удаётся подключиться») — это нормально."
    "\nСкопируй из адресной строки значение code (или весь URL целиком):"
)
code = extract_code(input("\nВставь code (или URL): "))

flow.fetch_token(code=code)
creds = flow.credentials

if not creds.refresh_token:
    print(
        "\n⚠️  Внимание: refresh_token отсутствует. Токен проживёт недолго."
        "\nОтзови доступ приложению на https://myaccount.google.com/permissions"
        "\nи запусти скрипт заново — Google выдаст refresh_token только при"
        "\nпервой выдаче согласия."
    )

print("\n✅ Авторизация завершена.")
print("\n📋 Скопируй JSON ниже и вставь в Railway как GOOGLE_TOKEN_JSON")
print("   в ОБА сервиса (бот и paola-app), затем передеплой:\n")
print(creds.to_json())
