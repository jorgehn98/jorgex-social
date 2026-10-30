import json
import logging
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from jorgex_social.access.tokens import create_token
from jorgex_social.account import kill_switch
from jorgex_social.account.connection import ConnectionTask
from jorgex_social.account.models import AuditEntry, ChannelIncident, Connection
from jorgex_social.api import create_app
from jorgex_social.clock import ManualClock
from jorgex_social.instagram.client import InstagramClient
from jorgex_social.instagram.errors import AccountRestricted, RateLimited, Refused, Unavailable
from jorgex_social.instagram.fake import ACCOUNT_ID, FakeInstagram
from jorgex_social.instagram.types import ContainerKind, ContainerRequest
from jorgex_social.logs import JsonFormatter
from jorgex_social.replies.models import Conversation, Delivery, Event, Rule
from jorgex_social.replies.rules import first_match, matches
from jorgex_social.replies.task import ReplyTask

START = datetime(2026, 10, 29, 12, tzinfo=UTC)
PERSON = "17900000000000042"
OTHER_PERSON = "17900000000000043"
PRIVATE_ONLY = {"keywords": ["precio"], "private_replies": ["DM"]}
PRICE_RULE = {
    "keywords": ["precio", "how much"],
    "public_replies": ["Sent you a DM!", "Check your inbox"],
    "private_replies": ["The price is on the website."],
}


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
        _, secret = create_token(session, "agent", {"rules": "write", "events": "read"})
    return {"Authorization": f"Bearer {secret}"}


@pytest.fixture
def task(ig, clock, sessions):
    # The connection check records the account's id, as it does first in the worker.
    ConnectionTask(ig, clock, None)(sessions)
    return ReplyTask(ig, clock, poll_every=timedelta(0))


def publish(ig) -> str:
    container = ig.create_container(ContainerRequest(ContainerKind.IMAGE, url="https://x.test/a"))
    return ig.publish(container)


def make_rule(client, headers, channel="comment", content=PRICE_RULE, activate=True, **fields):
    response = client.post(
        "/v1/rules",
        json={"name": "price", "channel": channel, "content": content, **fields},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    rule = response.json()
    if activate:
        rule = client.post(f"/v1/rules/{rule['id']}/activate", headers=headers).json()
    return rule


def at(value: str) -> datetime:
    return datetime.fromisoformat(value)


def deliveries(sessions):
    with sessions() as session:
        return session.scalars(
            select(Delivery).order_by(Delivery.created_at, Delivery.action)
        ).all()


def by_action(sessions):
    return {d.action: (d.status, d.reason) for d in deliveries(sessions)}


def count(sessions, model):
    with sessions() as session:
        return session.scalar(select(func.count()).select_from(model))


# Matching


@pytest.mark.parametrize(
    ("content", "text", "expected"),
    [
        ({"keywords": ["precio"]}, "¿Cuál es el PRECIO?", True),
        ({"keywords": ["precio"]}, "cual es el précio", True),  # accents are ignored both ways
        ({"keywords": ["précio"]}, "PRECIO", True),
        ({"keywords": ["precio"]}, "los precios", False),  # word: not inside another word
        ({"keywords": ["precio"], "match": "contains"}, "los precios", True),
        ({"keywords": ["how much"]}, "So   HOW\nmuch is it", True),  # whitespace collapses
        ({"keywords": ["info"], "match": "exact"}, " INFO ", True),
        ({"keywords": ["info"], "match": "exact"}, "info please", False),
        ({"keywords": ["precio"], "exclude": ["gratis"]}, "precio gratis?", False),
        ({"keywords": ["precio", "price"]}, "price?", True),
    ],
)
def test_matching(content, text, expected):
    assert matches(content, text) is expected


def test_first_match_follows_priority_media_and_activation():
    def rule(rule_id, media_ref=None, activated_at=START):
        return Rule(
            id=rule_id,
            media_ref=media_ref,
            activated_at=activated_at,
            content={"keywords": ["precio"]},
        )

    rules = [rule("other-post", media_ref="m2"), rule("later", activated_at=START.replace(hour=13))]
    rules.append(rule("global"))

    assert first_match(rules, "precio", media_ref="m1").id == "later"
    assert first_match(rules, "precio", media_ref="m2").id == "other-post"
    # Written before "later" was activated: only the rule active by then fires.
    assert first_match(rules, "precio", media_ref="m1", occurred_at=START).id == "global"
    assert first_match(rules, "nada", media_ref="m1") is None


# Rules API


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"channel": "comment", "content": {"keywords": ["x"]}}, "at least one"),
        (
            {"channel": "message", "content": {**PRICE_RULE, "private_replies": ["ok"]}},
            "public_replies",
        ),
        (
            {
                "channel": "message",
                "media_ref": "m1",
                "content": {"keywords": ["x"], "private_replies": ["ok"]},
            },
            "media_ref",
        ),
        # 501 characters, 1,002 bytes: the message limit is in bytes.
        (
            {"channel": "comment", "content": {"keywords": ["x"], "private_replies": ["é" * 501]}},
            "bytes",
        ),
        (
            {"channel": "comment", "content": {"keywords": ["x"], "public_replies": ["a"] * 21}},
            "20",
        ),
        (
            {"channel": "comment", "content": {"keywords": [" "], "public_replies": ["a"]}},
            "1 character",
        ),
        ({"channel": "comment", "content": {"keywords": ["x"], "public_replies": ["  "]}}, "blank"),
        # Only a combining accent: nothing is left once accents are ignored.
        (
            {"channel": "comment", "content": {"keywords": ["\u0301"], "public_replies": ["a"]}},
            "letters",
        ),
        (
            {
                "channel": "comment",
                "content": {"keywords": ["x"], "exclude": ["\u0301"], "public_replies": ["a"]},
            },
            "letters",
        ),
    ],
)
def test_invalid_rules_are_refused(client, headers, body, message):
    response = client.post("/v1/rules", json={"name": "r", **body}, headers=headers)

    assert response.status_code == 422
    assert message in response.text
    assert client.get("/v1/rules", headers=headers).json() == []


