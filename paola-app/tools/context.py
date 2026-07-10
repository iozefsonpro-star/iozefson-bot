"""Контекст текущего запроса для инструментов.

agent.run_chat выставляет проект, в котором идёт чат, — инструменты
(save_to_notion, update_client_memory) берут отсюда страницу-досье,
не таская её через input_schema.
"""
from contextvars import ContextVar

CURRENT_PROJECT_PAGE: ContextVar[str | None] = ContextVar(
    "current_project_page", default=None)
