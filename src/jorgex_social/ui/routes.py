"""Read-only web interface for the operator (docs: interfaz). Pages read through the read-only
session and reuse the API's own query functions; only signing in and out write."""

import ipaddress
import logging
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import OperationalError
from starlette.concurrency import run_in_threadpool

from jorgex_social.access import operator
from jorgex_social.access.deps import ReadSessionDep
from jorgex_social.access.limits import client_key, is_listed
from jorgex_social.access.models import OperatorSession
from jorgex_social.access.proxy import via_proxy
from jorgex_social.account.api import get_account, list_incidents
from jorgex_social.publishing import media as storage
from jorgex_social.publishing.api import get_post, list_posts, next_scheduled
from jorgex_social.publishing.models import Media, MediaType, PostStatus
from jorgex_social.replies.api import RuleOut, list_deliveries, list_events, list_rules
from jorgex_social.stats.api import account_history, latest_per_media, media_history
from jorgex_social.ui.chart import line_chart

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
COOKIE = "jxs_session"
INVALID = "Invalid credentials."
TOO_MANY = "Too many attempts. Try again later."
BUSY = "The server is busy. Try again in a moment."
HOME = "/ui/"
MAX_FORM_BYTES = 4096
# The pages need nothing from anywhere else: no scripts at all, styles, fonts and images from
# this origin only.
HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; font-src 'self'; img-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}
POST_SECTIONS = [
    (PostStatus.SCHEDULED, "Scheduled"),
    (PostStatus.PUBLISHING, "Publishing"),
    (PostStatus.OUTCOME_UNKNOWN, "Outcome unknown"),
    (PostStatus.FAILED, "Failed"),
    (PostStatus.PUBLISHED, "Published"),
]
# Shown last and folded (a <details>, no JavaScript): they never go out on their own.
FOLDED_SECTIONS = [(PostStatus.DRAFT, "Drafts"), (PostStatus.CANCELLED, "Cancelled")]

templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["when"] = lambda d: d.strftime("%Y-%m-%d %H:%M UTC") if d else "—"
templates.env.filters["day"] = lambda d: d.strftime("%Y-%m-%d") if d else "—"


class LoginRequired(Exception):
    pass


class SignInTooOld(LoginRequired):
    """The session is valid but too old for this page: its cookie still serves the others."""


def _is_loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback  # 127.0.0.0/8 and ::1
    except ValueError:
        return False


def cookie_is_secure(request: Request) -> bool:
    """Secure, except over plain http to a loopback host: the SSH tunnel (and local development)
    reach the interface at http://localhost, where a browser that does not treat localhost as a
    secure origin would drop a Secure cookie. Any other host, as a proxy would forward, gets
    Secure, in simulation and in real mode alike."""
    return not (request.url.scheme == "http" and _is_loopback(request.url.hostname))


def _origin(url: str) -> tuple[str, str, int] | None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    return parts.scheme, parts.hostname.lower(), port or (443 if parts.scheme == "https" else 80)


def same_origin(request: Request) -> None:
    """CSRF check for every form post, on top of the SameSite=Strict cookie. The Origin must be
    the configured public URL (not whatever Host says), or a loopback one when the request did
    not come through the proxy: that is the SSH tunnel. Browsers send Origin on every POST; a
    request with neither Origin nor Sec-Fetch-Site is not from a browser form, and is refused.
    Our own Referrer-Policy (no-referrer) makes browsers send `Origin: null` on these posts, so
    a null Origin falls back to Sec-Fetch-Site, which a page cannot set."""
    if request.method != "POST":
        return
    origin = request.headers.get("origin")
    if origin is None or origin == "null":
        if request.headers.get("sec-fetch-site") == "same-origin":
            return
        raise HTTPException(403)
    found = _origin(origin)
    public_url = request.app.state.settings.public_url
    if found is not None and public_url and found == _origin(public_url):
        return
    if found is not None and _is_loopback(found[1]) and not via_proxy(request.scope):
        return
    raise HTTPException(403)


def safe_next(value: str | None) -> str:
    """Where to go after signing in: only a path of this interface, never another site."""
    if not value or "\\" in value or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return HOME
    parts = urlsplit(value)
    segments = unquote(parts.path).split("/")
    if parts.scheme or parts.netloc or segments[:2] != ["", "ui"] or {".", ".."} & set(segments):
        return HOME
    return value


def require_operator(request: Request) -> OperatorSession:
    session_id = request.cookies.get(COOKIE)
    now = request.app.state.clock.now()
    found = None
    if session_id:
        with request.app.state.read_sessionmaker() as session, session.begin():
            found = operator.find_session(session, session_id, now)
    if found is None:
        raise LoginRequired
    if operator.needs_activity_recorded(found, now):
        try:
            with request.app.state.sessionmaker() as session, session.begin():
                operator.record_activity(session, found, now)
        except OperationalError:
            # Bookkeeping only (a lock timeout): the page is served; the next one retries.
            log.warning("could not record the session's activity", exc_info=True)
    return found


