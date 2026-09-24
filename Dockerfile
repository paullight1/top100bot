FROM python:3.11-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev

ENV PYTHONUNBUFFERED=1 \
    WHATSAPP_SESSION_PATH=/app/data/session.db

VOLUME ["/app/data"]

CMD ["uv", "run", "--no-dev", "unipod-bot"]
