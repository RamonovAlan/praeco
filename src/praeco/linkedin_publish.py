"""Official LinkedIn Posts and Images API boundary for one approved snapshot."""

from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, cast

_API_BASE_URL = "https://api.linkedin.com"
_RESTLI_VERSION = "2.0.0"
_MAX_RESPONSE_BYTES = 1_000_000
_MAX_IMAGE_BYTES = 10_000_000
_ACCOUNT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_IMAGE_URN_PATTERN = re.compile(r"^urn:li:image:[A-Za-z0-9_-]{1,300}$")


class LinkedInPublishError(RuntimeError):
    """Sanitized publication failure with an explicit ambiguity boundary."""

    def __init__(self, message: str, *, result_unknown: bool = False) -> None:
        super().__init__(message)
        self.result_unknown = result_unknown


@dataclass(frozen=True, slots=True)
class LinkedInPublisherConfig:
    """Versioned LinkedIn API configuration."""

    api_version: str = "202609"
    timeout_seconds: int = 30

    def __post_init__(self) -> None:
        if not re.fullmatch(r"20[0-9]{4}", self.api_version):
            raise ValueError("LinkedIn API version must use YYYYMM format")
        month = int(self.api_version[-2:])
        if not 1 <= month <= 12:
            raise ValueError("LinkedIn API version month is invalid")
        if self.timeout_seconds <= 0:
            raise ValueError("LinkedIn timeout must be positive")


@dataclass(frozen=True, slots=True)
class LinkedInPublicationRequest:
    """Exact approved post payload supplied only after the owner's final click."""

    account_id: str
    access_token: str = field(repr=False)
    commentary: str = field(repr=False)
    image_content: bytes = field(repr=False)
    image_mime_type: str
    alt_text: str = "Иллюстрация к публикации"

    def __post_init__(self) -> None:
        if not _ACCOUNT_ID_PATTERN.fullmatch(self.account_id):
            raise ValueError("LinkedIn account identifier is invalid")
        if not self.access_token.strip():
            raise ValueError("LinkedIn access token is required")
        if not self.commentary.strip() or len(self.commentary) > 3_000:
            raise ValueError("LinkedIn commentary must contain 1 to 3000 characters")
        if not self.image_content or len(self.image_content) > _MAX_IMAGE_BYTES:
            raise ValueError("LinkedIn image must contain at most 10 MB")
        if self.image_mime_type not in {"image/png", "image/jpeg"}:
            raise ValueError("LinkedIn image must be PNG or JPEG")
        if not self.alt_text.strip() or len(self.alt_text) > 1_000:
            raise ValueError("LinkedIn image alt text is invalid")

    @property
    def person_urn(self) -> str:
        """Build the Person URN from the OIDC subject documented for self-service share."""

        return f"urn:li:person:{self.account_id}"


@dataclass(frozen=True, slots=True)
class LinkedInPublicationResult:
    """Provider acknowledgement returned after a successful create response."""

    external_id: str


@dataclass(frozen=True, slots=True)
class HttpJsonResponse:
    """Bounded response exposed by the injectable HTTP transport."""

    body: object
    headers: Mapping[str, str]


class LinkedInPublishTransport(Protocol):
    """Injectable HTTP boundary; tests implement it without network calls."""

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        expected_status: int,
        result_unknown_on_transport_error: bool = False,
    ) -> HttpJsonResponse: ...

    def put_bytes(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        content: bytes,
        expected_statuses: frozenset[int],
    ) -> None: ...