def render(request: Request, name: str, status_code: int = 200, **context) -> Response:
    return templates.TemplateResponse(
        request, name, context, status_code=status_code, headers=HEADERS
    )


def _login_redirect(request: Request, reason: LoginRequired) -> Response:
    location = "/ui/login"
    back = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if request.method == "GET" and safe_next(back) == back != HOME:
        location += "?" + urlencode({"next": back})
    response = RedirectResponse(location, status_code=303, headers=HEADERS)
    # Expired or unknown: no use keeping it.
    if COOKIE in request.cookies and not isinstance(reason, SignInTooOld):
        response.delete_cookie(COOKIE, path="/ui")
    return response


public = APIRouter(prefix="/ui", include_in_schema=False, dependencies=[Depends(same_origin)])
pages = APIRouter(
    prefix="/ui",
    include_in_schema=False,
    dependencies=[Depends(same_origin), Depends(require_operator)],
)


# Signing in and out


@public.get("/login")
def login_form(request: Request):
    return render(request, "login.html", next=safe_next(request.query_params.get("next")))


async def read_form(request: Request) -> dict[str, str]:
    # Parsed by hand, with a cap on the body (nothing legitimate here is large) and on the number
    # of fields. python-multipart is installed now (the mcp SDK needs it), but this needs nothing
    # from it.
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            raise HTTPException(413)
    try:
        fields = parse_qs(body.decode(errors="replace"), max_num_fields=10)
    except ValueError:  # more fields than any form here has
        raise HTTPException(400) from None
    return {name: values[0] for name, values in fields.items()}


def _counts_the_proxy(request: Request) -> bool:
    """Whether the address counted is the proxy's rather than a visitor's: either it is a
    trusted proxy that sent no X-Forwarded-For, or a header was sent and not believed (uvicorn
    then leaves the client as the peer, which appears nowhere in the header)."""
    host = request.client.host if request.client else None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded is None:
        return is_listed(host, request.app.state.settings.forwarded_allow_ips)
    return host not in {entry.strip() for entry in forwarded.split(",")}


def _end_presented_session(request: Request) -> None:
    session_id = request.cookies.get(COOKIE)
    if session_id:
        # Looked up first in a read transaction, so a made-up cookie never takes the write lock.
        with request.app.state.read_sessionmaker() as session, session.begin():
            stored = operator.is_stored(session, session_id)
        if stored:
            with request.app.state.sessionmaker() as session, session.begin():
                operator.log_out(session, session_id)


@public.post("/login")
async def login(request: Request):
    state = request.app.state
    client = client_key(request.client.host if request.client else None)
    wait = state.login_limiter.hit(client, state.clock.now())
    if wait is not None:
        # Turned away before the body, argon2 or the database: only this line records it.
        log.warning("sign-in refused: too many attempts", extra={"client": client})
        if _counts_the_proxy(request):
            log.warning(
                "the sign-in limit is counting the reverse proxy's address, so every visitor "
                "shares one count: check JORGEX_SOCIAL_FORWARDED_ALLOW_IPS and that the proxy "
                "sets X-Forwarded-For (ignore this if it was the SSH tunnel)",
                extra={"client": client},
            )
        response = render(request, "login.html", 429, error=TOO_MANY, next=HOME)
        response.headers["Retry-After"] = str(int(wait.total_seconds()) + 1)
        return response
    form = await read_form(request)
    next_page = safe_next(form.get("next"))
    try:
        session_id = await run_in_threadpool(
            operator.log_in,
            state.sessionmaker,
            state.read_sessionmaker,
            state.settings.secret_key_file,
            state.network,
            form.get("password", ""),
            form.get("code", ""),
            client,
            state.clock.now(),
        )
    except operator.Busy:
        log.warning("sign-in refused: every password check slot is busy")
        return render(request, "login.html", 503, error=BUSY, next=next_page)
    if session_id is None:
        return render(request, "login.html", 401, error=INVALID, next=next_page)
    try:
        # The cookie it replaces would otherwise stay valid for days, unseen.
        await run_in_threadpool(_end_presented_session, request)
    except OperationalError:
        # A lock timeout must not cost the sign-in, whose code is already spent: it lapses alone.
        log.warning("could not end the session this sign-in replaces", exc_info=True)
    response = RedirectResponse(next_page, status_code=303, headers=HEADERS)
    response.set_cookie(
        COOKIE,
        session_id,
        max_age=int(operator.SESSION_LIFETIME.total_seconds()),
        path="/ui",
        secure=cookie_is_secure(request),
        httponly=True,
        samesite="strict",
    )
    return response


@public.post("/logout")
def logout(request: Request):
    _end_presented_session(request)
    response = RedirectResponse("/ui/login", status_code=303, headers=HEADERS)
    response.delete_cookie(COOKIE, path="/ui")
    return response