def test_rule_lifecycle(client, headers, clock):
    rule = make_rule(client, headers, activate=False)
    assert (rule["active"], rule["activated_at"]) == (False, None)

    clock.advance(minutes=5)
    active = client.post(f"/v1/rules/{rule['id']}/activate", headers=headers).json()
    edited = client.patch(
        f"/v1/rules/{rule['id']}", json={"priority": 5, "media_ref": "m1"}, headers=headers
    ).json()
    clock.advance(minutes=5)
    again = client.post(f"/v1/rules/{rule['id']}/activate", headers=headers).json()
    archived = client.post(f"/v1/rules/{rule['id']}/archive", headers=headers).json()
    refused = client.patch(f"/v1/rules/{rule['id']}", json={"name": "x"}, headers=headers)

    assert active["active"] and at(active["activated_at"]) == START + timedelta(minutes=5)
    assert (edited["priority"], edited["media_ref"], edited["active"]) == (5, "m1", True)
    assert again["activated_at"] == active["activated_at"]  # already active: unchanged
    assert (archived["active"], archived["archived_at"] is not None) == (False, True)
    assert refused.status_code == 409
    assert client.get("/v1/rules", headers=headers).json() == []
    assert [r["id"] for r in client.get("/v1/rules?archived=true", headers=headers).json()] == [
        rule["id"]
    ]


def test_rule_test_endpoint_stores_nothing(client, headers, settings):
    inactive = make_rule(client, headers, activate=False)
    make_rule(client, headers, content={"keywords": ["hola"], "private_replies": ["Hi!"]})

    by_active = client.post(
        "/v1/rules/test", json={"channel": "comment", "text": "hola y precio"}, headers=headers
    ).json()
    by_id = client.post(
        "/v1/rules/test",
        json={"channel": "comment", "text": "precio", "rule_id": inactive["id"]},
        headers=headers,
    ).json()
    none = client.post(
        "/v1/rules/test", json={"channel": "message", "text": "hola"}, headers=headers
    ).json()

    assert (by_active["rule"]["name"], by_active["private_replies"]) == ("price", ["Hi!"])
    assert by_id["rule"]["id"] == inactive["id"]
    assert by_id["public_replies"] == PRICE_RULE["public_replies"]
    assert none == {"rule": None, "public_replies": [], "private_replies": [], "note": None}
    assert "hola y precio" not in dump(settings)
    assert count_rows(settings, "events") == count_rows(settings, "deliveries") == 0


def test_permissions(app, client, headers):
    with app.state.sessionmaker() as session, session.begin():
        _, secret = create_token(session, "reader", {"rules": "read", "events": "read"})
    reader = {"Authorization": f"Bearer {secret}"}

    assert client.get("/v1/rules", headers=reader).status_code == 200
    assert client.get("/v1/deliveries", headers=reader).status_code == 200
    assert client.post("/v1/rules", json={}, headers=reader).status_code == 403
    assert client.post("/v1/conversations/x/pause", json={}, headers=reader).status_code == 403


# The cycle


def test_a_matching_comment_gets_both_replies_from_the_operator_variants(
    client, headers, ig, sessions, task
):
    make_rule(client, headers)
    media = publish(ig)
    comment = ig.add_comment(media, "Hola, ¿qué PRECIO tiene?")
    ig.add_comment(media, "Nice photo")

    task(sessions)

    sent = {(op, target) for op, target, _ in ig.sent}
    assert sent == {("reply_public", comment), ("reply_private", comment)}
    variants = PRICE_RULE["public_replies"] + PRICE_RULE["private_replies"]
    assert all(text in variants for _, _, text in ig.sent)
    assert by_action(sessions) == {"public_reply": ("sent", None), "private_reply": ("sent", None)}
    events = client.get("/v1/events", headers=headers).json()
    assert sorted(e["rule_id"] is not None for e in events) == [False, True]
    assert client.get("/v1/deliveries?status=sent", headers=headers).json()[0]["result_ref"]


def test_a_matching_message_is_answered_and_its_window_recorded(
    client, headers, ig, sessions, task, clock
):
    make_rule(
        client,
        headers,
        channel="message",
        content={"keywords": ["info"], "private_replies": ["Here it is"]},
    )
    ig.add_message(PERSON, "info please")

    task(sessions)

    assert ig.sent == [("send_message", PERSON, "Here it is")]
    [conversation] = client.get("/v1/conversations", headers=headers).json()
    assert conversation["external_id"] == PERSON
    assert at(conversation["window_until"]) == START + timedelta(hours=24)
    [delivery] = deliveries(sessions)
    assert delivery.window_until_at_send == START + timedelta(hours=24)

    clock.advance(hours=3)
    ig.add_message(PERSON, "thanks")
    task(sessions)
    [conversation] = client.get("/v1/conversations", headers=headers).json()
    assert at(conversation["window_until"]) == START + timedelta(hours=27)


