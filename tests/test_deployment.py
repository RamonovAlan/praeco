from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_docker_build_context_is_deny_by_default() -> None:
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert dockerignore.splitlines()[2] == "*"
    assert "!src/**" in dockerignore
    assert "!migrations/**" in dockerignore
    assert "!.env" not in dockerignore
    assert "**/__pycache__/" in dockerignore
    assert "**/*.py[cod]" in dockerignore


def test_container_contract_is_non_root_and_health_checked() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert (
        "FROM python:3.13.16-slim-bookworm@sha256:"
        "a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641"
        in dockerfile
    )
    assert "USER praeco:praeco" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "/health/ready" in dockerfile
    assert 'ENTRYPOINT ["python", "-m", "praeco.container_entrypoint"]' in dockerfile


def test_compose_contract_persists_state_without_publishing_ports() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert 'user: "10001:10001"' in compose
    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert "- ALL" in compose
    assert "praeco-state:/var/lib/praeco" in compose
    assert 'PRAECO_PROVIDER_MODE: "${PRAECO_PROVIDER_MODE:-mock}"' in compose
    assert 'PRAECO_AI_API_KEY: "${PRAECO_AI_API_KEY:-}"' in compose
    assert 'PRAECO_PUBLISHING_ENABLED: "false"' in compose
    assert "PRAECO_LINKEDIN_CLIENT_SECRET" not in compose
    assert "ports:" not in compose


def test_backup_profile_is_non_root_offline_and_separate_from_state() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    backup = compose.split("  backup:", maxsplit=1)[1]
    assert "- maintenance" in backup
    assert 'user: "10001:10001"' in backup
    assert "network_mode: none" in backup
    assert "PRAECO_BACKUP_ENCRYPTION_KEY" in backup
    assert "praeco-state:/var/lib/praeco" in backup
    assert "praeco-backups:/var/backups/praeco" in backup
    assert "PRAECO_TELEGRAM_BOT_TOKEN" not in backup
    assert "PRAECO_AI_API_KEY" not in backup
