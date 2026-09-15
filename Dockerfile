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

RUN useradd --system --uid 10001 --create-home mcphub

# /data must be created and owned *before* the VOLUME instruction. Declaring
# the volume first makes any later change to that path part of a layer the
# volume discards, so the chown would silently not apply and the container
# would fail to write hub.db as a non-root user.
RUN mkdir -p /data && chown mcphub:mcphub /data
VOLUME /data

COPY --from=build --chown=mcphub:mcphub /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCPHUB_DATA_DIR=/data \
    MCPHUB_HOST=0.0.0.0 \
    MCPHUB_PORT=8080

USER mcphub
WORKDIR /app
EXPOSE 8080

# No nested double quotes: the shell form of CMD would otherwise terminate the
# string early and the healthcheck would fail in a way that looks like the app.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request;p=os.environ.get('MCPHUB_PORT','8080');urllib.request.urlopen('http://127.0.0.1:'+p+'/healthz').read()"

CMD ["python", "-m", "mcphub"]
