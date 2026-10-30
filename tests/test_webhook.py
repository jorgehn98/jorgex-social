import hashlib
import hmac
import json
import logging
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from jorgex_social.access.tokens import create_token
from jorgex_social.account.connection import ConnectionTask
from jorgex_social.account.models import ChannelIncident, Connection
from jorgex_social.api import create_app
from jorgex_social.clock import ManualClock
from jorgex_social.instagram.fake import ACCOUNT_ID, FakeInstagram
from jorgex_social.instagram.types import ContainerKind, ContainerRequest
from jorgex_social.logs import JsonFormatter, hide_query_strings
from jorgex_social.replies.models import Conversation, Delivery, Event
from jorgex_social.replies.task import ReplyTask
from jorgex_social.replies.webhook import parse

START = datetime(2026, 10, 30, 12, tzinfo=UTC)
PERSON = "17900000000000042"
OTHERS = ["17900000000000077", "17900000000000078"]
ANOTHER_ACCOUNT = "17841499999999999"
VERIFY_TOKEN = "verify-token-for-tests"
APP_SECRET = "app-secret-for-tests"
COMMENT_RULE = {
    "keywords": ["precio"],
    "public_replies": ["Sent you a DM!", "Check your inbox", "Just wrote to you"],
    "private_replies": ["The price is on the website."],
}
MESSAGE_RULE = {"keywords": ["precio"], "private_replies": ["DM answer"]}
MARKERS = ["QZX-hook-comment-4411", "QZX-hook-message-8812", "qzx_hook_username_2290"]


@pytest.fixture
def clock():
    return ManualClock(START)


@pytest.fixture
def ig(clock):
    return FakeInstagram(clock)


@pytest.fixture
def settings(settings):
    return replace(settings, webhook_verify_token=VERIFY_TOKEN, meta_app_secret=APP_SECRET)


@pytest.fixture
def app(settings, clock):
    return create_app(settings, clock)


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def sessions(app):
    return app.state.sessionmaker


@pytest.fixture
def headers(app):
    with app.state.sessionmaker() as session, session.begin():
        _, secret = create_token(
            session, "agent", {"rules": "write", "events": "read", "account": "read"}
        )
    return {"Authorization": f"Bearer {secret}"}


@pytest.fixture
def task(ig, clock, sessions):
    # The connection check records the account's id, as it does first in the worker. Polling
    # every two minutes, as in production, so a pass in between only sends.
    ConnectionTask(ig, clock, None)(sessions)
    return ReplyTask(ig, clock, webhook=True)


def make_rule(client, headers, channel="comment", content=COMMENT_RULE):
    rule = client.post(
        "/v1/rules",
        json={"name": channel, "channel": channel, "content": content},
        headers=headers,
    ).json()
    client.post(f"/v1/rules/{rule['id']}/activate", headers=headers)


def publish(ig) -> str:
    container = ig.create_container(ContainerRequest(ContainerKind.IMAGE, url="https://x.test/a"))
    return ig.publish(container)


# Payloads shaped like Meta's examples for Business Login for Instagram
# ("Instagram webhook notification examples").


def comment_payload(comment_id, media_id, text, *, author=PERSON, at=START, **extra):
    return {
        "object": "instagram",
        "entry": [
            {
                "id": ACCOUNT_ID,
                "time": int(at.timestamp()),
                "field": "comments",
                "value": {
                    "id": comment_id,
                    "from": {"id": author, "username": MARKERS[2]},
                    "text": text,
                    "media": {"id": media_id, "media_product_type": "FEED"},
                    **extra,
                },
            }
        ],
    }


def message_payload(mid, text, *, sender=PERSON, recipient=ACCOUNT_ID, at=START, echo=False):
    millis = int(at.timestamp() * 1000)
    message = {"mid": mid, "text": text, **({"is_echo": True} if echo else {})}
    return {
        "object": "instagram",
        "entry": [
            {
                "id": ACCOUNT_ID,
                "time": millis,
                "messaging": [
                    {
                        "sender": {"id": sender},
                        "recipient": {"id": recipient},
                        "timestamp": millis,
                        "message": message,
                    }
                ],
            }
        ],
    }


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def notify(client, payload, *, signature: str | None = None):
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    # As bytes, so a test can send what a client could: a header that is not ASCII.
    headers["X-Hub-Signature-256"] = (sign(body) if signature is None else signature).encode(
        "latin-1"
    )
    if signature == "":
        del headers["X-Hub-Signature-256"]
    return client.post("/webhook", content=body, headers=headers)


