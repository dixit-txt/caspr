import re
from urllib.parse import quote, urlparse, urlunparse

def clean_snippet(snippet: str) -> str:
    # Handle new unicode citation markers: \ue200cite\ue202...\ue201 [wordlim: N] metadata;
    pattern_unicode = r'^\ue200[^\ue201]*\ue201\s*\[wordlim:\s*\d+\]\s*(?:[^;]*;\s*)*'
    cleaned = re.sub(pattern_unicode, '', snippet, count=1)
    if cleaned != snippet:
        return cleaned
    # Handle legacy format: 【...】 [wordlim: N] metadata;
    pattern_legacy = r'^【[^】]*】\s*\[wordlim:\s*\d+\]\s*(?:\w+:\s*[^;]*;\s*)*'
    cleaned = re.sub(pattern_legacy, '', snippet, count=1)
    if cleaned != snippet:
        return cleaned
    # Handle open_page/find_in_page format: "Content type:...; Source:...; Total lines: N\nLNN@PN:"
    pattern_open_page = r'^Content type:[^;]*;\s*(?:Number of pages:\s*\d+;\s*)?Source:[^;]*;\s*Total lines:\s*\d+\s*'
    cleaned = re.sub(pattern_open_page, '', snippet, count=1)
    if cleaned != snippet:
        # Also strip line-number prefixes like "L497@P16: "
        cleaned = re.sub(r'^L\d+(?:@P\d+)?:\s*', '', cleaned)
        return cleaned
    return snippet

def _extract_sentences(text: str) -> list:
    """Split text into sentences, handling common abbreviations."""
    return re.split(r'(?<=[.!?])\s+(?=[A-Z])', text)

def generate_citation_url(url: str, snippet: str, start_words: int = 4, end_words: int = 4) -> str:
    """
    Generates a URL with a text fragment that links to the snippet's location on the page.
    
    Picks the second sentence (or a middle sentence) from the cleaned snippet to avoid
    metadata remnants at the beginning. Falls back to first sentence if only one exists.
    Uses first N and last N words of the chosen sentence for the text range fragment.
    """
    cleaned = clean_snippet(snippet).strip()
    
    if not cleaned:
        return url

    sentences = _extract_sentences(cleaned)
    # Filter out very short sentences (< 5 words) that are likely leftover metadata
    valid_sentences = [s for s in sentences if len(s.split()) >= 5]
    
    if not valid_sentences:
        valid_sentences = sentences

    # Pick second sentence if available, otherwise first
    target = valid_sentences[1] if len(valid_sentences) > 1 else valid_sentences[0]
    target = target.strip()

    words = target.split()
    # Remove standalone punctuation tokens from the end
    while words and re.match(r'^[.!?,;:]+$', words[-1]):
        words.pop()
    
    if not words:
        return url
    
    def _strip_trailing_punct(s: str) -> str:
        """Remove trailing sentence-ending punctuation that breaks browser text fragments."""
        return re.sub(r'[.!?]+$', '', s)
    
    def _pick_start_words(word_list, count):
        """
        Pick start words avoiding commas in the fragment prefix.
        If a comma exists in the first `count` words, use the words
        immediately after the last comma-bearing word within that range.
        """
        candidate = word_list[:count]
        last_comma_idx = -1
        for i, w in enumerate(candidate):
            if ',' in w:
                last_comma_idx = i
        if last_comma_idx >= 0:
            after_comma = word_list[last_comma_idx + 1 : last_comma_idx + 1 + count]
            if after_comma:
                return after_comma
        return candidate

    if len(words) <= start_words + end_words:
        text_joined = _strip_trailing_punct(" ".join(words))
        text_start = quote(text_joined, safe='')
        fragment = f"#:~:text={text_start}"
    else:
        start_candidate = _pick_start_words(words, start_words)
        text_start_raw = " ".join(start_candidate)
        text_start = quote(text_start_raw, safe='')
        text_end_raw = _strip_trailing_punct(" ".join(words[-end_words:]))
        text_end = quote(text_end_raw, safe='')
        fragment = f"#:~:text={text_start},{text_end}"

    # Strip any existing fragment from the URL
    parsed = urlparse(url)
    base_url = urlunparse(parsed._replace(fragment=""))
    
    return f"{base_url}{fragment}"