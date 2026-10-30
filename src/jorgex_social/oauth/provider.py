"""The SDK's OAuthAuthorizationServerProvider over oauth/store.py. The SDK handles the protocol
(PKCE, redirect URI match, request validation); this decides who may register, binds every
request to this server's resource and keeps all state in the database.

Expiry is decided by the store with the app's clock. The SDK compares `expires_at` with the
wall clock too, so the times handed to it are the remaining lifetime from the wall clock's now."""

import time
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import urlencode, urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.concurrency import run_in_threadpool

from jorgex_social.oauth import store

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_REDIRECT_URIS = 5
MAX_URI_LENGTH = 300
MAX_NAME_LENGTH = 100
GRANT_TYPES = {"authorization_code", "refresh_token"}
SUBJECT = "operator"


def is_loopback_uri(uri: str) -> bool:
    try:
        parts = urlsplit(uri)
        parts.port  # noqa: B018  (raises on a malformed port)
    except ValueError:
        return False
    return (
        parts.scheme == "http"
        and parts.hostname in LOOPBACK_HOSTS
        and parts.username is None
        and parts.password is None
        and not parts.fragment
    )


def _wall_clock(remaining_from: datetime, now: datetime) -> float:
    return time.time() + (remaining_from - now).total_seconds()


class Provider:
    def __init__(self, state: Any, issuer: str, resource: str, redirect_uris: tuple[str, ...]):
        self._state = state
        self.issuer = issuer
        self.resource = resource
        self._allowed_redirects = set(redirect_uris)

    def redirect_allowed(self, uri: str) -> bool:
        """Loopback, so the code always goes back to the operator's own machine, or an https
        URI listed in the configuration. Matched exactly by the SDK when authorizing."""
        return len(uri) <= MAX_URI_LENGTH and (
            uri in self._allowed_redirects or is_loopback_uri(uri)
        )

    def _run(self, write: bool, fn: Callable[..., Any], *args: Any) -> Any:
        sessions = self._state.sessionmaker if write else self._state.read_sessionmaker
        with sessions() as session, session.begin():
            return fn(session, *args, self._state.clock.now())

    async def _read(self, fn: Callable[..., Any], *args: Any) -> Any:
        return await run_in_threadpool(self._run, False, fn, *args)

    async def _write(self, fn: Callable[..., Any], *args: Any) -> Any:
        return await run_in_threadpool(self._run, True, fn, *args)

    def _token(self, issued: store.Issued) -> OAuthToken:
        return OAuthToken(
            access_token=issued.access,
            expires_in=int(store.ACCESS_LIFETIME.total_seconds()),
            refresh_token=issued.refresh,
        )

    # Clients

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        def info(session, client_id, now):
            found = store.find_client(session, client_id, now)
            return found and found.info

        found = await self._read(info, client_id)
        return None if found is None else OAuthClientInformationFull.model_validate(found)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # The SDK turns an omitted method into client_secret_post and mints a secret: only an
        # explicit "none" is a public client.
        if client_info.token_endpoint_auth_method != "none":
            raise RegistrationError(
                "invalid_client_metadata",
                "only public clients are accepted: token_endpoint_auth_method must be none",
            )
        if not set(client_info.grant_types) <= GRANT_TYPES:
            raise RegistrationError(
                "invalid_client_metadata", "grant_types: authorization_code and refresh_token only"
            )
        name = client_info.client_name
        if name is not None and len(name) > MAX_NAME_LENGTH:
            raise RegistrationError("invalid_client_metadata", "client_name is too long")
        uris = [str(uri) for uri in client_info.redirect_uris or []]
        if not 1 <= len(uris) <= MAX_REDIRECT_URIS or not all(map(self.redirect_allowed, uris)):
            raise RegistrationError(
                "invalid_redirect_uri",
                "redirect URIs must be loopback (http://127.0.0.1, http://localhost or "
                "http://[::1], any port) or listed in this server's configuration",
            )
        # Only what the protocol needs is kept; the rest of the metadata is not stored.
        info = client_info.model_dump(
            mode="json",
            exclude_none=True,
            include={
                "client_id",
                "client_id_issued_at",
                "client_name",
                "redirect_uris",
                "grant_types",
                "response_types",
                "token_endpoint_auth_method",
                "scope",
            },
        )
        await self._write(store.add_client, client_info.client_id, name, info)

    # Authorization

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource != self.resource:
            raise AuthorizeError("invalid_target", f"resource must be {self.resource}")
        request_id = await self._write(
            store.create_request, client.client_id, params.model_dump(mode="json")
        )
        return f"{self.issuer}/ui/oauth/consent?{urlencode({'req': request_id})}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        def load(session, code, client_id, now):
            found = store.find_code(session, code, client_id, now)
            return found and (found.params, found.expires_at, found.used_at is not None, now)

        found = await self._read(load, authorization_code, client.client_id)
        if found is None:
            return None
        params, expires_at, used, now = found
        if used:
            await self._write(store.code_replayed, authorization_code)
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=[],
            expires_at=_wall_clock(expires_at, now),
            client_id=client.client_id,
            code_challenge=params["code_challenge"],
            redirect_uri=params["redirect_uri"],
            redirect_uri_provided_explicitly=params["redirect_uri_provided_explicitly"],
            resource=params["resource"],
            subject=SUBJECT,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        try:
            issued = await self._write(
                store.exchange_code, authorization_code.code, client.client_id
            )
        except store.Replayed:
            await self._write(store.code_replayed, authorization_code.code)
            raise TokenError("invalid_grant", "authorization code does not exist") from None
        except store.Invalid:
            raise TokenError("invalid_grant", "authorization code does not exist") from None
        return self._token(issued)

    # Refresh

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        usable = await self._read(store.check_refresh, refresh_token, client.client_id)
        if usable is None:
            return None
        if not usable:
            await self._write(store.refresh_replayed, refresh_token, client.client_id)
            return None
        # No expires_at: the store already checked it with the app's clock.
        return RefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=[],
            resource=self.resource,
            subject=SUBJECT,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        try:
            issued = await self._write(store.rotate, refresh_token.token, client.client_id)
        except store.Replayed:
            await self._write(store.refresh_replayed, refresh_token.token, client.client_id)
            raise TokenError("invalid_grant", "refresh token does not exist") from None
        except store.Invalid:
            raise TokenError("invalid_grant", "refresh token does not exist") from None
        return self._token(issued)

    # Access

    async def load_access_token(self, token: str) -> AccessToken | None:
        def load(session, token, now):
            found = store.find_access(session, token, None, now)
            return found and (found, now)

        # The token's own resource: the SDK's bearer middleware compares it with this server's
        # (validate_token_resource) and refuses any other.
        found = await self._read(load, token)
        if found is None:
            return None
        access, now = found
        return AccessToken(
            token=token,
            client_id=access.client_id,
            scopes=[],
            expires_at=int(_wall_clock(access.expires_at, now)),
            resource=access.resource,
            subject=SUBJECT,
        )

    # Revocation

    async def revoke(self, token: str, client_id: str) -> None:
        """RFC 7009 for public clients (the SDK's own endpoint requires a client secret). Looks
        first in a read transaction, so a made-up token never takes the write lock."""

        def found(session, token, client_id, _now):
            return store.revocable(session, token, client_id) is not None

        if await self._read(found, token, client_id):
            await self._write(store.revoke_by_client, token, client_id)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        await self.revoke(token.token, token.client_id)
