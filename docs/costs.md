# Preliminary cost model

Checked: 2026-10-12

This file is a planning model, not a quote. The owner enabled the `openai` runtime mode and accepted the numeric limits below. Prices can change and must be re-checked periodically.

## Measured minimal smoke baseline

One authorized call per capability was completed on 2026-10-12 with no automatic retries:

| Stage | Measured usage | Calculated cost |
| --- | ---: | ---: |
| `gpt-6-luna` text | 871 input + 781 output tokens | USD 0.000478 |
| `gpt-transcribe` | 2.785938 seconds | USD 0.000209 |
| `gpt-image-2.5-flare-2026-09-08`, low, 1536x1024 | 255 input + 158 output tokens | USD 0.006015 |
| Total | one call per capability | USD 0.006702 |

This is a smoke sample, not a guaranteed per-post price. Longer source material, longer voice, image edits, larger outputs, failed-billed requests, and price changes can increase it.

## Cost formula

For each draft, record actual provider usage separately from monetary estimates:

```text
text_cost = input_tokens * input_rate + output_tokens * output_rate
transcription_cost = audio_minutes * per_minute_rate
image_cost = text_input_tokens * text_input_rate
           + image_input_tokens * image_input_rate
           + image_output_tokens * image_output_rate
post_cost = sum(all successful, failed-billed, and unknown-billed calls for the draft)
```

Rates are normalized to their billing units and stored with currency, source URL, and effective date. Timeouts are not assumed to be free. Queued jobs reserve an upper-bound estimate before execution.

## Working monthly scenarios

All figures exclude taxes and foreign-exchange costs. The first scenario projects the measured smoke sample. The second adds one measured-rate text revision and one new image. Actual voice length, prompt length, image-edit inputs, and output tokens vary; owner-requested revisions remain subject to the same caps.

| Scenario | 20 posts/month | 60 posts/month |
| --- | ---: | ---: |
| Smoke-shaped economy: one text, one very short voice, one low-quality image | about USD 0.13 | about USD 0.40 |
| One text revision and one new image revision at the measured smoke rates | about USD 0.26 | about USD 0.79 |

At the requested cadence of roughly 30 posts per month, the measured smoke-shaped baseline is about USD 0.20. One measured-rate text revision plus one new image per post raises that to about USD 0.40. A practical planning reserve should be higher because real prompts and edits vary; the accepted USD 25 monthly hard stop is a safety ceiling, not a target or forecast.

## Accepted risk limits and economy behavior

Image edits can add billed image-input tokens, timeouts may still be billed, prices can change, and a provider may return usage later than the request. Praeco must reserve an upper bound before a call and count successful, failed-billed, and unknown-billed attempts against the same draft.

Owner-approved ceilings:

- warning at USD 0.50 accumulated AI cost for one post;
- hard stop at USD 0.75 for one post;
- hard stop at USD 25.00 for one calendar month;
- no automatic budget increase and no paid retry after the hard stop;
- require owner confirmation for a higher-cost fallback model or extra image attempt.

Economy is the default even when there is room under a limit:

- one text draft per request and one image only after text approval;
- the cheapest suitable text path first;
- no speculative variants, separate model-based quality grading, or automatic paid retries;
- cache by immutable input/version identity so duplicate callbacks and restarts do not pay twice;
- an additional image or higher-cost fallback requires an explicit owner action and a fresh budget check.

The per-post cap is deliberately higher than the expected average. It allows image input tokens, occasional owner-approved fallback, and provider uncertainty without silently turning one difficult draft into an open-ended loop. The implementation must reserve the upper-bound estimate before queueing a paid job and include pending reservations in both per-post and monthly checks.

## Hosting envelope

- Predictable VPS candidate: Hetzner CAX11, EUR 5.99/month excluding VAT and optional IPv4, before external backup storage.
- Managed deployment candidate: Railway Hobby, USD 5/month minimum; always-on RAM/CPU usage can raise the total.
- Metered machine candidate: Fly.io 1 GB shared machine about USD 6.70/month in the lowest-priced US regions, plus USD 0.15/GB-month volume and any other resources.

The owner must select and authorize a priced hosting option. No paid infrastructure is provisioned automatically.

With the predictable VPS candidate, backup allowance, and normal one-image AI usage, a practical expected total is roughly USD 12–20 equivalent per month. USD 25 is only the AI hard stop, not a target. A conservative infrastructure-plus-AI envelope can be higher if the owner explicitly requests many image revisions; no such increase is authorized automatically.

## Price references used

- [OpenAI API pricing](https://developers.openai.com/api/docs/pricing), accessed 2026-10-12: text and transcription rates.
- [OpenAI image-generation guide](https://developers.openai.com/api/docs/guides/tools-image-generation), accessed 2026-10-12: GPT Image models, sizes, quality controls, and token-based charges.
- Hosting provider pricing references are recorded in `docs/feasibility.md`.
