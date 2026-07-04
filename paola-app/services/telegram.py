"""Push-уведомления в Telegram через Bot API (без библиотеки-фреймворка)."""
import logging

import httpx

import config

logger = logging.getLogger(__name__)


async def send_message(text: str) -> bool:
    """Отправить сообщение Юлии. Возвращает True при успехе."""
    if not (config.TELEGRAM_BOT_TOKEN and config.OWNER_CHAT_ID):
        logger.warning("Telegram push пропущен: TELEGRAM_BOT_TOKEN/OWNER_CHAT_ID не заданы")
        return False
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Telegram ограничивает сообщение 4096 символами
            for chunk_start in range(0, len(text), 4000):
                resp = await client.post(url, json={
                    "chat_id": int(config.OWNER_CHAT_ID),
                    "text": text[chunk_start:chunk_start + 4000],
                })
                resp.raise_for_status()
        return True
    except Exception as e:
        logger.error("Telegram send error: %s", e)
        return False
