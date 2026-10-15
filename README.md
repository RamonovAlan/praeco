<p align="center">
  <img src="docs/assets/brand/praeco-avatar.png" width="144" alt="Praeco announcement-shaped P brand mark">
</p>

# Praeco

**A self-hosted, Telegram-first AI workflow for drafting, reviewing, and publishing LinkedIn posts.**

Praeco turns a text or voice thought into a short post and an original visual. The
owner reviews every revision, approves the exact text and image versions, and is
the only person who can trigger publication. AI prepares the material; it never
receives permission to publish by itself.

> Public source release. The owner-operated production deployment, credentials,
> data, backups, and operational history remain private.

[Русская версия](README.ru.md)

## Why Praeco

A useful professional observation often disappears between a voice note and a
finished post. Generic content tools solve that by taking over the voice. Praeco
uses a different boundary: automate the repetitive work, preserve the author's
judgment, and make every external action explicit.

## Workflow

```text
text or voice thought
        ↓
AI draft → owner revision loop → exact text approval
        ↓
image brief → image revision loop → exact image approval
        ↓
final text + image + measured preparation cost
        ↓
owner-only “Publish to LinkedIn” button
        ↓
official LinkedIn API acknowledgement
```

The Telegram cost and connection footer is operational metadata. It is never part
of the LinkedIn post body.

## Implemented

- One allowlisted owner and one private Telegram chat.
- Text and bounded voice input with transcription.
- Immutable text and image versions with stale-button rejection.
- AI revisions, manual text editing, direct `/text <post>` import, and
  owner-supplied PNG/JPEG images before or after image generation.
- Semantic image concept resets that stop reusing a rejected composition when
  the owner explicitly asks for a different concept.
- Original image storage with checksums; Telegram receives only a preview.
- Exact final snapshot bound to the approved text and image versions.
- Official LinkedIn OIDC, Images API, and Posts API integration.
- Duplicate protection and a non-retryable `PUBLISH_UNKNOWN` boundary.
- Per-draft usage accounting and configurable per-post/monthly spending limits.
- A Telegram command menu plus owner commands for recent drafts, settings,
  monthly cost, explicit versioned text/image preferences, credential-free data
  export, and confirmed local data deletion.
- SQLite-backed jobs and delivery outbox that survive process restarts.
- Hardened single-container deployment, health checks, encrypted backups, and
  restore verification.
- Deterministic mock mode for tests without credentials, paid calls, or posts.

The owner has completed three consecutive daily private-beta publication cycles.
This validates the exercised owner workflow; it is not a claim about audience
growth or multi-user production readiness.

## Deliberate boundaries

- Single owner, not a multi-user SaaS.
- Russian is the current default content language; it is configurable.
- Publishing is disabled by default and requires manual LinkedIn app setup.
- No browser automation, scraping, password access, cookies, scheduled posting,
  automatic likes/comments, or invented analytics.
- LinkedIn write access does not guarantee post readback or analytics access.
- No second social network is implemented in the current release candidate.

## Extensible publishing

The data model already records platform and account per publication. The intended
next step is a narrow `Publisher` interface so additional official adapters can be
implemented without weakening the approval boundary.

Candidate integrations include Telegram Channels, Instagram Professional, and
Facebook Pages. They are roadmap items, not current features. If you want to help
design or sponsor an adapter, contact [alanramon71@gmail.com](mailto:alanramon71@gmail.com).

## Architecture

Praeco is intentionally one Python service:

- Telegram polling and owner authorization;
- explicit domain workflow and immutable approvals;
- SQLite migrations, durable jobs, publications, and delivery outbox;
- text, speech, image, and publishing adapters behind narrow boundaries;
- a small HTTPS surface for LinkedIn OAuth and health checks; and
- original media on persistent storage.

External provider calls happen outside database transactions. A result is stored
before Telegram delivery is attempted, so a delivery failure cannot repeat a paid
generation. A publication request that may have reached LinkedIn is never retried
automatically.

See [architecture](docs/architecture.md) and [decisions](docs/decisions.md).

## Quick start

Requirements:

- Python 3.12 or 3.13;
- [`uv`](https://docs.astral.sh/uv/);
- a Telegram bot token and the numeric Telegram ID of its owner; and
- Docker only when using the containerized deployment.

Install and verify with deterministic providers:

```bash
git clone https://github.com/RamonovAlan/praeco.git
cd praeco
cp .env.example .env
uv sync --locked --dev
uv run praeco --check
uv run pytest
uv run ruff check .
uv run mypy src
```

Set `PRAECO_OWNER_TELEGRAM_USER_ID` and `PRAECO_TELEGRAM_BOT_TOKEN` in the
ignored `.env`, then start mock mode:

```bash
uv run praeco
```

Mock mode does not contact OpenAI or LinkedIn. OpenAI mode additionally needs an
API key. LinkedIn publishing additionally needs an application, exact redirect
URI, encryption key, required products/scopes, owner OAuth connection, and
`PRAECO_PUBLISHING_ENABLED=true`. Follow the dedicated setup documentation rather
than enabling publishing by trial and error.

Detailed instructions: [setup](docs/setup.md), [testing](docs/testing.md), and
[deployment](docs/deployment.md).

## Verification

The current suite exercises owner authorization, state transitions, immutable
versions, stale callbacks, budget reservations, external-call uncertainty,
Telegram delivery, media validation, OAuth state, encrypted credentials,
LinkedIn media upload, publication deduplication, backup, and restart behavior.

CI must use mock providers. Real paid calls and social-media publication are never
part of automated tests.

## Costs

Runtime cost depends on voice duration, token usage, image generations/revisions,
hosting, and backups. Praeco records measured provider usage and uses configurable
ceilings as safety limits, not spending targets. See the dated assumptions and
formulas in [costs](docs/costs.md).

## Security and privacy

- Credentials, OAuth tokens, runtime data, drafts, transcripts, original media,
  and backups stay outside Git.
- Only the configured numeric Telegram owner can issue commands or callbacks.
- OAuth credentials are encrypted at rest and decrypted only at the call boundary.
- Logs and errors exclude private content, credentials, provider bodies, and
  token-bearing URLs.
- A public source release must remain separate from production backup automation
  and operational artifacts.

See the [security model](docs/security.md), [privacy boundary](docs/privacy.md),
and [responsible disclosure policy](SECURITY.md).

Report vulnerabilities privately according to `SECURITY.md` once the public
security contact is activated. Do not open a public Issue containing a secret or
private content.

## Roadmap

See [ROADMAP.md](ROADMAP.md). The current public patch release is `v0.1.1`.
Additional official Publisher
adapters come only after the LinkedIn-first baseline is stable.

## Contact and support

- Email: [alanramon71@gmail.com](mailto:alanramon71@gmail.com)
- LinkedIn: [Alan Ramonov](https://www.linkedin.com/in/alanramonov)
- Voluntary support: [SUPPORT.md](SUPPORT.md)

Support helps fund continued work on practical tools that make complex workflows
simpler. It never buys roadmap priority, private data access, or influence over
security decisions.

## License

Praeco is licensed under the [Apache License 2.0](LICENSE). The license permits
commercial use, modification, and redistribution subject to its notice and other
conditions. See [NOTICE](NOTICE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

The dependency inventory is available in [SBOM.md](SBOM.md) and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
