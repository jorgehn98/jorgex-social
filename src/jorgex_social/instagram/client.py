import json
from collections.abc import Callable
from datetime import datetime, timedelta
from http.client import HTTPException
from itertools import islice
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from jorgex_social.instagram.errors import (
    AccountRestricted,
    AuthError,
    RateLimited,
    Refused,
    Unavailable,
)
from jorgex_social.instagram.types import (
    Account,
    Comment,
    ContainerRequest,
    ContainerStatus,
    Conversation,
    Media,
    Message,
    ProductType,
    PublishingLimit,
    Renewal,
)

# The API version is pinned; moving to a newer one is an explicit decision (decision 07).
GRAPH_URL = "https://graph.instagram.com/v26.0"
# Unversioned, and the only call where Meta wants the token in the URL.
REFRESH_URL = "https://graph.instagram.com/refresh_access_token"
TIMEOUT_SECONDS = 60
PAGE_SIZE = 50

# Meta rejects the whole request if one metric does not apply to the media, so each product
# type asks only for what it supports. Reel times are in milliseconds.
MEDIA_METRICS = {
    ProductType.FEED: (
        "reach,views,likes,comments,saved,shares,total_interactions,follows,profile_visits,"
        "profile_activity"
    ),
    ProductType.REELS: (
        "reach,views,likes,comments,saved,shares,total_interactions,"
        "ig_reels_avg_watch_time,ig_reels_video_view_total_time,reels_skip_rate"
    ),
    ProductType.STORY: (
        "reach,views,shares,total_interactions,replies,follows,profile_visits,profile_activity,"
        "navigation"
    ),
}
ACCOUNT_METRICS = (
    "reach,views,accounts_engaged,total_interactions,likes,comments,shares,saves,"
    "replies,profile_links_taps,website_clicks,profile_views"
)
MEDIA_FIELDS = "id,caption,media_product_type,media_type,permalink,timestamp"
# Only the author's id is asked for: the username is third-party data the service has no use for.
COMMENT_FIELDS = "id,text,timestamp,from{id}"
MESSAGE_FIELDS = "messages{id,created_time,from{id},message}"
RATE_LIMIT_CODES = {4, 17, 32, 613, 80002}
# Any subcode: temporarily blocked for policy violations.
ACCOUNT_RESTRICTED_CODES = {368}
# (code, subcode) in Meta's error reference. Two share a code with a rate limit, so these are
# checked first: every other 4 or 613 is a plain rate limit.
ACCOUNT_RESTRICTED = {
    (10, 1893063),  # temporarily restricted from sending
    (100, 2534029),  # business blocked from the Instagram Messaging API
    (613, 2018338),  # warning about behaviour users may find abusive
    (4, 2207051),  # activity restricted as suspected spam
    (9, 2018352),  # the user is performing too many actions
    (25, 2207050),  # account inactive, checkpointed or restricted
}
AUTH_ERROR_CODE = 190


class _NoRedirect(HTTPRedirectHandler):
    # urllib would forward the Authorization header to wherever a redirect points.
    def redirect_request(self, *args, **kwargs):
        return None


_opener = build_opener(_NoRedirect)

# () -> (access token, account id). Raises CredentialUnavailable when it cannot be read.
CredentialSource = Callable[[], tuple[str, str]]

# (method, url, headers, body) -> (status, body). Swapped in tests; the default is urllib.
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


def urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, bytes]:
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with _opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()


def _error(status: int, raw: bytes) -> Exception:
    try:
        error = json.loads(raw).get("error")
    except ValueError, AttributeError:
        error = None
    if not isinstance(error, dict):
        error = {}
    code, subcode = error.get("code"), error.get("error_subcode")
    message = f"Instagram API {status}: {str(error.get('message') or raw[:200])[:300]}"
    if status >= 500:
        return Unavailable(message, code=code, subcode=subcode)
    if code == AUTH_ERROR_CODE:
        return AuthError(message, code=code, subcode=subcode)
    if code in ACCOUNT_RESTRICTED_CODES or (code, subcode) in ACCOUNT_RESTRICTED:
        return AccountRestricted(message, code=code, subcode=subcode)
    if status == 429 or code in RATE_LIMIT_CODES:
        return RateLimited(message, code=code, subcode=subcode, transient=True)
    return Refused(message, code=code, subcode=subcode, transient=bool(error.get("is_transient")))


MAX_INSIGHTS_WINDOW = timedelta(days=30)


def check_insights_window(since: datetime, until: datetime) -> None:
    """Account insights accept at most 30 days per request."""
    if not timedelta(0) < until - since <= MAX_INSIGHTS_WINDOW:
        raise ValueError("account insights need 0 < until - since <= 30 days")


def _metrics(data: list[dict[str, Any]]) -> dict[str, Any]:
    # A metric Meta does not return is absent, never zero.
    values = {}
    for metric in data:
        if "total_value" in metric:
            values[metric["name"]] = metric["total_value"].get("value")
        elif metric.get("values"):
            values[metric["name"]] = metric["values"][0].get("value")
    return values


