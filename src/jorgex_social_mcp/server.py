"""MCP server over stdio that turns each tool into one call to the jorgex-social REST API.

It adds no capability and no rule: the API decides everything and checks the token's permissions
on every call. Tools are registered only for the groups the token can use, as a convenience for
the agent, not as security.
"""

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from jorgex_social_mcp.client import Api, ApiError, check_token, check_url, segment

# By extension, so the API gets a Content-Type it accepts; the server re-checks the signature.
UPLOAD_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
}
# Plain strings, not enums: the API validates them, and its catalog is the only list to keep.
PostType = Annotated[
    str, Field(description="Post type: one of the keys of get_catalog's post_types.")
]
CLEARABLE = Literal["cover_media_id", "thumb_offset_ms", "source_ref"]
Channel = Annotated[
    str, Field(description="comment (top-level comments on posts) or message (direct messages).")
]
RuleId = Annotated[str, Field(description="Rule id, as returned by create_rule or list_rules.")]
RuleContent = Annotated[
    dict[str, Any],
    Field(
        description="keywords (list, required), exclude (list), match (word, contains or "
        "exact; default word), public_replies (comment rules only) and private_replies (the "
        "private reply to a comment, or the answer to a message): lists of variants, one picked "
        "at random. Matching ignores case and accents. The API validates it and says what is "
        "wrong."
    ),
]
ConversationId = Annotated[
    str, Field(description="Conversation id, as returned by list_conversations or list_events.")
]

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
# Acts on the real Instagram account unless the service runs in simulation.
ON_ACCOUNT = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)

_RANK = {"deny": 0, "read": 1, "write": 2}
# The level of a tool any valid token may call, whatever its permissions.
ANY = "any"

