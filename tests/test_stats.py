from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from jorgex_social.access.tokens import create_token
from jorgex_social.clock import ManualClock
from jorgex_social.instagram.errors import AuthError, Refused
from jorgex_social.instagram.fake import FakeInstagram
from jorgex_social.instagram.types import ContainerKind, ContainerRequest
from jorgex_social.stats.models import AccountSnapshot, PostSnapshot
from jorgex_social.stats.snapshots import BACKFILL_DAYS, SnapshotTask, closed_days

START = datetime(2026, 10, 29, 12, tzinfo=UTC)
IMAGE = ContainerRequest(ContainerKind.IMAGE, url="https://x.test/a.jpg", caption="c")
REEL = ContainerRequest(ContainerKind.REELS, url="https://x.test/a.mp4", caption="r")


@pytest.fixture
def clock():
    return ManualClock(START)


@pytest.fixture
def ig(clock):
    return FakeInstagram(clock)


@pytest.fixture
def sessions(app):
    return app.state.sessionmaker


def publish(ig, clock, request):
    container = ig.create_container(request)
    clock.advance(minutes=1)
    return ig.publish(container)


def count(sessions, model):
    with sessions() as session:
        return session.scalar(select(func.count()).select_from(model))


def test_closed_days_end_at_least_48_hours_ago():
    days = closed_days(START)

    assert len(days) == BACKFILL_DAYS
    assert days[-1] == (datetime(2026, 10, 26, tzinfo=UTC), datetime(2026, 10, 27, tzinfo=UTC))
    assert all(end - start == timedelta(days=1) for start, end in days)


def test_first_sweep_backfills_account_and_snapshots_every_media(sessions, ig, clock):
    image = publish(ig, clock, IMAGE)
    reel = publish(ig, clock, REEL)

    SnapshotTask(ig, clock)(sessions)

    assert count(sessions, AccountSnapshot) == BACKFILL_DAYS
    with sessions() as session:
        rows = {r.media_ref: r for r in session.scalars(select(PostSnapshot))}
    assert set(rows) == {image, reel}
    assert rows[reel].product_type == "REELS"
    assert "ig_reels_avg_watch_time" in rows[reel].metrics


def test_sweeps_at_most_hourly_and_only_what_is_due(sessions, ig, clock):
    publish(ig, clock, IMAGE)
    task = SnapshotTask(ig, clock)
    task(sessions)

    clock.advance(minutes=30)
    task(sessions)
    assert count(sessions, PostSnapshot) == 1

    clock.advance(hours=2)
    task(sessions)
    assert count(sessions, PostSnapshot) == 1  # recent media: daily

    clock.advance(days=1)
    task(sessions)
    assert count(sessions, PostSnapshot) == 2
    assert count(sessions, AccountSnapshot) == BACKFILL_DAYS + 1  # one more closed day


def test_old_media_are_sampled_weekly(sessions, ig, clock):
    publish(ig, clock, IMAGE)
    clock.advance(days=40)
    task = SnapshotTask(ig, clock)
    task(sessions)

    clock.advance(days=2)
    task(sessions)
    assert count(sessions, PostSnapshot) == 1

    clock.advance(days=5)
    task(sessions)
    assert count(sessions, PostSnapshot) == 2


def test_one_failing_media_does_not_stop_the_others(sessions, ig, clock):
    publish(ig, clock, IMAGE)
    publish(ig, clock, IMAGE)
    ig.fail_next("media_insights", Refused("deleted", code=100))

    SnapshotTask(ig, clock)(sessions)

    assert count(sessions, PostSnapshot) == 1


def test_invalid_credential_stops_the_sweep(sessions, ig, clock):
    publish(ig, clock, IMAGE)
    ig.fail_next("account", AuthError("expired", code=190))

    with pytest.raises(AuthError):
        SnapshotTask(ig, clock)(sessions)

    assert count(sessions, PostSnapshot) == 0


def auth(app, **grants):
    with app.state.sessionmaker() as session, session.begin():
        _, secret = create_token(session, "agent", grants)
    return {"Authorization": f"Bearer {secret}"}


def test_api_requires_stats_read(app, client):
    assert client.get("/v1/stats/media", headers=auth(app, posts="write")).status_code == 403


def test_api_returns_history(app, client, sessions, ig, clock):
    media_id = publish(ig, clock, IMAGE)
    task = SnapshotTask(ig, clock)
    task(sessions)
    clock.advance(days=1, hours=1)
    task(sessions)
    headers = auth(app, stats="read")

    latest = client.get("/v1/stats/media", headers=headers).json()
    history = client.get(f"/v1/stats/media/{media_id}", headers=headers).json()
    account = client.get(
        "/v1/stats/account",
        params={"since": "2026-10-25T00:00:00Z"},
        headers=headers,
    ).json()

    assert [m["media_ref"] for m in latest] == [media_id]
    assert len(history) == 2
    assert history[0]["taken_at"] > history[1]["taken_at"]
    assert [a["period_start"][:10] for a in account] == ["2026-10-27", "2026-10-26", "2026-10-25"]


def test_api_rejects_dates_without_timezone(app, client):
    response = client.get(
        "/v1/stats/account",
        params={"since": "2026-10-25T00:00:00"},
        headers=auth(app, stats="read"),
    )

    assert response.status_code == 422