# Invariants


MARKERS = ["QZX-comment-text-7731", "QZX-message-text-5523", "qzx_author_username_9917"]


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


def count_rows(settings, table) -> int:
    with sqlite3.connect(settings.database_path) as con:
        return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def logged(caplog) -> str:
    formatter = JsonFormatter()
    return "\n".join(formatter.format(record) for record in caplog.records)


def test_third_party_text_never_reaches_the_database_or_the_logs(
    client, headers, ig, sessions, task, settings, caplog, clock
):
    caplog.set_level(logging.DEBUG)
    make_rule(client, headers)
    make_rule(
        client,
        headers,
        channel="message",
        content={"keywords": ["precio"], "private_replies": ["DM answer"]},
    )
    media = publish(ig)
    ig.add_comment(media, f"precio {MARKERS[0]}")
    ig.add_comment(media, f"no keyword {MARKERS[0]}")
    ig.add_message(PERSON, f"precio {MARKERS[1]}")
    ig.fail_next("reply_public", Refused("Instagram API 400: refused"), after=False)

    task(sessions)
    clock.advance(hours=1)
    task(sessions)

    assert len(ig.sent) == 2  # the private reply and the message; the public one was refused
    stored, logs = dump(settings), logged(caplog)
    for marker in MARKERS[:2]:
        assert marker not in stored
        assert marker not in logs
    assert count_rows(settings, "events") == 3


def test_third_party_text_and_usernames_from_the_real_client_are_not_kept(
    sessions, settings, caplog, clock
):
    """The same, through the real client's parsing of payloads shaped like Meta's, which carry
    the author's username."""
    caplog.set_level(logging.DEBUG)
    now = clock.now()
    meta = FakeMeta(now)
    ig = InstagramClient("token-not-real", "1784", transport=meta)
    with sessions() as session, session.begin():
        session.add(Connection(network="instagram", external_account_id="1784", username="own"))
        for channel, content in (
            ("comment", PRICE_RULE),
            ("message", {"keywords": ["precio"], "private_replies": ["DM answer"]}),
        ):
            session.add(
                Rule(
                    network="instagram",
                    name=channel,
                    channel=channel,
                    priority=1,
                    active=True,
                    activated_at=now - timedelta(days=1),
                    content=content,
                    created_at=now,
                    updated_at=now,
                )
            )

    ReplyTask(ig, clock, poll_every=timedelta(0))(sessions)

    posted = [parse_qs(body.decode()) for method, _, body in meta.calls if method == "POST"]
    assert len(posted) == 3  # public reply, private reply, message
    sent_texts = [
        json.loads(p["message"][0])["text"] if "recipient" in p else p["message"][0] for p in posted
    ]
    assert set(sent_texts) <= set(
        PRICE_RULE["public_replies"] + PRICE_RULE["private_replies"] + ["DM answer"]
    )
    stored, logs = dump(settings), logged(caplog)
    for marker in MARKERS:
        assert marker not in stored
        assert marker not in logs
        assert all(marker not in text for text in sent_texts)
    assert count_rows(settings, "events") == 2  # the account's own comment is not an event


class FakeMeta:
    """Answers the reply endpoints like graph.instagram.com, with synthetic third-party data."""

    def __init__(self, now):
        stamp = now.strftime("%Y-%m-%dT%H:%M:%S+0000")
        self.calls = []
        self.pages = {
            "/1784/media": {
                "data": [
                    {
                        "id": "m1",
                        "media_product_type": "FEED",
                        "media_type": "IMAGE",
                        "timestamp": stamp,
                    }
                ]
            },
            "/m1/comments": {
                "data": [
                    {
                        "id": "c1",
                        "text": f"precio {MARKERS[0]}",
                        "timestamp": stamp,
                        "from": {"id": "u1", "username": MARKERS[2]},
                    },
                    {
                        "id": "c2",
                        "text": "precio, says the account itself",
                        "timestamp": stamp,
                        "from": {"id": "1784", "username": "own"},
                    },
                ]
            },
            "/1784/conversations": {"data": [{"id": "t1", "updated_time": stamp}]},
            "/t1": {
                "messages": {
                    "data": [
                        {
                            "id": "msg1",
                            "created_time": stamp,
                            "from": {"id": "u2", "username": MARKERS[2]},
                            "message": f"precio {MARKERS[1]}",
                        }
                    ]
                }
            },
        }

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, body))
        path = urlsplit(url).path.removeprefix("/v26.0")
        if method == "POST":
            reply = {"id": "r1"} if path.endswith("/replies") else {"message_id": "mid1"}
            return 200, json.dumps(reply).encode()
        return 200, json.dumps(self.pages[path]).encode()


def test_one_private_reply_per_comment_even_across_restarts(
    client, headers, ig, sessions, task, clock
):
    make_rule(client, headers)
    media = publish(ig)
    ig.add_comment(media, "precio")

    task(sessions)
    restarted = ReplyTask(ig, clock, poll_every=timedelta(0))
    restarted(sessions)
    clock.advance(minutes=5)
    restarted(sessions)

    assert [op for op, _, _ in ig.sent].count("reply_private") == 1
    assert count(sessions, Event) == 1
    # And the database refuses a second one, whichever rule it would come from.
    [private] = [d for d in deliveries(sessions) if d.action == "private_reply"]
    other = make_rule(client, headers, content={"keywords": ["x"], "private_replies": ["y"]})
    with pytest.raises(IntegrityError), sessions() as session, session.begin():
        session.add(
            Delivery(
                event_id=private.event_id,
                rule_id=other["id"],
                action="private_reply",
                text="y",
                created_at=clock.now(),
            )
        )


