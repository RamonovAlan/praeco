# Feasibility report

Checked: 2026-10-12 (Asia/Yerevan)

This report separates facts confirmed by current primary documentation from access confirmed for the owner's accounts. No credentials were used and no real API call or publication was attempted.

## Status summary

| Area | Documentation confirms | Owner account confirms | Current status |
| --- | --- | --- | --- |
| LinkedIn personal publishing | `w_member_social` can create posts for an authenticated member | Normal profile exists; no developer app | Feasible on paper; Page/app setup and real access test required |
| LinkedIn image upload | Images API supports an upload flow and image URNs | No developer app | Feasible on paper; write-only/read restrictions must be handled |
| LinkedIn readback | Posts API supports reads, but member reads require restricted `r_member_social` | No | Do not promise automatic readback or analytics |
| Telegram voice and media | Bot API exposes voice metadata and file download/send methods | Existing `@Preaco_bot` token authenticated; sole private `/start` sender installed locally | Credentials and owner identity confirmed; owner-side username correction to `@Praeco_bot` remains, subject to availability |
| OpenAI text/audio/image APIs | Official APIs and current pricing exist | Installed key authenticated; model list includes all three capability groups | Read-only access confirmed; paid inference smoke tests remain disabled |
| Hosting | Several always-on persistent options exist | No | Await owner choice before any paid provisioning |

## LinkedIn

### Documented official path

1. A developer app is required. LinkedIn requires individual-developer API products to be associated with a default LinkedIn Page. The Page super admin approves the association, has 30 days to act, and the association cannot later be transferred to another Page.
2. `Sign in with LinkedIn using OpenID Connect` exposes `profile` and `email`; `Share on LinkedIn` exposes `w_member_social`. LinkedIn currently lists these as self-service open permissions.
3. `w_member_social` authorizes creating posts, comments, and likes on behalf of the authenticated member. It is distinct from organization publishing.
4. The current implementation target is the versioned Posts API, not a new `ugcPosts` integration. LinkedIn explicitly says that Posts API replaces `ugcPosts` and requires both `Linkedin-Version: YYYYMM` and `X-Restli-Protocol-Version: 2.0.0`.
5. For a single-image post, initialize an image upload, upload the original image to the returned URL, retain the returned image URN, and reference that URN in `POST /rest/posts`.
6. A successful create returns HTTP 201 and a Post ID in `x-restli-id`. This is API acknowledgement, not proof that Praeco read the rendered post back.

### Personal-profile author identifier

The self-service Share on LinkedIn guide explicitly points to OpenID Connect for the Person URN. The OIDC `profile` scope returns the authenticated member's opaque `sub` identifier, and LinkedIn documents that OIDC removes the need for another profile lookup. Therefore Praeco constructs the personal author as `urn:li:person:{sub}`. It does not call the restricted legacy `/v2/me` Profile API or treat a public profile URL as an API identifier.

### Readback limitation

`r_member_social` is restricted and available only to approved users. `w_member_social` is write-only for versioned Images API reads. Therefore the MVP must expose publisher capabilities explicitly:

- `publish`: expected to be available after the self-service product is actually enabled and tested.
- `get_status` or content readback: unavailable unless the app is separately granted the required read permission.
- Success wording without read access: “LinkedIn confirmed creation and returned an ID; verify the rendered post using the link.”

### API version decision

Use a configurable `LINKEDIN_API_VERSION`, not a hard-coded historical example. The latest active version visible in the official migration table on the check date is `202609`, with a listed sunset date of 2027-09-15. Re-check the migration table before the real adapter is enabled and at least quarterly thereafter. Do not use `202510`; its listed sunset is 2026-10-15.

### OAuth implications

Use the three-legged Authorization Code flow with an exact redirect URI and a server-verified, single-use `state`. Current documentation describes access tokens as 60-day tokens. Programmatic refresh tokens are limited to approved Marketing Developer Platform partners, so the baseline must support owner reauthorization instead of assuming a refresh token exists.

