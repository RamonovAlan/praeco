# Privacy

Praeco processes material supplied by its single owner: Telegram messages, voice
files, revision comments, generated text, images, approval state, usage records,
and LinkedIn connection data.

## Data flow

- Telegram delivers owner input to the self-hosted service.
- In OpenAI mode, the minimum input required for the requested text,
  transcription, or image operation is sent to OpenAI.
- Only the exact owner-approved text and original approved image are sent to
  LinkedIn after the final publish action.
- Railway hosts the current private service and persistent volume.
- Encrypted production backups are stored as private GitHub Actions artifacts.

Provider retention and account settings are governed by the owner's agreements
with those providers. Praeco does not claim to override them.

## Local retention

SQLite records workflow state, versions, jobs, publication state, usage, and
encrypted connection data. Original media is stored on persistent disk. Local
backup behavior is documented in [backup](backup.md); retention is selected by
the operator.

Owner-facing export and deletion commands are not yet complete. Until they are,
data removal is an administrative operation that must preserve database and media
consistency and be preceded by a verified backup when recovery is expected.

## Public repository boundary

Never publish real drafts, transcripts, screenshots, voice files, generated
owner media, Telegram identifiers, OAuth account identifiers, databases, logs,
or backups. Public examples must use synthetic content and synthetic identities.