def events(sessions):
    with sessions() as session:
        return session.scalars(select(Event).order_by(Event.received_at)).all()


def deliveries(sessions):
    with sessions() as session:
        return session.scalars(select(Delivery).order_by(Delivery.action)).all()


def conversation(sessions, person=PERSON):
    with sessions() as session:
        return session.scalar(select(Conversation).where(Conversation.external_id == person))


def open_incidents(sessions):
    with sessions() as session:
        return session.scalar(
            select(func.count()).where(
                ChannelIncident.type == "webhook_silent", ChannelIncident.closed_at.is_(None)
            )
        )


def dump(settings) -> str:
    """Every row of every table, plus the raw bytes of the database files (WAL included)."""
    with sqlite3.connect(settings.database_path) as con:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        rows = [repr(row) for t in tables for row in con.execute(f'SELECT * FROM "{t}"')]
    raw = b"".join(
        p.read_bytes()
        for p in settings.database_path.parent.glob(settings.database_path.name + "*")
    )
    return "\n".join(rows) + raw.decode("latin-1")


def logged(caplog) -> str:
    formatter = JsonFormatter()
    return "\n".join(formatter.format(record) for record in caplog.records)


# Subscription check


def test_verification_echoes_the_challenge_only_with_our_token(client):
    def check(token, mode="subscribe"):
        return client.get(
            "/webhook",
            params={"hub.mode": mode, "hub.verify_token": token, "hub.challenge": "1158201444"},
        )

    ok = check(VERIFY_TOKEN)
    assert (ok.status_code, ok.text) == (200, "1158201444")
    assert ok.headers["content-type"].startswith("text/plain")
    assert check("wrong-token").status_code == 403
    assert check("").status_code == 403
    assert check(VERIFY_TOKEN, mode="unsubscribe").status_code == 403


@pytest.mark.parametrize("missing", [{"webhook_verify_token": None}, {"meta_app_secret": None}])
def test_without_both_secrets_the_webhook_does_not_exist(settings, clock, missing):
    client = TestClient(create_app(replace(settings, **missing), clock))
    params = {"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "1"}

    assert client.get("/webhook", params=params).status_code == 404
    assert notify(client, comment_payload("1", "2", "precio")).status_code == 404


# Signature


@pytest.mark.parametrize(
    "signature",
    [
        "",  # no header
        "sha256=" + "0" * 64,
        "sha1=whatever",
        "not even close",
        "sha256=ünï",  # not ASCII
    ],
)
def test_without_a_valid_signature_nothing_is_recorded(client, ig, sessions, task, signature):
    media = publish(ig)
    comment_id = ig.add_comment(media, "precio")

    response = notify(client, comment_payload(comment_id, media, "precio"), signature=signature)

    assert response.status_code == 403
    assert events(sessions) == []


def test_a_body_signed_with_another_secret_is_refused(client, ig, sessions, task):
    payload = comment_payload("1", publish(ig), "precio")
    body = json.dumps(payload).encode()

    response = client.post(
        "/webhook", content=body, headers={"X-Hub-Signature-256": sign(body, "other-secret")}
    )

    assert response.status_code == 403
    assert events(sessions) == []


def test_an_oversized_body_is_refused_before_reading_it(client):
    body = b"x" * (1024 * 1024 + 1)

    response = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body)})

    assert response.status_code == 413


def test_an_oversized_body_without_a_declared_length_is_cut_off(client):
    chunks = (b"x" * 65536 for _ in range(17))  # sent chunked: no Content-Length

    response = client.post("/webhook", content=chunks, headers={"X-Hub-Signature-256": "x"})

    assert response.status_code == 413


# Recording and sending