def _field(data: dict[str, Any], name: str) -> Any:
    # A 2xx without the expected field is not an answer: for a write, the outcome is unknown.
    if name not in data:
        raise Unavailable(f"Instagram API response without {name!r}")
    return data[name]


def _media(item: dict[str, Any]) -> Media | None:
    """None for product types this service does not handle (for example ads or legacy IGTV)."""
    try:
        product_type = ProductType(item.get("media_product_type") or ProductType.FEED)
    except ValueError:
        return None
    return Media(
        id=item["id"],
        product_type=product_type,
        media_type=item.get("media_type", ""),
        permalink=item.get("permalink"),
        timestamp=datetime.fromisoformat(item["timestamp"]),
        caption=item.get("caption"),
    )


def _author(item: dict[str, Any]) -> str | None:
    author = item.get("from")
    return str(author["id"]) if isinstance(author, dict) and author.get("id") else None


def _parse_one(build: Callable[[dict[str, Any]], Any], item: Any) -> Any:
    """None for a malformed item, which is dropped rather than failing the whole listing (and
    never quoted, since it may hold third-party text)."""
    try:
        return build(item)
    except KeyError, TypeError, ValueError, AttributeError:
        return None


def _parsed(build: Callable[[dict[str, Any]], Any], items) -> list:
    return [x for x in (_parse_one(build, item) for item in items) if x is not None]


def _conversation(item: dict[str, Any]) -> Conversation:
    return Conversation(id=str(item["id"]), updated_at=datetime.fromisoformat(item["updated_time"]))


