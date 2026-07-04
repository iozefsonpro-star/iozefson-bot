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

# --- Интеграции ---
GOOGLE_TOKEN_JSON  = os.environ.get("GOOGLE_TOKEN_JSON")           # OAuth Google Calendar
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")          # для push-уведомлений
OWNER_CHAT_ID      = os.environ.get("OWNER_CHAT_ID")
OPENAI_API_KEY     = os.environ.get("OPENAI_API_KEY")              # голосовой ввод (Whisper); опционально

# --- Поведение ---
# Пуши дайджестов в Telegram выключены по умолчанию, чтобы не дублировать Паолу
# в переходный период. Включить: DIGESTS_TO_TELEGRAM=1
DIGESTS_TO_TELEGRAM = os.environ.get("DIGESTS_TO_TELEGRAM", "0") == "1"

MODEL_SMART = "claude-sonnet-5"    # агент, аналитика, совет директоров
MODEL_FAST  = "claude-haiku-4-5"   # переводы, короткие фразы

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
