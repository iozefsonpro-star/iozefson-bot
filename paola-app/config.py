"""Конфигурация приложения. Все настройки — через переменные окружения."""
import os
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --- Обязательные ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
NOTION_TOKEN      = os.environ.get("NOTION_TOKEN")
APP_PASSWORD      = os.environ.get("APP_PASSWORD")          # пароль входа в приложение
SECRET_KEY        = os.environ.get("SECRET_KEY")            # для подписи cookie сессии

# --- Базы Notion (общая память с Second Brain) ---
NOTION_TODOLIST_DB_ID  = os.environ.get("NOTION_TODOLIST_DB_ID")   # Задачи (та же, что у Паолы)
NOTION_HABITS_DB_ID    = os.environ.get("NOTION_HABITS_DB_ID")     # Привычки
NOTION_HABIT_LOG_DB_ID = os.environ.get("NOTION_HABIT_LOG_DB_ID")  # Журнал привычек
NOTION_REMINDERS_DB_ID = os.environ.get("NOTION_REMINDERS_DB_ID")  # Напоминания
NOTION_MATERIALS_PAGE_ID = os.environ.get("NOTION_MATERIALS_PAGE_ID")  # страница «Материалы» для чатов вне проектов
NOTION_CLIENTS_PAGE_ID   = os.environ.get("NOTION_CLIENTS_PAGE_ID")    # страница «Клиенты» — родитель досье проектов

# --- Консунтиво Generali (учёт часов и биллинг по клиенту Generali) ---
# Базы созданы внутри страницы клиента Generali; интеграция наследует доступ
# от «Клиенты Паола App». ID заданы дефолтами — переопределяются env при нужде.
NOTION_CONSUNTIVO_DB_ID = os.environ.get(
    "NOTION_CONSUNTIVO_DB_ID", "4ca156c710bf412eb0b9dc5d12d9338b")  # Consuntivo Generali
NOTION_TARIFFE_DB_ID    = os.environ.get(
    "NOTION_TARIFFE_DB_ID",    "908ab440e06c45f69f720c7f03dda72c")  # Tariffe Generali
NOTION_REPARTI_DB_ID    = os.environ.get(
    "NOTION_REPARTI_DB_ID",    "eea9e62c9dd945179ad672aacb008cfa")  # Reparti Generali
NOTION_GENERALI_PAGE_ID = os.environ.get(
    "NOTION_GENERALI_PAGE_ID", "39dd795881698004bd84cd446d46c783")  # страница клиента Generali (для отчётов)

# --- Интеграции ---
GOOGLE_TOKEN_JSON  = os.environ.get("GOOGLE_TOKEN_JSON")           # OAuth Google Calendar
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")          # для push-уведомлений
OWNER_CHAT_ID      = os.environ.get("OWNER_CHAT_ID")

# --- Поведение ---
# Пуши дайджестов в Telegram выключены по умолчанию, чтобы не дублировать Паолу
# в переходный период. Включить: DIGESTS_TO_TELEGRAM=1
DIGESTS_TO_TELEGRAM = os.environ.get("DIGESTS_TO_TELEGRAM", "0") == "1"

MODEL_SMART = "claude-sonnet-5"    # агент, аналитика, совет директоров, языковой ассистент
MODEL_FAST  = "claude-haiku-4-5"   # инструмент translate (чистый перевод), короткие фразы

ROME_TZ = ZoneInfo("Europe/Rome")

_missing = [name for name, val in {
    "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
    "NOTION_TOKEN": NOTION_TOKEN,
    "APP_PASSWORD": APP_PASSWORD,
    "SECRET_KEY": SECRET_KEY,
}.items() if not val]

def check_required() -> list[str]:
    """Возвращает список отсутствующих обязательных переменных (пустой = всё ок)."""
    return _missing
