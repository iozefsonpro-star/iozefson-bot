"""Планировщик: дайджесты по расписанию и доставка напоминаний.

Пуши уходят в Telegram. Дайджест-пуши по умолчанию выключены
(DIGESTS_TO_TELEGRAM=0), чтобы не дублировать бота Паолу в переходный период.
Напоминания работают всегда.
"""
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import config
import digests
from services import notion, telegram

logger = logging.getLogger(__name__)


async def _push_morning():
    text = await digests.build_morning_digest()
    await telegram.send_message(text)


async def _push_evening():
    text = await digests.build_evening_digest()
    await telegram.send_message(text)


async def _deliver_due_reminders():
    """Раз в минуту: отправить напоминания, чьё время наступило."""
    try:
        pending = await notion.get_pending_reminders()
    except Exception as e:
        logger.error("Reminders poll error: %s", e)
        return
    now = datetime.now(config.ROME_TZ)
    for r in pending:
        try:
            when = datetime.fromisoformat(r["when"])
            if when.tzinfo is None:
                when = when.replace(tzinfo=config.ROME_TZ)
        except ValueError:
            logger.warning("Пропускаю напоминание с нечитаемой датой: %r", r["when"])
            continue
        # окно доставки: время наступило, но не старше 24ч (защита от лавины после простоя)
        if when <= now and now - when < timedelta(hours=24):
            sent = await telegram.send_message(f"⏰ Напоминание: {r['text']}")
            if sent:
                await notion.mark_reminder_sent(r["id"])
        elif now - when >= timedelta(hours=24):
            # протухшее — помечаем, чтобы не висело вечно
            await notion.mark_reminder_sent(r["id"])
            logger.info("Напоминание протухло (>24ч), помечено отправленным: %s", r["text"])


def start_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=config.ROME_TZ)
    scheduler.add_job(_deliver_due_reminders, IntervalTrigger(seconds=60),
                      id="reminders", replace_existing=True)
    if config.DIGESTS_TO_TELEGRAM:
        scheduler.add_job(_push_morning, CronTrigger(hour=8, minute=0),
                          id="morning", replace_existing=True)
        scheduler.add_job(_push_evening, CronTrigger(hour=21, minute=0),
                          id="evening", replace_existing=True)
    scheduler.start()
    logger.info("Scheduler started (digests_to_telegram=%s)", config.DIGESTS_TO_TELEGRAM)
    return scheduler
