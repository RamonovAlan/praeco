# Setup

Praeco is a single-owner Python service. Start in mock provider mode and keep
publishing disabled until the local checks pass.

## Requirements

- Python 3.12 or 3.13;
- `uv` 0.12.1 or a compatible release;
- a Telegram bot token and the numeric Telegram user ID of the owner; and
- Docker only for the containerized deployment.

## Local installation

```bash
git clone https://github.com/RamonovAlan/praeco.git
cd praeco
cp .env.example .env
chmod 600 .env
uv sync --locked --dev
```

Fill only the owner ID and Telegram token first. Keep these values unchanged:

```dotenv
PRAECO_PROVIDER_MODE=mock
PRAECO_PUBLISHING_ENABLED=false
```

Migration, prompt, and style paths are optional. A source checkout uses its
tracked project files; an installed wheel uses the same files bundled inside the
package. Set an explicit path only when deliberately overriding one of them.

Validate configuration and migrations without contacting Telegram, OpenAI, or
LinkedIn:

```bash
uv run praeco --check
```

Then run the automated checks:

```bash
uv run pytest
uv run ruff check .
uv run mypy src
```

Starting `uv run praeco` begins Telegram long polling. Mock provider mode still
uses the configured real Telegram bot, but it does not call OpenAI or LinkedIn.

## Real AI mode

Real text, transcription, and image calls require an OpenAI API key and:

```dotenv
PRAECO_PROVIDER_MODE=openai
```

Keep the spending limits in `.env`. They are hard safety ceilings, not usage
targets. Praeco makes one requested output and no automatic paid retry by
default.

## LinkedIn publishing

Follow [LinkedIn setup](linkedin-setup.md). Publishing requires a registered
application, exact redirect URI, required products and scopes, an encryption key,
an owner OAuth connection, and a deliberate `PRAECO_PUBLISHING_ENABLED=true`.
Installing credentials alone cannot publish a post. The owner must still approve
the exact text and image and press the final button for that snapshot.

## Containers

Use [deployment](deployment.md) for the hardened Compose and Railway contract.
Never copy `.env`, databases, media, or backups into the image.

## Clean removal

Stopping the process does not delete data. Before deleting a volume, database,
media directory, or backup, create and verify a recoverable backup according to
[local backup and restore](backup.md).
