"""U8: card generation writes for an investor reader.

These assert prompt *composition*, not model output. Whether the generated
prose actually reads like an investor memo is a review judgement, not
something a test can decide.
"""

from __future__ import annotations

import pytest

from app.research.prompts.prompt_utils import (
    CASPR_VOICE,
    INVESTOR_AUDIENCE,
    audience_framing,
    build_brief_system_prompt,
    build_card_gen_prompt,
)

pytestmark = pytest.mark.unit


def _study_prompt(**overrides) -> str:
    kwargs = {
        "target_section": "Market Size",
        "section_description": "How large the addressable market is.",
        "sub_sections_str": "### Segments\nBy segment.\n\n",
        "descriptive_report_layout": "# UK Fintech\n## Market Size\n",
        "current_date": "September 08, 2026",
        "user_instructions": "Assess the UK fintech market.",
        "report_length": "STUDY",
    }
    kwargs.update(overrides)
    return build_card_gen_prompt(**kwargs)


def test_study_prompt_carries_the_investor_framing_and_the_existing_voice() -> None:
    prompt = _study_prompt(style="investor")

    assert INVESTOR_AUDIENCE in prompt
    assert CASPR_VOICE in prompt, "the analyst voice is kept, not replaced"
    assert "Generate content ONLY for the section titled: 'Market Size'" in prompt


def test_investor_is_the_default_so_existing_call_sites_need_no_change() -> None:
    """R14: the tone arrives without every caller having to opt in."""
    assert INVESTOR_AUDIENCE in _study_prompt()


def test_an_unrecognized_style_falls_back_to_investor() -> None:
    """Better a prompt with the wrong audience than one with no audience."""
    assert audience_framing("casual") == INVESTOR_AUDIENCE
    assert audience_framing("") == INVESTOR_AUDIENCE
    assert INVESTOR_AUDIENCE in _study_prompt(style="nonsense")


def test_the_brief_prompt_carries_the_same_framing() -> None:
    """A brief and a study of the same subject should read as one voice."""
    brief = build_brief_system_prompt("September 08, 2026")

    assert INVESTOR_AUDIENCE in brief
    assert "September 08, 2026" in brief
    assert "briefing note" in brief


def test_the_investor_framing_names_the_reader_and_stops_short_of_advice() -> None:
    assert "Your reader is an investor" in INVESTOR_AUDIENCE
    assert "Do not give investment advice" in INVESTOR_AUDIENCE


def test_adding_the_audience_left_the_rest_of_the_prompt_alone() -> None:
    """The only difference between styles is the audience block."""
    with_investor = _study_prompt(style="investor")
    assert with_investor.replace(INVESTOR_AUDIENCE, "").strip()
    assert with_investor.count(INVESTOR_AUDIENCE) == 1
