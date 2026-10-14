from http import HTTPStatus

from praeco.health import HealthState


def test_health_routes_expose_only_bounded_technical_state() -> None:
    state = HealthState(schema_version=9)
    live_status, live_body = state.response_for_path("/health/live")
    ready_status, ready_body = state.response_for_path("/health/ready")

    assert live_status == HTTPStatus.OK
    assert live_body == {"service": "praeco", "status": "ok"}
    assert ready_status == HTTPStatus.SERVICE_UNAVAILABLE
    assert ready_body == {
        "service": "praeco",
        "status": "not_ready",
        "runtime": "starting",
        "provider_mode": "mock",
        "publishing": "disabled",
        "schema_version": 9,
        "last_tick_at": None,
        "error_category": None,
    }
    assert not {
        "owner_id",
        "telegram_token",
        "database_path",
        "media_directory",
    } & ready_body.keys()

    state.mark_ready()
    ready_status, ready_body = state.response_for_path("/health/ready")
    assert ready_status == HTTPStatus.OK
    assert ready_body["status"] == "ok"
    assert ready_body["runtime"] == "running"

    state.record_error("telegram_unavailable")
    ready_status, ready_body = state.response_for_path("/health/ready")
    assert ready_status == HTTPStatus.SERVICE_UNAVAILABLE
    assert ready_body["error_category"] == "telegram_unavailable"

    missing_status, missing_body = state.response_for_path("/private")
    assert missing_status == HTTPStatus.NOT_FOUND
    assert missing_body == {"service": "praeco", "status": "not_found"}


def test_health_tick_and_shutdown_transitions() -> None:
    state = HealthState(schema_version=9)

    state.record_tick()
    running = state.snapshot()
    assert running.status == "ok"
    assert running.runtime == "running"
    assert running.last_tick_at is not None

    state.mark_stopping()
    stopping = state.snapshot()
    assert stopping.status == "not_ready"
    assert stopping.runtime == "stopping"