# What no single tool description says: the order of the flows, what touches the real account
# and what each error code asks of the agent. A backticked identifier here is always a tool name
# (tests check it), so the guide never points at a tool the server does not have.
_INSTRUCTIONS = """\
jorgex-social runs one Instagram account: posts (prepare, schedule, publish), automatic replies \
to comments and direct messages, and stored statistics. Each tool is one call to its REST API, \
which checks this token's permissions and enforces every rule.

Start with `get_token`, to see what this token may do, and `get_account`. With simulation \
false, `publish_now`, `schedule_post` (at its time), `activate_rule` and turning the kill \
switch off act on the real account, and a publication cannot be undone from here. With \
simulation true nothing reaches Instagram.

Publishing:
1. `get_catalog`: post types, item and caption limits, accepted files.
2. {upload}
3. `create_post` with the media ids and an idempotency_key, so a retry does not make a second \
draft. Fix its errors with `update_post`; content edits change content_hash, so review the \
one it returns.
4. Review the post (`get_post`), then `schedule_post` for a future time (ISO 8601 with offset) \
or `publish_now`. Always pass expected_hash, the content_hash you reviewed.
5. Follow it with `get_post`: scheduled or publishing means wait; then published (permalink \
may be null), failed (see failure; schedule it again once its cause is fixed) or \
outcome_unknown.
Editing a scheduled post returns it to draft. Only draft and scheduled posts can be edited or \
cancelled; to change a failed post, create a new one. A post not published within the grace \
window after its time (kill switch on, service down) fails as missed_schedule. With 50 posts \
published in the last 24 hours, the next due post fails as daily_limit without reaching Instagram; \
schedule it again for later. Do not write a caption that requires following, liking or \
commenting to receive promised content: Meta's spam policy lists engagement gating.

outcome_unknown: the service cannot tell whether Instagram published the post and keeps \
checking; after an hour it opens a publish_unresolved incident. Never schedule it again to \
retry: it could publish twice. You may settle it as published yourself only when \
`list_instagram_media` has a fetched_at later than the attempt's started_at (in `get_post`) \
and exactly one media with a timestamp after that started_at and the post's product_type and \
media_type (image: FEED and IMAGE; carousel: FEED and CAROUSEL_ALBUM; reel: REELS and VIDEO), \
and the operator did not publish by hand in that window (ask if unsure): then `resolve_post` \
with published and that id. Never resolve not_published without a human checking the account: \
absence from the listing proves nothing, and stories are never listed. Otherwise leave the post \
as it is and report its publish_unresolved incident.

Rules: `create_rule` makes an inactive rule. `test_rules` with its rule_id and sample texts \
shows what it would match and send, sending nothing. `activate_rule` makes it answer, only to \
what arrives afterwards. The first matching active rule wins, lowest priority number first. \
`list_deliveries` shows what was sent or why not; `pause_conversation` silences one person \
while a human answers, and reaches their comments only when Instagram uses the same id for a \
comment's author and a message's sender, which is not verified. Each rule answers a person once \
per post. Replies go out at a limited pace per minute, so a pending one may simply be waiting; \
public replies expire 24 hours after the comment and have their own, lower hourly cap. A sent \
delivery means Instagram accepted the reply, not that it was delivered.

A rule's warnings list what automation users report as risky (few public variants, public \
replies on every post, a link in the first private message, replies with no words). They are \
reported practice, not Meta's published rules, and block nothing: tell the operator and fix them \
if you can. Do not write rule copy that requires following, liking or commenting to receive \
promised content either.

Opt-out: a direct message that is exactly one of the operator's opt-out words pauses that person \
for good and fires no rule, and nothing is sent back. `list_conversations` shows it as \
paused_forever, and `audit_log` as conversation.opted_out. It reaches their comments under the \
same condition as a pause. Never `resume_conversation` or `pause_conversation` a paused_forever \
conversation unless a human asks: either one lifts the opt-out, a pause with until once that time \
passes. `test_rules` does not apply opt-out words.

Kill switch (`set_kill_switch`): on, nothing is written to Instagram. Scheduled posts wait, \
and replies due meanwhile are skipped for good. Off, publishing resumes; posts past their grace \
window fail as missed_schedule, and a post whose containers were being made when it turned on \
fails with failure kill_switch; both must be scheduled again. It also turns on by itself when \
Instagram reports the account as restricted, with an account_restricted incident holding Meta's \
code and subcode. Never turn it off on your own after that: ask a human to check the account \
first. Turning it off closes that incident, and if Instagram refuses again both come back. Open \
incidents (`list_incidents`) need a human; acknowledging one does not close it. After a \
restriction the API can keep answering while the account is limited, so calls that succeed prove \
nothing: a human must check Account Status in the Instagram app.

Lists are newest first unless said otherwise. Pass limit explicitly and page with offset until \
a page comes back shorter than limit; tools without offset say how to page in their \
description.

Errors are JSON with a stable code and the HTTP status; act on the code:
- invalid_token: the token is missing, unknown or revoked; stop. forbidden: this token lacks \
the group or level named in the error; do not retry, tell the operator.
- not_found: wrong id. invalid_state: the post or rule is not in a status that allows it (for \
a post, the error lists status and allowed).
- content_changed: the post changed since you reviewed it; read it again and review the new \
content_hash. invalid_post: it fails the catalog; see errors. unknown_media: those media ids \
do not exist (unused uploads are purged); upload the files again.
- in_the_past: use a future time, or publish_now. invalid_value, invalid_rule, or http_error \
with status 422: fix the arguments as the message or detail says.
- idempotency_key_taken: use another key. media_ref_taken: another post already has that \
media id; check it.
- public_url_missing: the operator must set the service's public URL; nothing to do from here.
- too_large, unsupported_media_type: the file is over the catalog's max_bytes or is not JPEG, \
MP4 or MOV.
- unreachable, or a 5xx status: the service did not answer properly; retry later, then report \
it. Never retry publish_now or schedule_post blindly: check the post with `get_post` first.
"""

LOCAL_UPLOAD = (
    "`upload_media` with the absolute path of each file; it returns the media id. invalid_path, "
    "not_a_file and file_error mean the path cannot be read."
)


def instructions(upload: str) -> str:
    """The server's MCP instructions. Local and remote servers differ only in how files are
    uploaded, which `upload` describes."""
    return _INSTRUCTIONS.format(upload=upload)


Limit = Annotated[int | None, Field(description="Maximum number of results.")]
Offset = Annotated[int | None, Field(description="Results to skip, for paging.")]
PostId = Annotated[str, Field(description="Post id, as returned by create_post or list_posts.")]
ExpectedHash = Annotated[
    str | None,
    Field(
        description="The content_hash you reviewed; always pass it. The API refuses with "
        "content_changed if the post changed since."
    ),
]


def _local_error(code: str, message: str) -> ToolError:
    return ToolError(json.dumps({"status": None, "code": code, "message": message}))