def test_a_send_interrupted_by_a_crash_is_never_repeated(
    client, headers, ig, sessions, task, clock
):
    make_rule(client, headers, content={"keywords": ["precio"], "private_replies": ["DM"]})
    media = publish(ig)
    ig.add_comment(media, "precio")
    original = ig.reply_private

    def crash_after_sending(comment_id, text):
        original(comment_id, text)
        raise RuntimeError("the worker died")

    ig.reply_private = crash_after_sending
    task(sessions)  # the failure is contained: the cycle goes on
    ig.reply_private = original
    ReplyTask(ig, clock, poll_every=timedelta(0))(sessions)

    assert len(ig.sent) == 1
    assert by_action(sessions) == {"private_reply": ("failed", "unknown")}


def test_the_accounts_own_comments_and_messages_never_fire_a_rule(
    client, headers, ig, sessions, task
):
    make_rule(client, headers)
    # The private reply below lands in the person's conversation as the account's message, and
    # contains the message rule's keyword: answering it would be a loop.
    make_rule(
        client,
        headers,
        channel="message",
        content={"keywords": ["price"], "private_replies": ["Our price list"]},
    )
    media = publish(ig)
    ig.add_comment(media, "precio, says the account", author_id=ACCOUNT_ID)
    ig.add_comment(media, "how much?", author_id=PERSON)

    task(sessions)
    task(sessions)

    assert [op for op, _, _ in ig.sent] == ["reply_public", "reply_private"]
    with sessions() as session:
        assert session.scalars(select(Event.type)).all() == ["comment"]


def test_a_rule_does_not_answer_what_came_before_its_activation(
    client, headers, ig, sessions, task, clock
):
    rule = make_rule(client, headers, activate=False)
    media = publish(ig)
    ig.add_comment(media, "precio")
    clock.advance(minutes=1)
    client.post(f"/v1/rules/{rule['id']}/activate", headers=headers)

    task(sessions)
    clock.advance(minutes=1)
    later = ig.add_comment(media, "precio")
    task(sessions)

    assert {target for _, target, _ in ig.sent} == {later}
    assert len(client.get("/v1/events?matched=false", headers=headers).json()) == 1


def test_kill_switch_skips_with_its_reason(client, headers, ig, sessions, task, clock):
    make_rule(client, headers)
    ig.add_comment(publish(ig), "precio")
    with sessions() as session, session.begin():
        kill_switch.set_state(session, True, clock.now())

    task(sessions)

    assert ig.sent == []
    assert by_action(sessions) == {
        "public_reply": ("skipped", "kill_switch"),
        "private_reply": ("skipped", "kill_switch"),
    }


def test_a_paused_conversation_is_skipped(client, headers, ig, sessions, task, clock):
    make_rule(
        client,
        headers,
        channel="message",
        content={"keywords": ["info"], "private_replies": ["Hi"]},
    )
    ig.add_message(PERSON, "info")
    task.poll_messages(sessions, ACCOUNT_ID, clock.now())
    [conversation] = client.get("/v1/conversations", headers=headers).json()

    paused = client.post(
        f"/v1/conversations/{conversation['id']}/pause", json={"forever": True}, headers=headers
    )
    task.deliver(sessions)
    resumed = client.post(f"/v1/conversations/{conversation['id']}/resume", headers=headers)

    assert paused.json()["paused_forever"] is True
    assert resumed.json()["paused_forever"] is False
    assert ig.sent == []
    assert by_action(sessions) == {"message": ("skipped", "paused")}
    assert client.get("/v1/conversations?paused=true", headers=headers).json() == []


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({}, "invalid_value"),
        ({"forever": True, "until": "2027-01-01T00:00:00Z"}, "invalid_value"),
        ({"until": "2020-01-01T00:00:00Z"}, "in_the_past"),
    ],
)
def test_pause_needs_until_or_forever(client, headers, body, code):
    response = client.post("/v1/conversations/nope/pause", json=body, headers=headers)

    assert (response.status_code, response.json()["detail"]["code"]) == (422, code)


def test_windows_close(client, headers, ig, sessions, task, clock):
    make_rule(client, headers, content={"keywords": ["precio"], "private_replies": ["DM"]})
    make_rule(
        client,
        headers,
        channel="message",
        content={"keywords": ["info"], "private_replies": ["Hi"]},
    )
    ig.add_comment(publish(ig), "precio")
    ig.add_message(PERSON, "info")
    task.poll_comments(sessions, ACCOUNT_ID, clock.now())
    task.poll_messages(sessions, ACCOUNT_ID, clock.now())

    clock.advance(days=7, seconds=1)
    task.deliver(sessions)

    assert ig.sent == []
    assert by_action(sessions) == {
        "private_reply": ("skipped", "window_closed"),
        "message": ("skipped", "window_closed"),
    }


def test_the_hourly_cap_skips_the_excess(client, headers, ig, sessions, clock):
    ConnectionTask(ig, clock, None)(sessions)
    capped = ReplyTask(ig, clock, poll_every=timedelta(0), hourly_cap=1)
    make_rule(client, headers, content={"keywords": ["precio"], "private_replies": ["DM"]})
    media = publish(ig)
    ig.add_comment(media, "precio", author_id=PERSON)
    ig.add_comment(media, "precio", author_id=OTHER_PERSON)

    capped(sessions)

    assert len(ig.sent) == 1
    assert sorted(by_status(sessions)) == [("sent", None), ("skipped", "hourly_cap")]


