"""Dependency-free validation for owner-provided PNG and JPEG originals."""

from dataclasses import dataclass


class ImageValidationError(ValueError):
    """Raised when uploaded bytes are not a supported bounded image."""


@dataclass(frozen=True, slots=True)
class ImageMetadata:
    """Validated technical metadata derived from image bytes, not filenames."""

    mime_type: str
    extension: str
    width: int
    height: int


_MAX_DIMENSION = 12_000
_MAX_PIXELS = 50_000_000
_JPEG_START_OF_FRAME = {
    0xC0,
    0xC1,
    0xC2,
    0xC3,
    0xC5,
    0xC6,
    0xC7,
    0xC9,
    0xCA,
    0xCB,
    0xCD,
    0xCE,
    0xCF,
}


def inspect_image_bytes(
    content: bytes,
    *,
    declared_mime_type: str,
) -> ImageMetadata:
    """Validate supported signatures, declared type, and bounded dimensions."""

    declared = declared_mime_type.partition(";")[0].strip().lower()
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        metadata = _inspect_png(content)
    elif content.startswith(b"\xff\xd8"):
        metadata = _inspect_jpeg(content)
    else:
        raise ImageValidationError("unsupported image signature")
    if declared != metadata.mime_type:
        raise ImageValidationError("declared image type does not match content")
    if (
        metadata.width > _MAX_DIMENSION
        or metadata.height > _MAX_DIMENSION
        or metadata.width * metadata.height > _MAX_PIXELS
    ):
        raise ImageValidationError("image dimensions exceed local limits")
    return metadata


def _inspect_png(content: bytes) -> ImageMetadata:
    if len(content) < 24 or content[12:16] != b"IHDR":
        raise ImageValidationError("PNG header is invalid")
    width = int.from_bytes(content[16:20], "big")
    height = int.from_bytes(content[20:24], "big")
    _require_dimensions(width=width, height=height)
    return ImageMetadata(
        mime_type="image/png",
        extension=".png",
        width=width,
        height=height,
    )


def _inspect_jpeg(content: bytes) -> ImageMetadata:
    position = 2
    while position + 4 <= len(content):
        if content[position] != 0xFF:
            raise ImageValidationError("JPEG marker is invalid")
        while position < len(content) and content[position] == 0xFF:
            position += 1
        if position >= len(content):
            break
        marker = content[position]
        position += 1
        if marker in {0x01, 0xD8, 0xD9}:
            continue
        if position + 2 > len(content):
            break
        segment_length = int.from_bytes(content[position : position + 2], "big")
        if segment_length < 2 or position + segment_length > len(content):
            raise ImageValidationError("JPEG segment is invalid")
        if marker in _JPEG_START_OF_FRAME:
            if segment_length < 7:
                raise ImageValidationError("JPEG dimensions are missing")
            height = int.from_bytes(content[position + 3 : position + 5], "big")
            width = int.from_bytes(content[position + 5 : position + 7], "big")
            _require_dimensions(width=width, height=height)
            return ImageMetadata(
                mime_type="image/jpeg",
                extension=".jpg",
                width=width,
                height=height,
            )
        position += segment_length
    raise ImageValidationError("JPEG dimensions are missing")


def _require_dimensions(*, width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise ImageValidationError("image dimensions must be positive")
