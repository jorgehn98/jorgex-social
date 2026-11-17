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

from jorgex_social.account.connection import mark_unreadable
from jorgex_social.account.models import Connection
from jorgex_social.clock import Clock
from jorgex_social.instagram.errors import CredentialUnavailable

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
    is gone stops running, and `forget` lets the caller drop what it kept for it. What fails for
    one account — building its client, or any of its tasks — is logged with its id and leaves the
    other accounts alone.

    A client that cannot be built (the stored credential does not decrypt, or the row has none)
    leaves the account with no check to run, so it is recorded as unreadable here, with its
    incident, and the build is retried on later passes. The connection check of the pass where it
    works again closes the incident as usual."""

    __name__ = "accounts"

    def __init__(
        self,
        accounts: Accounts,
        tasks_for: TasksFor,
        clock: Clock,
        forget: Callable[[str], None] = lambda account_id: None,
    ) -> None:
        self._accounts = accounts
        self._tasks_for = tasks_for
        self._clock = clock
        self._forget = forget
        self._built: dict[str, Sequence[Task]] = {}
        self._seen: set[str] | None = None
        # Accounts whose build failure was already logged with its traceback: the later passes
        # log one line, so a credential that stays unreadable does not fill the log every cycle.
        self._reported: set[str] = set()

    def __call__(self, sessions: sessionmaker) -> None:
        accounts = list(self._accounts(sessions))
        present = {account.id for account in accounts}
        for gone in sorted((self._seen or set()) - present):
            self._built.pop(gone, None)
            self._reported.discard(gone)
            self._forget(gone)
            log.info("account is gone; its tasks stop", extra={"account_id": gone})
        if present != self._seen:
            self._seen = present
            log.info(
                "accounts to work on" if present else "no account to work on yet; waiting for one",
                extra={"account_ids": sorted(present)},
            )
        for account in accounts:
            tasks = self._built.get(account.id)
            if tasks is None:
                try:
                    tasks = tuple(self._tasks_for(account))
                except Exception as exc:
                    self._build_failed(sessions, account, exc)
                    continue
                self._reported.discard(account.id)
                self._built[account.id] = tasks
            run_once(sessions, tasks, account.id)

    def _build_failed(self, sessions: sessionmaker, account: Connection, exc: Exception) -> None:
        """One account's build failure, after which the others of the pass go on.

        A credential that cannot be read is recorded as the connection check would record it, so
        the operator sees the account in `GET /v1/account` and its incident instead of only a log
        line. The traceback is logged once per account; later passes log the exception's type
        alone, since the type and the account are what says whether something changed."""
        if isinstance(exc, CredentialUnavailable):
            try:
                mark_unreadable(sessions, account.id, self._clock.now())
            except Exception:
                log.exception(
                    "could not record an unreadable account", extra={"account_id": account.id}
                )
        if account.id in self._reported:
            log.error(
                "could not start the tasks of an account",
                extra={"account_id": account.id, "error_type": type(exc).__name__},
            )
            return
        self._reported.add(account.id)
        log.exception("could not start the tasks of an account", extra={"account_id": account.id})


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
