# Testing

Automated tests are deterministic and use synthetic data, injected transports,
and mock providers. They must never use live credentials, make paid calls, or
create a real social-media post.

## Local checks

```bash
uv sync --locked --dev
uv run pytest
uv run ruff check .
uv run mypy src
git diff --check
```

The suite covers authorization, workflow transitions, immutable approvals,
budget reservations, provider uncertainty, Telegram delivery, media validation,
OAuth state, encrypted credentials, LinkedIn upload/publication boundaries,
backup and restore, and restart behavior.

## Configuration check

`uv run praeco --check` validates configuration and applies migrations without
contacting Telegram, OpenAI, or LinkedIn. Use an isolated database and media
directory for clean-install verification.

## Manual provider tests

Live provider smoke tests are exceptional. They require the owner's explicit
authorization, a bounded call count, spending limits, redacted output, and a
recorded result. A LinkedIn test publication is never automated: it requires the
owner to approve the exact snapshot and press its final button.

## CI contract

CI receives no production secrets. It installs the locked dependency graph and
runs tests, Ruff, and strict Mypy on supported Python versions. Workflows must not
use `pull_request_target`, production endpoints, real account IDs, or live
provider credentials.
