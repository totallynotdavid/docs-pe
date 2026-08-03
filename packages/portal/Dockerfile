# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

FROM python:3.14.6-slim-bookworm@sha256:86f975aca15cf04a40b399eebede9aea7c82eae084d1f1a0a6ef6bcaae871a30 AS builder

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN pip install --no-cache-dir --disable-pip-version-check uv==0.12.0

# The workspace lock requires every member's project metadata.
COPY pyproject.toml uv.lock ./
COPY packages/browser/pyproject.toml packages/browser/pyproject.toml
COPY packages/capture/pyproject.toml packages/capture/pyproject.toml
COPY packages/fetch/pyproject.toml packages/fetch/pyproject.toml
COPY packages/portal/pyproject.toml packages/portal/pyproject.toml

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package portal --no-install-workspace

COPY packages/fetch packages/fetch
COPY packages/portal packages/portal

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package portal --no-editable

FROM python:3.14.6-slim-bookworm@sha256:86f975aca15cf04a40b399eebede9aea7c82eae084d1f1a0a6ef6bcaae871a30

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORTAL_TLS_TERMINATED_UPSTREAM=true \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --system portal \
    && useradd --system --gid portal --home-dir /app portal

COPY --chown=portal:portal --from=builder /app/.venv /app/.venv

RUN mkdir -p /app/.data/objects \
    && chown portal:portal /app/.data /app/.data/objects

USER portal

EXPOSE 8000

CMD ["sh", "-ec", "portal-migrate && portal-provision --admin-email \"$PORTAL_BOOTSTRAP_ADMIN_EMAIL\" --admin-password-env PORTAL_BOOTSTRAP_ADMIN_PASSWORD --team-name \"$PORTAL_BOOTSTRAP_TEAM_NAME\" --team-slug \"$PORTAL_BOOTSTRAP_TEAM_SLUG\" && exec portal"]
