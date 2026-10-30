# jorgex-social

Self-hosted, agent-first tool for one Instagram professional account: schedule and publish posts,
keep insights history, and reply to comments and direct messages by rules. Everything is done
through an authenticated REST API, so an AI agent can operate it end to end.

**Status: early development. Not usable yet.**

## Install

One Docker image runs two services with `docker compose`: `api`, the REST API, and `worker`,
which publishes on schedule, takes insights snapshots, renews the Instagram credential and sends
automatic replies. Both run as an unprivileged user on a read-only filesystem; everything they write
(the SQLite database, uploaded media and the secret key) lives in the `data` volume, mounted at
`/data`.

### Requirements

- Docker with the Compose plugin.
- For real mode only: an Instagram professional account, your own Meta app with Instagram API
  access, and a public domain served over HTTPS. Instagram downloads the media to publish from
  this service, so real publishing does not work without it. Simulation needs none of this.

### Start in simulation

```sh
git clone <repository-url> jorgex-social && cd jorgex-social
cp .env.example .env
docker compose up -d --build
curl http://127.0.0.1:8000/health      # {"status":"ok","simulation":true}
```

The `api` service applies the database migrations before it starts serving, and creates the secret
key on the first run. The `worker` starts only once `api` is healthy, so it never runs against a
database that is still being migrated. The API listens on `127.0.0.1:8000` only; put a reverse proxy
in front of it (see below).

A fresh install runs in simulation mode: a fake Instagram with synthetic data, and no call ever
reaches Meta. Create an API token, granting only the groups the client needs:

```sh
docker compose exec api jorgex-social create-token --name my-agent \
  --grant posts=write --grant scheduling=write --grant stats=read
curl -H "Authorization: Bearer jxs_your_token_here" http://127.0.0.1:8000/v1/token
```

The token is printed once. `jorgex-social list-tokens` and `revoke-token --name ...` manage them the
same way. The API reference is at `/docs`.

### Reverse proxy

A minimal [Caddy](https://caddyserver.com/) site on the same host, which also obtains the HTTPS
certificate. Replace the domain with yours; the body limit must stay above 300 MB, the largest video
Instagram accepts.

```caddyfile
social.example.com {
	request_body {
		max_size 400MB
	}
	reverse_proxy 127.0.0.1:8000
}
```

### Switch to real mode

1. Connect your account while the service is still in simulation. The command reads a long-lived
   Instagram access token from standard input, without echoing it, and stores it encrypted. Never
   pass the token as an argument: it would stay in the shell history.

   ```sh
   docker compose run --rm -e JORGEX_SOCIAL_SIMULATION=false api jorgex-social connect-instagram
   ```

   To pipe it from a file instead, add `-T` after `run` and end the command with `< token.txt`.
2. In `.env`, set `JORGEX_SOCIAL_PUBLIC_URL=https://social.example.com` (your domain) and
   `JORGEX_SOCIAL_SIMULATION=false`.
3. Apply the change with `docker compose up -d`. `GET /v1/account` reports the connection, open
   incidents and the kill switch.

In real mode the worker refuses to start without a connected account, so connect before switching.

### Backups

Back up the database and the secret key, and keep them apart: the key decrypts the Instagram
credential stored in the database, so one without the other is useless to a thief, and the database
is useless to you without its key. Copy the database with SQLite's online backup, which is consistent
while both services keep running:

```sh
docker compose exec api python -c "import sqlite3; sqlite3.connect('/data/jorgex-social.db').backup(sqlite3.connect('/data/backup.db'))"
docker compose cp api:/data/backup.db ./jorgex-social-$(date +%F).db
docker compose exec api rm /data/backup.db
```

Copy the key once, and again only if you replace it, to somewhere other than the database backups:

```sh
docker compose cp api:/data/secret.key ./secret.key
```

Media are not backed up: they are deleted seven days after they are published.

### Upgrade

Back up the database first, since migrations run on start. Then:

```sh
git pull
docker compose up -d --build
```

To roll back, check out the previous version and run the same command; if its migrations are
incompatible with the newer database, restore the backup as well. On stop, the worker finishes the
publication in progress before exiting, so `docker compose down` can take up to 15 minutes while one
is running. You are responsible for keeping your installation updated.

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
