"""U7: a confirmed brief on due diligence produces a brief.

Due diligence was study-only by prompt wording and by two defensive
normalizations in the routers, not by any pipeline limit. A tier the popup
offers has to be a tier the pipeline can produce, so all of those are gone.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from app.chats import router_sessions
from app.reports import router_reports
from app.research.agent import _retrieve as agent_retrieve
from app.research.agent.model import Casper, _normalize_report_config
from app.research.prompts import prompt_utils

pytestmark = pytest.mark.unit


@pytest.fixture
def casper(monkeypatch) -> Casper:
    monkeypatch.setattr(agent_retrieve, "get_stream_writer", lambda: lambda event: None)
    instance = Casper(
        {
            "user_name": "tester",
            "chat_id": "chat-under-test",
            "user_id": "user-under-test",
            "user_previous_messages": [],
            "web_search": False,
        }
    )

    async def no_upload(*args: Any, **kwargs: Any) -> None:
        return None

    async def no_refresh() -> None:
        return None

    monkeypatch.setattr(instance, "_upload_report_layout_to_s3_background", no_upload)
    monkeypatch.setattr(instance, "_await_layout_refresh", no_refresh)
    return instance


async def test_a_confirmed_brief_survives_due_diligence_retrieval(casper: Casper) -> None:
    """AE3: brief, not a rejection and not a silent upgrade to study."""
    casper.report_config_submission = _normalize_report_config({"report_tier": "brief"})

    await casper.retrieve(
        user_instructions="Investigate Acme Ltd.",
        report_layout="# Acme Ltd\n\n## Ownership\n\n## Litigation\n",
        report_language="English",
        report_title="Acme Ltd",
        domain_name="due_diligence",
        report_type="study",
    )

    assert casper.retrieve_config["report_type"] == "brief"
    assert casper.retrieve_config["domain_name"] == "due_diligence"


def test_the_stream_loop_no_longer_upgrades_a_due_diligence_brief() -> None:
    """The two normalizations in the chat stream loop are gone."""
    source = inspect.getsource(router_sessions.chat_producer)
    assert "Normalized invalid due_diligence report_type" not in source
    assert 'domain_name == "due_diligence" and selected_report_type == "brief"' not in source
    assert 'domain_name == "due_diligence" and report_type_val == "brief"' not in source


def test_reading_back_a_stored_due_diligence_brief_does_not_correct_it() -> None:
    """R11: a persisted brief reads back as brief at output-generation time."""
    source = inspect.getsource(router_reports)
    assert "Corrected invalid persisted due_diligence report_type" not in source
    assert 'report.domain_name == "due_diligence" and report_type == "brief"' not in source


@pytest.mark.parametrize("prompt_name", ["SYSTEM_MESSAGE", "DRL_PROMPT"])
def test_the_prompts_no_longer_refuse_a_brief_due_diligence(prompt_name: str) -> None:
    prompt = getattr(prompt_utils, prompt_name)
    forbidden = [
        "Due Diligence reports MUST always be STUDY",
        "Due Diligence is only available as a STUDY",
        "must never be generated as BRIEF",
        "not eligible for brief mode",
        "not eligible for BRIEF",
        'Do NOT use "brief" when domain_name is "due_diligence"',
    ]
    found = [phrase for phrase in forbidden if phrase in prompt]
    assert not found, f"{prompt_name} still refuses a brief due diligence: {found}"


def test_no_prompt_in_the_module_refuses_a_brief_due_diligence() -> None:
    """Catch-all: the rule was stated in several prompts, not just one."""
    offenders: list[str] = []
    for name, value in vars(prompt_utils).items():
        if not isinstance(value, str) or name.startswith("__"):
            continue
        if "only available as a STUDY" in value or "MUST always be STUDY" in value:
            offenders.append(name)
    assert not offenders, f"prompts still block a brief due diligence: {offenders}"
