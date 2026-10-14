# LinkedIn setup for member publishing

Checked: 2026-10-13

A normal LinkedIn profile does not by itself provide API publishing access.
Praeco uses LinkedIn's official OAuth and member-posting APIs.

## Application setup

1. Create or choose a LinkedIn Page that can be associated with the developer
   application. You must be a Super Admin on that Page.
2. Create a LinkedIn application and associate it with that Page.
3. Request `Sign In with LinkedIn using OpenID Connect` and `Share on LinkedIn`.
4. Add an HTTPS redirect URI ending in `/oauth/linkedin/callback`. It must match
   `PRAECO_LINKEDIN_REDIRECT_URI` exactly.
5. Store the Client ID, Client Secret, and a dedicated credential-encryption key
   only in the local secret file or hosting provider's secret store.
6. Start Praeco, send `/linkedin` in the owner chat, and finish the one-time OAuth
   flow. Use `/status` to confirm the connection without exposing token data.

Praeco requests `openid profile w_member_social`. Product availability,
permissions, application review, and platform behavior can change; verify the
current LinkedIn documentation before relying on an integration.

## Implemented OAuth boundary

- `/linkedin` creates a one-time 10-minute consent link. Only a SHA-256 hash of
  the random CSRF state is stored, and each state can be consumed once.
- The callback validates state before any provider call, exchanges the code only
  through LinkedIn's official token endpoint, verifies required returned scopes,
  and obtains the pairwise member subject through OIDC userinfo.
- Access and optional refresh tokens are encrypted with account-bound
  AES-256-GCM before SQLite storage.
- Reauthorization may update the same member connection. A different member
  cannot silently replace it.
- `/status` reports only connection state and expiry. It never prints the member
  identifier, token, profile fields, or email.
- OAuth completion does not publish anything and does not bypass the final owner
  approval.

## Publishing constraints

- Member publishing uses the official Posts API and an authorized person author
  URN.
- Images are uploaded through the Images API before their URNs are referenced by
  a post.
- The final action remains a separate owner click bound to the exact approved
  text and image versions.
- Token expiry or revoked access pauses publishing and requires reauthorization;
  ambiguous creates are not retried automatically.
- Restricted permissions, readback, and analytics are not assumed.

No LinkedIn scraping, browser automation for posting, cookie reuse, or password
storage is permitted.

## Primary sources

- [Associate an app with a LinkedIn Page](https://www.linkedin.com/help/linkedin/answer/a553297)
- [Getting Access to LinkedIn APIs](https://learn.microsoft.com/en-us/linkedin/shared/authentication/getting-access)
- [Sign In with LinkedIn using OpenID Connect](https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/sign-in-with-linkedin-v2)
- [LinkedIn 3-legged OAuth flow](https://learn.microsoft.com/en-us/linkedin/shared/authentication/authorization-code-flow)
