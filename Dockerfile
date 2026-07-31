FROM ghcr.io/astral-sh/uv:0.11.21-python3.11-bookworm-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY . .
RUN uv sync --locked --no-dev --package osiptel-jobs

EXPOSE 8000

CMD ["osiptel-jobs"]
