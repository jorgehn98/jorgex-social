"""Read-only web interface for the operator (docs: interfaz). Pages read through the read-only
session and reuse the API's own query functions; only signing in and out write."""

import ipaddress
import logging
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import OperationalError
from starlette.concurrency import run_in_threadpool

from jorgex_social.access import operator
from jorgex_social.access.deps import ReadSessionDep
from jorgex_social.account.api import get_account, list_incidents
from jorgex_social.publishing import media as storage
from jorgex_social.publishing.api import get_post, list_posts, next_scheduled
from jorgex_social.publishing.models import Media, MediaType, PostStatus
from jorgex_social.replies.api import list_deliveries, list_events, list_rules
from jorgex_social.stats.api import account_history, latest_per_media, media_history
from jorgex_social.ui.chart import line_chart

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
COOKIE = "jxs_session"
INVALID = "Invalid credentials."
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


def require_operator(request: Request) -> None:
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


def render(request: Request, name: str, status_code: int = 200, **context) -> Response:
    return templates.TemplateResponse(
        request, name, context, status_code=status_code, headers=HEADERS
    )


def _login_redirect(request: Request, _: LoginRequired) -> Response:
    response = RedirectResponse("/ui/login", status_code=303, headers=HEADERS)
    if COOKIE in request.cookies:  # expired or unknown: no use keeping it
        response.delete_cookie(COOKIE, path="/ui")
    return response


public = APIRouter(prefix="/ui", include_in_schema=False)
pages = APIRouter(prefix="/ui", include_in_schema=False, dependencies=[Depends(require_operator)])


# Signing in and out


@public.get("/login")
def login_form(request: Request):
    return render(request, "login.html")


async def _form(request: Request) -> dict[str, str]:
    # Parsed by hand: FastAPI's Form needs python-multipart, a dependency the service otherwise
    # does without. The body is capped, as nothing legitimate here is large.
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


@public.post("/login")
async def login(request: Request):
    form = await _form(request)
    state = request.app.state
    session_id = await run_in_threadpool(
        operator.log_in,
        state.sessionmaker,
        state.read_sessionmaker,
        state.settings.secret_key_file,
        form.get("password", ""),
        form.get("code", ""),
        state.clock.now(),
    )
    if session_id is None:
        return render(request, "login.html", 401, error=INVALID)
    response = RedirectResponse("/ui/", status_code=303, headers=HEADERS)
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
    session_id = request.cookies.get(COOKIE)
    if session_id:
        # Looked up first in a read transaction, so a made-up cookie never takes the write lock.
        with request.app.state.read_sessionmaker() as session, session.begin():
            stored = operator.is_stored(session, session_id)
        if stored:
            with request.app.state.sessionmaker() as session, session.begin():
                operator.log_out(session, session_id)
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
    return render(
        request,
        "rules.html",
        rules=list_rules(session, network, limit=1000),
        archived=list_rules(session, network, archived=True, limit=1000),
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
