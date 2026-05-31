"""Search and fetch utilities for literature and dataset discovery.

Each ``search_*`` function queries an external API and returns candidate
records with ``title``, ``summary`` (abstract / description), and metadata.
Each ``fetch_*`` function takes an identifier (DOI, PMID, arxiv ID, etc.)
and returns enriched full-text content for downstream LLM extraction.
"""

import logging
import os
import random
import re
import time as _time
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

import requests

logger = logging.getLogger(__name__)

# Timeout for external HTTP requests (connect + read).
_SEARCH_TIMEOUT = 15  # seconds

PUBMED_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

# Global rate-limiters for APIs with strict limits
_last_call: Dict[str, float] = {}


def _rate_limit(source: str, min_interval: float = 1.0) -> None:
    """Ensure at least *min_interval* seconds since the last call to *source*."""
    elapsed = _time.time() - _last_call.get(source, 0.0)
    if elapsed < min_interval:
        _time.sleep(min_interval - elapsed)
    _last_call[source] = _time.time()

# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _extract_single(text: str, pattern: str) -> str:
    match = re.search(pattern, text, re.S)
    return _clean_text(match.group(1)) if match else ''


def _clean_text(text: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


# ---------------------------------------------------------------------------
# PubMed
# ---------------------------------------------------------------------------

def search_pubmed(query: str, max_results: int = 5) -> List[str]:
    """Search PubMed and return a list of PMIDs.

    Only returns papers with free full text (``fft[Filter]``) to ensure
    the Europe PMC full-text enrichment can actually fetch content.
    Automatically excludes reviews, meta-analyses, editorials.
    """
    try:
        # fft[Filter] = only papers with free full text available
        # Also exclude reviews to focus on original research
        filters = ' AND fft[Filter] NOT (Review[pt] OR Systematic Review[pt] OR Meta-Analysis[pt] OR Editorial[pt] OR Comment[pt])'
        url = (
            f"{PUBMED_EUTILS}/esearch.fcgi?db=pubmed&term={quote_plus(query + filters)}"
            f"&retmax={max_results}&retmode=json&tool=OmicsClaw&email=omicsclaw@example.com"
        )
        response = requests.get(url, timeout=_SEARCH_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        return data.get('esearchresult', {}).get('idlist', []) or []
    except Exception:
        return []


def fetch_pubmed_article(pmid: str) -> Dict[str, str]:
    """Fetch PubMed article metadata and abstract text."""
    try:
        url = f"{PUBMED_EUTILS}/efetch.fcgi?db=pubmed&id={pmid}&retmode=xml"
        response = requests.get(url, timeout=_SEARCH_TIMEOUT)
        response.raise_for_status()
        xml = response.text

        title = _extract_single(xml, r'<ArticleTitle>(.*?)</ArticleTitle>')
        abstract_parts = re.findall(r'<AbstractText[^>]*>(.*?)</AbstractText>', xml, re.S)
        abstract = ' '.join(_clean_text(part) for part in abstract_parts)
        journal = _extract_single(xml, r'<Title>(.*?)</Title>')
        author_pairs = re.findall(r'<LastName>(.*?)</LastName>\s*<ForeName>(.*?)</ForeName>', xml)
        authors = ', '.join(f'{ln} {fn}' for ln, fn in author_pairs)
        doi = _extract_single(xml, r'<ArticleId[^>]*IdType="doi"[^>]*>(.*?)</ArticleId>')

        return {
            'pmid': pmid,
            'title': title,
            'abstract': abstract,
            'journal': journal,
            'authors': authors,
            'doi': doi,
        }
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------

def search_arxiv(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """Search arXiv API for preprints matching *query*.

    Results are restricted to ``cat:q-bio`` (Quantitative Biology) category
    to avoid irrelevant CS/Math/Physics papers.
    """
    try:
        # Rate limit: arXiv allows ~1 req / 10 sec without an API key
        _rate_limit('arxiv', min_interval=10.0)
        # Restrict to quantitative biology category
        # Build search query and encode ONCE to avoid double-encoding
        search_query = f'all:({query}) AND cat:q-bio.* ANDNOT ti:Review ANDNOT ti:Survey'
        search_url = (
            'http://export.arxiv.org/api/query?search_query='
            f'{requests.utils.quote(search_query)}'
            f'&start=0&max_results={max_results}'
        )
        response = requests.get(search_url, timeout=_SEARCH_TIMEOUT)
        response.raise_for_status()
        entries = re.findall(r'<entry>(.*?)</entry>', response.text, re.S)
        results: List[Dict[str, Any]] = []
        for entry in entries[:max_results]:
            title = re.search(r'<title>(.*?)</title>', entry, re.S)
            summary = re.search(r'<summary>(.*?)</summary>', entry, re.S)
            link = re.search(r'<id>(.*?)</id>', entry, re.S)
            arxiv_id = ''
            if link:
                arxiv_id = link.group(1).strip().split('/')[-1]
            results.append({
                'title': title.group(1).strip() if title else '',
                'summary': summary.group(1).strip() if summary else '',
                'source': 'arxiv',
                'id': arxiv_id,
                'url': link.group(1).strip() if link else '',
            })
        return results
    except Exception:
        return []


def fetch_arxiv_article(arxiv_id: str) -> Dict[str, str]:
    """Fetch full abstract and metadata for a single arXiv paper by its ID.

    Also attempts to download the PDF and extract full text for richer LLM analysis.
    """
    result: Dict[str, str] = {
        'arxiv_id': arxiv_id,
        'title': '',
        'abstract': '',
        'doi': '',
        'url': '',
        'published': '',
        'full_text': '',  # PDF-extracted full text
    }

    # Step 1: Get metadata + abstract from arXiv API
    try:
        url = f'http://export.arxiv.org/api/query?id_list={arxiv_id}'
        response = requests.get(url, timeout=_SEARCH_TIMEOUT)
        response.raise_for_status()
        entry = re.search(r'<entry>(.*?)</entry>', response.text, re.S)
        if entry:
            text = entry.group(1)
            result['title'] = _extract_single(text, r'<title>(.*?)</title>')
            result['abstract'] = _extract_single(text, r'<summary>(.*?)</summary>')
            result['doi'] = _extract_single(text, r'<arxiv:doi[^>]*>(.*?)</arxiv:doi>')
            link = _extract_single(text, r'<id>(.*?)</id>')
            result['url'] = link
            result['published'] = _extract_single(text, r'<published>(.*?)</published>')
    except Exception:
        pass

    # Step 2: Try to download PDF and extract full text
    # arXiv PDF URL: https://arxiv.org/pdf/{arxiv_id}.pdf (without version suffix)
    pdf_id = arxiv_id.split('v')[0] if 'v' in arxiv_id else arxiv_id
    pdf_url = f'https://arxiv.org/pdf/{pdf_id}.pdf'
    try:
        resp = requests.get(pdf_url, timeout=_SEARCH_TIMEOUT * 2)
        resp.raise_for_status()
        import io
        # Suppress pypdf's noisy warnings about malformed PDF structures
        import logging as _logging
        _logging.getLogger('pypdf').setLevel(_logging.ERROR)
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(resp.content))
        pages = [p.extract_text() for p in reader.pages if p.extract_text()]
        if pages:
            result['full_text'] = '\n\n'.join(pages)[:30000]
    except Exception:
        pass

    return result


# ---------------------------------------------------------------------------
# Zenodo
# ---------------------------------------------------------------------------

def search_zenodo(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """Search Zenodo API for records matching *query*."""
    try:
        api_url = f'https://zenodo.org/api/records/?q={requests.utils.quote(query)}&size={max_results}'
        response = requests.get(api_url, timeout=_SEARCH_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        results: List[Dict[str, Any]] = []
        for record in data.get('hits', {}).get('hits', [])[:max_results]:
            metadata = record.get('metadata', {})
            results.append({
                'title': metadata.get('title', ''),
                'summary': metadata.get('description', ''),
                'source': 'zenodo',
                'id': str(record.get('id', '')),
                'url': record.get('links', {}).get('html', ''),
            })
        return results
    except Exception:
        return []


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def search_github(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """Search GitHub for repositories matching *query*."""
    token = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
    headers = {'Accept': 'application/vnd.github.v3+json'}
    if token:
        headers['Authorization'] = f'token {token}'
    try:
        api_url = (
            f'https://api.github.com/search/repositories'
            f'?q={requests.utils.quote(query)}+language:python'
            f'&sort=stars&order=desc&per_page={max_results}'
        )
        response = requests.get(api_url, headers=headers, timeout=_SEARCH_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        results: List[Dict[str, Any]] = []
        for repo in data.get('items', [])[:max_results]:
            results.append({
                'title': repo.get('full_name', ''),
                'summary': repo.get('description', ''),
                'source': 'github',
                'id': repo.get('full_name', ''),
                'url': repo.get('html_url', ''),
            })
        return results
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Google Scholar
# ---------------------------------------------------------------------------

def search_google_scholar(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """Search Google Scholar by scraping HTML (fragile; prefer Semantic Scholar)."""
    try:
        url = f'https://scholar.google.com/scholar?q={requests.utils.quote(query)}'
        headers = {'User-Agent': 'Mozilla/5.0 (OmicsClaw Literature Scraper)'}
        response = requests.get(url, headers=headers, timeout=_SEARCH_TIMEOUT)
        response.raise_for_status()
        text = response.text
        titles = re.findall(r'<h3 class="gs_rt">.*?<a[^>]*>(.*?)</a>', text, re.S)
        urls = re.findall(r'<h3 class="gs_rt">.*?<a href="([^"]+)"', text, re.S)
        snippets = re.findall(r'<div class="gs_rs">(.*?)</div>', text, re.S)
        results: List[Dict[str, Any]] = []
        for idx, title in enumerate(titles[:max_results]):
            summary = re.sub(r'<[^>]+>', ' ', snippets[idx]) if idx < len(snippets) else ''
            results.append({
                'title': re.sub(r'<[^>]+>', ' ', title).strip(),
                'summary': summary.strip(),
                'source': 'google_scholar',
                'id': f'scholar_{idx + 1}',
                'url': urls[idx] if idx < len(urls) else '',
            })
        return results
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Semantic Scholar
# ---------------------------------------------------------------------------

def search_semantic_scholar(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """Search Semantic Scholar API for papers with dataset accessions and code."""
    try:
        # Rate limit: Semantic Scholar free tier: ~1 req / 3 sec
        _rate_limit('semantic_scholar', min_interval=3.0)
        url = (
            'https://api.semanticscholar.org/graph/v1/paper/search'
            f'?query={requests.utils.quote(query)}'
            f'&limit={max_results}'
            '&fields=title,externalIds,url,abstract,publicationDate,citationCount'
        )
        headers = {'User-Agent': 'OmicsClaw/1.0 (mailto:omicsclaw@example.com)'}
        response = requests.get(url, headers=headers, timeout=_SEARCH_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        results: List[Dict[str, Any]] = []
        for paper in data.get('data', [])[:max_results]:
            ext_ids = paper.get('externalIds', {}) or {}
            pmid = ext_ids.get('PubMed', '')
            doi = ext_ids.get('DOI', '')
            paper_url = paper.get('url', '')
            if doi:
                paper_url = f'https://doi.org/{doi}'
            results.append({
                'title': paper.get('title', ''),
                'summary': paper.get('abstract', '') or '',
                'source': 'semantic_scholar',
                'id': ext_ids.get('CorpusId', paper.get('paperId', '')),
                'url': paper_url,
                'doi': doi,
                'pmid': pmid,
                'citation_count': paper.get('citationCount', 0),
                'publication_date': paper.get('publicationDate', ''),
            })
        return results
    except Exception:
        return []


def fetch_semantic_scholar_details(paper_id: str) -> Dict[str, Any]:
    """Fetch enriched paper details from Semantic Scholar by paperId or DOI."""
    try:
        url = (
            f'https://api.semanticscholar.org/graph/v1/paper/{paper_id}'
            '?fields=title,abstract,externalIds,url,publicationDate,citationCount,'
            'references.title,references.externalIds,tldr'
        )
        headers = {'User-Agent': 'OmicsClaw/1.0 (mailto:omicsclaw@example.com)'}
        response = requests.get(url, headers=headers, timeout=_SEARCH_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        ext_ids = data.get('externalIds', {}) or {}
        return {
            'title': data.get('title', ''),
            'abstract': data.get('abstract', '') or '',
            'doi': ext_ids.get('DOI', ''),
            'pmid': ext_ids.get('PubMed', ''),
            'url': data.get('url', ''),
            'citation_count': data.get('citationCount', 0),
            'publication_date': data.get('publicationDate', ''),
            'tldr': (data.get('tldr') or {}).get('text', '') if data.get('tldr') else '',
        }
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# bioRxiv / medRxiv (via Europe PMC)
# ---------------------------------------------------------------------------

def search_biorxiv(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """Search bioRxiv/medRxiv via Europe PMC API for preprints."""
    try:
        epmc_url = (
            'https://www.ebi.ac.uk/europepmc/webservices/rest/search'
            f'?query=({requests.utils.quote(query)})+AND+'
            f'(SRC:PPR)+AND+'
            f'(JOURNAL:bioRxiv%20OR%20JOURNAL:medRxiv)'
            f'&resultType=core&pageSize={max_results}&format=json'
        )
        response = requests.get(epmc_url, timeout=_SEARCH_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        articles = (data.get('resultList', {}).get('result', []) or [])
        
        # If full query returned 0, retry with simplified query
        # (bioRxiv titles/abstracts are shorter; complex multi-term queries over-match)
        if not articles:
            # Take first 3 meaningful terms from the query
            terms = [t for t in query.split() if not t.startswith('"') and len(t) > 3][:3]
            if terms and len(terms) < len(query.split()):
                simple_query = ' '.join(terms)
                simple_url = (
                    'https://www.ebi.ac.uk/europepmc/webservices/rest/search'
                    f'?query=({requests.utils.quote(simple_query)})+AND+'
                    f'(SRC:PPR)+AND+'
                    f'(JOURNAL:bioRxiv%20OR%20JOURNAL:medRxiv)'
                    f'&resultType=core&pageSize={max_results}&format=json'
                )
                retry = requests.get(simple_url, timeout=_SEARCH_TIMEOUT)
                retry.raise_for_status()
                articles = (retry.json().get('resultList', {}).get('result', []) or [])
                if articles:
                    logger.debug('bioRxiv simplified query "%s" returned %d results (original 0)', simple_query, len(articles))

        results: List[Dict[str, Any]] = []
        for article in articles[:max_results]:
            doi = article.get('doi', '')
            results.append({
                'title': article.get('title', ''),
                'summary': article.get('abstractText', '') or '',
                'source': 'biorxiv',
                'id': doi or article.get('id', ''),
                'url': f'https://doi.org/{doi}' if doi else '',
                'doi': doi,
                'pmid': article.get('pmid', ''),
                'source_type': article.get('source', 'PPR'),
            })
        return results
    except Exception:
        return []


def fetch_biorxiv_article(doi: str) -> Dict[str, str]:
    """Fetch metadata and full text for a bioRxiv/medRxiv preprint by DOI.

    Downloads the full-text PDF from bioRxiv and extracts text via pypdf.
    PDF URL format: https://www.biorxiv.org/content/{doi}.full.pdf
    """
    result: Dict[str, str] = {
        'doi': doi,
        'title': '',
        'abstract': '',
        'full_text': '',
    }

    # Try to get metadata + abstract from Europe PMC first
    try:
        epmc_data = fetch_europe_pmc_fulltext(doi)
        if epmc_data:
            result['title'] = epmc_data.get('title', '')
            result['abstract'] = epmc_data.get('abstract', '')
    except Exception:
        pass

    # Download PDF and extract full text
    # Note: bioRxiv uses Cloudflare protection, so direct PDF download may fail (403).
    # Fall back to fetching the HTML version via DOI resolution.
    pdf_url = f'https://www.biorxiv.org/content/{doi}.full.pdf'
    try:
        resp = requests.get(pdf_url, timeout=_SEARCH_TIMEOUT * 2, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        })
        if resp.status_code == 200:
            import io
            import logging as _logging
            _logging.getLogger('pypdf').setLevel(_logging.ERROR)
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(resp.content))
            pages = [p.extract_text() for p in reader.pages if p.extract_text()]
            if pages:
                result['full_text'] = '\n\n'.join(pages)[:30000]
    except Exception:
        pass

    # If PDF failed, try DOI page content as fallback
    if not result['full_text']:
        try:
            from literature.core.parser import parse_doi
            doi_text = parse_doi(doi)
            if doi_text and not doi_text.startswith('Error') and len(doi_text) > 500:
                result['full_text'] = doi_text[:30000]
        except Exception:
            pass

    return result


# ---------------------------------------------------------------------------
# Figshare
# ---------------------------------------------------------------------------

def search_figshare(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """Search Figshare API for datasets and code related to single-cell omics."""
    try:
        api_url = (
            'https://api.figshare.com/v2/articles/search'
            f'?search_for={requests.utils.quote(query)}'
            f'&page_size={max_results}&order=recent'
        )
        headers = {'Content-Type': 'application/json'}
        response = requests.post(api_url, headers=headers, timeout=_SEARCH_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        results: List[Dict[str, Any]] = []
        for article in (data or [])[:max_results]:
            results.append({
                'title': article.get('title', ''),
                'summary': article.get('description', '') or '',
                'source': 'figshare',
                'id': str(article.get('id', '')),
                'url': article.get('url', article.get('figshare_url', '')),
                'doi': article.get('doi', ''),
                'published_date': article.get('published_date', ''),
            })
        return results
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Europe PMC
# ---------------------------------------------------------------------------

def search_europe_pmc(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """Search Europe PMC for full-text indexed articles with dataset accessions."""
    try:
        epmc_url = (
            'https://www.ebi.ac.uk/europepmc/webservices/rest/search'
            f'?query=({requests.utils.quote(query)})+AND+'
            f'(GEO%20OR%20GSE%20OR%20ArrayExpress%20OR%20SRA%20OR%20cellxgene%20OR%20GitHub)'
            f'+AND+OPEN_ACCESS%3AY'
            f'&resultType=core&pageSize={max_results}&format=json'
        )
        response = requests.get(epmc_url, timeout=_SEARCH_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        results: List[Dict[str, Any]] = []
        for article in (data.get('resultList', {}).get('result', []) or [])[:max_results]:
            doi = article.get('doi', '')
            pmid = article.get('pmid', '')
            results.append({
                'title': article.get('title', ''),
                'summary': article.get('abstractText', '') or '',
                'source': 'europe_pmc',
                'id': pmid or doi or article.get('id', ''),
                'url': f'https://doi.org/{doi}' if doi else (
                    f'https://europepmc.org/article/MED/{pmid}' if pmid else ''),
                'doi': doi,
                'pmid': pmid,
                'journal': article.get('journalTitle', ''),
                'pub_year': article.get('pubYear', ''),
            })
        return results
    except Exception:
        return []


def fetch_europe_pmc_fulltext(pmid_or_doi: str) -> Dict[str, Any]:
    """Fetch enriched article details (including full text sections if open access)
    from Europe PMC by PMID or DOI."""
    try:
        epmc_url = (
            'https://www.ebi.ac.uk/europepmc/webservices/rest/search'
            f'?query={quote_plus(pmid_or_doi)}'
            f'&resultType=core&pageSize=1&format=json'
        )
        response = requests.get(epmc_url, timeout=_SEARCH_TIMEOUT)
        response.raise_for_status()
        articles = (response.json().get('resultList', {}).get('result', []) or [])
        if not articles:
            return {}
        art = articles[0]
        # Try to get full text sections for open-access articles
        full_text_sections = []
        if art.get('hasFullText', 'N') == 'Y':
            # Support multiple source types: MED (PubMed), PPR (bioRxiv/medRxiv), etc.
            src = art.get('source', 'MED')
            ft_url = (
                'https://www.ebi.ac.uk/europepmc/webservices/rest/'
                f'{src}/{art.get("id", "")}/fullTextXML'
            )
            try:
                ft_resp = requests.get(ft_url, timeout=_SEARCH_TIMEOUT)
                if ft_resp.ok:
                    ft_text = re.sub(r'<[^>]+>', ' ', ft_resp.text)
                    ft_text = re.sub(r'\s+', ' ', ft_text)
                    full_text_sections.append(ft_text[:10000])
            except Exception:
                pass

        return {
            'title': art.get('title', ''),
            'abstract': art.get('abstractText', '') or '',
            'doi': art.get('doi', ''),
            'pmid': art.get('pmid', ''),
            'journal': art.get('journalTitle', ''),
            'pub_year': art.get('pubYear', ''),
            'full_text_sections': full_text_sections,
            'has_full_text': art.get('hasFullText', 'N'),
        }
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Generic full-text enrichment via DOI
# ---------------------------------------------------------------------------

def fetch_full_text_by_doi(doi: str) -> str:
    """Attempt to fetch full article text via DOI resolution.

    Uses the DOI page content extractor (``literature.core.parser.parse_doi``)
    to get rendered HTML, then returns cleaned text.
    """
    try:
        from literature.core.parser import parse_doi
        text = parse_doi(doi)
        if text and not text.startswith('Error'):
            return text
    except Exception:
        pass
    return ''


# ---------------------------------------------------------------------------
# Timed-search wrapper (used by the orchestration loop)
# ---------------------------------------------------------------------------

def timed_search(search_fn, deadline: float, *args, label: str = '', **kwargs) -> list:
    """Call *search_fn* but skip if the deadline has already passed.

    Each individual ``requests.get(…, timeout=…)`` already enforces
    ``_SEARCH_TIMEOUT`` seconds per call.  This wrapper simply skips
    calls when the total multi-source budget is exhausted.
    """
    source_name = label or getattr(search_fn, '__name__', 'unknown')
    if _time.time() > deadline:
        logger.warning('Deadline exceeded, skipping source %s', source_name)
        return []
    start = _time.time()
    try:
        result = list(search_fn(*args, **kwargs) or [])
        elapsed = _time.time() - start
        if result:
            logger.debug('Source %s returned %d results in %.1fs', source_name, len(result), elapsed)
        else:
            logger.debug('Source %s returned 0 results in %.1fs', source_name, elapsed)
        return result
    except Exception as exc:
        elapsed = _time.time() - start
        logger.warning('Source %s failed after %.1fs: %s', source_name, elapsed, exc)
        return []
