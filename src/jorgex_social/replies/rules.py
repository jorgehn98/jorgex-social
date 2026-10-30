"""What a rule may contain, and whether a text fires it.

Matching ignores case and accents: both sides are lowercased and stripped of diacritics
(Unicode NFKD), and runs of whitespace count as one space.
"""

import re
import unicodedata
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from jorgex_social.account import audit
from jorgex_social.publishing.models import Post
from jorgex_social.replies.models import Channel, Event, Rule

# instagram.md, «Límites»: 1,000 bytes per message with Instagram Login; 2,200 characters is
# the caption limit, used for public replies.
MAX_MESSAGE_BYTES = 1000
MAX_PUBLIC_CHARS = 2200
MAX_VARIANTS = 20
MAX_KEYWORDS = 50
# Our own floor, not a Meta rule: an automation tool's interface asks for 3 and users of these
# tools suggest 3 to 5, with no number from Meta. Checked in `check`, not in `RuleContent`, so
# content stored with fewer still loads.
MIN_PUBLIC_VARIANTS = 3
LINK_MARKS = ("http://", "https://", "www.")
# The evidence behind every warning is community reports, so none of them blocks a rule.
_REPORTED = "Reported practice, not a published Meta rule: "


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


def _fits_a_message(value: str) -> str:
    if len(value.encode()) > MAX_MESSAGE_BYTES:
        raise ValueError(f"must be at most {MAX_MESSAGE_BYTES} bytes in UTF-8")
    return value


def _matchable(value: str) -> str:
    # Only combining marks, for example: nothing would be left to compare.
    if not normalize(value):
        raise ValueError("must keep some letters once accents and case are ignored")
    return value


Keyword = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    AfterValidator(_matchable),
]
PublicText = Annotated[str, Field(max_length=MAX_PUBLIC_CHARS), AfterValidator(_not_blank)]
PrivateText = Annotated[str, AfterValidator(_not_blank), AfterValidator(_fits_a_message)]


class RuleContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keywords: list[Keyword] = Field(min_length=1, max_length=MAX_KEYWORDS)
    # Any of these cancels the match.
    exclude: list[Keyword] = Field(default_factory=list, max_length=MAX_KEYWORDS)
    # word: whole word or phrase; contains: anywhere, even inside a word; exact: the whole text.
    match: Literal["word", "contains", "exact"] = "word"
    # One variant is picked at random. Public replies only exist for comments.
    public_replies: list[PublicText] = Field(default_factory=list, max_length=MAX_VARIANTS)
    # The private reply to a comment, or the answer to a direct message.
    private_replies: list[PrivateText] = Field(default_factory=list, max_length=MAX_VARIANTS)

    @model_validator(mode="after")
    def _has_a_reply(self) -> RuleContent:
        if not self.public_replies and not self.private_replies:
            raise ValueError("give at least one public_replies or private_replies variant")
        return self


def check(
    channel: Channel, media_ref: str | None, content: RuleContent, post_id: str | None = None
) -> None:
    """What a rule needs on top of its content's own shape, the channel first. Raises ValueError
    with a message for the caller."""
    if channel == Channel.MESSAGE:
        if media_ref is not None:
            raise ValueError("media_ref only applies to comment rules")
        if post_id is not None:
            raise ValueError("post_id only applies to comment rules")
        if content.public_replies:
            raise ValueError("message rules cannot have public_replies")
    if media_ref is not None and post_id is not None:
        raise ValueError("give media_ref or post_id, not both")
    distinct = {normalize(text) for text in content.public_replies}
    if content.public_replies and len(distinct) < MIN_PUBLIC_VARIANTS:
        raise ValueError(
            f"public_replies needs at least {MIN_PUBLIC_VARIANTS} variants that differ in more "
            f"than case, accents or spacing, or none at all (found {len(distinct)}); this is "
            "this service's own floor, not a Meta rule"
        )


