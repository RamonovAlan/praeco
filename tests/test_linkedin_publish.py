from collections.abc import Mapping

import pytest

from praeco.linkedin_publish import (
    HttpJsonResponse,
    LinkedInPublicationRequest,
    LinkedInPublisher,
    LinkedInPublisherConfig,
    LinkedInPublishError,
)


class FakeTransport:
    def __init__(self, *, post_id: str | None = "urn:li:share:123") -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.post_id = post_id

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        expected_status: int,
        result_unknown_on_transport_error: bool = False,
    ) -> HttpJsonResponse:
        self.calls.append(("post", url, dict(payload)))
        assert headers["Authorization"] == "Bearer synthetic-token"
        assert headers["Linkedin-Version"] == "202609"
        assert headers["X-Restli-Protocol-Version"] == "2.0.0"
        if url.endswith("initializeUpload"):
            assert expected_status == 200
            assert result_unknown_on_transport_error is False
            return HttpJsonResponse(
                body={
                    "value": {
                        "uploadUrl": "https://www.linkedin.com/dms-uploads/synthetic",
                        "image": "urn:li:image:synthetic_image",
                    }
                },
                headers={},
            )
        assert expected_status == 201
        assert result_unknown_on_transport_error is True
        headers_out = {} if self.post_id is None else {"x-restli-id": self.post_id}
        return HttpJsonResponse(body={}, headers=headers_out)

    def put_bytes(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        content: bytes,
        expected_statuses: frozenset[int],
    ) -> None:
        self.calls.append(("put", url, content))
        assert headers == {
            "Authorization": "Bearer synthetic-token",
            "Content-Type": "image/png",
        }
        assert expected_statuses == frozenset({200, 201, 204})


def request() -> LinkedInPublicationRequest:
    return LinkedInPublicationRequest(
        account_id="opaque_OIDC-sub",
        access_token="synthetic-token",
        commentary="Проверочный текст без реальной публикации.",
        image_content=b"synthetic-png",
        image_mime_type="image/png",
    )


def test_builds_current_posts_and_images_api_requests_without_network() -> None:
    transport = FakeTransport()
    publisher = LinkedInPublisher(
        config=LinkedInPublisherConfig(),
        transport=transport,
    )

    result = publisher.publish(request())

    assert result.external_id == "urn:li:share:123"
    assert [call[0] for call in transport.calls] == ["post", "put", "post"]
    initialize = transport.calls[0][2]
    assert initialize == {
        "initializeUploadRequest": {"owner": "urn:li:person:opaque_OIDC-sub"}
    }
    post = transport.calls[2][2]
    assert isinstance(post, dict)
    assert post["author"] == "urn:li:person:opaque_OIDC-sub"
    assert post["visibility"] == "PUBLIC"
    assert post["lifecycleState"] == "PUBLISHED"
    assert post["content"] == {
        "media": {
            "altText": "Иллюстрация к публикации",
            "id": "urn:li:image:synthetic_image",
        }
    }


def test_missing_post_id_is_unknown_and_cannot_be_retried_automatically() -> None:
    publisher = LinkedInPublisher(
        config=LinkedInPublisherConfig(),
        transport=FakeTransport(post_id=None),
    )

    with pytest.raises(LinkedInPublishError) as caught:
        publisher.publish(request())

    assert caught.value.result_unknown is True


@pytest.mark.parametrize(
    "api_version",
    ("20261", "202613", "latest", ""),
)
def test_rejects_invalid_api_versions(api_version: str) -> None:
    with pytest.raises(ValueError):
        LinkedInPublisherConfig(api_version=api_version)


def test_request_repr_hides_post_token_and_image() -> None:
    value = request()

    rendered = repr(value)

    assert "synthetic-token" not in rendered
    assert "Проверочный" not in rendered
    assert "synthetic-png" not in rendered


def test_rejects_unsafe_upload_url_before_sending_credentials() -> None:
    class UnsafeTransport(FakeTransport):
        def post_json(self, **kwargs):
            if str(kwargs["url"]).endswith("initializeUpload"):
                return HttpJsonResponse(
                    body={
                        "value": {
                            "uploadUrl": "https://example.org/steal-token",
                            "image": "urn:li:image:synthetic_image",
                        }
                    },
                    headers={},
                )
            return super().post_json(**kwargs)

    with pytest.raises(LinkedInPublishError, match="unsafe"):
        LinkedInPublisher(
            config=LinkedInPublisherConfig(),
            transport=UnsafeTransport(),
        ).publish(request())
