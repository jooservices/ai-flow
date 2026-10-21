# AI Flow v4 control plane image (control-api, control-loops). Built on the control-plane machine from a checkout.
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir uv==0.13.4
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY aiflow aiflow
COPY prompts prompts
RUN uv sync --frozen --no-dev
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
ENTRYPOINT ["aiflow4"]
