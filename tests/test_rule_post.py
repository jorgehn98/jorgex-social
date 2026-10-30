"""A rule prepared together with its post: it waits, gets the post's media when the post goes out,
and answers what arrived in between. Runs through the real API, worker tasks and fake Instagram."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select

from jorgex_social.access.tokens import create_token
from jorgex_social.account.connection import ConnectionTask
from jorgex_social.account.models import AuditEntry
from jorgex_social.api import create_app
from jorgex_social.clock import ManualClock
from jorgex_social.instagram.errors import Unavailable
from jorgex_social.instagram.fake import FakeInstagram
from jorgex_social.instagram.types import ContainerKind, ContainerRequest
from jorgex_social.publishing.models import Post
from jorgex_social.publishing.worker import PublishTask
from jorgex_social.replies.models import Delivery, Event, Rule
from jorgex_social.replies.rules import bind_to_published, first_match
from jorgex_social.replies.task import ReplyTask

START = datetime(2026, 10, 29, 12, tzinfo=UTC)
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 64
PERSON = "17900000000000042"
OTHER_PERSON = "17900000000000043"
PRIVATE_ONLY = {"keywords": ["precio"], "private_replies": ["DM"]}
PUBLIC_TOO = {**PRIVATE_ONLY, "public_replies": ["One", "Two", "Three"]}


@pytest.fixture
def clock():
    return ManualClock(START)


@pytest.fixture
def ig(clock):
    return FakeInstagram(clock)


@pytest.fixture
def app(settings, clock):
    return create_app(settings, clock)


@pytest.fixture
def sessions(app):
    return app.state.sessionmaker


@pytest.fixture
def headers(app):
    with app.state.sessionmaker() as session, session.begin():
        _, secret = create_token(
            session, "agent", {"posts": "write", "scheduling": "write", "rules": "write"}
        )
    return {"Authorization": f"Bearer {secret}"}


@pytest.fixture
def replies(ig, clock, sessions):
    ConnectionTask(ig, clock, None)(sessions)
    return ReplyTask(ig, clock, poll_every=timedelta(0))


@pytest.fixture
def publisher(ig, clock, settings):
    return PublishTask(
        ig,
        clock,
        settings.media_dir,
        "https://social.test",
        sleep=lambda s: clock.advance(seconds=s),
    )


def make_post(client, headers, type_="image"):
    media = client.post(
        "/v1/media", content=JPEG, headers={**headers, "Content-Type": "image/jpeg"}
    ).json()["id"]
    response = client.post(
        "/v1/posts",
        json={"type": type_, "caption": "A post", "items": [media]},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def make_rule(client, headers, *, activate=True, content=PRIVATE_ONLY, **fields):
    response = client.post(
        "/v1/rules",
        json={"name": "price", "channel": "comment", "content": content, **fields},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    rule = response.json()
    if activate:
        rule = client.post(f"/v1/rules/{rule['id']}/activate", headers=headers).json()
    return rule


def get_rule(client, headers, rule_id):
    return client.get(f"/v1/rules/{rule_id}", headers=headers).json()


def set_status(sessions, post_id, status, external_ref=None):
    with sessions() as session, session.begin():
        post = session.get_one(Post, post_id)
        post.status, post.external_ref = status, external_ref


def go_live(client, headers, publisher, sessions, post_id) -> str:
    assert (
        client.post(f"/v1/posts/{post_id}/publish-now", json={}, headers=headers).status_code == 200
    )
    publisher(sessions)
    with sessions() as session:
        return session.get_one(Post, post_id).external_ref


def events_of(sessions, media_ref):
    with sessions() as session:
        return session.scalars(select(Event).where(Event.media_ref == media_ref)).all()


def outcomes(sessions):
    with sessions() as session:
        found = session.scalars(select(Delivery).order_by(Delivery.created_at, Delivery.id)).all()
    return [(d.action, d.status, d.reason) for d in found]


def other_media(ig) -> str:
    return ig.publish(
        ig.create_container(ContainerRequest(ContainerKind.IMAGE, url="https://x.test/a"))
    )


# The end-to-end path


def test_a_rule_prepared_with_its_post_answers_with_nobody_watching(
    client, headers, ig, sessions, clock, publisher, replies
):
    post_id = make_post(client, headers)
    rule = make_rule(client, headers, post_id=post_id)
    assert (rule["active"], rule["waiting_for_post"], rule["media_ref"]) == (True, True, None)
    response = client.post(
        f"/v1/posts/{post_id}/schedule",
        json={"scheduled_for": (START + timedelta(hours=1)).isoformat()},
        headers=headers,
    )
    assert response.status_code == 200, response.text

    clock.advance(hours=1)
    publisher(sessions)

    with sessions() as session:
        media_ref = session.get_one(Post, post_id).external_ref
    bound = get_rule(client, headers, rule["id"])
    assert media_ref is not None
    assert (bound["media_ref"], bound["post_id"], bound["waiting_for_post"]) == (
        media_ref,
        post_id,
        False,
    )

    comment = ig.add_comment(media_ref, "precio?", author_id=PERSON)
    replies(sessions)

    assert ig.sent[0][:2] == ("reply_private", comment)
    assert outcomes(sessions) == [("private_reply", "sent", None)]


# Waiting


def test_first_match_never_picks_a_waiting_rule():
    waiting = Rule(
        id="waiting", post_id="p1", media_ref=None, activated_at=START, content=PRIVATE_ONLY
    )
    bound = Rule(id="bound", post_id="p1", media_ref="m1", activated_at=START, content=PRIVATE_ONLY)

    assert first_match([waiting], "precio", media_ref="m1") is None
    assert first_match([waiting], "precio") is None
    assert first_match([waiting, bound], "precio", media_ref="m1").id == "bound"


def test_an_active_waiting_rule_does_not_answer_a_poll_on_any_post(
    client, headers, ig, sessions, replies
):
    make_rule(client, headers, post_id=make_post(client, headers))
    media = other_media(ig)
    ig.add_comment(media, "precio", author_id=PERSON)

    replies(sessions)

    assert ig.sent == []
    assert [e.rule_id for e in events_of(sessions, media)] == [None]


# Creating and editing


@pytest.mark.parametrize(
    ("status", "external_ref", "waiting"),
    [
        ("draft", None, True),
        ("scheduled", None, True),
        ("publishing", None, True),
        ("failed", None, True),
        ("outcome_unknown", None, True),
        ("published", None, True),  # the service never learned its media id
        ("published", "m1", False),
    ],
)
def test_the_post_status_decides_whether_the_rule_waits(
    client, headers, sessions, status, external_ref, waiting
):
    post_id = make_post(client, headers)
    set_status(sessions, post_id, status, external_ref)

    created = make_rule(client, headers, post_id=post_id, activate=False)

    other = make_rule(client, headers, activate=False)
    patched = client.patch(
        f"/v1/rules/{other['id']}", json={"post_id": post_id}, headers=headers
    ).json()
    for rule in (created, patched):
        assert (rule["post_id"], rule["media_ref"], rule["waiting_for_post"]) == (
            post_id,
            external_ref,
            waiting,
        )


def test_refused_links(client, headers, sessions):
    post_id = make_post(client, headers)
    story = make_post(client, headers, "story")
    cancelled = make_post(client, headers)
    client.post(f"/v1/posts/{cancelled}/cancel", headers=headers)
    other_network = make_post(client, headers)
    with sessions() as session, session.begin():
        session.get_one(Post, other_network).network = "another"
    rule = make_rule(client, headers, activate=False)

    def create(**fields):
        body = {"name": "x", "channel": "comment", "content": PRIVATE_ONLY, **fields}
        return client.post("/v1/rules", json=body, headers=headers)

    def patch(**fields):
        return client.patch(f"/v1/rules/{rule['id']}", json=fields, headers=headers)

    def seen(response):
        return response.status_code, response.json()["detail"]["code"]

    for send in (create, patch):
        assert seen(send(post_id=post_id, media_ref="m1")) == (422, "invalid_rule")
        assert seen(send(post_id="nope")) == (404, "not_found")
        assert seen(send(post_id=other_network)) == (404, "not_found")
        assert seen(send(post_id=story)) == (422, "invalid_rule")
        assert "stories" in send(post_id=story).json()["detail"]["message"]
        assert seen(send(post_id=cancelled)) == (409, "invalid_state")
    assert seen(create(post_id=post_id, channel="message", content=PRIVATE_ONLY)) == (
        422,
        "invalid_rule",
    )
    assert seen(patch(post_id=None)) == (422, "invalid_value")
    dm = make_rule(client, headers, activate=False, channel="message")
    response = client.patch(f"/v1/rules/{dm['id']}", json={"post_id": post_id}, headers=headers)
    assert seen(response) == (422, "invalid_rule")
    assert get_rule(client, headers, rule["id"])["post_id"] is None


def test_setting_or_clearing_media_ref_ends_the_link(client, headers, sessions):
    def linked(external_ref=None):
        post_id = make_post(client, headers)
        set_status(sessions, post_id, "published" if external_ref else "draft", external_ref)
        return make_rule(client, headers, post_id=post_id, activate=False)["id"]

    def patch(rule_id, **fields):
        return client.patch(f"/v1/rules/{rule_id}", json=fields, headers=headers).json()

    set_media = patch(linked(), media_ref="m9")
    assert (set_media["media_ref"], set_media["post_id"]) == ("m9", None)

    # Bound: it applies to every post and does not fall back to waiting.
    for rule_id in (linked("m1"), linked()):
        cleared = patch(rule_id, media_ref=None)
        assert (cleared["media_ref"], cleared["post_id"], cleared["waiting_for_post"]) == (
            None,
            None,
            False,
        )


def test_an_edit_of_something_else_keeps_the_link(client, headers, sessions):
    post_id = make_post(client, headers)
    set_status(sessions, post_id, "published", "m1")
    rule = make_rule(client, headers, post_id=post_id, activate=False)

    edited = client.patch(f"/v1/rules/{rule['id']}", json={"priority": 5}, headers=headers).json()

    assert (edited["priority"], edited["media_ref"], edited["post_id"]) == (5, "m1", post_id)


def test_post_id_replaces_a_media_ref_the_rule_had(client, headers):
    post_id = make_post(client, headers)
    rule = make_rule(client, headers, media_ref="m1", activate=False)

    moved = client.patch(
        f"/v1/rules/{rule['id']}", json={"post_id": post_id}, headers=headers
    ).json()

    assert (moved["media_ref"], moved["post_id"], moved["waiting_for_post"]) == (
        None,
        post_id,
        True,
    )


# Binding


def test_the_worker_binds_and_audits_identifiers_only(
    client, headers, ig, sessions, publisher, replies
):
    post_id = make_post(client, headers)
    unrelated_post = make_post(client, headers)
    inactive = make_rule(client, headers, post_id=post_id, activate=False)
    active = make_rule(client, headers, post_id=post_id)
    unrelated = make_rule(client, headers, post_id=unrelated_post)
    message = make_rule(client, headers, channel="message", activate=False)

    media_ref = go_live(client, headers, publisher, sessions, post_id)

    assert get_rule(client, headers, inactive["id"])["media_ref"] == media_ref
    assert get_rule(client, headers, active["id"])["media_ref"] == media_ref
    assert get_rule(client, headers, unrelated["id"])["waiting_for_post"] is True
    assert get_rule(client, headers, message["id"])["media_ref"] is None
    with sessions() as session:
        entries = session.scalars(
            select(AuditEntry).where(AuditEntry.action == "rule.bound_to_post")
        ).all()
    assert {e.object_id for e in entries} == {inactive["id"], active["id"]}
    assert all(e.detail == {"post_id": post_id, "media_ref": media_ref} for e in entries)


def test_the_resolver_binds_a_post_whose_outcome_was_unknown(
    client, headers, ig, sessions, publisher, replies
):
    post_id = make_post(client, headers)
    rule = make_rule(client, headers, post_id=post_id)
    ig.fail_next("publish", Unavailable("502 after going live"), after=True)
    client.post(f"/v1/posts/{post_id}/publish-now", json={}, headers=headers)
    publisher.publish_due(sessions)
    assert get_rule(client, headers, rule["id"])["waiting_for_post"] is True

    publisher.resolve(sessions)

    with sessions() as session:
        media_ref = session.get_one(Post, post_id).external_ref
    assert media_ref is not None
    assert get_rule(client, headers, rule["id"])["media_ref"] == media_ref


def test_settling_a_post_by_hand_binds_it(client, headers, ig, sessions, publisher):
    post_id = make_post(client, headers)
    rule = make_rule(client, headers, post_id=post_id)
    ig.fail_next("publish", Unavailable("502"))
    client.post(f"/v1/posts/{post_id}/publish-now", json={}, headers=headers)
    publisher.publish_due(sessions)

    response = client.post(
        f"/v1/posts/{post_id}/resolve",
        json={"outcome": "published", "media_ref": "17999"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert get_rule(client, headers, rule["id"])["media_ref"] == "17999"


def test_a_post_published_with_no_media_id_leaves_the_rule_waiting(
    client, headers, ig, sessions, publisher
):
    post_id = make_post(client, headers)
    rule = make_rule(client, headers, post_id=post_id)
    ig.fail_next("publish", Unavailable("502"))
    client.post(f"/v1/posts/{post_id}/publish-now", json={}, headers=headers)
    publisher.publish_due(sessions)

    client.post(f"/v1/posts/{post_id}/resolve", json={"outcome": "published"}, headers=headers)

    assert get_rule(client, headers, rule["id"])["waiting_for_post"] is True
    with sessions() as session:
        actions = session.scalars(select(AuditEntry.action)).all()
    assert "rule.bound_to_post" not in actions


# Closing the gap by reading again


def bind(sessions, post_id, media_ref, now):
    with sessions() as session, session.begin():
        post = session.get_one(Post, post_id)
        post.status, post.external_ref = "published", media_ref
        bind_to_published(session, post, now)


def test_a_comment_in_the_gap_of_a_real_publish_is_answered_at_the_next_poll(
    client, headers, ig, sessions, clock, publisher, replies
):
    """Between Instagram's answer and the worker's commit, a poll sees the comment with no rule."""
    post_id = make_post(client, headers)
    make_rule(client, headers, post_id=post_id)
    comments = []
    original = ig.publish

    def publish_and_someone_comments(container):
        media = original(container)
        comments.append(ig.add_comment(media, "precio", author_id=PERSON))
        replies(sessions)
        assert ig.sent == []
        assert [e.rule_id for e in events_of(sessions, media)] == [None]
        return media

    ig.publish = publish_and_someone_comments

    media_ref = go_live(client, headers, publisher, sessions, post_id)
    assert events_of(sessions, media_ref) == []
    replies(sessions)

    assert [(op, target) for op, target, _ in ig.sent] == [("reply_private", comments[0])]


def test_a_comment_from_before_the_binding_gets_every_reply_from_its_own_time(
    client, headers, ig, sessions, clock, replies
):
    post_id = make_post(client, headers)
    make_rule(client, headers, post_id=post_id, content=PUBLIC_TOO)
    media = other_media(ig)
    clock.advance(minutes=1)
    comment = ig.add_comment(media, "precio", author_id=PERSON)
    ig.add_comment(media, "nice", author_id=OTHER_PERSON)
    replies(sessions)
    assert ig.sent == []
    assert [e.rule_id for e in events_of(sessions, media)] == [None, None]

    bind(sessions, post_id, media, clock.now())
    assert events_of(sessions, media) == []
    replies(sessions)

    assert {op for op, target, _ in ig.sent if target == comment} == {
        "reply_public",
        "reply_private",
    }
    assert len(ig.sent) == 2
    # The comment's own time comes back, not the time it was read again.
    events = events_of(sessions, media)
    assert len(events) == 2
    assert {e.occurred_at for e in events} == {START + timedelta(minutes=1)}

    # A later poll changes nothing.
    replies(sessions)
    assert len(ig.sent) == 2


def test_a_comment_that_fired_another_rule_is_not_answered_twice(
    client, headers, ig, sessions, clock, replies
):
    post_id = make_post(client, headers)
    make_rule(client, headers, post_id=post_id)
    make_rule(client, headers, content={"keywords": ["hola"], "private_replies": ["Hi"]})
    media = other_media(ig)
    clock.advance(minutes=1)
    fired = ig.add_comment(media, "hola, precio", author_id=PERSON)
    replies(sessions)
    [kept] = events_of(sessions, media)
    assert kept.rule_id is not None

    bind(sessions, post_id, media, clock.now())
    replies(sessions)

    [still] = events_of(sessions, media)
    assert (still.id, still.rule_id) == (kept.id, kept.rule_id)
    assert [target for _, target, _ in ig.sent] == [fired]


def test_a_person_repeating_the_keyword_before_the_binding_is_answered_once(
    client, headers, ig, sessions, clock, replies
):
    post_id = make_post(client, headers)
    make_rule(client, headers, post_id=post_id)
    media = other_media(ig)
    clock.advance(minutes=1)
    ig.add_comment(media, "precio", author_id=PERSON)
    clock.advance(seconds=1)
    ig.add_comment(media, "precio?", author_id=PERSON)
    replies(sessions)

    bind(sessions, post_id, media, clock.now())
    replies(sessions)

    assert len(ig.sent) == 1
    assert outcomes(sessions) == [
        ("private_reply", "sent", None),
        ("private_reply", "skipped", "already_answered"),
    ]


def test_a_comment_from_before_activation_stays_unanswered(
    client, headers, ig, sessions, clock, replies
):
    post_id = make_post(client, headers)
    rule = make_rule(client, headers, post_id=post_id, activate=False)
    media = other_media(ig)
    ig.add_comment(media, "precio", author_id=PERSON)
    replies(sessions)
    clock.advance(minutes=1)
    client.post(f"/v1/rules/{rule['id']}/activate", headers=headers)

    bind(sessions, post_id, media, clock.now())
    replies(sessions)

    assert ig.sent == []
    assert [e.rule_id for e in events_of(sessions, media)] == [None]


def test_a_rule_never_activated_answers_nothing_and_events_come_back_with_no_rule(
    client, headers, ig, sessions, clock, replies
):
    post_id = make_post(client, headers)
    make_rule(client, headers, post_id=post_id, activate=False)
    media = other_media(ig)
    ig.add_comment(media, "precio", author_id=PERSON)
    replies(sessions)

    bind(sessions, post_id, media, clock.now())
    assert events_of(sessions, media) == []
    replies(sessions)

    assert ig.sent == []
    assert [e.rule_id for e in events_of(sessions, media)] == [None]


def test_reading_again_does_not_widen_the_reply_windows(
    client, headers, ig, sessions, clock, replies
):
    """Rule active, post stuck for two days: the public reply's 24 hours count from the comment."""
    post_id = make_post(client, headers)
    make_rule(client, headers, post_id=post_id, content=PUBLIC_TOO)
    media = other_media(ig)
    clock.advance(minutes=1)
    comment = ig.add_comment(media, "precio", author_id=PERSON)
    replies(sessions)
    clock.advance(days=2)

    bind(sessions, post_id, media, clock.now())
    replies(sessions)

    assert [(op, target) for op, target, _ in ig.sent] == [("reply_private", comment)]
    assert ("public_reply", "skipped", "window_closed") in outcomes(sessions)

    # Past the private reply's 7 days as well, nothing goes out.
    other_post = make_post(client, headers)
    make_rule(client, headers, post_id=other_post, content=PUBLIC_TOO)
    late_media = other_media(ig)
    ig.add_comment(late_media, "precio", author_id=PERSON)
    replies(sessions)
    clock.advance(days=8)
    sent_before = len(ig.sent)

    bind(sessions, other_post, late_media, clock.now())
    replies(sessions)

    assert len(ig.sent) == sent_before


