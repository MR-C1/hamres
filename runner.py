"""
Task runner — executes a task's run() with the shared helpers it needs.
Keeps tasks.py simple: tasks receive a ctx dict instead of imports.
"""

from tasks import TASKS


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
        }
    )
    task["run"](ctx)
