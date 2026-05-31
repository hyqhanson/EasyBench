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
        'parsed': repr(parsed)[:4000] if parsed is not None else None,
        'error': error,
    })


def _audit_record_parsed(parsed: Any) -> None:
    """Update the *last* audit entry with the parsed result of the LLM call."""
    if not _AUDIT_ENABLED or not _AUDIT:
        return
    _AUDIT[-1]['parsed'] = repr(parsed)[:4000]


def _call_llm(directive: str, system_prompt: str, temperature: float = 0.3,
              call_type: str = 'unknown', max_tokens: int = 4096) -> Optional[str]:
    """Try to call the OmicsClaw LLM. Returns None if unavailable."""
    t0 = _time_mod.time()
    try:
        from omicsclaw.autoagent.llm_client import call_llm

        result = call_llm(
            directive,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
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


def _analysis_context(analysis_type: str) -> Tuple[str, str, str]:
    """Return (focus_terms, data_hints, data_requirements) for a given analysis type.

    - focus_terms:       Keywords for search queries.
    - data_hints:        What to look for in paper text.
    - data_requirements: What data features are needed for this benchmark (for LLM scoring).
    """
    # Each entry: (focus_terms, data_hints, data_requirements)
    type_map = {
        'integration': (
            'multi-sample, multi-batch, cross-dataset, batch correction, data harmonization',
            'two or more samples/conditions, GEO series with multiple GSM entries',
            'Requires ≥2 distinct batches/samples with overlapping cell types. '
            'Look for papers that collected or integrated multiple scRNA-seq samples, '
            'mention batch effects, or used harmony/Seurat/scanorama for integration.',
        ),
        'batch_correction': (
            'batch correction, batch effect removal, data harmonization, multi-sample integration',
            'multiple batches, batch effect mentioned, samples from different platforms/donors',
            'Requires ≥2 batches with shared cell types. Best if authors explicitly mention '
            '"batch effect" or compare samples from different sequencing runs/conditions.',
        ),
        'matching': (
            'cross-modality, paired measurements, cell correspondence, multi-omics',
            'CITE-seq, Multiome ATAC+RNA, spatial+scRNA pairs',
            'Requires paired multi-modal data (e.g. RNA+protein via CITE-seq, or RNA+ATAC via Multiome). '
            'Look for "matched", "paired", "multi-modal" or "joint profiling" keywords.',
        ),
        'clustering': (
            'cell-type discovery, unsupervised grouping, population structure, subpopulation',
            'diverse cell types, tissue atlas, cell lineage, annotated cell populations',
            'Requires data with ≥2 distinct cell types and ideally ground-truth labels. '
            'Look for tissue atlases, cell-type marker genes, or papers that identified '
            'novel subpopulations through clustering.',
        ),
        'annotation': (
            'cell-type label, reference map, marker gene, classification, cell identity',
            'annotated cell types, well-characterized tissue, marker gene lists, reference atlas',
            'Requires a reference dataset with known cell-type labels and a query dataset. '
            'Look for "cell-type annotation", "reference mapping", "label transfer", or '
            '"classification" of cell types using single-cell data.',
        ),
        'deconvolution': (
            'cell-type proportion, mixture decomposition, bulk-to-single-cell, composition',
            'spatial transcriptomics + scRNA reference, tumor microenvironment',
            'Requires paired scRNA-seq reference (with cell-type labels) and spatial or bulk '
            'data from the same tissue. Look for "deconvolution", "cell-type proportion", '
            '"CIBERSORT", or spatial transcriptomics data from the same tissue.',
        ),
        'trajectory': (
            'pseudotime, developmental trajectory, differentiation, lineage progression',
            'time-series, developing tissue, differentiation protocol',
            'Requires continuous developmental/differentiation process with multiple stages. '
            'Look for "pseudotime", "trajectory", "differentiation", "developmental", '
            '"Monocle", "slingshot", or time-series experiments.',
        ),
        'spatial': (
            'spatial transcriptomics, tissue context, spatial coordinates, Visium, MERFISH',
            'Visium, Slide-seq, MERFISH, Xenium, spatial gene expression',
            'Requires spatial transcriptomics data (Visium, MERFISH, Slide-seq, Xenium, etc.). '
            'Look for spatial coordinates, tissue sections, or spatial gene expression '
            'measurements at subcellular or spot resolution.',
        ),
        'multiome': (
            'multi-omics, CITE-seq, ATAC+RNA, multi-modal, joint profiling',
            'paired RNA+protein, ATAC+RNA, multimodal single-cell data',
            'Requires ≥2 modalities measured in the same cells (e.g. RNA+protein, RNA+ATAC, '
            'RNA+methylation). Look for "Multiome", "CITE-seq", "paired", "joint profiling", '
            'or technologies measuring multiple molecular layers simultaneously.',
        ),
        'imputation': (
            'dropout imputation, missing value recovery, gene expression reconstruction, denoising',
            '10x Genomics data with high dropout rate, Smart-seq2 as ground truth',
            'Requires scRNA-seq data with high dropout rate (~80-90%). Best if same-cell '
            'full-length data (e.g. Smart-seq2) is available as ground truth. Look for '
            '"imputation", "dropout", "MAGIC", "scImpute", "denoising" or "gene expression recovery".',
        ),
        'differential_expression': (
            'differential expression, DE analysis, condition comparison, treatment vs control',
            '≥2 conditions, multiple biological replicates per condition',
            'Requires ≥2 experimental conditions with ≥2 biological replicates each. '
            'Look for "differential expression", "DE genes", "case vs control", '
            '"treated vs untreated", or studies comparing disease vs healthy.',
        ),
        'rna_velocity': (
            'RNA velocity, spliced/unspliced, cell fate, transcriptional dynamics',
            'spliced/unspliced count matrices, loom format, developmental process',
            'Requires spliced and unspliced count matrices (.loom or .h5ad with velocity layers). '
            'Look for "RNA velocity", "velocyto", "scVelo", "spliced/unspliced", '
            'or "transcriptional dynamics" in dynamic processes.',
        ),
        'doublet_detection': (
            'doublet detection, multiplet identification, cell barcode collision',
            'mixed cell populations with known proportions, species mixing experiments',
            'Requires scRNA-seq data where doublets can be validated (e.g. species-mixing '
            'experiments, or known cell-type mixtures). Look for "doublet", "multiplet", '
            '"species mixing", "demultiplexing" or cell-hashing experiments.',
        ),
        'normalization': (
            'normalization, scaling, sequencing depth correction, library size adjustment',
            'raw count data with varying sequencing depths, spike-in controls',
            'Requires raw UMI count data with variable library sizes across cells. '
            'Look for "normalization", "scaling", "library size", "UMI counts", '
            'or studies comparing normalization methods.',
        ),
    }

    default = (
        f'single-cell omics {analysis_type}',
        f'{analysis_type} related datasets',
        f'Requires single-cell omics data relevant to {analysis_type} analysis.',
    )
    return type_map.get(analysis_type, default)


def llm_generate_queries(benchmark_type: str, user_query: Optional[str] = None) -> Optional[List[str]]:
    """Use LLM to generate search queries for dataset discovery.

    The goal is to find papers that contain *public single-cell omics datasets*
    (GEO, SRA, cellxgene, etc.) relevant to the target analysis type.  The
    benchmark itself is built *from* those datasets — we are searching for
    the raw data, not for existing benchmark papers.
    """
    focus, _, data_reqs = _analysis_context(benchmark_type)
    prompt = (
        f"You are an expert in discovering single-cell omics datasets for reuse. "
        f"Generate **12** concise search queries — **2 per source** — targeting "
        f"PubMed, arXiv, Google Scholar, Semantic Scholar, bioRxiv/medRxiv, and Europe PMC. "
        f"Each query must be tailored to the source's strengths:\n\n"
        f"- **PubMed / Europe PMC**: query by data accession patterns (GSE, SRP, GSE+topic) and "
        f"author-collected single-cell datasets. Include MeSH-like terms.\n"
        f"- **arXiv / bioRxiv**: query preprints with terms like \"single-cell\", \"dataset\", and "
        f"\"code\" that often contain data links in the manuscript.\n"
        f"- **Google Scholar**: query for academic papers that cite dataset accessions and "
        f"mention code availability in the snippet.\n"
        f"- **Semantic Scholar**: query for papers with high citation impact that "
        f"reference public single-cell datasets and provide code.\n\n"
        f"NOTE: GitHub, Zenodo, and Figshare are NOT search targets — they are "
        f"data/code repositories, not literature databases. Do NOT generate queries "
        f"for them.\n\n"
        f"The overall goal: find papers that contain or reference PUBLIC single-cell or spatial omics datasets "
        f"and associated code repositories, suitable for downstream reuse in {benchmark_type} data analysis.\n\n"
        f"DATA REQUIREMENTS for {benchmark_type} analysis:\n"
        f"{data_reqs}\n\n"
        f"Requirements:\n"
        f"- Return ONLY a JSON array of strings, no explanation.\n"
        f"- Each query must be under 200 chars.\n"
        f"- Focus on papers reporting author-collected, first-hand single-cell or spatial omics data, and that also provide code for reuse.\n"
        f"- Target terminology: {focus}.\n"
        f"- Include terms like \"primary data\", \"author-collected\", \"original dataset\", \"de novo data\", \"GEO\", \"GSE\", \"SRA\", \"cellxgene\", "
        f"\"Zenodo\", \"GitHub\", \"public data\", \"single-cell\", \"scRNA-seq\", \"h5ad\", \"code\".\n"
        f"- Do NOT use the word \"benchmark\" unless the user explicitly asked for it.\n"
        f"- AVOID terms that attract review papers (like \"survey\", \"review\", \"overview\", \"systematic\").\n"
        f"  Only generate queries that would find ORIGINAL RESEARCH papers with datasets and code.\n"
        f"- CRITICAL: Do NOT include source names (like \"PubMed\", \"arXiv\", \"bioRxiv\", \"Google Scholar\", \"Europe PMC\")\n"
        f"  inside the query text itself. All 12 queries are reused across all search sources, so a query\n"
        f"  containing \"arXiv\" would never match any bioRxiv paper. Keep queries generic and topic-focused.\n"
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
        # Safety filter: remove queries containing source names (they'd be reused across all sources)
        source_names = ['pubmed', 'arxiv', 'biorxiv', 'medrxiv', 'google scholar',
                        'semantic scholar', 'europe pmc', 'site:scholar.google.com']
        filtered = []
        for q in queries[:12]:
            q_lower = q.lower()
            if any(s in q_lower for s in source_names):
                logger.debug('Filtered out query containing source name: %s', q[:80])
                continue
            filtered.append(q)
        return filtered if filtered else queries[:12]
    return None


def llm_extract_paper_details(text: str, benchmark_type: str) -> Optional[Dict[str, Any]]:
    """Use LLM to extract structured dataset and method details from paper text."""
    focus, _, data_reqs = _analysis_context(benchmark_type)
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
        f"    \"doi\": \"...\",\n"
        f"    \"pmid\": \"...\",\n"
        f"    \"arxiv_ids\": [...],\n"
        f"    \"github_repos\": [...],\n"
        f"    \"figshare_links\": [...],\n"
        f"    \"zenodo_data\": [...],\n"
        f"    \"zenodo_code\": [...],\n"
        f"    \"data_format\": [\"h5ad\", \"mtx\", \"rds\", \"loom\", \"seurat\", \"other\"],\n"
        f"    \"num_samples\": \"...\",\n"
        f"    \"num_cells\": \"...\",\n"
        f"    \"organism\": \"...\",\n"
        f"    \"tissue\": \"...\",\n"
        f"    \"technology\": \"...\",\n"
        f"    \"data_quality_signals\": [\"raw_counts\", \"processed_counts\", \"both\", \"unknown\"],\n"
        f"    \"data_origin\": \"author_collected\"|\"public_reanalysis\"|\"unclear\",\n"
        f"    \"first_hand_data\": false,\n"
        f"    \"benchmark_relevance_score\": 0-10,\n"
        f"    \"reason\": \"...\",\n"
        f"    \"methods_summary\": \"...\",\n"
        f"    \"code_snippets\": \"...\"\n"
        f"  }}\n\n"
        f"Use the terminology: {focus}.\n"
        f"Scoring guidance:\n"
        f"- Prefer papers that clearly state 'we generated', 'we collected', 'our dataset' → first_hand_data=true, data_origin='author_collected'.\n"
        f"- Prefer papers that mention both (a) public dataset accessions (GSE, SRP, cellxgene, Zenodo datasets) AND (b) a code repository (GitHub, GitLab, Zenodo software archives, Figshare).\n"
        f"- **Critical: distinguish Zenodo records by content type**:\n"
        f"    * Put dataset DOIs/URLs in **zenodo_data** (raw .h5ad/.mtx/.rds files, count matrices, supplements)\n"
        f"    * Put software/notebook DOIs/URLs in **zenodo_code** (code archives, Python packages, analysis scripts)\n"
        f"    * If unsure, include in BOTH lists.\n"
        f"- Penalize (score ≤ 3) papers that only reanalyze existing public data without providing new data or reusable code.\n"
        f"- If the text contains no datasets, return empty arrays.\n"
        f"\n--- Data requirements for {benchmark_type} benchmark ---\n"
        f"The paper's data is most suitable if it matches these requirements:\n"
        f"{data_reqs}\n"
        f"A higher benchmark_relevance_score should reflect how well the data meets these requirements.\n"
        f"Return ONLY valid JSON."
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
        source = candidate.get('source', 'unknown')
        preview = f"{idx}. Title: {candidate.get('title', 'N/A')}\nSource: {source}\nSummary: {summary[:200]}\n"
        if source == 'pubmed' and candidate.get('doi'):
            preview += f"DOI: {candidate['doi']}\n"
        prompt_items.append(preview)

    joined_items = '\n'.join(prompt_items)
    _, _, data_reqs = _analysis_context(benchmark_type)
    prompt = (
        f"You are ranking single-cell omics literature candidates for downstream {benchmark_type} analysis. "
        f"Review each item and return a JSON array of objects with keys: \"rank\", \"index\", \"confidence\" (0-100), \"reason\".\n\n"
        f"Ranking rubric:\n"
        f"- **Tier 1 (rank 1-2)**: Paper explicitly mentions BOTH public dataset accessions (GEO/GSE, SRA, cellxgene, Zenodo) "
        f"AND a code repository (GitHub, GitLab, Zenodo software). Prefer tier 1 papers where data was collected by authors.\n"
        f"- **Tier 2 (rank 3-5)**: Paper has clear data accessions OR a code repository but not both explicitly. "
        f"Or paper has both but from third-party data.\n"
        f"- **Tier 3 (rank 6+)**: Paper is a review, purely methodological with no data, or unclear about data/code availability.\n\n"
        f"Additional guidance:\n"
        f"- The paper itself does not need to be a benchmark publication; rank it based on reusability of data and code.\n"
        f"- Prefer papers with author-collected, first-hand datasets rather than only secondary reanalysis.\n"
        f"- Data suitability for {benchmark_type} analysis:\n{data_reqs}\n\n"
        f"Candidates:\n{joined_items}\n"
        f"Return ONLY valid JSON."
    )
    raw = _call_llm(prompt, system_prompt="You output only valid JSON arrays.",
                    temperature=0.2, call_type='rank_articles', max_tokens=8192)
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
        # cellxgene IDs are UUIDs: 8-4-4-4-12 hex digits with hyphens
        metadata['cellxgene_ids'] = _validate_accessions(
            metadata.get('cellxgene_ids', []),
            r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$',
        )
    metadata['accession_validation'] = {
        'geo_count': len(metadata.get('geo_accessions', {}).get('gse', [])),
        'sra_count': len(metadata.get('sra_accessions', [])),
        'cellxgene_count': len(metadata.get('cellxgene_ids', [])),
    }
    return metadata



def llm_collect_literature(
    benchmark_type: str,
    user_query: Optional[str] = None,
    max_results: int = 30,
    enable_audit: bool = True,
    target_accepted: int = 5,
    max_rounds: int = 3,
) -> Dict[str, Any]:
    """Search literature and dataset sources using LLM-guided queries.

    Runs up to *max_rounds* rounds of search with adaptive queries,
    stopping when *target_accepted* ``FULLY_ACCEPTED`` papers are found.

    If *enable_audit* is True, returns ``{'results': [...], 'audit': [...]}``.
    Otherwise returns ``{'results': [...]}`` for backward compatibility.
    """
    if enable_audit:
        _enable_audit()

    try:
        results = _llm_collect_impl(
            benchmark_type, user_query, max_results,
            target_accepted=target_accepted, max_rounds=max_rounds,
        )
    finally:
        audit = _get_audit() if enable_audit else []
        if enable_audit:
            _disable_audit()

    if enable_audit:
        return {'results': results, 'audit': audit}
    return {'results': results}


def llm_generate_queries_adaptive(
    benchmark_type: str,
    round_idx: int,
    prev_candidates: Optional[List[Dict[str, Any]]] = None,
    user_query: Optional[str] = None,
) -> Optional[List[str]]:
    """Generate search queries with different focus per round.

    Round 0: standard queries (current behavior).
    Round 1: explore uncovered areas — LLM sees what was found in round 0.
    Round 2: wider/narrower scope — find previously missed papers.
    """
    if round_idx == 0:
        return llm_generate_queries(benchmark_type, user_query)

    focus, _, data_reqs = _analysis_context(benchmark_type)

    # Summarize what was found in previous rounds
    prev_summary = ''
    if prev_candidates:
        sources = {}
        types_found = {'data_and_code': 0, 'data_only': 0, 'code_only': 0}
        for c in prev_candidates:
            src = c.get('source', 'unknown')
            sources[src] = sources.get(src, 0) + 1
            acc = c.get('acceptance', '')
            if acc == 'FULLY_ACCEPTED':
                types_found['data_and_code'] += 1
            elif acc == 'DATA_ONLY':
                types_found['data_only'] += 1
            elif acc == 'CODE_ONLY':
                types_found['code_only'] += 1
        src_str = '; '.join(f'{k}={v}' for k, v in sorted(sources.items()))
        prev_summary = (
            f"\nPrevious round found {len(prev_candidates)} candidates "
            f"({src_str}). "
            f"Of these, {types_found['data_and_code']} have both data and code, "
            f"{types_found['data_only']} have data only, "
            f"{types_found['code_only']} have code only."
        )

    if round_idx == 1:
        focus_instruction = (
            f"Previous search focused on standard single-cell omics keywords. "
            f"Now use DIFFERENT angles to explore uncovered areas. "
            f"Avoid repeating the same keyword combinations. "
            f"Try: specific disease names + single-cell, "
            f"specific cell types + integration, "
            f"tissue-specific atlases, "
            f"spatial transcriptomics + batch correction, "
            f"multi-modal single-cell studies, "
            f"or specific technology names (10x, MERFISH, Slide-seq)."
        )
    else:  # round_idx >= 2
        focus_instruction = (
            f"Previous rounds found various papers but may have missed important ones. "
            f"Try a WIDER scope: use shorter, more general terms. "
            f"Also try a NARROWER scope: combine VARIED public dataset IDs "
            f"(like GSE\d+ numbers you know) with single-cell terms. "
            f"Use DIFFERENT IDs in different queries to explore diverse datasets. "
            f"Search for well-known single-cell datasets or atlases. "
            f"Include terms like 'benchmark', 'ground truth', 'curated dataset', "
            f"'reference atlas', 'cell atlas', 'comprehensive atlas'."
        )

    prompt = (
        f"You are an expert in discovering single-cell omics datasets for reuse. "
        f"Generate **12** concise search queries — **2 per source** — targeting "
        f"PubMed, arXiv, Google Scholar, Semantic Scholar, bioRxiv/medRxiv, and Europe PMC. "
        f"{prev_summary}"
        f"\n\n{focus_instruction}"
        f"\n\nDATA REQUIREMENTS for integration analysis:\n{data_reqs}"
        f"\n\nRequirements:\n"
        f"- Return ONLY a JSON array of strings, no explanation.\n"
        f"- Each query must be under 200 chars.\n"
        f"- Focus on papers reporting author-collected, first-hand single-cell or spatial omics data, "
        f"and that also provide code for reuse.\n"
        f"- Do NOT use source names like 'pubmed', 'arxiv', 'biorxiv' in query text.\n"
        f"- Include terms like 'GEO', 'GSE', 'SRA', 'cellxgene', 'GitHub', 'Zenodo'.\n"
        f"- Avoid words that trigger review/meta-analysis results.\n"
    )
    if user_query:
        prompt += f"\nUser interest: {user_query}\n"

    raw = _call_llm(prompt, system_prompt='You output only valid JSON arrays.', call_type='generate_queries')
    if not raw:
        return None
    result = _parse_llm_json(raw)
    if not isinstance(result, list):
        return None
    # Filter out any queries containing source names (post-generation safety filter)
    _source_names = {'pubmed', 'arxiv', 'biorxiv', 'medrxiv', 'semantic scholar', 'google scholar', 'europe pmc'}
    filtered = [q for q in result if not any(sn in q.lower() for sn in _source_names)]
    return filtered or result


def _recover_almost_accepted(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Try to fill in missing information for almost-accepted papers.

    - ``DATA_ONLY``: has GSE/SRA/cellxgene but no code
      → re-grep raw_text for GitHub/Zenodo URLs, try GitHub search by title
    - ``CODE_ONLY``: has GitHub/Zenodo code but no data
      → re-grep raw_text for GSE/SRA patterns more aggressively
    """
    import re as _re
    for c in candidates:
        acceptance = c.get('acceptance', '')
        raw_text = c.get('raw_text') or c.get('summary') or ''

        if acceptance == 'DATA_ONLY':
            # Check if we missed any code URLs in the full text
            existing_code = set(c.get('github_repos', []) or [])
            existing_code.update(c.get('zenodo_code', []) or [])
            existing_code.update(c.get('figshare_links', []) or [])

            # Search raw text for GitHub URLs
            new_gh = _re.findall(
                r'https?://(?:www\.)?github\.com/[\w.-]+/[\w.-]+',
                raw_text, _re.I
            )
            for url in new_gh:
                if url not in existing_code:
                    existing_code.add(url)
                    c.setdefault('github_repos', []).append(url)

            # Search for Zenocode DOIs (10.5281/zenodo.XXXXX)
            new_zen = _re.findall(
                r'(?:doi\.org/)?10\.5281/zenodo\.\d+',
                raw_text, _re.I
            )
            for z in new_zen:
                url = f'https://doi.org/{z}' if not z.startswith('http') else z
                if url not in existing_code:
                    existing_code.add(url)
                    c.setdefault('zenodo_code', []).append(url)

            # Try GitHub search by paper title if still no code found
            if not existing_code:
                title = c.get('title', '')
                if title and len(title) > 10:
                    try:
                        from literature.core.search import search_github
                        # Search with a shortened title (first 5 meaningful words)
                        keywords = ' '.join(
                            w for w in title.split()
                            if len(w) > 3 and w.lower() not in {'the', 'and', 'for', 'with', 'from'}
                        )[:100]
                        gh_results = search_github(keywords, max_results=3)
                        if gh_results:
                            for gh in gh_results:
                                url = gh.get('url', '')
                                if url:
                                    c.setdefault('github_repos', []).append(url)
                                    existing_code.add(url)
                    except Exception:
                        pass

            # Recompute has_code based on new findings
            new_has_code = bool(
                c.get('github_repos') or
                c.get('zenodo_code', []) or
                c.get('figshare_links', [])
            )
            old_has_data = bool(
                c.get('gse_ids') or
                c.get('sra_ids') or
                c.get('cellxgene_ids') or
                c.get('zenodo_data', [])
            )
            if new_has_code and old_has_data:
                c['acceptance'] = 'FULLY_ACCEPTED'

        elif acceptance == 'CODE_ONLY':
            # Re-grep for GSE/SRA patterns more aggressively
            existing_data = set(c.get('gse_ids', []) or [])
            existing_data.update(c.get('sra_ids', []) or [])
            existing_data.update(c.get('cellxgene_ids', []) or [])

            new_gse = _re.findall(r'GSE\d{4,}', raw_text)
            for gse in new_gse:
                if gse not in existing_data:
                    existing_data.add(gse)
                    c.setdefault('gse_ids', []).append(gse)

            new_sra = _re.findall(
                r'(?:SRP|ERP|SRR|ERR|DRR)\d{4,}', raw_text
            )
            for sra in new_sra:
                if sra not in existing_data:
                    existing_data.add(sra)
                    c.setdefault('sra_ids', []).append(sra)

            # Recompute has_data
            new_has_data = bool(
                c.get('gse_ids') or
                c.get('sra_ids') or
                c.get('cellxgene_ids') or
                c.get('zenodo_data', [])
            )
            old_has_code = bool(
                c.get('github_repos') or
                c.get('zenodo_code', []) or
                c.get('figshare_links', [])
            )
            if new_has_data and old_has_code:
                c['acceptance'] = 'FULLY_ACCEPTED'

    return candidates


def _llm_collect_impl(
    benchmark_type: str,
    user_query: Optional[str] = None,
    max_results: int = 30,
    target_accepted: int = 5,
    max_rounds: int = 3,
) -> List[Dict[str, Any]]:
    """Actual implementation of LLM-powered literature collection.

    Runs up to *max_rounds* of search with different query focuses, stopping
    early when *target_accepted* ``FULLY_ACCEPTED`` papers are found.
    """
    from literature.core.search import (
        search_pubmed, fetch_pubmed_article,
        search_arxiv, search_zenodo, search_github,
        search_google_scholar, search_semantic_scholar,
        search_biorxiv, search_figshare, search_europe_pmc,
        fetch_arxiv_article, fetch_semantic_scholar_details,
        fetch_europe_pmc_fulltext, fetch_full_text_by_doi,
        fetch_biorxiv_article,
        timed_search,
    )
    from literature.core.extractor import extract_metadata
    from literature.core.parser import parse_doi

    all_results: List[Dict[str, Any]] = []
    seen_keys: set = set()

    round_configs = [
        {'per_source_mult': 1, 'label': 'standard'},
        {'per_source_mult': 2, 'label': 'explore_uncovered'},
        {'per_source_mult': 3, 'label': 'wider_narrower'},
    ]

    for round_idx in range(min(max_rounds, len(round_configs))):
        # Check if we already have enough FULLY_ACCEPTED papers
        n_accepted = sum(
            1 for r in all_results if r.get('acceptance') == 'FULLY_ACCEPTED'
        )
        if n_accepted >= target_accepted and round_idx > 0:
            logger.info(
                'Target of %d fully accepted papers reached (round %d). Stopping.',
                target_accepted, round_idx,
            )
            break

        config = round_configs[round_idx]
        logger.info('--- Round %d: %s ---', round_idx + 1, config['label'])

        queries = llm_generate_queries_adaptive(
            benchmark_type, round_idx,
            prev_candidates=all_results,
            user_query=user_query,
        )
        if not queries:
            logger.info('LLM query generation unavailable, using hardcoded fallback queries.')
            queries = [
                f"{benchmark_type} single-cell RNA-seq GEO",
                f"{benchmark_type} benchmark dataset",
                f"single-cell {benchmark_type}",
            ]

        candidate_items: List[Dict[str, Any]] = []
        from collections import defaultdict
        source_counts: Dict[str, int] = defaultdict(int)
        num_sources = 6  # PubMed + 5 external
        per_source_limit = max(3, max_results // num_sources) * config['per_source_mult']

        def _dedup_key(item: Dict[str, Any]) -> str:
            doi = (item.get('doi') or '').strip().lower()
            if doi:
                return f'doi:{doi}'
            item_id = (item.get('id') or '').strip()
            if item_id and item.get('source'):
                return f'{item["source"]}:{item_id}'
            title = (item.get('title') or '').strip().lower()[:80]
            if title:
                return f'title:{title}'
            return ''

        def _source_has_capacity(source_name: str) -> bool:
            return source_counts[source_name] < per_source_limit

        # --- PubMed candidates ---
        for query in queries[:2]:
            for pmid in search_pubmed(query, max_results=max_results):
                if pmid in seen_keys or not _source_has_capacity('pubmed'):
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
                candidate_items.append({
                    'title': article.get('title', ''),
                    'summary': article.get('abstract', ''),
                    'source': 'pubmed',
                    'id': pmid,
                    'url': f'https://pubmed.ncbi.nlm.nih.gov/{pmid}/',
                    'doi': doi,
                    'raw_text': raw_text,
                })
                source_counts['pubmed'] += 1

        # Fallback: if PubMed returned nothing
        if not candidate_items:
            logger.warning('PubMed returned 0 results with LLM queries; retrying with simple fallback query.')
            fallback_query = f"{benchmark_type} single-cell RNA-seq GSE"
            for pmid in search_pubmed(fallback_query, max_results=max_results):
                if pmid in seen_keys or not _source_has_capacity('pubmed'):
                    continue
                seen_keys.add(pmid)
                article = fetch_pubmed_article(pmid)
                if not article or not article.get('title'):
                    continue
                candidate_items.append({
                    'title': article.get('title', ''),
                    'summary': article.get('abstract', ''),
                    'source': 'pubmed',
                    'id': pmid,
                    'url': f'https://pubmed.ncbi.nlm.nih.gov/{pmid}/',
                    'doi': article.get('doi', ''),
                    'raw_text': f"Title: {article.get('title', '')}\n\nAbstract: {article.get('abstract', '')}",
                })
                source_counts['pubmed'] += 1

        # ----- External sources -----
        if True:
            import time as _time
            _external_queries = queries[2:] if len(queries) > 2 else queries[:2]
            _active_sources = 5
            _search_deadline = _time.time() + _SEARCH_TIMEOUT * _active_sources * 2 * 1.5

            _num_ext_q = len(_external_queries)
            _external_sources = [
                ('biorxiv', search_biorxiv, min(per_source_limit, 5),
                 (0, 1) if _num_ext_q >= 2 else (0,)),
                ('europe_pmc', search_europe_pmc, min(per_source_limit, 5),
                 (2, 3) if _num_ext_q >= 4 else (min(2, _num_ext_q-1),)),
                ('arxiv', search_arxiv, min(per_source_limit, 5),
                 (4, 5) if _num_ext_q >= 6 else (min(4, _num_ext_q-1),)),
                ('google_scholar', search_google_scholar, min(per_source_limit, 5),
                 (6,) if _num_ext_q >= 7 else (min(6, _num_ext_q-1),)),
                ('semantic_scholar', search_semantic_scholar, min(per_source_limit, 5),
                 (7, 8, 9) if _num_ext_q >= 10 else (
                     tuple(range(min(7, _num_ext_q-1), _num_ext_q)) if _num_ext_q > 7 else (min(7, _num_ext_q-1),)
                 )),
            ]

            for source_name, search_fn, call_max, q_indices in _external_sources:
                if _time.time() > _search_deadline:
                    break
                for q_idx in q_indices:
                    if q_idx >= len(_external_queries):
                        continue
                    if not _source_has_capacity(source_name):
                        break
                    query = _external_queries[q_idx]
                    results = timed_search(
                        search_fn, _search_deadline, query,
                        max_results=call_max, label=source_name,
                    )
                    for item in results:
                        if not _source_has_capacity(source_name):
                            break
                        dk = _dedup_key(item)
                        if dk and dk in seen_keys:
                            continue
                        if dk:
                            seen_keys.add(dk)

                        item_raw = item.get('raw_text') or ''
                        if not item_raw:
                            item_raw = item.get('summary') or ''
                        item_doi = item.get('doi', '') or ''
                        item_id = item.get('id', '') or ''

                        if source_name in ('europe_pmc', 'biorxiv') and (item_doi or item_id):
                            try:
                                epmc_data = fetch_europe_pmc_fulltext(item_doi or item_id)
                                if epmc_data and epmc_data.get('abstract'):
                                    enriched = (
                                        f"Title: {epmc_data.get('title', '')}"
                                        f"\n\nAbstract: {epmc_data.get('abstract', '')}"
                                    )
                                    if epmc_data.get('full_text_sections'):
                                        enriched += "\n\nFULL_TEXT:\n" + '\n'.join(epmc_data['full_text_sections'])[:20000]
                                    elif item_doi:
                                        doi_text = fetch_full_text_by_doi(item_doi)
                                        if doi_text:
                                            enriched += f"\n\nDOI_PAGE_CONTENT:\n{doi_text[:80000]}"
                                    if len(enriched) > len(item_raw):
                                        item_raw = enriched
                            except Exception:
                                pass
                            if source_name == 'biorxiv' and item_doi:
                                try:
                                    bx_data = fetch_biorxiv_article(item_doi)
                                    if bx_data and bx_data.get('full_text'):
                                        enriched = (
                                            f"Title: {bx_data.get('title', '')}"
                                            f"\n\nAbstract: {bx_data.get('abstract', '')}"
                                            f"\n\nFULL_TEXT:\n{bx_data['full_text']}"
                                        )
                                        if len(enriched) > len(item_raw):
                                            item_raw = enriched
                                except Exception:
                                    pass
                        elif source_name == 'arxiv' and item_id:
                            try:
                                arxiv_data = fetch_arxiv_article(item_id)
                                if arxiv_data and arxiv_data.get('abstract'):
                                    enriched = f"Title: {arxiv_data.get('title', '')}\n\nAbstract: {arxiv_data.get('abstract', '')}"
                                    if arxiv_data.get('full_text'):
                                        enriched += f"\n\nFULL_TEXT:\n{arxiv_data['full_text']}"
                                    elif item_doi:
                                        doi_text = fetch_full_text_by_doi(item_doi)
                                        if doi_text:
                                            enriched += f"\n\nDOI_PAGE_CONTENT:\n{doi_text[:80000]}"
                                    if len(enriched) > len(item_raw):
                                        item_raw = enriched
                            except Exception:
                                pass
                        elif source_name in ('semantic_scholar', 'google_scholar') and item_doi:
                            try:
                                doi_text = fetch_full_text_by_doi(item_doi)
                                if doi_text:
                                    item_raw = item_raw + f"\n\nDOI_PAGE_CONTENT:\n{doi_text[:80000]}"
                            except Exception:
                                pass
                            try:
                                epmc_data = fetch_europe_pmc_fulltext(item_doi)
                                if epmc_data and epmc_data.get('abstract'):
                                    enriched = (
                                        f"Title: {epmc_data.get('title', '')}"
                                        f"\n\nAbstract: {epmc_data.get('abstract', '')}"
                                    )
                                    if epmc_data.get('full_text_sections'):
                                        enriched += "\n\nFULL_TEXT:\n" + '\n'.join(epmc_data['full_text_sections'])[:20000]
                                    if len(enriched) > len(item_raw):
                                        item_raw = enriched
                            except Exception:
                                pass

                        item['raw_text'] = item_raw
                        candidate_items.append(item)
                        source_counts[source_name] += 1

        if not candidate_items:
            continue

        # Rank candidates
        ranked_items = llm_rank_articles(candidate_items, benchmark_type)
        if ranked_items:
            candidate_items = ranked_items

        # --- Extract paper details and filter ---
        round_results: List[Dict[str, Any]] = []
        for candidate in candidate_items[:max_results]:
            candidate_title = (candidate.get('title') or '').lower()
            _review_keywords = [
                'review', 'survey', 'overview', 'systematic review', 'meta-analysis',
                'literature review', 'comprehensive review', 'critical review',
                'scoping review', 'narrative review', 'perspective',
            ]
            if any(kw in candidate_title for kw in _review_keywords):
                continue

            text = candidate.get('raw_text') or ''
            if not text:
                text = candidate.get('summary') or ''

            source = candidate.get('source', '')
            candidate_id = candidate.get('id', '') or ''
            doi = candidate.get('doi', '') or ''

            def _has_enrichment(t: str) -> bool:
                return 'DOI_PAGE_CONTENT' in t or 'FULL_TEXT' in t

            # (full-text enrichment per source — same as before)
            if source == 'pubmed':
                text = candidate.get('raw_text') or text
                if not _has_enrichment(text):
                    try:
                        epmc_data = fetch_europe_pmc_fulltext(candidate_id)
                        if epmc_data and epmc_data.get('abstract'):
                            full = (
                                f"Title: {epmc_data.get('title', '')}"
                                f"\n\nAbstract: {epmc_data.get('abstract', '')}"
                            )
                            if epmc_data.get('full_text_sections'):
                                full += "\n\nFULL_TEXT:\n" + '\n'.join(epmc_data['full_text_sections'])[:20000]
                            elif doi:
                                doi_text = fetch_full_text_by_doi(doi)
                                if doi_text:
                                    full += f"\n\nDOI_PAGE_CONTENT:\n{doi_text[:80000]}"
                            if len(full) > len(text):
                                text = full
                    except Exception:
                        pass
            elif source == 'arxiv' and candidate_id:
                try:
                    enriched = False
                    if doi:
                        doi_text = fetch_full_text_by_doi(doi)
                        if doi_text:
                            text += f"\n\nDOI_PAGE_CONTENT:\n{doi_text[:80000]}"
                            enriched = True
                    if not enriched:
                        arxiv_data = fetch_arxiv_article(candidate_id)
                        if arxiv_data and arxiv_data.get('abstract'):
                            full = f"Title: {arxiv_data.get('title', '')}\n\nAbstract: {arxiv_data.get('abstract', '')}"
                            if arxiv_data.get('full_text'):
                                full += f"\n\nFULL_TEXT:\n{arxiv_data['full_text']}"
                            if len(full) > len(text):
                                text = full
                except Exception:
                    pass
            elif source == 'europe_pmc' and (doi or candidate_id):
                if not _has_enrichment(text):
                    try:
                        epid = doi or candidate_id
                        epmc_data = fetch_europe_pmc_fulltext(epid)
                        if epmc_data and epmc_data.get('abstract'):
                            full = (
                                f"Title: {epmc_data.get('title', '')}"
                                f"\n\nAbstract: {epmc_data.get('abstract', '')}"
                            )
                            if epmc_data.get('full_text_sections'):
                                full += "\n\nFULL_TEXT:\n" + '\n'.join(epmc_data['full_text_sections'])[:20000]
                            if not epmc_data.get('full_text_sections') and doi:
                                doi_text = fetch_full_text_by_doi(doi)
                                if doi_text:
                                    full += f"\n\nDOI_PAGE_CONTENT:\n{doi_text[:80000]}"
                            if len(full) > len(text.split()) * 1.2:
                                text = full
                    except Exception:
                        pass
                if not _has_enrichment(text) and doi:
                    try:
                        doi_text = fetch_full_text_by_doi(doi)
                        if doi_text:
                            text += f"\n\nDOI_PAGE_CONTENT:\n{doi_text[:80000]}"
                    except Exception:
                        pass
            elif source == 'biorxiv' and doi:
                if not _has_enrichment(text):
                    try:
                        bx_data = fetch_biorxiv_article(doi)
                        if bx_data and bx_data.get('full_text'):
                            text = (
                                f"Title: {bx_data.get('title', '')}"
                                f"\n\nAbstract: {bx_data.get('abstract', '')}"
                                f"\n\nFULL_TEXT:\n{bx_data['full_text']}"
                            )
                        elif bx_data and bx_data.get('abstract'):
                            full = (
                                f"Title: {bx_data.get('title', '')}"
                                f"\n\nAbstract: {bx_data.get('abstract', '')}"
                            )
                            doi_text = fetch_full_text_by_doi(doi)
                            if doi_text:
                                full += f"\n\nDOI_PAGE_CONTENT:\n{doi_text[:80000]}"
                            if len(full) > len(text.split()) * 1.2:
                                text = full
                    except Exception:
                        pass
                if not _has_enrichment(text) and doi:
                    try:
                        doi_text = fetch_full_text_by_doi(doi)
                        if doi_text:
                            text += f"\n\nDOI_PAGE_CONTENT:\n{doi_text[:80000]}"
                    except Exception:
                        pass
            elif source == 'semantic_scholar' and candidate_id:
                try:
                    if doi:
                        doi_text = fetch_full_text_by_doi(doi)
                        if doi_text:
                            text += f"\n\nDOI_PAGE_CONTENT:\n{doi_text[:80000]}"
                except Exception:
                    pass
                if not _has_enrichment(text):
                    try:
                        epid = doi or candidate_id
                        epmc_data = fetch_europe_pmc_fulltext(epid)
                        if epmc_data and epmc_data.get('abstract'):
                            full = (
                                f"Title: {epmc_data.get('title', '')}"
                                f"\n\nAbstract: {epmc_data.get('abstract', '')}"
                            )
                            if epmc_data.get('full_text_sections'):
                                full += "\n\nFULL_TEXT:\n" + '\n'.join(epmc_data['full_text_sections'])[:20000]
                            if len(full) > len(text):
                                text = full
                    except Exception:
                        pass
            else:
                if doi and len(text) < 2000:
                    try:
                        doi_text = fetch_full_text_by_doi(doi)
                        if doi_text:
                            text = text + f"\n\nDOI_PAGE_CONTENT:\n{doi_text[:80000]}"
                    except Exception:
                        pass

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
            zenodo_data = llm_data.get('zenodo_data', []) or llm_data.get('zenodo_records', [])
            zenodo_code = llm_data.get('zenodo_code', []) or llm_data.get('zenodo_records', [])
            figshare_links = llm_data.get('figshare_links', [])
            has_data = bool(gse or sra or cellxgene or zenodo_data)
            has_code = bool(github_repos or zenodo_code or figshare_links)

            # --- Tiered acceptance ---
            if has_data and has_code:
                acceptance = 'FULLY_ACCEPTED'
            elif has_data and not has_code:
                acceptance = 'DATA_ONLY'
            elif not has_data and has_code:
                acceptance = 'CODE_ONLY'
            else:
                acceptance = 'REJECTED'

            skip_reason_parts = []
            if not has_data:
                skip_reason_parts.append('no dataset accessions (GSE/SRA/cellxgene/Zenodo data) found')
            if not has_code:
                skip_reason_parts.append('no code repositories (GitHub/Zenodo code/Figshare) found')
            skip_reason = '; '.join(skip_reason_parts) if skip_reason_parts else 'none'

            _audit_call(
                'filter_decision', f"Candidate {candidate.get('id', '?')}",
                '', None,
                {
                    'decision': acceptance,
                    'title': candidate.get('title', '')[:120],
                    'source': candidate.get('source', ''),
                    'has_data': has_data,
                    'has_code': has_code,
                    'gse_found': len(gse),
                    'sra_found': len(sra),
                    'cellxgene_found': len(cellxgene),
                    'github_found': len(github_repos),
                    'zenodo_data_found': len(zenodo_data),
                    'zenodo_code_found': len(zenodo_code),
                    'figshare_found': len(figshare_links),
                    'reason': skip_reason if acceptance in ('DATA_ONLY', 'CODE_ONLY', 'REJECTED') else 'data AND code both present',
                },
                None, 0.0
            )

            if acceptance == 'REJECTED':
                continue

            result_entry = {
                'title': candidate.get('title', ''),
                'doi': candidate.get('doi', ''),
                'source': source,
                'id': candidate.get('id', ''),
                'acceptance': acceptance,
                'gse_ids': gse,
                'sra_ids': sra,
                'cellxgene_ids': cellxgene,
                'github_repos': llm_data.get('github_repos', []),
                'zenodo_data': llm_data.get('zenodo_data', []),
                'zenodo_code': llm_data.get('zenodo_code', []),
                'figshare_links': llm_data.get('figshare_links', []),
                'relevance_score': relevance,
                'organism': organism,
                'tissue': tissue,
                'technology': technology,
                'reason': llm_data.get('reason', ''),
                'methods_summary': llm_data.get('methods_summary', ''),
            }
            round_results.append(result_entry)

        # --- Round completion: recovery + merge ---
        _recover_almost_accepted(round_results)
        all_results.extend(round_results)

        n_fully = sum(1 for r in all_results if r.get('acceptance') == 'FULLY_ACCEPTED')
        n_data = sum(1 for r in all_results if r.get('acceptance') == 'DATA_ONLY')
        n_code = sum(1 for r in all_results if r.get('acceptance') == 'CODE_ONLY')
        logger.info(
            'Round %d complete: %d fully accepted, %d data-only, %d code-only (target=%d)',
            round_idx + 1, n_fully, n_data, n_code, target_accepted,
        )

    # --- Final recovery pass across all rounds ---
    all_results = _recover_almost_accepted(all_results)
    all_results.sort(key=lambda r: (
        0 if r.get('acceptance') == 'FULLY_ACCEPTED' else
        1 if r.get('acceptance') == 'DATA_ONLY' else 2
    ), reverse=False)

    _audit_call(
        'collect_summary', f'target={target_accepted} rounds={max_rounds}',
        '', None,
        {
            'total_candidates': len(all_results),
            'fully_accepted': sum(1 for r in all_results if r.get('acceptance') == 'FULLY_ACCEPTED'),
            'data_only': sum(1 for r in all_results if r.get('acceptance') == 'DATA_ONLY'),
            'code_only': sum(1 for r in all_results if r.get('acceptance') == 'CODE_ONLY'),
        },
        None, 0.0
    )

    return all_results[:max_results]

