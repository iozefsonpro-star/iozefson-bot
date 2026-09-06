"""Планировщик приложения.

Дайджесты и напоминания читаются на дашборде по запросу (GET /api/digest,
/api/reminders) — push-уведомлений в Telegram больше нет (бот Паола выведен
из эксплуатации), поэтому фоновых job'ов пока не осталось. Функция оставлена
как точка расширения: жизненный цикл приложения (main.py) ожидает
AsyncIOScheduler на старте и остановке.
"""
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config

logger = logging.getLogger(__name__)


def start_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=config.ROME_TZ)
    scheduler.start()
    logger.info("Scheduler started (no jobs — push-уведомления отключены)")
    return scheduler
