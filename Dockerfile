# Lighter than the monolith's image: no LibreOffice, no texlive, no pandoc,
# no Node/pptxgenjs, no Playwright/Chromium — those only ever served the
# report/PPTX rendering path, which now lives in report-render-service's
# own (much heavier) image. This service still needs weasyprint's runtime
# libs because openai_file_utils.py imports weasyprint at module scope and
# uses it to render uploaded HTML to PDF — unrelated to report_util.
FROM python:3.14-slim

# Pango is all WeasyPrint 69 needs — it dropped the gdk-pixbuf/cairo
# requirement in v53, and the old `libgdk-pixbuf2.0-0` name no longer exists
# on trixie (this base image) anyway. Verified by rendering a PDF with only
# these installed.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangocairo-1.0-0 libffi-dev \
        shared-mime-info curl && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# uv drives dependency installation from uv.lock (see pyproject.toml).
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Precompile to .pyc at build time so the first request isn't paying for it.
    UV_COMPILE_BYTECODE=1 \
    # The cache mount and /app/.venv are different filesystems; copy, don't hardlink.
    UV_LINK_MODE=copy \
    # The base image already provides 3.14 — never let uv fetch its own.
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies resolve in their own layer, so editing application code does
# not invalidate the (slow) install. --frozen fails the build if uv.lock is
# out of date with pyproject.toml rather than silently re-resolving; --no-dev
# leaves the `dev` dependency group (ipykernel) out of the runtime image.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

# Put the project venv ahead of the system interpreter for CMD and for anyone
# who `docker exec`s in.
ENV PATH="/app/.venv/bin:$PATH" \
    VIRTUAL_ENV=/app/.venv

EXPOSE 8000

# Invoke uvicorn directly rather than `python main.py`: that entrypoint passes
# reload=True, which in a container spawns a filesystem-watching reloader that
# re-imports the whole app in a child process — double the memory and startup
# time, for a code-reload feature an immutable image can never use. main.py's
# __main__ block is left alone for local development.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
