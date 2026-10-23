# AI Flow v4 control plane image (control-api, control-loops). Built on the control-plane machine from a checkout.
FROM python:3.12-slim-bookworm
ARG AIFLOW4_VERSION=4.0.0a1
ARG AIFLOW4_REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/jooservices/ai-flow" \
      org.opencontainers.image.version="$AIFLOW4_VERSION" \
      org.opencontainers.image.revision="$AIFLOW4_REVISION"
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl gnupg \
 && mkdir -p /usr/share/keyrings \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /usr/share/keyrings/postgresql.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/postgresql.gpg] http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update && apt-get install -y --no-install-recommends postgresql-client-16 \
 && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir uv==0.13.4
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY aiflow aiflow
COPY prompts prompts
COPY deploy/v4/bin/aiflow4-bootstrap /app/deploy/v4/bin/aiflow4-bootstrap
RUN uv sync --frozen --no-dev
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 AIFLOW4_VERSION=$AIFLOW4_VERSION HOME=/home/aiflow
# the documentation site that /help serves and the console Help links open
COPY docs/guide docs/guide
RUN python -m aiflow.v4.core.docsite build --out /app/docs-site --version "$AIFLOW4_VERSION"
# Not root: the code is read-only for the runtime user. The compose file can run it as another uid when the host
# directories it mounts belong to someone else (AIFLOW4_CONTAINER_UID / AIFLOW4_CONTAINER_GID).
RUN groupadd --gid 10001 aiflow && useradd --uid 10001 --gid aiflow --create-home --shell /usr/sbin/nologin aiflow
USER aiflow
ENTRYPOINT ["aiflow4"]
