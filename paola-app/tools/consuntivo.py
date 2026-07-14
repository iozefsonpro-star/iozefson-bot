"""Инструменты агента: консунтиво Generali (учёт часов и биллинг).

Поток: собрать встречи из календаря за период → классифицировать (это делает
агент по кодам W#-F#, CI→CoInd, SAL→Track, BBR→Progett, нет кода→Progett) →
записать черновиком (Stato = TO CHECK) → Юлия проверяет/правит в Notion и
подтверждает («подтверждаю июль») → строки становятся Confermato и идут в
фактуру. Источник истины — база Consuntivo Generali; отчёт-страница за период
генерируется из неё как витрина.
"""
import logging

import config
from services import gcal
from services import notion as notion_service
from services import telegram

logger = logging.getLogger(__name__)

FASE_NOME = {
    "Fase 1": "Fase 1: Elenco dei processi AS-IS",
    "Fase 2": "Fase 2: Analisi dei processi",
    "Fase 3": "Fase 3: Trasformazione dei processi",
}
_MESE_IT = ["", "Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
            "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
# Нормализация кодов из названий встреч к типам тарифной карты.
_TIPO_ALIAS = {"CI": "CoInd", "INOPE": "IntOpe", "SAL": "Track", "BBR": "Progett"}


def _mese(date_iso: str) -> str:
    try:
        y, m, _ = date_iso.split("-")
        return f"{_MESE_IT[int(m)]} {y}"
    except (ValueError, IndexError):
        return ""


def _norm_tipo(tipo: str | None) -> str:
    if not tipo:
        return "Progett"
    return _TIPO_ALIAS.get(tipo.strip().upper(), tipo.strip())


def _fmt_hours(h: float) -> str:
    s = f"{h:.4f}".rstrip("0").rstrip(".")
    return s + "ч"


# ---------------------------------------------------------------------------
# 1. Сбор кандидатов из календаря (агент это классифицирует)
# ---------------------------------------------------------------------------

async def _list_generali_events(inp: dict) -> str:
    start, end = inp.get("start"), inp.get("end")
    if not (start and end):
        return "Нужны start и end (даты YYYY-MM-DD) периода."
    events = await gcal.get_range_events(start, end)
    if not events:
        return (f"За период {start}…{end} встреч с временем не найдено "
                "(или календарь недоступен).")
    existing = await notion_service.consuntivo_existing_keys(start, end)
    reparti = await notion_service.get_reparti()
    tariffe = await notion_service.get_tariffe()

    lines = [
        f"Встречи за {start}…{end} для классификации в консунтиво Generali.",
        "",
        "ПРАВИЛА: код в названии W#-F# → Ondata/Fase; тип: CI→CoInd, SAL→Track, "
        "IntOpe→IntOpe, PrMa/PrOnd/PrUpd/Form/Cons/AnDa как есть; BBR → Progett; "
        "нет кода/типа → Progett. «Yulia a Monza» и личное уже отфильтрованы. "
        "Reparto бери из справочника ниже по смыслу названия; если не уверена — "
        "оставь пусто (Юлия заполнит при проверке). Ore = длительность встречи.",
        "",
        "СПРАВОЧНИК РЕПАРТОВ (Reparto — Wave — Fase — ответственный):",
    ]
    for r in reparti:
        lines.append(f"  {r['reparto']} — {r['wave']} — {r['fase']} — {r['responsabile']}")
    lines.append("")
    lines.append("ТАРИФЫ (тип = €/час): " +
                 ", ".join(f"{t}={int(v) if v == int(v) else v}" for t, v in tariffe.items()))
    lines.append("")
    lines.append("ВСТРЕЧИ:")
    for i, ev in enumerate(events, 1):
        key = (ev["date"], ev["start"], ev["title"].strip().lower())
        dup = "  ⚠️УЖЕ В КОНСУНТИВО" if key in existing else ""
        gen = "Generali" if ev["generali"] else "без участников @agmonza"
        lines.append(f"  {i}. {ev['date']} {ev['start']}–{ev['end']} "
                     f"({_fmt_hours(ev['hours'])}) [{gen}] — {ev['title']}{dup}")
    lines.append("")
    lines.append("Дальше классифицируй встречи (кроме помеченных «УЖЕ В КОНСУНТИВО» "
                 "и явно личных без признаков Generali) и вызови add_consuntivo_rows.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 2. Запись классифицированных строк черновиком + отчёт-страница
# ---------------------------------------------------------------------------

def _table_block(headers: list[str], rows: list[list[str]]) -> dict:
    def cell(txt: str) -> list:
        return [{"type": "text", "text": {"content": str(txt)[:2000]}}]
    children = [{"type": "table_row",
                 "table_row": {"cells": [cell(h) for h in headers]}}]
    for row in rows[:99]:  # заголовок + ≤99 строк = лимит Notion в 100 детей
        children.append({"type": "table_row",
                         "table_row": {"cells": [cell(c) for c in row]}})
    return {"type": "table", "table": {
        "table_width": len(headers), "has_column_header": True,
        "has_row_header": False, "children": children}}


async def _build_report(start: str, end: str, label: str) -> str:
    """Отчёт-витрина из черновиков TO CHECK за период. Возвращает URL страницы."""
    rows = await notion_service.query_consuntivo(start, end, statuses=("TO CHECK",))
    rows.sort(key=lambda r: (r["data"], r["inizio"]))
    tot_ore = sum(r["ore"] for r in rows)
    tot_eur = sum(r["prezzo"] for r in rows)
    table_rows = [[r["data"], r["inizio"], r["tipo"], r["reparto"] or "—",
                   _fmt_hours(r["ore"]), f"{r['prezzo']:.2f}", r["attivita"]]
                  for r in rows]
    blocks = [
        {"type": "callout", "callout": {"icon": {"emoji": "🕓"},
         "rich_text": [{"type": "text", "text": {"content":
            f"Черновик на проверку ({label}). Правь часы/тип/Reparto прямо в базе "
            "Consuntivo Generali (вид «Da confermare»), затем скажи Паоле "
            "«подтверждаю» — строки станут Confermato и пойдут в фактуру."}}]}},
        {"type": "heading_2", "heading_2": {"rich_text": [{"type": "text",
            "text": {"content": f"Консунтиво Generali — {label} (черновик)"}}]}},
        _table_block(
            ["Дата", "Начало", "Тип", "Reparto", "Часы", "€", "Активность"],
            table_rows),
        {"type": "paragraph", "paragraph": {"rich_text": [{"type": "text",
            "text": {"content": f"Итого черновика: {len(rows)} встреч, "
                     f"{_fmt_hours(round(tot_ore, 2))}, €{tot_eur:.2f}."}}]}},
    ]
    return await notion_service.create_material_page(
        f"Консунтиво Generali — {label} (черновик)", blocks,
        parent_page_id=config.NOTION_GENERALI_PAGE_ID)


async def _add_consuntivo_rows(inp: dict) -> str:
    rows_in = inp.get("rows") or []
    if not rows_in:
        return "Пустой список строк — нечего заносить."
    dates = [r.get("data") for r in rows_in if r.get("data")]
    if not dates:
        return "У строк нет дат (data) — не могу занести."
    start, end = min(dates), max(dates)
    existing = await notion_service.consuntivo_existing_keys(start, end)
    tariffe = await notion_service.get_tariffe()

    prepared, skipped = [], 0
    for r in rows_in:
        attivita = (r.get("attivita") or "").strip()
        data = r.get("data")
        inizio = (r.get("inizio") or "").strip()
        if not (attivita and data):
            continue
        if (data, inizio, attivita.lower()) in existing:
            skipped += 1
            continue
        tipo = _norm_tipo(r.get("tipo"))
        ore = float(r.get("ore") or 0)
        prezzo = r.get("prezzo")
        if prezzo is None:
            prezzo = round(ore * tariffe.get(tipo, 0.0), 4)
        fase = r.get("fase") or ""
        prepared.append({
            "attivita": attivita, "data": data, "inizio": inizio,
            "fine": (r.get("fine") or "").strip(),
            "tipo": tipo, "reparto": r.get("reparto") or "",
            "ondata": r.get("ondata") or "", "fase": fase,
            "fase_nome": FASE_NOME.get(fase, ""), "mese": _mese(data),
            "ore": ore, "prezzo": prezzo,
        })

    if not prepared:
        return (f"Новых строк нет: все {skipped} уже есть в консунтиво за "
                f"{start}…{end}." if skipped else "Новых строк нет.")

    created = await notion_service.create_consuntivo_rows(prepared)
    tot_ore = sum(r["ore"] for r in prepared)
    tot_eur = sum(r["prezzo"] for r in prepared)
    label = _mese(start) if _mese(start) == _mese(end) else f"{start}…{end}"

    try:
        url = await _build_report(start, end, label)
    except Exception:
        logger.exception("Не удалось собрать отчёт-страницу консунтиво")
        url = ""

    dup_note = f" Пропущено дубликатов: {skipped}." if skipped else ""
    await telegram.send_message(
        f"🕓 Консунтиво Generali ({label}): собрала {created} встреч, "
        f"{_fmt_hours(round(tot_ore, 2))}, €{tot_eur:.2f} — на проверку (TO CHECK)."
        + (f"\n{url}" if url else ""))
    return (f"Занесено черновиком (Stato = TO CHECK): {created} строк за {label}, "
            f"{_fmt_hours(round(tot_ore, 2))}, €{tot_eur:.2f}.{dup_note}"
            + (f" Отчёт-витрина: {url}" if url else "")
            + " Проверь и правь в базе, затем скажи «подтверждаю» для перевода в Confermato.")


# ---------------------------------------------------------------------------
# 3. Подтверждение периода: TO CHECK → Confermato
# ---------------------------------------------------------------------------

async def _confirm_consuntivo(inp: dict) -> str:
    start, end = inp.get("start"), inp.get("end")
    if not (start and end):
        return "Нужны start и end (даты YYYY-MM-DD) периода для подтверждения."
    n = await notion_service.confirm_consuntivo_period(start, end)
    if not n:
        return f"За {start}…{end} черновиков TO CHECK нет — подтверждать нечего."
    return (f"Подтверждено: {n} строк за {start}…{end} переведены TO CHECK → "
            "Confermato. Теперь они учитываются в фактуре.")


# ---------------------------------------------------------------------------
# 4. Сводка часов/сумм за период
# ---------------------------------------------------------------------------

async def _query_consuntivo(inp: dict) -> str:
    start, end = inp.get("start"), inp.get("end")
    if not (start and end):
        return "Нужны start и end (даты YYYY-MM-DD) периода."
    rows = await notion_service.query_consuntivo(start, end)
    if not rows:
        return f"За {start}…{end} строк консунтиво нет."
    billable = [r for r in rows if r["stato"] in notion_service.CONSUNTIVO_BILLABLE]
    pending = [r for r in rows if r["stato"] == "TO CHECK"]

    def agg(items, key):
        d: dict[str, list] = {}
        for r in items:
            k = r[key] or "—"
            d.setdefault(k, [0.0, 0.0])
            d[k][0] += r["ore"]
            d[k][1] += r["prezzo"]
        return d

    b_ore = sum(r["ore"] for r in billable)
    b_eur = sum(r["prezzo"] for r in billable)
    out = [f"Консунтиво Generali за {start}…{end}:",
           f"Подтверждённое (в фактуру): {len(billable)} встреч, "
           f"{_fmt_hours(round(b_ore, 2))}, €{b_eur:.2f}."]
    if pending:
        p_ore = sum(r["ore"] for r in pending)
        out.append(f"На проверке (TO CHECK, НЕ в фактуре): {len(pending)} встреч, "
                   f"{_fmt_hours(round(p_ore, 2))}.")
    if billable:
        out.append("\nПо репартам (подтверждённое):")
        for k, (o, e) in sorted(agg(billable, "reparto").items(),
                                key=lambda x: -x[1][0]):
            out.append(f"  {k}: {_fmt_hours(round(o, 2))}, €{e:.2f}")
        out.append("По типам:")
        for k, (o, e) in sorted(agg(billable, "tipo").items(),
                                key=lambda x: -x[1][0]):
            out.append(f"  {k}: {_fmt_hours(round(o, 2))}, €{e:.2f}")
    return "\n".join(out)


TOOLS = [
    {
        "schema": {
            "name": "list_generali_events",
            "description": (
                "Собрать встречи Generali из календаря за период для консунтиво "
                "(включая прошедшие — timeMin в прошлом). Возвращает список встреч "
                "с длительностью, справочник репартов и тарифы. После этого "
                "классифицируй встречи и вызови add_consuntivo_rows. Даты — YYYY-MM-DD."),
            "input_schema": {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "Начало периода YYYY-MM-DD"},
                    "end":   {"type": "string", "description": "Конец периода YYYY-MM-DD"},
                },
                "required": ["start", "end"],
            },
        },
        "handler": _list_generali_events,
    },
    {
        "schema": {
            "name": "add_consuntivo_rows",
            "description": (
                "Занести классифицированные встречи в базу Consuntivo Generali "
                "черновиком (Stato = TO CHECK). Prezzo считается автоматически "
                "(Ore × тариф по Tipo), если не передан. Дубликаты (та же дата+"
                "начало+название) пропускаются. Собирает отчёт-страницу и шлёт "
                "пуш. НЕ идёт в фактуру до подтверждения Юлией."),
            "input_schema": {
                "type": "object",
                "properties": {
                    "rows": {
                        "type": "array",
                        "description": "Классифицированные встречи",
                        "items": {
                            "type": "object",
                            "properties": {
                                "attivita": {"type": "string", "description": "Название/описание встречи"},
                                "data":     {"type": "string", "description": "Дата YYYY-MM-DD"},
                                "inizio":   {"type": "string", "description": "Начало HH:MM"},
                                "fine":     {"type": "string", "description": "Конец HH:MM"},
                                "ore":      {"type": "number", "description": "Часы (длительность)"},
                                "tipo":     {"type": "string", "description": "CoInd/IntOpe/PrMa/Form/Track/PrOnd/PrUpd/Cons/AnDa/Progett"},
                                "reparto":  {"type": "string", "description": "Департамент из справочника (или пусто)"},
                                "ondata":   {"type": "string", "description": "Ondata 1..4 (или пусто)"},
                                "fase":     {"type": "string", "description": "Fase 1/2/3 (или пусто)"},
                            },
                            "required": ["attivita", "data", "ore", "tipo"],
                        },
                    },
                },
                "required": ["rows"],
            },
        },
        "handler": _add_consuntivo_rows,
    },
    {
        "schema": {
            "name": "confirm_consuntivo",
            "description": (
                "Подтвердить черновики консунтиво за период: перевести строки "
                "Stato TO CHECK → Confermato (после проверки Юлией). Только "
                "подтверждённые идут в фактуру. Даты — YYYY-MM-DD."),
            "input_schema": {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "Начало периода YYYY-MM-DD"},
                    "end":   {"type": "string", "description": "Конец периода YYYY-MM-DD"},
                },
                "required": ["start", "end"],
            },
        },
        "handler": _confirm_consuntivo,
    },
    {
        "schema": {
            "name": "query_consuntivo",
            "description": (
                "Сводка консунтиво Generali за период: подтверждённые часы и суммы "
                "(в фактуру) с разбивкой по репартам и типам, плюс отдельно "
                "черновики на проверке. Даты — YYYY-MM-DD."),
            "input_schema": {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "Начало периода YYYY-MM-DD"},
                    "end":   {"type": "string", "description": "Конец периода YYYY-MM-DD"},
                },
                "required": ["start", "end"],
            },
        },
        "handler": _query_consuntivo,
    },
]
