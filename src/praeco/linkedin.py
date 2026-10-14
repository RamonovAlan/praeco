"""Official LinkedIn OAuth boundary with encrypted single-owner credentials."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path
from typing import Protocol, cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from praeco.storage import connect_database, immediate_transaction

_AUTHORIZATION_URL = "https://www.linkedin.com/oauth/v2/authorization"
_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
_USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
_REQUIRED_SCOPES = frozenset({"openid", "profile", "w_member_social"})
_CREDENTIAL_MAGIC = b"PRAECO-LINKEDIN-CREDENTIAL-V1\x00"
_MAX_RESPONSE_BYTES = 1_000_000
_MAX_QUERY_BYTES = 12_000
_STATE_LIFETIME = timedelta(minutes=10)


class LinkedInOAuthError(RuntimeError):
    """Sanitized OAuth failure that never contains codes, tokens, or response bodies."""


class LinkedInAccountMismatch(LinkedInOAuthError):
    """Raised when a different member tries to replace the bound owner account."""


class OAuthCallbackOutcome(StrEnum):
    """Bounded outcomes safe to render in the public callback page."""

    CONNECTED = "CONNECTED"
    DENIED = "DENIED"
    INVALID = "INVALID"
    UPSTREAM_FAILED = "UPSTREAM_FAILED"
    TOKEN_EXCHANGE_FAILED = "TOKEN_EXCHANGE_FAILED"
    TOKEN_RESPONSE_INVALID = "TOKEN_RESPONSE_INVALID"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    USERINFO_FAILED = "USERINFO_FAILED"
    STORAGE_FAILED = "STORAGE_FAILED"
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"


@dataclass(frozen=True, slots=True)
class OAuthCallbackResult:
    """Public result with no member attributes or provider response content."""

    status: HTTPStatus
    outcome: OAuthCallbackOutcome


@dataclass(frozen=True, slots=True)
class LinkedInConnectionStatus:
    """Non-sensitive owner-facing connection state."""

    connected: bool
    expires_at: str | None = None
    required_scopes_present: bool = False


@dataclass(frozen=True, slots=True)
class LinkedInPublicationCredentials:
    """Decrypted credentials exposed only to the publication worker."""

    account_id: str
    access_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class LinkedInOAuthConfig:
    """Validated OAuth application configuration with redacted secrets."""

    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    credential_key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not self.client_id.strip() or not self.client_secret.strip():
            raise ValueError("LinkedIn client credentials are required")
        parsed = urllib.parse.urlsplit(self.redirect_uri)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.path != "/oauth/linkedin/callback"
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("LinkedIn redirect URI is invalid")
        if len(self.credential_key) != 32:
            raise ValueError("credential encryption key must contain 32 bytes")


class LinkedInOAuthTransport(Protocol):
    """Injectable official HTTP boundary used only after a valid callback state."""

    def exchange_code(self, *, code: str, config: LinkedInOAuthConfig) -> object: ...

    def userinfo(self, *, access_token: str) -> object: ...


class UrllibLinkedInOAuthTransport:
    """Bounded HTTPS client for LinkedIn token exchange and OIDC userinfo."""

    def __init__(self, *, timeout_seconds: int = 20) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        self._timeout_seconds = timeout_seconds
        self._ssl_context = ssl.create_default_context()

    def exchange_code(self, *, code: str, config: LinkedInOAuthConfig) -> object:
        body = urllib.parse.urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "redirect_uri": config.redirect_uri,
            }
        ).encode("ascii")
        request = urllib.request.Request(
            _TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        return self._request_json(request, operation="token exchange")

    def userinfo(self, *, access_token: str) -> object:
        request = urllib.request.Request(
            _USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            method="GET",
        )
        return self._request_json(request, operation="userinfo")

    def _request_json(self, request: urllib.request.Request, *, operation: str) -> object:
        try:
            with urllib.request.urlopen(
                request,
                timeout=self._timeout_seconds,
                context=self._ssl_context,
            ) as response:
                raw = cast(bytes, response.read(_MAX_RESPONSE_BYTES + 1))
        except urllib.error.HTTPError as error:
            raise LinkedInOAuthError(
                f"LinkedIn {operation} returned HTTP {error.code}"
            ) from None
        except (OSError, TimeoutError, urllib.error.URLError):
            raise LinkedInOAuthError(f"LinkedIn {operation} failed") from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise LinkedInOAuthError(f"LinkedIn {operation} response is too large")
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise LinkedInOAuthError(
                f"LinkedIn {operation} returned invalid JSON"
            ) from None


class CredentialCipher:
    """Encrypt OAuth credentials with account-bound AES-256-GCM."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("credential encryption key must contain 32 bytes")
        self._cipher = AESGCM(key)

    def encrypt(self, *, account_id: str, credentials: Mapping[str, object]) -> bytes:
        plaintext = json.dumps(
            dict(credentials),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        nonce = secrets.token_bytes(12)
        ciphertext = self._cipher.encrypt(nonce, plaintext, self._aad(account_id))
        return _CREDENTIAL_MAGIC + nonce + ciphertext

    def decrypt(self, *, account_id: str, encrypted: bytes) -> dict[str, object]:
        prefix_size = len(_CREDENTIAL_MAGIC)
        if not encrypted.startswith(_CREDENTIAL_MAGIC) or len(encrypted) <= prefix_size + 28:
            raise LinkedInOAuthError("stored LinkedIn credentials are invalid")
        nonce = encrypted[prefix_size : prefix_size + 12]
        ciphertext = encrypted[prefix_size + 12 :]
        try:
            plaintext = self._cipher.decrypt(nonce, ciphertext, self._aad(account_id))
            decoded = json.loads(plaintext)
        except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError):
            raise LinkedInOAuthError("stored LinkedIn credentials failed authentication") from None
        if not isinstance(decoded, dict):
            raise LinkedInOAuthError("stored LinkedIn credentials are invalid")
        return cast(dict[str, object], decoded)

    @staticmethod
    def _aad(account_id: str) -> bytes:
        return f"praeco:linkedin:account:{account_id}".encode()


