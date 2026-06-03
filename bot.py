import os
import io
import logging
import time
import asyncio
from datetime import datetime, timedelta

import anthropic
import httpx
from telegram import Update, Document
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)
from notion_client import Client as NotionClient

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]

AGENTS = {
    "paola": "Паола — Личный ассистент",
    "carlo": "Карло — Контент",
    "boris": "Борис — Аналитик",
    "sandro": "Сандро — Разработчик",
}

AGENTS_PAGE_TITLE = "Агенты"
PROMPT_CACHE_TTL = 3600  # seconds

# ---------------------------------------------------------------------------
# Notion helpers
# ---------------------------------------------------------------------------
notion = NotionClient(auth=NOTION_TOKEN)

# Cache: {agent_key: (prompt_text, fetched_at_timestamp)}
_prompt_cache: dict[str, tuple[str, float]] = {}


def _extract_text_from_blocks(blocks: list) -> str:
    parts = []
    for block in blocks:
        btype = block.get("type")
        content = block.get(btype, {})
        rich = content.get("rich_text", [])
        text = "".join(t.get("plain_text", "") for t in rich)
        if text:
            parts.append(text)
    return "\n".join(parts)


def _find_agents_parent_id() -> str | None:
    results = notion.search(
        query=AGENTS_PAGE_TITLE,
        filter={"property": "object", "value": "page"},
    ).get("results", [])
    for page in results:
        title_list = (
            page.get("properties", {})
            .get("title", {})
            .get("title", [])
        )
        title = "".join(t.get("plain_text", "") for t in title_list)
        if title.strip() == AGENTS_PAGE_TITLE:
            return page["id"]
    return None


def _find_agent_page_id(parent_id: str, agent_title: str) -> str | None:
    children = notion.blocks.children.list(block_id=parent_id).get("results", [])
    for block in children:
        if block.get("type") == "child_page":
            title = block["child_page"].get("title", "")
            if agent_title.lower() in title.lower():
                return block["id"]
    return None


def fetch_agent_prompt(agent_key: str) -> str:
    cached = _prompt_cache.get(agent_key)
    if cached and (time.time() - cached[1]) < PROMPT_CACHE_TTL:
        logger.info("Prompt cache hit for %s", agent_key)
        return cached[0]

    agent_title = AGENTS[agent_key]
    logger.info("Fetching prompt for %s from Notion", agent_title)

    parent_id = _find_agents_parent_id()
    if not parent_id:
        raise RuntimeError(f"Notion page '{AGENTS_PAGE_TITLE}' not found")

    page_id = _find_agent_page_id(parent_id, agent_title)
    if not page_id:
        raise RuntimeError(f"Agent page '{agent_title}' not found inside '{AGENTS_PAGE_TITLE}'")

    blocks = notion.blocks.children.list(block_id=page_id).get("results", [])
    prompt = _extract_text_from_blocks(blocks)
    if not prompt:
        prompt = f"Ты {agent_title}. Отвечай по-русски, профессионально и по делу."

    _prompt_cache[agent_key] = (prompt, time.time())
    return prompt


# ---------------------------------------------------------------------------
# File text extraction
# ---------------------------------------------------------------------------

async def extract_text_from_document(doc: Document, bot) -> str:
    file = await bot.get_file(doc.file_id)
    buf = io.BytesIO()
    await file.download_to_memory(buf)
    buf.seek(0)
    raw = buf.read()
    mime = doc.mime_type or ""

    if mime == "application/pdf" or doc.file_name.endswith(".pdf"):
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(raw))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            return text.strip()
        except Exception as e:
            logger.warning("PDF extraction failed: %s", e)
            return "[Не удалось извлечь текст из PDF]"

    if mime in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    ) or doc.file_name.endswith((".docx", ".doc")):
        try:
            import docx
            document = docx.Document(io.BytesIO(raw))
            text = "\n".join(p.text for p in document.paragraphs)
            return text.strip()
        except Exception as e:
            logger.warning("DOCX extraction failed: %s", e)
            return "[Не удалось извлечь текст из Word-документа]"

    # Fallback: try decode as text
    try:
        return raw.decode("utf-8")
    except Exception:
        return "[Неподдерживаемый формат файла]"


