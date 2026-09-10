"""one_pager.py — HTTP shim (NOT the real infographic generator).

The real implementation (weasyprint/PIL/Gemini image generation) now lives
in report-render-service. This reproduces `generate_one_pager`'s exact
original synchronous signature and return shape — `api.py`'s
`from app.research.infographics.one_pager import generate_one_pager` needed no
changes — but forwards the call over HTTP to report-render-service's
`POST /internal/render/infographic` instead of rendering in-process.

The original was sync and called via `run_in_threadpool(lambda:
generate_one_pager(...))` in api.py (blocking work off the event loop).
This shim keeps that same contract: it's a blocking `httpx` call, still
meant to be run through `run_in_threadpool` by its caller exactly as before.
"""

from __future__ import annotations

import httpx

from app.core.constants import REPORT_RENDER_SERVICE_BASE_URL
from app.core.logging import setup_logging
from app.internal.dependencies import internal_headers

logger = setup_logging(__name__)

_TIMEOUT = httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=15.0)


def generate_one_pager(
    user_id: str | None,
    user_name: str | None,
    report_id: str | None,
    report_markdown: str,
    report_title: str,
    report_subtitle: str,
    poster_image_url: str | None,
    report_date: str,
    chat_id: str,
    chat_title: str,
    version: int,
    report_generation_time: str,
) -> tuple[str, str | None]:
    payload = {
        "user_id": user_id,
        "user_name": user_name,
        "report_id": report_id,
        "report_markdown": report_markdown,
        "report_title": report_title,
        "report_subtitle": report_subtitle,
        "poster_image_url": poster_image_url,
        "report_date": report_date,
        "chat_id": chat_id,
        "chat_title": chat_title,
        "version": version,
        "report_generation_time": report_generation_time,
    }
    resp = httpx.post(
        f"{REPORT_RENDER_SERVICE_BASE_URL}/internal/render/infographic",
        json=payload,
        headers=internal_headers(),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["infographic_s3_uri"], data.get("poster_image_url")
