# analyst_reasoning

Turns inline / numbered citations into **visible source evaluation** — "GVR counts
hardware + services ($5.9bn); IBISWorld counts operator revenue ($0.4bn); the gap
is definitional, track $0.4–1.5bn" — on the small subset of claims that need it,
and strips tracking params off every citation URL.

## Why not the two obvious approaches

| Approach | Problem |
|---|---|
| Fetch + LLM per citation | ~2× calls per citation (600 on a 300-cite report); re-fetches pages already in `web_search_raw_responses` |
| One mega-prompt in card generation | model reasons over the whole card + every source at once → hallucination risk |

This module: **claim-level, not citation-level**; **reuse persisted evidence, no fetches**;
**one strong-model call per contested card only**. Cost scales with contested claims
(~6–10 per report), not citation count.

## Flow

```
card ─► clean_card_citation_urls        url_cleaner.py     deterministic, always
     ─► triage_card                     contested_claims.py reads card['contested_claims'],
                                                            else 1 cheap-model call
     ─► (no contested claims) ─► done
     ─► build_evidence_store            evidence_store.py   web_search_citations +
                                                            web_search_raw_responses, no network
     ─► generate_card_reasoning         reasoning_pass.py   1 strong-model call, rewrites
                                                            only the contested passages
     ─► clean_card_citation_urls again
card'
```

The reasoning call uses OpenAI **structured outputs at maximum reasoning effort** —
`SYNC_OPENAI_CLIENT.responses.parse(..., text_format=CardReasoningOutput, reasoning={"effort": "xhigh"})`
— so the result comes back as a validated `CardReasoningOutput`
(`response.output_parsed`), not hand-parsed JSON. The Gemini fallback uses the
Interactions API at `thinking_level: "high"` with the equivalent
`GEMINI_REASONING_SCHEMA`. Triage still uses a tool call (cheap model, tiny schema).

## Usage

```python
from src.core.analyst_reasoning import (
    apply_analyst_reasoning,
    apply_analyst_reasoning_to_report,
)

# one card — returns the card, notes attached inside it
card = await apply_analyst_reasoning(card, report_id, card_id=card_id,
                                     chat_id=chat_id, user_id=user_id)

# whole report — builds the evidence store once, bounded concurrency
cards = await apply_analyst_reasoning_to_report(cards, report_id,
                                                chat_id=chat_id, user_id=user_id)
```

## Where the reasoning lands

The pass returns **the card, and only the card**. Each analyst note is attached
under `analyst_reasoning` on the exact part of the card whose text it explains:

```jsonc
{
  "section": "Market Size",
  "content": "…rewritten top-level prose…",
  "analyst_reasoning": [                     // claims in `content`
    {"claim_id": "c1", "analyst_note": "…", "working_band": "$0.4–1.5bn",
     "gap_type": "definitional"}
  ],
  "sub_sections": [
    {"name": "UK segment", "content": "…rewritten…",
     "analyst_reasoning": [{"claim_id": "c2", "analyst_note": "…", …}]},
    {"name": "Outlook", "content": "…verbatim…", "analyst_reasoning": []}
  ]
}
```

The key is **always present** on the card and on every sub-section once the pass
has run — an uncontested passage gets `[]`, never a missing key. So consumers can
read `sub["analyst_reasoning"]` unconditionally, and an *absent* key means "the
pass never ran here" (`ANALYST_REASONING_ENABLED=false`, a card that skipped the
pipeline) rather than "nothing was contested". `ensure_reasoning_keys(card)`
establishes the invariant in place; `apply_analyst_reasoning` calls it on entry,
so the caller's card satisfies it even if a later step raises.

This costs **no extra LLM call**. Triage pins each claim to a sub-section
(`ContestedClaim.location`, validated against the card's real sub-section names by
`contested_claims.resolve_location`), the prompt groups the claims under those
headings, and `CardReasoningOutput` nests `analyst_reasoning` inside each
`RewrittenSubSection` — so the one reasoning call already returns the notes in
the right place. `count_reasoning(card)` totals them across the card.

`card_utils.modify_card` rebuilds the top-level card dict, so it carries
`analyst_reasoning` across explicitly and back-fills `[]` on every sub-section —
the notes survive into the DB card, and every card that went through the
generation pipeline has the key even when the pass never flagged anything. The
error-path empty cards in `model.py` / `planner.py::_empty_card` set it too.

The pass runs on the generation shape, where the section's prose is `card["content"]`,
so the card's own notes land at the card's top level. `modify_card` moves that prose
into `section[0]` and **mirrors the same notes onto `section[0]["analyst_reasoning"]`**,
so a consumer walking `[card["section"][0]] + card["sub_sections"]` finds the notes for
each block's own text. `card["analyst_reasoning"]` stays as the authoritative copy.

Just the deterministic URL cleaning, nothing else:

```python
from src.core.analyst_reasoning import clean_card_citation_urls
clean_card_citation_urls(card)   # in place; safe to run on every card
```

## The cheap win: flag at generation time