### Required owner action

Do not paste secrets into chat or commit them.

1. Confirm whether a LinkedIn developer app already exists.
2. If not, create or select the LinkedIn Page that should permanently publish the app identity, then create the app in the Developer Portal and complete Page-super-admin verification.
3. In the app's Products tab, enable `Sign in with LinkedIn using OpenID Connect` and request `Share on LinkedIn`.
4. In the Auth tab, add the exact HTTPS callback URL that Praeco will use. A temporary value should not be invented before the hosting/domain decision.
5. Put the client ID and secret into the server secret store or local `.env`, never into Git or a chat message.

### Primary sources

- [Getting Access to LinkedIn APIs](https://learn.microsoft.com/en-us/linkedin/shared/authentication/getting-access)
- [Associate an app with a LinkedIn Page](https://www.linkedin.com/help/linkedin/answer/a549354)
- [Authorization Code Flow](https://learn.microsoft.com/en-us/linkedin/shared/authentication/authorization-code-flow)
- [Sign In with LinkedIn using OpenID Connect](https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/sign-in-with-linkedin-v2)
- [Share on LinkedIn](https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/share-on-linkedin)
- [Posts API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api)
- [Images API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/images-api)
- [Marketing API migrations](https://learn.microsoft.com/en-us/linkedin/marketing/integrations/migrations)

## Telegram

### Documented limits and compatibility

- A `Voice` message supplies duration and optional MIME type and file size. Telegram voice notes are normally OGG/Opus.
- The hosted Bot API currently limits `getFile` downloads to 20 MB. Praeco should reject an oversized input before enqueueing transcription and explain the limit rather than silently truncating it.
- The official OpenAI transcription endpoint accepts OGG, so normal Telegram voice notes do not require format conversion for that provider.
- `sendPhoto` accepts photos up to 10 MB, with width plus height no more than 10,000 and aspect ratio no more than 20.
- `sendDocument` currently accepts files up to 50 MB. Praeco can send a compressed preview as a photo and, when requested, return the preserved original as a document.
- The original server-side asset remains the LinkedIn upload source; a Telegram-compressed preview must never replace it.

### Access state

The owner installed the existing `@Preaco_bot` token locally, and `getMe` authenticated successfully on 2026-10-12. After the owner sent `/start`, the redacted checker found exactly one private sender and installed its numeric ID in the ignored local `.env` without printing it. The bot token and numeric owner boundary remain valid after the product rename. The owner should change the public bot name and, if available, username to `Praeco`/`@Praeco_bot` in BotFather.

### Primary sources

- [Telegram Bot API](https://core.telegram.org/bots/api)
- [Telegram bot features and hosted/local file limits](https://core.telegram.org/bots/features)

## AI provider

### Preliminary comparison

| Approach | Advantages | Costs and risks | Decision |
| --- | --- | --- | --- |
| One OpenAI API project for text, transcription, and images | Fewest adapters and processors; OGG transcription is supported; image generation and iterative editing are documented | Model access is unverified; provider concentration; image organization verification may be required | Selected baseline for the first real smoke test |
| OpenAI for text/audio plus a separate image provider | Keeps the stable text/audio path while allowing a later visual-quality comparison | More secrets, retention terms, failure modes, accounting, and implementation work | Consider only if a small visual calibration set shows a material quality advantage |
| Local transcription plus cloud text/image | Keeps raw audio off a transcription provider and may reduce per-minute fees at scale | More server CPU/RAM, packaging, latency, model updates, and operational complexity | Not justified for the single-owner MVP unless privacy requirements change |

The default technical experiment should use one cheap text draft, one transcription, and one image per explicit request. The owner reports that API billing is active and accepted a USD 0.50 per-post warning, USD 0.75 per-post hard stop, and USD 25 monthly hard stop. The locally installed key authenticated through the official read-only model-list endpoint on 2026-10-12, which returned text, transcription, and image candidates. No paid inference was attempted. Paid smoke tests remain blocked until the accepted limits guard the persistent queue.

### Documented OpenAI facts

- ChatGPT subscription status is not evidence of API billing or model access.
- The transcription API accepts `flac`, `mp3`, `mp4`, `mpeg`, `mpga`, `m4a`, `ogg`, `wav`, and `webm`.
- Dated list prices include `gpt-4o-mini-transcribe` at an estimated USD 0.003/minute, `gpt-transcribe` at USD 0.0045/minute, and `gpt-4o-transcribe` at USD 0.006/minute.
- The current image APIs support generation and editing. The direct Image API is the simpler fit for one image per approved text; the Responses API is useful when high-fidelity multi-turn editing materially improves the product.
- Image billing is token-based and varies with model, resolution, and quality. Praeco must record returned usage and use explicit size/quality settings; it must not invent a flat image price.
- API data is not used to train OpenAI models by default unless the customer opts in. Default abuse-monitoring logs may retain customer content for up to 30 days. Zero Data Retention and Modified Abuse Monitoring require eligibility and approval; `store: false` is still the appropriate baseline where supported, but is not a substitute for an approved ZDR setting.

### Primary sources

- [OpenAI audio API reference](https://developers.openai.com/api/reference/resources/audio)
- [OpenAI image generation guide](https://developers.openai.com/api/docs/guides/image-generation)
- [OpenAI API pricing](https://developers.openai.com/api/docs/pricing)
- [OpenAI API data controls](https://developers.openai.com/api/docs/guides/your-data)

## Hosting shortlist

No hosting purchase or plan change is authorized. Figures below are dated list prices before taxes, optional IPv4, backups, outbound traffic beyond included allowances, or currency conversion where applicable.

| Option | Small persistent starting point | Dated price signal | Fit |
| --- | --- | --- | --- |
| Hetzner Cloud CAX11, EU | 2 ARM vCPU, 4 GB RAM, 40 GB NVMe | EUR 5.99/month after the 2026-06-15 adjustment, excluding VAT and IPv4 | Best predictable-cost candidate if payment and ARM compatibility are confirmed |
| Railway Hobby | Managed deployment with persistent volume | USD 5/month minimum including USD 5 usage; RAM USD 10/GB-month, CPU USD 20/vCPU-month, volume USD 0.15/GB-month | Easiest operations, but an always-on process can exceed the minimum and cost is less predictable |
| Fly.io | One shared machine plus a persistent volume | 1 GB shared machine about USD 6.70/month in the lowest-priced US regions; volume USD 0.15/GB-month | Good container/runtime fit; region-dependent compute price and single-volume locality require care |

The provisional recommendation is a small EU VPS with 4 GB RAM and persistent NVMe, then an encrypted backup target outside that server. Actual selection requires owner confirmation of payment availability and region. A sleeping free tier, ephemeral disk, and the owner's laptop are not acceptable production hosts.

### Primary sources

- [Hetzner 2026 price adjustment](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/)
- [Hetzner CAX plan specifications](https://www.hetzner.com/cloud/cost-optimized/)
- [Railway pricing](https://docs.railway.com/pricing)
- [Fly.io resource pricing](https://docs.fly.io/about/pricing)

## Remaining proof before Stage 1 is complete

- Create or select the owner's LinkedIn Page and developer app, then inspect actual enabled products and scopes without exposing credentials.
- Implement the Telegram polling adapter and enforce the installed numeric owner ID on every update and callback.
- Persist actual and unknown-billed usage when resolving reserved jobs before any paid OpenAI inference request.
- Run one explicitly authorized minimal transcription and image smoke test and record real usage.
- Confirm hosting payment availability and region, then obtain owner approval for one priced option.
- Review current provider terms for commercial use and the owner's intended jurisdiction before public or paid distribution.
