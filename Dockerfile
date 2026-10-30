# One image for both services: the API server (default command) and the worker.

FROM python:3.14.7-slim-trixie AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.24 /uv /bin/uv
# Bytecode is compiled here because the runtime filesystem is read-only.
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
# Dependencies first, in their own layer, so a code change does not reinstall them.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project
COPY . .
# --no-editable puts the package itself in the virtualenv, so the runtime image needs no source.
# The `mcp` extra stays out: the MCP server runs on the agent's machine, not here.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable


FROM python:3.14.7-slim-trixie
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --home-dir /nonexistent app \
    && mkdir /data && chown app:app /data
COPY --from=build /app/.venv /app/.venv
# /data is the only writable path: the database, the media and the secret key. A new named
# volume mounted there takes this directory's owner, so the service can write to it.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JORGEX_SOCIAL_DATABASE_PATH=/data/jorgex-social.db \
    JORGEX_SOCIAL_MEDIA_DIR=/data/media \
    JORGEX_SOCIAL_SECRET_KEY_FILE=/data/secret.key
USER app
WORKDIR /data
VOLUME /data
EXPOSE 8000
# The slim image has no curl; a failed request or a non-2xx answer exits non-zero.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]
CMD ["jorgex-social", "serve", "--host", "0.0.0.0"]