class LinkedInOAuthService:
    """Create state-bound grants and persist one encrypted LinkedIn connection."""

    def __init__(
        self,
        *,
        database_path: Path,
        config: LinkedInOAuthConfig,
        transport: LinkedInOAuthTransport | None = None,
    ) -> None:
        self._database_path = database_path
        self._config = config
        self._transport = transport or UrllibLinkedInOAuthTransport()
        self._cipher = CredentialCipher(config.credential_key)

    def authorization_url(self, *, now: datetime | None = None) -> str:
        """Persist a hashed, short-lived state and return the official consent URL."""

        issued_at = _utc_now(now)
        state = secrets.token_urlsafe(32)
        state_hash = _state_hash(state)
        expires_at = issued_at + _STATE_LIFETIME
        try:
            with closing(self._connection()) as connection, immediate_transaction(connection):
                connection.execute(
                    "DELETE FROM linkedin_oauth_sessions WHERE expires_at <= ?",
                    (_timestamp(issued_at),),
                )
                connection.execute(
                    """
                    INSERT INTO linkedin_oauth_sessions (
                        state_hash, expires_at, created_at, consumed_at
                    ) VALUES (?, ?, ?, NULL)
                    """,
                    (state_hash, _timestamp(expires_at), _timestamp(issued_at)),
                )
        except (OSError, sqlite3.Error):
            raise LinkedInOAuthError("LinkedIn authorization state could not be stored") from None
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": self._config.client_id,
                "redirect_uri": self._config.redirect_uri,
                "state": state,
                "scope": " ".join(sorted(_REQUIRED_SCOPES)),
                "enable_extended_login": "true",
            }
        )
        return f"{_AUTHORIZATION_URL}?{query}"

    def handle_callback(
        self,
        query: str,
        *,
        now: datetime | None = None,
    ) -> OAuthCallbackResult:
        """Consume one state, exchange the code, verify scopes/identity, and encrypt."""

        received_at = _utc_now(now)
        parsed = _callback_query(query)
        state = parsed.get("state")
        try:
            state_valid = state is not None and self._consume_state(
                state=state,
                now=received_at,
            )
        except (OSError, sqlite3.Error):
            return OAuthCallbackResult(
                HTTPStatus.SERVICE_UNAVAILABLE,
                OAuthCallbackOutcome.UPSTREAM_FAILED,
            )
        if not state_valid:
            return OAuthCallbackResult(HTTPStatus.UNAUTHORIZED, OAuthCallbackOutcome.INVALID)
        if "error" in parsed:
            return OAuthCallbackResult(HTTPStatus.OK, OAuthCallbackOutcome.DENIED)
        code = parsed.get("code")
        if code is None:
            return OAuthCallbackResult(HTTPStatus.BAD_REQUEST, OAuthCallbackOutcome.INVALID)
        try:
            raw_token = self._transport.exchange_code(code=code, config=self._config)
        except (LinkedInOAuthError, OSError):
            return OAuthCallbackResult(
                HTTPStatus.BAD_GATEWAY,
                OAuthCallbackOutcome.TOKEN_EXCHANGE_FAILED,
            )
        try:
            token = _token_payload(raw_token)
        except LinkedInOAuthError:
            return OAuthCallbackResult(
                HTTPStatus.BAD_GATEWAY,
                OAuthCallbackOutcome.TOKEN_RESPONSE_INVALID,
            )
        scopes = _scopes(token["scope"])
        if not scopes >= _REQUIRED_SCOPES:
            return OAuthCallbackResult(
                HTTPStatus.BAD_GATEWAY,
                OAuthCallbackOutcome.SCOPE_MISMATCH,
            )
        try:
            profile = _userinfo_payload(
                self._transport.userinfo(access_token=cast(str, token["access_token"]))
            )
        except (LinkedInOAuthError, OSError):
            return OAuthCallbackResult(
                HTTPStatus.BAD_GATEWAY,
                OAuthCallbackOutcome.USERINFO_FAILED,
            )
        account_id = profile["sub"]
        expires_at = received_at + timedelta(seconds=cast(int, token["expires_in"]))
        credentials = {
            "access_token": token["access_token"],
            "token_type": token["token_type"],
        }
        if "refresh_token" in token:
            credentials["refresh_token"] = token["refresh_token"]
        try:
            encrypted = self._cipher.encrypt(
                account_id=account_id,
                credentials=credentials,
            )
            self._store_connection(
                account_id=account_id,
                scopes=scopes,
                encrypted_credentials=encrypted,
                expires_at=expires_at,
                now=received_at,
            )
        except LinkedInAccountMismatch:
            return OAuthCallbackResult(
                HTTPStatus.CONFLICT,
                OAuthCallbackOutcome.ACCOUNT_MISMATCH,
            )
        except (LinkedInOAuthError, OSError, sqlite3.Error):
            return OAuthCallbackResult(
                HTTPStatus.SERVICE_UNAVAILABLE,
                OAuthCallbackOutcome.STORAGE_FAILED,
            )
        return OAuthCallbackResult(HTTPStatus.OK, OAuthCallbackOutcome.CONNECTED)

    def connection_status(
        self,
        *,
        now: datetime | None = None,
    ) -> LinkedInConnectionStatus:
        checked_at = _utc_now(now)
        try:
            with closing(self._connection()) as connection:
                row = connection.execute(
                    """
                    SELECT scopes_json, expires_at
                    FROM account_connections
                    WHERE platform = 'linkedin'
                    ORDER BY id
                    LIMIT 1
                    """
                ).fetchone()
        except (OSError, sqlite3.Error):
            return LinkedInConnectionStatus(connected=False)
        if row is None:
            return LinkedInConnectionStatus(connected=False)
        try:
            values = json.loads(str(row["scopes_json"]))
            scopes = frozenset(values) if isinstance(values, list) else frozenset()
            expires_at = str(row["expires_at"])
            active = _parse_timestamp(expires_at) > checked_at
        except (TypeError, ValueError, json.JSONDecodeError):
            return LinkedInConnectionStatus(connected=False)
        required = scopes >= _REQUIRED_SCOPES
        return LinkedInConnectionStatus(
            connected=active and required,
            expires_at=expires_at,
            required_scopes_present=required,
        )

    def publication_credentials(
        self,
        *,
        now: datetime | None = None,
    ) -> LinkedInPublicationCredentials:
        """Decrypt one active least-privilege connection for an explicit publish."""

        checked_at = _utc_now(now)
        try:
            with closing(self._connection()) as connection:
                row = connection.execute(
                    """
                    SELECT account_id, scopes_json, encrypted_credentials, expires_at
                    FROM account_connections
                    WHERE platform = 'linkedin'
                    ORDER BY id
                    LIMIT 1
                    """
                ).fetchone()
        except (OSError, sqlite3.Error):
            raise LinkedInOAuthError("LinkedIn connection could not be loaded") from None
        if row is None:
            raise LinkedInOAuthError("LinkedIn is not connected")
        account_id = str(row["account_id"])
        try:
            values = json.loads(str(row["scopes_json"]))
            scopes = frozenset(values) if isinstance(values, list) else frozenset()
            expires_at = _parse_timestamp(str(row["expires_at"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            raise LinkedInOAuthError("stored LinkedIn connection is invalid") from None
        if not scopes >= _REQUIRED_SCOPES or expires_at <= checked_at:
            raise LinkedInOAuthError("LinkedIn connection is expired or incomplete")
        encrypted = row["encrypted_credentials"]
        if not isinstance(encrypted, bytes):
            raise LinkedInOAuthError("stored LinkedIn credentials are invalid")
        credentials = self._cipher.decrypt(
            account_id=account_id,
            encrypted=encrypted,
        )
        access_token = credentials.get("access_token")
        token_type = credentials.get("token_type")
        if (
            not isinstance(access_token, str)
            or not access_token.strip()
            or token_type != "Bearer"
        ):
            raise LinkedInOAuthError("stored LinkedIn credentials are invalid")
        return LinkedInPublicationCredentials(
            account_id=account_id,
            access_token=access_token,
        )

    def _consume_state(self, *, state: str, now: datetime) -> bool:
        if not 20 <= len(state) <= 256:
            return False
        with closing(self._connection()) as connection, immediate_transaction(connection):
            cursor = connection.execute(
                """
                UPDATE linkedin_oauth_sessions
                SET consumed_at = ?
                WHERE state_hash = ?
                  AND consumed_at IS NULL
                  AND expires_at > ?
                """,
                (_timestamp(now), _state_hash(state), _timestamp(now)),
            )
            return cursor.rowcount == 1

    def _store_connection(
        self,
        *,
        account_id: str,
        scopes: frozenset[str],
        encrypted_credentials: bytes,
        expires_at: datetime,
        now: datetime,
    ) -> None:
        with closing(self._connection()) as connection, immediate_transaction(connection):
            existing = connection.execute(
                "SELECT account_id FROM account_connections WHERE platform = 'linkedin'",
            ).fetchone()
            if existing is not None and str(existing["account_id"]) != account_id:
                raise LinkedInAccountMismatch(
                    "a different LinkedIn member is already connected"
                )
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO account_connections (
                        platform, account_id, scopes_json, encrypted_credentials,
                        expires_at, created_at, updated_at
                    ) VALUES ('linkedin', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        json.dumps(sorted(scopes), separators=(",", ":")),
                        encrypted_credentials,
                        _timestamp(expires_at),
                        _timestamp(now),
                        _timestamp(now),
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE account_connections
                    SET scopes_json = ?, encrypted_credentials = ?, expires_at = ?,
                        updated_at = ?
                    WHERE platform = 'linkedin' AND account_id = ?
                    """,
                    (
                        json.dumps(sorted(scopes), separators=(",", ":")),
                        encrypted_credentials,
                        _timestamp(expires_at),
                        _timestamp(now),
                        account_id,
                    ),
                )

    def _connection(self) -> sqlite3.Connection:
        return connect_database(self._database_path)


def decode_credential_key(value: str) -> bytes:
    """Decode a URL-safe, optionally unpadded 256-bit key."""

    normalized = value.strip()
    normalized += "=" * (-len(normalized) % 4)
    try:
        decoded = base64.urlsafe_b64decode(normalized.encode("ascii"))
    except (UnicodeEncodeError, ValueError):
        raise ValueError("credential encryption key is invalid") from None
    if len(decoded) != 32:
        raise ValueError("credential encryption key must decode to 32 bytes")
    return decoded


def _callback_query(query: str) -> dict[str, str]:
    if not query or len(query.encode("utf-8")) > _MAX_QUERY_BYTES:
        return {}
    try:
        parsed = urllib.parse.parse_qs(
            query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=8,
        )
    except (UnicodeError, ValueError):
        return {}
    if any(len(values) != 1 for values in parsed.values()):
        return {}
    result = {key: values[0] for key, values in parsed.items()}
    if set(result) - {"code", "state", "error", "error_description"}:
        return {}
    if any(len(value) > 10_000 for value in result.values()):
        return {}
    return result


def _token_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise LinkedInOAuthError("LinkedIn token response is invalid")
    access_token = payload.get("access_token")
    token_type = payload.get("token_type", "Bearer")
    expires_in = payload.get("expires_in")
    scope = payload.get("scope")
    if (
        not isinstance(access_token, str)
        or not 20 <= len(access_token) <= 8192
        or not isinstance(token_type, str)
        or token_type.lower() != "bearer"
        or not isinstance(expires_in, int)
        or isinstance(expires_in, bool)
        or not 1 <= expires_in <= 366 * 24 * 60 * 60
        or not isinstance(scope, str)
    ):
        raise LinkedInOAuthError("LinkedIn token response is invalid")
    result: dict[str, object] = {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "scope": scope,
    }
    refresh_token = payload.get("refresh_token")
    if refresh_token is not None:
        if not isinstance(refresh_token, str) or not 20 <= len(refresh_token) <= 8192:
            raise LinkedInOAuthError("LinkedIn refresh token is invalid")
        result["refresh_token"] = refresh_token
    return result


def _userinfo_payload(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise LinkedInOAuthError("LinkedIn userinfo response is invalid")
    subject = payload.get("sub")
    if (
        not isinstance(subject, str)
        or not subject.strip()
        or len(subject) > 255
        or any(ord(character) < 32 for character in subject)
    ):
        raise LinkedInOAuthError("LinkedIn userinfo subject is invalid")
    return {"sub": subject}


def _scopes(raw: object) -> frozenset[str]:
    if not isinstance(raw, str):
        return frozenset()
    decoded = urllib.parse.unquote_plus(raw)
    return frozenset(part for part in decoded.replace(",", " ").split() if part)


def _state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def _utc_now(value: datetime | None) -> datetime:
    result = value or datetime.now(UTC)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("OAuth timestamps must be timezone-aware")
    return result.astimezone(UTC).replace(microsecond=0)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
