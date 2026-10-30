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

Limit = Annotated[int | None, Field(description="Maximum number of results.")]
Offset = Annotated[int | None, Field(description="Results to skip, for paging.")]
PostId = Annotated[str, Field(description="Post id, as returned by create_post or list_posts.")]
ExpectedHash = Annotated[
    str | None,
    Field(
        description="The content_hash you reviewed. The API refuses with content_changed if the "
        "post changed since."
    ),
]


def _local_error(code: str, message: str) -> ToolError:
    return ToolError(json.dumps({"status": None, "code": code, "message": message}))


def _tools(api: Api) -> list[tuple[str, str, Callable[..., str], ToolAnnotations]]:
    """(group, level, tool, annotations). The docstring of each tool is what the agent reads."""

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
            Field(description="Sending the same key again returns the post created first."),
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
        back to draft and has to be scheduled again. Published, failed or cancelled posts cannot
        be edited."""
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
        """Schedule a draft, scheduled or failed post. At that time the service publishes it to
        the real account, unless it runs in simulation. The post must pass the catalog."""
        body = {"scheduled_for": scheduled_for, "expected_hash": expected_hash}
        return call(
            "POST",
            "/v1/posts/{}/schedule",
            post_id,
            json_body={k: v for k, v in body.items() if v is not None},
        )

    def publish_now(post_id: PostId, expected_hash: ExpectedHash = None) -> str:
        """Schedule the post for now: the worker publishes it to the real account on its next
        pass, unless the service runs in simulation. Check the result with get_post."""
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
        """Account metrics per closed day, newest first, from the stored snapshots."""
        return call(
            "GET", "/v1/stats/account", query={"since": since, "until": until, "limit": limit}
        )

    def media_stats(limit: Limit = None, offset: Offset = None) -> str:
        """The latest stored metrics of each published media, newest publication first."""
        return call("GET", "/v1/stats/media", query={"limit": limit, "offset": offset})

    def media_history(
        media_ref: Annotated[str, Field(description="Instagram media id.")], limit: Limit = None
    ) -> str:
        """Every stored snapshot of one media, newest first. Empty if none has been taken yet."""
        return call("GET", "/v1/stats/media/{}", media_ref, query={"limit": limit})

    # account

    def get_account() -> str:
        """Connection status of the account, publishing limits, kill switch and number of open
        incidents. Never includes the credential."""
        return call("GET", "/v1/account")

    def list_incidents(
        open: Annotated[
            bool | None, Field(description="true: only open; false: only closed.")
        ] = None,
        limit: Limit = None,
        offset: Offset = None,
    ) -> str:
        """Channel incidents, newest first. An open incident's cause is still there."""
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

    def acknowledge_incident(incident_id: str) -> str:
        """Mark an incident as seen. It stays open until its cause is gone."""
        return call("POST", "/v1/account/incidents/{}/acknowledge", incident_id)

    def set_kill_switch(enabled: bool) -> str:
        """On: the service stops writing to the network (no publishing, no replies); scheduled
        posts stay scheduled and replies due meanwhile are skipped. Off: it resumes, and posts
        past their grace window fail as missed_schedule and can be rescheduled."""
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
        and sends nothing."""
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
        priority: Annotated[int | None, Field(description="Lower is tried first.")] = None,
    ) -> str:
        """Create an inactive rule. It answers nothing until activate_rule."""
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
        new content from now on. Archived rules cannot be edited."""
        given = {"name": name, "content": content, "media_ref": media_ref, "priority": priority}
        given = {k: v for k, v in given.items() if v is not None}
        both = sorted(set(clear or []) & set(given))
        if both:
            raise _local_error("invalid_value", f"Both given and cleared: {', '.join(both)}.")
        body: dict[str, Any] = {field: None for field in clear or []} | given
        return call("PATCH", "/v1/rules/{}", rule_id, json_body=body)

    def activate_rule(rule_id: RuleId) -> str:
        """Start answering with this rule on the real account, unless the service runs in
        simulation. Only comments and messages written from now on fire it."""
        return call("POST", "/v1/rules/{}/activate", rule_id)

    def deactivate_rule(rule_id: RuleId) -> str:
        """Stop answering with this rule. Replies already queued are still sent."""
        return call("POST", "/v1/rules/{}/deactivate", rule_id)

    def archive_rule(rule_id: RuleId) -> str:
        """Deactivate and archive a rule. It can no longer be edited or activated."""
        return call("POST", "/v1/rules/{}/archive", rule_id)

    def pause_conversation(
        conversation_id: ConversationId,
        until: Annotated[
            str | None, Field(description="ISO 8601 with offset, in the future.")
        ] = None,
        forever: Annotated[bool | None, Field(description="true: until resumed.")] = None,
    ) -> str:
        """Stop automatic messages to one person, until a time or until resumed; give exactly
        one of until or forever. Queued messages to them are skipped."""
        body = {"until": until, "forever": forever}
        return call(
            "POST",
            "/v1/conversations/{}/pause",
            conversation_id,
            json_body={k: v for k, v in body.items() if v is not None},
        )

    def resume_conversation(conversation_id: ConversationId) -> str:
        """Lift a pause. What was skipped while paused is not sent afterwards."""
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
                "cooldown, hourly_cap, refused or unknown."
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
        """Automatic replies, newest first: what was sent, or why not."""
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
        pause."""
        return call(
            "GET",
            "/v1/conversations",
            query={"paused": paused, "limit": limit, "offset": offset},
        )

    return [
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
    server = MCPServer(
        "jorgex-social",
        instructions="Prepare, schedule and review posts, and manage automatic reply rules, of "
        "one Instagram account through the jorgex-social API. Errors carry a stable `code`; "
        "decide by it.",
    )
    for group, level, tool, annotations in _tools(api):
        if _RANK.get(permissions.get(group, "deny"), 0) >= _RANK[level]:
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
