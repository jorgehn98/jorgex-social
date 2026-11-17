"""The worker loop: every account's work, pass after pass.

A task reads its due work from the domain tables (decision 09: no generic jobs table). It gets
the sessionmaker, not a session, because it must commit before each call to Meta and never
hold the database lock while waiting on the network.

Tasks are per account (decision 10): each connection has its own client, its own credential, its
own quota and caps, and its own incidents. `AccountTasks` runs one set per account, so a failing,
revoked, restricted or unreadable account stops neither the others nor the loop.
"""

import logging
import threading
from collections.abc import Callable, Sequence

from sqlalchemy.orm import sessionmaker

from jorgex_social.account.models import Connection

log = logging.getLogger(__name__)

Task = Callable[[sessionmaker], None]
# The accounts of one pass, read from the database: a connection added while the worker runs is
# picked up on the next pass.
Accounts = Callable[[sessionmaker], Sequence[Connection]]
# The tasks of one account, built the first pass the account is seen. Building them can fail.
TasksFor = Callable[[Connection], Sequence[Task]]

TASKS: Sequence[Task] = ()


def run_once(sessions: sessionmaker, tasks: Sequence[Task], account_id: str | None = None) -> None:
    for task in tasks:
        try:
            task(sessions)
        except Exception:
            # One failing task must not stop the others or the loop.
            log.exception(
                "task failed",
                extra={"task": getattr(task, "__name__", repr(task)), "account_id": account_id},
            )


class AccountTasks:
    """The tasks of every account, one set each, run account by account.

    A set is built the first pass its account is seen and kept between passes, so its per-account
    state (last connection check, held sends, read conversations) survives. An account whose row
    is gone stops running. What fails for one account — building its client, or any of its tasks —
    is logged with its id and leaves the other accounts alone."""

    __name__ = "accounts"

    def __init__(self, accounts: Accounts, tasks_for: TasksFor) -> None:
        self._accounts = accounts
        self._tasks_for = tasks_for
        self._built: dict[str, Sequence[Task]] = {}
        self._seen: set[str] = set()

    def __call__(self, sessions: sessionmaker) -> None:
        accounts = list(self._accounts(sessions))
        present = {account.id for account in accounts}
        for gone in sorted(self._seen - present):
            self._built.pop(gone, None)
            log.info("account is gone; its tasks stop", extra={"account_id": gone})
        if present != self._seen:
            log.info("accounts to work on", extra={"account_ids": sorted(present)})
            self._seen = present
        for account in accounts:
            tasks = self._built.get(account.id)
            if tasks is None:
                try:
                    tasks = tuple(self._tasks_for(account))
                except Exception:
                    log.exception(
                        "could not start the tasks of an account",
                        extra={"account_id": account.id},
                    )
                    continue
                self._built[account.id] = tasks
            run_once(sessions, tasks, account.id)


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
