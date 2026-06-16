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
import urllib3
# Disable InsecureRequestWarning for verify=False fallbacks (e.g. Springer Nature)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

# Timeout for external HTTP requests (connect + read).
_SEARCH_TIMEOUT = 15  # seconds

PUBMED_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

# Email for Unpaywall API (required; used only for API rate-limiting accountability)
_UNPAYWALL_EMAIL = '24110720041@m.fudan.edu.cn'

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

    Tries progressively broader searches:
    1. ``fft[Filter]`` (free full text) + exclusions
    2. Without fft filter + exclusions
    3. Minimal filtering — just the raw query with only Review exclusion

    Automatically excludes reviews, meta-analyses, editorials.
    """
    # Common exclusion filters
    _exclude = ' NOT (Review[pt] OR Systematic Review[pt] OR Meta-Analysis[pt] OR Editorial[pt] OR Comment[pt])'
    _light_exclude = ' NOT (Review[pt])'

    # --- Attempt 1: with fft[Filter] ---
    try:
        filters_fft = f' AND fft[Filter]{_exclude}'
        url = (
            f"{PUBMED_EUTILS}/esearch.fcgi?db=pubmed&term={quote_plus(query + filters_fft)}"
            f"&retmax={max_results}&retmode=json&tool=OmicsClaw&email=omicsclaw@example.com"
        )
        response = requests.get(url, timeout=_SEARCH_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        ids = data.get('esearchresult', {}).get('idlist', []) or []
        if ids:
            logger.debug('PubMed fft search returned %d PMIDs for: %s', len(ids), query[:60])
            return ids
        logger.debug('PubMed fft search returned 0 for: %s — retrying without fft filter', query[:60])
    except Exception as exc:
        logger.debug('PubMed fft search failed: %s — retrying without fft filter', exc)

    # --- Attempt 2: without fft[Filter] (broader) ---
    try:
        url2 = (
            f"{PUBMED_EUTILS}/esearch.fcgi?db=pubmed&term={quote_plus(query + _exclude)}"
            f"&retmax={max_results}&retmode=json&tool=OmicsClaw&email=omicsclaw@example.com"
        )
        resp2 = requests.get(url2, timeout=_SEARCH_TIMEOUT)
        resp2.raise_for_status()
        data2 = resp2.json()
        ids2 = data2.get('esearchresult', {}).get('idlist', []) or []
        if ids2:
            logger.debug('PubMed broad search returned %d PMIDs for: %s', len(ids2), query[:60])
            return ids2
        logger.debug('PubMed broad search returned 0 for: %s — retrying with light filtering', query[:60])
    except Exception as exc:
        logger.debug('PubMed broad search also failed: %s', exc)

    # --- Attempt 3: minimal filtering, raw query only ---
    try:
        url3 = (
            f"{PUBMED_EUTILS}/esearch.fcgi?db=pubmed&term={quote_plus(query + _light_exclude)}"
            f"&retmax={max_results * 2}&retmode=json&tool=OmicsClaw&email=omicsclaw@example.com"
        )
        resp3 = requests.get(url3, timeout=_SEARCH_TIMEOUT)
        resp3.raise_for_status()
        data3 = resp3.json()
        ids3 = data3.get('esearchresult', {}).get('idlist', []) or []
        logger.debug('PubMed light-filter search returned %d PMIDs for: %s', len(ids3), query[:60])
        return ids3
    except Exception as exc:
        logger.debug('PubMed light-filter search also failed: %s', exc)
        return []


def search_pubmed_as_source(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """Search PubMed and return results as dicts (same shape as other sources).

    Unlike ``search_pubmed`` (which returns raw PMIDs), this function
    fetches full metadata, enriches with DOI page content and Europe PMC
    full text, and returns a list of dicts compatible with ``timed_search``.
    """
    items: List[Dict[str, Any]] = []
    seen: set = set()
    for pmid in search_pubmed(query, max_results=max_results):
        if pmid in seen:
            continue
        seen.add(pmid)
        article = fetch_pubmed_article(pmid)
        if not article or not article.get('title'):
            continue
        raw_text = f"Title: {article.get('title', '')}\n\nAbstract: {article.get('abstract', '')}"
        doi = article.get('doi', '')
        if doi:
            try:
                from literature.core.parser import parse_doi
                doi_text = parse_doi(doi)
                if doi_text and not doi_text.startswith('Error fetching'):
                    is_useful = (
                        len(doi_text) > 200 and
                        not doi_text.strip().startswith('<?xml')
                    )
                    if is_useful:
                        raw_text = raw_text + '\n\nDOI_PAGE_CONTENT:\n' + doi_text[:80000]
            except Exception:
                logger.debug('Failed to fetch DOI page content for %s', doi, exc_info=True)
        try:
            epmc_data = fetch_europe_pmc_fulltext(pmid)
            if epmc_data and epmc_data.get('abstract'):
                ft = (
                    f"Title: {epmc_data.get('title', '')}"
                    f"\n\nAbstract: {epmc_data.get('abstract', '')}"
                )
                if epmc_data.get('full_text_sections'):
                    ft += "\n\nFULL_TEXT:\n" + '\n'.join(epmc_data['full_text_sections'])[:20000]
                if len(ft) > len(raw_text):
                    raw_text = ft
        except Exception:
            pass
        # Step 3: Try Unpaywall OA full text (catches OA papers not in PMC)
        if doi and 'FULL_TEXT' not in raw_text and 'DOI_PAGE_CONTENT' not in raw_text:
            try:
                unpaywall_text = fetch_unpaywall_text(doi)
                if unpaywall_text:
                    raw_text += f"\n\nFULL_TEXT:\n{unpaywall_text}"
            except Exception:
                pass
        items.append({
            'title': article.get('title', ''),
            'summary': article.get('abstract', ''),
            'source': 'pubmed',
            'id': pmid,
            'url': f'https://pubmed.ncbi.nlm.nih.gov/{pmid}/',
            'doi': doi,
            'raw_text': raw_text,
        })
    return items


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

        logger.debug('Fetched PubMed article %s: title=%s, doi=%s', pmid, bool(title), bool(doi))
        return {
            'pmid': pmid,
            'title': title,
            'abstract': abstract,
            'journal': journal,
            'authors': authors,
            'doi': doi,
        }
    except Exception as exc:
        logger.debug('Failed to fetch PubMed article %s: %s', pmid, exc)
        return {}


# ---------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------

def search_arxiv(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """Search arXiv API for preprints matching *query*.

    Results are restricted to ``cat:q-bio`` (Quantitative Biology) category
    to avoid irrelevant CS/Math/Physics papers.  Long queries are
    automatically simplified to extract the most meaningful terms.
    """
    try:
        # arXiv rate limit: ~1 req / 10 sec without an API key
        _rate_limit('arxiv', min_interval=10.0)

        # Simplify long queries — arXiv API chokes on complex multi-term queries
        # Extract 3-5 meaningful biological terms, drop method/action words
        _stop_words = {
            'analysis', 'analyses', 'reveals', 'identifies', 'shows', 'using',
            'based', 'across', 'between', 'via', 'from', 'through', 'data',
            'dataset', 'datasets', 'study', 'studies', 'approach', 'method',
            'novel', 'new', 'multiple', 'comprehensive', 'integrative',
            'integration', 'integrated', 'integrating', 'cross-dataset',
            'multi-cohort', 'multi-dataset', 'multi-sample',
        }
        terms = [
            t for t in query.lower().split()
            if len(t) > 2 and t not in _stop_words
            and not t.startswith('gse') and not t.startswith('geo')
        ][:5]
        # Always include a single-cell signal if present
        _sc_signals = {'single-cell', 'scrna-seq', 'snrna-seq', 'single-nucleus',
                       'singlecell', 'scrna', 'spatial transcriptomics'}
        has_sc = any(s in query.lower() for s in _sc_signals)
        if not has_sc:
            terms = ['single-cell'] + terms
        simplified = ' '.join(terms) if terms else query.split()[:3]

        search_query = f'all:({simplified}) AND cat:q-bio.* ANDNOT ti:Review ANDNOT ti:Survey'
        search_url = (
            'http://export.arxiv.org/api/query?search_query='
            f'{requests.utils.quote(search_query)}'
            f'&start=0&max_results={max_results}'
        )
        response = requests.get(search_url, timeout=_SEARCH_TIMEOUT)
        response.raise_for_status()
        entries = re.findall(r'<entry>(.*?)</entry>', response.text, re.S)

        # If empty, retry with even shorter query (just first 3 meaningful terms)
        if not entries:
            short_terms = [t for t in simplified.split() if t not in _stop_words][:3]
            if short_terms:
                short_query = f'all:({" ".join(short_terms)}) AND cat:q-bio.* ANDNOT ti:Review ANDNOT ti:Survey'
                retry_url = (
                    'http://export.arxiv.org/api/query?search_query='
                    f'{requests.utils.quote(short_query)}'
                    f'&start=0&max_results={max_results}'
                )
                retry_resp = requests.get(retry_url, timeout=_SEARCH_TIMEOUT)
                if retry_resp.ok:
                    entries = re.findall(r'<entry>(.*?)</entry>', retry_resp.text, re.S)

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

    # Retry with different timeouts — arXiv can be slow but also fast
    _pdf_timeouts = [15, 25]  # first try short, then a bit longer
    pdf_content = None
    for attempt, timeout in enumerate(_pdf_timeouts):
        try:
            resp = requests.get(pdf_url, timeout=timeout)
            resp.raise_for_status()
            pdf_content = resp.content
            logger.debug('arXiv PDF fetched (attempt %d, timeout=%ds) for %s',
                         attempt + 1, timeout, arxiv_id)
            break
        except Exception as exc:
            logger.debug('arXiv PDF attempt %d failed for %s (timeout=%ds): %s',
                         attempt + 1, arxiv_id, timeout, exc)
            continue

    if pdf_content:
        try:
            import io
            # Suppress pypdf's noisy warnings about malformed PDF structures
            import logging as _logging
            _logging.getLogger('pypdf').setLevel(_logging.ERROR)
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(pdf_content))
            pages = [p.extract_text() for p in reader.pages if p.extract_text()]
            if pages:
                result['full_text'] = '\n\n'.join(pages)[:30000]
                logger.debug('arXiv PDF extracted %d chars for %s',
                             len(result['full_text']), arxiv_id)
        except Exception as exc:
            logger.debug('arXiv PDF text extraction failed for %s: %s', arxiv_id, exc)

    return result


def fetch_unpaywall_text(doi: str) -> str:
    """Query Unpaywall API to find OA full text for a DOI.

    Returns extracted text (up to 50 000 chars) from the best available
    OA source, or ``''`` if none found.

    Strategy:
    1. Query Unpaywall for OA status + PDF URLs
    2. Download best OA PDF and extract text via ``pypdf``
    3. Fallback to landing-page HTML scraping
    """
    try:
        url = f'https://api.unpaywall.org/v2/{requests.utils.quote(doi)}?email={_UNPAYWALL_EMAIL}'
        resp = requests.get(url, timeout=_SEARCH_TIMEOUT)
        if resp.status_code != 200:
            return ''
        data = resp.json()
        if not data.get('is_oa'):
            logger.debug('Unpaywall: %s is not OA', doi)
            return ''

        # Collect all unique PDF URLs from all OA locations
        seen_urls: set = set()
        candidate_urls: list = []

        if data.get('best_oa_location'):
            loc = data['best_oa_location']
            for key in ('url_for_pdf', 'pdf_url'):
                u = loc.get(key)
                if u and u not in seen_urls:
                    seen_urls.add(u)
                    candidate_urls.append(u)

        for loc in data.get('oa_locations', []):
            for key in ('url_for_pdf', 'pdf_url'):
                u = loc.get(key)
                if u and u not in seen_urls:
                    seen_urls.add(u)
                    candidate_urls.append(u)

        # Try each PDF URL
        for pdf_url in candidate_urls:
            try:
                pdf_resp = requests.get(pdf_url, timeout=min(10, _SEARCH_TIMEOUT))
                if not pdf_resp.ok:
                    continue
                ctype = pdf_resp.headers.get('Content-Type', '')
                if 'application/pdf' not in ctype and 'application/octet-stream' not in ctype:
                    continue
                import io
                import logging as _logging
                _logging.getLogger('pypdf').setLevel(_logging.ERROR)
                from pypdf import PdfReader
                reader = PdfReader(io.BytesIO(pdf_resp.content))
                pages = [p.extract_text() for p in reader.pages if p.extract_text()]
                if pages:
                    text = '\n\n'.join(pages)[:50000]
                    if len(text) > 500:
                        logger.debug('Unpaywall: fetched %d chars from PDF for %s', len(text), doi)
                        return text
            except Exception:
                continue

        # Fallback: try landing-page HTML via DOI resolver
        landing = data.get('best_oa_location', {}).get('url_for_landing_page', '')
        if landing:
            try:
                lr = requests.get(landing, timeout=_SEARCH_TIMEOUT,
                                  headers={'User-Agent': 'Mozilla/5.0'})
                if lr.ok:
                    import re as _re
                    body = _re.sub(r'<script[^>]*>.*?</script>', ' ', lr.text, flags=_re.S | _re.I)
                    body = _re.sub(r'<style[^>]*>.*?</style>', ' ', body, flags=_re.S | _re.I)
                    body = _re.sub(r'<[^>]+>', ' ', body)
                    body = _re.sub(r'\s+', ' ', body).strip()
                    if len(body) > 500:
                        logger.debug('Unpaywall: fetched %d chars from landing page for %s', len(body), doi)
                        return body[:50000]
            except Exception:
                pass
    except Exception as exc:
        logger.debug('Unpaywall query failed for %s: %s', doi, exc)
    return ''


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
    """Search Semantic Scholar API for papers with dataset accessions and code.

    Requires ``SEMANTIC_SCHOLAR_API_KEY`` environment variable to be set.
    Get a free API key from https://www.semanticscholar.org/product/api#api-key-form.

    Results are filtered to prefer scRNA-seq / single-cell studies over
    bulk microarray or purely computational papers.
    """
    api_key = os.environ.get('SEMANTIC_SCHOLAR_API_KEY', '').strip()
    if not api_key:
        logger.warning('SEMANTIC_SCHOLAR_API_KEY not set — skipping Semantic Scholar search.')
        return []

    try:
        # Rate limit: with API key, ~10 req / sec is allowed; be conservative
        _rate_limit('semantic_scholar', min_interval=0.5)
        url = (
            'https://api.semanticscholar.org/graph/v1/paper/search'
            f'?query={requests.utils.quote(query)}'
            f'&limit={max_results * 3}'  # fetch extra for post-filtering
            '&fields=title,externalIds,url,abstract,publicationDate,citationCount'
        )
        headers = {
            'User-Agent': 'OmicsClaw/1.0 (mailto:omicsclaw@example.com)',
            'x-api-key': api_key,
        }
        _session = requests.Session()
        _session.trust_env = False  # bypass system proxy (e.g. Clash/V2Ray)
        response = _session.get(url, headers=headers, timeout=_SEARCH_TIMEOUT)
        data = response.json()

        # Keywords that indicate scRNA-seq / single-cell studies
        _sc_keywords = {
            'single-cell', 'single cell', 'scRNA-seq', 'scRNAseq', 'snRNA-seq',
            'single-nucleus', 'single nucleus', 'scATAC-seq', 'spatial transcriptom',
            '10x genomics', 'cell atlas', 'cell type', 'cellxgene',
        }
        # Keywords that indicate irrelevant bulk / microarray studies
        _bulk_keywords = {
            'microarray', 'bulk RNA-seq', 'bulk rna-seq', 'TCGA', 'GEO microarray',
            'Affymetrix', 'RNA-seq data from GEO', 'RNA-seq datasets from',
        }

        results: List[Dict[str, Any]] = []
        for paper in data.get('data', [])[:max_results * 3]:
            title = (paper.get('title') or '').lower()
            abstract = (paper.get('abstract') or '').lower()
            text = title + ' ' + abstract

            # Skip papers that mention bulk/microarray but NOT single-cell
            has_bulk = any(kw in text for kw in _bulk_keywords)
            has_sc = any(kw in text for kw in _sc_keywords)
            if has_bulk and not has_sc:
                continue
            # Prefer papers with single-cell signals
            if not has_sc:
                continue

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
            if len(results) >= max_results:
                break
        return results
    except Exception:
        return []


def fetch_semantic_scholar_details(paper_id: str) -> Dict[str, Any]:
    """Fetch enriched paper details from Semantic Scholar by paperId or DOI.

    Requires ``SEMANTIC_SCHOLAR_API_KEY`` environment variable to be set.
    """
    api_key = os.environ.get('SEMANTIC_SCHOLAR_API_KEY', '').strip()
    if not api_key:
        logger.warning('SEMANTIC_SCHOLAR_API_KEY not set — skipping details fetch.')
        return {}

    try:
        url = (
            f'https://api.semanticscholar.org/graph/v1/paper/{paper_id}'
            '?fields=title,abstract,externalIds,url,publicationDate,citationCount,'
            'references.title,references.externalIds,tldr'
        )
        headers = {
            'User-Agent': 'OmicsClaw/1.0 (mailto:omicsclaw@example.com)',
            'x-api-key': api_key,
        }
        _session = requests.Session()
        _session.trust_env = False  # bypass system proxy
        response = _session.get(url, headers=headers, timeout=_SEARCH_TIMEOUT)
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
# Springer Nature (Nature journals + sub-journals)
# ---------------------------------------------------------------------------
# Requires these environment variables:
#   SPRINGER_NATURE_API_KEY   — Meta API key from https://dev.springernature.com/
#   SPRINGER_NATURE_OA_API_KEY — Open Access API key (same site, different key)
#   UNIVERSITY_PROXY          — HTTP proxy for PDF download (e.g. http://proxy.univ.edu:8080)
#                               Set to empty string or omit to skip proxy PDF downloads.

_SPRINGER_NATURE_API_URL = 'https://api.springernature.com/metadata/json'


def _get_proxy_url() -> str:
    """Get university proxy URL securely.

    Priority:
    1. ``UNIVERSITY_PROXY`` environment variable (for backward compatibility)
    2. ``keyring``: retrieves password stored as
       ``keyring.set_password('OmicsClaw.FudanProxy', '<username>', '<password>')``

    Returns empty string if no proxy is configured.
    """
    # 1. Check env var (backward compat / Linux)
    proxy = os.environ.get('UNIVERSITY_PROXY', '').strip()
    if proxy:
        return proxy

    # 2. Try keyring (Windows Credential Manager / macOS Keychain / Linux Secret Service)
    _fudan_user = '24110720041'
    try:
        import keyring
        pwd = keyring.get_password('OmicsClaw.FudanProxy', _fudan_user)
        if pwd:
            from urllib.parse import quote
            proxy = f'http://{quote(_fudan_user, safe="")}:{quote(pwd, safe="")}@libproxy.fudan.edu.cn:8080'
            logger.debug('Loaded proxy credentials from keyring')
            return proxy
    except Exception:
        pass

    return ''


def search_springer_nature(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """Search Springer Nature API for papers matching *query*.

    Returns papers from Nature-branded journals (Nature, Nature Methods,
    Nature Biotechnology, Nature Communications, Scientific Reports, etc.).

    Environment variable ``SPRINGER_NATURE_API_KEY`` must be set.
    Returns an empty list if the key is missing.
    """
    api_key = os.environ.get('SPRINGER_NATURE_API_KEY', '').strip()
    oa_api_key = os.environ.get('SPRINGER_NATURE_OA_API_KEY', '').strip()
    if not api_key and not oa_api_key:
        logger.warning('SPRINGER_NATURE_API_KEY / SPRINGER_NATURE_OA_API_KEY not set — skipping Springer Nature search.')
        return []

    records = []
    # Try Meta API first
    if api_key:
        try:
            _rate_limit('springer_nature', min_interval=1.0)
            params = {
                'q': query,
                'api_key': api_key,
                'p': min(max_results, 20),
                's': 0,
            }
            resp = requests.get(
                _SPRINGER_NATURE_API_URL,
                params=params,
                timeout=_SEARCH_TIMEOUT,
            )
            if resp.ok:
                records = resp.json().get('records', [])
                logger.debug('Springer Nature Meta API returned %d records', len(records))
        except Exception as exc:
            logger.debug('Springer Nature Meta API error: %s', exc)

    # Fallback to Open Access API
    if not records and oa_api_key:
        try:
            _rate_limit('springer_nature', min_interval=1.0)
            oa_url = (
                'https://api.springernature.com/openaccess/json'
                f'?q={requests.utils.quote(query)}'
                f'&api_key={oa_api_key}'
            )
            resp = requests.get(oa_url, timeout=_SEARCH_TIMEOUT)
            if resp.ok:
                records = resp.json().get('records', [])
                logger.debug('Springer Nature OA API returned %d records', len(records))
        except Exception as exc:
            logger.debug('Springer Nature OA API error: %s', exc)

    results: List[Dict[str, Any]] = []
    seen_ids: set = set()
    for rec in records[:max_results]:
        doi = (rec.get('doi') or '').strip()
        item_id = doi or rec.get('identifier', '')
        if not item_id or item_id in seen_ids:
            continue
        seen_ids.add(item_id)

        title = _clean_text(rec.get('title') or '')
        raw_abstract = rec.get('abstract')
        if isinstance(raw_abstract, dict):
            parts = []
            for v in raw_abstract.values():
                if isinstance(v, str):
                    parts.append(v)
                elif isinstance(v, list):
                    parts.extend(str(x) for x in v if x)
            abstract = ' '.join(parts)
        elif isinstance(raw_abstract, str):
            abstract = _clean_text(raw_abstract)
        else:
            abstract = ''
        journal = rec.get('publicationName', '')
        url = rec.get('url', [{}])
        if isinstance(url, list):
            url = url[0].get('value', '') if url else ''
        else:
            url = url.get('value', '')

        results.append({
            'title': title,
            'summary': abstract,
            'source': 'springer_nature',
            'id': doi or item_id,
            'doi': doi,
            'url': url,
            'journal': journal,
            'raw_text': f'Title: {title}\n\nAbstract: {abstract}',
        })
    return results


def fetch_springer_nature_pdf(doi: str, skip_pdf: bool = False) -> Dict[str, str]:
    """Download a Nature journal PDF via Open Access API, proxy, or DOI scraping.

    Environment variables used (in order of priority):
      SPRINGER_NATURE_OA_API_KEY  — for Open Access API full-text retrieval
      UNIVERSITY_PROXY             — HTTP proxy URL (e.g. ``http://proxy.univ.edu:8080``)

    Args:
        doi: The DOI of the article.
        skip_pdf: If True, only fetch OA API metadata (title + abstract) without
                  attempting PDF download or DOI scraping. Used during the search
                  phase for fast candidate collection.

    Returns a dict with keys ``full_text``, ``title``, ``abstract``, ``url``.
    Falls back through: OA API → proxy PDF → DOI page scraping.
    """
    result: Dict[str, str] = {
        'full_text': '',
        'title': '',
        'abstract': '',
        'url': '',
    }

    doi_suffix = doi.split('/', 1)[-1] if '/' in doi else doi
    # Try multiple PDF URL formats (Nature-branded + BMC/Springer journals)
    _pdf_urls = [
        f'https://link.springer.com/content/pdf/{doi}.pdf',   # universal
        f'https://www.nature.com/articles/{doi_suffix}.pdf',  # Nature-branded
    ]
    result['url'] = _pdf_urls[0]
    pdf_text = ''

    # Step 1: try Springer Nature Open Access API (always, fast metadata)
    oa_api_key = os.environ.get('SPRINGER_NATURE_OA_API_KEY', '').strip()
    if oa_api_key and not pdf_text:
        try:
            oa_url = (
                'https://api.springernature.com/openaccess/json'
                f'?q=doi:{doi}'
                f'&api_key={oa_api_key}'
            )
            resp = requests.get(oa_url, timeout=min(10, _SEARCH_TIMEOUT))
            if resp.ok:
                oa_data = resp.json()
                records = oa_data.get('records', [])
                if records:
                    result['title'] = _clean_text(records[0].get('title') or '')
                    raw_abs = records[0].get('abstract')
                    if isinstance(raw_abs, dict):
                        parts = []
                        for v in raw_abs.values():
                            if isinstance(v, str):
                                parts.append(v)
                            elif isinstance(v, list):
                                parts.extend(str(x) for x in v if x)
                        result['abstract'] = ' '.join(parts)
                    elif isinstance(raw_abs, str):
                        result['abstract'] = _clean_text(raw_abs)
                    else:
                        result['abstract'] = ''
            logger.debug('Springer Nature OA API returned for %s', doi)
        except Exception as exc:
            logger.debug('Springer Nature OA API failed for %s: %s', doi, exc)

    # If skip_pdf is True, return OA API metadata only (no PDF download)
    if skip_pdf:
        if result['abstract']:
            result['full_text'] = f"Title: {result['title']}\n\nAbstract: {result['abstract']}"
        return result

    # Step 2: try PDF via university proxy (from env var or Windows Credential Manager)
    proxy_url = _get_proxy_url()
    if proxy_url and not pdf_text:
        for pdf_url in _pdf_urls:
            if pdf_text:
                break
            try:
                resp = requests.get(
                    pdf_url,
                    proxies={'http': proxy_url, 'https': proxy_url},
                    timeout=min(10, _SEARCH_TIMEOUT),
                    allow_redirects=True,
                )
                if not resp.ok:
                    continue
                import io
                import logging as _logging
                _logging.getLogger('pypdf').setLevel(_logging.ERROR)
                from pypdf import PdfReader
                reader = PdfReader(io.BytesIO(resp.content))
                pages = [p.extract_text() for p in reader.pages if p.extract_text()]
                if pages:
                    pdf_text = '\n\n'.join(pages)[:50000]
                    result['url'] = pdf_url
                    logger.debug('Springer Nature PDF downloaded via %s (%d pages, %d chars)',
                                 pdf_url, len(reader.pages), len(pdf_text))
            except Exception as exc:
                logger.debug('Nature PDF download failed for %s via %s: %s', doi, pdf_url, exc)

    # Step 3: fallback — DOI page HTML scraping
    if not pdf_text:
        try:
            from literature.core.parser import parse_doi
            html_text = parse_doi(doi)
            if html_text and not html_text.startswith('Error'):
                pdf_text = html_text[:30000]
        except Exception:
            pass

    if pdf_text:
        result['full_text'] = pdf_text
    return result


# ---------------------------------------------------------------------------
# Springer Nature full-text HTML fetcher (for extraction phase)
# ---------------------------------------------------------------------------

def fetch_springer_nature_fulltext_html(doi: str) -> str:
    """Fetch the full article HTML from a Springer Nature DOI and extract body text.

    Uses the article page (``link.springer.com/article/{doi}`` or
    ``www.nature.com/articles/{doi_suffix}``) and searches for
    the article body and data-availability sections via regex.

    Falls back through: direct HTTP → university proxy → direct HTTP without SSL verify.

    Returns cleaned article text (up to 50 000 chars) or ``''`` on failure.
    """
    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/125.0.0.0 Safari/537.36'
        ),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }

    doi_suffix = doi.split('/', 1)[-1] if '/' in doi else doi
    urls_to_try = [
        f'https://link.springer.com/article/{doi}',
        f'https://www.nature.com/articles/{doi_suffix}',
    ]

    # Determine proxy config
    proxy_url = _get_proxy_url()
    proxies = {'http': proxy_url, 'https': proxy_url} if proxy_url else None

    html = ''
    last_status = 0
    for url in urls_to_try:
        # Try direct first (fast, works for OA articles),
        # then via proxy (for restricted content),
        # then without SSL verify (last resort).
        for try_proxies, try_verify in [
            (None, True),              # direct (fastest, most common)
            (None, False),             # direct, no SSL verify
            (proxies, True),           # with proxy (slow, but needed for paywall)
        ]:
            try:
                timeout = _SEARCH_TIMEOUT if try_proxies is None else min(10, _SEARCH_TIMEOUT)
                resp = requests.get(
                    url, headers=headers, timeout=timeout,
                    proxies=try_proxies, verify=try_verify,
                )
                last_status = resp.status_code
                if resp.ok:
                    html = resp.text
                    if try_proxies:
                        logger.debug('Springer Nature HTML fetched via proxy for %s', doi)
                    elif not try_verify:
                        logger.debug('Springer Nature HTML fetched (no SSL verify) for %s', doi)
                    else:
                        logger.debug('Springer Nature HTML fetched directly for %s', doi)
                    break
            except Exception as exc:
                logger.debug('SN HTML fetch attempt failed for %s: %s', doi, exc)
                continue
        if html:
            break

    if not html:
        if last_status:
            logger.debug('Springer Nature HTML fetch failed for %s (HTTP %d)', doi, last_status)
        else:
            logger.debug('Springer Nature HTML fetch failed for %s (all attempts failed)', doi)
        return ''

    # Extract article body using a simpler approach:
    # Find the article start (c-article-body div or <article> tag)
    # then find the position of key sections or just extract from data-article-body
    # to the main </article>.
    body_start = html.find('data-article-body="true"')
    if body_start < 0:
        body_start = html.find('c-article-body')
    if body_start < 0:
        body_start = html.find('article-body')
    if body_start < 0:
        # Fall back to the main <article> tag
        for tag in ['<article lang="en" id="main"', '<article id="main"', '<article class="app-masthead']:
            body_start = html.find(tag)
            if body_start >= 0:
                break
    if body_start < 0:
        # Last resort: just find any <article> tag
        body_start = html.find('<article')
        if body_start < 0:
            return ''

    # Go back to the start of the containing <article> tag
    art_start = html.rfind('<article', 0, body_start)
    if art_start < 0:
        art_start = body_start

    # Now find the NEXT </article> that closes the main article
    # by counting nesting depth
    depth = 0
    pos = art_start
    art_end = -1
    while pos < len(html):
        n_open = html.find('<article', pos)
        n_close = html.find('</article>', pos)
        if n_close < 0:
            break
        if n_open >= 0 and n_open < n_close:
            depth += 1
            pos = n_open + 8
        else:
            depth -= 1
            pos = n_close + 10
            if depth <= 0:
                art_end = n_close + 10
                break

    if art_end < 0:
        return ''

    body_html = html[art_start:art_end]
    # Remove scripts, styles, and other non-content elements
    body_html = re.sub(r'<script[^>]*>.*?</script>', ' ', body_html, flags=re.S | re.I)
    body_html = re.sub(r'<style[^>]*>.*?</style>', ' ', body_html, flags=re.S | re.I)
    body_html = re.sub(r'<nav[^>]*>.*?</nav>', ' ', body_html, flags=re.S | re.I)
    body_html = re.sub(r'<svg[^>]*>.*?</svg>', ' ', body_html, flags=re.S | re.I)

    # --- Pre-extract critical supplementary sections BEFORE text truncation ---
    # Nature/Springer puts Data Availability, Code Availability, etc. near the
    # END of the article body.  The main text is truncated to 80000 chars which
    # can cut off these crucial sections.  We extract them by HTML section IDs
    # from body_html before stripping tags.
    _critical_section_ids = [
        'data-availability-section',
        'code-availability-section',
    ]
    supp_parts: list = []
    for section_id in _critical_section_ids:
        for quote_char in ('"', "'"):
            id_marker = f'id={quote_char}{section_id}{quote_char}'
            idx = body_html.find(id_marker)
            if idx >= 0:
                # Walk back to the start of the containing HTML tag
                tag_start = body_html.rfind('<', 0, idx)
                if tag_start >= 0:
                    idx = tag_start
                chunk = body_html[idx:idx + 6000]
                chunk = re.sub(r'<script[^>]*>.*?</script>', ' ', chunk, flags=re.S | re.I)
                chunk = re.sub(r'<style[^>]*>.*?</style>', ' ', chunk, flags=re.S | re.I)
                chunk = re.sub(r'<nav[^>]*>.*?</nav>', ' ', chunk, flags=re.S | re.I)
                chunk = re.sub(r'<svg[^>]*>.*?</svg>', ' ', chunk, flags=re.S | re.I)
                chunk = re.sub(r'<[^>]+>', ' ', chunk)
                # Clean any remaining HTML attribute fragments
                chunk = re.sub(r'\b(?:id|class|data-[a-z-]+|aria-[a-z-]+)\s*=\s*"[^"]*"', ' ', chunk)
                chunk = re.sub(r'\s+', ' ', chunk).strip()
                if len(chunk) > 30:
                    section_title = section_id.replace('-section', '').replace('-', ' ').title()
                    supp_parts.append(f"{section_title}: {chunk}")
                break

    # Strip HTML tags for the main body
    text = re.sub(r'<[^>]+>', ' ', body_html)
    text = re.sub(r'\s+', ' ', text).strip()

    # Skip past the first Abstract to avoid CSS/layout noise
    abstract_idx = text.lower().find('abstract')
    if abstract_idx > 100:
        text = text[abstract_idx:]

    # Truncate main body to limit token usage
    if len(text) > 500:
        text = text[:80000]

    # Append pre-extracted sections so critical data/code info is never lost
    if supp_parts:
        supp_text = '\n\n'.join(supp_parts)
        text += f"\n\nSUPPLEMENTARY_SECTIONS:\n{supp_text}"
        logger.debug('Appended %d supp section(s) for %s (%d chars)',
                     len(supp_parts), doi, len(supp_text))

    if len(text) > 500:
        logger.debug('Springer Nature HTML body extracted for %s (%d chars)', doi, len(text))
        # Allow extra space for supplementary sections (up to 100 000 chars total)
        return text[:100000]

    # Last resort: clean whole page
    html_clean = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.S | re.I)
    html_clean = re.sub(r'<style[^>]*>.*?</style>', ' ', html_clean, flags=re.S | re.I)
    text = re.sub(r'<[^>]+>', ' ', html_clean)
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) > 1000:
        logger.debug('Springer Nature full-page fallback for %s (%d chars)', doi, len(text))
        return text[:30000]

    return ''


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