def by_status(sessions):
    return [(d.status, d.reason) for d in deliveries(sessions)]


def test_an_unavailable_send_is_never_retried(client, headers, ig, sessions, task, clock):
    make_rule(client, headers, content={"keywords": ["precio"], "private_replies": ["DM"]})
    ig.add_comment(publish(ig), "precio")
    # Instagram took it and then the connection broke: it did go out.
    ig.fail_next("reply_private", Unavailable("Instagram API 502"), after=True)

    task(sessions)
    clock.advance(minutes=5)
    task(sessions)

    assert len(ig.sent) == 1
    assert by_action(sessions) == {"private_reply": ("failed", "unknown")}


def test_a_refused_send_records_metas_code(client, headers, ig, sessions, task):
    make_rule(client, headers, content={"keywords": ["precio"], "private_replies": ["DM"]})
    ig.add_comment(publish(ig), "precio")
    ig.fail_next("reply_private", Refused("Instagram API 400", code=10, subcode=2534022))

    task(sessions)

    [delivery] = deliveries(sessions)
    assert (delivery.status, delivery.reason, delivery.error_code) == ("failed", "refused", 10)
    assert delivery.error_subcode == 2534022


def test_a_rate_limited_send_waits_for_the_next_poll(client, headers, ig, sessions, task):
    make_rule(client, headers, content={"keywords": ["precio"], "private_replies": ["DM"]})
    ig.add_comment(publish(ig), "precio")
    ig.fail_next("reply_private", RateLimited("Instagram API 429", code=4))

    task(sessions)
    assert by_action(sessions) == {"private_reply": ("pending", None)}
    task(sessions)

    assert by_action(sessions) == {"private_reply": ("sent", None)}
    assert len(ig.sent) == 1


def test_sends_on_every_pass_but_waits_for_the_next_poll_after_a_rate_limit(
    client, headers, ig, sessions, clock
):
    ConnectionTask(ig, clock, None)(sessions)
    task = ReplyTask(ig, clock)  # polls every 120 seconds
    make_rule(client, headers)
    media = publish(ig)
    task(sessions)
    ig.add_comment(media, "precio")
    clock.advance(seconds=120)
    ig.fail_next("reply_public", RateLimited("Instagram API 429", code=4))
    task(sessions)  # polls, the first send is rate limited
    calls = []
    original = ig.reply_public
    ig.reply_public = lambda *args: calls.append(clock.now()) or original(*args)

    clock.advance(seconds=10)
    task(sessions)  # a pass without a poll: still holding
    assert calls == []
    clock.advance(seconds=110)
    task(sessions)

    assert calls == [START + timedelta(seconds=240)]
    assert by_action(sessions) == {"public_reply": ("sent", None), "private_reply": ("sent", None)}


def test_polls_at_most_once_per_interval(ig, sessions, clock):
    ConnectionTask(ig, clock, None)(sessions)
    task = ReplyTask(ig, clock)  # every 120 seconds
    polls = []
    original = ig.list_media
    ig.list_media = lambda max_items=None: polls.append(clock.now()) or original(max_items)

    task(sessions)
    clock.advance(seconds=119)
    task(sessions)
    clock.advance(seconds=1)
    task(sessions)

    assert polls == [START, START + timedelta(seconds=120)]


def test_without_a_connected_account_nothing_is_polled(ig, sessions, clock):
    ig.list_media = lambda max_items=None: pytest.fail("polled without knowing the account")

    ReplyTask(ig, clock)(sessions)


def test_retention_purges_events_deliveries_and_conversations(
    client, headers, ig, sessions, task, clock
):
    make_rule(client, headers)
    make_rule(
        client,
        headers,
        channel="message",
        content={"keywords": ["info"], "private_replies": ["Hi"]},
    )
    ig.add_comment(publish(ig), "precio")
    ig.add_message(PERSON, "info")
    ig.add_message("17900000000000077", "hello")
    task(sessions)
    kept = client.get("/v1/conversations", headers=headers).json()[1]
    client.post(f"/v1/conversations/{kept['id']}/pause", json={"forever": True}, headers=headers)

    clock.advance(days=89)
    task(sessions)
    assert (count(sessions, Event), count(sessions, Conversation)) == (3, 2)
    clock.advance(days=1, seconds=1)
    task(sessions)

    assert (count(sessions, Event), count(sessions, Delivery)) == (0, 0)
    # The paused one stays while paused: purging it would lift the pause.
    with sessions() as session:
        assert session.scalars(select(Conversation.id)).all() == [kept["id"]]


def test_comment_and_message_text_stay_out_of_repr(ig):
    media = publish(ig)
    ig.add_comment(media, MARKERS[0])
    ig.add_message(PERSON, MARKERS[1])

    [comment] = ig.list_comments(media)
    [conversation] = ig.list_conversations()
    [message] = ig.list_messages(conversation.id)

    assert comment.text == MARKERS[0] and message.text == MARKERS[1]
    assert MARKERS[0] not in repr(comment) and MARKERS[1] not in repr(message)


