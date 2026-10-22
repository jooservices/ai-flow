# AI Flow v4 control plane image (control-api, control-loops). Built on the control-plane machine from a checkout.
FROM python:3.12-slim-bookworm
ARG AIFLOW4_VERSION=4.0.0a1
ARG AIFLOW4_REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/jooservices/ai-flow" \
      org.opencontainers.image.version="$AIFLOW4_VERSION" \
      org.opencontainers.image.revision="$AIFLOW4_REVISION"
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir uv==0.13.4
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY aiflow aiflow
COPY prompts prompts
COPY deploy/v4/bin/aiflow4-bootstrap /app/deploy/v4/bin/aiflow4-bootstrap
RUN uv sync --frozen --no-dev
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 AIFLOW4_VERSION=$AIFLOW4_VERSION
# the documentation site that /help serves and the console Help links open
COPY docs/guide docs/guide
RUN python -m aiflow.v4.core.docsite build --out /app/docs-site --version "$AIFLOW4_VERSION"
ENTRYPOINT ["aiflow4"]
