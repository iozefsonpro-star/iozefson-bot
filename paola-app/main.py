"""Паола App — персональный ассистент: рутина + аналитика.

Запуск: uvicorn main:app --host 0.0.0.0 --port $PORT
"""
import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, TimestampSigner
from pydantic import BaseModel

import agent
import config
import digests
import scheduler as scheduler_module
import storage
from services import notion

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    level=logging.INFO)
logger = logging.getLogger(__name__)

SESSION_COOKIE = "paola_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 дней

_signer = TimestampSigner(config.SECRET_KEY or "dev-only")


def _notion_hint(e: Exception) -> str:
    """Человеческое объяснение типовых ошибок Notion."""
    msg = str(e)
    if "Could not find database" in msg or "object_not_found" in msg:
        return ("База не найдена. Проверь ID в переменных окружения и что база "
                "расшарена интеграции: открой базу в Notion → ⋯ → Connections → "
                "добавь интеграцию бота.")
    if "Unauthorized" in msg or "API token is invalid" in msg:
        return "NOTION_TOKEN неверный или не задан."
    if "is not a property that exists" in msg or "validation_error" in msg:
        return f"Схема базы не совпадает с ожидаемой (см. README): {msg[:200]}"
    return msg[:300]


@asynccontextmanager
async def lifespan(app: FastAPI):
    missing = config.check_required()
    if missing:
        logger.error("Не заданы обязательные переменные: %s", ", ".join(missing))
    sched = scheduler_module.start_scheduler()
    yield
    sched.shutdown(wait=False)


