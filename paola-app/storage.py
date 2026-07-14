"""Хранилище чатов и проектов: SQLite.

На Railway подключить Volume и указать DATA_DIR=/data — тогда истории чатов
переживают редеплой. Без volume база живёт до следующего деплоя.
"""
import asyncio
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

DATA_DIR = os.environ.get("DATA_DIR", "data")
DB_PATH = os.path.join(DATA_DIR, "paola.db")

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

# Режимы чатов; описания используются в UI
CHAT_MODES = {
    "assistant":  "🤖 Ассистент",
    "translator": "🇮🇹 Переводчик",
    "research":   "🔎 Ресерч",
    "board":      "🏛 Совет директоров",
    "business":   "🧩 Бизнес-модель",
}

DEFAULT_CHATS = [
    ("assistant",  "Ассистент"),
    ("translator", "Переводчик"),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chats (
                id TEXT PRIMARY KEY,
                project_id TEXT REFERENCES projects(id),
                mode TEXT NOT NULL,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL REFERENCES chats(id),
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id);
        """)
        # миграция: у старых баз нет колонок досье (страница клиента в Notion)
        for col in ("notion_page_id", "notion_url"):
            try:
                _conn.execute(f"ALTER TABLE projects ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass
        # стартовые чаты при первом запуске
        if _conn.execute("SELECT COUNT(*) FROM chats").fetchone()[0] == 0:
            for mode, title in DEFAULT_CHATS:
                _conn.execute(
                    "INSERT INTO chats (id, project_id, mode, title, created_at) "
                    "VALUES (?, NULL, ?, ?, ?)",
                    (uuid.uuid4().hex, mode, title, _now()))
        _conn.commit()
    return _conn


def _run(fn):
    """Выполнить sync-операцию с БД в thread-пуле, под общим замком."""
    def wrapper(*args, **kwargs):
        with _lock:
            return fn(_get_conn(), *args, **kwargs)
    async def async_call(*args, **kwargs):
        return await asyncio.to_thread(wrapper, *args, **kwargs)
    return async_call


# ---------------------------------------------------------------------------
# Проекты
# ---------------------------------------------------------------------------

def _create_project(conn, name: str, description: str = "") -> dict:
    pid = uuid.uuid4().hex
    conn.execute("INSERT INTO projects (id, name, description, created_at) VALUES (?,?,?,?)",
                 (pid, name, description, _now()))
    conn.commit()
    return {"id": pid, "name": name, "description": description}


def _sync_projects(conn, pages: list[dict]) -> dict:
    """Свести дочерние страницы «Клиенты» из Notion с проектами приложения.

    Notion → приложение (обратное направление к _create_project):
      • страница, которой ещё нет ни у одного проекта, становится новым
        проектом (adopt) — так папки, заведённые вручную в Notion, появляются
        в списке проектов;
      • у уже связанного проекта подхватывается переименование страницы.
    Приложение узнаёт «свои» страницы по notion_page_id, поэтому досье,
    созданные самим приложением, повторно не заводятся. Возвращает сводку.
    """
    linked = {}
    for row in conn.execute(
            "SELECT id, name, notion_page_id FROM projects "
            "WHERE notion_page_id IS NOT NULL").fetchall():
        linked[row["notion_page_id"].replace("-", "")] = row
    adopted, renamed = [], 0
    for p in pages:
        norm = p["id"].replace("-", "")
        row = linked.get(norm)
        if row is None:
            pid = uuid.uuid4().hex
            name = (p.get("title") or "").strip() or "Клиент"
            conn.execute(
                "INSERT INTO projects (id, name, description, created_at, "
                "notion_page_id, notion_url) VALUES (?,?,?,?,?,?)",
                (pid, name, "", _now(), p["id"], p.get("url", "")))
            adopted.append({"id": pid, "name": name})
        else:
            title = (p.get("title") or "").strip()
            if title and title != row["name"]:
                conn.execute("UPDATE projects SET name=? WHERE id=?", (title, row["id"]))
                renamed += 1
    conn.commit()
    return {"adopted": adopted, "renamed": renamed}


def _overview(conn) -> dict:
    """Проекты с их чатами + отдельные чаты (вне проектов)."""
    projects = []
    for p in conn.execute("SELECT * FROM projects ORDER BY created_at").fetchall():
        chats = conn.execute(
            "SELECT id, mode, title FROM chats WHERE project_id=? ORDER BY created_at",
            (p["id"],)).fetchall()
        projects.append({
            "id": p["id"], "name": p["name"], "description": p["description"],
            "notion_url": p["notion_url"],
            "chats": [dict(c) for c in chats],
        })
    standalone = conn.execute(
        "SELECT id, mode, title FROM chats WHERE project_id IS NULL ORDER BY created_at"
    ).fetchall()
    return {"projects": projects, "chats": [dict(c) for c in standalone]}


# ---------------------------------------------------------------------------
# Чаты и сообщения
# ---------------------------------------------------------------------------

def _create_chat(conn, mode: str, project_id: str | None, title: str) -> dict:
    cid = uuid.uuid4().hex
    conn.execute("INSERT INTO chats (id, project_id, mode, title, created_at) VALUES (?,?,?,?,?)",
                 (cid, project_id, mode, title, _now()))
    conn.commit()
    return {"id": cid, "mode": mode, "project_id": project_id, "title": title}


def _get_chat(conn, chat_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
    if not row:
        return None
    chat = dict(row)
    if chat["project_id"]:
        p = conn.execute(
            "SELECT name, description, notion_page_id, notion_url "
            "FROM projects WHERE id=?", (chat["project_id"],)).fetchone()
        if p:
            chat["project_name"] = p["name"]
            chat["project_description"] = p["description"]
            chat["project_page_id"] = p["notion_page_id"]
            chat["project_notion_url"] = p["notion_url"]
    return chat


def _set_project_notion(conn, project_id: str, page_id: str, url: str) -> None:
    conn.execute("UPDATE projects SET notion_page_id=?, notion_url=? WHERE id=?",
                 (page_id, url, project_id))
    conn.commit()


def _delete_chat(conn, chat_id: str) -> None:
    conn.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
    conn.execute("DELETE FROM chats WHERE id=?", (chat_id,))
    conn.commit()


def _rename_chat(conn, chat_id: str, title: str) -> None:
    conn.execute("UPDATE chats SET title=? WHERE id=?", (title, chat_id))
    conn.commit()


def _get_messages(conn, chat_id: str, limit: int = 60) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content, created_at FROM messages WHERE chat_id=? "
        "ORDER BY id DESC LIMIT ?", (chat_id, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def _add_message(conn, chat_id: str, role: str, content: str) -> None:
    conn.execute("INSERT INTO messages (chat_id, role, content, created_at) VALUES (?,?,?,?)",
                 (chat_id, role, content, _now()))
    conn.commit()


create_project     = _run(_create_project)
set_project_notion = _run(_set_project_notion)
sync_projects      = _run(_sync_projects)
overview       = _run(_overview)
create_chat    = _run(_create_chat)
get_chat       = _run(_get_chat)
delete_chat    = _run(_delete_chat)
rename_chat    = _run(_rename_chat)
get_messages   = _run(_get_messages)
add_message    = _run(_add_message)
