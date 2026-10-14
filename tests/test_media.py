import pytest

from praeco.media import ImageValidationError, inspect_image_bytes


def png_bytes(*, width: int = 1200, height: int = 1200) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        + b"\x00\x00\x00\x00"
    )


def jpeg_bytes(*, width: int = 1200, height: int = 1200) -> bytes:
    return (
        b"\xff\xd8"
        + b"\xff\xc0\x00\x11\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
        + b"\xff\xd9"
    )


@pytest.mark.parametrize(
    ("content", "declared", "expected"),
    [
        (png_bytes(width=800, height=600), "image/png", (".png", 800, 600)),
        (jpeg_bytes(width=640, height=480), "image/jpeg", (".jpg", 640, 480)),
    ],
)
def test_inspects_supported_image_headers(
    content: bytes,
    declared: str,
    expected: tuple[str, int, int],
) -> None:
    metadata = inspect_image_bytes(content, declared_mime_type=declared)

    assert (metadata.extension, metadata.width, metadata.height) == expected


def test_rejects_mismatched_or_excessive_image_metadata() -> None:
    with pytest.raises(ImageValidationError, match="does not match"):
        inspect_image_bytes(png_bytes(), declared_mime_type="image/jpeg")
    with pytest.raises(ImageValidationError, match="limits"):
        inspect_image_bytes(
            png_bytes(width=12_001, height=1),
            declared_mime_type="image/png",
        )
    with pytest.raises(ImageValidationError, match="signature"):
        inspect_image_bytes(b"not-an-image", declared_mime_type="image/png")