# ---------------------------------------------------------------------------
# Claude API
# ---------------------------------------------------------------------------
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def ask_claude(system_prompt: str, user_message: str) -> str:
    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2048,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def get_active_agent(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("agent", "paola")


def set_active_agent(context: ContextTypes.DEFAULT_TYPE, agent: str) -> None:
    context.user_data["agent"] = agent


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👋 Привет! Я многоагентный бот.\n\n"
        "Доступные агенты:\n"
        "/paola — Паола, личный ассистент (по умолчанию)\n"
        "/carlo — Карло, контент\n"
        "/boris — Борис, аналитик\n"
        "/sandro — Сандро, разработчик\n"
        "/team — режим совета директоров (все агенты)\n\n"
        "Отправь текст или прикрепи PDF/Word файл."
    )
    await update.message.reply_text(text)


async def _switch_agent(agent_key: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    set_active_agent(context, agent_key)
    name = AGENTS[agent_key]
    await update.message.reply_text(f"Активный агент: *{name}*", parse_mode="Markdown")


async def cmd_paola(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _switch_agent("paola", update, context)

async def cmd_carlo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _switch_agent("carlo", update, context)

async def cmd_boris(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _switch_agent("boris", update, context)

async def cmd_sandro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _switch_agent("sandro", update, context)

async def cmd_team(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    set_active_agent(context, "team")
    await update.message.reply_text(
        "🏛 Режим *Совет директоров* активирован.\n"
        "Все агенты ответят по очереди.",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_message = update.message.text or ""
    doc = update.message.document

    # Extract file text if present
    file_text = ""
    if doc:
        await update.message.reply_text("⏳ Читаю файл...")
        try:
            file_text = await extract_text_from_document(doc, context.bot)
        except Exception as e:
            logger.error("File extraction error: %s", e)
            await update.message.reply_text(f"Ошибка при чтении файла: {e}")
            return

    combined = user_message
    if file_text:
        combined = f"{user_message}\n\n[Содержимое файла {doc.file_name}]:\n{file_text}" if user_message else f"[Содержимое файла {doc.file_name}]:\n{file_text}"

    if not combined.strip():
        await update.message.reply_text("Пожалуйста, напиши вопрос или прикрепи файл.")
        return

    agent = get_active_agent(context)

    if agent == "team":
        await _handle_team(update, context, combined)
    else:
        await _handle_single(update, context, agent, combined)


async def _handle_single(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    agent_key: str,
    message: str,
) -> None:
    await update.message.chat.send_action("typing")
    try:
        prompt = fetch_agent_prompt(agent_key)
    except Exception as e:
        logger.error("Notion fetch error for %s: %s", agent_key, e)
        await update.message.reply_text(f"Ошибка при загрузке промпта из Notion: {e}")
        return

    try:
        reply = ask_claude(prompt, message)
    except Exception as e:
        logger.error("Claude API error: %s", e)
        await update.message.reply_text(f"Ошибка Claude API: {e}")
        return

    await update.message.reply_text(reply)


async def _handle_team(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message: str,
) -> None:
    await update.message.reply_text("🏛 Совет директоров начинает работу...\n")

    for agent_key, agent_name in AGENTS.items():
        await update.message.chat.send_action("typing")
        try:
            prompt = fetch_agent_prompt(agent_key)
        except Exception as e:
            logger.error("Notion fetch error for %s: %s", agent_key, e)
            await update.message.reply_text(f"*{agent_name}*: Ошибка загрузки промпта — {e}", parse_mode="Markdown")
            continue

        try:
            reply = ask_claude(prompt, message)
        except Exception as e:
            logger.error("Claude API error for %s: %s", agent_key, e)
            await update.message.reply_text(f"*{agent_name}*: Ошибка Claude API — {e}", parse_mode="Markdown")
            continue

        await update.message.reply_text(f"*{agent_name}*:\n{reply}", parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("paola", cmd_paola))
    app.add_handler(CommandHandler("carlo", cmd_carlo))
    app.add_handler(CommandHandler("boris", cmd_boris))
    app.add_handler(CommandHandler("sandro", cmd_sandro))
    app.add_handler(CommandHandler("team", cmd_team))

    # Handle text messages and documents
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_message))

    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