def tool_definitions(api: Api) -> list[tuple[str, str, Callable[..., str], ToolAnnotations]]:
    """(group, level, tool, annotations). The docstring of each tool is what the agent reads.
    The service's remote MCP (jorgex_social/remote_mcp) registers these same definitions."""

    def call(method: str, path: str, *ids: str, **kwargs) -> str:
        """`path` takes each id in a `{}`, escaped."""
        # One JSON text block, as the API answered: the SDK would turn a list into one block per
        # item, and an empty list into no content at all.
        try:
            return json.dumps(api.request(method, path.format(*map(segment, ids)), **kwargs))
        except ApiError as exc:
            # As it came from the API: the agent decides by `code`.
            raise ToolError(exc.args[0]) from None

    limits: dict[str, int] = {}

    def upload_limit() -> int:
        # From the catalog, so it follows the server; it does not change while the server runs.
        if "max_bytes" not in limits:
            limits["max_bytes"] = json.loads(call("GET", "/v1/catalog"))["upload"]["max_bytes"]
        return limits["max_bytes"]

    # access

    def get_token() -> str:
        """What this token may do: its permissions per group (posts, scheduling, stats,
        account, rules, events), each deny, read or write. Call it first; a tool whose group
        is not granted at its level fails with forbidden."""
        return call("GET", "/v1/token")

    # posts

    def get_catalog() -> str:
        """Limits per post type (items, media types, caption) and accepted uploads, as the server
        enforces them. Read before preparing a post."""
        return call("GET", "/v1/catalog")

    def list_posts(
        status: Annotated[
            str | None,
            Field(
                description="Only posts in this status: draft, scheduled, publishing, published, "
                "failed, outcome_unknown or cancelled."
            ),
        ] = None,
        limit: Limit = None,
        offset: Offset = None,
    ) -> str:
        """Posts, newest first, each with its content_hash and catalog errors and warnings."""
        return call("GET", "/v1/posts", query={"status": status, "limit": limit, "offset": offset})

    def get_post(post_id: PostId) -> str:
        """One post with its publish attempts."""
        return call("GET", "/v1/posts/{}", post_id)

    def upload_media(
        path: Annotated[
            str, Field(description="Absolute path of a local .jpg, .jpeg, .mp4 or .mov file.")
        ],
    ) -> str:
        """Upload one local file to the service and return its media id, to use in create_post or
        update_post. Reads only that file and sends it unchanged; the server checks its real type
        from the content. Uploaded media are not published by themselves, and unused ones are
        purged after a while."""
        file = Path(path)
        if not file.is_absolute():
            raise _local_error("invalid_path", "The path must be absolute.")
        content_type = UPLOAD_CONTENT_TYPES.get(file.suffix.lower())
        if content_type is None:
            raise _local_error(
                "unsupported_media_type",
                f"Extension must be one of {', '.join(sorted(UPLOAD_CONTENT_TYPES))}.",
            )
        # A directory, FIFO or device is not an upload, and opening a FIFO would block.
        if not file.is_file():
            raise _local_error("not_a_file", f"Not a regular file: {path}")
        try:
            with file.open("rb") as fh:
                size = os.fstat(fh.fileno()).st_size
                # Checked here because the server refuses an oversized upload as soon as it reads
                # Content-Length, and the client, still sending, only sees a broken connection.
                limit = upload_limit()
                if size > limit:
                    raise _local_error("too_large", f"Uploads are limited to {limit} bytes.")
                return call("POST", "/v1/media", raw=(fh, content_type, size))
        except OSError as exc:
            raise _local_error("file_error", f"{exc.strerror or exc}: {path}") from None

    def create_post(
        type: PostType,
        items: Annotated[list[str], Field(description="Media ids, in order.")],
        caption: str = "",
        cover_media_id: Annotated[
            str | None, Field(description="Media id of the cover image, where the type allows.")
        ] = None,
        thumb_offset_ms: Annotated[
            int | None, Field(description="Video frame used as thumbnail, in milliseconds.")
        ] = None,
        source_ref: Annotated[
            str | None, Field(description="Your own reference for this post.")
        ] = None,
        idempotency_key: Annotated[
            str | None,
            Field(
                description="Recommended: sending the same key again returns the post created "
                "first instead of a second one."
            ),
        ] = None,
    ) -> str:
        """Create a draft. Returns it with its content_hash and the catalog errors and warnings; a
        draft with errors is kept but cannot be scheduled. Does not schedule or publish."""
        body = {
            "type": type,
            "items": items,
            "caption": caption,
            "cover_media_id": cover_media_id,
            "thumb_offset_ms": thumb_offset_ms,
            "source_ref": source_ref,
            "idempotency_key": idempotency_key,
        }
        return call("POST", "/v1/posts", json_body={k: v for k, v in body.items() if v is not None})

    def update_post(
        post_id: PostId,
        type: PostType | None = None,
        items: Annotated[list[str] | None, Field(description="Media ids, in order.")] = None,
        caption: str | None = None,
        cover_media_id: str | None = None,
        thumb_offset_ms: int | None = None,
        source_ref: str | None = None,
        clear: Annotated[
            list[CLEARABLE] | None, Field(description="Optional fields to remove.")
        ] = None,
    ) -> str:
        """Edit a draft or a scheduled post; only the fields given change. A scheduled post goes
        back to draft and has to be scheduled again. Other statuses cannot be edited: to change a
        failed post, create a new one. Returns the new content_hash, to review before
        scheduling."""
        given = {
            "type": type,
            "items": items,
            "caption": caption,
            "cover_media_id": cover_media_id,
            "thumb_offset_ms": thumb_offset_ms,
            "source_ref": source_ref,
        }
        given = {k: v for k, v in given.items() if v is not None}
        both = sorted(set(clear or []) & set(given))
        if both:
            raise _local_error("invalid_value", f"Both given and cleared: {', '.join(both)}.")
        body: dict[str, Any] = {field: None for field in clear or []} | given
        return call("PATCH", "/v1/posts/{}", post_id, json_body=body)

    # scheduling

    def schedule_post(
        post_id: PostId,
        scheduled_for: Annotated[
            str, Field(description="When to publish, ISO 8601 with offset, in the future.")
        ],
        expected_hash: ExpectedHash = None,
    ) -> str:
        """Schedule a draft, scheduled or failed post for a future time (a past one is refused
        with in_the_past; use publish_now). At that time the service publishes it to the real
        account, unless it runs in simulation. The post must pass the catalog. Editing it later
        returns it to draft."""
        body = {"scheduled_for": scheduled_for, "expected_hash": expected_hash}
        return call(
            "POST",
            "/v1/posts/{}/schedule",
            post_id,
            json_body={k: v for k, v in body.items() if v is not None},
        )

    def publish_now(post_id: PostId, expected_hash: ExpectedHash = None) -> str:
        """Schedule a draft, scheduled or failed post for now: the worker publishes it to the
        real account on its next pass, unless the service runs in simulation. Cannot be undone
        once published. The post must pass the catalog. Follow it with get_post; do not call
        this again for the same post unless get_post shows it failed."""
        body = {"expected_hash": expected_hash} if expected_hash is not None else {}
        return call("POST", "/v1/posts/{}/publish-now", post_id, json_body=body)

    def cancel_post(post_id: PostId) -> str:
        """Cancel a draft or scheduled post. It will not be published and cannot be edited or
        scheduled again. Does not delete anything already on Instagram."""
        return call("POST", "/v1/posts/{}/cancel", post_id)

    def resolve_post(
        post_id: PostId,
        outcome: Annotated[
            Literal["published", "not_published"],
            Field(description="What you found on the account."),
        ],
        media_ref: Annotated[
            str | None, Field(description="Instagram media id, only with published.")
        ] = None,
    ) -> str:
        """Settle a post in outcome_unknown after checking the account yourself. not_published
        returns it to draft, to be scheduled again on purpose. Publishes nothing."""
        body = {"outcome": outcome, "media_ref": media_ref}
        return call(
            "POST",
            "/v1/posts/{}/resolve",
            post_id,
            json_body={k: v for k, v in body.items() if v is not None},
        )

    # stats

    def account_stats(
        since: Annotated[str | None, Field(description="ISO 8601 with offset.")] = None,
        until: Annotated[str | None, Field(description="ISO 8601 with offset.")] = None,
        limit: Limit = None,
    ) -> str:
        """Account metrics per closed day, newest first, from the stored snapshots: the 90 most
        recent stored days unless limit says otherwise. No offset: page back with until."""
        return call(
            "GET", "/v1/stats/account", query={"since": since, "until": until, "limit": limit}
        )

    def media_stats(limit: Limit = None, offset: Offset = None) -> str:
        """The latest stored metrics of each published media, newest publication first."""
        return call("GET", "/v1/stats/media", query={"limit": limit, "offset": offset})

    def media_history(
        media_ref: Annotated[str, Field(description="Instagram media id.")], limit: Limit = None
    ) -> str:
        """Every stored snapshot of one media, newest first (365 unless limit says otherwise; no
        offset). Empty if none has been taken yet."""
        return call("GET", "/v1/stats/media/{}", media_ref, query={"limit": limit})

    def list_instagram_media() -> str:
        """The account's newest feed posts and reels (up to 25), newest first: id (the media id
        for resolve_post and for a rule's media_ref), product_type, media_type, permalink and
        timestamp. A snapshot from the worker's last listing, not a live call: check fetched_at
        (null if never listed). It is refreshed by the hourly statistics sweep (and when the
        worker finds a published container, in which case it settles the post itself), so it
        can be up to an hour old, or older while the sweep fails. Stories are never listed.
        No paging: it is the whole snapshot."""
        return call("GET", "/v1/instagram-media")

    # account

    def get_account() -> str:
        """Whether the service runs in simulation (true: nothing reaches Instagram; false:
        publishing, rules and the kill switch act on the real account), connection status,
        publishing limits, kill switch, number of open incidents and the webhook (configured,
        time of the last valid notification). Never includes the credential."""
        return call("GET", "/v1/account")

    def list_incidents(
        open: Annotated[
            bool | None, Field(description="true: only open; false: only closed.")
        ] = None,
        limit: Limit = None,
        offset: Offset = None,
    ) -> str:
        """Channel incidents, newest first. An open incident's cause is still there, except
        account_restricted, which stays open until the kill switch is turned off."""
        return call(
            "GET",
            "/v1/account/incidents",
            query={"open": open, "limit": limit, "offset": offset},
        )

    def audit_log(
        object_type: str | None = None,
        object_id: str | None = None,
        limit: Limit = None,
        offset: Offset = None,
    ) -> str:
        """Audit log of writes and denied calls, newest first."""
        query = {"object_type": object_type, "object_id": object_id, "limit": limit}
        return call("GET", "/v1/account/audit", query={**query, "offset": offset})

    def acknowledge_incident(
        incident_id: Annotated[str, Field(description="Incident id, from list_incidents.")],
    ) -> str:
        """Mark an incident as seen. It stays open until its cause is gone, or for
        account_restricted until the kill switch is turned off."""
        return call("POST", "/v1/account/incidents/{}/acknowledge", incident_id)

    def set_kill_switch(enabled: bool) -> str:
        """On: the service stops writing to the network (no publishing, no replies); scheduled
        posts stay scheduled and replies due meanwhile are skipped for good. Off: due posts
        publish and active rules answer again on the real account, unless the service runs in
        simulation; posts past their grace window fail as missed_schedule and can be
        rescheduled. It also turns on by itself when Instagram reports the account as restricted
        (account_restricted incident); turning it off closes that incident, so a human should
        check the account first."""
        return call("PUT", "/v1/account/kill-switch", json_body={"enabled": enabled})

    # rules

    def list_rules(
        channel: Annotated[str | None, Field(description="comment or message.")] = None,
        active: bool | None = None,
        archived: Annotated[
            bool | None, Field(description="true: only archived rules, which are hidden otherwise.")
        ] = None,
        limit: Limit = None,
        offset: Offset = None,
    ) -> str:
        """Automatic reply rules in the order they are tried: lower priority first, then the
        oldest. The first rule that matches an event wins."""
        query = {"channel": channel, "active": active, "archived": archived, "limit": limit}
        return call("GET", "/v1/rules", query={**query, "offset": offset})

    def get_rule(rule_id: RuleId) -> str:
        """One rule with its content."""
        return call("GET", "/v1/rules/{}", rule_id)

    def test_rules(
        channel: Channel,
        text: Annotated[str, Field(description="A sample comment or message.")],
        media_ref: Annotated[
            str | None, Field(description="Instagram media id the comment would be on.")
        ] = None,
        rule_id: Annotated[
            str | None,
            Field(description="Test this rule, even if inactive; otherwise the active rules."),
        ] = None,
    ) -> str:
        """Which rule a sample text would fire and which variants it could send. Stores nothing
        and sends nothing. Opt-out words are not applied: a direct message that is exactly one of
        them fires no rule, whatever this says."""
        body = {"channel": channel, "text": text, "media_ref": media_ref, "rule_id": rule_id}
        return call(
            "POST", "/v1/rules/test", json_body={k: v for k, v in body.items() if v is not None}
        )

    def create_rule(
        name: str,
        channel: Channel,
        content: RuleContent,
        media_ref: Annotated[
            str | None,
            Field(description="Only comments on this Instagram media id; omit for every post."),
        ] = None,
        priority: Annotated[
            int | None, Field(description="Lower is tried first; default 100.")
        ] = None,
    ) -> str:
        """Create an inactive rule. It answers nothing until activate_rule. The answer's warnings
        list what automation users report as risky; they block nothing."""
        body = {
            "name": name,
            "channel": channel,
            "content": content,
            "media_ref": media_ref,
            "priority": priority,
        }
        return call("POST", "/v1/rules", json_body={k: v for k, v in body.items() if v is not None})

    def update_rule(
        rule_id: RuleId,
        name: str | None = None,
        content: Annotated[
            RuleContent | None, Field(description="Replaces the whole content.")
        ] = None,
        media_ref: str | None = None,
        priority: int | None = None,
        clear: Annotated[
            list[Literal["media_ref"]] | None,
            Field(description="media_ref: apply the rule to every post."),
        ] = None,
    ) -> str:
        """Edit a rule; only the fields given change. An active rule keeps answering, with the
        new content from now on. Archived rules cannot be edited. The answer's warnings list what
        automation users report as risky; they block nothing."""
        given = {"name": name, "content": content, "media_ref": media_ref, "priority": priority}
        given = {k: v for k, v in given.items() if v is not None}
        both = sorted(set(clear or []) & set(given))
        if both:
            raise _local_error("invalid_value", f"Both given and cleared: {', '.join(both)}.")
        body: dict[str, Any] = {field: None for field in clear or []} | given
        return call("PATCH", "/v1/rules/{}", rule_id, json_body=body)

    def activate_rule(rule_id: RuleId) -> str:
        """Start answering with this rule on the real account, unless the service runs in
        simulation. Only comments and messages written from now on fire it. A rule with warnings
        activates all the same; read them first."""
        return call("POST", "/v1/rules/{}/activate", rule_id)

    def deactivate_rule(rule_id: RuleId) -> str:
        """Stop answering with this rule. Replies already queued are still sent."""
        return call("POST", "/v1/rules/{}/deactivate", rule_id)

    def archive_rule(rule_id: RuleId) -> str:
        """Deactivate and archive a rule, for good: it can no longer be edited or activated.
        To stop it for now, use deactivate_rule."""
        return call("POST", "/v1/rules/{}/archive", rule_id)

    def pause_conversation(
        conversation_id: ConversationId,
        until: Annotated[
            str | None, Field(description="ISO 8601 with offset, in the future.")
        ] = None,
        forever: Annotated[bool | None, Field(description="true: until resumed.")] = None,
    ) -> str:
        """Stop automatic messages to one person, until a time or until resumed; give exactly
        one of until or forever. Queued messages to them are skipped. Replies to their comments
        are too, but only when Instagram uses the same id for a comment's author and a message's
        sender, which is not verified. With the webhook configured, a conversation also pauses
        for a while on its own when the account answers it by hand."""
        body = {"until": until, "forever": forever}
        return call(
            "POST",
            "/v1/conversations/{}/pause",
            conversation_id,
            json_body={k: v for k, v in body.items() if v is not None},
        )

    def resume_conversation(conversation_id: ConversationId) -> str:
        """Lift a pause, or an opt-out (a person who sent an opt-out word), which only a human
        should decide. What was skipped while paused is not sent afterwards."""
        return call("POST", "/v1/conversations/{}/resume", conversation_id)

    # events

    def list_events(
        type: Annotated[str | None, Field(description="comment or message.")] = None,
        rule_id: str | None = None,
        matched: Annotated[
            bool | None, Field(description="true: fired a rule; false: fired none.")
        ] = None,
        limit: Limit = None,
        offset: Offset = None,
    ) -> str:
        """Comments and messages seen, newest first: identifiers and the rule they fired. Their
        text and authors are never stored, so they are not here."""
        query = {"type": type, "rule_id": rule_id, "matched": matched, "limit": limit}
        return call("GET", "/v1/events", query={**query, "offset": offset})

    def list_deliveries(
        status: Annotated[
            str | None, Field(description="pending, sent, failed or skipped.")
        ] = None,
        reason: Annotated[
            str | None,
            Field(
                description="Why skipped or failed: kill_switch, paused, window_closed, "
                "cooldown, already_answered, hourly_cap, refused or unknown."
            ),
        ] = None,
        action: Annotated[
            str | None, Field(description="public_reply, private_reply or message.")
        ] = None,
        rule_id: str | None = None,
        event_id: str | None = None,
        limit: Limit = None,
        offset: Offset = None,
    ) -> str:
        """Automatic replies, newest first: what was sent, or why not. pending may mean waiting
        for the per-minute pace. already_answered: this rule already answered this person on this
        post. window_closed includes public replies 24 hours after the comment. hourly_cap is the
        overall cap or the lower one on public replies."""
        query = {
            "status": status,
            "reason": reason,
            "action": action,
            "rule_id": rule_id,
            "event_id": event_id,
            "limit": limit,
        }
        return call("GET", "/v1/deliveries", query={**query, "offset": offset})

    def list_conversations(
        paused: bool | None = None, limit: Limit = None, offset: Offset = None
    ) -> str:
        """People who wrote by direct message, most recent first, with their 24-hour window and
        pause. Someone who sent an opt-out word shows as paused_forever."""
        return call(
            "GET",
            "/v1/conversations",
            query={"paused": paused, "limit": limit, "offset": offset},
        )

    return [
        ("access", ANY, get_token, READ),
        ("posts", "read", get_catalog, READ),
        ("posts", "read", list_posts, READ),
        ("posts", "read", get_post, READ),
        ("posts", "write", upload_media, WRITE),
        ("posts", "write", create_post, WRITE),
        ("posts", "write", update_post, WRITE),
        ("scheduling", "write", schedule_post, ON_ACCOUNT),
        ("scheduling", "write", publish_now, ON_ACCOUNT),
        ("scheduling", "write", cancel_post, WRITE),
        ("scheduling", "write", resolve_post, WRITE),
        ("stats", "read", account_stats, READ),
        ("stats", "read", media_stats, READ),
        ("stats", "read", media_history, READ),
        ("stats", "read", list_instagram_media, READ),
        ("account", "read", get_account, READ),
        ("account", "read", list_incidents, READ),
        ("account", "read", audit_log, READ),
        ("account", "write", acknowledge_incident, WRITE),
        ("account", "write", set_kill_switch, WRITE),
        ("rules", "read", list_rules, READ),
        ("rules", "read", get_rule, READ),
        ("rules", "read", test_rules, READ),
        ("rules", "write", create_rule, WRITE),
        ("rules", "write", update_rule, WRITE),
        ("rules", "write", activate_rule, ON_ACCOUNT),
        ("rules", "write", deactivate_rule, WRITE),
        ("rules", "write", archive_rule, WRITE),
        ("rules", "write", pause_conversation, WRITE),
        ("rules", "write", resume_conversation, WRITE),
        ("events", "read", list_events, READ),
        ("events", "read", list_deliveries, READ),
        ("events", "read", list_conversations, READ),
    ]