def test_a_message_rule_answers_a_person_once_per_cooldown(
    client, headers, ig, sessions, task, clock
):
    make_rule(
        client,
        headers,
        channel="message",
        content={"keywords": ["info"], "private_replies": ["Here it is"]},
    )
    ig.add_message(PERSON, "info")
    ig.add_message(PERSON, "info?")
    ig.add_message("17900000000000077", "info")  # someone else is not held back

    task(sessions)
    clock.advance(minutes=59)
    ig.add_message(PERSON, "info!!")
    task(sessions)
    clock.advance(minutes=2)
    ig.add_message(PERSON, "info again")
    task(sessions)

    assert [target for _, target, _ in ig.sent] == [PERSON, "17900000000000077", PERSON]
    skipped = client.get("/v1/deliveries?reason=cooldown", headers=headers).json()
    assert len(skipped) == 2


def test_a_rule_test_says_when_the_rule_needs_another_media(client, headers):
    rule = make_rule(client, headers, media_ref="m1", activate=False)

    def tried(**extra):
        body = {"channel": "comment", "text": "precio", "rule_id": rule["id"], **extra}
        return client.post("/v1/rules/test", json=body, headers=headers).json()

    assert tried()["rule"] is None and "m1" in tried()["note"]
    assert "m1" in tried(media_ref="m2")["note"]
    assert (tried(media_ref="m1")["rule"]["id"], tried(media_ref="m1")["note"]) == (
        rule["id"],
        None,
    )


# Retention and polling scope


def test_activity_near_the_retention_limit_is_never_evaluated(client, headers, ig, sessions, clock):
    """With retention of 8 days, a purged event is not seen as new and answered again."""
    ConnectionTask(ig, clock, None)(sessions)
    task = ReplyTask(ig, clock, poll_every=timedelta(0), retention=timedelta(days=8))
    make_rule(client, headers)
    media = publish(ig)
    clock.advance(minutes=1)
    ig.add_comment(media, "precio")
    task(sessions)
    assert len(ig.sent) == 2

    clock.advance(days=8, minutes=1)
    task(sessions)  # purges the event and its deliveries
    assert (count(sessions, Event), count(sessions, Delivery)) == (0, 0)
    clock.advance(minutes=5)
    task(sessions)

    assert len(ig.sent) == 2
    assert count(sessions, Event) == 0


def test_a_comment_older_than_the_horizon_records_nothing(client, headers, ig, sessions, clock):
    ConnectionTask(ig, clock, None)(sessions)
    task = ReplyTask(ig, clock, poll_every=timedelta(0), retention=timedelta(days=8))
    make_rule(client, headers)
    media = publish(ig)
    ig.add_comment(media, "precio", at=clock.now())
    clock.advance(days=9)

    task(sessions)

    assert ig.sent == []
    assert count(sessions, Event) == 0


def test_comments_are_polled_on_recent_media_and_on_media_a_rule_names(
    client, headers, ig, sessions, task, clock
):
    old, unnamed = publish(ig), publish(ig)
    clock.advance(days=15)
    recent = publish(ig)
    clock.advance(days=20)  # old and unnamed are 35 days old, recent 20
    make_rule(client, headers, content={"keywords": ["precio"], "public_replies": ["A"]})
    make_rule(
        client, headers, media_ref=old, content={"keywords": ["info"], "public_replies": ["B"]}
    )
    on_old, on_recent = ig.add_comment(old, "precio"), ig.add_comment(recent, "precio")
    ig.add_comment(unnamed, "precio")

    task(sessions)

    assert {target for _, target, _ in ig.sent} == {on_old, on_recent}
    with sessions() as session:
        assert set(session.scalars(select(Event.media_ref))) == {old, recent}


# Robustness


def test_one_failing_media_or_conversation_stops_nothing_else(
    client, headers, ig, sessions, task, caplog
):
    make_rule(client, headers, content={"keywords": ["precio"], "public_replies": ["A"]})
    make_rule(
        client, headers, channel="message", content={"keywords": ["info"], "private_replies": ["B"]}
    )
    broken, fine = publish(ig), publish(ig)
    ig.add_comment(broken, f"precio {MARKERS[0]}")
    on_fine = ig.add_comment(fine, "precio")
    ig.add_message(PERSON, f"info {MARKERS[1]}")
    ig.add_message("17900000000000077", "info")
    list_comments, list_messages = ig.list_comments, ig.list_messages
    first_conversation = ig.list_conversations()[0].id

    def odd_comments(media_id):
        if media_id == broken:
            raise ValueError(f"could not parse {MARKERS[0]}")
        return list_comments(media_id)

    def odd_messages(conversation_id):
        if conversation_id == first_conversation:
            raise ValueError(f"could not parse {MARKERS[1]}")
        return list_messages(conversation_id)

    ig.list_comments, ig.list_messages = odd_comments, odd_messages
    task(sessions)

    assert ("reply_public", on_fine, "A") in ig.sent
    assert [op for op, _, _ in ig.sent].count("send_message") == 1
    assert all(marker not in logged(caplog) for marker in MARKERS)


def test_malformed_and_repeated_comments_from_meta_are_handled(sessions, clock):
    now = clock.now()
    meta = FakeMeta(now)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S+0000")
    good = {"id": "c1", "text": "precio", "timestamp": stamp, "from": {"id": "u1"}}
    meta.pages["/m1/comments"] = {
        "data": [{"id": "c0", "text": "precio", "from": {"id": "u1"}}, good],
        "paging": {"cursors": {"after": "A"}, "next": "https://graph.instagram.com/next"},
    }
    second_page = {"data": [good]}  # the same comment again on the next page
    original = meta.__call__

    def transport(method, url, headers, body):
        if "after=A" in url:
            return 200, json.dumps(second_page).encode()
        return original(method, url, headers, body)

    ig = InstagramClient("token-not-real", "1784", transport=transport)
    with sessions() as session, session.begin():
        session.add(Connection(network="instagram", external_account_id="1784", username="own"))
        session.add(
            Rule(
                network="instagram",
                name="price",
                channel="comment",
                priority=1,
                active=True,
                activated_at=now - timedelta(days=1),
                content={"keywords": ["precio"], "public_replies": ["A"]},
                created_at=now,
                updated_at=now,
            )
        )

    ReplyTask(ig, clock, poll_every=timedelta(0))(sessions)

    with sessions() as session:
        assert session.scalars(select(Event.external_ref).where(Event.type == "comment")).all() == [
            "c1"
        ]
    assert [url for method, url, _ in meta.calls if method == "POST"][0].endswith("/c1/replies")