# Pages


def _network(request: Request) -> str:
    return request.app.state.network


@pages.get("/")
def home(request: Request, session: ReadSessionDep):
    network = _network(request)
    return render(
        request,
        "home.html",
        account=get_account(request, session),
        incidents=list_incidents(request, session, open=True, limit=20),
        upcoming=next_scheduled(session, network, limit=5),
    )


def _thumbnail(session, media_dir: Path, post: dict) -> str | None:
    """The first image's media id, if it is a JPEG whose file is still on disk."""
    if not post["items"]:
        return None
    media = session.get(Media, post["items"][0])
    if media is None or media.type != MediaType.JPEG or media.deleted_at:
        return None
    return media.id if storage.path_of(media_dir, media).is_file() else None


@pages.get("/posts")
def posts(request: Request, session: ReadSessionDep):
    network, media_dir = _network(request), request.app.state.settings.media_dir
    sections = []
    for status, title in POST_SECTIONS + FOLDED_SECTIONS:
        # Scheduled ones soonest first; the rest newest first.
        if status is PostStatus.SCHEDULED:
            found = next_scheduled(session, network, limit=100)
        else:
            found = list_posts(session, network, status=status, limit=100)
        for post in found:
            post["thumbnail"] = _thumbnail(session, media_dir, post)
        sections.append((title, found, (status, title) in FOLDED_SECTIONS))
    return render(request, "posts.html", sections=sections)


@pages.get("/posts/{post_id}")
def post_detail(post_id: str, request: Request, session: ReadSessionDep):
    try:
        post = get_post(post_id, session, _network(request))
    except HTTPException:
        return render(request, "not_found.html", 404, what="post")
    media_dir = request.app.state.settings.media_dir
    items = [session.get(Media, media_id) for media_id in post["items"]]
    on_disk = {m.id: storage.path_of(media_dir, m).is_file() for m in items if m}
    return render(
        request,
        "post.html",
        post=post,
        items=items,
        on_disk=on_disk,
        thumbnail=_thumbnail(session, media_dir, post),
    )


@pages.get("/media/{media_id}")
def media_file(media_id: str, request: Request, session: ReadSessionDep):
    """The image of a post, for its thumbnail; only JPEGs still on disk."""
    media = session.get(Media, media_id)
    if media is None or media.type != MediaType.JPEG or media.deleted_at:
        raise HTTPException(404)
    path = storage.path_of(request.app.state.settings.media_dir, media)
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg", headers=HEADERS)


@pages.get("/stats")
def stats(request: Request, session: ReadSessionDep):
    network = _network(request)
    history = account_history(session, network, limit=90)
    oldest_first = list(reversed(history))
    return render(
        request,
        "stats.html",
        history=history,
        reach_chart=line_chart(
            [(s.period_start, s.metrics.get("reach")) for s in oldest_first],
            title="Reach per day",
            series="primary",
        ),
        followers_chart=line_chart(
            [(s.period_start, s.followers) for s in oldest_first],
            title="Followers",
            series="secondary",
        ),
        media=latest_per_media(session, network, limit=100),
    )


@pages.get("/stats/{media_ref}")
def media_stats(media_ref: str, request: Request, session: ReadSessionDep):
    snapshots = media_history(media_ref, session, _network(request), limit=365)
    if not snapshots:
        return render(request, "not_found.html", 404, what="media")
    metrics = sorted({name for s in snapshots for name in s.metrics})
    return render(
        request, "media_stats.html", media_ref=media_ref, snapshots=snapshots, metrics=metrics
    )


@pages.get("/rules")
def rules(request: Request, session: ReadSessionDep):
    network = _network(request)

    def out(found) -> list[RuleOut]:
        return [RuleOut.model_validate(rule, from_attributes=True) for rule in found]

    return render(
        request,
        "rules.html",
        rules=out(list_rules(session, network, limit=1000)),
        archived=out(list_rules(session, network, archived=True, limit=1000)),
    )


@pages.get("/events")
def events(request: Request, session: ReadSessionDep):
    network = _network(request)
    names = {rule.id: rule.name for rule in list_rules(session, network, limit=1000)}
    names |= {r.id: r.name for r in list_rules(session, network, archived=True, limit=1000)}
    return render(
        request,
        "events.html",
        events=list_events(session, network, limit=100),
        deliveries=list_deliveries(session, network, limit=100),
        rule_names=names,
    )


@pages.get("/incidents")
def incidents(request: Request, session: ReadSessionDep):
    return render(request, "incidents.html", incidents=list_incidents(request, session, limit=200))


def install(app: FastAPI) -> None:
    app.add_exception_handler(LoginRequired, _login_redirect)
    app.mount("/ui/static", StaticFiles(directory=HERE / "static"), name="ui-static")
    app.include_router(public)
    app.include_router(pages)
