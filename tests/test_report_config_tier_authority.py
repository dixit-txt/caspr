"""U6: the confirmed tier is the only thing that decides brief versus study.

These drive the real ``retrieve`` rather than a stand-in, because the whole
point of the unit is which of several competing inputs wins inside it.
``domain_name='due_diligence'`` makes it return right after the configuration
is stored, which is everything these assertions are about.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.research.agent import _retrieve as agent_retrieve
from app.research.agent.model import Casper, _normalize_report_config

pytestmark = pytest.mark.unit

STUDY_LAYOUT = "# UK Fintech\n\n## Market Size\n- Segments\n- Growth\n\n## Regulation\n- FCA\n"
BRIEF_LAYOUT = "# UK Fintech\n\n## Market Size\n\n## Regulation\n"
REFRESHED_BRIEF_LAYOUT = "# UK Fintech\n\n## Market Size After Refresh\n\n## Regulation\n"


@pytest.fixture
def casper(monkeypatch) -> Casper:
    """A Casper with the layouts already paired, as they are at the pause."""
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

    instance.report_layout_pair = {
        "study": {
            "report_tier": "study",
            "report_title": "UK Fintech",
            "markdown": STUDY_LAYOUT,
            "report_layout": [{"title": "UK Fintech"}],
        },
        "brief": {
            "report_tier": "brief",
            "report_title": "UK Fintech",
            # What the refresh produced for the brief tier (R9).
            "markdown": REFRESHED_BRIEF_LAYOUT,
            "report_layout": [{"title": "UK Fintech"}],
        },
    }
    return instance


async def _retrieve(instance: Casper, *, report_type: str = "study", layout: str = STUDY_LAYOUT):
    return await instance.retrieve(
        user_instructions="Assess the UK fintech market.",
        report_layout=layout,
        report_language="English",
        report_title="UK Fintech",
        domain_name="due_diligence",
        report_type=report_type,
    )


async def test_confirming_brief_when_study_was_live_builds_brief(casper: Casper) -> None:
    """AE1: the confirmed tier selects the layout and the generation path."""
    casper.report_config_submission = _normalize_report_config({"report_tier": "brief"})

    await _retrieve(casper, report_type="study")

    # generate_report reads report_type off retrieve_config to pick
    # generate_brief_cards, so this is the branch selection.
    assert casper.retrieve_config["report_type"] == "brief"
    assert casper.retrieve_config["report_length"] == "brief"
    assert casper.retrieve_config["report_layout"] == REFRESHED_BRIEF_LAYOUT


async def test_confirmation_beats_the_paid_tier(casper: Casper) -> None:
    """AE2: the pre-paid tier no longer overrides the user's selection."""
    casper.forced_report_type = "study"
    casper.report_config_submission = _normalize_report_config({"report_tier": "brief"})

    await _retrieve(casper, report_type="study")

    assert casper.retrieve_config["report_type"] == "brief"


async def test_confirmation_beats_the_models_own_argument(casper: Casper) -> None:
    casper.report_config_submission = _normalize_report_config({"report_tier": "study"})

    await _retrieve(casper, report_type="brief", layout=BRIEF_LAYOUT)

    assert casper.retrieve_config["report_type"] == "study"
    assert casper.retrieve_config["report_layout"] == STUDY_LAYOUT


async def test_the_confirmed_tiers_refreshed_layout_is_what_gets_built(casper: Casper) -> None:
    """R9: the selected layout is used in its web-refreshed form."""
    casper.report_config_submission = _normalize_report_config({"report_tier": "brief"})

    await _retrieve(casper, report_type="study")

    assert casper.retrieve_config["report_layout"] == REFRESHED_BRIEF_LAYOUT
    assert BRIEF_LAYOUT != REFRESHED_BRIEF_LAYOUT, "the refresh must have changed something"


async def test_inert_fields_are_stored_and_change_no_branch(casper: Casper) -> None:
    """R12, R13: everything is persisted; only tier and style act."""
    casper.report_config_submission = _normalize_report_config(
        {
            "report_tier": "brief",
            "style": "investor",
            "output_formats": ["pdf", "pptx"],
            "language": "English",
            "data_sources": ["web", "filings"],
        }
    )

    await _retrieve(casper, report_type="study")

    config = casper.retrieve_config
    assert config["output_formats"] == ["pdf", "pptx"]
    assert config["data_sources"] == ["web", "filings"]
    assert config["language"] == "English"
    assert config["style"] == "investor"
    # The inert fields did not disturb what actually drives generation.
    assert config["report_type"] == "brief"
    assert config["report_language"] == "English", "reports are still English-only"


async def test_without_a_submission_the_models_argument_still_applies(casper: Casper) -> None:
    """No pause happened (or nothing was confirmed): behaviour is unchanged."""
    await _retrieve(casper, report_type="brief", layout=BRIEF_LAYOUT)

    assert casper.retrieve_config["report_type"] == "brief"
    assert casper.retrieve_config["style"] == "investor"


async def test_a_paid_tier_alone_no_longer_overrides_anything(casper: Casper) -> None:
    """KTD5: the override block is gone, not reordered."""
    casper.forced_report_type = "study"

    await _retrieve(casper, report_type="brief", layout=BRIEF_LAYOUT)

    assert casper.retrieve_config["report_type"] == "brief"