class UrllibLinkedInPublishTransport:
    """Bounded official HTTPS transport that never includes provider bodies in errors."""

    def __init__(self, *, timeout_seconds: int = 30) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        self._timeout_seconds = timeout_seconds
        self._ssl_context = ssl.create_default_context()

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        expected_status: int,
        result_unknown_on_transport_error: bool = False,
    ) -> HttpJsonResponse:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            ),
            headers=dict(headers),
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self._timeout_seconds,
                context=self._ssl_context,
            ) as response:
                status = response.status
                raw = cast(bytes, response.read(_MAX_RESPONSE_BYTES + 1))
                response_headers = {key.lower(): value for key, value in response.headers.items()}
        except urllib.error.HTTPError as error:
            raise LinkedInPublishError(
                f"LinkedIn request returned HTTP {error.code}"
            ) from None
        except (OSError, TimeoutError, urllib.error.URLError):
            raise LinkedInPublishError(
                "LinkedIn request outcome is unknown"
                if result_unknown_on_transport_error
                else "LinkedIn request failed",
                result_unknown=result_unknown_on_transport_error,
            ) from None
        if status != expected_status:
            raise LinkedInPublishError(f"LinkedIn request returned HTTP {status}")
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise LinkedInPublishError(
                "LinkedIn response is too large",
                result_unknown=result_unknown_on_transport_error,
            )
        if not raw:
            body: object = {}
        else:
            try:
                body = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise LinkedInPublishError(
                    "LinkedIn returned invalid JSON",
                    result_unknown=result_unknown_on_transport_error,
                ) from None
        return HttpJsonResponse(body=body, headers=response_headers)

    def put_bytes(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        content: bytes,
        expected_statuses: frozenset[int],
    ) -> None:
        _validate_upload_url(url)
        request = urllib.request.Request(
            url,
            data=content,
            headers=dict(headers),
            method="PUT",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self._timeout_seconds,
                context=self._ssl_context,
            ) as response:
                status = response.status
                response.read(1)
        except urllib.error.HTTPError as error:
            raise LinkedInPublishError(
                f"LinkedIn image upload returned HTTP {error.code}"
            ) from None
        except (OSError, TimeoutError, urllib.error.URLError):
            raise LinkedInPublishError("LinkedIn image upload failed") from None
        if status not in expected_statuses:
            raise LinkedInPublishError(
                f"LinkedIn image upload returned HTTP {status}"
            )


class LinkedInPublisher:
    """Upload one approved image and create one public personal-profile post."""

    def __init__(
        self,
        *,
        config: LinkedInPublisherConfig,
        transport: LinkedInPublishTransport | None = None,
    ) -> None:
        self._config = config
        self._transport = transport or UrllibLinkedInPublishTransport(
            timeout_seconds=config.timeout_seconds
        )

    def publish(self, request: LinkedInPublicationRequest) -> LinkedInPublicationResult:
        headers = _api_headers(
            access_token=request.access_token,
            api_version=self._config.api_version,
        )
        initialized = self._transport.post_json(
            url=f"{_API_BASE_URL}/rest/images?action=initializeUpload",
            headers=headers,
            payload={"initializeUploadRequest": {"owner": request.person_urn}},
            expected_status=200,
        )
        upload_url, image_urn = _upload_ticket(initialized.body)
        self._transport.put_bytes(
            url=upload_url,
            headers={
                "Authorization": f"Bearer {request.access_token}",
                "Content-Type": request.image_mime_type,
            },
            content=request.image_content,
            expected_statuses=frozenset({200, 201, 204}),
        )
        created = self._transport.post_json(
            url=f"{_API_BASE_URL}/rest/posts",
            headers=headers,
            payload=_post_payload(request=request, image_urn=image_urn),
            expected_status=201,
            result_unknown_on_transport_error=True,
        )
        external_id = _header(created.headers, "x-restli-id")
        if external_id is None or not external_id.strip():
            raise LinkedInPublishError(
                "LinkedIn created a post without a usable ID",
                result_unknown=True,
            )
        return LinkedInPublicationResult(external_id=external_id.strip())


def _api_headers(*, access_token: str, api_version: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Linkedin-Version": api_version,
        "X-Restli-Protocol-Version": _RESTLI_VERSION,
    }


def _post_payload(
    *, request: LinkedInPublicationRequest, image_urn: str
) -> dict[str, object]:
    return {
        "author": request.person_urn,
        "commentary": request.commentary,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "content": {
            "media": {
                "altText": request.alt_text,
                "id": image_urn,
            }
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }


def _upload_ticket(value: object) -> tuple[str, str]:
    if not isinstance(value, dict) or not isinstance(value.get("value"), dict):
        raise LinkedInPublishError("LinkedIn image initialization is invalid")
    result = cast(dict[object, object], value["value"])
    upload_url = result.get("uploadUrl")
    image_urn = result.get("image")
    if not isinstance(upload_url, str) or not isinstance(image_urn, str):
        raise LinkedInPublishError("LinkedIn image initialization is incomplete")
    _validate_upload_url(upload_url)
    if not _IMAGE_URN_PATTERN.fullmatch(image_urn):
        raise LinkedInPublishError("LinkedIn returned an invalid image identifier")
    return upload_url, image_urn


def _validate_upload_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not hostname
        or (hostname != "linkedin.com" and not hostname.endswith(".linkedin.com"))
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise LinkedInPublishError("LinkedIn returned an unsafe upload URL")


def _header(headers: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None