`triage_card` first looks for `card['contested_claims']`. If card generation emits
that field (a list of `{claim_id, claim_text, source_urls, kind, why, location}` — a *flag*,
not prose), the triage LLM call is skipped entirely. The generation model already
saw the conflicting figures, so flagging there is nearly free. See
`schemas.ContestedClaim`. Nothing emits that field yet — every card currently pays
for the triage call.

### Triage recall

The failure mode on a long card is not a false positive, it's the reviewer
reporting the first obvious hit and stopping — you get one claim on a card with
four contested passages. Two things push back on that:

* **`location_sweep`** is a required field on the triage tool call: one entry per
  location (top-level content first, then every sub-section, by exact name) with a
  one-line verdict, filled in *before* `contested_claims`. It forces a full pass
  and it is logged, so a card that comes back clean still says why per location.
* **The "already analyst-style" exclusion is narrow on purpose.** A passage is only
  excluded if it names what each source counted *and* says which figure the report
  stands behind. Attribution alone does not clear it — a sourced comparison table
  that never reconciles its rows against each other is still a flag.

Cross-bullet and cross-sub-section contradictions count as `conflicting_sources`
even when each figure has its own single source; hedging prose ("evidence is
mixed", "however") around a pair of numbers is the signal to look for.

## Where it is wired in

**Card generation** — immediately after `card_fixer.fix_card`, before citation
dressing, in both loops:

  * `src/core/model.py` — study + brief reports (per-section loop)
  * `src/core/domains/planner.py` — primary-research / due-diligence domains

**Card refinement** — in `refiner.py::refine_card`, right after `fix_content`,
via the sync `apply_analyst_reasoning_to_content(content, section_name, ...)`.
Same wiring in `ask-caspr-service` (which vendors a copy of this module — its
`build_evidence_store` DB path is a no-op there; it uses the analytics path).

Every call site passes evidence from the in-memory analytics collector
(`build_evidence_store_from_analytics`) — **no DB round trip**, no read-your-writes
race with the fire-and-forget analytics writer. Guarded by
`ANALYST_REASONING_ENABLED` and wrapped so it can never break generation /
refinement (on failure the card passes through unchanged).

## Citations: named, not numbered

Reports render **named citations** (`[Grand View](grandviewresearch.com/...)`),
not `[1] [2] [3]`. `url_cleaner.dress_inline_citations` replaces the numbered
rewrite everywhere:

  * generation — `card_utils.replace_citations` no longer called; briefs use
    `replace_brief_citations(..., keep_labels=True)`
  * refinement — `refiner.replace_citations` + `_enforce_numbered_citations` no
    longer called (both services)

`dress_inline_citations` keeps the model's `[Source Name](url)` label, strips
tracking params, adds a `#:~:text=` deep link when a snippet is known, and
registers every source in the report-wide `citation_url_map` (and, for refine,
fills `collected` with `{url: number}` merged into the card's `citations` map)
so the sources panel stays complete. The reasoning pass's `[Publisher](url)`
output survives to the final render unchanged.

## Config (`src/config/constants.py`)

| var | default | role |
|---|---|---|
| `ANALYST_REASONING_ENABLED` | `true` | master on/off for the wired-in pass |
| `ANALYST_CLAIM_TRIAGE_MODEL` | `gpt-4o-mini` | fallback contested-claim flagging |
| `ANALYST_REASONING_MODEL` | `gpt-5.5` | writes the analyst prose (reasoning model) |
| `ANALYST_REASONING_EFFORT` | `xhigh` | Responses API `reasoning.effort` — `xhigh` is the ceiling for gpt-5.5 |
| `GEMINI_ANALYST_REASONING_MODEL` | `gemini-3.1-pro-preview` | fallback reasoning model |
| `ANALYST_REASONING_GEMINI_THINKING_LEVEL` | `high` | Gemini `thinking_level` — `high` is the ceiling for Gemini 3 Pro |

The reasoning pass runs the strong model at its **maximum reasoning setting**:
OpenAI `responses.parse(..., reasoning={"effort": "xhigh"})`, Gemini fallback
`interactions.create(..., generation_config={"thinking_level": "high"})`. Triage
stays cheap (no/low thinking).

## Not done here (deliberate follow-ups)

- **Lazy gap-fill**: `evidence_store.missing_evidence_urls` returns cited URLs with
  no stored snippet. Wiring a single Tavily call for those is left to the caller —
  it's rare and bounded.
- **Deterministic calculation notes**: for `kind="derived_number"` where the formula
  is known (unit conversion, CAGR), render the explanation from a template instead
  of an LLM call.
- **Card schema change** to emit `contested_claims` at generation time.

## Related READMEs (this service)

- [`../../../README.md`](../../../README.md) — caspr-api overview
- [`../domains/README.md`](../domains/README.md) — report domains; this pass runs after `fix_card` in `planner.py`
- [`../caspr_mcp/README.md`](../caspr_mcp/README.md) — MCP server
- [`../../db/README.md`](../../db/README.md) — the data layer
