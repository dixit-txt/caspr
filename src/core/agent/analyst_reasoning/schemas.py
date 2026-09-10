"""Shared types for the analyst-reasoning pipeline.

``ContestedClaim`` is the lightweight flag produced either by card generation
(preferred — the model already saw the conflicting figures) or by the fallback
triage call in :mod:`contested_claims`. It carries *no* prose — only enough to
decide the card needs a reasoning pass and to point that pass at the right
sources.

``EvidenceItem`` is one already-retrieved source (from ``web_search_citations``
+ the persisted raw OpenAI response), keyed by canonical URL.

The reasoning pass returns the card itself: contested passages rewritten, and
the analyst notes attached under ``REASONING_KEY`` on the exact part of the card
they belong to — the card's own ``content`` or one of its ``sub_sections``.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

#: ``location`` value meaning "the card's own top-level ``content``", as opposed
#: to one of its named sub-sections.
TOP_LEVEL = ""

#: Key under which analyst notes are attached to a card / sub-section dict.
REASONING_KEY = "analyst_reasoning"


class ContestedClaim(BaseModel):
    """A claim that needs visible analyst reasoning rather than a bare citation."""

    claim_id: str = Field(description="Stable id, unique within the card, e.g. 'c1'.")
    claim_text: str = Field(
        description="The exact sentence / phrase from the card making the contested or derived claim."
    )
    source_urls: List[str] = Field(
        default_factory=list,
        description="URLs cited for this claim (as they appear in the card, tracking params ok).",
    )
    kind: str = Field(
        default="conflicting_sources",
        description=(
            "'conflicting_sources' (publishers disagree), "
            "'derived_number' (we calculated it), or "
            "'scope_mismatch' (one figure inherited at the wrong scope)."
        ),
    )
    why: str = Field(
        default="",
        description="One line: why this claim can't just be cited (e.g. 'GVR $5.9bn vs IBIS $0.4bn').",
    )
    location: str = Field(
        default=TOP_LEVEL,
        description=(
            "Where the claim sits in the card: the exact sub-section name it appears under, "
            "or an empty string for the card's top-level content."
        ),
    )


class EvidenceItem(BaseModel):
    url: str
    domain: Optional[str] = None
    title: Optional[str] = None
    snippet: Optional[str] = Field(
        default=None,
        description="Search-result snippet from web_search_citations.snippet.",
    )
    quoted: Optional[str] = Field(
        default=None,
        description="The passage OpenAI actually quoted for a url_citation annotation, if captured.",
    )

    def as_prompt_line(self) -> str:
        label = self.domain or self.url
        body = " ".join(p for p in (self.quoted, self.snippet) if p).strip()
        return f"- {label}: {body}" if body else f"- {label}: (no snippet captured)"


class ClaimReasoning(BaseModel):
    claim_id: str
    analyst_note: str = Field(
        description="2-4 sentences weighing the sources: what each counted, why one band is preferred."
    )
    working_band: Optional[str] = Field(
        default=None, description="The figure or range the report should stand behind, if applicable."
    )
    gap_type: Optional[str] = Field(
        default=None, description="'definitional' (scope) or 'genuine' (real disagreement)."
    )


# ── Structured-output model for the reasoning pass (OpenAI responses.parse) ───

class RewrittenSubSection(BaseModel):
    name: str = Field(description="The sub-section name, returned unchanged.")
    content: str = Field(
        description="Sub-section content with contested passages rewritten; every other sentence verbatim."
    )
    analyst_reasoning: List[ClaimReasoning] = Field(
        default_factory=list,
        description=(
            "One entry per contested claim rewritten IN THIS SUB-SECTION. "
            "Empty list if this sub-section had no contested claims."
        ),
    )


class CardReasoningOutput(BaseModel):
    """What the reasoning model returns — parsed directly into this shape.

    The notes are nested where the rewrite happened, so a single call yields
    per-sub-section attribution with no second pass and no location matching.
    """

    content: str = Field(
        description="Full top-level section content with contested passages rewritten; the rest verbatim."
    )
    analyst_reasoning: List[ClaimReasoning] = Field(
        default_factory=list,
        description=(
            "One entry per contested claim rewritten in the TOP-LEVEL content. "
            "Empty list if every contested claim sat in a sub-section."
        ),
    )
    sub_sections: List[RewrittenSubSection] = Field(
        default_factory=list,
        description="Every sub-section of the card, in order, with the same rewrite rule applied.",
    )


# ── Gemini fallback schema (Interactions API needs a raw JSON schema) ─────────

_REASONING_ENTRY_SCHEMA = {
    "type": "object",
    "properties": {
        "claim_id": {"type": "string"},
        "analyst_note": {"type": "string"},
        "working_band": {"type": "string"},
        "gap_type": {"type": "string"},
    },
    "required": ["claim_id", "analyst_note"],
}

GEMINI_REASONING_SCHEMA = {
    "type": "object",
    "properties": {
        "content": {"type": "string"},
        "analyst_reasoning": {"type": "array", "items": _REASONING_ENTRY_SCHEMA},
        "sub_sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "content": {"type": "string"},
                    "analyst_reasoning": {"type": "array", "items": _REASONING_ENTRY_SCHEMA},
                },
                "required": ["name", "content", "analyst_reasoning"],
            },
        },
    },
    "required": ["content", "analyst_reasoning", "sub_sections"],
}


# ── OpenAI tool schema for the fallback triage call ──────────────────────────

#: Forces the reviewer to visit every part of the card and commit to a verdict there,
#: instead of reporting the first obvious hit and stopping.
_LOCATION_SWEEP_SCHEMA = {
    "type": "array",
    "description": (
        "One entry per location, in order: the top-level content (location \"\") followed "
        "by every sub_section. Fill this in before deciding contested_claims."
    ),
    "items": {
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "Exact sub_section name, or \"\" for the top-level content.",
            },
            "finding": {
                "type": "string",
                "description": (
                    "One line: the claim_ids flagged in this location, or why nothing here qualified."
                ),
            },
        },
        "required": ["location", "finding"],
    },
}

TRIAGE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "flag_contested_claims",
        "description": (
            "Identify claims in a report card that require visible analyst reasoning "
            "(conflicting published figures, derived numbers, or inherited-scope figures). "
            "Sweep every location first, then return the claims — an empty list if there are none."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location_sweep": _LOCATION_SWEEP_SCHEMA,
                "contested_claims": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "claim_id": {"type": "string"},
                            "claim_text": {"type": "string"},
                            "source_urls": {"type": "array", "items": {"type": "string"}},
                            "kind": {
                                "type": "string",
                                "enum": [
                                    "conflicting_sources",
                                    "derived_number",
                                    "scope_mismatch",
                                ],
                            },
                            "why": {"type": "string"},
                            "location": {
                                "type": "string",
                                "description": (
                                    "Exact sub_section name the claim appears under, "
                                    "or \"\" if it is in the card's top-level content."
                                ),
                            },
                        },
                        "required": ["claim_id", "claim_text", "kind", "location"],
                    },
                },
            },
            "required": ["location_sweep", "contested_claims"],
        },
    },
}

GEMINI_TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "location_sweep": _LOCATION_SWEEP_SCHEMA,
        "contested_claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim_id": {"type": "string"},
                    "claim_text": {"type": "string"},
                    "source_urls": {"type": "array", "items": {"type": "string"}},
                    "kind": {"type": "string"},
                    "why": {"type": "string"},
                    "location": {"type": "string"},
                },
                "required": ["claim_id", "claim_text", "kind", "location"],
            },
        },
    },
    "required": ["location_sweep", "contested_claims"],
}
