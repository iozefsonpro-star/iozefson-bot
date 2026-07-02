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
from services import notion

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    level=logging.INFO)
logger = logging.getLogger(__name__)

SESSION_COOKIE = "paola_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 дней

_signer = TimestampSigner(config.SECRET_KEY or "dev-only")

# История чата держится в памяти процесса (один пользователь).
# При рестарте начинается новый диалог — приемлемо для MVP.
_chat_history: list[dict] = []
MAX_HISTORY_MESSAGES = 40


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
    open_paths = ("/api/login", "/static", "/", "/favicon.ico", "/healthz")
    if path.startswith("/api/") and path != "/api/login" and not _is_authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if path not in open_paths and not path.startswith(("/static", "/api")):
        return JSONResponse({"error": "not found"}, status_code=404)
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
# Рутина: дайджест, привычки, напоминания
# ---------------------------------------------------------------------------

@app.get("/api/digest")
async def get_digest(kind: str = "morning"):
    if kind == "evening":
        text = await digests.build_evening_digest()
    else:
        text = await digests.build_morning_digest()
    return {"kind": kind, "text": text}


@app.get("/api/habits")
async def get_habits():
    habits = await notion.get_habits()
    entries = await notion.get_habit_log(days=60)
    today = datetime.now(config.ROME_TZ).date()
    today_iso = today.isoformat()
    done_today = {e["habit"] for e in entries if e["date"] == today_iso and e["done"]}
    return {"date": today_iso, "habits": [
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
    await notion.log_habit(body.habit_name, day, body.done)
    return {"ok": True}


@app.get("/api/reminders")
async def get_reminders():
    return {"reminders": await notion.get_pending_reminders()}


class ReminderBody(BaseModel):
    text: str
    when: str  # ISO YYYY-MM-DDTHH:MM


@app.post("/api/reminders")
async def post_reminder(body: ReminderBody):
    await notion.create_reminder(body.text, body.when)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Аналитика: чат с агентом (ресерч, переводы, совет директоров, анализ)
# ---------------------------------------------------------------------------

class ChatBody(BaseModel):
    message: str


@app.post("/api/chat")
async def chat(body: ChatBody):
    global _chat_history
    _chat_history.append({"role": "user", "content": body.message})
    try:
        reply = await agent.run_agent(_chat_history)
    except Exception as e:
        logger.exception("Agent error")
        _chat_history.pop()  # не оставляем безответный ход в истории
        raise HTTPException(status_code=502, detail=f"Ошибка агента: {e}")
    _chat_history.append({"role": "assistant", "content": reply})
    if len(_chat_history) > MAX_HISTORY_MESSAGES:
        _chat_history = _chat_history[-MAX_HISTORY_MESSAGES:]
        if _chat_history and _chat_history[0]["role"] != "user":
            _chat_history = _chat_history[1:]
    return {"reply": reply}


@app.post("/api/chat/reset")
async def chat_reset():
    _chat_history.clear()
    return {"ok": True}


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
