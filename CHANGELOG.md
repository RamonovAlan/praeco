# Changelog

All notable changes to Praeco will be documented in this file. The project uses
semantic versioning for public release candidates.

## [Unreleased]

No changes yet.

## [0.1.0] - 2026-10-15

### Added

- Owner-only Telegram workflow for text and voice thoughts.
- Immutable text and image review with explicit version-bound approval.
- OpenAI-backed text, transcription, and image generation with measured costs.
- Official LinkedIn OAuth, image upload, and protected final publication.
- Durable SQLite jobs, Telegram delivery outbox, and duplicate protection.
- Hardened container deployment, health checks, encrypted backups, and restore
  verification.
- Discoverable Russian Telegram command menu, owner preference memory, monthly
  cost summary, credential-free export, and confirmed local-data deletion.

### Security

- Publishing is disabled by default and requires the owner to press the final
  button for the exact approved snapshot.
- Credentials, runtime content, original media, and backups remain outside Git.

The source is distributed under Apache-2.0.

