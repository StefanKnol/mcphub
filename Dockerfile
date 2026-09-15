# syntax=docker/dockerfile:1
FROM python:3.13-slim AS build

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

WORKDIR /app
# Dependencies resolve from the lockfile alone, so editing source does not
# invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src ./src
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev


FROM python:3.13-slim

RUN useradd --system --uid 10001 --create-home mcphub
COPY --from=build --chown=mcphub:mcphub /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    MCPHUB_DATA_DIR=/data \
    MCPHUB_HOST=0.0.0.0 \
    MCPHUB_PORT=8080

# hub.db and master.key live here. Back this up; losing master.key means every
# stored backend credential becomes unreadable.
VOLUME /data
RUN mkdir -p /data && chown mcphub:mcphub /data

USER mcphub
WORKDIR /app
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"MCPHUB_PORT\"]}/healthz').read()"

CMD ["python", "-m", "mcphub"]