def test_only_the_bound_media_is_forgotten(client, headers, ig, sessions, clock, replies):
    post_id = make_post(client, headers)
    make_rule(client, headers, post_id=post_id)
    media, elsewhere = other_media(ig), other_media(ig)
    ig.add_comment(media, "nice", author_id=PERSON)
    ig.add_comment(elsewhere, "nice", author_id=PERSON)
    replies(sessions)
    [kept] = events_of(sessions, elsewhere)

    bind(sessions, post_id, media, clock.now())

    assert events_of(sessions, media) == []
    assert [e.id for e in events_of(sessions, elsewhere)] == [kept.id]


def test_nothing_is_forgotten_when_no_rule_was_bound(client, headers, ig, sessions, clock, replies):
    post_id = make_post(client, headers)
    media = other_media(ig)
    ig.add_comment(media, "nice", author_id=PERSON)
    replies(sessions)
    [kept] = events_of(sessions, media)

    bind(sessions, post_id, media, clock.now())

    assert [e.id for e in events_of(sessions, media)] == [kept.id]


def test_a_rule_already_on_its_media_forgets_nothing(client, headers, ig, sessions, clock, replies):
    post_id = make_post(client, headers)
    set_status(sessions, post_id, "published", "m1")
    make_rule(client, headers, post_id=post_id)
    media = other_media(ig)
    ig.add_comment(media, "nice", author_id=PERSON)
    replies(sessions)
    [kept] = events_of(sessions, media)

    bind(sessions, post_id, media, clock.now())

    assert [e.id for e in events_of(sessions, media)] == [kept.id]


