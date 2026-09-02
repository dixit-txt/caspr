import asyncio
from fastapi.concurrency import run_in_threadpool
from passlib.hash import argon2
from uuid_utils import uuid7


def generate_uuid() -> str:
    """Generate a time-sortable UUID v7 string (better for DB indexing than UUID v4)."""
    return str(uuid7())


pwd_context = argon2.using(
    type="ID",  # Argon2id variant
    memory_cost=65536,  # 64MB
    time_cost=3,  # Number of iterations
    parallelism=4,  # Parallel threads
)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a password against a hash
    """
    return pwd_context.verify(plain_password, hashed_password)


def generate_password_hash(password: str) -> str:
    """
    Hash a password for storing
    """
    return pwd_context.hash(password)


def extract_content(card: dict, default_key: str = None) -> str:
    """
    Extract content safely from either new 'section' format or legacy fields.
    """
    if card.get("section") and isinstance(card["section"], list) and card["section"]:
        return card["section"][0].get("content", "")
    if default_key:
        return card.get(default_key, "")
    return ""



from copy import deepcopy
from typing import Dict, List, Set, Tuple

# --- Configuration: what to always keep ---
# section "types" on cards that are always common and should be retained even without cross-checking
ALWAYS_KEEP_CARD_TYPES = {"title", "subtitle", "toc", "es"}
# sometimes section names might be used instead of types to identify the same
ALWAYS_KEEP_SECTION_NAMES = {"title", "subtitle", "table_of_contents", "executive_summary"}

def _norm(s: str) -> str:
    """Normalize strings for robust matching."""
    return (s or "").strip().lower()

def _extract_section_name(section_block: dict) -> str:
    """
    report_layout section is like:
      {"section": [{"name": "<SectionName>", ...}], "sub_sections": [...], ...}
    Return the section name or "" if missing.
    """
    try:
        return section_block.get("section", [{}])[0].get("name", "") or ""
    except Exception:
        return ""

def _build_card_index(cards: List[dict]) -> Tuple[Set[str], Dict[str, Set[str]], Set[str]]:
    """
    Build indices from cards:
      - section_names_in_cards: set of section titles found in cards (excluding always-keep types)
      - subsection_names_by_section: mapping of section title -> set of subsection names in that card
      - always_keep_ids: set of normalized section names that should be kept because card type is always-keep
    """
    section_names_in_cards: Set[str] = set()
    subsection_names_by_section: Dict[str, Set[str]] = {}
    always_keep_ids: Set[str] = set()

    for card in cards or []:
        ctype = _norm(card.get("type", ""))
        title = _norm(card.get("title", ""))

        if ctype in ALWAYS_KEEP_CARD_TYPES or title in { _norm(n) for n in ALWAYS_KEEP_SECTION_NAMES }:
            # mark this section name as always-keep too (helps when names are used instead of types)
            if title:
                always_keep_ids.add(title)
            continue

        if title:
            section_names_in_cards.add(title)

        # collect subsections
        subs = card.get("sub_sections") or []
        names = set()
        for sub in subs:
            sname = _norm(sub.get("name", ""))
            if sname:
                names.add(sname)
        if title:
            subsection_names_by_section[title] = names

    return section_names_in_cards, subsection_names_by_section, always_keep_ids

def prune_report_layout(report_layout: List[dict], cards: List[dict]) -> List[dict]:
    """
    Return a deep-copied report_layout where:
      - Sections are kept only if:
          a) section name/type is one of ALWAYS_KEEP, OR
          b) a card exists with matching section title.
      - For kept sections that are NOT in ALWAYS_KEEP:
          - sub_sections are filtered to those that exist as card sub_sections for that section.
      - Order and structure are preserved; only deletions happen. No content mutation.

    Args:
        report_layout: the 'report_layout' list from your API payload.
        cards: the 'cards' list from your API payload.

    Returns:
        A new list representing the pruned report_layout.
    """
    if not isinstance(report_layout, list):
        # Be permissive: if it's wrapped like {"report_layout":[...]} handle that
        if isinstance(report_layout, dict) and isinstance(report_layout.get("report_layout"), list):
            rl = report_layout.get("report_layout")
        else:
            return []
    else:
        rl = report_layout

    section_names_in_cards, subsection_names_by_section, always_keep_ids = _build_card_index(cards)

    pruned: List[dict] = []
    for block in rl:
        # defensive copy
        block_copy = deepcopy(block)

        # Identify the section's canonical name
        sec_name_raw = _extract_section_name(block_copy)
        sec_name_norm = _norm(sec_name_raw)

        # Decide if section is always-keep
        is_always_keep = (
            sec_name_norm in { _norm(n) for n in ALWAYS_KEEP_SECTION_NAMES }
            or sec_name_norm in always_keep_ids
        )

        # Keep if always-keep, or if there is a matching card section
        if is_always_keep or sec_name_norm in section_names_in_cards:
            # If it's not an always-keep section, filter its sub_sections against card sub_sections
            if not is_always_keep:
                # The card that matched is keyed by normalized section name
                allowed_subs = subsection_names_by_section.get(sec_name_norm, set())
                subs_list = block_copy.get("sub_sections")

                if isinstance(subs_list, list):
                    new_subs = []
                    for sub in subs_list:
                        subname = _norm(sub.get("name", ""))
                        # keep only if subname present in the card's sub sections
                        if subname and subname in allowed_subs:
                            new_subs.append(sub)
                    block_copy["sub_sections"] = new_subs

            # push kept/possibly filtered block
            pruned.append(block_copy)

        # else: skip the whole section block

    return pruned


async def prune_report_layout_async(report_layout: list[dict], cards: list[dict]) -> list[dict]:
    # Offload the blocking sync function to the app’s shared thread pool
    return await run_in_threadpool(prune_report_layout, report_layout, cards)