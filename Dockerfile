# qr-service (Issue #53) — platform-agnostic image.
#
# Postgres (Neon) is external and all config arrives via env vars, so this image
# runs anywhere: local docker compose, AWS ECS/Fargate, Fly.io, a plain VPS. No
# cloud-proprietary API is baked in.
FROM python:3.13-slim

# uv for fast, reproducible installs straight from uv.lock.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Layer 1: deps only (cached across code changes) from the lockfile.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Layer 2: project source + install the package itself.
COPY . .
RUN uv sync --frozen --no-dev

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "quant_researcher.service.api:app", \
     "--host", "0.0.0.0", "--port", "8000"]
