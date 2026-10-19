# Docker Official Images manifest for python:3.13-slim-bookworm:
# https://github.com/docker-library/repo-info/blob/master/repos/python/remote/3.13-slim-bookworm.md
FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641

ARG VCS_REF=unknown
LABEL org.opencontainers.image.source="https://github.com/milesfoundry/gcp-platform-delivery-lab" \
      org.opencontainers.image.revision="$VCS_REF" \
      org.opencontainers.image.title="Portfolio GCP project reaper"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY recovery/reaper/requirements.lock /tmp/requirements.lock
RUN python -m pip install --require-hashes -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock

COPY scripts/gcp_cleanup_adapter.py /app/
COPY scripts/gcp_project_lifecycle_adapter.py /app/
COPY scripts/gcp_reaper_entrypoint.py /app/
COPY scripts/gcp_token_identity.py /app/
COPY scripts/project_reclaimer_runtime.py /app/
COPY scripts/recovery_controller.py /app/
COPY scripts/run_contract.py /app/
COPY scripts/run_ledger.py /app/

USER 65532:65532

ENTRYPOINT ["python", "-m", "gcp_reaper_entrypoint"]
