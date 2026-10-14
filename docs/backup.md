# Local backup and restore

Praeco provides a local encrypted backup CLI for the SQLite database and media
directory. It does not upload archives or configure a remote storage provider.

Generate a dedicated random 32-byte key, encode it with URL-safe Base64, and keep
it outside the repository and outside the backup storage. Do not reuse an OAuth
credential key. Losing the key makes encrypted archives unrecoverable.

Create and verify an archive:

```bash
uv run praeco-backup create \
  --database ./data/praeco.sqlite3 \
  --media ./media \
  --output ./backups

uv run praeco-backup verify --backup ./backups/ARCHIVE.backup
```

Restore only into an empty destination after verifying the archive:

```bash
uv run praeco-backup restore \
  --backup ./backups/ARCHIVE.backup \
  --destination ./restore-test
```

The key is read from `PRAECO_BACKUP_ENCRYPTION_KEY`. Keep archives private: they
contain owner data even though the payload is encrypted. Test restoration
regularly and retain more than one generation before deleting working state.
