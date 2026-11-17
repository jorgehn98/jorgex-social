"""One credential, one account: no call sees, changes or learns about another account (decision 10).

Two connected accounts with one token each, as two agents of one installation have. Every test
checks the same property from a different module: what the other account has never appears in this
one's answer, a resource of the other account is `not_found`, and nothing in an error betrays that
it exists.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from jorgex_social.access.tokens import create_token
from jorgex_social.account.models import AuditEntry, ChannelIncident, Connection
from jorgex_social.publishing.models import Media
from jorgex_social.replies.models import Conversation, Event
from jorgex_social.stats.models import AccountSnapshot, PostSnapshot, RecentMedia

NOW = datetime(2026, 11, 17, 12, tzinfo=UTC)
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 64
# Every group at its highest level, on both sides: what these tests check is the account, not the
# permissions, which test_access.py already covers.
ALL = {
    "posts": "write",
    "scheduling": "write",
    "rules": "write",
    "events": "read",
    "stats": "read",
    "account": "write",
}


class Accounts:
    """Two accounts of one installation and the token that works on each."""

    def __init__(self, a: Connection, b: Connection, secret_a: str, secret_b: str):
        self.a, self.b = a, b
        self.a_headers = {"Authorization": f"Bearer {secret_a}"}
        self.b_headers = {"Authorization": f"Bearer {secret_b}"}


@pytest.fixture
def accounts(app) -> Accounts:
    with app.state.sessionmaker() as session, session.begin():
        a = Connection(network="instagram", external_account_id="1784a", username="account_a")
        b = Connection(network="instagram", external_account_id="1784b", username="account_b")
        session.add_all([a, b])
        session.flush()
        _, secret_a = create_token(session, "agent-a", ALL, a.id)
        _, secret_b = create_token(session, "agent-b", ALL, b.id)
    return Accounts(a, b, secret_a, secret_b)


def upload(client, headers) -> str:
    response = client.post(
        "/v1/media", content=JPEG, headers={**headers, "Content-Type": "image/jpeg"}
    )
    return response.json()["id"]


def draft(client, headers, media_id: str, **body) -> dict:
    return client.post(
        "/v1/posts", json={"type": "image", "items": [media_id], **body}, headers=headers
    ).json()


# What a token may do, and on which account


def test_each_token_reports_and_works_on_its_own_account(client, accounts):
    a = client.get("/v1/token", headers=accounts.a_headers).json()
    b = client.get("/v1/token", headers=accounts.b_headers).json()

    assert a["account"] == {
        "id": accounts.a.id,
        "network": "instagram",
        "username": "account_a",
    }
    assert b["account"]["id"] == accounts.b.id
    assert client.get("/v1/account", headers=accounts.a_headers).json()["account"]["username"] == (
        "account_a"
    )
    assert client.get("/v1/account", headers=accounts.b_headers).json()["account"]["username"] == (
        "account_b"
    )


def test_the_account_never_comes_from_the_request(client, accounts):
    """A token cannot choose another account: no query string, body or header moves it."""
    b_media = upload(client, accounts.b_headers)
    b_post = draft(client, accounts.b_headers, b_media, caption="B's post")
    b_rule = client.post(
        "/v1/rules",
        json={
            "name": "greeting",
            "channel": "comment",
            "content": {"keywords": ["hola"], "private_replies": ["a", "b", "c"]},
        },
        headers=accounts.b_headers,
    ).json()
    asking = {
        "account": accounts.b.id,
        "account_id": accounts.b.id,
        "network": "instagram",
        "username": "account_b",
    }

    for path in ("/v1/posts", "/v1/rules", "/v1/events", "/v1/conversations", "/v1/stats/media"):
        assert client.get(path, headers=accounts.a_headers, params=asking).json() == [], path
    assert client.get("/v1/account/audit", headers=accounts.a_headers, params=asking).json() == []
    # A body cannot move a write to the other account: an account field is refused outright...
    refused = client.post(
        "/v1/posts",
        json={"type": "image", "items": [b_media], "account_id": accounts.b.id},
        headers=accounts.a_headers,
    )
    assert refused.status_code == 422
    # ...and without it, B's media is not A's.
    unknown = client.post(
        "/v1/posts", json={"type": "image", "items": [b_media]}, headers=accounts.a_headers
    )
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["code"] == "unknown_media"
    assert client.get("/v1/posts", headers=accounts.a_headers).json() == []
    assert [post["id"] for post in client.get("/v1/posts", headers=accounts.b_headers).json()] == [
        b_post["id"]
    ]
    # And neither post nor rule of B is reachable by id.
    assert client.get(f"/v1/posts/{b_post['id']}", headers=accounts.a_headers).status_code == 404
    assert client.get(f"/v1/rules/{b_rule['id']}", headers=accounts.a_headers).status_code == 404


# Posts and media


def test_posts_of_one_account_never_appear_in_the_other(client, accounts):
    a_media = upload(client, accounts.a_headers)
    mine = draft(client, accounts.a_headers, a_media, caption="A's post")

    assert client.get("/v1/posts", headers=accounts.a_headers).json()[0]["id"] == mine["id"]
    assert client.get("/v1/posts", headers=accounts.b_headers).json() == []
    # The same answer as a made-up id: nothing says the post exists.
    for method, path, body in (
        ("GET", f"/v1/posts/{mine['id']}", None),
        ("PATCH", f"/v1/posts/{mine['id']}", {"caption": "not mine"}),
        ("DELETE", f"/v1/posts/{mine['id']}", None),
        ("POST", f"/v1/posts/{mine['id']}/cancel", None),
        ("POST", f"/v1/posts/{mine['id']}/publish-now", {}),
        ("POST", f"/v1/posts/{mine['id']}/resolve", {"outcome": "not_published"}),
    ):
        response = client.request(
            method, path, headers=accounts.b_headers, json=body, follow_redirects=True
        )
        assert response.status_code == 404, (method, path)
        assert response.json()["detail"] == {"code": "not_found", "message": "No such post."}
    # Nothing of A's changed.
    kept = client.get(f"/v1/posts/{mine['id']}", headers=accounts.a_headers).json()
    assert (kept["caption"], kept["status"]) == ("A's post", "draft")


def test_media_belongs_to_the_account_that_uploaded_it(app, client, accounts):
    a_media = upload(client, accounts.a_headers)
    b_media = upload(client, accounts.b_headers)

    with app.state.sessionmaker() as session:
        accounts_of = dict(session.execute(select(Media.id, Media.account_id)).all())
    assert accounts_of == {a_media: accounts.a.id, b_media: accounts.b.id}
    # A post of one account cannot carry the other's media, in items or as its cover.
    crossed = client.post(
        "/v1/posts",
        json={"type": "image", "items": [a_media], "cover_media_id": b_media},
        headers=accounts.a_headers,
    )
    assert crossed.status_code == 422
    assert crossed.json()["detail"]["media_ids"] == [b_media]
    mine = draft(client, accounts.a_headers, a_media)
    assert (
        client.patch(
            f"/v1/posts/{mine['id']}", json={"items": [b_media]}, headers=accounts.a_headers
        ).status_code
        == 422
    )


def test_the_idempotency_key_is_unique_per_account(client, accounts):
    a_media = upload(client, accounts.a_headers)
    b_media = upload(client, accounts.b_headers)

    first = draft(client, accounts.a_headers, a_media, idempotency_key="note-1")
    again = draft(client, accounts.a_headers, a_media, idempotency_key="note-1")
    other = draft(client, accounts.b_headers, b_media, idempotency_key="note-1")

    assert again["id"] == first["id"]
    assert other["id"] not in {first["id"]}
    assert other["idempotency_key"] == "note-1"


# Rules, events and conversations


def test_rules_of_one_account_never_appear_in_the_other(client, accounts):
    content = {"keywords": ["hola"], "private_replies": ["a", "b", "c"]}
    rule = client.post(
        "/v1/rules",
        json={"name": "greeting", "channel": "comment", "content": content},
        headers=accounts.a_headers,
    ).json()

    assert [r["id"] for r in client.get("/v1/rules", headers=accounts.a_headers).json()] == [
        rule["id"]
    ]
    assert client.get("/v1/rules", headers=accounts.b_headers).json() == []
    for method, path, body in (
        ("GET", f"/v1/rules/{rule['id']}", None),
        ("PATCH", f"/v1/rules/{rule['id']}", {"name": "theirs"}),
        ("POST", f"/v1/rules/{rule['id']}/activate", None),
        ("POST", f"/v1/rules/{rule['id']}/deactivate", None),
        ("POST", f"/v1/rules/{rule['id']}/archive", None),
    ):
        response = client.request(method, path, headers=accounts.b_headers, json=body)
        assert response.status_code == 404, (method, path)
        assert response.json()["detail"]["code"] == "not_found"
    tested = client.post(
        "/v1/rules/test",
        json={"channel": "comment", "text": "hola", "rule_id": rule["id"]},
        headers=accounts.b_headers,
    )
    assert tested.status_code == 404
    # The rule of A is untouched and still inactive.
    assert client.get(f"/v1/rules/{rule['id']}", headers=accounts.a_headers).json()["name"] == (
        "greeting"
    )


def test_events_and_conversations_are_the_accounts_own(app, client, accounts):
    with app.state.sessionmaker() as session, session.begin():
        for account, person in ((accounts.a, "person_a"), (accounts.b, "person_b")):
            conversation = Conversation(
                account_id=account.id,
                external_id=person,
                last_user_message_at=NOW,
                window_until=NOW + timedelta(hours=24),
                created_at=NOW,
                updated_at=NOW,
                purge_at=NOW + timedelta(days=30),
            )
            session.add(conversation)
            session.flush()
            session.add(
                Event(
                    account_id=account.id,
                    type="message",
                    external_ref=f"event_{person}",
                    conversation_id=conversation.id,
                    occurred_at=NOW,
                    received_at=NOW,
                )
            )

    a_events = client.get("/v1/events", headers=accounts.a_headers).json()
    a_people = client.get("/v1/conversations", headers=accounts.a_headers).json()
    assert [e["external_ref"] for e in a_events] == ["event_person_a"]
    assert [c["external_id"] for c in a_people] == ["person_a"]
    assert client.get("/v1/deliveries", headers=accounts.a_headers).json() == []

    b_people = client.get("/v1/conversations", headers=accounts.b_headers).json()
    response = client.post(
        f"/v1/conversations/{b_people[0]['id']}/pause",
        json={"forever": True},
        headers=accounts.a_headers,
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"
    assert (
        client.post(
            f"/v1/conversations/{b_people[0]['id']}/resume", headers=accounts.a_headers
        ).status_code
        == 404
    )
    assert (
        client.get("/v1/conversations", headers=accounts.b_headers).json()[0]["paused_forever"]
        is False
    )


# Statistics


def test_statistics_never_mix_accounts(app, client, accounts):
    with app.state.sessionmaker() as session, session.begin():
        for account, size in ((accounts.a, 10), (accounts.b, 99)):
            session.add(
                AccountSnapshot(
                    account_id=account.id,
                    period_start=NOW - timedelta(days=1),
                    period_end=NOW,
                    taken_at=NOW,
                    followers=size,
                    metrics={},
                )
            )
            session.add(
                PostSnapshot(
                    account_id=account.id,
                    media_ref="shared_ref",
                    taken_at=NOW,
                    product_type="FEED",
                    published_at=NOW,
                    metrics={"reach": size},
                )
            )
        for account, item in ((accounts.a, "media_a"), (accounts.b, "media_b")):
            session.add(
                RecentMedia(
                    account_id=account.id,
                    fetched_at=NOW,
                    items=[
                        {
                            "id": item,
                            "product_type": "FEED",
                            "media_type": "IMAGE",
                            "permalink": None,
                            "timestamp": NOW.isoformat(),
                        }
                    ],
                )
            )

    a_days = client.get("/v1/stats/account", headers=accounts.a_headers).json()
    a_media = client.get("/v1/stats/media", headers=accounts.a_headers).json()
    a_history = client.get("/v1/stats/media/shared_ref", headers=accounts.a_headers).json()
    a_listing = client.get("/v1/instagram-media", headers=accounts.a_headers).json()

    assert [d["followers"] for d in a_days] == [10]
    assert [m["metrics"]["reach"] for m in a_media] == [10]
    # One media ref of each account: only this account's snapshot comes back.
    assert [s["metrics"]["reach"] for s in a_history] == [10]
    assert [item["id"] for item in a_listing["items"]] == ["media_a"]
    b_days = client.get("/v1/stats/account", headers=accounts.b_headers).json()
    assert [d["followers"] for d in b_days] == [99]


# Audit log


def test_the_audit_log_is_the_accounts_own(app, client, accounts):
    a_media = upload(client, accounts.a_headers)
    draft(client, accounts.a_headers, a_media, caption="A writes")
    with app.state.sessionmaker() as session, session.begin():
        # The operator's own: no account's, and a token never sees it.
        session.add(
            AuditEntry(
                occurred_at=NOW,
                account_id=None,
                actor_type="operator",
                action="operator.login",
                outcome="ok",
            )
        )

    a_log = client.get("/v1/account/audit", headers=accounts.a_headers).json()
    b_log = client.get("/v1/account/audit", headers=accounts.b_headers).json()

    actions = [entry["action"] for entry in a_log]
    assert "POST /v1/posts" in actions and "POST /v1/media" in actions
    assert all(entry["action"] != "operator.login" for entry in a_log)
    assert b_log == []
    # What the token wrote carries its account, so the other token cannot reach it.
    with app.state.sessionmaker() as session:
        written = session.scalars(
            select(AuditEntry).where(AuditEntry.action == "POST /v1/posts")
        ).all()
    assert {entry.account_id for entry in written} == {accounts.a.id}


# Incidents and the kill switch


def test_incidents_are_the_accounts_own(app, client, accounts):
    with app.state.sessionmaker() as session, session.begin():
        mine = ChannelIncident(account_id=accounts.a.id, type="publish_unresolved", opened_at=NOW)
        theirs = ChannelIncident(account_id=accounts.b.id, type="credential_expired", opened_at=NOW)
        operator = ChannelIncident(account_id=None, type="sign_in_password_accepted", opened_at=NOW)
        session.add_all([mine, theirs, operator])
        session.flush()
        ids = {"mine": mine.id, "theirs": theirs.id, "operator": operator.id}

    a_incidents = client.get("/v1/account/incidents", headers=accounts.a_headers).json()
    assert [i["type"] for i in a_incidents] == ["publish_unresolved"]
    assert a_incidents[0]["network"] == "instagram"
    # The installation's own incident, which the operator sees in the web interface.
    assert "sign_in_password_accepted" not in str(a_incidents)
    for name in ("theirs", "operator"):
        response = client.post(
            f"/v1/account/incidents/{ids[name]}/acknowledge", headers=accounts.a_headers
        )
        assert response.status_code == 404, name
        assert response.json()["detail"]["code"] == "not_found"
    assert (
        client.post(
            f"/v1/account/incidents/{ids['mine']}/acknowledge", headers=accounts.a_headers
        ).status_code
        == 200
    )
    # The count in /v1/account is this account's open incidents alone.
    assert client.get("/v1/account", headers=accounts.a_headers).json()["open_incidents"] == 1
    assert client.get("/v1/account", headers=accounts.b_headers).json()["open_incidents"] == 1


def test_the_kill_switch_stops_one_account_and_closes_its_own_incident(app, client, accounts):
    with app.state.sessionmaker() as session, session.begin():
        # Instagram restricted both accounts: each switch turned on by itself, with its incident.
        for account in (accounts.a, accounts.b):
            session.get(Connection, account.id).kill_switch = True
            session.add(
                ChannelIncident(account_id=account.id, type="account_restricted", opened_at=NOW)
            )

    # Only A's token turns A's off; B's is out of its reach.
    off = client.put("/v1/account/kill-switch", json={"enabled": False}, headers=accounts.a_headers)

    assert off.json()["enabled"] is False
    assert client.get("/v1/account", headers=accounts.a_headers).json()["kill_switch"] is False
    b_account = client.get("/v1/account", headers=accounts.b_headers).json()
    assert b_account["kill_switch"] is True
    assert [
        i["type"]
        for i in client.get(
            "/v1/account/incidents", params={"open": True}, headers=accounts.b_headers
        ).json()
    ] == ["account_restricted"]
    assert (
        client.get(
            "/v1/account/incidents", params={"open": True}, headers=accounts.a_headers
        ).json()
        == []
    )
    with app.state.sessionmaker() as session:
        switches = dict(session.execute(select(Connection.id, Connection.kill_switch)).all())
    assert switches == {accounts.a.id: False, accounts.b.id: True}
    # The write was audited under A, so B's audit log does not show it.
    actions = [
        e["action"] for e in client.get("/v1/account/audit", headers=accounts.b_headers).json()
    ]
    assert "PUT /v1/account/kill-switch" not in actions
    assert "PUT /v1/account/kill-switch" in [
        e["action"] for e in client.get("/v1/account/audit", headers=accounts.a_headers).json()
    ]
