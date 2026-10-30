# jorgex-social

Self-hosted, agent-first tool for one Instagram professional account: schedule and publish posts,
keep insights history, and reply to comments and direct messages by rules. Everything is done
through an authenticated REST API, so an AI agent can operate it end to end.

**Status: early. It runs in production for one account, but expect breaking changes until a first release.**

## What it does

- Publishes images, carousels, reels and stories to your own Instagram professional account, at the
  time you schedule (the Instagram API does not schedule; the worker does).
- Takes insights snapshots and keeps their history, beyond the 90 days Instagram keeps.
- Replies to comments and direct messages by rules ("if a comment on this post contains X, reply
  with one of these versions"), within Meta's limits: one private reply per comment within 7 days,
  and direct messages only within 24 hours of the person's last message.
- Exposes all of it through a REST API, a thin [MCP server](#mcp-server) for agents, and a
  read-only web interface.

## What it does not do

- Start conversations or send bulk or promotional messages.
- Store the text of comments or messages. It reads them in memory, matches them against the rules
  and discards them; only Instagram identifiers, dates and status are kept. There is no inbox and no
  conversation history.
- Serve several accounts, several users, or other networks.

## What you need

- A server with Docker and the Compose plugin.
- For real mode: an Instagram professional account, your own Meta app, and a public domain served
  over HTTPS.

Simulation needs none of the real-mode requirements: a fresh install runs against a fake Instagram
and never calls Meta.

## Install

```sh
git clone <repository-url> jorgex-social && cd jorgex-social
cp .env.example .env
docker compose up -d --build
curl http://127.0.0.1:8000/health      # {"status":"ok","simulation":true}
```

[INSTALL.md](INSTALL.md) covers the rest: API tokens, the reverse proxy, the web interface, creating
your Meta app step by step, switching to real mode, the webhook, backups and upgrades.

## Before you use it

- **No support.** The code is public so anyone can run their own installation; nobody provides
  accounts, hosting or support. Issues and pull requests are read when possible, with no commitment.
- **Not affiliated with Meta.** Instagram and Meta are trademarks of Meta Platforms, Inc.
- **You are responsible for your installation**: following Meta's platform terms and its policies on
  automated messages, your own legal obligations towards the people who write to you, and keeping
  the installation updated.
- **Security**: see [SECURITY.md](SECURITY.md). Never paste tokens or secrets into issues.

## Development

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run jorgex-social migrate
uv run jorgex-social create-token --name my-agent --grant posts=write --grant stats=read
uv run jorgex-social create-operator && uv run jorgex-social enroll-totp   # for /ui
uv run jorgex-social serve        # API on http://127.0.0.1:8000, docs at /docs, web interface at /ui
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
        "JORGEX_SOCIAL_URL": "http://localhost:8000",
        "JORGEX_SOCIAL_TOKEN": "jxs_your_token_here"
      }
    }
  }
}
```

Give the agent its own token with only the groups it needs. The URL must use HTTPS unless the API is
on the same machine (`localhost`, `127.0.0.0/8` or `::1`): the token travels in every request. With
the recommended proxy the API is not public, so `http://localhost:8000` goes through the SSH tunnel
described in [INSTALL.md](INSTALL.md#reverse-proxy).

## License

MIT. See `LICENSE`.

The web interface ships the Outfit, Inter and JetBrains Mono fonts, which are not under MIT: each is
licensed under the SIL Open Font License 1.1, whose text is next to the font files in
`src/jorgex_social/ui/static/fonts/`.

Dependencies are installed from PyPI and keep their own licenses.
