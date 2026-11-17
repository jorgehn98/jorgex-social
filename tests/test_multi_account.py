"""Two accounts in one installation: each one's work is its own (decision 10).

Every test here uses two simulated accounts with different ids, the way the worker builds them with
a client per connection (`instagram.Clients`), and checks that nothing of one ends up in the other
and that one failing, restricted or capped account leaves the other working.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from jorgex_social.access.tokens import create_token
from jorgex_social.account import kill_switch, scope
from jorgex_social.account.connection import ConnectionTask
from jorgex_social.account.models import ChannelIncident, Connection
from jorgex_social.api import create_app
from jorgex_social.clock import ManualClock
from jorgex_social.instagram import Clients
from jorgex_social.instagram.errors import AccountRestricted, CredentialUnavailable
from jorgex_social.instagram.fake import FakeInstagram
from jorgex_social.instagram.types import ContainerKind, ContainerRequest
from jorgex_social.publishing.models import Post
from jorgex_social.publishing.worker import PublishTask
from jorgex_social.replies.models import Delivery, Event
from jorgex_social.replies.rules import Rule
from jorgex_social.replies.task import ReplyTask
from jorgex_social.stats.models import PostSnapshot
from jorgex_social.stats.snapshots import SnapshotTask
from jorgex_social.worker import AccountTasks, run_once

START = datetime(2026, 10, 30, 12, tzinfo=UTC)
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 64
PUBLIC = "https://social.test"
FIRST = "17841400000000001"
SECOND = "17841400000000002"
PERSON_ONE = "17900000000000042"
PERSON_TWO = "17900000000000043"
REPLIES = ["One", "Two", "Three", "Four", "Five"]


@pytest.fixture
def clock():
    return ManualClock(START)


@pytest.fixture
def app(settings, clock):
    return create_app(settings, clock)


@pytest.fixture
def sessions(app):
    return app.state.sessionmaker


@pytest.fixture
def accounts(sessions) -> dict[str, str]:
    """Two connected accounts: the id of each row by the account id Instagram gives it."""
    with sessions() as session, session.begin():
        rows = {}
        for external in (FIRST, SECOND):
            connection = Connection(
                network="simulation",
                external_account_id=external,
                username=f"user_{external[-1]}",
            )
            session.add(connection)
            session.flush()
            rows[external] = connection.id
    return rows


@pytest.fixture
def fakes(clock) -> dict[str, FakeInstagram]:
    return {
        FIRST: FakeInstagram(clock, account_id=FIRST),
        SECOND: FakeInstagram(clock, account_id=SECOND),
    }


def headers_for(sessions, account_id, **grants) -> dict:
    with sessions() as session, session.begin():
        _, secret = create_token(
            session,
            f"agent-{account_id}",
            grants or {"posts": "write", "scheduling": "write", "stats": "read"},
            account_id,
        )
    return {"Authorization": f"Bearer {secret}"}


def due_post(client, headers, caption="A post") -> str:
    """A post the worker finds due now, made through the API as any client would."""
    media = client.post(
        "/v1/media", content=JPEG, headers={**headers, "Content-Type": "image/jpeg"}
    ).json()["id"]
    post = client.post(
        "/v1/posts", json={"type": "image", "caption": caption, "items": [media]}, headers=headers
    ).json()["id"]
    assert client.post(f"/v1/posts/{post}/publish-now", json={}, headers=headers).status_code == 200
    return post


def add_rule(sessions, account_id, channel="message", keywords=("precio",)) -> str:
    with sessions() as session, session.begin():
        rule = Rule(
            account_id=account_id,
            name=f"{channel}-{account_id}",
            channel=channel,
            priority=1,
            active=True,
            activated_at=START - timedelta(days=1),
            content={"keywords": list(keywords), "private_replies": REPLIES},
            created_at=START,
            updated_at=START,
        )
        session.add(rule)
        session.flush()
        return rule.id


def account_tasks(clock, settings, fakes, *, reply=None) -> AccountTasks:
    """What the worker builds each pass: the tasks of each account, with its own client."""

    def accounts(sessions):
        with sessions() as session:
            return scope.accounts_of(session, "simulation")

    def tasks_for(connection):
        instagram = fakes[connection.external_account_id]
        return [
            PublishTask(
                instagram,
                clock,
                settings.media_dir,
                PUBLIC,
                account_id=connection.id,
                sleep=lambda seconds: clock.advance(seconds=seconds),
            ),
            ConnectionTask(instagram, clock, None, account_id=connection.id),
            ReplyTask(
                instagram,
                clock,
                account_id=connection.id,
                poll_every=timedelta(0),
                **(reply or {}),
            ),
        ]

    return AccountTasks(accounts, tasks_for)


def post_status(sessions, post_id) -> str:
    with sessions() as session:
        return session.get_one(Post, post_id).status


def statuses_of(sessions, account_id) -> list[str]:
    with sessions() as session:
        return sorted(
            session.scalars(select(Delivery.status).where(Delivery.account_id == account_id))
        )


def rows_by_account(sessions) -> dict[str, Connection]:
    with sessions() as session:
        return {c.id: c for c in session.scalars(select(Connection))}


def incidents(sessions) -> list[tuple[str | None, str]]:
    with sessions() as session:
        return [
            (incident.account_id, incident.type)
            for incident in session.scalars(select(ChannelIncident))
        ]


# One client per connection


def test_the_worker_builds_one_client_per_connection(sessions, settings, clock, accounts):
    """Two accounts of one network get two clients, and each answers as its own account."""
    clients = Clients(settings, clock, None)
    with sessions() as session:
        first = session.get_one(Connection, accounts[FIRST])
        second = session.get_one(Connection, accounts[SECOND])

    assert clients(first) is clients(first)  # kept between passes: a fake is the account's state
    assert clients(first) is not clients(second)
    assert (clients(first).account().id, clients(second).account().id) == (FIRST, SECOND)


# Publishing


def test_each_account_publishes_its_own_due_posts_with_its_own_client(
    client, sessions, settings, clock, accounts, fakes
):
    first_post = due_post(client, headers_for(sessions, accounts[FIRST]), "For the first account")
    second_post = due_post(client, headers_for(sessions, accounts[SECOND]), "For the second")
    expected = dict(zip((first_post, second_post), ("published", "published"), strict=True))

    account_tasks(clock, settings, fakes)(sessions)

    assert {post: post_status(sessions, post) for post in expected} == expected
    first_media = fakes[FIRST].list_media()
    second_media = fakes[SECOND].list_media()
    assert [m.caption for m in first_media] == ["For the first account"]
    assert [m.caption for m in second_media] == ["For the second"]
    with sessions() as session:
        refs = {post.id: post.external_ref for post in session.scalars(select(Post))}
    assert refs == {first_post: first_media[0].id, second_post: second_media[0].id}


def test_a_credential_error_in_one_account_does_not_stop_the_other(
    client, sessions, settings, clock, accounts, fakes
):
    first_post = due_post(client, headers_for(sessions, accounts[FIRST]))
    second_post = due_post(client, headers_for(sessions, accounts[SECOND]))
    fakes[FIRST].fail_next(
        "create_container", CredentialUnavailable("the stored credential does not decrypt")
    )

    account_tasks(clock, settings, fakes)(sessions)

    assert post_status(sessions, second_post) == "published"
    with sessions() as session:
        failed = session.get_one(Post, first_post)
    assert (failed.status, failed.failure) == ("failed", "credential_unavailable")


def test_an_account_whose_client_cannot_be_built_does_not_stop_the_other(
    client, sessions, settings, clock, accounts, fakes, caplog
):
    second_post = due_post(client, headers_for(sessions, accounts[SECOND]))

    def accounts_of_simulation(sessions):
        with sessions() as session:
            return scope.accounts_of(session, "simulation")

    def tasks_for(connection):
        if connection.external_account_id == FIRST:
            raise CredentialUnavailable("the stored credential does not decrypt")
        return [
            PublishTask(
                fakes[SECOND],
                clock,
                settings.media_dir,
                PUBLIC,
                account_id=connection.id,
                sleep=lambda seconds: clock.advance(seconds=seconds),
            )
        ]

    AccountTasks(accounts_of_simulation, tasks_for)(sessions)

    assert post_status(sessions, second_post) == "published"
    failed = [r for r in caplog.records if getattr(r, "account_id", None) == accounts[FIRST]]
    assert failed and all(r.levelname == "ERROR" for r in failed)


def test_a_new_account_is_picked_up_on_a_later_pass_without_a_restart(
    client, sessions, settings, clock, accounts, fakes
):
    first_post = due_post(client, headers_for(sessions, accounts[FIRST]), "For the first account")
    runner = account_tasks(clock, settings, fakes)
    runner(sessions)
    assert post_status(sessions, first_post) == "published"

    second_post = due_post(client, headers_for(sessions, accounts[SECOND]), "For the second")
    runner(sessions)

    assert post_status(sessions, second_post) == "published"
    assert [m.caption for m in fakes[SECOND].list_media()] == ["For the second"]


def test_the_kill_switch_of_one_account_stops_only_that_account(
    client, sessions, settings, clock, accounts, fakes
):
    first_post = due_post(client, headers_for(sessions, accounts[FIRST]))
    second_post = due_post(client, headers_for(sessions, accounts[SECOND]))
    for external in (FIRST, SECOND):
        add_rule(sessions, accounts[external])
        fakes[external].add_message(PERSON_ONE, "precio")
    with sessions() as session, session.begin():
        kill_switch.set_state(
            session, session.get_one(Connection, accounts[FIRST]), True, clock.now()
        )

    account_tasks(clock, settings, fakes)(sessions)

    assert (post_status(sessions, first_post), post_status(sessions, second_post)) == (
        "scheduled",
        "published",
    )
    assert fakes[FIRST].sent == []
    assert [operation for operation, _, _ in fakes[SECOND].sent] == ["send_message"]
    assert statuses_of(sessions, accounts[FIRST]) == ["skipped"]


def test_the_hourly_cap_of_one_account_does_not_hold_the_other(
    sessions, settings, clock, accounts, fakes
):
    comments = {FIRST: (PERSON_ONE, PERSON_TWO), SECOND: (PERSON_ONE,)}
    for external in (FIRST, SECOND):
        add_rule(sessions, accounts[external], channel="comment")
        container = fakes[external].create_container(
            ContainerRequest(ContainerKind.IMAGE, url="https://x.test/media")
        )
        published = fakes[external].publish(container)
        for person in comments[external]:
            fakes[external].add_comment(published, "precio", author_id=person)
    runner = account_tasks(clock, settings, fakes, reply={"hourly_private_cap": 1})

    runner(sessions)

    # The first account reaches its cap with one private reply still pending; the second, with the
    # same cap, is not held back by the first one's count.
    assert statuses_of(sessions, accounts[FIRST]) == ["pending", "sent"]
    assert statuses_of(sessions, accounts[SECOND]) == ["sent"]
    assert len(fakes[FIRST].sent) == 1
    assert len(fakes[SECOND].sent) == 1


# Connection checks


def test_a_revoked_credential_in_one_account_leaves_the_other_alone(
    sessions, clock, accounts, fakes
):
    checks = [
        ConnectionTask(fakes[external], clock, None, account_id=accounts[external])
        for external in (FIRST, SECOND)
    ]
    fakes[FIRST].revoke()

    run_once(sessions, checks)

    rows = rows_by_account(sessions)
    assert (rows[accounts[FIRST]].status, rows[accounts[SECOND]].status) == ("revoked", "ok")
    assert (accounts[FIRST], "permission_revoked") in incidents(sessions)
    assert [i for i in incidents(sessions) if i[0] == accounts[SECOND]] == []


def test_a_restricted_account_trips_only_its_own_switch(
    client, sessions, settings, clock, accounts, fakes
):
    due_post(client, headers_for(sessions, accounts[FIRST]))
    second_post = due_post(client, headers_for(sessions, accounts[SECOND]))
    fakes[FIRST].fail_next("create_container", AccountRestricted("restricted", code=25))

    account_tasks(clock, settings, fakes)(sessions)

    rows = rows_by_account(sessions)
    assert (rows[accounts[FIRST]].kill_switch, rows[accounts[SECOND]].kill_switch) == (True, False)
    assert (accounts[FIRST], "account_restricted") in incidents(sessions)
    assert [i for i in incidents(sessions) if i[0] == accounts[SECOND]] == []
    assert post_status(sessions, second_post) == "published"


# Statistics


def test_snapshots_are_the_account_of_the_task(sessions, clock, accounts, fakes):
    media = {}
    for external in (FIRST, SECOND):
        container = fakes[external].create_container(
            ContainerRequest(ContainerKind.IMAGE, url=f"https://x.test/{external}")
        )
        media[external] = fakes[external].publish(container)

    for external in (FIRST, SECOND):
        SnapshotTask(fakes[external], clock, account_id=accounts[external])(sessions)

    with sessions() as session:
        rows = session.execute(
            select(PostSnapshot.account_id, PostSnapshot.media_ref).where(
                PostSnapshot.media_ref.in_(list(media.values()))
            )
        ).all()
    assert sorted(rows) == sorted(
        [(accounts[external], media[external]) for external in (FIRST, SECOND)]
    )


def test_events_and_deliveries_are_the_account_of_the_task(
    sessions, settings, clock, accounts, fakes
):
    """The same text from the same person on each account fires each account's own rule, and no
    event, delivery or conversation of one is written as the other."""
    for external in (FIRST, SECOND):
        add_rule(sessions, accounts[external], channel="comment")
        container = fakes[external].create_container(
            ContainerRequest(ContainerKind.IMAGE, url="https://x.test/media")
        )
        published = fakes[external].publish(container)
        fakes[external].add_comment(published, "precio", author_id=PERSON_ONE)

    account_tasks(clock, settings, fakes)(sessions)

    with sessions() as session:
        events = [event.account_id for event in session.scalars(select(Event))]
        deliveries = [d.account_id for d in session.scalars(select(Delivery))]
    assert sorted(events) == sorted([accounts[FIRST], accounts[SECOND]])
    assert sorted(deliveries) == sorted([accounts[FIRST], accounts[SECOND]])
    assert statuses_of(sessions, accounts[FIRST]) == ["sent"]
    assert statuses_of(sessions, accounts[SECOND]) == ["sent"]
