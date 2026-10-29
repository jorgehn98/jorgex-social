import threading

from jorgex_social.db import make_engine, make_sessionmaker
from jorgex_social.worker import run, run_once


def test_a_failing_task_does_not_stop_the_others(settings):
    sessions = make_sessionmaker(make_engine(settings))
    calls = []

    def broken(session):
        raise RuntimeError("boom")

    def healthy(session):
        calls.append("healthy")

    run_once(sessions, [broken, healthy])

    assert calls == ["healthy"]


def test_stops_when_asked(settings):
    sessions = make_sessionmaker(make_engine(settings))
    stop = threading.Event()

    def stop_after_first(session):
        stop.set()

    run(sessions, interval_seconds=60, stop=stop, tasks=[stop_after_first])

    assert stop.is_set()
