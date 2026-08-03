# syntax=docker/dockerfile:1.7
FROM python:3.11.15-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba AS builder

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN pip install --no-cache-dir --disable-pip-version-check uv==0.11.21

# Workspace metadata changes far less often than application source.
COPY pyproject.toml uv.lock ./
COPY packages/fetch/pyproject.toml packages/fetch/pyproject.toml
COPY packages/portal/pyproject.toml packages/portal/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package portal --no-install-workspace

COPY packages/fetch packages/fetch
COPY packages/portal packages/portal
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package portal --no-editable

FROM python:3.11.15-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --system portal && useradd --system --gid portal --home-dir /app portal

COPY --from=builder /app/.venv /app/.venv
RUN mkdir -p /app/.data/objects && chown -R portal:portal /app

USER portal

EXPOSE 8000

CMD ["sh", "-ec", "export PORTAL_TLS_TERMINATED_UPSTREAM=true && portal-migrate && portal-provision --admin-email \"$PORTAL_BOOTSTRAP_ADMIN_EMAIL\" --admin-password-env PORTAL_BOOTSTRAP_ADMIN_PASSWORD --team-name \"$PORTAL_BOOTSTRAP_TEAM_NAME\" --team-slug \"$PORTAL_BOOTSTRAP_TEAM_SLUG\" && exec portal"]
