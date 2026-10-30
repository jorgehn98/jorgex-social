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

## MCP server

`jorgex-social-mcp` is a thin MCP server (stdio) that runs on the agent's machine and turns each
tool into one call to the REST API. It needs the `mcp` extra (`uv sync --extra mcp`, or
`pip install "jorgex-social[mcp]"`) and two environment variables: `JORGEX_SOCIAL_URL`, the base URL
of the API, and `JORGEX_SOCIAL_TOKEN`, an API token created with `create-token`. At startup it reads
the token's permissions from `GET /v1/token` and exposes only the tools that token can use; the API
still checks every call. API errors reach the agent as they come, with their stable `code`.

Most agent clients take a JSON entry like this one (the exact file and key depend on the client):

```json
{
  "mcpServers": {
    "jorgex-social": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/jorgex-social", "--extra", "mcp", "jorgex-social-mcp"],
      "env": {
        "JORGEX_SOCIAL_URL": "https://social.example.com",
        "JORGEX_SOCIAL_TOKEN": "jxs_your_token_here"
      }
    }
  }
}
```

Give the agent its own token with only the groups it needs. The URL must use HTTPS unless the API is
on the same machine (`localhost`, `127.0.0.0/8` or `::1`): the token travels in every request.

## License

MIT. See `LICENSE`.