def test_a_comment_notified_is_answered_on_the_next_pass_without_polling(
    client, headers, ig, sessions, task, clock
):
    make_rule(client, headers)
    media = publish(ig)
    task(sessions)  # the first poll, at start; the next is two minutes later
    clock.advance(seconds=5)
    comment_id = ig.add_comment(media, "precio?")
    polls = []
    original = ig.list_media
    ig.list_media = lambda max_items=None: polls.append(clock.now()) or original(max_items)

    assert notify(client, comment_payload(comment_id, media, "precio?", at=clock.now())).text == (
        "ok"
    )
    clock.advance(seconds=10)
    task(sessions)

    assert polls == []
    [event] = events(sessions)
    assert (event.source, event.external_ref, event.media_ref) == ("webhook", comment_id, media)
    assert {(d.action, d.status) for d in deliveries(sessions)} == {
        ("private_reply", "sent"),
        ("public_reply", "sent"),
    }


def test_a_comment_seen_by_webhook_and_by_poll_is_one_event_and_one_reply(
    client, headers, ig, sessions, task, clock
):
    make_rule(client, headers)
    media = publish(ig)
    comment_id = ig.add_comment(media, "precio")
    payload = comment_payload(comment_id, media, "precio")

    notify(client, payload)
    notify(client, payload)  # Meta retries
    task(sessions)  # polls, sees the same comment, sends
    clock.advance(minutes=2)
    notify(client, payload)
    task(sessions)

    [event] = events(sessions)
    assert event.source == "webhook"
    assert sorted(d.action for d in deliveries(sessions)) == ["private_reply", "public_reply"]
    assert sorted(op for op, _, _ in ig.sent) == ["reply_private", "reply_public"]


def test_a_comment_polled_first_keeps_poll_as_its_source(
    client, headers, ig, sessions, task, clock
):
    make_rule(client, headers)
    media = publish(ig)
    comment_id = ig.add_comment(media, "precio")

    task(sessions)
    notify(client, comment_payload(comment_id, media, "precio"))
    clock.advance(seconds=10)
    task(sessions)

    [event] = events(sessions)
    assert event.source == "poll"
    assert len(ig.sent) == 2


def test_a_message_seen_by_webhook_and_by_poll_is_one_event_and_one_reply(
    client, headers, ig, sessions, task, clock
):
    make_rule(client, headers, channel="message", content=MESSAGE_RULE)
    mid = ig.add_message(PERSON, "precio")

    notify(client, message_payload(mid, "precio"))
    task(sessions)

    [event] = events(sessions)
    assert (event.type, event.source, event.external_ref) == ("message", "webhook", mid)
    assert [(d.action, d.status) for d in deliveries(sessions)] == [("message", "sent")]
    assert conversation(sessions).window_until == START + timedelta(hours=24)


def test_the_accounts_own_comments_and_messages_fire_nothing(client, headers, ig, sessions, task):
    make_rule(client, headers)
    make_rule(client, headers, channel="message", content=MESSAGE_RULE)
    media = publish(ig)

    notify(client, comment_payload("901", media, "precio", author=ACCOUNT_ID))
    # Sent by the account but without is_echo: still the account's own.
    notify(client, message_payload("902", "precio", sender=ACCOUNT_ID, recipient=PERSON))

    assert events(sessions) == []
    assert deliveries(sessions) == []


def test_replies_to_comments_and_other_fields_are_ignored(client, headers, ig, sessions, task):
    make_rule(client, headers)
    media = publish(ig)

    notify(client, comment_payload("903", media, "precio", parent_id="900"))
    live = comment_payload("904", media, "precio")
    live["entry"][0]["field"] = "live_comments"
    notify(client, live)
    other_account = comment_payload("905", media, "precio")
    other_account["entry"][0]["id"] = ANOTHER_ACCOUNT
    notify(client, other_account)

    assert events(sessions) == []


