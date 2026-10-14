# Security Model

Praeco is designed for one owner and one private Telegram chat. It is not a
general multi-user authorization system.

## Trust boundaries

- The numeric Telegram owner ID and private-chat check guard commands, replies,
  and callbacks.
- Text and image approvals bind to immutable version IDs. A stale callback cannot
  approve a newer version.
- The final publication action binds one exact approved snapshot. Tests, mocks,
  and background retries cannot cross this boundary.
- Provider and Telegram calls occur outside database transactions. Their results
  or ambiguous outcomes are persisted before further work is attempted.
- An ambiguous external create becomes `PUBLISH_UNKNOWN` and is never retried
  automatically.

## Secrets and stored data

- `.env`, OAuth credentials, API keys, databases, media, transcripts, exports,
  and backups are excluded from Git.
- LinkedIn credentials are encrypted at rest with account-bound AES-256-GCM.
- Backups use AES-256-GCM, an authenticated manifest, and an external checksum.
- Application logs and errors must not include private content, credentials,
  provider response bodies, authorization headers, or token-bearing URLs.

## Deployment controls

The container uses a numeric non-root user, read-only root filesystem, dropped
capabilities, `no-new-privileges`, bounded temporary storage, a persistent state
volume, and a minimal readiness endpoint. Production secrets belong in the
hosting provider's secret store.

## Known limits

- A compromised owner Telegram account or bot token can control the bot.
- SQLite and local media assume a trusted single-host storage boundary.
- The current production backup workflow is private operations code and must not
  be copied into a public source release.
- GitHub publication requires a separate history, secret, artifact, dependency,
  and metadata review.

See [SECURITY.md](../SECURITY.md) for responsible disclosure.
