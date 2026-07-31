FROM python:3.11-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

RUN pip install --no-cache-dir uv==0.11.21

COPY . .
RUN uv sync --locked --no-dev --package osiptel-jobs

EXPOSE 8000

CMD ["osiptel-jobs"]
