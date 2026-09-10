"""Part A — deterministic citation-URL cleaning (no LLM).

The card-generation model returns inline citations shaped like
``[Grand View](https://www.grandviewresearch.com/report?utm_source=openai)``.
This module strips tracking parameters from the URL slot of every markdown
link in a card's content, leaving the link text and everything else untouched:

    [Grand View](https://www.grandviewresearch.com/report)

It is intentionally a pure string transform — cheap, safe to run on every
card, and independent of the analyst-reasoning pass. ``normalize_url`` is also
the canonical key used by :mod:`evidence_store` so a URL cited with tracking
junk matches the same URL stored clean in ``web_search_citations``.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.core.logging import setup_logging

logger = setup_logging(__name__)

# Mirrors src/db/web_search_db.py::_TRACKING_QUERY_PARAMETERS plus the prefix
# rule for ``utm_*``. Keep the two in sync — the DB layer normalizes on the way
# in, this layer normalizes what the model wrote into the rendered card.
_TRACKING_QUERY_PARAMETERS = {
    "gclid",
    "fbclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "ref",
    "ref_src",
    "spm",
}

# ``[label](url)`` that is NOT an image (`!` prefix). Group 1 = label, 2 = url.
_MD_LINK_RE = re.compile(r"(?<!!)\[([^\]]*)\]\((https?://[^)\s]+)\)")


def _strip_tracking_params(query: str) -> str:
    kept = [
        (key, value)
        for key, value in parse_qsl(query, keep_blank_values=True)
        if not (key.lower().startswith("utm_") or key.lower() in _TRACKING_QUERY_PARAMETERS)
    ]
    return urlencode(kept, doseq=True)


def normalize_url(raw_url: str, *, keep_fragment: bool = False) -> str | None:
    """Return a deterministic http(s) URL with tracking params removed.

    Lower-cases the host, drops default ports, strips ``utm_*`` and known
    tracking keys, and (by default) drops the fragment. Returns ``None`` for
    anything that is not a plain http(s) URL so callers can skip it.
    """
    if not isinstance(raw_url, str) or not raw_url.strip():
        return None
    try:
        parts = urlsplit(raw_url.strip())
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return None
        host = parts.hostname.lower()
        if parts.port and not (
            (parts.scheme.lower() == "http" and parts.port == 80)
            or (parts.scheme.lower() == "https" and parts.port == 443)
        ):
            host = f"{host}:{parts.port}"
        return urlunsplit(
            (
                parts.scheme.lower(),
                host,
                parts.path,
                _strip_tracking_params(parts.query),
                parts.fragment if keep_fragment else "",
            )
        )
    except (ValueError, TypeError):
        return None


def canonical_key(raw_url: str) -> str | None:
    """Aggressive key for *matching* a URL across sources (not for display).

    On top of :func:`normalize_url`: drops a leading ``www.``, lower-cases the
    whole thing, and strips a trailing slash. Use this to key the evidence store
    and to look claims up in it, so ``www.grandviewresearch.com/x`` and
    ``grandviewresearch.com/x/`` resolve to the same source.
    """
    normalized = normalize_url(raw_url)
    if not normalized:
        return None
    parts = urlsplit(normalized)
    host = parts.hostname.removeprefix("www.") if parts.hostname else ""
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, host, path, parts.query, "")).lower()


_NUMERIC_LABEL_RE = re.compile(r"^\s*\[?\s*\d+\s*\]?\s*$")

#: Suffixes that act as a TLD's second level, so the real name is left of them.
_SECOND_LEVEL_TLDS = frozenset(
    {"co", "com", "org", "net", "gov", "edu", "ac", "or", "ne", "go"}
)


def _is_numeric_label(label: str) -> bool:
    """True for citation labels that are just a number, e.g. '1' or '[12]'."""
    return bool(label) and bool(_NUMERIC_LABEL_RE.match(label))


_HOSTNAME_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$", re.I)


def _is_hostname_label(label: str) -> bool:
    """True for a label that is really a hostname, e.g. 'annualreport.pif.gov.sa'.

    Models often satisfy "use the source name" by pasting the domain. That is not
    a name, so it gets reduced to the publisher instead.
    """
    s = (label or "").strip()
    return bool(s) and " " not in s and bool(_HOSTNAME_LABEL_RE.match(s))


def _display_name(name: str) -> str:
    """Present a registrable domain name as a source label.

    Short names are almost always acronyms (PIF, IDC, IMF, BBC, FT), longer ones
    read better capitalised (Reuters, Bloomberg).
    """
    if not name:
        return name
    return name.upper() if len(name) <= 4 else name[:1].upper() + name[1:]


def source_label(raw_url: str) -> str | None:
    """A readable source name from a URL host — 'Reuters' for reuters.com.

    The fallback when a model emits a bare number, a bare URL, or the hostname
    itself as the citation label, so the report still shows something that reads
    as a source. Short registrable names are treated as acronyms (PIF, IDC, FT).
    """
    if not raw_url:
        return None
    candidate = raw_url if raw_url.startswith(("http://", "https://")) else f"http://{raw_url}"
    try:
        host = urlsplit(candidate).netloc.lower()
    except ValueError:
        return None
    host = host.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    parts = [p for p in host.split(".") if p]
    if not parts:
        return None
    # bbc.co.uk / example.com.au: the registrable name sits one further left than
    # a plain .com would put it, so a naive parts[-2] yields "co" / "com".
    if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL_TLDS:
        return _display_name(parts[-3])
    return _display_name(parts[-2] if len(parts) >= 2 else parts[0])


_MD_CITATION_RE = re.compile(r"(?<!!)\[([^\]]*)\]\((https?://[^)\s]+)\)")


def name_bare_citation_labels(md_text: str) -> str:
    """Give a readable source name to citations whose label is a URL or empty.

    Replaces the old ``_enforce_numbered_citations`` pass at the end of
    ``convert_json_to_md``. That pass rewrote EVERY citation label to a number,
    which undid every ``[Source Name](url)`` the cards had produced. It existed
    to stop an LLM (the card fixer, or the executive summary) emitting an ugly
    ``[https://...](https://...)`` — this keeps exactly that guard and leaves
    labels that are already names, or already numbers, untouched.
    """
    if not md_text:
        return md_text

    def _fix(match: re.Match) -> str:
        label, url = match.group(1), match.group(2)
        stripped = (label or "").strip()
        looks_like_url = stripped.startswith(("http://", "https://", "www."))
        if stripped and not looks_like_url and not _is_hostname_label(stripped) \
                and any(ch.isalpha() for ch in stripped):
            return match.group(0)  # already a real source name — leave it alone
        # Empty, a bare URL, or a bare number: name it from the host. This is the
        # last step before the markdown is returned, so it also rescues any
        # numeric label an upstream pass missed rather than trusting them all.
        return f"[{source_label(url) or stripped or 'source'}]({url})"

    return _MD_CITATION_RE.sub(_fix, md_text)


def clean_citation_urls(text: str) -> str:
    """Strip tracking params from every markdown-link URL in ``text``.

    The text-fragment (``#:~:text=``) part of a URL is preserved — those are
    added deliberately by :mod:`src.core.citation_utils` to deep-link a
    snippet — only the query string is cleaned.
    """
    if not text:
        return text

    def _replace(match: re.Match) -> str:
        label, url = match.group(1), match.group(2)
        cleaned = normalize_url(url, keep_fragment=True)
        return f"[{label}]({cleaned or url})"

    return _MD_LINK_RE.sub(_replace, text)


def dress_inline_citations(
    text: str,
    *,
    url_to_snippet: dict | None = None,
    citation_url_map: dict | None = None,
    report_citations: list | None = None,
    collected: dict | None = None,
) -> str:
    """Keep ``[Source Name](url)`` citations exactly as the model wrote them,
    only cleaning the URL and (when a snippet is known) deep-linking to the
    cited passage.

    This is the named-citation replacement for the numbered-citation rewrite
    (``card_utils.replace_citations``): the label is never touched, so reports
    render ``[Grand View](grandviewresearch.com/...)`` instead of ``[3](...)``.

    ``utm_*`` and other tracking params are stripped. If ``url_to_snippet`` maps
    the (cleaned) URL to a snippet, a ``#:~:text=`` fragment is appended so the
    link opens at that sentence. When ``citation_url_map`` / ``report_citations``
    are passed, every cited URL is also registered there so the report-wide
    sources panel stays complete even for links that were only ever inline;
    ``collected`` (if given) is filled with ``{cleaned_url: number}`` for just
    the citations seen in this text — the refine flow merges that into the
    card's own ``citations`` map.
    """
    if not text:
        return text

    # Imported here to keep this module import-light for callers that only need
    # the pure URL cleaners.
    from app.cards.service_citations import generate_citation_url

    def _snippet_for(clean: str) -> str | None:
        if not url_to_snippet:
            return None
        for key in (clean, clean.rstrip("/"), clean + "/"):
            if key in url_to_snippet:
                return url_to_snippet[key]
        return None

    def _register(clean: str) -> None:
        if citation_url_map is not None and clean not in citation_url_map:
            if report_citations is not None:
                report_citations.append(clean)
                citation_url_map[clean] = len(report_citations)
            else:
                citation_url_map[clean] = max(citation_url_map.values(), default=0) + 1
        if collected is not None:
            num = citation_url_map.get(clean, 0) if citation_url_map is not None else 0
            collected[clean] = num

    def _replace(match: re.Match) -> str:
        label, url = match.group(1), match.group(2)
        clean = normalize_url(url)  # drops fragment + tracking params
        if not clean:
            return match.group(0)
        _register(clean)
        snippet = _snippet_for(clean)
        final_url = generate_citation_url(clean, snippet) if snippet else clean
        # A named label is kept verbatim, but a bare "[1]" is not a name — the
        # schema asks for [Source Name](url), and cementing the number here is
        # what left numbered citations in finished reports.
        return f"[{source_label(clean) or label}]({final_url})" if _is_numeric_label(label) \
            else f"[{label}]({final_url})"

    return _MD_LINK_RE.sub(_replace, text)


def clean_card_citation_urls(card: dict) -> dict:
    """Clean citation URLs in every content field of a card, in place.

    Handles both card shapes used in the codebase:
      * ``{"content": str, "sub_sections": [{"content": str}, ...]}``
      * ``{"section": [{"content": str}], "sub_sections": [...]}``
    """
    if not isinstance(card, dict):
        return card
    changed = 0
    for container, key in _iter_content_slots(card):
        original = container.get(key)
        if isinstance(original, str) and original:
            cleaned = clean_citation_urls(original)
            if cleaned != original:
                container[key] = cleaned
                changed += 1
    if changed:
        logger.info("[analyst_reasoning.url_cleaner] Cleaned citation URLs in %d content block(s)", changed)
    return card


def _iter_content_slots(card: dict):
    """Yield ``(container_dict, key)`` for every editable content string in a card."""
    section = card.get("section")
    if isinstance(section, list):
        for block in section:
            if isinstance(block, dict) and "content" in block:
                yield block, "content"
    if "content" in card:
        yield card, "content"
    for sub in card.get("sub_sections") or []:
        if isinstance(sub, dict) and "content" in sub:
            yield sub, "content"
