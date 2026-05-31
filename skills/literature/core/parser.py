"""Parse different input types (URL, PDF, DOI) to extract text."""

import re
import tempfile
from pathlib import Path
from typing import Tuple
import requests


def parse_input(input_value: str, input_type: str = "auto") -> Tuple[str, str]:
    """Parse input and return (text_content, detected_type).

    Args:
        input_value: URL, DOI, file path, or text
        input_type: "auto", "url", "doi", "pubmed", "file", "text"

    Returns:
        Tuple of (extracted_text, actual_input_type)
    """
    if input_type == "auto":
        input_type = detect_input_type(input_value)

    if input_type == "url":
        return parse_url(input_value), "url"
    elif input_type == "doi":
        return parse_doi(input_value), "doi"
    elif input_type == "pubmed":
        return parse_pubmed(input_value), "pubmed"
    elif input_type == "file":
        return parse_file(input_value), "file"
    else:
        return input_value, "text"


def detect_input_type(value: str) -> str:
    """Auto-detect input type."""
    value = value.strip()

    # DOI pattern
    if re.match(r'^10\.\d{4,}/\S+', value):
        return "doi"

    # PubMed ID
    if re.match(r'^\d{7,8}$', value):
        return "pubmed"

    # URL
    if value.startswith(('http://', 'https://')):
        return "url"

    # File path
    if Path(value).exists() and Path(value).suffix == '.pdf':
        return "file"

    return "text"


def _extract_meaningful_html(html: str) -> str:
    """Extract readable text from HTML, targeting article/main content areas."""
    # Try to extract from <article> or <main> blocks first (cleanest content)
    for tag in ('article', 'main', '[role="main"]'):
        pattern = rf'<{tag}[^>]*>(.*?)</{tag}>'
        matches = re.findall(pattern, html, re.S | re.I)
        if matches:
            # Use the longest match (the actual content, not a stub)
            best = max(matches, key=len)
            html = best
            break

    # Remove script, style, nav, footer, header, aside blocks
    html = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.S | re.I)
    html = re.sub(r'<style[^>]*>.*?</style>', ' ', html, flags=re.S | re.I)
    html = re.sub(r'<nav[^>]*>.*?</nav>', ' ', html, flags=re.S | re.I)
    html = re.sub(r'<footer[^>]*>.*?</footer>', ' ', html, flags=re.S | re.I)
    html = re.sub(r'<header[^>]*>.*?</header>', ' ', html, flags=re.S | re.I)
    html = re.sub(r'<aside[^>]*>.*?</aside>', ' ', html, flags=re.S | re.I)

    # Strip remaining HTML tags
    text = re.sub(r'<[^>]+>', ' ', html)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text)
    # Remove very short lines (likely noise)
    lines = [l.strip() for l in text.split('\n') if len(l.strip()) > 30]
    return '\n'.join(lines) if lines else text.strip()


def _fetch_webpage_markdown(url: str, timeout: float = 15.0) -> str:
    """Fetch a webpage and convert HTML → clean Markdown.

    Uses ``httpx`` for fetching and ``markdownify`` for HTML→Markdown
    conversion — a more robust alternative to regex-based extraction.
    Falls back gracefully to the regex-based ``_extract_meaningful_html``
    if either library is unavailable.
    """
    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/125.0.0.0 Safari/537.36'
        ),
        'Accept': 'text/html,application/xhtml+xml',
    }

    # Try httpx + markdownify first
    try:
        import httpx
        from markdownify import markdownify as md

        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            html = response.text

        # Pre-clean: strip known noise regions before markdownify conversion
        html = re.sub(
            r'<script[^>]*>.*?</script>', ' ', html,
            flags=re.S | re.I,
        )
        html = re.sub(
            r'<style[^>]*>.*?</style>', ' ', html,
            flags=re.S | re.I,
        )
        html = re.sub(
            r'<nav[^>]*>.*?</nav>', ' ', html,
            flags=re.S | re.I,
        )
        html = re.sub(
            r'<footer[^>]*>.*?</footer>', ' ', html,
            flags=re.S | re.I,
        )
        html = re.sub(
            r'<header[^>]*>.*?</header>', ' ', html,
            flags=re.S | re.I,
        )
        html = re.sub(
            r'<aside[^>]*>.*?</aside>', ' ', html,
            flags=re.S | re.I,
        )

        # Convert HTML to clean Markdown
        text = md(
            html,
            heading_style='ATX',      # # heading style
            bullets='-',               # - for unordered lists
        )

        # Post-clean: drop short lines (likely nav/footer residue)
        clean_lines = []
        for line in text.split('\n'):
            line = line.strip()
            # Skip lines that look like JS/CSS/goobledygook
            if re.match(r'^function\s+\w+\s*\(', line, re.I):
                continue
            if re.match(r'^var\s+\w+\s*=', line):
                continue
            if re.match(r'^[{};\s]+$', line):
                continue
            if line and len(line) >= 15:
                clean_lines.append(line)
        text = '\n'.join(clean_lines)

        # Collapse excessive whitespace
        text = re.sub(r'\n{4,}', '\n\n', text)
        text = re.sub(r'[ \t]+', ' ', text)
        text = text.strip()

        # Quality check: if markdown output is very short, fall back
        if len(text.split()) >= 50:
            return text
    except ImportError:
        pass  # fall through to regex fallback
    except Exception:
        pass  # fall through to regex fallback

    # Fallback: use requests + regex extraction
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        html = response.text

        text = _extract_meaningful_html(html)
        words = text.split()
        if len(words) < 50:
            text = re.sub(r'<[^>]+>', ' ', html)
            text = re.sub(r'\s+', ' ', text)
        return text.strip()
    except Exception as e:
        return f"Error fetching URL: {e}"


def parse_url(url: str) -> str:
    """Fetch and extract text from URL."""
    try:
        # Handle PubMed URLs
        if 'pubmed.ncbi.nlm.nih.gov' in url:
            pmid = re.search(r'/(\d+)', url)
            if pmid:
                return parse_pubmed(pmid.group(1))

        return _fetch_webpage_markdown(url)[:80000]

    except Exception as e:
        return f"Error fetching URL: {e}"


def parse_doi(doi: str) -> str:
    """Fetch article via DOI."""
    # Normalize DOI
    doi = doi.strip()
    if not doi.startswith('10.'):
        doi = '10.' + doi.lstrip('10.')

    # Try dx.doi.org redirect
    url = f"https://doi.org/{doi}"
    return parse_url(url)


def parse_pubmed(pmid: str) -> str:
    """Fetch article from PubMed."""
    pmid = pmid.strip()

    # Use PubMed E-utilities API
    base_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    try:
        # Fetch abstract
        fetch_url = f"{base_url}/efetch.fcgi?db=pubmed&id={pmid}&retmode=xml"
        response = requests.get(fetch_url, timeout=30)
        response.raise_for_status()

        # Extract text from XML (simple approach)
        text = re.sub(r'<[^>]+>', ' ', response.text)
        text = re.sub(r'\s+', ' ', text)
        return text

    except Exception as e:
        return f"Error fetching PubMed {pmid}: {e}"


def parse_file(filepath: str) -> str:
    """Extract text from PDF file."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(filepath)
        text_parts = []

        for page in reader.pages:
            text_parts.append(page.extract_text())

        return ' '.join(text_parts)

    except ImportError:
        return "Error: pypdf not installed. Run: pip install pypdf"
    except Exception as e:
        return f"Error parsing PDF: {e}"
