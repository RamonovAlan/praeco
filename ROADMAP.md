# Roadmap

This roadmap describes intent, not a delivery promise. Security and the owner's
explicit approval remain higher priority than feature count.

## Private beta and release candidate

- Complete 5–7 days of owner feedback-driven use.
- Verify restart behavior after successful publication without creating duplicates.
- Finish the required owner data-management and preference-memory controls.
- Validate clean installation with credentials belonging to a second tester.
- Separate production backup automation from the future public source boundary.
- Complete the security/history audit, dependency notices, SBOM, and public
  release checklist.
- Apply Apache-2.0 and prepare a separate clean-history public repository.

## `v0.1.0`

- Publish reviewed workflow documentation and original brand visuals.
- Provide reproducible mock-mode setup and tested self-hosting instructions.
- Publish the verified LinkedIn-first workflow and its real API limitations.
- Add useful Issue forms, contribution guidance, security reporting, and CI.

## Publisher adapters

The intended extension point is a narrow `Publisher` interface with platform
capabilities, validation, media upload, publication, and readback when officially
available. Each destination must retain exact-snapshot approval, independent
status, deduplication, and non-retryable ambiguous outcomes.

Potential adapters:

1. **Telegram Channels** — the lowest-complexity next adapter when the bot is a
   channel administrator.
2. **Instagram Professional** — an official Meta integration for eligible
   professional accounts, subject to permissions and review.
3. **Facebook Pages** — official Page publication, subject to Meta permissions and
   review. Personal-profile automation is not promised.

These adapters are not implemented in the current release candidate. To discuss
an integration or collaboration, email
[alanramon71@gmail.com](mailto:alanramon71@gmail.com).

## Later, only with evidence

- Owner-approved learning from recurring edits.
- Additional content languages and platform-specific formatting.
- Lightweight manual performance input where official analytics are unavailable.
- Multiple owners or teams only after the single-owner security model is
  deliberately redesigned.

Praeco will not add browser automation, scraping, automatic likes/comments,
engagement spam, or unattended social posting.