def test_the_changes_wrapper_is_read_too(client, headers, ig, sessions, task):
    """Meta's generic and Unified Login examples wrap `field`/`value` in `changes`."""
    make_rule(client, headers)
    media = publish(ig)
    payload = comment_payload("906", media, "precio")
    entry = payload["entry"][0]
    entry["changes"] = [{"field": entry.pop("field"), "value": entry.pop("value")}]

    assert notify(client, payload).status_code == 200

    assert [e.external_ref for e in events(sessions)] == ["906"]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "text",
        {"object": "page", "entry": []},
        {"object": "instagram"},
        {"object": "instagram", "entry": "x"},
        {"object": "instagram", "entry": [None, 3, {"id": ACCOUNT_ID, "changes": "x"}]},
        {"object": "instagram", "entry": [{"id": ACCOUNT_ID, "changes": [None, {"field": 1}]}]},
        {"object": "instagram", "entry": [{"id": ACCOUNT_ID, "field": "comments", "value": []}]},
        {
            "object": "instagram",
            "entry": [{"id": ACCOUNT_ID, "field": "comments", "value": {"id": "1"}}],
        },
        {"object": "instagram", "entry": [{"id": ACCOUNT_ID, "messaging": [None, {"x": 1}]}]},
        {
            "object": "instagram",
            "entry": [
                {
                    "id": ACCOUNT_ID,
                    "messaging": [
                        {"sender": {"id": PERSON}, "message": {"mid": "m1"}},  # no timestamp
                        {"sender": "x", "timestamp": "soon", "message": {"mid": ["m2"]}},
                        {"sender": {"id": PERSON}, "timestamp": 10**30, "message": {"mid": "m3"}},
                        {"sender": {"id": PERSON}, "timestamp": 1, "reaction": {"mid": "m4"}},
                    ],
                }
            ],
        },
    ],
)
def test_unexpected_shapes_are_ignored_not_failed(client, ig, sessions, task, payload):
    assert notify(client, payload).status_code == 200
    assert events(sessions) == []


def test_a_signed_body_that_is_not_json_is_acknowledged(client):
    body = b"not json"

    response = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body)})

    assert response.status_code == 200


def test_message_timestamps_in_milliseconds_and_comment_times_in_seconds():
    millis = message_payload("m", "hi", at=START)
    seconds = comment_payload("c", "media", "hi", at=START)
    later = START + timedelta(hours=1)

    [message] = parse(millis, ACCOUNT_ID, later).messages
    [(_, comment)] = parse(seconds, ACCOUNT_ID, later).comments

    assert message.created_at == START
    assert comment.timestamp == START


def test_deleted_and_self_test_messages_are_not_activity():
    payload = message_payload("m", "precio")
    item = payload["entry"][0]["messaging"][0]
    deleted = json.loads(json.dumps(payload))
    item["message"]["is_self"] = True
    deleted["entry"][0]["messaging"][0]["message"]["is_deleted"] = True

    for case in (payload, deleted):
        notification = parse(case, ACCOUNT_ID, START)
        assert (notification.messages, notification.echoes) == ([], [])


# Human intervention


def test_an_echo_that_is_not_ours_pauses_the_conversation(
    client, headers, ig, sessions, task, clock
):
    make_rule(client, headers, channel="message", content=MESSAGE_RULE)
    ig.add_message(PERSON, "hola")
    task(sessions)  # records the conversation; no rule matches "hola"
    clock.advance(minutes=1)

    notify(
        client,
        message_payload(
            "human-1",
            "typed by hand",
            sender=ACCOUNT_ID,
            recipient=PERSON,
            at=clock.now(),
            echo=True,
        ),
    )
    clock.advance(minutes=1)
    mid = ig.add_message(PERSON, "precio")
    notify(client, message_payload(mid, "precio", at=clock.now()))
    task(sessions)

    paused = conversation(sessions)
    assert paused.paused_until == START + timedelta(minutes=31)
    assert [(d.status, d.reason) for d in deliveries(sessions)] == [("skipped", "paused")]
    assert ig.sent == []


def test_an_echo_of_our_own_reply_pauses_nothing(client, headers, ig, sessions, task, clock):
    make_rule(client, headers, channel="message", content=MESSAGE_RULE)
    ig.add_message(PERSON, "precio")
    task(sessions)
    [delivery] = deliveries(sessions)
    assert delivery.status == "sent"
    clock.advance(minutes=3)  # past the recent-send safety net: only the id match protects

    notify(
        client,
        message_payload(
            delivery.result_ref,
            "DM answer",
            sender=ACCOUNT_ID,
            recipient=PERSON,
            at=clock.now(),
            echo=True,
        ),
    )

    assert conversation(sessions).paused_until is None


