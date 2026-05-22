#!/usr/bin/env python3
"""LLM-powered literature collector.

Replaces the hardcoded PubMed query / regex extraction logic with
LLM-driven search query generation, relevance scoring, and dataset
extraction. Falls back gracefully when the LLM is unavailable.

Audit trail
-----------
When ``enable_audit=True`` is passed to ``llm_collect_literature``, the
function returns a second dict ``audit_log`` containing every LLM call's
prompt, response, parsed result, timestamp, and any errors — serialisable
as ``llm_audit.json`` for later review.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time as _time_mod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# Timeout for external HTTP requests (connect + read).
_SEARCH_TIMEOUT = 15  # seconds

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / 'skills'))


# ---------------------------------------------------------------------------
# Audit trail (accumulates every LLM interaction for post-hoc review)
# ---------------------------------------------------------------------------

_AUDIT: List[Dict[str, Any]] = []
_AUDIT_ENABLED = False


def _enable_audit() -> None:
    global _AUDIT_ENABLED, _AUDIT
    _AUDIT_ENABLED = True
    _AUDIT.clear()


def _disable_audit() -> None:
    global _AUDIT_ENABLED
    _AUDIT_ENABLED = False


def _get_audit() -> List[Dict[str, Any]]:
    return list(_AUDIT)


def _audit_call(call_type: str, prompt: str, system_prompt: str,
                response: Optional[str], parsed: Any, error: Optional[str],
                duration_ms: float) -> None:
    if not _AUDIT_ENABLED:
        return
    _AUDIT.append({
        'call_type': call_type,
        'timestamp': _time_mod.time(),
        'duration_ms': round(duration_ms, 1),
        'prompt': prompt[:2000],
        'system_prompt': system_prompt[:500],
        'response': (response or '')[:2000],
        'parsed': repr(parsed)[:2000] if parsed is not None else None,
        'error': error,
    })


def _audit_record_parsed(parsed: Any) -> None:
    """Update the *last* audit entry with the parsed result of the LLM call."""
    if not _AUDIT_ENABLED or not _AUDIT:
        return
    _AUDIT[-1]['parsed'] = repr(parsed)[:2000]


def _call_llm(directive: str, system_prompt: str, temperature: float = 0.3,
              call_type: str = 'unknown') -> Optional[str]:
    """Try to call the OmicsClaw LLM. Returns None if unavailable."""
    t0 = _time_mod.time()
    try:
        from omicsclaw.autoagent.llm_client import call_llm

        result = call_llm(
            directive,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=2048,
        )
        if not result or not str(result).strip():
            _audit_call(call_type, directive, system_prompt, result, None,
                        'empty response', (_time_mod.time() - t0) * 1000)
            logger.warning('LLM call returned empty response for %s.', call_type)
            return None
        _audit_call(call_type, directive, system_prompt, result, None, None,
                    (_time_mod.time() - t0) * 1000)
        return result
    except Exception as exc:
        _audit_call(call_type, directive, system_prompt, None, None,
                    str(exc), (_time_mod.time() - t0) * 1000)
        logger.warning('LLM call failed, falling back to hardcoded logic: %s', exc)
        return None


def _extract_json_fragment(raw: str) -> Optional[str]:
    raw = raw.strip()
    if raw.startswith('```'):
        raw = raw.split('\n', 1)[1]
    if raw.endswith('```'):
        raw = raw.rsplit('```', 1)[0]

    # Find the first JSON object or array in the response.
    start = min((pos for pos in (raw.find('{'), raw.find('[')) if pos != -1), default=-1)
    if start == -1:
        return raw

    open_char = raw[start]
    close_char = '}' if open_char == '{' else ']'
    depth = 0
    for idx in range(start, len(raw)):
        ch = raw[idx]
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return raw[start:idx + 1]
    return raw[start:]


def _repair_truncated_json(fragment: str) -> str:
    repaired_chars: List[str] = []
    stack: List[str] = []
    in_string = False
    escape = False

    for ch in fragment:
        if escape:
            repaired_chars.append(ch)
            escape = False
            continue

        if ch == '\\':
            repaired_chars.append(ch)
            escape = True
            continue

        if ch == '"':
            repaired_chars.append(ch)
            in_string = not in_string
            continue

        if in_string:
            if ch == '\n' or ch == '\r':
                repaired_chars.append(' ')
            else:
                repaired_chars.append(ch)
            continue

        if ch == '{':
            stack.append('}')
        elif ch == '[':
            stack.append(']')
        elif ch == '}' or ch == ']':
            if stack and ch == stack[-1]:
                stack.pop()
        repaired_chars.append(ch)

    if in_string:
        repaired_chars.append('"')
    while stack:
        repaired_chars.append(stack.pop())

    return ''.join(repaired_chars)


def _parse_llm_json(raw: str) -> Optional[Any]:
    fragment = _extract_json_fragment(raw)
    if fragment is None:
        return None

    try:
        return json.loads(fragment)
    except (json.JSONDecodeError, TypeError) as exc:
        repaired = _repair_truncated_json(fragment)
        try:
            return json.loads(repaired)
        except (json.JSONDecodeError, TypeError) as exc2:
            logger.warning('LLM returned invalid JSON: %s | raw=%s', exc2, raw[:500])
            return None


def _analysis_context(analysis_type: str) -> Tuple[str, str]:
    """Return (focus_terms, data_hints) for a given analysis type.

    Used by LLM prompts to describe the kind of single-cell datasets to look
    for, without suggesting evaluation/benchmark framing.
    """
    type_map = {
        'integration': (
            'multi-sample, multi-batch, cross-dataset, batch correction, data harmonization',
            'two or more samples/conditions, GEO series with multiple GSM entries',
        ),
        'matching': (
            'cross-modality, paired measurements, cell correspondence, multi-omics',
            'CITE-seq, Multiome ATAC+RNA, spatial+scRNA pairs',
        ),
        'clustering': (
            'cell-type discovery, unsupervised grouping, population structure, subpopulation',
            'diverse cell types, tissue atlas, cell lineage',
        ),
        'annotation': (
            'cell-type label, reference map, marker gene, classification, cell identity',
            'annotated cell types, well-characterized tissue, marker gene lists',
        ),
        'deconvolution': (
            'cell-type proportion, mixture decomposition, bulk-to-single-cell, composition',
            'spatial transcriptomics + scRNA reference, tumor microenvironment',
        ),
        'trajectory': (
            'pseudotime, developmental trajectory, differentiation, lineage progression',
            'time-series, developing tissue, differentiation protocol',
        ),
        'spatial': (
            'spatial transcriptomics, tissue context, spatial coordinates, Visium, MERFISH',
            'Visium, Slide-seq, MERFISH, Xenium, spatial gene expression',
        ),
        'multiome': (
            'multi-omics, CITE-seq, ATAC+RNA, multi-modal, joint profiling',
            'paired RNA+protein, ATAC+RNA, multimodal single-cell data',
        ),
    }

    return type_map.get(
        analysis_type,
        (f'single-cell omics {analysis_type}', f'{analysis_type} related datasets'),
    )


def llm_generate_queries(benchmark_type: str, user_query: Optional[str] = None) -> Optional[List[str]]:
    """Use LLM to generate search queries for dataset discovery.

    The goal is to find papers that contain *public single-cell omics datasets*
    (GEO, SRA, cellxgene, etc.) relevant to the target analysis type.  The
    benchmark itself is built *from* those datasets — we are searching for
    the raw data, not for existing benchmark papers.
    """
    focus, _ = _analysis_context(benchmark_type)
    prompt = (
        f"You are an expert in discovering single-cell omics datasets for reuse. "
        f"Generate 5 concise search queries for PubMed, arXiv, Google Scholar, GitHub, and Zenodo. "
        f"The goal is to find papers that contain or reference PUBLIC single-cell or spatial omics datasets "
        f"and associated code repositories, suitable for downstream reuse in {benchmark_type} data analysis.\n\n"
        f"Requirements:\n"
        f"- Return ONLY a JSON array of strings, no explanation.\n"
        f"- Queries must be under 200 chars each.\n"
        f"- Focus on papers reporting author-collected, first-hand single-cell or spatial omics data, and that also provide code for reuse.\n"
        f"- Target terminology: {focus}.\n"
        f"- Include terms like \"primary data\", \"author-collected\", \"original dataset\", \"de novo data\", \"GEO\", \"GSE\", \"SRA\", \"cellxgene\", "
        f"\"Zenodo\", \"GitHub\", \"public data\", \"single-cell\", \"scRNA-seq\", \"h5ad\", \"code\".\n"
        f"- Do NOT use the word \"benchmark\" unless the user explicitly asked for it.\n"
    )
    if user_query:
        prompt += f"\nUser interest: {user_query}\n"

    raw = _call_llm(prompt, system_prompt="You output only valid JSON arrays.", call_type='generate_queries')
    if not raw:
        return None

    queries = _parse_llm_json(raw)
    # Update last audit entry with parsed result
    _audit_record_parsed(queries)
    if isinstance(queries, list) and all(isinstance(q, str) for q in queries):
        return queries[:5]
    return None


def llm_extract_paper_details(text: str, benchmark_type: str) -> Optional[Dict[str, Any]]:
    """Use LLM to extract structured dataset and method details from paper text."""
    focus, _ = _analysis_context(benchmark_type)
    prompt = (
        f"You are a scientific literature curator specialized in single-cell omics data discovery. "
        f"Analyze the following paper text to extract dataset accessions, code repositories, and "
        f"biological metadata relevant for downstream reuse in {benchmark_type} data analysis.\n\n"
        f"Paper text:\n{text[:16000]}\n\n"
        f"Return a JSON object with these exact keys:\n"
        f"  {{\n"
        f"    \"gse_ids\": [...],\n"
        f"    \"sra_ids\": [...],\n"
        f"    \"cellxgene_ids\": [...],\n"
        f"    \"arxiv_ids\": [...],\n"
        f"    \"github_repos\": [...],\n"
        f"    \"zenodo_records\": [...],\n"
        f"    \"organism\": \"...\",\n"
        f"    \"tissue\": \"...\",\n"
        f"    \"technology\": \"...\",\n"
        f"    \"benchmark_relevance_score\": 0-10,\n"
        f"    \"data_origin\": \"author_collected\"|\"public_reanalysis\"|\"unclear\",\n"
        f"    \"first_hand_data\": false,\n"
        f"    \"reason\": \"...\",\n"
        f"    \"methods_summary\": \"...\",\n"
        f"    \"code_snippets\": \"...\"\n"
        f"  }}\n\n"
        f"Use the terminology: {focus}.\n"
        f"Score the paper by how likely it is to provide both reusable single-cell data and code/repositories, with preference for primary datasets collected by the authors themselves.\n"
        f"If the text contains no datasets, return empty arrays. Return ONLY valid JSON."
    )

    raw = _call_llm(prompt, system_prompt="You are a skilled scientific literature curator.",
                    temperature=0.2, call_type='extract_paper_details')
    if not raw:
        return None

    result = _parse_llm_json(raw)
    _audit_record_parsed(result)
    if not isinstance(result, dict):
        return None
    return result


def llm_rank_articles(candidates: List[Dict[str, Any]], benchmark_type: str) -> List[Dict[str, Any]]:
    """Rank candidate literature items by data and code availability, relevance, and confidence."""
    if not candidates:
        return []

    prompt_items = []
    for idx, candidate in enumerate(candidates, start=1):
        summary = candidate.get('summary') or candidate.get('abstract') or candidate.get('description') or ''
        prompt_items.append(
            f"{idx}. Title: {candidate.get('title', 'N/A')}\n"
            f"Source: {candidate.get('source', 'unknown')}\n"
            f"Summary: {summary[:600]}\n"
        )

    joined_items = '\n'.join(prompt_items)
    prompt = (
        f"You are ranking single-cell omics literature candidates for downstream {benchmark_type} analysis. "
        f"Review each item and return a JSON array of objects with keys: \"rank\", \"index\", \"confidence\" (0-100), \"reason\".\n"
        f"Prioritize papers that are MOST LIKELY to contain or reference public single-cell datasets "
        f"(GEO, SRA, cellxgene, or Zenodo accessions) AND publicly available code repositories "
        f"(GitHub, Zenodo, or other code archives).\n"
        f"Prefer papers with author-collected, first-hand datasets rather than only secondary reanalysis or review datasets.\n"
        f"The paper itself does not need to be a benchmark publication; rank it based on reusability of data and code.\n\n"
        f"Candidates:\n{joined_items}\n"
        f"Return ONLY valid JSON."
    )
    raw = _call_llm(prompt, system_prompt="You output only valid JSON arrays.",
                    temperature=0.2, call_type='rank_articles')
    if not raw:
        logger.warning('LLM rank_articles did not return content; using original candidate order.')
        return candidates

    ranked = _parse_llm_json(raw)
    if not isinstance(ranked, list):
        logger.warning('LLM rank_articles returned invalid JSON or unexpected structure; using original candidate order.')
        return candidates

    _audit_record_parsed(ranked)
    indexed = {item.get('index'): item for item in ranked if isinstance(item, dict) and 'index' in item}
    sorted_candidates = []
    for idx in range(1, len(candidates) + 1):
        if idx in indexed:
            candidate = candidates[idx - 1].copy()
            candidate['rank'] = indexed[idx].get('rank')
            candidate['confidence'] = indexed[idx].get('confidence', 0)
            candidate['rank_reason'] = indexed[idx].get('reason', '')
            sorted_candidates.append(candidate)
    sorted_candidates.sort(key=lambda x: (x.get('rank', 999), -int(x.get('confidence', 0))))
    return sorted_candidates


def _validate_accessions(ids: Any, pattern: str) -> List[str]:
    if not ids:
        return []
    if isinstance(ids, str):
        ids = re.split(r'[;,\s]+', ids.strip())
    valid = []
    for item in ids:
        if not item:
            continue
        candidate = item.strip().upper()
        if re.match(pattern, candidate):
            valid.append(candidate)
    return list(dict.fromkeys(valid))


def validate_accessions(metadata: Dict[str, Any]) -> Dict[str, Any]:
    if 'geo_accessions' in metadata:
        metadata['geo_accessions'] = {
            'gse': _validate_accessions(metadata['geo_accessions'].get('gse', []), r'^GSE\d{3,}$'),
            'gsm': _validate_accessions(metadata['geo_accessions'].get('gsm', []), r'^GSM\d{3,}$'),
            'gpl': _validate_accessions(metadata['geo_accessions'].get('gpl', []), r'^GPL\d{3,}$'),
        }
    if 'sra_accessions' in metadata:
        metadata['sra_accessions'] = _validate_accessions(metadata.get('sra_accessions', []), r'^(?:SRP|SRR|SRS|ERP|ERS|DRP|DRS)\d{3,}$')
    if 'cellxgene_ids' in metadata:
        metadata['cellxgene_ids'] = _validate_accessions(metadata.get('cellxgene_ids', []), r'^[A-Za-z0-9_\-]{6,}$')
    metadata['accession_validation'] = {
        'geo_count': len(metadata.get('geo_accessions', {}).get('gse', [])),
        'sra_count': len(metadata.get('sra_accessions', [])),
        'cellxgene_count': len(metadata.get('cellxgene_ids', [])),
    }
    return metadata


def search_arxiv(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    try:
        search_url = (
            'http://export.arxiv.org/api/query?search_query='
            f'all:{requests.utils.quote(query)}&start=0&max_results={max_results}'
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


def search_zenodo(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
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


def search_github(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    token = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
    headers = {'Accept': 'application/vnd.github.v3+json'}
    if token:
        headers['Authorization'] = f'token {token}'
    try:
        api_url = f'https://api.github.com/search/repositories?q={requests.utils.quote(query)}+language:python&sort=stars&order=desc&per_page={max_results}'
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


def search_google_scholar(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
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


def _timed_search(search_fn, deadline: float, *args, label: str = '', **kwargs) -> list:
    """Call *search_fn* but skip if the deadline has already passed.

    Each individual ``requests.get(…, timeout=…)`` already enforces
    ``_SEARCH_TIMEOUT`` seconds per call.  This wrapper simply skips
    calls when the total multi-source budget is exhausted.
    """
    import time as _time
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


def llm_collect_literature(
    benchmark_type: str,
    user_query: Optional[str] = None,
    max_results: int = 5,
    enable_audit: bool = True,
) -> Dict[str, Any]:
    """Search literature and dataset sources using LLM-guided queries.

    If *enable_audit* is True, returns ``{'results': [...], 'audit': [...]}``.
    Otherwise returns ``{'results': [...]}`` for backward compatibility.
    """
    if enable_audit:
        _enable_audit()

    try:
        results = _llm_collect_impl(benchmark_type, user_query, max_results)
    finally:
        audit = _get_audit() if enable_audit else []
        if enable_audit:
            _disable_audit()

    if enable_audit:
        return {'results': results, 'audit': audit}
    return {'results': results}


def _llm_collect_impl(
    benchmark_type: str,
    user_query: Optional[str] = None,
    max_results: int = 5,
) -> List[Dict[str, Any]]:
    """Actual implementation of LLM-powered literature collection."""
    from literature.core.search import search_pubmed, fetch_pubmed_article
    from literature.core.extractor import extract_metadata
    from literature.core.parser import parse_doi

    queries = llm_generate_queries(benchmark_type, user_query)
    if not queries:
        logger.info('LLM query generation unavailable, using hardcoded fallback queries.')
        queries = [
            f"{benchmark_type} single-cell RNA-seq GEO",
            f"{benchmark_type} benchmark dataset",
            f"single-cell {benchmark_type}",
        ]

    candidate_items: List[Dict[str, Any]] = []
    seen_keys = set()

    # PubMed candidates
    for query in queries[:3]:
        for pmid in search_pubmed(query, max_results=max_results):
            if pmid in seen_keys:
                continue
            seen_keys.add(pmid)
            article = fetch_pubmed_article(pmid)
            if not article or not article.get('title'):
                continue
            raw_text = f"Title: {article.get('title', '')}\n\nAbstract: {article.get('abstract', '')}"
            doi = article.get('doi', '')
            if doi:
                try:
                    doi_text = parse_doi(doi)
                    if doi_text and not doi_text.startswith('Error fetching'):
                        raw_text = raw_text + '\n\nDOI_PAGE_CONTENT:\n' + doi_text[:14000]
                except Exception:
                    logger.debug('Failed to fetch DOI page content for %s', doi, exc_info=True)

            candidate_items.append({
                'title': article.get('title', ''),
                'summary': article.get('abstract', ''),
                'source': 'pubmed',
                'id': pmid,
                'url': f'https://pubmed.ncbi.nlm.nih.gov/{pmid}/',
                'doi': doi,
                'raw_text': raw_text,
            })

    # ----- External sources (arxiv / zenodo / github / scholar) ----------
    #
    # Each source call has a per-request timeout of _SEARCH_TIMEOUT (15 s).
    # The pipeline-wide budget below gives each query-pair generous headroom,
    # scaled by the number of queries * sources * per-call timeout + 50 % buffer.
    # This way a single slow source does not starve the entire loop.
    import time as _time
    _active_queries = len(queries[:2])
    _active_sources = 4
    _search_deadline = _time.time() + _SEARCH_TIMEOUT * _active_sources * _active_queries * 1.5
    for query in queries[:2]:
        if _time.time() > _search_deadline:
            logger.warning(
                'External search deadline (%.0f s) exceeded after %.0f s — '
                'skipping %d remaining query(s). Consider reducing --use-llm '
                'or running with a faster network.',
                _SEARCH_TIMEOUT * _active_sources * _active_queries * 1.5,
                _time.time() - (_search_deadline - _SEARCH_TIMEOUT * _active_sources * _active_queries * 1.5),
                len(queries[:2]) - queries[:2].index(query),
            )
            break
        candidate_items.extend(
            _timed_search(search_arxiv, _search_deadline, query, max_results=max_results,
                          label='arxiv')
        )
        candidate_items.extend(
            _timed_search(search_zenodo, _search_deadline, query, max_results=max_results,
                          label='zenodo')
        )
        candidate_items.extend(
            _timed_search(search_github, _search_deadline, query, max_results=max_results,
                          label='github')
        )
        candidate_items.extend(
            _timed_search(search_google_scholar, _search_deadline, query, max_results=max_results,
                          label='google_scholar')
        )

    if not candidate_items:
        return []

    # Rank candidates if LLM available
    ranked_items = llm_rank_articles(candidate_items, benchmark_type)
    if ranked_items:
        candidate_items = ranked_items

    results: List[Dict[str, Any]] = []
    for candidate in candidate_items[:max_results]:
        text = candidate.get('raw_text') or candidate.get('summary') or ''
        if candidate.get('source') == 'pubmed':
            text = candidate.get('raw_text', text)

        if not text:
            continue

        llm_data = llm_extract_paper_details(text, benchmark_type)
        if llm_data:
            gse = llm_data.get('gse_ids', [])
            sra = llm_data.get('sra_ids', [])
            cellxgene = llm_data.get('cellxgene_ids', [])
            organism = llm_data.get('organism', 'unknown')
            tissue = llm_data.get('tissue', 'unknown')
            technology = llm_data.get('technology', 'unknown')
            relevance = llm_data.get('data_relevance_score')
            if relevance is None:
                relevance = llm_data.get('benchmark_relevance_score', 0)
            else:
                llm_data['benchmark_relevance_score'] = relevance
            llm_data = validate_accessions(llm_data)
        else:
            metadata = extract_metadata(text, benchmark_type)
            gse = metadata.get('geo_accessions', {}).get('gse', [])
            sra = metadata.get('sra_accessions', [])
            cellxgene = metadata.get('cellxgene_accessions', [])
            organism = metadata.get('organism', 'unknown')
            tissue = metadata.get('tissue', 'unknown')
            technology = metadata.get('technology', 'unknown')
            relevance = metadata.get('relevance_score', 0)
            llm_data = metadata

        github_repos = llm_data.get('github_repos', [])
        zenodo_records = llm_data.get('zenodo_records', [])
        has_data = bool(gse or sra or cellxgene)
        has_code = bool(github_repos or zenodo_records)
        if not has_data or not has_code:
            logger.info(
                'Skipping candidate %s: requires both dataset accessions and code/archive sources (data=%s, code=%s).',
                candidate.get('id'), has_data, has_code,
            )
            continue

        result = {
            'input': candidate.get('title', ''),
            'id': candidate.get('id', ''),
            'source': candidate.get('source', 'unknown'),
            'url': candidate.get('url', ''),
            'raw_text': text,
            'metadata': {
                'organism': organism,
                'tissue': tissue,
                'technology': technology,
                'relevance_score': relevance,
                'geo_accessions': {'gse': gse, 'gsm': llm_data.get('geo_accessions', {}).get('gsm', []), 'gpl': llm_data.get('geo_accessions', {}).get('gpl', [])},
                'sra_accessions': sra,
                'cellxgene_accessions': cellxgene,
                'arxiv_ids': llm_data.get('arxiv_ids', []),
                'github_repos': llm_data.get('github_repos', []),
                'zenodo_records': llm_data.get('zenodo_records', []),
                'methods_summary': llm_data.get('methods_summary', ''),
                'code_snippets': llm_data.get('code_snippets', ''),
                'first_hand_data': llm_data.get('first_hand_data', False),
                'data_origin': llm_data.get('data_origin', 'unclear'),
                'benchmark_relevance_score': llm_data.get('benchmark_relevance_score', relevance),
            },
            'relevance_score': relevance,
        }
        if 'rank' in candidate:
            result['rank'] = candidate['rank']
            result['confidence'] = candidate.get('confidence', 0)
            result['rank_reason'] = candidate.get('rank_reason', '')

        results.append(result)

    return results
