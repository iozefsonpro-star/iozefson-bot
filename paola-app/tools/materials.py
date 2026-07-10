"""Инструмент агента: сохранение материалов страницами в Notion.

Объёмные результаты (сравнительные таблицы, отчёты, разборы) в чате нечитаемы —
агент сохраняет их страницей внутри «Материалы Паола App» и даёт ссылку.
Markdown конвертируется в блоки Notion: таблицы становятся настоящими
таблицами, заголовки — заголовками, списки — списками.
"""
import re

import config
from services import notion as notion_service

MAX_TABLE_ROWS = 98          # + строка заголовка ≤ лимита Notion в 100 детей блока
MAX_TEXT_CHUNK = 1900        # лимит Notion на rich_text: 2000 символов

_INLINE = re.compile(
    r"\*\*(?P<bold>[^*\n]+)\*\*"
    r"|\[(?P<label>[^\]\n]+)\]\((?P<url>https?://[^\s)]+)\)"
    r"|(?P<bare>https?://[^\s)>\]]+)"
)


def _rt(text: str, bold: bool = False, link: str | None = None) -> dict:
    obj: dict = {"type": "text", "text": {"content": text[:MAX_TEXT_CHUNK]}}
    if link:
        obj["text"]["link"] = {"url": link}
    if bold:
        obj["annotations"] = {"bold": True}
    return obj


def _md_rich(text: str) -> list[dict]:
    """Инлайн-markdown → rich_text: **жирный**, [ссылки](url), голые URL."""
    out: list[dict] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            out.append(_rt(text[pos:m.start()]))
        if m.group("bold") is not None:
            out.append(_rt(m.group("bold"), bold=True))
        elif m.group("label") is not None:
            out.append(_rt(m.group("label"), link=m.group("url")))
        else:
            out.append(_rt(m.group("bare"), link=m.group("bare")))
        pos = m.end()
    if pos < len(text):
        out.append(_rt(text[pos:]))
    return out


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(line: str) -> bool:
    s = line.strip()
    return bool(re.fullmatch(r"\|?[\s:|-]+\|?", s)) and "-" in s


def _paragraph(text: str) -> dict:
    return {"type": "paragraph", "paragraph": {"rich_text": _md_rich(text)}}


def markdown_to_blocks(md: str) -> list[dict]:
    """Markdown → список блоков Notion (заголовки, списки, таблицы, абзацы)."""
    lines = md.replace("\r\n", "\n").split("\n")
    blocks: list[dict] = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue

        # Таблица: строка |...| + разделитель |---|---| дальше
        if s.startswith("|") and i + 1 < len(lines) and _is_separator(lines[i + 1]):
            header = _split_row(s)
            i += 2
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_split_row(lines[i]))
                i += 1
            width = max([len(header)] + [len(r) for r in rows])

            def cells(row: list[str]) -> list[list[dict]]:
                padded = row + [""] * (width - len(row))
                return [_md_rich(c) if c else [] for c in padded]

            children = [{"type": "table_row", "table_row": {"cells": cells(header)}}]
            children += [{"type": "table_row", "table_row": {"cells": cells(r)}}
                         for r in rows[:MAX_TABLE_ROWS]]
            blocks.append({"type": "table", "table": {
                "table_width": width,
                "has_column_header": True,
                "has_row_header": False,
                "children": children,
            }})
            if len(rows) > MAX_TABLE_ROWS:
                blocks.append(_paragraph(
                    f"… таблица усечена: показано {MAX_TABLE_ROWS} строк из {len(rows)}."))
            continue

        if s.startswith("#"):
            level = min(len(s) - len(s.lstrip("#")), 3)
            key = f"heading_{level}"
            blocks.append({"type": key, key: {"rich_text": _md_rich(s.lstrip("#").strip())}})
        elif re.match(r"^[-*•]\s+", s):
            blocks.append({"type": "bulleted_list_item", "bulleted_list_item":
                           {"rich_text": _md_rich(re.sub(r"^[-*•]\s+", "", s))}})
        elif re.match(r"^\d+[.)]\s+", s):
            blocks.append({"type": "numbered_list_item", "numbered_list_item":
                           {"rich_text": _md_rich(re.sub(r"^\d+[.)]\s+", "", s))}})
        elif re.fullmatch(r"-{3,}", s):
            blocks.append({"type": "divider", "divider": {}})
        else:
            blocks.append(_paragraph(s))
        i += 1
    return blocks


async def _save_to_notion(inp: dict) -> str:
    if not config.NOTION_MATERIALS_PAGE_ID:
        return ("Локация для материалов не настроена. Скажи Юлии: нужно создать "
                "в Notion страницу «Материалы Паола App», расшарить её интеграции "
                "бота (⋯ → Connections) и добавить её ID в переменную окружения "
                "NOTION_MATERIALS_PAGE_ID на Railway.")
    title = (inp.get("title") or "").strip() or "Материал Паолы"
    blocks = markdown_to_blocks(inp.get("content") or "")
    if not blocks:
        return "Содержимое пустое — сохранять нечего."
    url = await notion_service.create_material_page(title, blocks)
    return f"Страница «{title}» создана в Notion.\nСсылка: {url}"


TOOLS = [
    {
        "schema": {
            "name": "save_to_notion",
            "description": (
                "Сохранить объёмный структурный материал (сравнительную таблицу, "
                "отчёт, результаты research, разбор бизнес-модели) страницей в Notion "
                "и получить ссылку. Используй всегда, когда результат содержит таблицу "
                "или длиннее ~30 строк: в чате такое нечитаемо. Markdown-таблицы "
                "станут настоящими таблицами Notion. После вызова дай в чате краткий "
                "вывод (3-5 предложений) и ссылку на страницу. Файлы (Excel, PDF) ты "
                "создавать НЕ умеешь — это единственный способ выдать материал."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Название страницы: суть материала + проект/клиент, "
                                       "если чат привязан к проекту. Например «Бизнес-модели "
                                       "джелатерий — Джелатерия LAB».",
                    },
                    "content": {
                        "type": "string",
                        "description": "Полное содержимое в markdown: заголовки #/##/###, "
                                       "списки, **жирный**, [ссылки](url) и таблицы "
                                       "|...|...| с разделителем |---|---|.",
                    },
                },
                "required": ["title", "content"],
            },
        },
        "handler": _save_to_notion,
    },
]