def echo_after_our_send(client, headers, ig, sessions, task, clock, minutes):
    """Our rule answers the person, then an echo with an id we do not know arrives."""
    make_rule(client, headers, channel="message", content=MESSAGE_RULE)
    ig.add_message(PERSON, "precio")
    task(sessions)
    assert [d.status for d in deliveries(sessions)] == ["sent"]
    clock.advance(minutes=minutes)
    notify(
        client,
        message_payload(
            "unknown-mid", "x", sender=ACCOUNT_ID, recipient=PERSON, at=clock.now(), echo=True
        ),
    )
    return conversation(sessions).paused_until


def test_an_unknown_echo_just_after_our_send_is_taken_as_ours(
    client, headers, ig, sessions, task, clock
):
    assert echo_after_our_send(client, headers, ig, sessions, task, clock, minutes=1) is None


def test_an_unknown_echo_long_after_our_send_pauses(client, headers, ig, sessions, task, clock):
    paused_until = echo_after_our_send(client, headers, ig, sessions, task, clock, minutes=3)

    assert paused_until == START + timedelta(minutes=33)


def test_an_echo_while_a_reply_is_in_flight_pauses_nothing(
    client, headers, ig, sessions, task, clock
):
    """Meta may echo a reply before the worker has stored the id Instagram gave it."""
    make_rule(client, headers, channel="message", content=MESSAGE_RULE)
    ig.add_message(PERSON, "precio")
    original = ig.send_message
    echoed = []

    def send_and_echo(recipient_id, text):
        message_id = original(recipient_id, text)
        echoed.append(
            notify(
                client,
                message_payload(
                    message_id, text, sender=ACCOUNT_ID, recipient=PERSON, at=clock.now(), echo=True
                ),
            )
        )
        return message_id

    ig.send_message = send_and_echo
    task(sessions)

    assert [r.status_code for r in echoed] == [200]
    assert conversation(sessions).paused_until is None


def human_echo(client, clock, mid="typed-by-hand"):
    return notify(
        client,
        message_payload(mid, "x", sender=ACCOUNT_ID, recipient=PERSON, at=clock.now(), echo=True),
    )


def test_sends_to_other_conversations_do_not_block_a_human_pause(
    client, headers, ig, sessions, task, clock
):
    make_rule(client, headers, channel="message", content=MESSAGE_RULE)
    ig.add_message(PERSON, "hola")  # a conversation, and no rule matches
    for other in OTHERS:
        ig.add_message(other, "precio")
    original = ig.send_message
    echoed = []

    def send_and_echo(recipient_id, text):
        # While each reply to someone else is in flight, the operator answers PERSON by hand.
        echoed.append(human_echo(client, clock, mid=f"by-hand-{recipient_id}").status_code)
        return original(recipient_id, text)

    ig.send_message = send_and_echo
    task(sessions)

    assert echoed == [200, 200]
    assert conversation(sessions).paused_until == START + timedelta(minutes=30)


def test_an_echo_while_a_private_reply_is_in_flight_pauses_nothing(
    client, headers, ig, sessions, task, clock
):
    """A private reply's event has no conversation, so any one in flight holds every pause."""
    make_rule(client, headers)
    ig.add_message(PERSON, "hola")
    ig.add_comment(publish(ig), "precio", author_id=PERSON)
    original = ig.reply_private
    echoed = []

    def reply_and_echo(comment_id, text):
        message_id = original(comment_id, text)
        echoed.append(human_echo(client, clock, mid=message_id).status_code)
        return message_id

    ig.reply_private = reply_and_echo
    task(sessions)

    assert echoed == [200]
    assert conversation(sessions).paused_until is None


def test_an_echo_does_not_shorten_a_longer_pause(client, headers, ig, sessions, task, clock):
    ig.add_message(PERSON, "hola")
    task(sessions)
    [conv] = [conversation(sessions)]
    until = (START + timedelta(days=2)).isoformat()
    client.post(f"/v1/conversations/{conv.id}/pause", json={"until": until}, headers=headers)

    notify(client, message_payload("human-2", "x", sender=ACCOUNT_ID, recipient=PERSON, echo=True))

    assert conversation(sessions).paused_until == START + timedelta(days=2)


# Health


