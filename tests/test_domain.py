import pytest

from praeco.domain import ApprovalRef, DraftState


def test_approval_reference_requires_a_positive_version() -> None:
    with pytest.raises(ValueError, match="positive"):
        ApprovalRef(draft_id="draft-1", version=0)


def test_publish_unknown_is_an_explicit_state() -> None:
    assert DraftState.PUBLISH_UNKNOWN.value == "PUBLISH_UNKNOWN"
