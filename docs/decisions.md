# Decision log

## 2026-10-12 — Initial implementation baseline

- **Status:** provisional, pending feasibility checks.
- **Decision:** use Python, explicit workflows, SQLite, a database-backed worker, and Docker Compose for the first implementation.
- **Reason:** this is the simplest architecture consistent with a single owner, durable work, and restart safety.
- **Constraints:** one application instance and one worker; no microservices, Kubernetes, autonomous agent, web dashboard, or vector database in the MVP.

## 2026-10-12 — Publishing disabled by default

- **Status:** accepted.
- **Decision:** real publishing must require configured official LinkedIn access, current version approvals, an immutable snapshot, and the owner's final click.
- **Reason:** this is a hard product and safety requirement.

## 2026-10-12 — Visual reference handling

- **Status:** accepted.
- **Decision:** record user-provided visual references as high-level attributes and links/descriptions. Do not copy a living artist's recognizable style, composition, characters, or copyrighted image into generated assets.
- **Reason:** the reference is preference evidence, not permission to reproduce the work.

## 2026-10-12 — Product name correction

- **Status:** accepted by the owner.
- **Decision:** the product and public-facing name is `Praeco`; technical identifiers, package name, bot references, and default private repository name use `praeco` where lowercase is required.
- **Reason:** the owner explicitly corrected the earlier working name. The Git history preserves both superseded spellings; active product references use only `Praeco`/`praeco`.

## 2026-10-12 — Initial audience and language

- **Status:** accepted by the owner.
- **Decision:** start in Russian for hiring IT companies, HR professionals in those companies, and managers. Keep language configurable for later expansion.
- **Reason:** these are the owner's stated first audience and language.

## 2026-10-12 — Voice calibration without sample texts

- **Status:** accepted.
- **Decision:** do not block implementation on a pre-supplied writing corpus. Build an external structure/indicator research set and learn the owner's voice from explicit edits and approvals inside the private product.
- **Reason:** the owner wants to provide thoughts through the working product rather than prepare example texts in advance.

## 2026-10-12 — API risk envelope and economy policy

- **Status:** accepted by the owner.
- **Decision:** warn at USD 0.50 per post, stop at USD 0.75 per post, and stop at USD 25 per calendar month. Treat these values as ceilings, not spending targets. Generate one text draft and one image per owner request, use the cheapest suitable text path, and perform no automatic paid retries.
- **Reason:** the envelope covers one daily post, owner-requested revisions, one normal image, transcription, and billing uncertainty while the economy defaults keep expected spend materially below the caps.
