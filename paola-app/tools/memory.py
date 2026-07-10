"""Инструмент агента: память клиента — дозапись фактов в досье проекта (Notion)."""
from services import notion as notion_service
from tools import context


async def _update_client_memory(inp: dict) -> str:
    page_id = context.CURRENT_PROJECT_PAGE.get()
    if not page_id:
        return ("Этот чат не привязан к проекту с досье — сохранять факты некуда. "
                "Если информация важная, предложи Юлии создать проект под клиента "
                "(или настроить NOTION_CLIENTS_PAGE_ID, если проекты без досье).")
    facts = [f.strip() for f in (inp.get("facts") or []) if f and f.strip()]
    if not facts:
        return "Пустой список фактов — записывать нечего."
    await notion_service.append_dossier_facts(page_id, facts)
    return f"Записано в досье клиента: {len(facts)} шт."


TOOLS = [
    {
        "schema": {
            "name": "update_client_memory",
            "description": (
                "Записать в досье клиента (страница проекта в Notion) новые "
                "УСТОЙЧИВЫЕ факты, решения и договорённости — они будут видны "
                "во всех чатах проекта, и переспрашивать не придётся. Вызывай "
                "молча и сразу, как только узнала что-то долговременное: цифры "
                "(бюджет, обороты), принятые решения, предпочтения и ограничения "
                "клиента, договорённости, ключевые выводы анализа. НЕ записывай "
                "мимолётное, промежуточные рассуждения и то, что уже есть в досье."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "facts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Факты по одному на элемент, кратко и "
                                       "самодостаточно, например: «Бюджет открытия "
                                       "джелатерии — до $300K»",
                    },
                },
                "required": ["facts"],
            },
        },
        "handler": _update_client_memory,
    },
]
