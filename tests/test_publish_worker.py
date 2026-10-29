from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from jorgex_social.access.tokens import create_token
from jorgex_social.api import create_app
from jorgex_social.clock import ManualClock
from jorgex_social.instagram.errors import Refused, Unavailable
from jorgex_social.instagram.fake import FakeInstagram
from jorgex_social.instagram.types import ContainerKind, ProductType
from jorgex_social.publishing.models import Media
from jorgex_social.publishing.worker import LONGEST_ATTEMPT, STALE_ATTEMPT, PublishTask
from jorgex_social.stats.models import PostSnapshot
from jorgex_social.stats.snapshots import SnapshotTask

START = datetime(2026, 10, 29, 12, tzinfo=UTC)
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 64
MP4 = b"\x00\x00\x00\x18ftypisom" + b"v" * 64
BASE_URL = "https://social.test"


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
        _, secret = create_token(session, "agent", {"posts": "write", "scheduling": "write"})
    return {"Authorization": f"Bearer {secret}"}


@pytest.fixture
def requests(ig):
    """The container requests sent to the fake, to check what reaches Instagram."""
    sent = []
    original = ig.create_container

    def create_container(request):
        sent.append(request)
        return original(request)

    ig.create_container = create_container
    return sent


@pytest.fixture
def task(ig, clock, settings):
    # Polling sleeps move the manual clock, so video processing finishes without real waiting.
    return PublishTask(
        ig, clock, settings.media_dir, BASE_URL, sleep=lambda s: clock.advance(seconds=s)
    )


