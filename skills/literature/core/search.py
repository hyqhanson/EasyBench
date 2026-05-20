"""Search utilities for literature and dataset discovery."""

import re
from typing import Dict, List
from urllib.parse import quote_plus

import requests

PUBMED_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def search_pubmed(query: str, max_results: int = 5) -> List[str]:
    """Search PubMed and return a list of PMIDs."""
    try:
        url = (
            f"{PUBMED_EUTILS}/esearch.fcgi?db=pubmed&term={quote_plus(query)}"
            f"&retmax={max_results}&retmode=json&tool=OmicsClaw&email=omicsclaw@example.com"
        )
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
        return data.get('esearchresult', {}).get('idlist', []) or []
    except Exception:
        return []


def fetch_pubmed_article(pmid: str) -> Dict[str, str]:
    """Fetch PubMed article metadata and abstract text."""
    try:
        url = f"{PUBMED_EUTILS}/efetch.fcgi?db=pubmed&id={pmid}&retmode=xml"
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        xml = response.text

        title = _extract_single(xml, r'<ArticleTitle>(.*?)</ArticleTitle>')
        abstract_parts = re.findall(r'<AbstractText[^>]*>(.*?)</AbstractText>', xml, re.S)
        abstract = ' '.join(_clean_text(part) for part in abstract_parts)
        journal = _extract_single(xml, r'<Title>(.*?)</Title>')
        author_pairs = re.findall(r'<LastName>(.*?)</LastName>\s*<ForeName>(.*?)</ForeName>', xml)
        authors = ', '.join(f'{ln} {fn}' for ln, fn in author_pairs)

        return {
            'pmid': pmid,
            'title': title,
            'abstract': abstract,
            'journal': journal,
            'authors': authors,
        }
    except Exception:
        return {}


def _extract_single(text: str, pattern: str) -> str:
    match = re.search(pattern, text, re.S)
    return _clean_text(match.group(1)) if match else ''


def _clean_text(text: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()
