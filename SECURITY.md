# Security policy

## Supported versions

Only the `main` branch is supported. There are no maintained releases or backports: fixes land on
`main`, and you are responsible for updating your installation (see [INSTALL.md](INSTALL.md#upgrade)).

## Reporting a vulnerability

Report it privately through GitHub's private vulnerability reporting: open the repository's
**Security** tab and choose **Report a vulnerability**. Do not open a public issue, pull request or
discussion for a vulnerability.

Include what an attacker can do, the affected version or commit, and the steps or a minimal proof of
concept to reproduce it against an installation in simulation mode.

## What not to include

- Tokens, app secrets, verify tokens, passwords, TOTP secrets or the contents of `secret.key`,
  yours or anyone else's. If one has been exposed, revoke it first.
- Screenshots or copies of conversations, comments or messages.
- Instagram usernames, user ids or any other identifier of people other than you.

Simulation mode uses synthetic data, so a report never needs real data.

## What to expect

This is a personal project maintained on a best-effort basis. There is no bug bounty and no
guaranteed response time. Valid reports are acknowledged and fixed when possible, and credited in
the fix if you want.
