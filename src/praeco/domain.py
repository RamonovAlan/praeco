"""Small dependency-free domain primitives used during the safe bootstrap."""

from dataclasses import dataclass
from enum import StrEnum


class DraftState(StrEnum):
    """Explicit workflow states from the product specification."""

    RECEIVED = "RECEIVED"
    TEXT_GENERATING = "TEXT_GENERATING"
    TEXT_REVIEW = "TEXT_REVIEW"
    TEXT_APPROVED = "TEXT_APPROVED"
    IMAGE_GENERATING = "IMAGE_GENERATING"
    IMAGE_REVIEW = "IMAGE_REVIEW"
    READY_TO_PUBLISH = "READY_TO_PUBLISH"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    WAITING_FOR_INPUT = "WAITING_FOR_INPUT"
    CANCELLED = "CANCELLED"
    FAILED_RECOVERABLE = "FAILED_RECOVERABLE"
    PUBLISH_UNKNOWN = "PUBLISH_UNKNOWN"


@dataclass(frozen=True, slots=True)
class ApprovalRef:
    """Bind an approval to one immutable draft version."""

    draft_id: str
    version: int

    def __post_init__(self) -> None:
        if not self.draft_id.strip():
            raise ValueError("draft_id must not be blank")
        if self.version < 1:
            raise ValueError("version must be positive")
