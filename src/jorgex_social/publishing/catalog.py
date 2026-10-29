"""What each post type accepts, checked on the server before a post can be scheduled.

Served as is by GET /v1/catalog, so clients never duplicate these limits.
"""

import hashlib
import json
from dataclasses import dataclass

from jorgex_social.publishing.models import Media, MediaType, PostType

MB = 1024 * 1024
MAX_IMAGE_BYTES = 8 * MB
MAX_VIDEO_BYTES = 300 * MB
MAX_STORY_VIDEO_BYTES = 100 * MB
MAX_CAPTION_CHARS = 2200
MAX_HASHTAGS = 30
# Meta documents 20, but 10 is what has been used in production and the difference is unverified.
MAX_CAROUSEL_ITEMS = 10

CATALOG = {
    PostType.IMAGE: {
        "items": {"min": 1, "max": 1, "types": ["jpeg"], "max_bytes": {"image": MAX_IMAGE_BYTES}},
        "caption": "required",
        "cover": False,
    },
    PostType.REEL: {
        "items": {
            "min": 1,
            "max": 1,
            "types": ["mp4", "mov"],
            "max_bytes": {"video": MAX_VIDEO_BYTES},
        },
        "caption": "required",
        "cover": {"types": ["jpeg"], "max_bytes": MAX_IMAGE_BYTES},
        "thumb_offset_ms": {"min": 0},
    },
    PostType.CAROUSEL: {
        "items": {
            "min": 2,
            "max": MAX_CAROUSEL_ITEMS,
            "types": ["jpeg", "mp4", "mov"],
            "max_bytes": {"image": MAX_IMAGE_BYTES, "video": MAX_VIDEO_BYTES},
        },
        "caption": "required",
        "cover": False,
    },
    PostType.STORY: {
        "items": {
            "min": 1,
            "max": 1,
            "types": ["jpeg", "mp4", "mov"],
            "max_bytes": {"image": MAX_IMAGE_BYTES, "video": MAX_STORY_VIDEO_BYTES},
        },
        "caption": "ignored",
        "cover": False,
    },
}
CAPTION_LIMITS = {"max_chars": MAX_CAPTION_CHARS, "max_hashtags": MAX_HASHTAGS}


@dataclass(frozen=True)
class Issue:
    code: str
    message: str


@dataclass(frozen=True)
class Check:
    errors: list[Issue]
    warnings: list[Issue]


def _max_bytes(post_type: PostType, media_type: MediaType) -> int:
    if not media_type.is_video:
        return MAX_IMAGE_BYTES
    return MAX_STORY_VIDEO_BYTES if post_type == PostType.STORY else MAX_VIDEO_BYTES


def _hashtags(caption: str) -> int:
    return sum(word.startswith("#") for word in caption.split())


def validate(
    post_type: PostType,
    caption: str,
    items: list[Media],
    cover: Media | None,
    thumb_offset_ms: int | None,
) -> Check:
    """Errors block scheduling; warnings do not. Video duration and aspect ratio are not checked:
    that would need ffprobe, a new dependency."""
    errors: list[Issue] = []
    warnings: list[Issue] = []
    rules = CATALOG[post_type]["items"]

    if not rules["min"] <= len(items) <= rules["max"]:
        count = (
            str(rules["min"])
            if rules["min"] == rules["max"]
            else f"{rules['min']} to {rules['max']}"
        )
        errors.append(Issue("item_count", f"A {post_type} needs {count} items."))
    for position, media in enumerate(items):
        media_type = MediaType(media.type)
        if media.deleted_at is not None:
            errors.append(Issue("media_deleted", f"Item {position} was deleted."))
        if media_type.value not in rules["types"]:
            errors.append(
                Issue(
                    "item_type",
                    f"Item {position} is {media_type}; a {post_type} takes "
                    f"{', '.join(rules['types'])}.",
                )
            )
        elif media.bytes > (limit := _max_bytes(post_type, media_type)):
            errors.append(Issue("item_too_large", f"Item {position} exceeds {limit // MB} MB."))
    if any(MediaType(m.type).is_video for m in items):
        warnings.append(
            Issue("duration_unchecked", "Video duration and aspect ratio are not checked.")
        )

    if cover is not None:
        if post_type != PostType.REEL:
            errors.append(Issue("cover_not_allowed", "Only reels take a cover image."))
        elif cover.type != MediaType.JPEG:
            errors.append(Issue("cover_type", "The cover must be a JPEG."))
        elif cover.bytes > MAX_IMAGE_BYTES:
            errors.append(
                Issue("cover_too_large", f"The cover exceeds {MAX_IMAGE_BYTES // MB} MB.")
            )
        if cover.deleted_at is not None:
            errors.append(Issue("media_deleted", "The cover was deleted."))

    if thumb_offset_ms is not None:
        if post_type != PostType.REEL:
            errors.append(Issue("thumb_offset_not_allowed", "Only reels take thumb_offset_ms."))
        elif thumb_offset_ms < 0:
            errors.append(Issue("thumb_offset_negative", "thumb_offset_ms cannot be negative."))
        elif cover is not None:
            warnings.append(
                Issue("thumb_offset_ignored", "With a cover image, Instagram ignores thumb_offset.")
            )

    if post_type == PostType.STORY:
        if caption.strip():
            warnings.append(
                Issue("caption_ignored", "Stories have no caption; it will be ignored.")
            )
    else:
        if not caption.strip():
            errors.append(Issue("caption_required", f"A {post_type} needs a caption."))
        if len(caption) > MAX_CAPTION_CHARS:
            errors.append(
                Issue("caption_too_long", f"Caption exceeds {MAX_CAPTION_CHARS} characters.")
            )
        if _hashtags(caption) > MAX_HASHTAGS:
            errors.append(Issue("too_many_hashtags", f"Caption exceeds {MAX_HASHTAGS} hashtags."))
    return Check(errors, warnings)


def content_hash(
    post_type: PostType,
    caption: str,
    items: list[Media],
    cover: Media | None,
    thumb_offset_ms: int | None,
) -> str:
    """Fingerprint of what would go live; schedule and publish-now refuse if it changed."""
    material = {
        "type": post_type.value,
        "caption": caption,
        "items": [m.sha256 for m in items],
        "cover": cover.sha256 if cover else None,
        "thumb_offset_ms": thumb_offset_ms,
    }
    raw = json.dumps(material, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()
