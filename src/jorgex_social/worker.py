import logging
import threading
from collections.abc import Callable, Sequence

from sqlalchemy.orm import sessionmaker

log = logging.getLogger(__name__)

# A task reads its due work from the domain tables (decision 09: no generic jobs table). It gets
# the sessionmaker, not a session, because it must commit before each call to Meta and never
# hold the database lock while waiting on the network.
Task = Callable[[sessionmaker], None]

TASKS: Sequence[Task] = ()


def run_once(sessions: sessionmaker, tasks: Sequence[Task]) -> None:
    for task in tasks:
        try:
            task(sessions)
        except Exception:
            # One failing task must not stop the others or the loop.
            log.exception("task failed", extra={"task": getattr(task, "__name__", repr(task))})


def run(
    sessions: sessionmaker,
    interval_seconds: float,
    stop: threading.Event,
    tasks: Sequence[Task] = TASKS,
) -> None:
    log.info("worker started", extra={"interval_seconds": interval_seconds})
    while not stop.is_set():
        run_once(sessions, tasks)
        stop.wait(interval_seconds)
    log.info("worker stopped")