def build_server(api: Api) -> MCPServer:
    """Asks the API what the token may do and registers only those tools."""
    answer = api.request("GET", "/v1/token")
    permissions = answer.get("permissions") if isinstance(answer, dict) else None
    if not isinstance(permissions, dict):
        raise ApiError(
            {
                "status": None,
                "code": "invalid_response",
                "message": "GET /v1/token did not return the token's permissions.",
            }
        )
    server = MCPServer("jorgex-social", instructions=instructions(LOCAL_UPLOAD))
    for group, level, tool, annotations in tool_definitions(api):
        if level == ANY or _RANK.get(permissions.get(group, "deny"), 0) >= _RANK[level]:
            server.add_tool(tool, annotations=annotations, structured_output=False)
    return server


def main() -> None:
    url = os.environ.get("JORGEX_SOCIAL_URL")
    token = os.environ.get("JORGEX_SOCIAL_TOKEN")
    if not url or not token:
        sys.exit("jorgex-social-mcp: set JORGEX_SOCIAL_URL and JORGEX_SOCIAL_TOKEN.")
    try:
        check_url(url)
        check_token(token)
    except ValueError as exc:
        sys.exit(f"jorgex-social-mcp: {exc}")
    try:
        server = build_server(Api(url, token))
    except ApiError as exc:
        sys.exit(f"jorgex-social-mcp: cannot read the token's permissions: {exc}")
    server.run("stdio")
