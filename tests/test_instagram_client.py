import json
from datetime import UTC, datetime, timedelta
from http.client import IncompleteRead
from urllib.parse import parse_qs, urlsplit

import pytest

from jorgex_social.instagram.client import InstagramClient
from jorgex_social.instagram.errors import (
    AccountRestricted,
    AuthError,
    RateLimited,
    Refused,
    Unavailable,
)
from jorgex_social.instagram.types import ContainerKind, ContainerRequest, ProductType

TOKEN = "test-token-not-real"


class StubTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        status, payload = response
        return status, json.dumps(payload).encode() if not isinstance(payload, bytes) else payload


def client(*responses):
    transport = StubTransport(*responses)
    return InstagramClient(TOKEN, "1784", transport=transport), transport


def query(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def test_token_goes_in_the_header_never_in_the_url():
    ig, transport = client((200, {"id": "1"}))

    ig.create_container(ContainerRequest(ContainerKind.IMAGE, url="https://x.test/a.jpg"))

    method, url, headers, body = transport.calls[0]
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in url and TOKEN not in body.decode()
    assert TOKEN not in repr(ig)


def test_get_params_go_in_the_query_and_post_params_in_the_body():
    ig, transport = client((200, {"status_code": "FINISHED"}), (200, {"id": "9"}))

    ig.container_status("55")
    ig.publish("55")

    assert query(transport.calls[0][1]) == {"fields": "status_code"}
    assert transport.calls[1][0] == "POST"
    assert parse_qs(transport.calls[1][3].decode()) == {"creation_id": ["55"]}


@pytest.mark.parametrize(
    ("request_", "params"),
    [
        (
            ContainerRequest(
                ContainerKind.REELS, url="v", caption="c", cover_url="i", thumb_offset_ms=5
            ),
            {
                "media_type": "REELS",
                "video_url": "v",
                "caption": "c",
                "cover_url": "i",
                "thumb_offset": "5",
            },
        ),
        (
            ContainerRequest(ContainerKind.STORIES, url="v", is_video=True),
            {"media_type": "STORIES", "video_url": "v"},
        ),
        (
            ContainerRequest(ContainerKind.VIDEO, url="v", is_carousel_item=True),
            {"media_type": "VIDEO", "video_url": "v", "is_carousel_item": "true"},
        ),
        (
            ContainerRequest(ContainerKind.CAROUSEL, children=("1", "2"), caption="c"),
            {"media_type": "CAROUSEL", "children": "1,2", "caption": "c"},
        ),
        (ContainerRequest(ContainerKind.IMAGE, url="i"), {"image_url": "i"}),
    ],
)
def test_container_params(request_, params):
    ig, transport = client((200, {"id": "1"}))

    ig.create_container(request_)

    sent = {k: v[0] for k, v in parse_qs(transport.calls[0][3].decode()).items()}
    assert sent == params


@pytest.mark.parametrize(
    ("response", "error"),
    [
        ((500, {"error": {"message": "boom", "code": 2}}), Unavailable),
        ((502, b"<html>bad gateway</html>"), Unavailable),
        (OSError("connection reset"), Unavailable),
        (TimeoutError(), Unavailable),
        ((400, {"error": {"message": "expired", "code": 190}}), AuthError),
        ((400, {"error": {"message": "too many", "code": 4}}), RateLimited),
        ((429, {"error": {"message": "slow down"}}), RateLimited),
        ((400, {"error": {"message": "bad", "code": 100, "error_subcode": 33}}), Refused),
    ],
)
def test_error_classification(response, error):
    ig, _ = client(response)

    with pytest.raises(error):
        ig.publish("1")


@pytest.mark.parametrize(
    ("code", "subcode"),
    [
        (10, 1893063),
        (100, 2534029),
        (613, 2018338),
        (4, 2207051),
        (9, 2018352),
        (25, 2207050),
        (368, 1404078),
        (368, None),
    ],
)
def test_a_restricted_account_is_told_apart_and_never_looks_retryable(code, subcode):
    error = {"message": "restricted", "code": code, "error_subcode": subcode, "is_transient": True}
    ig, _ = client((400, {"error": error}))

    with pytest.raises(AccountRestricted) as caught:
        ig.reply_public("1", "hi")

    assert (caught.value.code, caught.value.subcode) == (code, subcode)
    assert not caught.value.transient


@pytest.mark.parametrize(
    ("code", "subcode", "expected"),
    [
        (4, None, RateLimited),
        (4, 1234, RateLimited),
        (613, None, RateLimited),
        (613, 1234, RateLimited),
        (80002, None, RateLimited),
        (9, 2207042, Refused),  # the daily publishing limit
        (10, None, Refused),
        (190, 2207050, AuthError),
    ],
)
def test_codes_near_a_restriction_keep_their_own_meaning(code, subcode, expected):
    error = {"message": "no", "code": code, "error_subcode": subcode}
    ig, _ = client((400, {"error": error}))

    with pytest.raises(Refused) as caught:
        ig.reply_public("1", "hi")

    assert type(caught.value) is expected


def test_refusal_keeps_meta_codes():
    ig, _ = client(
        (400, {"error": {"message": "bad", "code": 100, "error_subcode": 33, "is_transient": True}})
    )

    with pytest.raises(Refused) as caught:
        ig.publish("1")

    assert (caught.value.code, caught.value.subcode, caught.value.transient) == (100, 33, True)


def test_unavailable_is_not_a_refusal():
    """A 5xx after publishing may mean it went live: callers must not treat it as 'not done'."""
    assert not issubclass(Unavailable, Refused)


def test_list_media_pages_with_the_cursor_and_stops():
    item = {
        "id": "1",
        "media_product_type": "REELS",
        "media_type": "VIDEO",
        "timestamp": "2026-10-01T10:00:00+0000",
    }
    ig, transport = client(
        (
            200,
            {
                "data": [item],
                "paging": {"cursors": {"after": "C1"}, "next": "https://x?access_token=t"},
            },
        ),
        (200, {"data": [{**item, "id": "2"}], "paging": {"cursors": {"after": "C2"}}}),
    )

    media = ig.list_media()

    assert [m.id for m in media] == ["1", "2"]
    assert media[0].product_type == ProductType.REELS
    assert media[0].timestamp == datetime(2026, 10, 1, 10, tzinfo=UTC)
    assert query(transport.calls[1][1])["after"] == "C1"
    assert all(TOKEN not in call[1] for call in transport.calls)


def test_list_media_respects_max_items():
    item = {"id": "1", "timestamp": "2026-10-01T10:00:00+0000"}
    ig, transport = client(
        (
            200,
            {
                "data": [item, {**item, "id": "2"}],
                "paging": {"cursors": {"after": "C"}, "next": "n"},
            },
        ),
    )

    assert [m.id for m in ig.list_media(max_items=1)] == ["1"]
    assert len(transport.calls) == 1


def test_missing_metric_is_absent_not_zero():
    ig, transport = client(
        (
            200,
            {
                "data": [
                    {"name": "reach", "values": [{"value": 12}]},
                    {"name": "views", "total_value": {"value": 30}},
                    {"name": "likes", "values": []},
                ]
            },
        )
    )

    assert ig.media_insights("1", ProductType.STORY) == {"reach": 12, "views": 30}
    assert "navigation" in query(transport.calls[0][1])["metric"]


def test_account_insights_window_is_at_most_30_days():
    ig, _ = client()
    now = datetime(2026, 10, 29, tzinfo=UTC)

    with pytest.raises(ValueError):
        ig.account_insights(now - timedelta(days=31), now)
    with pytest.raises(ValueError):
        ig.account_insights(now, now)


def test_publishing_limit():
    ig, _ = client(
        (
            200,
            {"data": [{"quota_usage": 3, "config": {"quota_total": 100, "quota_duration": 86400}}]},
        )
    )

    limit = ig.publishing_limit()

    assert (limit.quota_usage, limit.quota_total, limit.quota_duration_seconds) == (3, 100, 86400)


@pytest.mark.parametrize(
    "response",
    [
        IncompleteRead(b"partial"),
        (503, {"error": "upstream"}),
        (200, {"unexpected": True}),
    ],
)
def test_publish_without_a_usable_answer_is_unavailable(response):
    ig, _ = client(response)

    with pytest.raises(Unavailable):
        ig.publish("1")


def test_list_media_skips_unknown_product_types():
    base = {"timestamp": "2026-10-01T10:00:00+0000"}
    ig, _ = client(
        (
            200,
            {
                "data": [
                    {**base, "id": "1", "media_product_type": "AD"},
                    {**base, "id": "2", "media_product_type": "FEED"},
                ]
            },
        )
    )

    assert [m.id for m in ig.list_media()] == ["2"]


# Token renewal: the one call where Meta wants the token in the URL


def test_refresh_sends_the_token_as_documented_and_returns_the_new_one():
    ig, transport = client((200, {"access_token": "renewed-not-real", "expires_in": 5184000}))

    renewal = ig.refresh_token()

    method, url, headers, body = transport.calls[0]
    assert method == "GET"
    assert url.startswith("https://graph.instagram.com/refresh_access_token?")
    assert query(url) == {"grant_type": "ig_refresh_token", "access_token": TOKEN}
    assert renewal.access_token == "renewed-not-real"
    assert renewal.expires_in == timedelta(days=60)
    assert "renewed-not-real" not in repr(renewal)


@pytest.mark.parametrize(
    ("response", "error"),
    [
        # Meta does not document echoing the token; if it ever did, it must not get through.
        ((400, {"error": {"message": f"bad token {TOKEN}", "code": 190}}), AuthError),
        ((400, f"<html>{TOKEN}</html>".encode()), Refused),
        (OSError(f"cannot reach https://graph.instagram.com/?access_token={TOKEN}"), Unavailable),
        ((200, f'{{"access_token": "{TOKEN}"}}'.encode()), Unavailable),  # no expires_in
        ((200, b"not json"), Unavailable),
    ],
)
def test_refresh_errors_never_carry_the_token_or_the_url(response, error):
    ig, _ = client(response)

    with pytest.raises(error) as caught:
        ig.refresh_token()

    assert TOKEN not in str(caught.value)
    assert "refresh_access_token" not in str(caught.value)


def test_a_credential_source_is_asked_on_every_call():
    credentials = iter([("first-not-real", "1784"), ("second-not-real", "2048")])
    transport = StubTransport((200, {"id": "1"}), (200, {"id": "2"}))
    ig = InstagramClient.from_source(lambda: next(credentials), transport=transport)

    ig.publish("9")
    ig.publish("9")

    assert [(call[1].split("/")[-2], call[2]["Authorization"]) for call in transport.calls] == [
        ("1784", "Bearer first-not-real"),
        ("2048", "Bearer second-not-real"),
    ]


def test_comments_ask_only_for_the_author_id_and_page_with_the_cursor():
    first = {
        "data": [
            {
                "id": "c1",
                "text": "third-party text",
                "timestamp": "2026-10-29T12:00:00+0000",
                "from": {"id": "u1", "username": "someone"},
            }
        ],
        "paging": {"cursors": {"after": "A"}, "next": "https://graph.instagram.com/next"},
    }
    second = {"data": [{"id": "c2", "timestamp": "2026-10-29T12:01:00+0000"}]}
    ig, transport = client((200, first), (200, second))

    comments = ig.list_comments("m1")

    assert [(c.id, c.author_id, c.text) for c in comments] == [
        ("c1", "u1", "third-party text"),
        ("c2", None, ""),
    ]
    assert comments[0].timestamp == datetime(2026, 10, 29, 12, tzinfo=UTC)
    assert "someone" not in repr(comments) and "third-party" not in repr(comments)
    assert urlsplit(transport.calls[0][1]).path.endswith("/m1/comments")
    assert query(transport.calls[0][1])["fields"] == "id,text,timestamp,from{id}"
    assert query(transport.calls[1][1])["after"] == "A"


def test_conversations_stop_at_the_first_one_older_than_since():
    page = {
        "data": [
            {"id": "t1", "updated_time": "2026-10-29T12:00:00+0000"},
            {"id": "t2", "updated_time": "2026-10-27T12:00:00+0000"},
        ],
        "paging": {"cursors": {"after": "A"}, "next": "https://graph.instagram.com/next"},
    }
    ig, transport = client((200, page))

    found = ig.list_conversations(since=datetime(2026, 10, 28, tzinfo=UTC))

    assert [c.id for c in found] == ["t1"]
    assert len(transport.calls) == 1
    assert query(transport.calls[0][1])["platform"] == "instagram"
    assert urlsplit(transport.calls[0][1]).path.endswith("/1784/conversations")


def test_messages_of_a_conversation():
    payload = {
        "id": "t1",
        "messages": {
            "data": [
                {
                    "id": "mid1",
                    "created_time": "2026-10-29T12:00:00+0000",
                    "from": {"id": "u1", "username": "someone"},
                    "message": "hello",
                },
                {"id": "mid2", "created_time": "2026-10-29T11:00:00+0000", "from": {"id": "1784"}},
            ]
        },
    }
    ig, transport = client((200, payload))

    messages = ig.list_messages("t1")

    assert [(m.id, m.sender_id, m.text) for m in messages] == [
        ("mid1", "u1", "hello"),
        ("mid2", "1784", ""),
    ]
    assert query(transport.calls[0][1]) == {"fields": "messages{id,created_time,from{id},message}"}


def test_replies_and_messages_go_to_the_documented_endpoints():
    ig, transport = client(
        (200, {"id": "r1"}),
        (200, {"recipient_id": "u1", "message_id": "mid1"}),
        (200, {"recipient_id": "u2", "message_id": "mid2"}),
    )

    results = (
        ig.reply_public("c1", "Thanks!"),
        ig.reply_private("c1", "Sent"),
        ig.send_message("u2", "Hi"),
    )

    assert results == ("r1", "mid1", "mid2")
    sent = [(m, urlsplit(u).path, parse_qs(b.decode())) for m, u, _, b in transport.calls]
    assert sent[0] == ("POST", "/v26.0/c1/replies", {"message": ["Thanks!"]})
    assert sent[1][:2] == ("POST", "/v26.0/1784/messages")
    assert json.loads(sent[1][2]["recipient"][0]) == {"comment_id": "c1"}
    assert json.loads(sent[1][2]["message"][0]) == {"text": "Sent"}
    assert json.loads(sent[2][2]["recipient"][0]) == {"id": "u2"}


def test_a_send_without_a_message_id_is_unavailable():
    ig, _ = client((200, {"recipient_id": "u1"}))

    with pytest.raises(Unavailable):
        ig.reply_private("c1", "Sent")
