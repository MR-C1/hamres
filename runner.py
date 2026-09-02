"""Executes a task with the shared toolkit injected into ctx."""

from datetime import datetime

import comms
import config
import llm
import research
from tasks import TASKS


def run_task(name):
    task = TASKS[name]
    ctx = dict(task.get("ctx", {}))
    ctx.update({
        "llm": llm.complete,
        "web_search": research.web_search,
        "tg_send": comms.send,
        "log": comms.log,
        "now": datetime.now() + config.BD_OFFSET,  # Dhaka time
        "name": name,  # tasks may need their own name (self-removal)
    })
    task["run"](ctx)