# Visibility


def test_a_warning_shows_when_the_post_is_not_going_out(client, headers, sessions):
    post_id = make_post(client, headers)
    rule = make_rule(client, headers, post_id=post_id, activate=False)

    def codes():
        return [w["code"] for w in get_rule(client, headers, rule["id"])["warnings"]]

    assert codes() == []

    assert client.post(f"/v1/posts/{post_id}/cancel", headers=headers).status_code == 200
    assert codes() == ["post_not_going_out"]
    message = get_rule(client, headers, rule["id"])["warnings"][0]["message"]
    assert "answers nothing until the post is published or the rule is changed" in message

    set_status(sessions, post_id, "failed")
    assert codes() == ["post_not_going_out"]
    listed = client.get("/v1/rules", headers=headers).json()
    assert [w["code"] for w in listed[0]["warnings"]] == ["post_not_going_out"]
    assert "post_status" not in listed[0]

    set_status(sessions, post_id, "scheduled")
    assert codes() == []


def test_a_waiting_rule_is_not_warned_as_public_on_every_post(client, headers):
    post_id = make_post(client, headers)

    rule = make_rule(client, headers, post_id=post_id, content=PUBLIC_TOO, activate=False)

    assert rule["warnings"] == []


def test_listing_rules_reads_the_posts_in_one_query(client, headers, app):
    for _ in range(3):
        make_rule(client, headers, post_id=make_post(client, headers), activate=False)
    statements = []

    def seen(_conn, _cursor, statement, *_):
        if "FROM posts" in statement:
            statements.append(statement)

    engine = app.state.sessionmaker.kw["bind"]
    event.listen(engine, "before_cursor_execute", seen)
    try:
        assert len(client.get("/v1/rules", headers=headers).json()) == 3
    finally:
        event.remove(engine, "before_cursor_execute", seen)

    assert len(statements) == 1


def test_test_rules_says_a_rule_is_waiting_and_what_it_would_match(client, headers):
    post_id = make_post(client, headers)
    rule = make_rule(client, headers, post_id=post_id, activate=True)

    def tried(**fields):
        body = {"channel": "comment", **fields}
        return client.post("/v1/rules/test", json=body, headers=headers).json()

    hit = tried(text="precio", rule_id=rule["id"])
    assert hit["rule"]["id"] == rule["id"]
    assert hit["private_replies"] == PRIVATE_ONLY["private_replies"]
    assert "waiting" in hit["note"] and post_id in hit["note"]

    miss = tried(text="nothing here", rule_id=rule["id"])
    assert miss["rule"] is None
    assert "waiting" in miss["note"]

    # Without rule_id it behaves as the worker: a waiting rule fires nothing.
    assert tried(text="precio", media_ref="m1")["rule"] is None