class Api:
    def __init__(self, client, headers):
        self.client = client
        self.headers = headers

    def upload(self, body=JPEG, content_type="image/jpeg"):
        return self.client.post(
            "/v1/media", content=body, headers={**self.headers, "Content-Type": content_type}
        ).json()["id"]

    def post(self, type_="reel", caption="A reel #ai", items=None, **extra):
        if items is None:
            items = [self.upload(MP4, "video/mp4") if type_ == "reel" else self.upload()]
        response = self.client.post(
            "/v1/posts",
            json={"type": type_, "caption": caption, "items": items, **extra},
            headers=self.headers,
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    def publish_now(self, post_id):
        response = self.client.post(
            f"/v1/posts/{post_id}/publish-now", json={}, headers=self.headers
        )
        assert response.status_code == 200, response.text

    def get(self, post_id):
        return self.client.get(f"/v1/posts/{post_id}", headers=self.headers).json()


@pytest.fixture
def api(client, headers):
    return Api(client, headers)


def published(ig):
    return [ig.media(m.id) for m in ig.list_media()]


# Happy paths


def test_publishes_a_reel_and_closes_the_attempt(api, task, sessions, ig, clock):
    post_id = api.post(caption="A reel #ai")
    api.publish_now(post_id)

    task(sessions)

    post = api.get(post_id)
    [live] = published(ig)
    assert post["status"] == "published"
    assert post["external_ref"] == live.id
    assert post["permalink"] == live.permalink
    assert live.caption == "A reel #ai"
    assert [(a["result"], a["container_ref"] is not None) for a in post["attempts"]] == [
        ("ok", True)
    ]
    with sessions() as session:
        [media] = session.scalars(select(Media)).all()
    assert media.url_token is None
    assert media.delete_at == clock.now() + timedelta(days=7)


def test_carousel_creates_children_then_parent(api, task, sessions, ig, requests):
    items = [api.upload(), api.upload(MP4, "video/mp4")]
    post_id = api.post("carousel", "Two slides", items)
    api.publish_now(post_id)

    task(sessions)

    assert [(r.kind, r.is_carousel_item) for r in requests] == [
        (ContainerKind.IMAGE, True),
        (ContainerKind.VIDEO, True),
        (ContainerKind.CAROUSEL, False),
    ]
    assert api.get(post_id)["status"] == "published"
    assert published(ig)[0].media_type == "CAROUSEL_ALBUM"


def test_video_story_goes_out_as_a_story_without_caption(api, task, sessions, ig, requests):
    post_id = api.post("story", "", [api.upload(MP4, "video/mp4")])
    api.publish_now(post_id)

    task(sessions)

    [request] = requests
    assert (request.kind, request.is_video, request.caption) == (
        ContainerKind.STORIES,
        True,
        None,
    )
    ref = api.get(post_id)["external_ref"]
    assert ig.media(ref).product_type == ProductType.STORY


def test_reel_cover_is_sent_with_its_own_url(api, task, sessions, requests):
    cover = api.upload()
    post_id = api.post(cover_media_id=cover, thumb_offset_ms=1500)
    api.publish_now(post_id)

    task(sessions)

    [request] = requests
    assert request.cover_url.startswith(f"{BASE_URL}/m/")
    assert request.url.startswith(f"{BASE_URL}/m/")
    assert request.cover_url != request.url
    assert request.thumb_offset_ms == 1500


def test_media_url_is_public_during_the_attempt_and_revoked_after(
    api, client, sessions, ig, clock, settings, requests
):
    post_id = api.post()
    api.publish_now(post_id)
    seen = []

    def sleep(seconds):
        # While the video container processes, Instagram would be downloading the file.
        seen.append(client.get(requests[0].url))
        clock.advance(seconds=seconds)

    PublishTask(ig, clock, settings.media_dir, BASE_URL, sleep=sleep)(sessions)

    assert seen and all(r.status_code == 200 and r.content == MP4 for r in seen)
    assert client.get(requests[0].url).status_code == 404


def test_permalink_failure_does_not_change_the_outcome(api, task, sessions, ig):
    post_id = api.post()
    api.publish_now(post_id)
    ig.fail_next("media", Unavailable("lookup failed"))

    task(sessions)

    post = api.get(post_id)
    assert (post["status"], post["permalink"]) == ("published", None)
    assert post["external_ref"] is not None


# Never twice


def test_server_error_after_going_live_is_resolved_without_publishing_again(
    api, task, sessions, ig
):
    post_id = api.post()
    api.publish_now(post_id)
    ig.fail_next("publish", Unavailable("502 after going live"), after=True)

    task.publish_due(sessions)
    assert api.get(post_id)["status"] == "outcome_unknown"

    task(sessions)

    post = api.get(post_id)
    [live] = published(ig)
    assert post["status"] == "published"
    assert post["external_ref"] == live.id
    assert [(a["result"], a["resolved_as"]) for a in post["attempts"]] == [("unknown", "published")]


def test_finished_container_is_published_again_safely(api, task, sessions, ig):
    post_id = api.post()
    api.publish_now(post_id)
    ig.fail_next("publish", Unavailable("lost connection before Meta acted"))

    task(sessions)
    task(sessions)

    post = api.get(post_id)
    assert post["status"] == "published"
    assert len(published(ig)) == 1
    first, retry = post["attempts"]
    assert (first["result"], first["resolved_as"]) == ("unknown", "not_published")
    assert retry["result"] == "ok"
    assert retry["container_ref"] == first["container_ref"]


def test_refusal_checks_the_container_before_failing(api, task, sessions, ig):
    post_id = api.post()
    api.publish_now(post_id)
    ig.fail_next("publish", Refused("bad request", code=100, subcode=2207001))

    task(sessions)

    post = api.get(post_id)
    assert (post["status"], post["failure"]) == ("failed", "refused")
    assert (post["attempts"][0]["error_code"], post["attempts"][0]["error_subcode"]) == (
        100,
        2207001,
    )
    assert published(ig) == []


def test_refusal_on_a_live_container_is_not_a_failure(api, task, sessions, ig):
    post_id = api.post()
    api.publish_now(post_id)
    ig.fail_next("publish", Refused("odd 4xx after going live", code=1), after=True)

    task.publish_due(sessions)
    assert api.get(post_id)["status"] == "outcome_unknown"
    task(sessions)

    assert api.get(post_id)["status"] == "published"
    assert len(published(ig)) == 1


def test_a_refused_post_can_be_rescheduled(api, task, sessions, ig):
    post_id = api.post()
    api.publish_now(post_id)
    ig.fail_next("publish", Refused("quota", code=9))
    task(sessions)

    api.publish_now(post_id)
    task(sessions)

    assert api.get(post_id)["status"] == "published"
    assert len(published(ig)) == 1


def test_unavailable_while_resolving_waits_for_the_next_pass(api, task, sessions, ig):
    post_id = api.post()
    api.publish_now(post_id)
    ig.fail_next("publish", Unavailable("502"), after=True)
    task.publish_due(sessions)

    ig.fail_next("container_status", Unavailable("still down"))
    task(sessions)
    assert api.get(post_id)["status"] == "outcome_unknown"

    task(sessions)
    assert api.get(post_id)["status"] == "published"


def test_live_story_is_resolved_without_media_ref(api, task, sessions, ig):
    post_id = api.post("story", "", [api.upload()])
    api.publish_now(post_id)
    ig.fail_next("publish", Unavailable("502"), after=True)

    task(sessions)
    task(sessions)

    post = api.get(post_id)
    assert (post["status"], post["external_ref"]) == ("published", None)


def test_published_post_does_not_change(api, client, headers, task, sessions):
    post_id = api.post()
    api.publish_now(post_id)
    task(sessions)
    before = api.get(post_id)

    responses = [
        client.patch(f"/v1/posts/{post_id}", json={"caption": "x"}, headers=headers),
        client.post(f"/v1/posts/{post_id}/publish-now", json={}, headers=headers),
        client.post(f"/v1/posts/{post_id}/cancel", headers=headers),
        client.post(
            f"/v1/posts/{post_id}/resolve", json={"outcome": "not_published"}, headers=headers
        ),
    ]
    task(sessions)

    assert [r.status_code for r in responses] == [409] * 4
    assert api.get(post_id) == before


# Containers that do not get ready


def test_container_error_fails_the_post(api, sessions, ig, clock, settings, requests):
    post_id = api.post()
    api.publish_now(post_id)
    containers = []

    def sleep(seconds):
        ig.mark_error(containers[-1])
        clock.advance(seconds=seconds)

    original = ig.create_container
    ig.create_container = lambda r: containers.append(original(r)) or containers[-1]
    PublishTask(ig, clock, settings.media_dir, BASE_URL, sleep=sleep)(sessions)

    post = api.get(post_id)
    assert (post["status"], post["failure"]) == ("failed", "container_error")
    assert post["attempts"][0]["result"] == "rejected"
    assert published(ig) == []


def test_slow_container_times_out(api, sessions, clock, settings):
    slow = FakeInstagram(clock, video_processing=timedelta(hours=1))
    post_id = api.post()
    api.publish_now(post_id)

    PublishTask(
        slow, clock, settings.media_dir, BASE_URL, sleep=lambda s: clock.advance(seconds=s)
    )(sessions)

    post = api.get(post_id)
    assert (post["status"], post["failure"]) == ("failed", "container_timeout")
    assert clock.now() - START < timedelta(minutes=11)


def test_server_error_before_publishing_fails_without_doubt(api, task, sessions, ig):
    post_id = api.post()
    api.publish_now(post_id)
    ig.fail_next("create_container", Unavailable("503"))

    task(sessions)

    post = api.get(post_id)
    assert (post["status"], post["failure"]) == ("failed", "unavailable")
    assert post["attempts"][0]["result"] == "server_error"


# Crashes and schedule


def test_crash_before_any_container_is_retried(api, task, sessions, ig, clock):
    post_id = api.post()
    api.publish_now(post_id)
    ig.fail_next("create_container", RuntimeError("worker killed"))

    with pytest.raises(RuntimeError):
        task(sessions)
    assert api.get(post_id)["status"] == "publishing"

    clock.advance(seconds=STALE_ATTEMPT.total_seconds() - 60)
    task(sessions)
    assert api.get(post_id)["status"] == "publishing"  # not stale yet

    clock.advance(minutes=2)
    task(sessions)
    task(sessions)

    post = api.get(post_id)
    assert post["status"] == "published"
    assert [(a["result"], a["resolved_as"]) for a in post["attempts"]] == [
        ("unknown", "not_published"),
        ("ok", None),
    ]


def test_crash_after_going_live_is_resolved_as_published(api, task, sessions, ig, clock):
    post_id = api.post()
    api.publish_now(post_id)
    ig.fail_next("publish", RuntimeError("worker killed"), after=True)

    with pytest.raises(RuntimeError):
        task(sessions)
    clock.advance(seconds=STALE_ATTEMPT.total_seconds() + 60)
    task(sessions)

    post = api.get(post_id)
    [live] = published(ig)
    assert (post["status"], post["external_ref"]) == ("published", live.id)


def test_missed_schedule_beyond_grace_fails(api, client, headers, task, sessions, ig, clock):
    late = api.post()
    on_time = api.post()
    for post_id in (late, on_time):
        client.post(
            f"/v1/posts/{post_id}/schedule",
            json={"scheduled_for": "2026-10-29T13:00:00Z"},
            headers=headers,
        )
    client.patch(f"/v1/posts/{on_time}", json={}, headers=headers)
    client.post(
        f"/v1/posts/{on_time}/schedule",
        json={"scheduled_for": "2026-10-29T13:20:00Z"},
        headers=headers,
    )

    clock.advance(hours=1, minutes=31)  # 13:31
    task(sessions)

    assert (api.get(late)["status"], api.get(late)["failure"]) == ("failed", "missed_schedule")
    assert api.get(on_time)["status"] == "published"
    assert len(published(ig)) == 1


def test_manual_resolution(api, client, headers, task, sessions, ig):
    published_id, lost_id = api.post(), api.post()
    for post_id in (published_id, lost_id):
        api.publish_now(post_id)
        ig.fail_next("publish", Unavailable("502"))
    task.publish_due(sessions)
    for _ in range(2):
        ig.fail_next("container_status", Unavailable("down"))
    task.resolve(sessions)  # the resolver could not check either, so both stay unknown

    yes = client.post(
        f"/v1/posts/{published_id}/resolve",
        json={"outcome": "published", "media_ref": "17999"},
        headers=headers,
    )
    no = client.post(
        f"/v1/posts/{lost_id}/resolve", json={"outcome": "not_published"}, headers=headers
    )
    task(sessions)

    assert yes.status_code == 200, yes.text
    assert (yes.json()["status"], yes.json()["external_ref"]) == ("published", "17999")
    assert yes.json()["attempts"][0]["resolved_as"] == "manual"
    assert no.json()["status"] == "draft"
    assert published(ig) == []  # the worker did not touch what was settled by hand


def test_task_refuses_to_run_without_public_url(api, sessions, ig, clock, settings):
    post_id = api.post()
    api.publish_now(post_id)

    with pytest.raises(RuntimeError, match="PUBLIC_URL"):
        PublishTask(ig, clock, settings.media_dir, None)(sessions)

    assert api.get(post_id)["status"] == "scheduled"


# Files and stories


def test_files_are_purged_seven_days_after_publishing_unless_still_in_use(
    api, task, sessions, clock, settings
):
    shared = api.upload()
    post_id = api.post("image", "One", [shared])
    only_published = api.upload(JPEG + b"x")
    other_id = api.post("image", "Two", [only_published])
    api.post("image", "Draft reusing the file", [shared])
    api.publish_now(post_id)
    api.publish_now(other_id)
    task(sessions)

    clock.advance(days=6)
    task(sessions)
    assert len(list(settings.media_dir.iterdir())) == 2

    clock.advance(days=1, minutes=1)
    task(sessions)

    assert [p.stem for p in settings.media_dir.iterdir()] == [shared]
    with sessions() as session:
        assert session.get(Media, only_published).deleted_at is not None
        assert session.get(Media, shared).deleted_at is None


def test_published_stories_get_snapshots_during_their_first_day(api, task, sessions, ig, clock):
    post_id = api.post("story", "", [api.upload()])
    api.publish_now(post_id)
    task(sessions)
    ref = api.get(post_id)["external_ref"]
    snapshots = SnapshotTask(ig, clock)

    def taken():
        with sessions() as session:
            return session.scalars(
                select(PostSnapshot.product_type).where(PostSnapshot.media_ref == ref)
            ).all()

    snapshots(sessions)
    clock.advance(hours=2)
    snapshots(sessions)
    assert taken() == ["STORY"]

    clock.advance(hours=4)
    snapshots(sessions)
    assert len(taken()) == 2

    for _ in range(4):
        clock.advance(hours=6)
        snapshots(sessions)
    assert len(taken()) == 4  # at 0, 6, 12 and 18 hours; nothing once the story is gone


def test_crash_noticed_after_the_grace_window_still_publishes(api, task, sessions, ig, clock):
    """Nothing went out, so the post is due again now rather than failed as a missed schedule."""
    post_id = api.post()
    api.publish_now(post_id)
    ig.fail_next("create_container", RuntimeError("worker killed"))
    with pytest.raises(RuntimeError):
        task(sessions)

    clock.advance(seconds=STALE_ATTEMPT.total_seconds() + 60)
    task(sessions)
    task(sessions)

    assert api.get(post_id)["status"] == "published"


def stale():
    return STALE_ATTEMPT.total_seconds() + 60


def test_a_post_that_keeps_crashing_the_worker_stops_after_three_attempts(
    api, task, sessions, ig, clock
):
    post_id = api.post()
    api.publish_now(post_id)
    for _ in range(3):
        ig.fail_next("create_container", RuntimeError("worker killed"))
        with pytest.raises(RuntimeError):
            task(sessions)
        clock.advance(seconds=stale())
        task.resolve(sessions)

    post = api.get(post_id)
    assert (post["status"], post["failure"]) == ("failed", "too_many_attempts")
    assert len(post["attempts"]) == 3
    task(sessions)
    assert published(ig) == []


def test_stale_threshold_outlasts_the_longest_legitimate_attempt():
    assert STALE_ATTEMPT > LONGEST_ATTEMPT > timedelta(minutes=11 * 10)


def test_a_second_worker_does_not_republish_a_long_carousel_in_progress(
    api, sessions, ig, clock, settings
):
    """Slow carousel children keep worker A busy far beyond the old 15-minute threshold while
    worker B runs on the same database: B must not take A's attempt for dead."""
    slow = timedelta(minutes=9)
    ig.video_processing = slow
    items = [api.upload(MP4 + bytes([i]), "video/mp4") for i in range(4)]
    post_id = api.post("carousel", "Long carousel", items)
    api.publish_now(post_id)
    second = PublishTask(
        ig, clock, settings.media_dir, BASE_URL, sleep=lambda s: clock.advance(seconds=s)
    )

    # B was already running before A's attempt started (the lock prevents this in production;
    # the long threshold is the safety net), so only the threshold can protect A's attempt.
    second(sessions)

    def sleep(seconds):
        clock.advance(seconds=seconds)
        second(sessions)  # B runs on every pass while A waits on Instagram

    PublishTask(ig, clock, settings.media_dir, BASE_URL, sleep=sleep)(sessions)

    post = api.get(post_id)
    assert clock.now() - START > timedelta(minutes=30)  # A really ran that long
    assert post["status"] == "published"
    assert [a["result"] for a in post["attempts"]] == ["ok"]
    assert len(published(ig)) == 1


def test_an_attempt_closed_by_someone_else_is_left_alone(api, task, sessions, ig, clock):
    from jorgex_social.publishing.models import AttemptResult, PublishAttempt

    post_id = api.post()
    api.publish_now(post_id)
    original = ig.publish

    def publish_after_takeover(container):
        # Another process declared this attempt dead while the call was in flight.
        with sessions() as session, session.begin():
            session.scalar(select(PublishAttempt)).result = AttemptResult.UNKNOWN
        return original(container)

    ig.publish = publish_after_takeover
    task.publish_due(sessions)

    post = api.get(post_id)
    assert post["status"] == "publishing"  # untouched by the late answer
    assert post["attempts"][0]["result"] == "unknown"


def test_purge_keeps_failed_posts_and_drops_unused_files_after_a_week(
    api, client, headers, task, sessions, ig, clock, settings
):
    unused = api.upload(JPEG + b"unused")
    cancelled = api.upload(JPEG + b"cancelled")
    failed = api.upload(JPEG + b"failed")
    cancelled_post = api.post("image", "Cancelled", [cancelled])
    client.post(f"/v1/posts/{cancelled_post}/cancel", headers=headers)
    failed_post = api.post("image", "Refused", [failed])
    api.publish_now(failed_post)
    ig.fail_next("publish", Refused("bad", code=100))
    task(sessions)
    assert api.get(failed_post)["status"] == "failed"

    clock.advance(days=6, hours=23)
    task.purge(sessions)
    assert len(list(settings.media_dir.iterdir())) == 3

    clock.advance(hours=2)
    task.purge(sessions)

    assert [p.stem for p in settings.media_dir.iterdir()] == [failed]
    with sessions() as session:
        assert session.get(Media, unused).deleted_at is not None
        assert session.get(Media, cancelled).deleted_at is not None


def test_stale_attempt_and_manual_resolution_revoke_public_urls(
    api, client, headers, task, sessions, ig, clock
):
    post_id = api.post()
    api.publish_now(post_id)
    ig.fail_next("create_container", RuntimeError("worker killed"))
    with pytest.raises(RuntimeError):
        task(sessions)
    with sessions() as session:
        assert session.scalar(select(Media)).url_token is not None

    clock.advance(seconds=stale())
    task.resolve(sessions)
    with sessions() as session:
        assert session.scalar(select(Media)).url_token is None

    task(sessions)  # the first post goes out again and is done
    assert api.get(post_id)["status"] == "published"

    # Manual resolution of an attempt whose URL is still live.
    other = api.post(caption="Second")
    api.publish_now(other)
    ig.fail_next("publish", Unavailable("502"))
    task.publish_due(sessions)
    with sessions() as session, session.begin():
        from jorgex_social.publishing.media import issue_url

        for media in session.scalars(select(Media)):
            issue_url(media, clock.now())
    resolved = client.post(
        f"/v1/posts/{other}/resolve", json={"outcome": "not_published"}, headers=headers
    )
    assert resolved.status_code == 200, resolved.text

    item = api.get(other)["items"][0]
    with sessions() as session:
        assert session.get(Media, item).url_token is None


def test_a_restarted_worker_resolves_the_dead_attempt_at_once(api, task, sessions, ig, clock):
    post_id = api.post()
    api.publish_now(post_id)
    ig.fail_next("create_container", RuntimeError("worker killed"))
    with pytest.raises(RuntimeError):
        task(sessions)

    clock.advance(minutes=1)  # the process restarts with a fresh task
    restarted = PublishTask(
        ig, clock, task.media_dir, BASE_URL, sleep=lambda s: clock.advance(seconds=s)
    )
    restarted(sessions)
    restarted(sessions)

    post = api.get(post_id)
    assert post["status"] == "published"
    assert [(a["result"], a["resolved_as"]) for a in post["attempts"]] == [
        ("unknown", "not_published"),
        ("ok", None),
    ]


# Kill switch, incidents and audit (build step 5)


@pytest.fixture
def switch(app, client):
    with app.state.sessionmaker() as session, session.begin():
        _, secret = create_token(session, "operator", {"account": "write"})
    account_headers = {"Authorization": f"Bearer {secret}"}

    def set_switch(enabled):
        response = client.put(
            "/v1/account/kill-switch", json={"enabled": enabled}, headers=account_headers
        )
        assert response.status_code == 200, response.text

    return set_switch


@pytest.fixture
def publishes(ig):
    calls = []
    original = ig.publish

    def publish(container):
        calls.append(container)
        return original(container)

    ig.publish = publish
    return calls


def test_kill_switch_stops_containers_and_publishing(
    api, client, headers, task, sessions, clock, switch, requests, publishes
):
    late, on_time = api.post(), api.post()
    switch(True)
    for post_id, at in ((late, "2026-10-29T12:10:00Z"), (on_time, "2026-10-29T12:50:00Z")):
        client.post(f"/v1/posts/{post_id}/schedule", json={"scheduled_for": at}, headers=headers)

    clock.advance(hours=1)  # both due, the first beyond the 30-minute grace
    task(sessions)
    assert (requests, publishes) == ([], [])
    assert {api.get(late)["status"], api.get(on_time)["status"]} == {"scheduled"}

    switch(False)
    task(sessions)

    assert (api.get(late)["status"], api.get(late)["failure"]) == ("failed", "missed_schedule")
    assert api.get(on_time)["status"] == "published"


def test_kill_switch_turned_on_mid_attempt_stops_before_media_publish(
    api, sessions, ig, clock, settings, switch, publishes
):
    post_id, next_id = api.post(), api.post()
    api.publish_now(post_id)
    api.publish_now(next_id)

    def sleep(seconds):
        switch(True)  # while the video container processes
        clock.advance(seconds=seconds)

    PublishTask(ig, clock, settings.media_dir, BASE_URL, sleep=sleep)(sessions)

    post = api.get(post_id)
    assert publishes == []
    assert (post["status"], post["failure"]) == ("failed", "kill_switch")
    assert post["attempts"][0]["result"] == "rejected"
    assert api.get(next_id)["status"] == "scheduled"  # the next due post is not even started


def test_kill_switch_holds_the_resolvers_retry(api, task, sessions, ig, switch, publishes):
    post_id = api.post()
    api.publish_now(post_id)
    ig.fail_next("publish", Unavailable("lost before Meta acted"))
    task.publish_due(sessions)
    publishes.clear()

    switch(True)
    task(sessions)
    assert publishes == []
    assert api.get(post_id)["status"] == "outcome_unknown"

    switch(False)
    task(sessions)
    assert api.get(post_id)["status"] == "published"
    assert len(published(ig)) == 1


def test_unresolved_outcome_opens_an_incident_after_an_hour_and_closes(
    api, task, sessions, ig, clock, settings
):
    from jorgex_social.account.connection import ConnectionTask
    from jorgex_social.account.models import ChannelIncident

    watcher = ConnectionTask(ig, clock, settings.secret_key_file)
    post_id = api.post()
    api.publish_now(post_id)
    ig.fail_next("publish", Unavailable("502"), after=True)
    task.publish_due(sessions)

    def incidents():
        with sessions() as session:
            return [
                (i.type, i.closed_at is None, i.detail)
                for i in session.scalars(
                    select(ChannelIncident).where(ChannelIncident.type == "publish_unresolved")
                )
            ]

    clock.advance(minutes=59)
    watcher.watch_unresolved(sessions, clock.now())
    assert incidents() == []

    clock.advance(minutes=2)
    watcher.watch_unresolved(sessions, clock.now())
    watcher.watch_unresolved(sessions, clock.now())
    assert incidents() == [("publish_unresolved", True, {"post_ids": [post_id]})]

    task(sessions)  # the resolver settles it
    watcher.watch_unresolved(sessions, clock.now())
    assert api.get(post_id)["status"] == "published"
    assert incidents() == [("publish_unresolved", False, {"post_ids": [post_id]})]


def test_worker_transitions_are_audited_as_system(api, task, sessions, ig):
    from jorgex_social.account.models import AuditEntry

    published_id, refused_id = api.post(), api.post()
    api.publish_now(published_id)
    task(sessions)
    api.publish_now(refused_id)
    ig.fail_next("publish", Refused("bad", code=100))
    task(sessions)

    with sessions() as session:
        entries = session.scalars(
            select(AuditEntry).where(AuditEntry.actor_type == "system").order_by(AuditEntry.id)
        ).all()
    assert [(e.action, e.object_id, e.outcome, e.detail) for e in entries] == [
        ("post.published", published_id, "ok", None),
        ("post.failed", refused_id, "error", {"failure": "refused"}),
    ]
