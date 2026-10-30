# Installing jorgex-social

This guide takes you from a clean server to a running installation connected to your own Instagram
professional account. Start in simulation, which needs no Meta account, and switch to real mode only
once the service runs.

Every installation uses its own Meta app, its own credentials and one Instagram account. Nothing is
shared between installations, and nobody provides accounts, hosting or support for you.

**Never paste a token, an app secret or a verify token into a chat, an issue, a pull request or a
screenshot.** If one leaks, assume generating a new token does not invalidate the old one: remove the app from
the account's app permissions in Instagram (or reset the app secret in Meta's dashboard), then
connect again with `connect-instagram --replace`.

- [Requirements](#requirements)
- [Start in simulation](#start-in-simulation)
- [Reverse proxy](#reverse-proxy)
- [Web interface](#web-interface)
- [Create your Meta app](#create-your-meta-app)
- [Switch to real mode](#switch-to-real-mode)
- [Webhook](#webhook)
- [Backups](#backups)
- [Upgrade](#upgrade)

## Requirements

- Docker with the Compose plugin.
- For real mode only:
  - an Instagram **professional** account (Business or Creator). A personal account cannot use the
    API;
  - a Meta developer account, to create your own Meta app;
  - a public domain served over HTTPS. Instagram downloads the media to publish from this service,
    and Meta sends webhook notifications to it, so real mode does not work without it;
  - a public privacy policy URL and a terms of service URL for the Meta app.

One Docker image runs two services with `docker compose`: `api`, the REST API, and `worker`, which
publishes on schedule, takes insights snapshots, renews the Instagram credential and sends automatic
replies. Both run as an unprivileged user on a read-only filesystem; everything they write (the SQLite
database, uploaded media and the secret key) lives in the `data` volume, mounted at `/data`.

## Start in simulation

```sh
git clone <repository-url> jorgex-social && cd jorgex-social
cp .env.example .env
docker compose up -d --build
curl http://127.0.0.1:8000/health      # {"status":"ok","simulation":true}
```

The `api` service applies the database migrations before it starts serving, and creates the secret
key on the first run. The `worker` starts only once `api` is healthy, so it never runs against a
database that is still being migrated. The API listens on `127.0.0.1:8000` only; put a reverse proxy
in front of it (see [Reverse proxy](#reverse-proxy)).

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

## Reverse proxy

Only a few paths need to be public: `/ui`, the web interface (see [Web interface](#web-interface)),
`/m/*`, the short-lived media URLs Instagram downloads from to publish, `/health`, `/webhook`,
where Meta notifies new comments and messages (see [Webhook](#webhook)), and what remote agents use
(see [Remote MCP](#remote-mcp)): `/mcp`, the OAuth endpoints (`/authorize`, `/token`, `/register`,
`/revoke` and their `/.well-known/oauth-*` metadata) and `/upload/*`, the one-use upload URLs.
Everything else answers 404 at the proxy. The API itself (`/v1`) stays on `127.0.0.1:8000` and is reached through an SSH tunnel
(below). A [Caddy](https://caddyserver.com/) site on the same host, which also obtains the HTTPS
certificate; replace the domain with yours:

```caddyfile
social.example.com {
	header {
		# Start modest; raise max-age to a year (31536000) once HTTPS on this domain is known to work.
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
		reverse_proxy 127.0.0.1:8000 {
			header_up X-Forwarded-For {remote_host}
		}
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
		reverse_proxy 127.0.0.1:8000 {
			header_up X-Forwarded-For {remote_host}
		}
	}
	@ui {
		path /ui /ui/*
		method GET HEAD POST
	}
	handle @ui {
		# The sign-in form is the largest body here; the service refuses more than 4 KB.
		request_body {
			max_size 16KB
		}
		reverse_proxy 127.0.0.1:8000 {
			header_up X-Forwarded-For {remote_host}
		}
	}

	@mcp {
		path /mcp /authorize /token /register /revoke /.well-known/oauth-authorization-server /.well-known/oauth-protected-resource/*
		method GET POST DELETE OPTIONS
	}
	handle @mcp {
		# MCP messages and OAuth forms are small; the service refuses OAuth bodies over 16 KB.
		request_body {
			max_size 1MB
		}
		reverse_proxy 127.0.0.1:8000 {
			header_up X-Forwarded-For {remote_host}
			# /mcp answers with event streams; pass each event on at once.
			flush_interval -1
		}
	}
	@upload {
		path /upload/*
		method POST
	}
	handle @upload {
		# One file per one-use URL; the service stops at 300 MB too.
		request_body {
			max_size 301MB
		}
		reverse_proxy 127.0.0.1:8000 {
			header_up X-Forwarded-For {remote_host}
		}
	}

	handle {
		respond 404
	}
}
```

`header_up X-Forwarded-For {remote_host}` **replaces** whatever the client sent with the address it
connected from; never let the proxy append to a client's header. The service believes that header
only from the proxy (`JORGEX_SOCIAL_FORWARDED_ALLOW_IPS`), and limits sign-in attempts per address.
With Docker, the proxy reaches the container from the gateway of the network `compose.yaml` defines
(`172.30.250.1` unless you set `JORGEX_SOCIAL_DOCKER_SUBNET` and `JORGEX_SOCIAL_DOCKER_GATEWAY` in
`.env`), so that is the address trusted. Without Docker the default trusts `127.0.0.1` and `::1`.

The service also answers 404 to `/v1`, `/docs`, `/redoc` and `/openapi.json` for any request that
carries `X-Forwarded-For` (or `Forwarded`, or `X-Real-IP`), so a mistake in the proxy's allowlist
does not publish the API. A proxy other than Caddy must set `X-Forwarded-For` on every request it
forwards for both protections to work.

Check that the service sees real addresses once the proxy is up: sign in once with a wrong
password from your browser through the public URL, then look at the access log:

```sh
docker compose logs api | grep '/ui/login'
```

The line must show your own public address. If it shows the Docker gateway (`172.30.250.1`) instead,
the trusted address does not match the proxy and every visitor shares one sign-in limit, yours
included: fix `JORGEX_SOCIAL_FORWARDED_ALLOW_IPS` (or the two Docker variables) before going on.

Caddy writes no access log unless you add a `log` directive; if you do, it already redacts the
`Authorization` and `Cookie` headers, so leave `log_credentials` off. It does not redact paths: an
upload URL's path is its credential (one use, ten minutes), so keep such a log private, or leave it
off as above. The service's own access log shows `/upload/[redacted]`.

To use the API from your machine, open a tunnel and talk to `localhost` as if the API ran there:

```sh
ssh -N -L 8000:127.0.0.1:8000 <server>
curl -H "Authorization: Bearer jxs_your_token_here" http://localhost:8000/v1/token
```

Point the MCP server at `http://localhost:8000` while the tunnel is open (see
[MCP server](README.md#mcp-server)).

## Web interface

A read-only web interface at `/ui` shows the connection, scheduled and published posts, insights,
rules, events and incidents. It cannot change anything; agents and scripts keep using the API. It is
for one operator, who signs in with a password and a TOTP code from an authenticator app:

```sh
docker compose exec api jorgex-social create-operator   # asks for the password (12+ characters)
docker compose exec api jorgex-social enroll-totp       # shows an otpauth:// URI, asks for a code
```

`enroll-totp` prints the secret as an `otpauth://` URI (and as a plain key) for your authenticator
app, and only enables it once you type a code the app shows. Running it again replaces the second
factor; `set-password` changes the password. Both sign every browser out.

With the proxy above, open `https://social.example.com/ui/`. `JORGEX_SOCIAL_PUBLIC_URL` must be that
same origin: every form the interface posts (signing in and out) must come from it, which is what
stops another site from posting on your behalf. Through the SSH tunnel, `http://localhost:8000/ui/`
works too. The session cookie is `Secure` for every host except a loopback one (`localhost`,
`127.0.0.0/8`, `::1`) over plain http, which is how the tunnel reaches it. Sessions last 12 hours at
most and end after an hour without activity.

Each client address (IPv6: each `/64`) gets 5 sign-in attempts a minute and 20 an hour
(`JORGEX_SOCIAL_LOGIN_LIMIT_PER_MINUTE`, `JORGEX_SOCIAL_LOGIN_LIMIT_PER_HOUR`); beyond that the
service answers 429 without checking anything. There is no account lockout, so nobody can keep you
out by failing on purpose from elsewhere. Password checks are expensive by design; at most two run at
once, and a sign-in that cannot get a turn within two seconds gets 503.

The audit log records each sign-in with the client's address (IPv6: its `/64`). Denied sign-ins older
than 90 days are deleted at the next successful sign-in, since anyone can make the service write
them; every other entry stays.

Three sign-ins with the right password and a wrong code within an hour, with no successful sign-in
between them, open the incident `sign_in_password_accepted`: **your password may have leaked**, and
only the second factor stopped whoever has it. The page they get is the same as for a wrong password,
so nothing tells them. If it was not you, run `set-password`. Changing the password or the second
factor is the only thing that closes it: signing in or acknowledging it does not, so after your own
mistyped codes you close it the same way. The audit log's denied `operator.login` entries show the
addresses, with `wrong_code` as their reason.

## Remote MCP

An agent connects to `https://social.example.com/mcp` with nothing but that URL: no token to copy,
no tunnel. It needs `JORGEX_SOCIAL_PUBLIC_URL` set (it is the OAuth issuer; without it neither `/mcp`
nor OAuth exist), the proxy above, and the operator with a password and a TOTP code (see
[Web interface](#web-interface)). With Claude Code:

```sh
claude mcp add --transport http jorgex-social https://social.example.com/mcp
```

or, in a `.mcp.json`:

```json
{
  "mcpServers": {
    "jorgex-social": { "type": "http", "url": "https://social.example.com/mcp" }
  }
}
```

The first time, the client opens your browser on this service: sign in, and a consent page shows
the name the client gives itself (unverified: anyone can claim any name), where the answer goes back
to (the client's own machine, `localhost` or `127.0.0.1` with a port) and the six groups, each with
write, read or deny. Approve only a connection you started yourself, and only the groups the agent
needs. The client then keeps and renews its tokens on its own (access tokens last an hour; the
renewal token thirty days and changes on every use).

Every tool of the local MCP server is there, and each call is checked against what you granted: a
denied group answers with a `forbidden` error. Files are not read from a path, which would be the
server's: the `create_upload_url` tool gives a one-use URL, valid ten minutes, and the agent sends
the file from its machine with `curl --data-binary @/path/to/photo.jpg '<url>'`. The URL is the only
credential; it takes one file, up to 300 MB. A grant gets at most 5 unused URLs at once and 20 an
hour.

Grants are listed and ended from the server:

```sh
docker compose exec api jorgex-social list-grants
docker compose exec api jorgex-social revoke-grant --id <id>   # or --all
```

Ending a grant ends its tokens and upload URLs; the client stays registered, so it can ask for your
consent again. Changing the password (`set-password`) or the second factor (`enroll-totp`) ends every
grant. If a client keeps failing to connect after that, clear what it stored and start again; with
Claude Code, `claude mcp logout jorgex-social` (the name you gave it in `claude mcp add`).

A grant lasts 90 days from your consent (`expires_at` in `list-grants`), however often its tokens are
refreshed. Past that its tokens and upload URLs stop working and the client asks for your consent
again.

Clients may only register redirect URIs on the loopback address, so an approved code always goes
back to the machine that asked. A client that needs an https redirect must be listed in
`JORGEX_SOCIAL_OAUTH_REDIRECT_URIS`. The OAuth endpoints are limited per client address: `/register`
to 10 a minute and 30 an hour, and `/authorize`, `/token`, `/revoke` and `/upload` to 30 a minute
and 300 an hour between the four of them, one budget they share. Clients nobody approved are
forgotten after an hour.

## Create your Meta app

The service uses the Instagram API with Instagram login (`graph.instagram.com`). Your app only serves
your own account, which has a role in the app. On the app this guide was tested with, **standard
access** was enough: no App Review, no advanced access and no business verification. That has not
been re-verified on a newly created app; see the note below.

Meta's dashboard changes often, so names and places below may differ slightly from what you see.

**What has been verified, and what has not.** These steps were followed on one existing app in
October 2026. On that app, Meta accepted publishing (Live) with standard access and no App Review;
once Live, the API returned comments and direct messages from accounts with no role in the app, and
public and private replies worked; the `comments` and `messages` webhooks arrived within seconds.
Creating a brand-new app from scratch, and publishing a new app under the same conditions, have
**not** been re-verified. If Meta asks a new app for App Review or business verification before it
can go Live, this guide does not cover it.

**Meta now labels "API setup with Instagram login" as legacy** and recommends the unified login
setup for new integrations. The Instagram login setup still works and is what this service uses; it
just will not get new features.

### 1. Prepare the Instagram account

Switch the account to a professional account (Business or Creator) in the Instagram app's settings.

### 2. Create the app

In the [Meta for Developers](https://developers.facebook.com/apps/) dashboard, create an app and
choose the use case **Manage messages and content on Instagram**. This adds the Instagram product
with the **API setup with Instagram login** page used below.

### 3. Add the permissions

Under the use case's **Permissions and features**, add these five permissions. Each one should show
standard access, ready for testing:

- `instagram_business_basic`
- `instagram_business_content_publish`
- `instagram_business_manage_comments`
- `instagram_business_manage_insights`
- `instagram_business_manage_messages`

### 4. Fill in the basic settings

In **App settings > Basic**, fill in the app domain (for example `social.example.com`), the privacy
policy URL, the terms of service URL, the category and a contact email. On the tested app, Meta
considered the required settings complete without a data deletion URL or an app icon.

The privacy policy is yours to write; this project does not provide one, and nothing here is legal
advice.

### 5. Add the account and generate the token

On **API setup with Instagram login**, in the step to generate access tokens, add your Instagram
account and generate a token for it. If the dashboard asks for it, add the account as an Instagram
tester under **App roles > Roles** first and accept the invitation from the account.

The token is long-lived (60 days). Copy it straight into the connect command in
[Switch to real mode](#switch-to-real-mode); do not save it in a file, a note or a chat. The service
renews it before it expires. If it ever expires, generate a new one here and connect again.

### 6. Publish the app

In the dashboard's **Publish** section, publish the app (Live mode). This matters: while the app is
in development, the API does not return comments or messages from accounts without a role in the
app (on the tested app, the comment count went up but the list came back empty), and webhooks need a
published app. You can return to development later with **Unpublish**.

You should not need advanced access or App Review: they are for apps that serve other people's
accounts, and this one serves only yours. If a new app behaves differently, please open an issue
(without secrets).

## Switch to real mode

1. Connect your account while the service is still in simulation. The command reads the long-lived
   Instagram access token from standard input, without echoing it, checks it against Instagram and
   stores it encrypted. There is no `--token` argument: it would stay in the shell history.

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

   To switch an installation to a different account later, add `--replace`.
2. In `.env`, set `JORGEX_SOCIAL_PUBLIC_URL=https://social.example.com` (your domain) and
   `JORGEX_SOCIAL_SIMULATION=false`.
3. Apply the change with `docker compose up -d`. `GET /v1/account` reports the connection, open
   incidents, the kill switch and the webhook.

In real mode the worker refuses to start without a connected account, so connect before switching.

## Webhook

Without it, automatic replies still work: the worker polls comments and messages every two minutes.
With it, Meta notifies each new comment and message as it happens and the reply goes out within
seconds (Meta's messaging policy documentation, at the time of writing, asks automated replies to
answer within 30 seconds). Polling keeps running and
catches anything a notification missed; a comment seen both ways is answered once.

The app must be published (Live) first.

1. Generate a random verify token, for example `openssl rand -hex 32`.
2. In `.env`, set `JORGEX_SOCIAL_WEBHOOK_VERIFY_TOKEN` to it and `JORGEX_SOCIAL_META_APP_SECRET` to
   the **Instagram app secret**: the one shown with the Instagram app ID on the
   **API setup with Instagram login** page. It is not the app secret under **App settings > Basic**;
   notifications signed with the Instagram app secret would fail verification against that one and
   get 403. Keep `.env` readable only by you (`chmod 600 .env`). Apply with `docker compose up -d`.
   Until both variables are set, `/webhook` answers 404.
3. Make `/webhook` public at the proxy (see [Reverse proxy](#reverse-proxy)).
4. On **API setup with Instagram login**, in the step to configure webhooks, enter the callback URL
   `https://social.example.com/webhook` and the verify token, and verify. Meta calls the URL and the
   service answers its challenge.
5. Subscribe to the `comments` and `messages` fields.
6. Subscribe your account to the app: turn on the webhook subscription for your account in the same
   step (the alternative is `POST /<IG_ID>/subscribed_apps` with
   `subscribed_fields=comments,messages`, described in Meta's "Set up Instagram Webhooks" guide).

Meta's guide says comment notifications need advanced access and a public account. On the tested
app, they arrived with standard access. If yours never arrive, polling still answers them, and the
incident `webhook_silent` opens when polling finds activity the webhook has not reported for a day.

Every notification must carry Meta's signature (`X-Hub-Signature-256`) made with the Instagram app
secret; anything else gets 403 and is not processed. Notification bodies hold other people's text
and are never logged or stored. `GET /v1/account` shows when the last valid notification arrived.

A direct message you send by hand from Instagram pauses automatic replies to that person for
`JORGEX_SOCIAL_HUMAN_PAUSE_MINUTES` (30), so a rule does not talk over you. Meta echoes messages sent
from the Instagram app as webhook notifications, which is what triggers the pause; without the
webhook, `POST /v1/conversations/{id}/pause` does the same by hand. A pause also holds back replies
to that person's comments, but only when Instagram uses the same id for a comment's author and a
message's sender, which has not been verified.

## Protecting the account

Too many automatic replies can get an Instagram account restricted, so they are held back on purpose:

- A rule answers one person once per post, however many times they repeat the keyword. A later
  comment on another post, or one matching another rule, is answered.
- At most `JORGEX_SOCIAL_REPLIES_PER_MINUTE` (12) replies go out in any minute. The rest stay pending
  and the worker continues on later passes, oldest first.
- At most `JORGEX_SOCIAL_HOURLY_REPLY_CAP` (200) replies go out in any hour, and at most
  `JORGEX_SOCIAL_HOURLY_PUBLIC_REPLY_CAP` (30) of them public. Public replies are reported to be the
  riskier kind, on community evidence only. A reply over a cap is skipped for good; when only the
  public cap is reached, the private reply to the same comment still goes out.
- A public reply not sent within 24 hours of the comment is skipped.
- A direct message that is exactly one of `JORGEX_SOCIAL_OPT_OUT_WORDS` (`stop`, `unsubscribe`,
  `baja`) silences that person for good, as Meta's Developer Policies (5.2b) ask to respect opt-outs
  immediately. Case, accents and punctuation or symbols at either end are ignored, so "Stop!" counts,
  but the rest of the message must match exactly, so "stop it" and "please stop by the store" do not.
  Nothing is sent back, not even a confirmation, and no rule fires. Replies already pending end as `paused`; those to the
  person's comments only if Instagram uses the same id for a comment's author and a message's sender,
  which has not been verified. The person shows as `paused_forever` in `GET /v1/conversations`, with
  `conversation.opted_out` in the audit log. Only `POST /v1/conversations/{id}/resume` undoes it: a
  pause with `until` is refused. An empty value turns this off.
- A rule that replies publicly needs at least 3 public variants that differ in more than case, accents
  or spacing; anything less is refused with a 422. This is this service's own floor, not a Meta rule:
  Meta publishes no number, and automation tools and their users advise 3 to 5. Private replies have
  no minimum. A rule stored earlier with fewer keeps answering, but any edit to it is refused until
  its content is fixed.
- Rules get `warnings` in the API, the MCP and the web interface: public replies on every post, a link
  in a comment's first private message, or a variant with no letter or digit. These are reported by
  automation users, not published by Meta, so a rule with warnings can still be activated.
- At most 50 posts are published in any 24 hours, the lower of the two figures Meta's page gives. Over
  that, the next due post fails as `daily_limit` without calling Instagram, and can be scheduled again.
  Only posts published by this service count; Meta's own count, with the ones you publish by hand, is
  the real limit.

If Instagram reports the account as restricted (error code 368, or code and subcode 10/1893063,
100/2534029, 613/2018338, 4/2207051, 9/2018352 or 25/2207050), the service stops on its own: the kill
switch turns on and the incident
`account_restricted` opens with the code and subcode. Scheduled posts wait and replies due meanwhile
are skipped for good. Check the account in Instagram, deal with the cause, and only then turn the
switch off with `PUT /v1/account/kill-switch`, which closes the incident. If Instagram refuses again,
both come back.

What the service cannot do for you:

- After a restriction the API can keep answering while the account is limited, so check **Account
  Status** in the Instagram app rather than trusting that calls succeed. Likewise, a delivery marked
  `sent` means Instagram accepted the reply, not that it reached the person.
- Do not write captions or replies that require following, liking or commenting to receive promised
  content: Meta's spam policy lists engagement gating.
- An opt-out written during a full outage of over 24 hours is not seen, because messages that old are
  not read. Nobody is answered from that time either, but the person stays unpaused, so a later
  message from them can fire a rule.
- After an outage of over an hour, check the webhook subscription in Meta's dashboard. Meta's pages
  disagree on whether it unsubscribes the account: one says it may after an hour of failures, another
  talks about 36 hours of retries.
- Automated replies must say they are automated where the law requires it. That is yours to write into
  the variants and to check for your country.
- Meta's Messenger policy asks bots to answer within 30 seconds. Its scope on Instagram is not stated.
  Polling can take up to `JORGEX_SOCIAL_REPLY_POLL_SECONDS` (120), so the webhook is what meets it.

## Backups

Back up the database and the secret key, and keep them apart: the key decrypts the Instagram
credential stored in the database, so one without the other is useless to a thief, and the database
is useless to you without its key. Keep both outside the clone, so neither can end up in a commit or
in an image.

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

## Upgrade

Back up the database first, since migrations run on start. Then:

```sh
git pull
docker compose up -d --build
```

The first upgrade to the version that publishes `/ui` (with a fixed subnet in `compose.yaml`) needs
the network recreated, which `up` alone does not do:

```sh
docker compose down && docker compose up -d --build
```

Then run the real-address check in [Reverse proxy](#reverse-proxy) before relying on the sign-in
limits.

Compose stops the old worker before it replaces `api`, so the new version migrates with no worker
running, and starts the new worker once `api` is healthy again. On stop, the worker finishes the
publication in progress before exiting, so an upgrade or `docker compose down` can take up to
15 minutes while one is running.

To roll back, check out the previous version and run the same command; if its migrations are
incompatible with the newer database, restore the backup as well. You are responsible for keeping
your installation updated.
