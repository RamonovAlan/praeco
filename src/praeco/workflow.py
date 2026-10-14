"""Pure draft workflow with version-bound approvals and publication guards."""

from dataclasses import dataclass

from praeco.domain import DraftState


class WorkflowError(Exception):
    """Base class for expected workflow rejections."""


class UnauthorizedActorError(WorkflowError):
    """Raised when an actor other than the draft owner attempts an action."""


class InvalidTransitionError(WorkflowError):
    """Raised when an action is not valid in the current state."""


class StaleVersionError(WorkflowError):
    """Raised when a callback refers to a superseded text or image version."""


@dataclass(frozen=True, slots=True)
class PublicationSnapshotRef:
    """Version identity for the exact owner-approved publication payload."""

    draft_id: str
    text_version: int
    image_version: int


@dataclass(slots=True)
class DraftWorkflow:
    """In-memory aggregate used by adapters and persistent workflow services."""

    draft_id: str
    owner_id: int
    state: DraftState = DraftState.RECEIVED
    current_text_version: int = 0
    approved_text_version: int | None = None
    current_image_version: int = 0
    image_source_text_version: int | None = None
    approved_image_version: int | None = None
    external_publication_id: str | None = None

    def __post_init__(self) -> None:
        if not self.draft_id.strip():
            raise ValueError("draft_id must not be blank")
        if self.owner_id < 1:
            raise ValueError("owner_id must be positive")

    def start_text_generation(self, *, actor_id: int) -> None:
        """Start a paid or mocked text job and invalidate dependent approvals."""

        self._authorize(actor_id)
        self._require_state(
            DraftState.RECEIVED,
            DraftState.TEXT_REVIEW,
            DraftState.TEXT_APPROVED,
            DraftState.IMAGE_REVIEW,
            DraftState.READY_TO_PUBLISH,
            DraftState.FAILED_RECOVERABLE,
            DraftState.WAITING_FOR_INPUT,
        )
        self.approved_text_version = None
        self.approved_image_version = None
        self.state = DraftState.TEXT_GENERATING

    def record_text(self, *, actor_id: int, version: int) -> None:
        """Record one new immutable text version produced by AI or manual editing."""

        self._authorize(actor_id)
        self._require_state(
            DraftState.TEXT_GENERATING,
            DraftState.TEXT_REVIEW,
            DraftState.WAITING_FOR_INPUT,
        )
        self._require_next_version("text", version, self.current_text_version)
        self.current_text_version = version
        self.approved_text_version = None
        self.approved_image_version = None
        self.state = DraftState.TEXT_REVIEW

    def mark_generation_failed(self) -> None:
        """Expose a failed provider stage for an explicit owner-controlled retry."""

        self._require_state(
            DraftState.TEXT_GENERATING,
            DraftState.IMAGE_GENERATING,
        )
        self.state = DraftState.FAILED_RECOVERABLE

    def wait_for_text_input(self, *, actor_id: int, version: int) -> None:
        """Bind a later revision or manual edit to the displayed text version."""

        self._authorize(actor_id)
        self._require_current_version("text", version, self.current_text_version)
        self._require_state(DraftState.TEXT_REVIEW)
        self.state = DraftState.WAITING_FOR_INPUT

    def stop_waiting_for_text_input(self, *, actor_id: int, version: int) -> None:
        """Return an abandoned edit binding to its unchanged review version."""

        self._authorize(actor_id)
        self._require_current_version("text", version, self.current_text_version)
        self._require_state(DraftState.WAITING_FOR_INPUT)
        self.state = DraftState.TEXT_REVIEW

    def wait_for_image_input(self, *, actor_id: int, version: int) -> None:
        """Bind a later revision or upload to the displayed image version."""

        self._authorize(actor_id)
        self._require_current_version("image", version, self.current_image_version)
        self._require_state(DraftState.IMAGE_REVIEW)
        self.state = DraftState.WAITING_FOR_INPUT

    def stop_waiting_for_image_input(self, *, actor_id: int, version: int) -> None:
        """Return an abandoned image binding to its unchanged review version."""

        self._authorize(actor_id)
        self._require_current_version("image", version, self.current_image_version)
        self._require_state(DraftState.WAITING_FOR_INPUT)
        self.state = DraftState.IMAGE_REVIEW

    def return_to_text_review(self, *, actor_id: int, image_version: int) -> None:
        """Return from an exact image preview to editable text review."""

        self._authorize(actor_id)
        self._require_current_version(
            "image", image_version, self.current_image_version
        )
        self._require_state(DraftState.IMAGE_REVIEW)
        self.approved_text_version = None
        self.approved_image_version = None
        self.state = DraftState.TEXT_REVIEW

    def approve_text(self, *, actor_id: int, version: int) -> None:
        """Approve only the currently displayed text version."""

        self._authorize(actor_id)
        self._require_current_version("text", version, self.current_text_version)
        self._require_state(DraftState.TEXT_REVIEW)
        self.approved_text_version = version
        self.state = DraftState.TEXT_APPROVED

    def start_image_generation(self, *, actor_id: int) -> None:
        """Start an image job only for an approved current text version."""

        self._authorize(actor_id)
        self._require_state(
            DraftState.TEXT_APPROVED,
            DraftState.IMAGE_REVIEW,
            DraftState.READY_TO_PUBLISH,
            DraftState.FAILED_RECOVERABLE,
            DraftState.WAITING_FOR_INPUT,
        )
        if self.approved_text_version != self.current_text_version:
            raise InvalidTransitionError("current text is not approved")
        self.approved_image_version = None
        self.state = DraftState.IMAGE_GENERATING

    def record_image(
        self,
        *,
        actor_id: int,
        version: int,
        source_text_version: int,
    ) -> None:
        """Record an image tied to the exact approved text that produced its brief."""

        self._authorize(actor_id)
        self._require_state(DraftState.IMAGE_GENERATING)
        self._require_next_version("image", version, self.current_image_version)
        if source_text_version != self.current_text_version:
            raise StaleVersionError("image was generated for a stale text version")
        if self.approved_text_version != source_text_version:
            raise InvalidTransitionError("image source text is not approved")
        self.current_image_version = version
        self.image_source_text_version = source_text_version
        self.approved_image_version = None
        self.state = DraftState.IMAGE_REVIEW

    def record_owner_image(
        self,
        *,
        actor_id: int,
        version: int,
        source_text_version: int,
    ) -> None:
        """Store one owner upload bound to the approved current text."""

        self._authorize(actor_id)
        self._require_state(DraftState.WAITING_FOR_INPUT)
        self._require_next_version("image", version, self.current_image_version)
        if source_text_version != self.current_text_version:
            raise StaleVersionError("owner image belongs to a stale text version")
        if self.approved_text_version != source_text_version:
            raise InvalidTransitionError("owner image source text is not approved")
        self.current_image_version = version
        self.image_source_text_version = source_text_version
        self.approved_image_version = None
        self.state = DraftState.IMAGE_REVIEW

    def approve_image(self, *, actor_id: int, version: int) -> None:
        """Approve only a current image that belongs to the approved text."""

        self._authorize(actor_id)
        self._require_current_version("image", version, self.current_image_version)
        if self.image_source_text_version != self.current_text_version:
            raise StaleVersionError("image belongs to a stale text version")
        self._require_state(DraftState.IMAGE_REVIEW)
        if self.image_source_text_version != self.approved_text_version:
            raise InvalidTransitionError("image source text is not approved")
        self.approved_image_version = version
        self.state = DraftState.READY_TO_PUBLISH

    def publication_snapshot(self, *, actor_id: int) -> PublicationSnapshotRef:
        """Return the immutable version identity shown in the final preview."""

        self._authorize(actor_id)
        self._require_state(DraftState.READY_TO_PUBLISH)
        if self.approved_text_version != self.current_text_version:
            raise InvalidTransitionError("current text is not approved")
        if self.approved_image_version != self.current_image_version:
            raise InvalidTransitionError("current image is not approved")
        if self.image_source_text_version != self.current_text_version:
            raise InvalidTransitionError("approved image does not match current text")
        return PublicationSnapshotRef(
            draft_id=self.draft_id,
            text_version=self.current_text_version,
            image_version=self.current_image_version,
        )

    def begin_publish(
        self,
        *,
        actor_id: int,
        snapshot: PublicationSnapshotRef,
    ) -> None:
        """Consume the final click for exactly the snapshot that was previewed."""

        current_snapshot = self.publication_snapshot(actor_id=actor_id)
        if snapshot != current_snapshot:
            raise StaleVersionError("publication snapshot is stale")
        self.state = DraftState.PUBLISHING

    def mark_published(self, *, external_id: str) -> None:
        """Store a confirmed provider ID after a successful create response."""

        self._require_state(DraftState.PUBLISHING)
        if not external_id.strip():
            raise ValueError("external_id must not be blank")
        self.external_publication_id = external_id
        self.state = DraftState.PUBLISHED

    def mark_publish_unknown(self) -> None:
        """Stop automatic retries after an ambiguous external publish outcome."""

        self._require_state(DraftState.PUBLISHING)
        self.state = DraftState.PUBLISH_UNKNOWN

    def mark_publish_failed(self) -> None:
        """Return a definitively failed create attempt to explicit owner control."""

        self._require_state(DraftState.PUBLISHING)
        self.state = DraftState.READY_TO_PUBLISH

    def cancel(self, *, actor_id: int) -> None:
        """Cancel an unfinished draft without deleting its versions."""

        self._authorize(actor_id)
        self._require_state(
            DraftState.RECEIVED,
            DraftState.TEXT_GENERATING,
            DraftState.TEXT_REVIEW,
            DraftState.TEXT_APPROVED,
            DraftState.IMAGE_GENERATING,
            DraftState.IMAGE_REVIEW,
            DraftState.READY_TO_PUBLISH,
            DraftState.WAITING_FOR_INPUT,
            DraftState.FAILED_RECOVERABLE,
        )
        self.state = DraftState.CANCELLED

    def _authorize(self, actor_id: int) -> None:
        if actor_id != self.owner_id:
            raise UnauthorizedActorError("actor is not the draft owner")

    def _require_state(self, *allowed: DraftState) -> None:
        if self.state not in allowed:
            expected = ", ".join(state.value for state in allowed)
            raise InvalidTransitionError(
                f"state {self.state.value} does not allow this action; expected {expected}"
            )

    @staticmethod
    def _require_next_version(kind: str, version: int, current: int) -> None:
        if version != current + 1:
            raise StaleVersionError(
                f"{kind} version must be {current + 1}; received {version}"
            )

    @staticmethod
    def _require_current_version(kind: str, version: int, current: int) -> None:
        if version != current:
            raise StaleVersionError(
                f"{kind} version {version} is stale; current version is {current}"
            )