def test_a_message_that_is_one_of_our_sends_never_fires_a_rule(client, headers, ig, sessions, task):
    """Even when Instagram names its sender with an id the connection does not hold."""
    make_rule(client, headers, content={"keywords": ["precio"], "private_replies": ["precio: 10"]})
    make_rule(
        client,
        headers,
        channel="message",
        content={"keywords": ["precio"], "private_replies": ["B"]},
    )
    ig.add_comment(publish(ig), "precio", author_id=PERSON)
    task(sessions)
    [thread] = ig._threads.values()
    thread.messages = [replace(m, sender_id="another-id-for-us") for m in thread.messages]

    task(sessions)

    assert [op for op, _, _ in ig.sent] == ["reply_private"]
    with sessions() as session:
        assert session.scalars(select(Event.type)).all() == ["comment"]


# Protecting the account

RESTRICTED = AccountRestricted("restricted", code=10, subcode=1893063)


def refuse_every_send(ig, error):
    """The fake refuses every write from now on; returns the calls that reached it."""
    calls = []

    def refuse(target, text):
        calls.append(target)
        raise error

    ig.reply_public = ig.reply_private = ig.send_message = refuse
    return calls


def restriction_incidents(sessions):
    with sessions() as session:
        return session.scalars(
            select(ChannelIncident)
            .where(ChannelIncident.type == "account_restricted")
            .order_by(ChannelIncident.opened_at)
        ).all()


def kill_switch_is_on(sessions):
    with sessions() as session:
        return kill_switch.is_on(session)


@pytest.fixture
def operator(app):
    with app.state.sessionmaker() as session, session.begin():
        _, secret = create_token(session, "operator", {"account": "write"})
    return {"Authorization": f"Bearer {secret}"}


def test_a_restriction_stops_everything_after_one_call(client, headers, ig, sessions, task):
    make_rule(client, headers)
    media = publish(ig)
    for number in range(20):
        ig.add_comment(media, "precio", author_id=f"179000000000001{number:02}")
    calls = refuse_every_send(ig, RESTRICTED)

    task(sessions)

    assert len(calls) == 1
    [failed] = [d for d in deliveries(sessions) if d.status != "pending"]
    assert (failed.status, failed.reason) == ("failed", "refused")
    assert (failed.error_code, failed.error_subcode) == (10, 1893063)
    assert len(deliveries(sessions)) == 40
    assert kill_switch_is_on(sessions)
    [incident] = restriction_incidents(sessions)
    assert incident.closed_at is None
    assert incident.detail == {"code": 10, "subcode": 1893063}
    with sessions() as session:
        entry = session.scalar(select(AuditEntry).where(AuditEntry.action == "kill_switch.on"))
    assert (entry.actor_type, entry.detail) == ("system", {"code": 10, "subcode": 1893063})

    task(sessions)

    assert len(calls) == 1
    rest = [d for d in deliveries(sessions) if d.id != failed.id]
    assert {(d.status, d.reason) for d in rest} == {("skipped", "kill_switch")}


def test_only_turning_the_switch_off_closes_the_incident_and_a_new_refusal_reopens_it(
    client, headers, operator, ig, sessions, task, clock
):
    make_rule(client, headers)
    media = publish(ig)
    ig.add_comment(media, "precio", author_id=PERSON)
    refuse_every_send(ig, RESTRICTED)
    task(sessions)

    task(sessions)
    client.put("/v1/account/kill-switch", json={"enabled": True}, headers=operator)
    assert restriction_incidents(sessions)[0].closed_at is None

    client.put("/v1/account/kill-switch", json={"enabled": False}, headers=operator)
    assert restriction_incidents(sessions)[0].closed_at is not None
    assert not kill_switch_is_on(sessions)

    clock.advance(seconds=1)
    ig.add_comment(media, "precio", author_id=OTHER_PERSON)
    task(sessions)

    first, second = restriction_incidents(sessions)
    assert first.closed_at is not None
    assert second.closed_at is None
    assert kill_switch_is_on(sessions)


def test_a_restriction_found_while_polling_stops_the_poll_and_the_writes(
    client, headers, ig, sessions, task
):
    make_rule(client, headers)
    publish(ig)
    publish(ig)
    calls = []
    original = ig.list_comments

    def list_comments(media_id):
        calls.append(media_id)
        ig.fail_next("list_comments", RESTRICTED)
        return original(media_id)

    ig.list_comments = list_comments

    task(sessions)

    assert len(calls) == 1
    assert kill_switch_is_on(sessions)
    assert len(restriction_incidents(sessions)) == 1