def rule_warnings(
    channel: str, media_ref: str | None, content: dict, post_id: str | None = None
) -> list[dict[str, str]]:
    """What automation users report as risky in a rule. A rule with warnings is still valid."""
    public = content.get("public_replies") or []
    private = content.get("private_replies") or []
    found = []

    def warn(code: str, message: str) -> None:
        found.append({"code": code, "message": _REPORTED + message})

    if channel == Channel.COMMENT:
        if public and media_ref is None and post_id is None:
            warn("public_on_every_post", "public replies on every post can look like spam.")
        if any(mark in text.casefold() for text in private for mark in LINK_MARKS):
            warn("link_in_first_message", "a link in the first private message can look like spam.")
    if any(not any(c.isalnum() for c in text) for text in [*public, *private]):
        warn("no_words", "a reply of only emoji or punctuation can look like spam.")
    return found


def normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.casefold().split())


def _is_edge_mark(char: str) -> bool:
    return char.isspace() or unicodedata.category(char)[0] in "PS"


def opt_out_form(text: str) -> str:
    """`text` normalized and without punctuation or symbols at its ends, as people write "stop."
    or "¡baja!". Inside it nothing changes, so "stop it" stays different from "stop"."""
    normalized = normalize(text)
    return normalized.strip("".join(filter(_is_edge_mark, normalized)))


def _hit(mode: str, text: str, keyword: str) -> bool:
    match mode:
        case "exact":
            return text == keyword
        case "contains":
            return keyword in text
        case _:
            return re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", text) is not None


def matches(content: dict, text: str) -> bool:
    """Whether `text` fires a rule with this (validated) content."""
    mode = content.get("match", "word")
    normalized = normalize(text)
    if not any(_hit(mode, normalized, normalize(k)) for k in content["keywords"]):
        return False
    return not any(_hit(mode, normalized, normalize(k)) for k in content.get("exclude", []))


def active_rules(session: Session, network: str, channel: Channel) -> list[Rule]:
    """In the order they are tried: lower priority first, then the oldest."""
    return list(
        session.scalars(
            select(Rule)
            .where(
                Rule.network == network,
                Rule.channel == channel,
                Rule.active.is_(True),
                Rule.archived_at.is_(None),
            )
            .order_by(Rule.priority, Rule.created_at, Rule.id)
        )
    )


def first_match(
    rules: Sequence[Rule],
    text: str,
    *,
    media_ref: str | None = None,
    occurred_at: datetime | None = None,
) -> Rule | None:
    """The first rule `text` fires. With `occurred_at`, only rules active since before then."""
    for rule in rules:
        if rule.waiting_for_post:
            continue
        if rule.media_ref is not None and rule.media_ref != media_ref:
            continue
        if occurred_at is not None and (
            rule.activated_at is None or occurred_at < rule.activated_at
        ):
            continue
        if matches(rule.content, text):
            return rule
    return None


def bind_to_published(session: Session, post: Post, now: datetime) -> None:
    """Points every rule waiting for `post` at its media. Call it in the transaction that marks
    the post published. Without a media id the rules keep waiting."""
    if post.external_ref is None:
        return
    waiting = session.scalars(
        select(Rule).where(Rule.post_id == post.id, Rule.media_ref.is_(None))
    ).all()
    if not waiting:
        return
    for rule in waiting:
        rule.media_ref = post.external_ref
        rule.updated_at = now
        audit.record(
            session,
            now,
            "rule.bound_to_post",
            object_type="rule",
            object_id=rule.id,
            detail={"post_id": post.id, "media_ref": post.external_ref},
        )
    # Comments seen while the rule waited cannot be evaluated again here: their text was never
    # stored. Forgetting them makes the next poll read them as new. rule_id NULL means no
    # deliveries (Intake._record), so no foreign key stops this and nothing answered is forgotten.
    # Timestamps come back from Instagram, so no reply window moves.
    session.execute(
        delete(Event).where(
            Event.network == post.network,
            Event.type == Channel.COMMENT,
            Event.media_ref == post.external_ref,
            Event.rule_id.is_(None),
        )
    )
