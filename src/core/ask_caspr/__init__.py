"""Ask Caspr Q&A moved to ask-caspr-service.

The public `/api/v1/ask-caspr/*` routes, `CasprSession`, and the LLM
`ask_caspr()` implementation now live in that service. Card-generation
summaries (`update_summaries_for_ask_caspr` in card_utils.py) and the
`ask_caspr_chats` insert during report generation stay here — those are
report-generation side effects, not the Q&A path.

See ask-caspr-service/README.md.
"""

raise ImportError(
    "Ask Caspr Q&A lives in ask-caspr-service now. "
    "See ask-caspr-service/README.md."
)
