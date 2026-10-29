# AI Flow v4 control plane image (control-api, control-loops). Built on the control-plane machine from a checkout.
# Keep uv in a build-only stage so the runtime image does not carry the package manager.
FROM ghcr.io/astral-sh/uv:0.13.4 AS uv-build

FROM ubuntu:24.04
ARG AIFLOW4_VERSION=4.0.0a1
ARG AIFLOW4_REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/jooservices/ai-flow" \
      org.opencontainers.image.version="$AIFLOW4_VERSION" \
      org.opencontainers.image.revision="$AIFLOW4_REVISION"
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
 && apt-get install -y --no-install-recommends ca-certificates curl git postgresql-client-16 python3.12 python3.12-venv \
 && rm -rf /var/lib/apt/lists/*
COPY --from=uv-build /uv /uvx /usr/local/bin/
ENV UV_PYTHON=/usr/bin/python3.12
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY aiflow aiflow
COPY prompts prompts
COPY deploy/v4/bin/aiflow4-bootstrap /app/deploy/v4/bin/aiflow4-bootstrap
RUN uv sync --frozen --no-dev \
 && rm -f /usr/local/bin/uv /usr/local/bin/uvx
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 AIFLOW4_VERSION=$AIFLOW4_VERSION HOME=/home/aiflow
# the documentation site that /help serves and the console Help links open
COPY docs/guide docs/guide
COPY docs/v4 docs/v4
RUN python -m aiflow.v4.core.docsite build --out /app/docs-site --version "$AIFLOW4_VERSION"
# Not root: the code is read-only for the runtime user. The compose file can run it as another uid when the host
# directories it mounts belong to someone else (AIFLOW4_CONTAINER_UID / AIFLOW4_CONTAINER_GID).
RUN groupadd --gid 10001 aiflow && useradd --uid 10001 --gid aiflow --create-home --shell /usr/sbin/nologin aiflow
USER aiflow
ENTRYPOINT ["aiflow4"]
