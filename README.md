# jorgex-social

Self-hosted, agent-first tool for one Instagram professional account: schedule and publish posts,
keep insights history, and reply to comments and direct messages by rules. Everything is done
through an authenticated REST API, so an AI agent can operate it end to end.

**Status: early development. Not usable yet.**

## Development

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run jorgex-social migrate
uv run jorgex-social create-token --name my-agent --grant posts=write --grant stats=read
uv run jorgex-social serve        # API on http://127.0.0.1:8000, docs at /docs
uv run jorgex-social worker       # background worker
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Configuration comes from `JORGEX_SOCIAL_*` environment variables; see `.env.example`. A fresh install runs
in simulation mode and never calls Meta.

To go live, set `JORGEX_SOCIAL_SIMULATION=false` and connect your account once with
`uv run jorgex-social connect-instagram`, which reads a long-lived Instagram token from standard input
without echoing it (`--token` arguments would end up in the shell history, so there is none). The token
is stored encrypted with AES-GCM under the key in `JORGEX_SOCIAL_SECRET_KEY_FILE`, which `migrate`
creates with mode 600. Back that key up separately from the database. The worker renews the token
before it expires, and `GET /v1/account` reports the connection status, open incidents and the kill
switch.

## License

MIT. See `LICENSE`.