def test_webhook_silent_opens_after_a_silent_day_and_closes_on_the_next_notification(
    client, headers, ig, sessions, task, clock
):
    media = publish(ig)
    ig.add_comment(media, "hola")
    task(sessions)
    assert open_incidents(sessions) == 0  # the webhook has not had a day yet

    clock.advance(hours=24, minutes=1)
    ig.add_comment(media, "otra")
    task(sessions)
    assert open_incidents(sessions) == 1

    notify(client, comment_payload("907", media, "hola", at=clock.now()))
    assert open_incidents(sessions) == 0
    with sessions() as session:
        assert session.scalar(select(Connection.webhook_notified_at)) == clock.now()


def test_notifications_for_another_account_do_not_count_as_the_webhook_working(
    client, ig, sessions, task, clock, caplog
):
    caplog.set_level(logging.DEBUG)
    media = publish(ig)

    def foreign():
        payload = comment_payload("914", media, f"precio {MARKERS[0]}", at=clock.now())
        payload["entry"][0]["id"] = ANOTHER_ACCOUNT
        assert notify(client, payload).status_code == 200

    ig.add_comment(media, "hola")
    task(sessions)
    clock.advance(hours=23)
    foreign()
    clock.advance(hours=1, minutes=1)
    ig.add_comment(media, "otra")
    task(sessions)
    assert open_incidents(sessions) == 1
    foreign()

    assert open_incidents(sessions) == 1
    with sessions() as session:
        assert session.scalar(select(Connection.webhook_notified_at)) is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(getattr(r, "account_ids", None) == [ANOTHER_ACCOUNT] for r in warnings)
    assert MARKERS[0] not in logged(caplog)


def test_webhook_silent_needs_activity_the_webhook_missed(ig, sessions, task, clock):
    task(sessions)
    clock.advance(hours=25)
    task(sessions)

    assert open_incidents(sessions) == 0


def test_webhook_silent_stays_closed_while_notifications_arrive(client, ig, sessions, task, clock):
    media = publish(ig)
    clock.advance(hours=23)
    notify(client, comment_payload("908", media, "hola", at=clock.now()))
    clock.advance(hours=2)
    ig.add_comment(media, "hola")
    task(sessions)

    assert open_incidents(sessions) == 0


def test_without_the_webhook_configured_webhook_silent_never_opens(ig, sessions, clock):
    ConnectionTask(ig, clock, None)(sessions)
    task = ReplyTask(ig, clock)
    media = publish(ig)
    task(sessions)
    clock.advance(hours=25)
    ig.add_comment(media, "hola")
    task(sessions)

    assert open_incidents(sessions) == 0


def test_the_account_reports_the_webhook(client, headers, ig, sessions, task, clock):
    before = client.get("/v1/account", headers=headers).json()["webhook"]
    clock.advance(minutes=3)
    notify(client, comment_payload("909", publish(ig), "hola", at=clock.now()))
    after = client.get("/v1/account", headers=headers).json()["webhook"]

    assert before == {"configured": True, "last_notification_at": None}
    assert after == {"configured": True, "last_notification_at": "2026-10-30T12:03:00Z"}


# Privacy


def test_a_failure_while_processing_answers_500_without_quoting_it(
    client, app, ig, task, caplog, monkeypatch
):
    caplog.set_level(logging.DEBUG)

    def explode(*args, **kwargs):
        raise RuntimeError(f"could not store {MARKERS[0]}")

    monkeypatch.setattr(app.state.intake, "comments", explode)
    response = notify(client, comment_payload("915", publish(ig), f"precio {MARKERS[0]}"))

    assert response.status_code == 500  # Meta retries
    assert "RuntimeError" in logged(caplog)
    assert MARKERS[0] not in logged(caplog)


