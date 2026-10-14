# Contributing

Praeco is preparing for its first public-source release. Small, focused changes
with tests and clear safety reasoning are welcome after the repository opens.

## Before a change

1. Open an Issue or contact the maintainer for non-trivial features.
2. Do not include private content, credentials, account identifiers, or provider
   response bodies.
3. Preserve exact-version approval and the owner-only final publication boundary.
4. Use official platform APIs; browser automation, scraping, engagement spam,
   and unattended posting are out of scope.

## Development

```bash
uv sync --locked --dev
uv run pytest
uv run ruff check .
uv run mypy src
```

Tests must use synthetic fixtures and injected transports. They must not make
paid calls or publish real posts. Keep commits and code documentation in English.

By submitting a contribution, you confirm that you have the right to provide it
under Apache-2.0 and agree that it may be distributed under that license. Do not
copy code from sources with incompatible terms.
