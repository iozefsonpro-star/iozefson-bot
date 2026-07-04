"""Инструмент агента: переводы RU / IT / EN (быстрая модель)."""
from anthropic import AsyncAnthropic

import config

_client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

LANG_NAMES = {"ru": "русский", "it": "итальянский", "en": "английский"}


async def _translate(inp: dict) -> str:
    target = inp.get("target_lang", "it")
    style = inp.get("style", "business")
    style_note = {
        "business": "Деловой стиль: письмо партнёру или клиенту, вежливо и естественно.",
        "casual":   "Разговорный стиль, живой и естественный.",
        "formal":   "Официальный стиль: документы, регуляторика.",
    }.get(style, "")
    resp = await _client.messages.create(
        model=config.MODEL_FAST,
        max_tokens=2000,
        system=(f"Ты профессиональный переводчик (русский/итальянский/английский) "
                f"для бизнес-консультанта в Италии. Переведи текст на "
                f"{LANG_NAMES.get(target, target)}. {style_note} "
                f"Верни ТОЛЬКО перевод, без пояснений. Если во входном тексте есть "
                f"термин с несколькими вариантами перевода, выбери принятый в деловой "
                f"практике Италии."),
        messages=[{"role": "user", "content": inp["text"]}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


TOOLS = [
    {
        "schema": {
            "name": "translate",
            "description": "Перевести текст между русским, итальянским и английским. "
                           "Используй для писем, сообщений, документов.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "text":        {"type": "string"},
                    "target_lang": {"type": "string", "enum": ["ru", "it", "en"]},
                    "style":       {"type": "string", "enum": ["business", "casual", "formal"]},
                },
                "required": ["text", "target_lang"],
            },
        },
        "handler": _translate,
    },
]