app = FastAPI(title="Paola App", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Аутентификация: один пользователь, пароль из env, подписанная cookie
# ---------------------------------------------------------------------------

def _is_authed(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    try:
        _signer.unsign(token, max_age=SESSION_MAX_AGE)
        return True
    except BadSignature:
        return False


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path != "/api/login" and not _is_authed(request):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


class LoginBody(BaseModel):
    password: str


@app.post("/api/login")
async def login(body: LoginBody, response: Response):
    if not config.APP_PASSWORD or body.password != config.APP_PASSWORD:
        raise HTTPException(status_code=401, detail="Неверный пароль")
    token = _signer.sign(b"ok").decode()
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_MAX_AGE,
                        httponly=True, samesite="lax", secure=True)
    return {"ok": True}


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Рутина: сводка, задачи, привычки, напоминания
# ---------------------------------------------------------------------------

@app.get("/api/digest")
async def get_digest(kind: str = "morning"):
    try:
        if kind == "evening":
            text = await digests.build_evening_digest()
        else:
            text = await digests.build_morning_digest()
        return {"kind": kind, "text": text}
    except Exception as e:
        logger.exception("Digest error")
        raise HTTPException(status_code=502, detail=_notion_hint(e))


@app.get("/api/calendar")
async def get_calendar(days: int = 0):
    """События календаря для дашборда, сгруппированные по дням.

    from_now=True — прошедшие встречи сегодняшнего дня скрываются (идущие
    сейчас остаются). Долгие события (курсы) — отдельным списком long.
    """
    if not config.GOOGLE_TOKEN_JSON:
        return {"configured": False, "days": [], "long": []}
    from services import gcal
    data = await gcal.get_events_full(days=min(days, 14), from_now=True)
    by_day: dict[str, list] = {}
    for ev in data["events"]:
        by_day.setdefault(ev["day"], []).append(
            {"time": ev["time"], "title": ev["title"],
             "emoji": ev["emoji"], "calendar": ev["calendar"]})
    return {"configured": True,
            "days": [{"date": d, "events": by_day[d]} for d in sorted(by_day)],
            "long": [{"title": ev["title"], "end_day": ev["end_day"],
                      "label": gcal.format_long(ev)} for ev in data["long"]],
            "hidden_past_today": data["hidden_past_today"]}


@app.get("/api/tasks")
async def get_tasks():
    """Задачи для дашборда: просроченные / сегодня / остальные активные."""
    try:
        active = await notion.get_active_tasks()
        overdue = await notion.get_overdue_tasks()
    except Exception as e:
        logger.exception("Tasks error")
        raise HTTPException(status_code=502, detail=_notion_hint(e))
    today = datetime.now(config.ROME_TZ).date().isoformat()
    overdue_ids = {t["id"] for t in overdue}
    today_tasks, other = [], []
    for t in notion.sort_by_priority(active):
        if t["id"] in overdue_ids:
            continue
        if t.get("deadline", "")[:10] == today:
            today_tasks.append(t)
        else:
            other.append(t)
    # «Остальные»: сначала ближайший дедлайн → дальше, задачи без даты — в конце.
    other.sort(key=lambda t: (0, t["deadline"][:10]) if t.get("deadline") else (1, ""))
    return {
        "overdue": notion.sort_by_priority(overdue),
        "today": today_tasks,
        "other": other,
    }


@app.post("/api/tasks/{task_id}/complete")
async def complete_task(task_id: str):
    try:
        await notion.close_task(task_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=_notion_hint(e))
    return {"ok": True}


class RescheduleBody(BaseModel):
    new_date: str  # YYYY-MM-DD


@app.post("/api/tasks/{task_id}/reschedule")
async def reschedule_task(task_id: str, body: RescheduleBody):
    try:
        await notion.reschedule_task(task_id, body.new_date)
    except Exception as e:
        raise HTTPException(status_code=502, detail=_notion_hint(e))
    return {"ok": True}


@app.get("/api/day-status")
async def get_day_status():
    """Карточка на Доме: обзор дня.

    До полудня — утренний снимок (сколько задач и встреч, фокус, просрочка,
    без даты). После полудня — статус выполнения (запланировано/сделано/
    просрочено), раз задачи на сегодня уже могли закрыться или появиться новые.
    """
    now = datetime.now(config.ROME_TZ)
    today_iso = now.date().isoformat()
    try:
        active, overdue, undated = await asyncio.gather(
            notion.get_active_tasks(), notion.get_overdue_tasks(),
            notion.get_undated_active())
    except Exception as e:
        raise HTTPException(status_code=502, detail=_notion_hint(e))

    overdue_ids = {t["id"] for t in overdue}
    today_tasks = [t for t in active
                   if t.get("deadline", "")[:10] == today_iso and t["id"] not in overdue_ids]

    zones: dict[str, int] = {}
    for t in today_tasks + overdue:
        z = t.get("zone") or "📌 Без зоны"
        zones[z] = zones.get(z, 0) + 1
    focus_zone = max(zones, key=zones.get) if zones else None

    result = {
        "mode": "morning" if now.hour < 12 else "day",
        "total_tasks": len(active),
        "today_total": len(today_tasks),
        "overdue": len(overdue),
        "undated": len(undated),
        "focus_zone": focus_zone,
    }
    if result["mode"] == "morning":
        if config.GOOGLE_TOKEN_JSON:
            from services import gcal
            try:
                result["meetings_today"] = len(await gcal.get_events(days=0))
            except Exception as e:
                logger.warning("day-status: не удалось посчитать встречи: %s", e)
                result["meetings_today"] = 0
        else:
            result["meetings_today"] = 0
    else:
        try:
            done_today = await notion.get_done_between(
                f"{today_iso}T00:00:00", f"{today_iso}T23:59:59")
            result["today_done"] = len(done_today)
        except Exception:
            result["today_done"] = 0
    return result


@app.get("/api/habits")
async def get_habits():
    try:
        habits = await notion.get_habits()
        entries = await notion.get_habit_log(days=60)
    except Exception as e:
        logger.exception("Habits error")
        raise HTTPException(status_code=502, detail=_notion_hint(e))
    today = datetime.now(config.ROME_TZ).date()
    today_iso = today.isoformat()
    done_today = {e["habit"] for e in entries if e["date"] == today_iso and e["done"]}
    return {"date": today_iso, "configured": bool(config.NOTION_HABITS_DB_ID),
            "habits": [
                {
                    "name": h["name"],
                    "goal": h.get("goal", ""),
                    "done_today": h["name"] in done_today,
                    "streak": notion.habit_streak(entries, h["name"], today),
                } for h in habits
            ]}


class HabitLogBody(BaseModel):
    habit_name: str
    done: bool = True
    date: str | None = None


@app.post("/api/habits/log")
async def post_habit_log(body: HabitLogBody):
    day = body.date or datetime.now(config.ROME_TZ).date().isoformat()
    try:
        await notion.log_habit(body.habit_name, day, body.done)
    except Exception as e:
        raise HTTPException(status_code=502, detail=_notion_hint(e))
    return {"ok": True}


@app.get("/api/reminders")
async def get_reminders():
    try:
        return {"configured": bool(config.NOTION_REMINDERS_DB_ID),
                "reminders": await notion.get_pending_reminders()}
    except Exception as e:
        logger.exception("Reminders error")
        raise HTTPException(status_code=502, detail=_notion_hint(e))


class ReminderBody(BaseModel):
    text: str
    when: str  # ISO YYYY-MM-DDTHH:MM


@app.post("/api/reminders")
async def post_reminder(body: ReminderBody):
    if not config.NOTION_REMINDERS_DB_ID:
        raise HTTPException(status_code=400,
                            detail="База «Напоминания» не настроена (NOTION_REMINDERS_DB_ID).")
    try:
        await notion.create_reminder(body.text, body.when)
    except Exception as e:
        raise HTTPException(status_code=502, detail=_notion_hint(e))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Аналитика недели: фокус по зонам, понедельная навигация
# ---------------------------------------------------------------------------

from datetime import timedelta


def _week_bounds(offset: int):
    """Границы недели Пн–Вс; offset 0 — текущая, -1 — прошлая и т.д."""
    today = datetime.now(config.ROME_TZ).date()
    monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
    sunday = monday + timedelta(days=6)
    return monday, sunday


@app.get("/api/analytics")
async def get_analytics(offset: int = 0):
    offset = max(-52, min(0, offset))
    monday, sunday = _week_bounds(offset)
    # Каждый запрос независим: если один падает (например, Notion временно
    # отдал ошибку), остальные блоки аналитики всё равно отрисуются.
    labels = ["done", "carry", "habits", "log"]
    results = await asyncio.gather(
        notion.get_done_between(monday.isoformat(), sunday.isoformat() + "T23:59:59"),
        notion.get_deadline_between(monday.isoformat(), sunday.isoformat()),
        notion.get_habits(),
        notion.get_habit_log(days=abs(offset) * 7 + 14),
        return_exceptions=True,
    )
    for label, r in zip(labels, results):
        if isinstance(r, Exception):
            logger.error("Analytics block '%s' failed: %s", label, r)
    done, carry, habits, log = (r if not isinstance(r, Exception) else [] for r in results)
    if all(isinstance(r, Exception) for r in results):
        raise HTTPException(status_code=502, detail=_notion_hint(results[0]))

    zones: dict[str, list[str]] = {}
    for t in done:
        zones.setdefault(t.get("zone") or "📌 Без зоны", []).append(t["title"])
    zone_list = sorted(
        ({"zone": z, "count": len(titles), "titles": titles}
         for z, titles in zones.items()),
        key=lambda x: -x["count"])

    # «Переносится»: незакрытые задачи с дедлайном на этой неделе, сгруппированы по зоне.
    carry_zones: dict[str, list[str]] = {}
    for t in carry:
        carry_zones.setdefault(t.get("zone") or "📌 Без зоны", []).append(t["title"])
    carry_list = sorted(
        ({"zone": z, "titles": titles} for z, titles in carry_zones.items()),
        key=lambda x: -len(x["titles"]))

    week_days = {(monday + timedelta(days=i)).isoformat() for i in range(7)}
    habits_week = []
    for h in habits:
        days_done = sum(1 for e in log
                        if e["habit"] == h["name"] and e["done"] and e["date"] in week_days)
        habits_week.append({"name": h["name"], "days_done": days_done})

    return {
        "offset": offset,
        "week_start": monday.isoformat(),
        "week_end": sunday.isoformat(),
        "total_done": len(done),
        "zones": zone_list,
        "focus_zone": zone_list[0]["zone"] if zone_list else None,
        "carry_over": carry_list,
        "carry_total": len(carry),
        "habits_week": habits_week,
    }


@app.get("/api/plan")
async def get_plan():
    """План следующей недели (Пн–Пт): календарь по дням + задачи по зонам + очередь.

    Аналог воскресной сводки бота — «подготовка к неделе» на вкладке Аналитика.
    """
    today = datetime.now(config.ROME_TZ).date()
    next_mon = today + timedelta(days=(7 - today.weekday()))
    next_fri = next_mon + timedelta(days=4)
    labels = ["deadline_tasks", "undated"]
    results = await asyncio.gather(
        notion.get_deadline_between(next_mon.isoformat(), next_fri.isoformat()),
        notion.get_undated_active(),
        return_exceptions=True,
    )
    for label, r in zip(labels, results):
        if isinstance(r, Exception):
            logger.error("Plan block '%s' failed: %s", label, r)
    deadline_tasks, undated = (r if not isinstance(r, Exception) else [] for r in results)
    if all(isinstance(r, Exception) for r in results):
        raise HTTPException(status_code=502, detail=_notion_hint(results[0]))

    important = [t for t in undated if t.get("priority") == "❗ Важное"]
    queue = [t for t in undated if t.get("priority") != "❗ Важное"][:5]

    week_tasks = deadline_tasks + important
    zones: dict[str, list[str]] = {}
    for t in week_tasks:
        zones.setdefault(t.get("zone") or "📌 Без зоны", []).append(t["title"])
    zone_list = sorted(
        ({"zone": z, "titles": titles} for z, titles in zones.items()),
        key=lambda x: -len(x["titles"]))

    # Календарь Пн–Пт следующей недели.
    days_out = (next_fri - today).days
    cal_days = []
    if config.GOOGLE_TOKEN_JSON:
        from services import gcal
        events = await gcal.get_events(days=days_out)
        week_iso = {(next_mon + timedelta(days=i)).isoformat() for i in range(5)}
        by_day: dict[str, list] = {}
        for ev in events:
            if ev["day"] in week_iso:
                by_day.setdefault(ev["day"], []).append(
                    {"time": ev["time"], "title": ev["title"], "emoji": ev["emoji"]})
        for i in range(5):
            d = (next_mon + timedelta(days=i)).isoformat()
            cal_days.append({"date": d, "events": by_day.get(d, [])})

    return {
        "week_start": next_mon.isoformat(),
        "week_end": next_fri.isoformat(),
        "calendar_configured": bool(config.GOOGLE_TOKEN_JSON),
        "calendar": cal_days,
        "zones": zone_list,
        "focus_zone": zone_list[0]["zone"] if zone_list else None,
        "queue": [t["title"] for t in queue],
        "total_tasks": len(week_tasks),
    }


@app.post("/api/analytics/recommendation")
async def analytics_recommendation(offset: int = 0):
    """Рекомендации Паолы по итогам недели (по запросу, чтобы не жечь токены)."""
    data = await get_analytics(offset=offset)
    try:
        active = await notion.get_active_tasks()
        overdue = await notion.get_overdue_tasks()
    except Exception:
        active, overdue = [], []
    zones_str = "\n".join(f"- {z['zone']}: {z['count']} задач — "
                          + "; ".join(z["titles"][:5]) for z in data["zones"]) or "ничего"
    habits_str = "\n".join(f"- {h['name']}: {h['days_done']}/7 дней"
                           for h in data["habits_week"]) or "нет данных"
    prompt = (
        f"Неделя {data['week_start']}—{data['week_end']}.\n"
        f"Закрыто задач по зонам:\n{zones_str}\n\n"
        f"Привычки за неделю:\n{habits_str}\n\n"
        f"Сейчас активных задач: {len(active)}, из них просрочено: {len(overdue)}.\n\n"
        "Дай короткий разбор эффективности недели: 1) где был фокус и что это значит; "
        "2) какая сфера просела; 3) две конкретные рекомендации на следующую неделю. "
        "До 120 слов, plain text с эмодзи зон, без markdown."
    )
    try:
        resp = await agent.client.messages.create(
            model=config.MODEL_SMART, max_tokens=800,
            system=agent.BASE_IDENTITY, messages=[{"role": "user", "content": prompt}])
        text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Ошибка генерации: {e}")
    return {"recommendation": text}


# ---------------------------------------------------------------------------
# Аналитика: проекты и чаты
# ---------------------------------------------------------------------------

@app.get("/api/overview")
async def get_overview():
    data = await storage.overview()
    data["modes"] = storage.CHAT_MODES
    return data


class ProjectBody(BaseModel):
    name: str
    description: str = ""


async def _ensure_dossier(project_id: str, name: str, description: str) -> str | None:
    """Создать досье клиента в Notion, если настроено; вернуть page_id.

    Ошибка Notion не должна ломать работу с проектом — логируем и живём без
    досье (создастся при следующей попытке).
    """
    if not config.NOTION_CLIENTS_PAGE_ID:
        return None
    try:
        d = await notion.create_dossier_page(name, description)
    except Exception as e:
        logger.error("Не удалось создать досье «%s»: %s", name, _notion_hint(e))
        return None
    await storage.set_project_notion(project_id, d["id"], d["url"])
    return d["id"]


@app.post("/api/projects")
async def post_project(body: ProjectBody):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Название проекта пустое")
    project = await storage.create_project(body.name.strip(), body.description.strip())
    await _ensure_dossier(project["id"], project["name"], project["description"])
    return project


class ChatCreateBody(BaseModel):
    mode: str
    project_id: str | None = None
    title: str = ""


@app.post("/api/chats")
async def post_chat(body: ChatCreateBody):
    if body.mode not in storage.CHAT_MODES:
        raise HTTPException(status_code=400, detail=f"Неизвестный режим: {body.mode}")
    # внутри проекта режим не выбирается — там всегда единый агент проекта
    mode = "assistant" if body.project_id else body.mode
    default = "Рабочий чат" if body.project_id else storage.CHAT_MODES[mode].split(" ", 1)[1]
    title = body.title.strip() or default
    return await storage.create_chat(mode, body.project_id, title)


@app.get("/api/chats/{chat_id}")
async def get_chat(chat_id: str):
    chat = await storage.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Чат не найден")
    chat["messages"] = await storage.get_messages(chat_id)
    return chat


@app.delete("/api/chats/{chat_id}")
async def delete_chat(chat_id: str):
    await storage.delete_chat(chat_id)
    return {"ok": True}


class MessageBody(BaseModel):
    message: str


@app.post("/api/chats/{chat_id}/messages")
async def post_message(chat_id: str, body: MessageBody):
    chat = await storage.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Чат не найден")

    history = await storage.get_messages(chat_id)
    api_messages = ([{"role": m["role"], "content": m["content"]} for m in history]
                    + [{"role": "user", "content": body.message}])

    # досье клиента: у старых проектов его может не быть — досоздаём на лету
    project_page_id = chat.get("project_page_id")
    if chat.get("project_id") and not project_page_id:
        project_page_id = await _ensure_dossier(
            chat["project_id"], chat.get("project_name") or "Клиент",
            chat.get("project_description") or "")

    try:
        reply = await agent.run_chat(
            chat["mode"], api_messages,
            project_name=chat.get("project_name"),
            project_desc=chat.get("project_description"),
            project_page_id=project_page_id,
        )
    except Exception as e:
        logger.exception("Agent error")
        raise HTTPException(status_code=502, detail=f"Ошибка агента: {e}")

    await storage.add_message(chat_id, "user", body.message)
    await storage.add_message(chat_id, "assistant", reply)

    # первому сообщению — имя чата (если оно стандартное)
    default_titles = ({v.split(" ", 1)[1] for v in storage.CHAT_MODES.values()}
                      | {"Рабочий чат"})
    if not history and chat["title"] in default_titles and chat["mode"] != "translator":
        new_title = body.message.strip().replace("\n", " ")[:42]
        if new_title:
            await storage.rename_chat(chat_id, new_title)

    return {"reply": reply}


# ---------------------------------------------------------------------------
# Служебное и статика
# ---------------------------------------------------------------------------

@app.get("/healthz")
async def healthz():
    return {"ok": True, "missing_env": config.check_required()}


def _asset_version() -> str:
    """Хэш статики для cache-busting — iOS PWA (WKWebView) агрессивно кэширует
    app.js/style.css по URL и не подтягивает новые версии после редеплоя,
    даже после перезахода. Версия в query-параметре меняет URL при каждом
    изменении файлов, так что кэш всегда промахивается на новый деплой."""
    h = hashlib.sha1()
    for name in ("static/app.js", "static/style.css"):
        try:
            h.update(Path(name).read_bytes())
        except FileNotFoundError:
            pass
    return h.hexdigest()[:10]


ASSET_VERSION = _asset_version()
_INDEX_HTML = (Path("static/index.html").read_text(encoding="utf-8")
               .replace('href="/static/style.css"', f'href="/static/style.css?v={ASSET_VERSION}"')
               .replace('src="/static/app.js"', f'src="/static/app.js?v={ASSET_VERSION}"'))


@app.get("/")
async def index():
    return Response(content=_INDEX_HTML, media_type="text/html",
                    headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory="static"), name="static")
