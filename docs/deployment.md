# Deployment

Praeco runs as one long-lived Python process with Telegram polling, durable work,
and a small HTTP listener for health checks and LinkedIn OAuth. A deployment
needs persistent storage and exactly one active application replica.

## Container contract

- The image runs as numeric UID/GID `10001:10001`.
- The root filesystem should remain read-only with Linux capabilities dropped
  and `no-new-privileges` enabled.
- Persist `/var/lib/praeco`; it contains SQLite state and original media.
- Keep temporary storage bounded and do not copy `.env`, databases, media,
  backups, tests, or Git history into the image.
- Route the provider's readiness probe to `/health/ready` on the configured
  application port.
- Run one replica unless the storage and worker model are deliberately changed.

## Required configuration

Copy `.env.example` to an ignored secret file or install the same values in the
hosting provider's secret store. Begin with mock providers and publishing
disabled. Real OpenAI and LinkedIn settings should be enabled only after the
local configuration check succeeds.

If LinkedIn OAuth is enabled, expose the callback through HTTPS and make its URL
match the registered redirect URI exactly. Do not expose databases, media, log
files, or backup storage through the web server.

## Local validation

Validate Compose without rendering interpolated secrets:

```bash
docker compose config --no-interpolate --quiet
docker compose build
docker compose run --rm --no-deps praeco --check
```

The final command applies migrations in the persistent volume and exits without
contacting Telegram or an AI provider.

## Start and inspect

Starting the service begins Telegram long polling with the configured bot token:

```bash
docker compose up -d
docker compose ps
docker compose logs --tail 100 praeco
```

The expected container state is `healthy`. Stop without deleting persistent
state:

```bash
docker compose down
```

Do not add `--volumes` unless permanent deletion is intended and a verified,
recoverable backup exists.

## Update boundary

Before each update, create and verify a backup, build the new image, run
`--check`, and only then replace the service. Schema rollback is not assumed to
be safe. Keep publishing disabled until the owner has approved the exact text
and image snapshot through the product's final publication control.

Hosting-specific automation, production endpoints, credentials, and backup
schedules belong in a separate private operations repository.
