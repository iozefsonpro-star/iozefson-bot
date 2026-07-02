"""Паола App — персональный ассистент: рутина + аналитика.

Запуск: uvicorn main:app --host 0.0.0.0 --port $PORT
"""
import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
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
    """События календаря для дашборда, сгруппированные по дням."""
    if not config.GOOGLE_TOKEN_JSON:
        return {"configured": False, "days": []}
    from services import gcal
    events = await gcal.get_events(days=min(days, 14))
    by_day: dict[str, list] = {}
    for ev in events:
        by_day.setdefault(ev["day"], []).append(
            {"time": ev["time"], "title": ev["title"],
             "emoji": ev["emoji"], "calendar": ev["calendar"]})
    return {"configured": True,
            "days": [{"date": d, "events": by_day[d]} for d in sorted(by_day)]}


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
    return {
        "overdue": notion.sort_by_priority(overdue),
        "today": today_tasks,
        "other": other,
    }


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


@app.post("/api/projects")
async def post_project(body: ProjectBody):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Название проекта пустое")
    return await storage.create_project(body.name.strip(), body.description.strip())


class ChatCreateBody(BaseModel):
    mode: str
    project_id: str | None = None
    title: str = ""


@app.post("/api/chats")
async def post_chat(body: ChatCreateBody):
    if body.mode not in storage.CHAT_MODES:
        raise HTTPException(status_code=400, detail=f"Неизвестный режим: {body.mode}")
    title = body.title.strip() or storage.CHAT_MODES[body.mode].split(" ", 1)[1]
    return await storage.create_chat(body.mode, body.project_id, title)


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
    try:
        reply = await agent.run_chat(
            chat["mode"], api_messages,
            project_name=chat.get("project_name"),
            project_desc=chat.get("project_description"),
        )
    except Exception as e:
        logger.exception("Agent error")
        raise HTTPException(status_code=502, detail=f"Ошибка агента: {e}")

    await storage.add_message(chat_id, "user", body.message)
    await storage.add_message(chat_id, "assistant", reply)

    # первому сообщению — имя чата (если оно стандартное)
    default_titles = {v.split(" ", 1)[1] for v in storage.CHAT_MODES.values()}
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


@app.get("/")
async def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
