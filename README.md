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

Run exactly one worker: two would both publish what is due. The worker holds a lock file next to the
database, so a second one (`--scale worker=2`) exits with an error and restarts in a loop. The lock is
reliable on a local Docker volume, not necessarily on a network filesystem; keep `/data` local.

A fresh install runs in simulation mode: a fake Instagram with synthetic data, and no call ever
reaches Meta. Create an API token, granting only the groups the client needs:

```sh
docker compose exec api jorgex-social create-token --name my-agent \
  --grant posts=write --grant scheduling=write --grant stats=read
curl -H "Authorization: Bearer jxs_your_token_here" http://127.0.0.1:8000/v1/token
```

The token is printed once. `jorgex-social list-tokens` and `revoke-token --name ...` manage them the
same way. The API reference is at `/docs` in simulation; in real mode it answers 404 unless
`JORGEX_SOCIAL_API_DOCS=true`.

### Reverse proxy

Only three paths need to be public: `/m/*`, the short-lived media URLs Instagram downloads from to
publish, `/health`, and `/webhook`, where Meta notifies new comments and messages (see
[Webhook](#webhook)). The API itself (`/v1`) stays on `127.0.0.1:8000` and is reached through an
SSH tunnel (below). A [Caddy](https://caddyserver.com/) site on the same host, which also obtains the
HTTPS certificate; replace the domain with yours:

```caddyfile
social.example.com {
	header {
		# Start modest; raise max-age once HTTPS on this domain is known to work.
		Strict-Transport-Security "max-age=86400"
		X-Content-Type-Options nosniff
		Referrer-Policy no-referrer
		X-Frame-Options DENY
		-Server
	}

	@public {
		path /m/* /health
		method GET HEAD
	}
	handle @public {
		# GET requests carry no body; uploads arrive only through the SSH tunnel.
		request_body {
			max_size 1KB
		}
		reverse_proxy 127.0.0.1:8000
	}
	@webhook {
		path /webhook
		method GET POST
	}
	handle @webhook {
		# Meta's notifications are small; the service refuses bodies over 1 MB in any case.
		request_body {
			max_size 1MB
		}
		reverse_proxy 127.0.0.1:8000
	}

	handle {
		respond 404
	}
}
```

To use the API from your machine, open a tunnel and talk to `localhost` as if the API ran there:

```sh
ssh -N -L 8000:127.0.0.1:8000 <server>
curl -H "Authorization: Bearer jxs_your_token_here" http://localhost:8000/v1/token
```

Point the MCP server at `http://localhost:8000` while the tunnel is open (see
[MCP server](#mcp-server)). Exposing `/v1` publicly also works, since every call needs a token, but
then add a rate limit at the proxy so nobody can hammer it with guessed tokens.

### Web interface

A read-only web interface at `/ui` shows the connection, scheduled and published posts, insights,
rules, events and incidents. It cannot change anything; agents and scripts keep using the API. It is
for one operator, who signs in with a password and a TOTP code from an authenticator app:

```sh
docker compose exec api jorgex-social create-operator   # asks for the password (12+ characters)
docker compose exec api jorgex-social enroll-totp       # shows an otpauth:// URI, asks for a code
```

`enroll-totp` prints the secret as an `otpauth://` URI (and as a plain key) for your authenticator
app, and only enables it once you type a code the app shows. Running it again replaces the second
factor; `set-password` changes the password. Both sign every browser out. Five failed sign-ins in a
row lock the operator out for 15 minutes; `set-password` lifts the lock.

Like the API, `/ui` is not public with the recommended proxy. Open the SSH tunnel and browse to
`http://localhost:8000/ui/`. The session cookie is `Secure` for every host except a loopback one
(`localhost`, `127.0.0.0/8`, `::1`) over plain http, which is how the tunnel reaches it; if you put
`/ui` behind a proxy, serve it over HTTPS. Sessions last 12 hours at most and end after an hour
without activity.

### Switch to real mode

1. Connect your account while the service is still in simulation. The command reads a long-lived
   Instagram access token from standard input, without echoing it, and stores it encrypted. Never
   pass the token as an argument: it would stay in the shell history.

   ```sh
   docker compose run --rm -e JORGEX_SOCIAL_SIMULATION=false api jorgex-social connect-instagram
   ```

   Without an interactive terminal, paste the token into a shell variable and pipe it, so it is
   never written to a file:

   ```sh
   read -rs IG_TOKEN
   printf '%s\n' "$IG_TOKEN" | docker compose run --rm -T -e JORGEX_SOCIAL_SIMULATION=false \
     api jorgex-social connect-instagram
   unset IG_TOKEN
   ```
2. In `.env`, set `JORGEX_SOCIAL_PUBLIC_URL=https://social.example.com` (your domain) and
   `JORGEX_SOCIAL_SIMULATION=false`.
3. Apply the change with `docker compose up -d`. `GET /v1/account` reports the connection, open
   incidents, the kill switch and the webhook.

In real mode the worker refuses to start without a connected account, so connect before switching.

### Webhook

Without it, automatic replies still work: the worker polls comments and messages every two minutes.
With it, Meta notifies each new comment and message as it happens and the reply goes out within
seconds (Meta's messaging policy asks bots to answer within 30 seconds). Polling keeps running and
catches anything a notification missed; a comment seen both ways is answered once.

1. Generate a random verify token, for example `openssl rand -hex 32`.
2. In `.env`, set `JORGEX_SOCIAL_WEBHOOK_VERIFY_TOKEN` to it and `JORGEX_SOCIAL_META_APP_SECRET` to
   your Meta app's secret (App settings > Basic). Keep `.env` readable only by you (`chmod 600 .env`).
   Apply with `docker compose up -d`. Until both are set, `/webhook` answers 404.
3. Make `/webhook` public at the proxy (above).
4. In the Meta app dashboard, Instagram API setup > Configure webhooks, enter
   `https://social.example.com/webhook` and the verify token, verify, and subscribe to the
   `comments` and `messages` fields. Your account must also be subscribed to the app (Meta's "Set up Instagram Webhooks" guide: the dashboard's
   webhook subscription for the account, or `POST /<IG_ID>/subscribed_apps`).

Every notification must carry Meta's signature (`X-Hub-Signature-256`) made with the app secret;
anything else gets 403 and is not processed. Notification bodies hold other people's text and are
never logged or stored. `GET /v1/account` shows when the last valid notification arrived, and the
incident `webhook_silent` opens when polling finds activity the webhook has not reported for a day.

A direct message you send by hand from Instagram pauses automatic replies to that person for
`JORGEX_SOCIAL_HUMAN_PAUSE_MINUTES` (30), so a rule does not talk over you. This relies on Meta
echoing messages sent from the app, which is not yet confirmed; `POST /v1/conversations/{id}/pause`
does the same by hand.

### Backups

Back up the database and the secret key, and keep them apart: the key decrypts the Instagram
credential stored in the database, so one without the other is useless to a thief, and the database
is useless to you without its key. Keep both outside the clone, so neither can end up in a commit or in an image.

Copy the database with SQLite's online backup, which is consistent while both services keep running:

```sh
install -d -m 700 ~/jorgex-social-backups
docker compose exec api python -c "import sqlite3; sqlite3.connect('/data/jorgex-social.db').backup(sqlite3.connect('/data/backup.db'))"
docker compose cp api:/data/backup.db ~/jorgex-social-backups/jorgex-social-$(date +%F).db
chmod 600 ~/jorgex-social-backups/*.db
docker compose exec api rm /data/backup.db
```

Copy the key once, and again only if you replace it. Take it off the server to a separate secure
place (a password manager or an encrypted drive), never next to the database backups:

```sh
install -d -m 700 ~/jorgex-social-key
docker compose cp api:/data/secret.key ~/jorgex-social-key/secret.key
# move it to its secure place, then delete the local copy
```

Media are not backed up: they are deleted seven days after they are published.

### Upgrade

Back up the database first, since migrations run on start. Then:

```sh
git pull
docker compose up -d --build
```

Compose stops the old worker before it replaces `api`, so the new version migrates with no worker
running, and starts the new worker once `api` is healthy again. On stop, the worker finishes the
publication in progress before exiting, so an upgrade or `docker compose down` can take up to
15 minutes while one is running.

To roll back, check out the previous version and run the same command; if its migrations are
incompatible with the newer database, restore the backup as well. You are responsible for keeping
your installation updated.

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
described in [Reverse proxy](#reverse-proxy).

## License

MIT. See `LICENSE`.

The web interface ships the Outfit, Inter and JetBrains Mono fonts, which are not under MIT: each is
licensed under the SIL Open Font License 1.1, whose text is next to the font files in
`src/jorgex_social/ui/static/fonts/`.