def test_a_person_is_answered_once_per_rule_and_post(client, headers, ig, sessions, task):
    make_rule(client, headers)
    media, other_media = publish(ig), publish(ig)
    first = ig.add_comment(media, "precio", author_id=PERSON)
    for _ in range(4):
        ig.add_comment(media, "precio", author_id=PERSON)

    task(sessions)

    assert {target for _, target, _ in ig.sent} == {first}
    assert len(ig.sent) == 2
    reasons = [d.reason for d in deliveries(sessions) if d.status == "skipped"]
    assert reasons == ["already_answered"] * 8

    on_another_post = ig.add_comment(other_media, "precio", author_id=PERSON)
    from_someone_else = ig.add_comment(media, "precio", author_id=OTHER_PERSON)
    task(sessions)

    assert {target for _, target, _ in ig.sent} == {first, on_another_post, from_someone_else}
    assert len(ig.sent) == 6
    assert all("author_ref" not in e for e in client.get("/v1/events", headers=headers).json())


def test_a_refused_reply_does_not_count_as_answered(client, headers, ig, sessions, task):
    make_rule(client, headers, content=PRIVATE_ONLY)
    media = publish(ig)
    ig.add_comment(media, "precio", author_id=PERSON)
    ig.fail_next("reply_private", Refused("comment is gone", code=100))
    task(sessions)
    assert ig.sent == []

    again = ig.add_comment(media, "precio", author_id=PERSON)
    task(sessions)

    assert [target for _, target, _ in ig.sent] == [again]


def test_a_reply_that_may_have_gone_out_counts_as_answered(client, headers, ig, sessions, task):
    make_rule(client, headers, content=PRIVATE_ONLY)
    media = publish(ig)
    ig.add_comment(media, "precio", author_id=PERSON)
    ig.fail_next("reply_private", Unavailable("lost connection"))
    task(sessions)

    ig.add_comment(media, "precio", author_id=PERSON)
    task(sessions)

    assert ig.sent == []
    assert sorted((d.status, d.reason) for d in deliveries(sessions)) == [
        ("failed", "unknown"),
        ("skipped", "already_answered"),
    ]


def test_a_second_rule_still_answers_the_same_person_on_the_same_post(
    client, headers, ig, sessions, task
):
    make_rule(client, headers, content=PRIVATE_ONLY)
    make_rule(client, headers, content={"keywords": ["info"], "private_replies": ["Info"]})
    media = publish(ig)
    ig.add_comment(media, "precio", author_id=PERSON)
    ig.add_comment(media, "info", author_id=PERSON)

    task(sessions)

    assert sorted(text for _, _, text in ig.sent) == ["DM", "Info"]


def test_a_pause_reaches_the_comments_of_the_same_id(client, headers, ig, sessions, task, clock):
    make_rule(client, headers)
    ig.add_message(PERSON, "hola")
    task.poll_messages(sessions, ACCOUNT_ID, clock.now())
    [conversation] = client.get("/v1/conversations", headers=headers).json()
    client.post(
        f"/v1/conversations/{conversation['id']}/pause", json={"forever": True}, headers=headers
    )
    media = publish(ig)
    ig.add_comment(media, "precio", author_id=PERSON)
    other = ig.add_comment(media, "precio", author_id=OTHER_PERSON)

    task(sessions)

    assert {target for _, target, _ in ig.sent} == {other}
    skipped = [(d.action, d.reason) for d in deliveries(sessions) if d.status == "skipped"]
    assert sorted(skipped) == [("private_reply", "paused"), ("public_reply", "paused")]


def test_pacing_leaves_the_rest_pending_and_the_next_passes_continue(
    client, headers, ig, sessions, task, clock
):
    make_rule(client, headers, content=PRIVATE_ONLY)
    media = publish(ig)
    comments = [
        ig.add_comment(media, "precio", author_id=f"179000000000001{number:02}")
        for number in range(13)
    ]

    task(sessions)

    assert [target for _, target, _ in ig.sent] == comments[:12]
    last = deliveries(sessions)[-1]
    assert (last.status, last.reason, last.attempted_at) == ("pending", None, None)

    task(sessions)
    assert len(ig.sent) == 12

    clock.advance(seconds=61)
    task(sessions)

    assert [target for _, target, _ in ig.sent] == comments
    assert {d.status for d in deliveries(sessions)} == {"sent"}


def test_a_public_reply_expires_after_a_day_and_the_private_one_does_not(
    client, headers, ig, sessions, task, clock
):
    make_rule(client, headers)
    ig.add_comment(publish(ig), "precio")
    task.poll_comments(sessions, ACCOUNT_ID, clock.now())

    clock.advance(hours=24, seconds=1)
    task.deliver(sessions)

    assert by_action(sessions) == {
        "public_reply": ("skipped", "window_closed"),
        "private_reply": ("sent", None),
    }


def test_the_public_cap_lets_the_private_reply_through(client, headers, ig, sessions, clock):
    ConnectionTask(ig, clock, None)(sessions)
    capped = ReplyTask(ig, clock, poll_every=timedelta(0), hourly_public_cap=1)
    make_rule(client, headers)
    media = publish(ig)
    ig.add_comment(media, "precio", author_id=PERSON)
    second = ig.add_comment(media, "precio", author_id=OTHER_PERSON)

    capped(sessions)

    assert sorted(op for op, _, _ in ig.sent) == ["reply_private", "reply_private", "reply_public"]
    assert ("reply_private", second) in {(op, target) for op, target, _ in ig.sent}
    skipped = [(d.action, d.reason) for d in deliveries(sessions) if d.status == "skipped"]
    assert skipped == [("public_reply", "hourly_cap")]
