# R-TOOL-5: multi-stage, no uv in the runtime image, non-root user.
#
# Lighter than the monolith's image: no LibreOffice, no texlive, no pandoc, no
# Node/pptxgenjs, no Playwright/Chromium — those only ever served the
# report/PPTX rendering path, which now lives in render-report's own (much
# heavier) image. This service still needs WeasyPrint's runtime libraries
# because app/adapters/openai_files.py imports weasyprint at module scope.

FROM ghcr.io/astral-sh/uv:0.12.5 AS uv

# ---------------------------------------------------------------- builder ---
FROM python:3.14-slim AS builder
COPY --from=uv /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    # The cache mount and /app/.venv are different filesystems; copy, don't hardlink.
    UV_LINK_MODE=copy \
    # The base image already provides 3.14 — never let uv fetch its own.
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Layer 1: dependencies only. Invalidated only when the lock/manifest changes.
# Inverting this with the source copy would rebuild the slow layer on every
# code edit.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev

# Layer 2: the project itself. Invalidated on source change.
# --frozen fails the build on a stale lockfile rather than silently
# re-resolving; freshness was already verified by CI's `uv sync --locked`.
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ---------------------------------------------------------------- runtime ---
FROM python:3.14-slim AS runtime

# Pango is all WeasyPrint 69 needs — it dropped the gdk-pixbuf/cairo
# requirement in v53, and the old libgdk-pixbuf2.0-0 name no longer exists on
# trixie (this base image) anyway.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangocairo-1.0-0 libffi-dev \
        shared-mime-info curl && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN groupadd -r app && useradd -r -g app app

COPY --from=builder --chown=app:app /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY --chown=app:app src /app/src
COPY --chown=app:app db_migrations /app/db_migrations
COPY --chown=app:app alembic.ini /app/alembic.ini

USER app
EXPOSE 8000

# `uv run` is deliberately not the entrypoint: it re-checks the environment on
# every invocation, and uv is not in this image anyway. The venv is on PATH.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
