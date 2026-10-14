import hashlib
import sqlite3
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path

from praeco.linkedin import (
    CredentialCipher,
    LinkedInOAuthConfig,
    LinkedInOAuthService,
    OAuthCallbackOutcome,
)
from praeco.storage import apply_migrations, connect_database

MIGRATIONS = Path(__file__).parents[1] / "migrations"
NOW = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)
ACCESS_TOKEN = "synthetic-access-token-" + "a" * 40


class FakeTransport:
    def __init__(
        self,
        *,
        subject: str = "synthetic-member",
        scope: str = "openid profile w_member_social",
        exchange_fails: bool = False,
        userinfo_fails: bool = False,
    ) -> None:
        self.subject = subject
        self.scope = scope
        self.exchange_fails = exchange_fails
        self.userinfo_fails = userinfo_fails
        self.exchanges = 0
        self.userinfo_calls = 0

    def exchange_code(self, *, code: str, config: LinkedInOAuthConfig) -> object:
        assert code == "synthetic-code"
        assert config.client_secret == "synthetic-secret"
        self.exchanges += 1
        if self.exchange_fails:
            raise OSError("synthetic provider failure")
        return {
            "access_token": ACCESS_TOKEN,
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": self.scope,
        }

    def userinfo(self, *, access_token: str) -> object:
        assert access_token == ACCESS_TOKEN
        self.userinfo_calls += 1
        if self.userinfo_fails:
            raise OSError("synthetic provider failure")
        return {"sub": self.subject, "name": "Private Owner Name"}


def setup(tmp_path: Path, *, transport: FakeTransport | None = None):
    database = tmp_path / "praeco.sqlite3"
    connection = connect_database(database)
    apply_migrations(connection, MIGRATIONS)
    connection.close()
    config = LinkedInOAuthConfig(
        client_id="synthetic-client",
        client_secret="synthetic-secret",
        redirect_uri="https://praeco.example/oauth/linkedin/callback",
        credential_key=b"k" * 32,
    )
    fake = transport or FakeTransport()
    return LinkedInOAuthService(
        database_path=database,
        config=config,
        transport=fake,
    ), database, fake


def authorization_state(service: LinkedInOAuthService) -> tuple[str, str]:
    url = service.authorization_url(now=NOW)
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "www.linkedin.com"
    assert query["scope"] == ["openid profile w_member_social"]
    assert query["enable_extended_login"] == ["true"]
    return url, query["state"][0]


def test_oauth_success_uses_hashed_one_time_state_and_encrypted_credentials(
    tmp_path: Path,
) -> None:
    service, database, transport = setup(tmp_path)
    _, state = authorization_state(service)

    with sqlite3.connect(database) as connection:
        stored_hash = connection.execute(
            "SELECT state_hash FROM linkedin_oauth_sessions"
        ).fetchone()[0]
    assert stored_hash == hashlib.sha256(state.encode()).hexdigest()
    assert state not in stored_hash

    result = service.handle_callback(
        urllib.parse.urlencode({"state": state, "code": "synthetic-code"}),
        now=NOW + timedelta(minutes=1),
    )

    assert result.outcome is OAuthCallbackOutcome.CONNECTED
    assert transport.exchanges == 1
    assert transport.userinfo_calls == 1
    status = service.connection_status(now=NOW + timedelta(minutes=2))
    assert status.connected is True
    assert status.required_scopes_present is True
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            """
            SELECT account_id, scopes_json, encrypted_credentials
            FROM account_connections WHERE platform = 'linkedin'
            """
        ).fetchone()
    assert row[0] == "synthetic-member"
    assert ACCESS_TOKEN.encode() not in row[2]
    credentials = CredentialCipher(b"k" * 32).decrypt(
        account_id="synthetic-member",
        encrypted=row[2],
    )
    assert credentials["access_token"] == ACCESS_TOKEN

    replay = service.handle_callback(
        urllib.parse.urlencode({"state": state, "code": "synthetic-code"}),
        now=NOW + timedelta(minutes=2),
    )
    assert replay.outcome is OAuthCallbackOutcome.INVALID
    assert transport.exchanges == 1


