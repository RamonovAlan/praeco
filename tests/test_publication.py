import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from praeco.drafts import DraftIdentity, DraftRepository
from praeco.linkedin import CredentialCipher, LinkedInOAuthConfig, LinkedInOAuthService
from praeco.linkedin_publish import (
    HttpJsonResponse,
    LinkedInPublisher,
    LinkedInPublisherConfig,
    LinkedInPublishError,
)
from praeco.publication import (
    PublicationRepository,
    PublicationWorker,
    PublicationWorkerOutcome,
)
from praeco.storage import apply_migrations, connect_database

MIGRATIONS = Path(__file__).parents[1] / "migrations"
OWNER_ID = 101
NOW = "2026-10-13T15:00:00Z"
IMAGE = b"synthetic-png"


class FakeTransport:
    def __init__(self, *, unknown: bool = False) -> None:
        self.unknown = unknown
        self.calls = 0

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        expected_status: int,
        result_unknown_on_transport_error: bool = False,
    ) -> HttpJsonResponse:
        self.calls += 1
        assert headers["Authorization"] == "Bearer synthetic-access-token"
        if url.endswith("initializeUpload"):
            return HttpJsonResponse(
                body={
                    "value": {
                        "uploadUrl": "https://www.linkedin.com/dms-uploads/test",
                        "image": "urn:li:image:synthetic",
                    }
                },
                headers={},
            )
        if self.unknown:
            raise LinkedInPublishError("synthetic timeout", result_unknown=True)
        return HttpJsonResponse(body={}, headers={"x-restli-id": "urn:li:share:1"})

    def put_bytes(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        content: bytes,
        expected_statuses: frozenset[int],
    ) -> None:
        self.calls += 1
        assert content == IMAGE


def setup(tmp_path: Path):
    database = tmp_path / "praeco.sqlite3"
    connection = connect_database(database)
    apply_migrations(connection, MIGRATIONS)
    media = tmp_path / "media"
    image_path = media / "draft-1" / "image-v1.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(IMAGE)
    drafts = DraftRepository(connection)
    drafts.create(
        identity=DraftIdentity(
            draft_id="draft-1",
            owner_id=OWNER_ID,
            source_kind="text",
            source_text="Synthetic thought",
            language="ru",
        ),
        now=NOW,
    )
    drafts.start_text_generation(draft_id="draft-1", actor_id=OWNER_ID, now=NOW)
    drafts.record_text(
        draft_id="draft-1",
        actor_id=OWNER_ID,
        version=1,
        body="Synthetic approved post",
        revision_comment=None,
        prompt_version="test",
        style_version="test",
        provider_model="mock",
        image_brief="Synthetic image",
        now=NOW,
    )
    drafts.approve_text(draft_id="draft-1", actor_id=OWNER_ID, version=1, now=NOW)
    drafts.start_image_generation(draft_id="draft-1", actor_id=OWNER_ID, now=NOW)
    drafts.record_image(
        draft_id="draft-1",
        actor_id=OWNER_ID,
        version=1,
        source_text_version=1,
        brief="Synthetic image",
        prompt="Synthetic prompt",
        local_path="draft-1/image-v1.png",
        sha256=hashlib.sha256(IMAGE).hexdigest(),
        mime_type="image/png",
        width=1200,
        height=627,
        now=NOW,
    )
    drafts.approve_image(draft_id="draft-1", actor_id=OWNER_ID, version=1, now=NOW)
    key = b"k" * 32
    encrypted = CredentialCipher(key).encrypt(
        account_id="synthetic-member",
        credentials={"access_token": "synthetic-access-token", "token_type": "Bearer"},
    )
    connection.execute(
        """
        INSERT INTO account_connections (
            platform, account_id, scopes_json, encrypted_credentials,
            expires_at, created_at, updated_at
        ) VALUES ('linkedin', ?, ?, ?, ?, ?, ?)
        """,
        (
            "synthetic-member",
            json.dumps(["openid", "profile", "w_member_social"]),
            encrypted,
            "2026-12-01T00:00:00Z",
            NOW,
            NOW,
        ),
    )
    connection.commit()
    oauth = LinkedInOAuthService(
        database_path=database,
        config=LinkedInOAuthConfig(
            client_id="synthetic-client",
            client_secret="synthetic-secret",
            redirect_uri="https://praeco.example/oauth/linkedin/callback",
            credential_key=key,
        ),
    )
    return connection, drafts, media, oauth


def test_explicit_snapshot_is_published_once_and_notified(tmp_path: Path) -> None:
    connection, drafts, media, oauth = setup(tmp_path)
    repository = PublicationRepository(connection)
    publication_id = repository.enqueue(
        draft_id="draft-1",
        actor_id=OWNER_ID,
        text_version=1,
        image_version=1,
        now=NOW,
    )
    transport = FakeTransport()
    worker = PublicationWorker(
        connection=connection,
        media_directory=media,
        oauth=oauth,
        publisher=LinkedInPublisher(
            config=LinkedInPublisherConfig(),
            transport=transport,
        ),
    )

    result = worker.run_once(now=NOW)
    idle = worker.run_once(now=NOW)

    assert result.outcome is PublicationWorkerOutcome.PUBLISHED
    assert idle.outcome is PublicationWorkerOutcome.IDLE
    assert transport.calls == 3
    assert drafts.get("draft-1").state.value == "PUBLISHED"
    row = connection.execute(
        "SELECT state, external_id FROM publications WHERE id = ?",
        (publication_id,),
    ).fetchone()
    assert tuple(row) == ("PUBLISHED", "urn:li:share:1")
    notification = connection.execute(
        "SELECT kind, publication_id, state FROM telegram_outbox"
    ).fetchone()
    assert tuple(notification) == ("PUBLICATION_RESULT", publication_id, "PENDING")


def test_ambiguous_create_stops_without_retry(tmp_path: Path) -> None:
    connection, drafts, media, oauth = setup(tmp_path)
    repository = PublicationRepository(connection)
    publication_id = repository.enqueue(
        draft_id="draft-1",
        actor_id=OWNER_ID,
        text_version=1,
        image_version=1,
        now=NOW,
    )
    transport = FakeTransport(unknown=True)
    worker = PublicationWorker(
        connection=connection,
        media_directory=media,
        oauth=oauth,
        publisher=LinkedInPublisher(
            config=LinkedInPublisherConfig(),
            transport=transport,
        ),
    )

    result = worker.run_once(now=NOW)
    idle = worker.run_once(now=NOW)

    assert result.outcome is PublicationWorkerOutcome.PUBLISH_UNKNOWN
    assert idle.outcome is PublicationWorkerOutcome.IDLE
    assert transport.calls == 3
    assert drafts.get("draft-1").state.value == "PUBLISH_UNKNOWN"
    state = connection.execute(
        "SELECT state FROM publications WHERE id = ?",
        (publication_id,),
    ).fetchone()[0]
    assert state == "PUBLISH_UNKNOWN"
