# Praeco v0.1.0

Released: 2026-10-15.

Praeco is a self-hosted, Telegram-first AI workflow for turning a private text or
voice thought into a reviewed LinkedIn post with an original visual. Every text,
image, and publication action remains under the owner's explicit control.

## Highlights

- Text and bounded voice input with transcription.
- Immutable text and image versions, AI revisions, manual text edits, and owner
  image uploads.
- Exact-version approval with stale-button and duplicate protection.
- Official LinkedIn OIDC, Images API, and Posts API integration.
- A final owner-only publication button; automated tests and retries cannot
  publish a post.
- Russian Telegram command menu for drafts, settings, style rules, current-month
  costs, data export, LinkedIn connection, and deliberate local-data deletion.
- Durable SQLite jobs and Telegram delivery that survive process restarts.
- Per-post and monthly spending limits with measured provider usage.
- Hardened container deployment, health checks, encrypted backups, and restore
  verification.

## Safety and privacy

Publishing is disabled by default. Credentials, OAuth tokens, drafts,
transcripts, original media, production backups, and owner runtime data are not
part of the public repository. An ambiguous provider or LinkedIn result is not
retried automatically.

## Scope

This release supports one allowlisted owner, one private Telegram chat, and one
LinkedIn member account. It is not a multi-user SaaS and does not implement
scheduled posting, browser automation, scraping, engagement automation, audience
analytics, or additional social networks.

## Installation

Use the mock-mode quick start in `README.md` and the full instructions in
`docs/setup.md`. Real OpenAI or LinkedIn use requires the owner's own provider
credentials and explicit configuration.

## License

Praeco is distributed under Apache-2.0. See `LICENSE`, `NOTICE`, and
`THIRD_PARTY_NOTICES.md`.
