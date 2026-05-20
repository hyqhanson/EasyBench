"""Extract methodology and code sections from paper text."""

import re
from typing import Dict, List

_METHOD_SECTION_PATTERNS = [
    r'Materials? and Methods',
    r'Methods?',
    r'Methodology',
    r'Experimental Procedures',
    r'Experimental Setup',
    r'Analysis Pipeline',
    r'Code Availability',
    r'Software Availability',
]

_CODE_BLOCK_PATTERN = re.compile(
    r'```(?:[^\n]*\n)?(.*?)```|(?:\n    .+)+',
    re.DOTALL,
)

_HEADING_PATTERN = re.compile(
    r'^(?P<heading>' + r'|'.join(_METHOD_SECTION_PATTERNS) + r')\s*[:\n]',
    re.IGNORECASE | re.MULTILINE,
)


def extract_method_sections(text: str) -> List[str]:
    """Extract method or procedure sections from paper text."""
    sections: List[str] = []
    lower_text = text
    matches = list(_HEADING_PATTERN.finditer(lower_text))

    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(lower_text)
        section = lower_text[start:end].strip()
        if section:
            sections.append(section)

    # Fallback: use longer text chunks if no explicit headings found
    if not sections:
        for label in ['method', 'analysis', 'pipeline', 'experimental']:
            pattern = re.compile(rf'([A-Z][^\n]{{0,1000}}?{label}[^\n]{{0,1000}})', re.IGNORECASE)
            match = pattern.search(text)
            if match:
                sections.append(match.group(1).strip())
                break

    return [section[:3000] for section in sections]


def extract_code_snippets(text: str) -> List[str]:
    """Extract code-like snippets from paper text."""
    snippets: List[str] = []
    for match in _CODE_BLOCK_PATTERN.finditer(text):
        snippet = match.group(0).strip()
        if snippet:
            snippets.append(snippet)
    return snippets


def extract_paper_steps(text: str) -> Dict[str, List[str]]:
    """Extract structured method/code steps from paper text."""
    return {
        'method_sections': extract_method_sections(text),
        'code_snippets': extract_code_snippets(text),
    }
