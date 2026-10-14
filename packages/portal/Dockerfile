# syntax=docker/dockerfile:1.28.0@sha256:bb22d9815c728170f72750f4e5b0d672e06176142e1d602c7e66c050100b7e5b

FROM python:3.15.0-slim-trixie@sha256:ea4f1f290e071bf7d28199cfb3e09f5dbad31b50e9c32fd58f2f2d7be915f3ae AS builder

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

RUN pip install --no-cache-dir --disable-pip-version-check uv==0.13.0

COPY packages/core/pyproject.toml packages/core/pyproject.toml
COPY packages/portal/pyproject.toml packages/portal/pyproject.toml
COPY packages/portal/uv.lock packages/portal/uv.lock

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --project packages/portal --locked --no-dev \
      --no-install-project --no-install-local

COPY packages/core packages/core
COPY packages/portal packages/portal

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --project packages/portal --locked --no-dev --no-editable

FROM python:3.15.0-slim-trixie@sha256:ea4f1f290e071bf7d28199cfb3e09f5dbad31b50e9c32fd58f2f2d7be915f3ae

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORTAL_TLS_TERMINATED_UPSTREAM=true \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --system portal \
    && useradd --system --gid portal --home-dir /app portal

COPY --chown=portal:portal --from=builder /app/.venv /app/.venv

# A new named volume mounted here copies this directory's owner and mode, which
# is how the non-root init service can write the master key into it.
RUN install -d -o portal -g portal -m 700 /app/.data/keys

USER portal

EXPOSE 8000

CMD ["python", "-m", "portal.web.app"]