def test_the_access_log_never_shows_query_strings(app, caplog):
    """Through a real uvicorn server: its access log prints the query string, where Meta's check
    carries the verify token."""
    hide_query_strings()  # what `serve` does through logs.configure()
    caplog.set_level(logging.INFO, logger="uvicorn.access")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="off")
    )
    thread = threading.Thread(target=server.run)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.01)
        base = f"http://127.0.0.1:{port}"
        query = f"hub.mode=subscribe&hub.verify_token={VERIFY_TOKEN}&hub.challenge=77"
        with urllib.request.urlopen(f"{base}/webhook?{query}") as response:
            assert response.read() == b"77"
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"{base}/m/QZX-media-token.jpg?sig=QZX-query-5531")
    finally:
        server.should_exit = True
        thread.join(10)

    lines = [r.getMessage() for r in caplog.records if r.name == "uvicorn.access"]
    assert any('"GET /webhook HTTP/1.1" 200' in line for line in lines)
    assert any("/m/QZX-media-token.jpg HTTP/1.1" in line for line in lines)  # the path stays
    for text in (logged(caplog), *lines):
        assert VERIFY_TOKEN not in text
        assert "QZX-query-5531" not in text


def test_notifications_do_not_log_the_unauthenticated_request_line(client, ig, task, caplog):
    caplog.set_level(logging.INFO)

    notify(client, comment_payload("912", publish(ig), "hola"))
    notify(client, comment_payload("913", publish(ig), "hola"), signature="sha256=0")

    assert "unauthenticated" not in logged(caplog)
    assert "webhook notification" in logged(caplog)


def test_third_party_text_from_notifications_never_reaches_the_database_or_the_logs(
    client, headers, ig, sessions, task, settings, caplog, clock
):
    caplog.set_level(logging.DEBUG)
    make_rule(client, headers)
    make_rule(client, headers, channel="message", content=MESSAGE_RULE)
    media = publish(ig)
    comment_id = ig.add_comment(media, f"precio {MARKERS[0]}")
    mid = ig.add_message(PERSON, f"precio {MARKERS[1]}")
    valid = [
        comment_payload(comment_id, media, f"precio {MARKERS[0]}", author=OTHERS[0]),
        comment_payload("910", media, f"no keyword {MARKERS[0]}", author=OTHERS[0]),
        message_payload(mid, f"precio {MARKERS[1]}"),
        message_payload("911", MARKERS[1], sender=ACCOUNT_ID, recipient=PERSON, echo=True),
    ]

    for payload in valid:
        assert notify(client, payload).status_code == 200
    for payload in valid:
        assert notify(client, payload, signature="sha256=" + "0" * 64).status_code == 403
    clock.advance(seconds=10)
    task(sessions)

    # The comment's two replies; the message was skipped, since the echo paused the person.
    assert len(ig.sent) == 2
    assert len(events(sessions)) == 3
    stored, logs = dump(settings), logged(caplog)
    for marker in MARKERS:
        assert marker not in stored
        assert marker not in logs


# Opt-out


def test_an_opt_out_word_by_webhook_pauses_and_sends_nothing(
    client, headers, ig, sessions, task, clock
):
    make_rule(client, headers, channel="message", content=MESSAGE_RULE)
    first = ig.add_message(PERSON, "precio")
    notify(client, message_payload(first, "precio"))

    clock.advance(seconds=1)
    notify(client, message_payload("mid-stop", "STOP", at=clock.now()))
    task.deliver(sessions)

    paused = conversation(sessions)
    assert paused.paused_forever is True
    assert ig.sent == []
    assert [(d.status, d.reason) for d in deliveries(sessions)] == [("skipped", "paused")]
    assert events(sessions)[-1].rule_id is None


def test_the_webhook_uses_the_configured_opt_out_words_and_stores_none(settings, clock, ig):
    marker = "QZX-opt-out-3307"
    app = create_app(replace(settings, opt_out_words=(marker,)), clock)
    sessions = app.state.sessionmaker
    ConnectionTask(ig, clock, None)(sessions)

    notify(TestClient(app), message_payload("mid-1", marker))
    notify(TestClient(app), message_payload("mid-2", "stop", sender=OTHERS[0]))

    assert conversation(sessions).paused_forever is True
    assert conversation(sessions, OTHERS[0]).paused_forever is False
    assert marker not in dump(settings)


def test_an_empty_list_turns_opt_out_off_by_webhook(settings, clock, ig):
    app = create_app(replace(settings, opt_out_words=()), clock)
    sessions = app.state.sessionmaker
    ConnectionTask(ig, clock, None)(sessions)

    notify(TestClient(app), message_payload("mid-1", "stop"))

    assert conversation(sessions).paused_forever is False
