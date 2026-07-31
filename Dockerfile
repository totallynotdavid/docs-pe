FROM python:3.11-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

RUN pip install --no-cache-dir uv==0.11.21

COPY . .
RUN uv sync --locked --no-dev --package osiptel-portal

EXPOSE 8000

CMD ["sh", "-ec", "portal-migrate && portal-provision --admin-email \"$PORTAL_BOOTSTRAP_ADMIN_EMAIL\" --admin-password-env PORTAL_BOOTSTRAP_ADMIN_PASSWORD --team-name \"$PORTAL_BOOTSTRAP_TEAM_NAME\" --team-slug \"$PORTAL_BOOTSTRAP_TEAM_SLUG\" && exec osiptel-portal"]
