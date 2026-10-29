"""Media files on disk and the short-lived public URLs Instagram downloads them from."""

import secrets
from datetime import datetime, timedelta
from pathlib import Path

from jorgex_social.publishing.models import Media, MediaType

MAX_UPLOAD_BYTES = 300 * 1024 * 1024
# Instagram fetches the file while the container processes; a container lives 24 hours.
URL_LIFETIME = timedelta(hours=24)
# Kept this long after publishing, in case it has to be checked or published again by hand.
KEEP_AFTER_PUBLISH = timedelta(days=7)

UPLOAD_CONTENT_TYPES = {"image/jpeg", "video/mp4", "video/quicktime"}
CONTENT_TYPE = {
    MediaType.JPEG: "image/jpeg",
    MediaType.MP4: "video/mp4",
    MediaType.MOV: "video/quicktime",
}
# Enough to read the JPEG marker or the MP4/MOV `ftyp` box with its major brand.
SNIFF_BYTES = 12


def sniff(head: bytes) -> MediaType | None:
    """The type comes from the file signature, never from what the client claims."""
    if head[:3] == b"\xff\xd8\xff":
        return MediaType.JPEG
    if head[4:8] == b"ftyp":
        return MediaType.MOV if head[8:12] == b"qt  " else MediaType.MP4
    return None


def path_of(media_dir: Path, media: Media) -> Path:
    return media_dir / media.relative_path


def public_url(base_url: str, media: Media) -> str:
    return f"{base_url}/m/{media.url_token}"


def issue_url(media: Media, now: datetime) -> None:
    media.url_token = secrets.token_urlsafe(32)
    media.url_expires_at = now + URL_LIFETIME


def revoke_url(media: Media) -> None:
    media.url_token = None
    media.url_expires_at = None