def test_oauth_rejects_missing_scope_without_storing_connection(tmp_path: Path) -> None:
    service, database, _ = setup(
        tmp_path,
        transport=FakeTransport(scope="openid profile"),
    )
    _, state = authorization_state(service)

    result = service.handle_callback(
        urllib.parse.urlencode({"state": state, "code": "synthetic-code"}),
        now=NOW + timedelta(minutes=1),
    )

    assert result.outcome is OAuthCallbackOutcome.SCOPE_MISMATCH
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM account_connections").fetchone()[0] == 0


def test_oauth_accepts_linkedin_comma_delimited_scopes(tmp_path: Path) -> None:
    service, _, _ = setup(
        tmp_path,
        transport=FakeTransport(scope="openid,profile,w_member_social"),
    )
    _, state = authorization_state(service)

    result = service.handle_callback(
        urllib.parse.urlencode({"state": state, "code": "synthetic-code"}),
        now=NOW + timedelta(minutes=1),
    )

    assert result.outcome is OAuthCallbackOutcome.CONNECTED


def test_oauth_reports_token_exchange_stage(tmp_path: Path) -> None:
    service, _, _ = setup(tmp_path, transport=FakeTransport(exchange_fails=True))
    _, state = authorization_state(service)

    result = service.handle_callback(
        urllib.parse.urlencode({"state": state, "code": "synthetic-code"}),
        now=NOW + timedelta(minutes=1),
    )

    assert result.outcome is OAuthCallbackOutcome.TOKEN_EXCHANGE_FAILED


def test_oauth_reports_userinfo_stage(tmp_path: Path) -> None:
    service, _, _ = setup(tmp_path, transport=FakeTransport(userinfo_fails=True))
    _, state = authorization_state(service)

    result = service.handle_callback(
        urllib.parse.urlencode({"state": state, "code": "synthetic-code"}),
        now=NOW + timedelta(minutes=1),
    )

    assert result.outcome is OAuthCallbackOutcome.USERINFO_FAILED


def test_oauth_cancellation_consumes_state_without_provider_call(tmp_path: Path) -> None:
    service, _, transport = setup(tmp_path)
    _, state = authorization_state(service)

    result = service.handle_callback(
        urllib.parse.urlencode({"state": state, "error": "user_cancelled_authorize"}),
        now=NOW + timedelta(minutes=1),
    )

    assert result.outcome is OAuthCallbackOutcome.DENIED
    assert transport.exchanges == 0


def test_oauth_refuses_to_replace_a_different_member(tmp_path: Path) -> None:
    first = FakeTransport(subject="member-one")
    service, database, _ = setup(tmp_path, transport=first)
    _, state = authorization_state(service)
    assert service.handle_callback(
        urllib.parse.urlencode({"state": state, "code": "synthetic-code"}),
        now=NOW + timedelta(minutes=1),
    ).outcome is OAuthCallbackOutcome.CONNECTED

    second = LinkedInOAuthService(
        database_path=database,
        config=LinkedInOAuthConfig(
            client_id="synthetic-client",
            client_secret="synthetic-secret",
            redirect_uri="https://praeco.example/oauth/linkedin/callback",
            credential_key=b"k" * 32,
        ),
        transport=FakeTransport(subject="member-two"),
    )
    _, second_state = authorization_state(second)

    result = second.handle_callback(
        urllib.parse.urlencode({"state": second_state, "code": "synthetic-code"}),
        now=NOW + timedelta(minutes=2),
    )

    assert result.outcome is OAuthCallbackOutcome.ACCOUNT_MISMATCH
    with sqlite3.connect(database) as connection:
        account_ids = connection.execute(
            "SELECT account_id FROM account_connections"
        ).fetchall()
    assert account_ids == [("member-one",)]
