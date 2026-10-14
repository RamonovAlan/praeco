# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

FROM python:3.13.16-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641

ARG PRAECO_UID=10001
ARG PRAECO_GID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

RUN python -m pip install --no-cache-dir "cryptography==46.0.7"

RUN groupadd --gid "${PRAECO_GID}" praeco \
    && useradd --uid "${PRAECO_UID}" --gid "${PRAECO_GID}" \
        --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin praeco \
    && install -d -o praeco -g praeco -m 0700 \
        /var/lib/praeco /var/lib/praeco/data /var/lib/praeco/media \
        /var/backups/praeco

WORKDIR /app

COPY --chown=praeco:praeco src/ /app/src/
COPY --chown=praeco:praeco migrations/ /app/migrations/
COPY --chown=praeco:praeco prompts/ /app/prompts/
COPY --chown=praeco:praeco config/ /app/config/

USER praeco:praeco

STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PRAECO_HEALTH_PORT','8080')+'/health/ready',timeout=3).close()"]

ENTRYPOINT ["python", "-m", "praeco.container_entrypoint"]
