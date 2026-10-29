import hashlib
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from jorgex_social.access.tokens import create_token
from jorgex_social.api import create_app
from jorgex_social.clock import ManualClock
from jorgex_social.config import Settings
from jorgex_social.publishing import catalog
from jorgex_social.publishing import media as storage
from jorgex_social.publishing.models import Media, Post

START = datetime(2026, 10, 29, 12, tzinfo=UTC)
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 64
MP4 = b"\x00\x00\x00\x18ftypisom" + b"v" * 64
MOV = b"\x00\x00\x00\x14ftypqt  " + b"q" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 64


@pytest.fixture
def clock():
    return ManualClock(START)


@pytest.fixture
def app(settings, clock):
    return create_app(settings, clock)


@pytest.fixture
def headers(app):
    with app.state.sessionmaker() as session, session.begin():
        _, secret = create_token(session, "agent", {"posts": "write", "scheduling": "write"})
    return {"Authorization": f"Bearer {secret}"}


def upload(client, headers, body=JPEG, content_type="image/jpeg") -> str:
    response = client.post(
        "/v1/media", content=body, headers={**headers, "Content-Type": content_type}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def create(client, headers, **body) -> dict:
    response = client.post("/v1/posts", json=body, headers=headers)
    assert response.status_code in (200, 201), response.text
    return response.json()


def reel(client, headers, caption="New reel #ai", **extra) -> dict:
    return create(
        client,
        headers,
        type="reel",
        caption=caption,
        items=[upload(client, headers, MP4, "video/mp4")],
        **extra,
    )


def stored_files(settings):
    return (
        sorted(p.name for p in settings.media_dir.iterdir()) if settings.media_dir.exists() else []
    )


# Upload


def test_upload_takes_the_type_from_the_signature(client, headers, app):
    response = client.post(
        "/v1/media", content=MOV, headers={**headers, "Content-Type": "video/mp4"}
    )

    body = response.json()
    assert response.status_code == 201
    assert body["type"] == "mov"
    assert body["bytes"] == len(MOV)
    assert body["sha256"] == hashlib.sha256(MOV).hexdigest()
    assert stored_files(app.state.settings) == [f"{body['id']}.mov"]


@pytest.mark.parametrize(
    ("body", "content_type"), [(PNG, "image/jpeg"), (JPEG, "image/png"), (b"", "image/jpeg")]
)
def test_upload_rejects_other_files(client, headers, app, body, content_type):
    response = client.post(
        "/v1/media", content=body, headers={**headers, "Content-Type": content_type}
    )

    assert response.status_code == 415
    assert stored_files(app.state.settings) == []


def test_upload_over_the_limit_leaves_nothing(client, headers, app, monkeypatch):
    monkeypatch.setattr(storage, "MAX_UPLOAD_BYTES", len(MP4) - 1)

    def chunks():
        yield MP4[:20]
        yield MP4[20:]

    response = client.post(
        "/v1/media", content=chunks(), headers={**headers, "Content-Type": "video/mp4"}
    )

    assert response.status_code == 413
    assert stored_files(app.state.settings) == []
    with app.state.sessionmaker() as session:
        assert session.scalar(select(func.count()).select_from(Media)) == 0


def test_upload_needs_posts_write(app, client):
    with app.state.sessionmaker() as session, session.begin():
        _, secret = create_token(session, "reader", {"posts": "read"})

    response = client.post(
        "/v1/media",
        content=JPEG,
        headers={"Authorization": f"Bearer {secret}", "Content-Type": "image/jpeg"},
    )

    assert response.status_code == 403


# Catalog


def media(type_="jpeg", size=1000) -> Media:
    return Media(type=type_, bytes=size, sha256="0" * 64)


MB = 1024 * 1024
VALID_CAPTION = "Hello #ai"


@pytest.mark.parametrize(
    ("post_type", "caption", "items", "cover", "thumb", "code"),
    [
        ("image", VALID_CAPTION, [media("mp4")], None, None, "item_type"),
        ("image", VALID_CAPTION, [media(size=8 * MB + 1)], None, None, "item_too_large"),
        ("reel", VALID_CAPTION, [media("jpeg")], None, None, "item_type"),
        ("reel", VALID_CAPTION, [media("mov", 300 * MB + 1)], None, None, "item_too_large"),
        ("reel", "  ", [media("mp4")], None, None, "caption_required"),
        ("reel", VALID_CAPTION, [media("mp4")], media("mp4"), None, "cover_type"),
        ("reel", VALID_CAPTION, [media("mp4")], None, -1, "thumb_offset_negative"),
        ("image", VALID_CAPTION, [media()], media(), None, "cover_not_allowed"),
        ("image", VALID_CAPTION, [media()], None, 0, "thumb_offset_not_allowed"),
        ("carousel", VALID_CAPTION, [media()], None, None, "item_count"),
        ("carousel", VALID_CAPTION, [media()] * 11, None, None, "item_count"),
        ("story", "", [media("mp4", 100 * MB + 1)], None, None, "item_too_large"),
        ("story", "", [media(), media()], None, None, "item_count"),
        ("image", "x" * 2201, [media()], None, None, "caption_too_long"),
        (
            "image",
            " ".join(f"#t{i}" for i in range(31)),
            [media()],
            None,
            None,
            "too_many_hashtags",
        ),
    ],
)
def test_catalog_rejects(post_type, caption, items, cover, thumb, code):
    check = catalog.validate(catalog.PostType(post_type), caption, items, cover, thumb)

    assert code in [e.code for e in check.errors]


def test_catalog_accepts_a_full_mixed_carousel():
    items = [media(), media("mp4", 300 * MB), media("mov")] + [media()] * 7

    check = catalog.validate(catalog.PostType.CAROUSEL, VALID_CAPTION, items, None, None)

    assert check.errors == []
    assert [w.code for w in check.warnings] == ["duration_unchecked"]


def test_catalog_warnings_do_not_block():
    story = catalog.validate(catalog.PostType.STORY, "ignored", [media()], None, None)
    reel_ = catalog.validate(catalog.PostType.REEL, VALID_CAPTION, [media("mp4")], media(), 500)

    assert story.errors == reel_.errors == []
    assert [w.code for w in story.warnings] == ["caption_ignored"]
    assert {w.code for w in reel_.warnings} == {"duration_unchecked", "thumb_offset_ignored"}


def test_catalog_endpoint_declares_the_limits(client, headers):
    body = client.get("/v1/catalog", headers=headers).json()

    assert body["post_types"]["carousel"]["items"]["max"] == 10
    assert body["caption"] == {"max_chars": 2200, "max_hashtags": 30}


# Posts


def test_create_returns_draft_with_hash_and_issues(client, headers):
    post = reel(client, headers)

    assert post["status"] == "draft"
    assert len(post["content_hash"]) == 64
    assert post["errors"] == []
    assert [w["code"] for w in post["warnings"]] == ["duration_unchecked"]


def test_hash_follows_content_not_identity(client, headers):
    image = upload(client, headers)
    first = create(client, headers, type="image", caption="Same", items=[image])
    second = create(client, headers, type="image", caption="Same", items=[image])
    other = create(client, headers, type="image", caption="Other", items=[image])

    assert first["content_hash"] == second["content_hash"] != other["content_hash"]


def test_unknown_media_is_rejected(client, headers):
    response = client.post(
        "/v1/posts", json={"type": "image", "caption": "x", "items": ["nope"]}, headers=headers
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unknown_media"


def test_idempotency_key_does_not_duplicate(app, client, headers):
    image = upload(client, headers)
    body = {"type": "image", "caption": "x", "items": [image], "idempotency_key": "note-1"}

    first = client.post("/v1/posts", json=body, headers=headers)
    again = client.post("/v1/posts", json={**body, "caption": "changed"}, headers=headers)

    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["caption"] == "x"
    with app.state.sessionmaker() as session:
        assert session.scalar(select(func.count()).select_from(Post)) == 1


def test_invalid_post_is_kept_as_draft_but_cannot_be_scheduled(client, headers):
    post = create(client, headers, type="image", caption="", items=[upload(client, headers)])

    response = client.post(f"/v1/posts/{post['id']}/publish-now", json={}, headers=headers)

    assert [e["code"] for e in post["errors"]] == ["caption_required"]
    assert response.status_code == 422
    assert response.json()["detail"]["errors"][0]["code"] == "caption_required"
    assert client.get(f"/v1/posts/{post['id']}", headers=headers).json()["status"] == "draft"


def test_expected_hash_mismatch_is_409_and_changes_nothing(client, headers):
    post = reel(client, headers)

    response = client.post(
        f"/v1/posts/{post['id']}/schedule",
        json={"scheduled_for": "2026-10-30T09:00:00+02:00", "expected_hash": "0" * 64},
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "content_changed"
    after = client.get(f"/v1/posts/{post['id']}", headers=headers).json()
    assert (after["status"], after["scheduled_for"]) == ("draft", None)


def test_schedule_stores_utc(client, headers):
    post = reel(client, headers)

    response = client.post(
        f"/v1/posts/{post['id']}/schedule",
        json={"scheduled_for": "2026-10-30T09:00:00+02:00", "expected_hash": post["content_hash"]},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "scheduled"
    assert response.json()["scheduled_for"] == "2026-10-30T07:00:00Z"


@pytest.mark.parametrize("when", ["2026-10-29T11:59:00Z", "2026-10-30T09:00:00"])
def test_schedule_rejects_past_or_naive_dates(client, headers, when):
    post = reel(client, headers)

    response = client.post(
        f"/v1/posts/{post['id']}/schedule", json={"scheduled_for": when}, headers=headers
    )

    assert response.status_code == 422


def test_editing_a_scheduled_post_returns_it_to_draft(client, headers):
    post = reel(client, headers)
    client.post(
        f"/v1/posts/{post['id']}/schedule",
        json={"scheduled_for": "2026-10-30T09:00:00Z"},
        headers=headers,
    )

    edited = client.patch(f"/v1/posts/{post['id']}", json={"caption": "Better"}, headers=headers)

    body = edited.json()
    assert edited.status_code == 200
    assert (body["status"], body["scheduled_for"], body["caption"]) == ("draft", None, "Better")
    assert body["content_hash"] != post["content_hash"]


def test_editing_replaces_items_in_order(client, headers):
    a, b, c = (upload(client, headers, JPEG + bytes([i])) for i in range(3))
    post = create(client, headers, type="carousel", caption="x", items=[a, b])

    edited = client.patch(f"/v1/posts/{post['id']}", json={"items": [c, a, b]}, headers=headers)

    assert edited.json()["items"] == [c, a, b]


def test_scheduling_needs_its_own_group(app, client, headers):
    post = reel(client, headers)
    with app.state.sessionmaker() as session, session.begin():
        _, secret = create_token(session, "writer", {"posts": "write"})

    response = client.post(
        f"/v1/posts/{post['id']}/publish-now",
        json={},
        headers={"Authorization": f"Bearer {secret}"},
    )

    assert response.status_code == 403


def test_cancel_only_from_draft_or_scheduled(client, headers):
    post = reel(client, headers)

    first = client.post(f"/v1/posts/{post['id']}/cancel", headers=headers)
    second = client.post(f"/v1/posts/{post['id']}/cancel", headers=headers)

    assert first.json()["status"] == "cancelled"
    assert second.status_code == 409


def test_list_filters_by_status(client, headers):
    draft = reel(client, headers)
    cancelled = reel(client, headers)
    client.post(f"/v1/posts/{cancelled['id']}/cancel", headers=headers)

    listed = client.get("/v1/posts", params={"status": "draft"}, headers=headers).json()

    assert [p["id"] for p in listed] == [draft["id"]]


def test_real_mode_without_public_url_refuses_to_schedule(settings, clock):
    real = create_app(
        Settings(
            database_path=settings.database_path, media_dir=settings.media_dir, simulation=False
        ),
        clock,
    )
    client = TestClient(real)
    with real.state.sessionmaker() as session, session.begin():
        _, secret = create_token(session, "agent", {"posts": "write", "scheduling": "write"})
    headers = {"Authorization": f"Bearer {secret}"}
    post = reel(client, headers)

    response = client.post(f"/v1/posts/{post['id']}/publish-now", json={}, headers=headers)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "public_url_missing"


def test_simulated_posts_never_show_in_real_mode(settings, clock, client, headers):
    reel(client, headers)
    real = create_app(Settings(database_path=settings.database_path, simulation=False), clock)

    assert TestClient(real).get("/v1/posts", headers=headers).json() == []


# Public media URL


def test_public_media_url_needs_no_token_and_only_works_while_valid(app, client, headers, clock):
    media_id = upload(client, headers)
    with app.state.sessionmaker() as session, session.begin():
        item = session.get(Media, media_id)
        storage.issue_url(item, clock.now())
        token = item.url_token

    served = client.get(f"/m/{token}")
    clock.advance(hours=24)
    expired = client.get(f"/m/{token}")

    assert served.status_code == 200
    assert served.content == JPEG
    assert served.headers["content-type"] == "image/jpeg"
    assert expired.status_code == 404
    assert client.get("/m/unknown").status_code == 404
