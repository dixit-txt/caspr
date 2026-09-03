"""entry_point.py — HTTP shim (NOT the real rendering pipeline).

The real report_util (MD -> HTML -> PDF, Playwright render, cost tracking)
now lives in report-render-service. This module exists only so that
`api.py`'s existing
`from src.core.report_util.entry_point import process_report_cards, generate_report_output`
keeps working unchanged. `generate_report_output` reproduces the original
async signature exactly and forwards the call over HTTP to
report-render-service's `POST /internal/render/report-output`.

`process_report_cards` is intentionally NOT shimmed: in the monolith it was
only ever called internally by `generate_report_output` itself (see
report-render-service/README.md → "Known follow-up work" #2), never
directly from api.py, so there is no call site here that needs it. It is
re-exported as a stub that raises, so an import succeeds but a stray call
fails loudly instead of silently doing nothing.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

import httpx

from src.config.constants import REPORT_RENDER_SERVICE_BASE_URL
from src.config.log_helper import setup_logging
from src.core.common.internal_auth import internal_headers

logger = setup_logging(__name__)

_TIMEOUT = httpx.Timeout(connect=15.0, read=1800.0, write=60.0, pool=15.0)


async def generate_report_output(
    report_cards,
    table_id_map,
    user_id: str,
    user_name: str,
    report_id: str,
    base_filename: str,
    report_title: str,
    output_type: str,
    existing_s3_uri: dict,
    chat_id: str = "",
    chat_title: str = "",
    version: int = 1,
    existing_poster_url: Optional[str] = None,
    report_type: str = "study",
) -> Dict[str, Any]:
    payload = {
        "report_cards": report_cards,
        "table_id_map": table_id_map,
        "user_id": user_id,
        "user_name": user_name,
        "report_id": report_id,
        "base_filename": base_filename,
        "report_title": report_title,
        "output_type": output_type,
        "existing_s3_uri": existing_s3_uri,
        "chat_id": chat_id,
        "chat_title": chat_title,
        "version": version,
        "existing_poster_url": existing_poster_url,
        "report_type": report_type,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{REPORT_RENDER_SERVICE_BASE_URL}/internal/render/report-output",
            json=payload,
            headers=internal_headers(),
        )
    resp.raise_for_status()
    return restore_report_output_datetimes(resp.json())


def restore_report_output_datetimes(payload: Dict[str, Any]) -> Dict[str, Any]:
    """JSON cannot carry datetime; parse ISO strings back to the old contract.

    api.py still calls ``report_generation_time.isoformat()``. After the
    render-service split that field arrives as a string and a successful
    PDF was being marked ERROR_GENERATION_REPORT.
    """
    data = payload.get("data")
    if isinstance(data, dict) and "report_generation_time" in data:
        data["report_generation_time"] = parse_iso_datetime(
            data["report_generation_time"]
        )
    return payload


def parse_iso_datetime(value: Any) -> Any:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def process_report_cards(*args, **kwargs):
    raise NotImplementedError(
        "process_report_cards was not called directly from api.py in the "
        "monolith (only internally by generate_report_output) and has no "
        "HTTP shim here. If you have a new call site that needs it, add a "
        "route for it in report-render-service and a matching client call "
        "here — see report-render-service/README.md."
    )
