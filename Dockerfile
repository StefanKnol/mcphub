# syntax=docker/dockerfile:1
ARG PYTHON_VERSION=3.13

FROM python:${PYTHON_VERSION}-slim AS build

# Pinned rather than :latest, so a rebuild of an old commit resolves the same
# toolchain it was tested with.
COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

WORKDIR /app
# Dependencies resolve from the lockfile alone, so editing source does not
# invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src ./src
COPY README.md LICENSE ./
# Installs the project itself, which is what publishes the `mcphub.plugins`
# entry points. Without this step the plugin registry finds nothing and the
# hub starts with no backends available.
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev


FROM python:${PYTHON_VERSION}-slim

LABEL org.opencontainers.image.source="https://github.com/StefanKnol/mcphub" \
      org.opencontainers.image.description="Self-hosted MCP platform: backends as plugins, one OAuth-protected MCP endpoint per backend." \
      org.opencontainers.image.licenses="MIT"

# /data must be created before the VOLUME instruction: anything written to that
# path afterwards lands in a layer the volume discards.
RUN mkdir -p /data
VOLUME /data

COPY --from=build /app /app
COPY docker-entrypoint.py /usr/local/bin/docker-entrypoint.py

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCPHUB_DATA_DIR=/data \
    MCPHUB_HOST=0.0.0.0 \
    MCPHUB_PORT=8080 \
    PUID=99 \
    PGID=100

# Starts as root so the entrypoint can align /data with PUID/PGID, then drops
# to that user before running anything. Pass `--user` to skip that entirely.
WORKDIR /app
EXPOSE 8080

# No nested double quotes: the shell form of CMD would otherwise terminate the
# string early and the healthcheck would fail in a way that looks like the app.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request;p=os.environ.get('MCPHUB_PORT','8080');urllib.request.urlopen('http://127.0.0.1:'+p+'/healthz').read()"

ENTRYPOINT ["python", "/usr/local/bin/docker-entrypoint.py"]
