import pytest

from praeco.domain import DraftState
from praeco.workflow import (
    DraftWorkflow,
    InvalidTransitionError,
    PublicationSnapshotRef,
    StaleVersionError,
    UnauthorizedActorError,
)

OWNER_ID = 101


def text_ready_draft() -> DraftWorkflow:
    draft = DraftWorkflow(draft_id="draft-1", owner_id=OWNER_ID)
    draft.start_text_generation(actor_id=OWNER_ID)
    draft.record_text(actor_id=OWNER_ID, version=1)
    return draft


def publish_ready_draft() -> DraftWorkflow:
    draft = text_ready_draft()
    draft.approve_text(actor_id=OWNER_ID, version=1)
    draft.start_image_generation(actor_id=OWNER_ID)
    draft.record_image(actor_id=OWNER_ID, version=1, source_text_version=1)
    draft.approve_image(actor_id=OWNER_ID, version=1)
    return draft


def test_non_owner_cannot_approve_text() -> None:
    draft = text_ready_draft()

    with pytest.raises(UnauthorizedActorError):
        draft.approve_text(actor_id=999, version=1)


def test_stale_text_button_cannot_approve_new_version() -> None:
    draft = text_ready_draft()
    draft.start_text_generation(actor_id=OWNER_ID)
    draft.record_text(actor_id=OWNER_ID, version=2)

    with pytest.raises(StaleVersionError, match="stale"):
        draft.approve_text(actor_id=OWNER_ID, version=1)


def test_image_cannot_start_before_current_text_is_approved() -> None:
    draft = text_ready_draft()

    with pytest.raises(InvalidTransitionError):
        draft.start_image_generation(actor_id=OWNER_ID)


def test_manual_edit_waits_for_and_creates_the_next_text_version() -> None:
    draft = text_ready_draft()

    draft.wait_for_text_input(actor_id=OWNER_ID, version=1)
    draft.record_text(actor_id=OWNER_ID, version=2)

    assert draft.state is DraftState.TEXT_REVIEW
    assert draft.current_text_version == 2


def test_stale_text_button_cannot_open_an_input_binding() -> None:
    draft = text_ready_draft()

    with pytest.raises(StaleVersionError, match="stale"):
        draft.wait_for_text_input(actor_id=OWNER_ID, version=2)


def test_abandoned_text_input_returns_to_review_without_a_new_version() -> None:
    draft = text_ready_draft()
    draft.wait_for_text_input(actor_id=OWNER_ID, version=1)

    draft.stop_waiting_for_text_input(actor_id=OWNER_ID, version=1)

    assert draft.state is DraftState.TEXT_REVIEW
    assert draft.current_text_version == 1


def test_image_input_binding_and_return_to_text_are_version_bound() -> None:
    draft = text_ready_draft()
    draft.approve_text(actor_id=OWNER_ID, version=1)
    draft.start_image_generation(actor_id=OWNER_ID)
    draft.record_image(actor_id=OWNER_ID, version=1, source_text_version=1)

    draft.wait_for_image_input(actor_id=OWNER_ID, version=1)
    draft.stop_waiting_for_image_input(actor_id=OWNER_ID, version=1)
    draft.return_to_text_review(actor_id=OWNER_ID, image_version=1)

    assert draft.state is DraftState.TEXT_REVIEW
    assert draft.current_image_version == 1
    assert draft.approved_text_version is None
    with pytest.raises(StaleVersionError):
        draft.return_to_text_review(actor_id=OWNER_ID, image_version=2)


def test_owner_image_upload_creates_next_review_version_without_generation() -> None:
    draft = text_ready_draft()
    draft.approve_text(actor_id=OWNER_ID, version=1)
    draft.start_image_generation(actor_id=OWNER_ID)
    draft.record_image(actor_id=OWNER_ID, version=1, source_text_version=1)
    draft.wait_for_image_input(actor_id=OWNER_ID, version=1)

    draft.record_owner_image(
        actor_id=OWNER_ID,
        version=2,
        source_text_version=1,
    )

    assert draft.state is DraftState.IMAGE_REVIEW
    assert draft.current_image_version == 2
    assert draft.approved_text_version == 1
    assert draft.approved_image_version is None


def test_text_revision_invalidates_an_approved_image() -> None:
    draft = publish_ready_draft()

    draft.start_text_generation(actor_id=OWNER_ID)
    draft.record_text(actor_id=OWNER_ID, version=2)

    assert draft.state is DraftState.TEXT_REVIEW
    assert draft.approved_text_version is None
    assert draft.approved_image_version is None
    with pytest.raises(StaleVersionError, match="stale"):
        draft.approve_image(actor_id=OWNER_ID, version=1)


def test_final_click_is_bound_to_the_previewed_snapshot() -> None:
    draft = publish_ready_draft()
    stale_snapshot = PublicationSnapshotRef(
        draft_id="draft-1",
        text_version=1,
        image_version=2,
    )

    with pytest.raises(StaleVersionError, match="snapshot"):
        draft.begin_publish(actor_id=OWNER_ID, snapshot=stale_snapshot)

    current_snapshot = draft.publication_snapshot(actor_id=OWNER_ID)
    draft.begin_publish(actor_id=OWNER_ID, snapshot=current_snapshot)
    assert draft.state is DraftState.PUBLISHING


def test_double_publish_click_does_not_start_a_second_attempt() -> None:
    draft = publish_ready_draft()
    snapshot = draft.publication_snapshot(actor_id=OWNER_ID)
    draft.begin_publish(actor_id=OWNER_ID, snapshot=snapshot)

    with pytest.raises(InvalidTransitionError):
        draft.begin_publish(actor_id=OWNER_ID, snapshot=snapshot)


def test_ambiguous_publish_result_stops_automatic_retry_path() -> None:
    draft = publish_ready_draft()
    snapshot = draft.publication_snapshot(actor_id=OWNER_ID)
    draft.begin_publish(actor_id=OWNER_ID, snapshot=snapshot)

    draft.mark_publish_unknown()

    assert draft.state is DraftState.PUBLISH_UNKNOWN
    with pytest.raises(InvalidTransitionError):
        draft.begin_publish(actor_id=OWNER_ID, snapshot=snapshot)