class InstagramClient:
    """Instagram API with Instagram Login (graph.instagram.com), standard library only."""

    network = "instagram"

    def __init__(
        self,
        access_token: str,
        user_id: str,
        *,
        transport: Transport = urllib_transport,
        base_url: str = GRAPH_URL,
        refresh_url: str = REFRESH_URL,
    ) -> None:
        self._credentials: CredentialSource = lambda: (access_token, user_id)
        self._label = f"user_id={user_id!r}"
        self._transport = transport
        self._base_url = base_url
        self._refresh_url = refresh_url

    @classmethod
    def from_source(cls, source: CredentialSource, **kwargs) -> InstagramClient:
        """A client that asks `source` for the token and account id on every call, together, so
        a renewal or a switch to another account made elsewhere applies at once and the two never
        come from different connections (account.credential.StoredCredential)."""
        client = cls("", "", **kwargs)
        client._credentials = source
        client._label = repr(source)
        return client

    def __repr__(self) -> str:
        return f"InstagramClient({self._label})"

    def _call(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        credentials: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        """`path` may hold the literal `{user_id}`, filled from the same credential read."""
        token, user_id = credentials or self._credentials()
        params = {k: v for k, v in (params or {}).items() if v is not None}
        url = self._base_url + path.replace("{user_id}", user_id)
        body = None
        if method == "GET" and params:
            url += "?" + urlencode(params)
        elif params:
            body = urlencode(params).encode()
        # The token goes in a header, never in the URL, so it cannot leak into errors or logs.
        headers = {"Authorization": f"Bearer {token}"}
        try:
            status, raw = self._transport(method, url, headers, body)
        except (URLError, TimeoutError, OSError, HTTPException) as exc:
            raise Unavailable(f"Instagram API connection error: {exc}") from None
        if status >= 400:
            raise _error(status, raw)
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            raise Unavailable(f"Instagram API returned invalid JSON ({status})") from None

    def account(self) -> Account:
        credentials = self._credentials()
        data = self._call(
            "GET",
            "/me",
            {"fields": "user_id,username,account_type,followers_count,media_count"},
            credentials,
        )
        return Account(
            id=str(data.get("user_id") or credentials[1]),
            username=data.get("username", ""),
            account_type=data.get("account_type"),
            followers_count=data.get("followers_count"),
            media_count=data.get("media_count"),
        )

    def publishing_limit(self) -> PublishingLimit:
        data = self._call(
            "GET",
            "/{user_id}/content_publishing_limit",
            {"fields": "config,quota_usage"},
        )
        row = (data.get("data") or [{}])[0]
        config = row.get("config") or {}
        return PublishingLimit(
            quota_usage=row.get("quota_usage", 0),
            quota_total=config.get("quota_total"),
            quota_duration_seconds=config.get("quota_duration"),
        )

    def create_container(self, request: ContainerRequest) -> str:
        return _field(self._call("POST", "/{user_id}/media", request.to_params()), "id")

    def container_status(self, container_id: str) -> ContainerStatus:
        data = self._call("GET", f"/{container_id}", {"fields": "status_code"})
        return ContainerStatus(_field(data, "status_code"))

    def publish(self, container_id: str) -> str:
        data = self._call("POST", "/{user_id}/media_publish", {"creation_id": container_id})
        return _field(data, "id")

    def media(self, media_id: str) -> Media:
        media = _media(self._call("GET", f"/{media_id}", {"fields": MEDIA_FIELDS}))
        if media is None:
            raise Refused(f"media {media_id} has an unsupported product type")
        return media

    def list_media(self, max_items: int | None = None) -> list[Media]:
        """Feed posts and reels, newest first. Stories are not listed by this endpoint."""
        items = map(_media, self._pages("/{user_id}/media", {"fields": MEDIA_FIELDS}))
        return list(islice((m for m in items if m is not None), max_items))

    def media_insights(self, media_id: str, product_type: ProductType) -> dict[str, Any]:
        data = self._call("GET", f"/{media_id}/insights", {"metric": MEDIA_METRICS[product_type]})
        return _metrics(data.get("data", []))

    def account_insights(self, since: datetime, until: datetime) -> dict[str, Any]:
        check_insights_window(since, until)
        data = self._call(
            "GET",
            "/{user_id}/insights",
            {
                "metric": ACCOUNT_METRICS,
                "period": "day",
                "metric_type": "total_value",
                "since": int(since.timestamp()),
                "until": int(until.timestamp()),
            },
        )
        return _metrics(data.get("data", []))

    def _pages(self, path: str, params: dict[str, Any]):
        """Every item of a paged listing, page by page, following the cursor (never paging.next,
        whose URL would carry the token)."""
        after = None
        while True:
            page = self._call("GET", path, {**params, "limit": PAGE_SIZE, "after": after})
            yield from page.get("data", [])
            paging = page.get("paging") or {}
            after = (paging.get("cursors") or {}).get("after")
            if "next" not in paging or not after:
                return

    def list_comments(self, media_id: str) -> list[Comment]:
        return _parsed(
            lambda item: Comment(
                id=str(item["id"]),
                author_id=_author(item),
                timestamp=datetime.fromisoformat(item["timestamp"]),
                text=item.get("text") or "",
            ),
            self._pages(f"/{media_id}/comments", {"fields": COMMENT_FIELDS}),
        )

    def list_conversations(self, since: datetime | None = None) -> list[Conversation]:
        # Assumes Instagram lists the most recently updated first, as its inbox does.
        found = []
        pages = self._pages(
            "/{user_id}/conversations", {"platform": "instagram", "fields": "id,updated_time"}
        )
        for item in pages:
            conversation = _parse_one(_conversation, item)
            if conversation is None:
                continue
            if since is not None and conversation.updated_at < since:
                break
            found.append(conversation)
        return found

    def list_messages(self, conversation_id: str) -> list[Message]:
        data = self._call("GET", f"/{conversation_id}", {"fields": MESSAGE_FIELDS})
        return _parsed(
            lambda item: Message(
                id=str(item["id"]),
                sender_id=_author(item),
                created_at=datetime.fromisoformat(item["created_time"]),
                text=item.get("message") or "",  # absent for attachments
            ),
            (data.get("messages") or {}).get("data", []),
        )

    def reply_public(self, comment_id: str, text: str) -> str:
        return str(_field(self._call("POST", f"/{comment_id}/replies", {"message": text}), "id"))

    def _send(self, recipient: dict[str, str], text: str) -> str:
        params = {"recipient": json.dumps(recipient), "message": json.dumps({"text": text})}
        return str(_field(self._call("POST", "/{user_id}/messages", params), "message_id"))

    def reply_private(self, comment_id: str, text: str) -> str:
        return self._send({"comment_id": comment_id}, text)

    def send_message(self, recipient_id: str, text: str) -> str:
        return self._send({"id": recipient_id}, text)

    def check_credential(self) -> None:
        self._credentials()

    def refresh_token(self) -> Renewal:
        token, _ = self._credentials()
        query = urlencode({"grant_type": "ig_refresh_token", "access_token": token})
        try:
            status, raw = self._transport("GET", f"{self._refresh_url}?{query}", {}, None)
        except URLError, TimeoutError, OSError, HTTPException:
            # Not quoting the exception: it may carry the URL, and here the URL carries the token.
            raise Unavailable("Instagram token refresh: connection error") from None
        if status >= 400:
            error = _error(status, raw)
            error.args = (str(error).replace(token, "[redacted]"),)
            raise error
        try:
            data = json.loads(raw)
            renewal = Renewal(
                access_token=data["access_token"],
                expires_in=timedelta(seconds=int(data["expires_in"])),
            )
        except ValueError, KeyError, TypeError:
            # Never quote the body: on success it holds the new token.
            raise Unavailable(f"Instagram token refresh: unusable answer ({status})") from None
        if not isinstance(renewal.access_token, str) or not renewal.access_token:
            raise Unavailable(f"Instagram token refresh: unusable answer ({status})")
        return renewal
