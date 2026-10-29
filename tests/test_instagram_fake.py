from datetime import timedelta

import pytest

from jorgex_social.clock import ManualClock
from jorgex_social.instagram.errors import Refused, Unavailable
from jorgex_social.instagram.fake import FakeInstagram
from jorgex_social.instagram.types import (
    ContainerKind,
    ContainerRequest,
    ContainerStatus,
    ProductType,
)

IMAGE = ContainerRequest(ContainerKind.IMAGE, url="https://x.test/a.jpg", caption="hello")
REEL = ContainerRequest(ContainerKind.REELS, url="https://x.test/a.mp4", caption="reel")
STORY = ContainerRequest(ContainerKind.STORIES, url="https://x.test/s.jpg")


@pytest.fixture
def clock():
    return ManualClock()


@pytest.fixture
def ig(clock):
    return FakeInstagram(clock)


def publish(ig, request):
    return ig.publish(ig.create_container(request))


def test_image_is_ready_at_once_and_publishes(ig):
    container = ig.create_container(IMAGE)
    assert ig.container_status(container) == ContainerStatus.FINISHED

    media_id = ig.publish(container)

    assert ig.container_status(container) == ContainerStatus.PUBLISHED
    assert ig.media(media_id).caption == "hello"
    assert ig.media(media_id).product_type == ProductType.FEED


def test_video_processes_before_it_can_be_published(ig, clock):
    container = ig.create_container(REEL)

    assert ig.container_status(container) == ContainerStatus.IN_PROGRESS
    with pytest.raises(Refused):
        ig.publish(container)

    clock.advance(seconds=31)
    assert ig.container_status(container) == ContainerStatus.FINISHED
    assert ig.media(ig.publish(container)).product_type == ProductType.REELS


def test_container_expires_after_24_hours(ig, clock):
    container = ig.create_container(IMAGE)
    clock.advance(hours=24, seconds=1)

    assert ig.container_status(container) == ContainerStatus.EXPIRED
    with pytest.raises(Refused):
        ig.publish(container)


def test_processing_error(ig):
    container = ig.create_container(REEL)
    ig.mark_error(container)

    assert ig.container_status(container) == ContainerStatus.ERROR


def test_a_container_publishes_only_once(ig):
    container = ig.create_container(IMAGE)
    ig.publish(container)

    with pytest.raises(Refused):
        ig.publish(container)


def test_server_error_after_going_live_leaves_the_post_published(ig):
    """The 'outcome unknown' case: the caller sees a 5xx, but the post exists."""
    container = ig.create_container(IMAGE)
    ig.fail_next("publish", Unavailable("Instagram API 502"), after=True)

    with pytest.raises(Unavailable):
        ig.publish(container)

    assert ig.container_status(container) == ContainerStatus.PUBLISHED
    assert [m.caption for m in ig.list_media()] == ["hello"]


def test_error_before_the_call_changes_nothing(ig):
    container = ig.create_container(IMAGE)
    ig.fail_next("publish", Unavailable("connection reset"))

    with pytest.raises(Unavailable):
        ig.publish(container)

    assert ig.container_status(container) == ContainerStatus.FINISHED


def test_carousel_needs_ready_carousel_items(ig, clock):
    image = ig.create_container(
        ContainerRequest(ContainerKind.IMAGE, url="i", is_carousel_item=True)
    )
    video = ig.create_container(
        ContainerRequest(ContainerKind.VIDEO, url="v", is_carousel_item=True)
    )
    carousel = ContainerRequest(ContainerKind.CAROUSEL, children=(image, video), caption="c")

    with pytest.raises(Refused):
        ig.create_container(carousel)  # the video is still processing
    clock.advance(minutes=1)
    media = ig.media(publish(ig, carousel))

    assert media.media_type == "CAROUSEL_ALBUM"
    with pytest.raises(Refused):
        ig.publish(image)  # a carousel item is never published on its own


@pytest.mark.parametrize("children", [("1",), tuple(str(i) for i in range(21))])
def test_carousel_size(ig, children):
    with pytest.raises(Refused):
        ig.create_container(ContainerRequest(ContainerKind.CAROUSEL, children=children))


def test_stories_are_not_listed_and_their_insights_expire(ig, clock):
    story = publish(ig, STORY)
    publish(ig, IMAGE)

    assert [m.product_type for m in ig.list_media()] == [ProductType.FEED]
    assert ig.media_insights(story, ProductType.STORY)["reach"] > 0
    clock.advance(hours=25)
    with pytest.raises(Refused):
        ig.media_insights(story, ProductType.STORY)


def test_list_media_is_newest_first(ig, clock):
    first = publish(ig, IMAGE)
    clock.advance(minutes=1)
    second = publish(ig, IMAGE)

    assert [m.id for m in ig.list_media()] == [second, first]
    assert [m.id for m in ig.list_media(max_items=1)] == [second]


def test_publishing_quota(clock):
    ig = FakeInstagram(clock, quota_total=2)
    publish(ig, IMAGE)
    publish(ig, IMAGE)

    assert ig.publishing_limit().quota_usage == 2
    with pytest.raises(Refused):
        publish(ig, IMAGE)
    clock.advance(hours=24, seconds=1)
    publish(ig, IMAGE)


def test_account_insights_window(ig, clock):
    now = clock.now()

    assert ig.account_insights(now - timedelta(days=7), now)["reach"] > 0
    with pytest.raises(ValueError):
        ig.account_insights(now - timedelta(days=31), now)
