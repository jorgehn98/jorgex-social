import json
from datetime import UTC, datetime, timedelta
from http.client import IncompleteRead
from urllib.parse import parse_qs, urlsplit

import pytest

from jorgex_social.instagram.client import InstagramClient
from jorgex_social.instagram.errors import AuthError, RateLimited, Refused, Unavailable
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


def test_a_token_source_is_asked_on_every_call():
    tokens = iter(["first-not-real", "second-not-real"])
    transport = StubTransport(
        (200, {"status_code": "FINISHED"}), (200, {"status_code": "FINISHED"})
    )
    ig = InstagramClient(lambda: next(tokens), "1784", transport=transport)

    ig.container_status("1")
    ig.container_status("1")

    assert [call[2]["Authorization"] for call in transport.calls] == [
        "Bearer first-not-real",
        "Bearer second-not-real",
    ]
