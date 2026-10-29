import logging
import threading
from collections.abc import Callable, Sequence

from sqlalchemy.orm import Session, sessionmaker

log = logging.getLogger(__name__)

# A task reads its due work from the domain tables (decision 09: no generic jobs table).
Task = Callable[[Session], None]

TASKS: Sequence[Task] = ()


def run_once(sessions: sessionmaker, tasks: Sequence[Task]) -> None:
    for task in tasks:
        try:
            with sessions() as session, session.begin():
                task(session)
        except Exception:
            # One failing task must not stop the others or the loop.
            log.exception("task failed", extra={"task": task.__name__})


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
