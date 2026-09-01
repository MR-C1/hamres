"""
Task runner — executes a task's run() with the shared helpers it needs.
Keeps tasks.py simple: tasks receive a ctx dict instead of imports.
"""

from datetime import datetime, timedelta

from tasks import TASKS

BD_OFFSET = timedelta(hours=6)  # Bangladesh is UTC+6


def run_task(name):
    task = TASKS[name]
    ctx = dict(task.get("ctx", {}))
    # Inject the shared helpers (imported here to avoid a circular import
    # between app.py and tasks.py at module load).
    from app import llm, web_search, tg_send, log

    ctx.update(
        {
            "llm": llm,
            "web_search": web_search,
            "tg_send": tg_send,
            "log": log,
            "now": datetime.now() + BD_OFFSET,  # Dhaka time
            "name": name,  # tasks may need their own name (self-removal)
        }
    )
    task["run"](ctx)
