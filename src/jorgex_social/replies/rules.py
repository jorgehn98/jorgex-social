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
from sqlalchemy import select
from sqlalchemy.orm import Session

from jorgex_social.replies.models import Channel, Rule

# instagram.md, «Límites»: 1,000 bytes per message with Instagram Login; 2,200 characters is
# the caption limit, used for public replies.
MAX_MESSAGE_BYTES = 1000
MAX_PUBLIC_CHARS = 2200
MAX_VARIANTS = 20
MAX_KEYWORDS = 50


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


def check(channel: Channel, media_ref: str | None, content: RuleContent) -> None:
    """What depends on the channel. Raises ValueError with a message for the caller."""
    if channel == Channel.MESSAGE:
        if media_ref is not None:
            raise ValueError("media_ref only applies to comment rules")
        if content.public_replies:
            raise ValueError("message rules cannot have public_replies")


def normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.casefold().split())


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
        if rule.media_ref is not None and rule.media_ref != media_ref:
            continue
        if occurred_at is not None and (
            rule.activated_at is None or occurred_at < rule.activated_at
        ):
            continue
        if matches(rule.content, text):
            return rule
    return None
