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
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# Timeout for external HTTP requests (connect + read).
_SEARCH_TIMEOUT = 15  # seconds

# ---------------------------------------------------------------------------
# Model routing: Flash for high-volume structured tasks, Pro for judgment
# ---------------------------------------------------------------------------
# OmicsClaw provider config maps:
#   "deepseek-v4-flash" → deepseek-v4-flash API model
#   "deepseek-v4-pro"   → deepseek-v4-pro   API model
_MODEL_FLASH = "deepseek-v4-flash"
_MODEL_PRO = "deepseek-v4-pro"

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
              call_type: str = 'unknown', max_tokens: int = 4096,
              llm_model: str = '') -> Optional[str]:
    """Try to call the OmicsClaw LLM. Returns None if unavailable.

    Parameters
    ----------
    llm_model:
        Explicit model name override (e.g. ``"deepseek-v4-flash"`` or
        ``"deepseek-v4-pro"``). When empty, the default provider model
        is used.
    """
    t0 = _time_mod.time()
    try:
        from omicsclaw.autoagent.llm_client import call_llm

        result = call_llm(
            directive,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            llm_model=llm_model,
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
    """Repair truncated/incomplete JSON by closing brackets and stripping
    trailing incomplete key-value pairs (e.g. ``"key": `` with no value)."""
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
        # Strip trailing comma before a closing bracket (e.g. "false,}")
        if repaired_chars and repaired_chars[-1] == ',':
            repaired_chars[-1] = ' '
        repaired_chars.append(stack.pop())

    result = ''.join(repaired_chars)
    # Also strip any trailing comma that ended up right before a closing bracket
    result = re.sub(r',\s*}', '}', result)
    result = re.sub(r',\s*]', ']', result)

    # --- Strip trailing incomplete key-value pair ---
    # When LLM output is truncated mid-stream, the last element may be
    # incomplete: ``"key": `` (no value), ``"key": 1`` (partial number),
    # or ``"key": "incomplete string`` (unclosed, already handled above).
    # Strategy: find the last ``"key":`` that lacks a complete value
    # and strip everything from that colon forward, then re-close.
    result = _strip_trailing_incomplete(result)
    return result


def _strip_trailing_incomplete(text: str) -> str:
    """Remove the last incomplete key-value pair from repaired JSON.

    Example: ``[{"a":1},{"b":}]`` → ``[{"a":1}]``
             ``[{"a":1},{"b":  ``  → ``[{"a":1}]`` (closing brackets already added)
    """
    # Strategy: find the last ``:`` not inside a string and check if
    # there's a complete value after it (before the closing bracket).
    # If not, strip back to the preceding ``,``.
    #
    # We search for the pattern: ``:`` followed by optional whitespace
    # then immediately a ``,``, ``}`` or ``]`` (i.e. no value).
    # Also handles partial values like ``: 95`` at the very end when
    # the value might be incomplete (we conservatively strip it).
    improved = re.sub(
        r',\s*"[^"]*"\s*:\s*(?=[,}\]])', '',
        text,
    )
    if improved != text:
        # Re-close brackets after stripping (inline to avoid recursion)
        open_count = improved.count('{') + improved.count('[')
        close_count = improved.count('}') + improved.count(']')
        missing = open_count - close_count
        if missing > 0:
            stack2: List[str] = []
            in_str2 = False
            esc2 = False
            for ch in improved:
                if esc2:
                    esc2 = False; continue
                if ch == '\\':
                    esc2 = True; continue
                if ch == '"':
                    in_str2 = not in_str2; continue
                if in_str2:
                    continue
                if ch == '{':
                    stack2.append('}')
                elif ch == '[':
                    stack2.append(']')
                elif ch in ('}', ']'):
                    if stack2 and ch == stack2[-1]:
                        stack2.pop()
            while stack2:
                if improved and improved[-1] == ',':
                    improved = improved[:-1]
                improved += stack2.pop()
        return improved
    return text


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
            'cell-type label, reference map, marker gene, classification, cell identity, annotated atlas',
            'annotated cell types, well-characterized tissue, marker gene lists, reference atlas, ground-truth labels',
            'Requires a single-cell dataset with KNOWN cell-type labels (ground-truth annotation) '
            'or an annotated reference atlas. The data MUST include per-cell cell-type labels '
            '(e.g. "CD4 T cells", "hepatocytes", cluster names) — papers that only provide raw '
            'unlabeled expression matrices are NOT suitable unless they also supply an '
            'annotation/ground-truth label file. Look for "cell-type annotation", "reference mapping", '
            '"label transfer", "annotated atlas", "cell-type labels", or "scRNA-seq atlas with cell types".',
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
    """Use LLM to generate search queries for biological discovery papers.

    The goal is to find papers that (1) collected or integrated original single-cell
    or spatial omics data AND (2) performed biological analysis to draw conclusions
    about cell types, tissues, development, disease, or other biological phenomena.
    We want primary research papers with data, NOT purely computational method papers.
    """
    focus, _, data_reqs = _analysis_context(benchmark_type)
    prompt = (
        f"You are an expert in discovering single-cell omics biological research papers. "
        f"Generate **12** concise search queries — **2 per source** — targeting "
        f"PubMed, arXiv, Semantic Scholar, bioRxiv/medRxiv, and Europe PMC. "
        f"Each query must be tailored to the source's strengths:\n\n"
        f"- **PubMed / Europe PMC**: query by biological topics + single-cell (e.g. cell atlas, "
        f"tissue-specific transcriptomics, disease mechanism at single-cell resolution). "
        f"Include GEO/GSE terms to find papers that deposited data.\n"
        f"- **arXiv / bioRxiv**: query preprints reporting biological discoveries from "
        f"single-cell data — cell atlases, tissue maps, developmental trajectories, "
        f"disease signatures.\n"
        f"- **Semantic Scholar**: query for highly-cited biological discovery papers "
        f"that generated new single-cell datasets and reported biological findings.\n\n"
        f"CRITICAL PRIORITY — find papers that:\n"
        f"1. Collected or assembled original single-cell/spatial omics data (author-collected)\n"
        f"2. Performed biological analysis: cell type characterization, tissue mapping, "
        f"   developmental biology, disease mechanism, biomarker discovery, aging, etc.\n"
        f"3. Deposited data to GEO/SRA/cellxgene/Zenodo\n"
        f"\n"
        f"DO NOT focus on — exclude these from ALL queries:\n"
        f"- Pure method/algorithm papers (new tools, packages, pipelines, algorithms)\n"
        f"  These typically have titles like: \"Tool: ...\", \"Package: ...\", "
        f"\"...: a method for ...\", \"... enables ... analysis\"\n"
        f"- Benchmark papers comparing methods\n"
        f"- Review papers, surveys, meta-analyses\n"
        f"- Papers that only reanalyze existing public data without new biological insight\n"
        f"- Papers about software/tool development (even if they process scRNA-seq data)\n"
        f"\n"
        f"INSTEAD, target papers with titles like:\n"
        f"  \"A single-cell atlas of ...\", \"Single-cell transcriptomics reveals ...\", "
        f"\"Cell-type mapping of ...\"\n"
        f"These are BIOLOGICAL DISCOVERY papers — they generate new data and biological insights.\n"
        f"\n"
        f"NOTE: GitHub and Zenodo code are helpful signals but not the primary target — "
        f"we want the BIOLOGICAL DISCOVERY paper itself.\n\n"
        f"Requirements:\n"
        f"- Return ONLY a JSON array of strings, no explanation.\n"
        f"- Each query must be under 200 chars.\n"
        f"- Use biological discovery terms: cell atlas, tissue atlas, cell type, developmental, "
        f"  differentiation, lineage, disease, mechanism, signature, heterogeneity, landscape, "
        f"  map, trajectory, niche, microenvironment.\n"
        f"- Include terms like \"single-cell\", \"scRNA-seq\", \"snRNA-seq\", \"spatial transcriptomics\", "
        f"  \"GEO\", \"GSE\", \"cellxgene\" to ensure papers with deposited data.\n"
        f"- Include \"human\" or \"mouse\" or specific tissues/organs to target biological studies.\n"
        f"- For PubMed/Europe PMC queries, ALWAYS include \"GEO\" or \"GSE\" to find deposited data.\n"
        f"- 🔴 CRITICAL: Do NOT use method/algorithm keywords (\"tool\", \"package\", \"pipeline\", "
        f"  \"software\", \"method\", \"algorithm\", \"framework\", \"integration\", \"batch correction\", "
        f"  \"clustering\", \"imputation\", \"normalization\", \"embedding\", \"representation learning\").\n"
        f"- 🔴 CRITICAL: Do NOT include source names (like \"PubMed\", \"arXiv\", \"bioRxiv\", \"Europe PMC\")\n"
        f"  inside the query text itself.\n"
        f"- Avoid words that trigger review/meta-analysis results (\"survey\", \"review\", \"overview\").\n"
    )
    if user_query:
        prompt += f"\nUser interest: {user_query}\n"

    raw = _call_llm(prompt, system_prompt="You output only valid JSON arrays.", call_type='generate_queries',
                    llm_model=_MODEL_FLASH)
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
    # Classify paper type from text
    _is_method_paper = any(kw in text[:4000].lower() for kw in [
        'we propose', 'we introduce', 'we present', 'our method', 'our framework',
        'novel algorithm', 'novel method', 'we develop a', 'we designed',
        'our approach achieves', 'state-of-the-art performance',
    ])
    prompt = (
        f"You are a scientific literature curator specialized in single-cell omics data discovery. "
        f"Analyze the following paper text to extract dataset accessions, code repositories, and "
        f"biological metadata relevant for {benchmark_type} data analysis.\n\n"
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
        f"    \"other_code_urls\": [...],\n"
        f"    \"data_format\": [\"h5ad\", \"mtx\", \"rds\", \"loom\", \"seurat\", \"other\"],\n"
        f"    \"num_samples\": \"...\",\n"
        f"    \"num_cells\": \"...\",\n"
        f"    \"organism\": \"...\",\n"
        f"    \"tissue\": \"...\",\n"
        f"    \"technology\": \"...\",\n"
        f"    \"data_quality_signals\": [\"raw_counts\", \"processed_counts\", \"both\", \"unknown\"],\n"
        f"    \"has_cell_type_labels\": true|false,\n"
        f"    \"data_origin\": \"author_collected\"|\"public_reanalysis\"|\"unclear\",\n"
        f"    \"first_hand_data\": false,\n"
        f"    \"benchmark_relevance_score\": 0-10,\n"
        f"    \"reason\": \"...\",\n"
        f"    \"methods_summary\": \"...\",\n"
        f"    \"code_snippets\": \"...\"\n"
        f"  }}\n\n"
        f"Use the terminology: {focus}.\n"
        f"Scoring guidance:\n"
        f"- First, determine if this is a BIOLOGICAL DISCOVERY paper or a COMPUTATIONAL METHOD paper.\\n"
        f"  * Biological discovery: collected data, analyzed to understand biology (cell types, disease, development, tissue)\\n"
        f"  * Computational method: proposes new algorithm/tool/framework, evaluates on existing data\\n"
        f"- Prefer papers that clearly state 'we generated', 'we collected', 'our dataset' → first_hand_data=true, data_origin='author_collected'.\n"
        f"- **CRITICAL: Look for 'Data Availability' and 'Code Availability' sections!**\n"
        f"  Many biological discovery papers deposit data to GEO/SRA/cellxgene and state accessions in these sections.\n"
        f"  Pay special attention to **KEY_SECTIONS:** — they contain the article's data deposition\n"
        f"  and code repository information extracted directly from the HTML.\n"
        f"  Scan the text for phrases like:\n"
        f"    * 'Data availability', 'Data deposition', 'Accession numbers', 'Data are available'\n"
        f"    * 'Code availability', 'Code is available at', 'Source code', 'Software availability'\n"
        f"    * 'GSE', 'SRP', 'SRR', 'PRJNA', 'PRJEB', 'ArrayExpress'\n"
        f"    * 'cellxgene', 'CZ CELLxGENE', 'CELLxGENE', 'cellxgene.cz'\\n"
        f"    * GitHub URLs (github.com/...), Zenodo DOIs, Figshare links\\n"
        f"- For biological discovery papers: be more lenient about code — the data is the primary asset. "
        f"  If the paper collected data and deposited it to GEO/SRA/cellxgene, score it highly (≥6) even without code.\\n"
        f"- For computational method papers: require BOTH data accessions AND code repository for high score.\\n"
        f"- **Critical: distinguish Zenodo records by content type**:\n"
        f"    * Put dataset DOIs/URLs in **zenodo_data** (raw .h5ad/.mtx/.rds files, count matrices, supplements)\n"
        f"    * Put software/notebook DOIs/URLs in **zenodo_code** (code archives, Python packages, analysis scripts)\n"
        f"    * If unsure, include in BOTH lists.\n"
        f"- **has_cell_type_labels**: set TRUE only if the deposited dataset itself includes per-cell\n"
        f"  cell-type labels / annotation (e.g. an annotated Seurat/h5ad object, a metadata file with\n"
        f"  cell_type/annotation columns, cluster labels). FALSE if only raw unlabeled counts are deposited.\n"
        f"  For {benchmark_type} benchmarks this field is critical.\n"
        f"- **New: capture institutional/non-standard code URLs in other_code_urls**.\n"
        f"    * ANY code repository URL that is NOT GitHub and NOT Zenodo (Figshare links already have their own key figshare_links).\n"
        f"    * Examples: keeper.mpdl.mpg.de, osf.io, dryad, institutional GitLab, custom data portals.\n"
        f"    * Look for URLs in 'Code Availability' sections that don't match github.com or zenodo.org patterns.\n"
        f"    * Pay attention to URLs like 'https://keeper.mpdl.mpg.de/d/...', 'https://osf.io/...', 'https://gitlab.com/...' etc.\n"
        f"- Penalize (score ≤ 3) papers that only reanalyze existing public data without providing new data or reusable code.\n"
        f"- If the text contains no datasets, return empty arrays.\n"
        f"\n--- Data requirements for {benchmark_type} benchmark ---\n"
        f"The paper's data is most suitable if it matches these requirements:\n"
        f"{data_reqs}\n"
        f"A higher benchmark_relevance_score should reflect how well the data meets these requirements.\n"
        f"Return ONLY valid JSON."
    )

    raw = _call_llm(prompt, system_prompt="You are a skilled scientific literature curator.",
                    temperature=0.2, call_type='extract_paper_details', llm_model=_MODEL_FLASH)
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
        f"- **Tier 1 (rank 1-2)**: BIOLOGICAL DISCOVERY paper: paper collected/assembled original single-cell data, "
        f"performed biological analysis (cell type characterization, tissue mapping, disease mechanism, developmental biology, etc.), "
        f"AND deposited data to GEO/SRA/cellxgene/Zenodo. Code repository is a bonus but not required.\n"
        f"- **Tier 2 (rank 3-5)**: Paper has clear data accessions and/or code but is a computational method paper, "
        f"or a biological discovery paper with data but lacking explicit data accessions.\n"
        f"- **Tier 3 (rank 6+)**: Paper is a review, purely methodological with no data, unclear about data/code availability, "
        f"or only reanalyzes public data without new biological insight.\n\n"
        f"Additional guidance:\n"
        f"- PRIORITIZE biological discovery papers that generated new data and drew biological conclusions.\n"
        f"- DEPRIORITIZE pure method papers (new clustering, integration, imputation, normalization methods) "
        f"even if they provide code and use public data.\n"
        f"- Prefer papers with author-collected, first-hand datasets rather than only secondary reanalysis.\n"
        f"- Data suitability for {benchmark_type} analysis:\n{data_reqs}\n\n"
        f"Candidates:\n{joined_items}\n"
        f"Return ONLY valid JSON."
    )
    raw = _call_llm(prompt, system_prompt="You output only valid JSON arrays.",
                    temperature=0.2, call_type='rank_articles', max_tokens=16384,
                    llm_model=_MODEL_PRO)
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
            f"Previous search focused on general single-cell biology keywords. "
            f"Now try DIFFERENT biological angles to find discovery papers we missed. "
            f"Avoid repeating the same keyword combinations. "
            f"Try:\n"
            f"- Specific diseases + single-cell atlas (cancer, Alzheimer's, diabetes, IBD, COVID-19)\n"
            f"- Specific tissues/organs (lung, brain, heart, liver, kidney, skin, pancreas, gut)\n"
            f"- Developmental biology (embryogenesis, organogenesis, differentiation trajectories)\n"
            f"- Aging, regeneration, or immune response at single-cell resolution\n"
            f"- Cell type discovery or cell state characterization in specific contexts\n"
            f"- Spatial biology: tissue architecture, microenvironment, cell-cell interactions\n"
            f"\n"
            f"IMPORTANT: Target papers that COLLECTED original data and performed BIOLOGICAL "
            f"analysis. Avoid method/algorithm terms entirely."
        )
    else:  # round_idx >= 2
        focus_instruction = (
            f"Previous rounds found various papers but may have missed important biological "
            f"discovery papers. Try WIDER and NARROWER angles:\n"
            f"- Wider: use shorter general terms like 'single-cell atlas human tissue' or "
            f"  'scRNA-seq GSE tissue map'\n"
            f"- Narrower: combine specific known dataset IDs (GSE\d+) with biological context "
            f"  terms. Search for well-known single-cell atlases (Tabula Sapiens, Human Cell "
            f"  Atlas, Tabula Muris, HCA, HTAN, etc.)\n"
            f"- Try: specific cell types (T cells, neurons, hepatocytes, cardiomyocytes, "
            f"  epithelial cells) + single-cell atlas + GEO\n"
            f"- Try: specific technology (10x, Smart-seq2, Drop-seq, MERFISH, Visium) + "
            f"  biological discovery + GSE\n"
            f"\n"
            f"IMPORTANT: Avoid method/algorithm terms. Keep the focus on BIOLOGICAL DISCOVERY "
            f"papers that collected data and drew biological conclusions."
        )

    prompt = (
        f"You are an expert in discovering single-cell omics biological research papers. "
        f"Generate **12** concise search queries — **2 per source** — targeting "
        f"PubMed, arXiv, Semantic Scholar, bioRxiv/medRxiv, and Europe PMC. "
        f"{prev_summary}"
        f"\n\n{focus_instruction}"
        f"\n\nRequirements:\n"
        f"- Return ONLY a JSON array of strings, no explanation.\n"
        f"- Each query must be under 200 chars.\n"
        f"- Target BIOLOGICAL DISCOVERY papers: cell atlases, tissue maps, disease "
        f"  mechanisms, developmental biology, cell type characterization.\n"
        f"- Papers should have COLLECTED original data and performed BIOLOGICAL analysis.\n"
        f"- Include terms like 'GEO', 'GSE', 'cellxgene' to find papers with deposited data.\n"
        f"- For PubMed/Europe PMC queries, ALWAYS include 'GEO' or 'GSE'.\n"
        f"🔴 STRICTLY FORBIDDEN keywords in any query:\n"
        f"  'tool', 'package', 'pipeline', 'software', 'method', 'algorithm',\n"
        f"  'framework', 'platform', 'resource', 'workflow', 'protocol'\n"
        f"- Do NOT use method/algorithm keywords (integration, clustering, imputation, etc.).\n"
        f"- Do NOT use source names like 'pubmed', 'arxiv', 'biorxiv' in query text.\n"
        f"- Avoid words that trigger review/meta-analysis results.\n"
    )
    if user_query:
        prompt += f"\nUser interest: {user_query}\n"

    raw = _call_llm(prompt, system_prompt='You output only valid JSON arrays.', call_type='generate_queries',
                    llm_model=_MODEL_FLASH)
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
            # Guard against Zenodo-overlap false positive:
            # if the only "data" is a Zenodo DOI also in code, and relevance is low, skip upgrade
            _zen_data = set(c.get('zenodo_data', []) or [])
            _zen_code = set(c.get('zenodo_code', []) or [])
            _has_other_data = bool(c.get('gse_ids') or c.get('sra_ids') or c.get('cellxgene_ids'))
            _has_other_code = bool(c.get('github_repos') or c.get('figshare_links'))
            _zen_overlap_upgrade = (
                not _has_other_data and not _has_other_code
                and _zen_data and _zen_data == _zen_code
                and c.get('relevance_score', 0) < 7
            )
            if new_has_code and old_has_data and not _zen_overlap_upgrade:
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

            # Search for SRA / BioProject accessions
            new_sra = _re.findall(r'(?:SRP|SRR|PRJNA|PRJEB|ERP|DRP)\d{4,}', raw_text)
            for sra in new_sra:
                if sra not in existing_data:
                    existing_data.add(sra)
                    c.setdefault('sra_ids', []).append(sra)

            # Search for cellxgene CZ IDs (UUIDs preceded by cellxgene/CZ context)
            new_cellx = _re.findall(
                r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
                raw_text
            )
            for cx in new_cellx:
                if cx not in existing_data:
                    existing_data.add(cx)
                    c.setdefault('cellxgene_ids', []).append(cx)

            # Search for ArrayExpress accessions
            new_array = _re.findall(r'E-(?:MTAB|GEOD|MEXP|TABM)-\d{4,}', raw_text)
            for ae in new_array:
                if ae not in existing_data:
                    existing_data.add(ae)
                    c.setdefault('gse_ids', []).append(ae)

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
        search_pubmed_as_source,
        search_arxiv, search_zenodo, search_github,
        search_google_scholar, search_semantic_scholar,
        search_biorxiv, search_figshare, search_europe_pmc,
        search_springer_nature, fetch_springer_nature_pdf, fetch_springer_nature_fulltext_html,
        fetch_arxiv_article, fetch_semantic_scholar_details,
        fetch_europe_pmc_fulltext, fetch_full_text_by_doi,
        fetch_biorxiv_article,
        timed_search,
    )
    from literature.core.extractor import extract_metadata

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
        n_data = sum(1 for r in all_results if r.get('acceptance') == 'DATA_ONLY')
        print(f"\n  ══ Round {round_idx + 1}/{min(max_rounds, len(round_configs))} "
              f"({n_accepted}/{target_accepted} fully accepted so far) ══")
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
        # Pad queries to exactly 12 so all 6 sources get 2 queries each
        _default_queries = [
            'single-cell atlas human tissue GEO GSE',
            'scRNA-seq tissue map cell types deposited GEO',
            'single-cell RNA-seq human development differentiation GSE',
            'mouse single-cell atlas cell types GSE',
            'human disease single-cell RNA-seq atlas GEO',
            'single-nucleus RNA-seq tissue atlas cell types GSE',
            'spatial transcriptomics human tissue atlas GEO',
            'single-cell multi-omics human cell atlas GSE',
            'development single-cell atlas mouse embryo GEO',
            'aging single-cell transcriptomics tissue GEO',
            'immune cell atlas single-cell RNA-seq human GEO',
            'cancer single-cell atlas tumor microenvironment GSE',
        ]
        while len(queries) < 12:
            queries.append(_default_queries[len(queries) % len(_default_queries)])

        candidate_items: List[Dict[str, Any]] = []
        source_counts: Dict[str, int] = defaultdict(int)
        num_sources = 6  # pubmed + biorxiv + europe_pmc + arxiv + springer_nature + semantic_scholar
        per_source_limit = max(3, max_results // num_sources) * config['per_source_mult']

        def _dedup_key(item: Dict[str, Any]) -> str:
            doi = str(item.get('doi') or '').strip().lower()
            if doi:
                return f'doi:{doi}'
            item_id = str(item.get('id') or '').strip()
            if item_id and item.get('source'):
                return f'{item["source"]}:{item_id}'
            title = str(item.get('title') or '').strip().lower()[:80]
            if title:
                return f'title:{title}'
            return ''

        def _has_enrichment(t: str) -> bool:
            return 'DOI_PAGE_CONTENT' in t or 'FULL_TEXT' in t

        # ----- Unified source search (PubMed + 5 external) — PARALLEL -----
        _unified_sources = [
            ('semantic_scholar', search_semantic_scholar, min(per_source_limit, 10), (0, 1)),
            ('springer_nature', search_springer_nature, min(per_source_limit, 10), (2, 3)),
            ('arxiv', search_arxiv, min(per_source_limit, 10), (4, 5)),
            ('pubmed', search_pubmed_as_source, min(per_source_limit, 10), (6, 7)),
            ('biorxiv', search_biorxiv, min(per_source_limit, 10), (8, 9)),
            ('europe_pmc', search_europe_pmc, min(per_source_limit, 10), (10, 11)),
        ]
        _search_deadline = _time_mod.time() + 60 * len(_unified_sources)

        print(f"\n  ── Round {round_idx + 1}: searching {len(_unified_sources)} sources (parallel) ──")
        _round_hard_deadline = _time_mod.time() + 600
        _round_start = _time_mod.time()

        # Snapshot shared state for thread-safe dedup
        _seen_snapshot = seen_keys.copy()

        def _search_one_source(source_name, search_fn, call_max, q_indices):
            """Search one source — runs in a background thread."""
            local_items: List[Dict[str, Any]] = []
            local_seen: set = set()
            local_count = 0
            _src_deadline = _time_mod.time() + 60
            queries_for_source = [queries[i] for i in q_indices if i < len(queries)]
            q_short = [q[:60] + '...' if len(q) > 60 else q for q in queries_for_source]
            print(f"  🔍 [{source_name}] query: {q_short[0] if q_short else 'none'}")
            for q_idx in q_indices:
                if _time_mod.time() > min(_search_deadline, _src_deadline):
                    break
                if q_idx >= len(queries):
                    continue
                if local_count >= call_max:
                    break
                query = queries[q_idx]
                results = timed_search(
                    search_fn, _search_deadline, query,
                    max_results=call_max, label=source_name,
                )
                for item in results:
                    if local_count >= call_max:
                        break
                    dk = _dedup_key(item)
                    if dk and (dk in _seen_snapshot or dk in local_seen):
                        continue
                    if dk:
                        local_seen.add(dk)

                    item_raw = item.get('raw_text') or ''
                    if not item_raw:
                        item_raw = item.get('summary') or ''
                    item_doi = item.get('doi', '') or ''
                    item_id = item.get('id', '') or ''

                    # --- per-source enrichment (same logic as before) ---
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
                    elif source_name == 'springer_nature' and item_doi:
                        try:
                            from literature.core.search import fetch_springer_nature_pdf
                            nat_data = fetch_springer_nature_pdf(item_doi, skip_pdf=True)
                            if nat_data:
                                enriched = f"Title: {nat_data.get('title', '')}\n\nAbstract: {nat_data.get('abstract', '')}"
                                if nat_data.get('full_text'):
                                    enriched += f"\n\nDOI_PAGE_CONTENT:\n{nat_data['full_text'][:20000]}"
                                if len(enriched) > len(item_raw):
                                    item_raw = enriched
                        except Exception:
                            pass
                        if not _has_enrichment(item_raw):
                            try:
                                doi_text = fetch_full_text_by_doi(item_doi)
                                if doi_text:
                                    item_raw += f"\n\nDOI_PAGE_CONTENT:\n{doi_text[:20000]}"
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
                        if not _has_enrichment(item_raw) and item_doi:
                            try:
                                nat_data = fetch_springer_nature_pdf(item_doi, skip_pdf=True)
                                if nat_data and nat_data.get('abstract'):
                                    item_raw = (
                                        f"Title: {nat_data.get('title', '')}"
                                        f"\n\nAbstract: {nat_data.get('abstract', '')}"
                                    )
                            except Exception:
                                pass

                    item['raw_text'] = item_raw
                    local_items.append(item)
                    local_count += 1
            return source_name, local_items, local_seen

        # Launch all sources in parallel
        with ThreadPoolExecutor(max_workers=min(6, len(_unified_sources))) as executor:
            futures = {}
            for source_name, search_fn, call_max, q_indices in _unified_sources:
                if _time_mod.time() > min(_search_deadline, _round_hard_deadline):
                    break
                futures[executor.submit(
                    _search_one_source, source_name, search_fn, call_max, q_indices
                )] = source_name

            for future in as_completed(futures):
                source_name = futures[future]
                try:
                    src_name, local_items, local_seen = future.result(timeout=75)
                    for item in local_items:
                        dk = _dedup_key(item)
                        if dk and dk in seen_keys:
                            continue
                        if dk:
                            seen_keys.add(dk)
                        candidate_items.append(item)
                        source_counts[src_name] += 1
                    print(f"    └─ {len(local_items)} candidate(s) from [{src_name}]")
                except Exception as exc:
                    print(f"    └─ [{source_name}] search failed: {exc}")

        # --- Retry sources that returned 0 candidates ---
        _zero_sources = [(sn, sf, cm, qi) for sn, sf, cm, qi in _unified_sources
                         if source_counts.get(sn, 0) == 0]
        if _zero_sources and _time_mod.time() < _round_hard_deadline:
            print(f"\n  🔄 {len(_zero_sources)} source(s) returned 0 candidates — generating retry queries...")
            # Ask LLM for 1 simplified query per failing source
            _zero_names = [sn for sn, _, _, _ in _zero_sources]
            _retry_prompt = (
                f"Generate **{len(_zero_names)}** simplified, broad single-cell biology "
                f"search queries — one per source listed below. Each query must include "
                f"'single-cell' or 'scRNA-seq' and 'GEO' or 'GSE'. Keep each under "
                f"120 chars. Return ONLY a JSON array of strings.\n"
                f"Sources that need queries: {', '.join(_zero_names)}"
            )
            _retry_raw = _call_llm(_retry_prompt,
                                   system_prompt='You output only valid JSON arrays.',
                                   call_type='retry_queries', llm_model=_MODEL_FLASH,
                                   max_tokens=512)
            _retry_queries = _parse_llm_json(_retry_raw or '[]') if _retry_raw else []
            if isinstance(_retry_queries, list) and _retry_queries:
                for idx, (sn, sf, cm, qi) in enumerate(_zero_sources):
                    if idx >= len(_retry_queries):
                        break
                    _rq = _retry_queries[idx]
                    if not isinstance(_rq, str) or not _rq.strip():
                        continue
                    _rq = _rq.strip()[:150]
                    print(f"  🔄 [{sn}] retry query: {_rq[:80]}")
                    _retry_results = timed_search(sf, _search_deadline, _rq,
                                                  max_results=min(cm, 8), label=f'{sn}_retry')
                    _added = 0
                    for item in (_retry_results or []):
                        dk = _dedup_key(item)
                        if dk and dk in seen_keys:
                            continue
                        if dk:
                            seen_keys.add(dk)
                        item_raw = item.get('raw_text') or item.get('summary') or ''
                        item['raw_text'] = item_raw
                        candidate_items.append(item)
                        source_counts[sn] += 1
                        _added += 1
                    if _added:
                        print(f"    └─ retry added {_added} candidate(s) from [{sn}]")

        if not candidate_items:
            print(f"  ⚠️  Round {round_idx + 1}: no candidates found from any source.")
            continue

        # Check round hard deadline before expensive ranking + extraction
        if _time_mod.time() > _round_hard_deadline:
            print(f"  ⏰ Round hard deadline exceeded, skipping ranking/extraction.")
            continue

        # Rank candidates (with error resilience)
        try:
            ranked_items = llm_rank_articles(candidate_items, benchmark_type)
            if ranked_items:
                candidate_items = ranked_items
        except Exception as exc:
            logger.warning('llm_rank_articles crashed (round %d): %s — using unsorted candidates.', round_idx + 1, exc)
            # continue with unsorted candidates — better than losing the round

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
                # Unpaywall OA full text (catches papers outside PMC)
                if not _has_enrichment(text) and doi:
                    try:
                        unpaywall_text = fetch_unpaywall_text(doi)
                        if unpaywall_text:
                            text += f"\n\nFULL_TEXT:\n{unpaywall_text}"
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
                # Also try Springer Nature PDF download via proxy for SS papers with DOI
                if not _has_enrichment(text) and doi:
                    try:
                        nat_data = fetch_springer_nature_pdf(doi)
                        if nat_data and nat_data.get('full_text'):
                            full = f"Title: {candidate.get('title', '')}\n\nFULL_TEXT:\n{nat_data['full_text']}"
                            if len(full) > len(text):
                                text = full
                    except Exception:
                        pass
                # Try Springer Nature HTML full-text (catches Nature journals + BMC/SpringerOpen)
                if not _has_enrichment(text) and doi:
                    try:
                        sn_html = fetch_springer_nature_fulltext_html(doi)
                        if sn_html and len(sn_html) > 500:
                            text += f"\n\nFULL_TEXT:\n{sn_html}"
                    except Exception:
                        pass
            elif source == 'springer_nature' and doi:
                if not _has_enrichment(text):
                    try:
                        nat_data = fetch_springer_nature_pdf(doi)
                        if nat_data and nat_data.get('full_text'):
                            full = f"Title: {candidate.get('title', '')}\n\nFULL_TEXT:\n{nat_data['full_text']}"
                            if len(full) > len(text):
                                text = full
                    except Exception:
                        pass
                # Try Springer Nature dedicated HTML fetch (always try - provides better full text)
                try:
                    sn_html = fetch_springer_nature_fulltext_html(doi)
                    if sn_html and len(sn_html) > 500:
                        # Move SUPPLEMENTARY_SECTIONS to the front so they fit within the
                        # 16000-char limit of llm_extract_paper_details. Data/Code availability
                        # sections are at positions 60000+ in the raw body text, but the LLM
                        # extractor only sees the first 16000 chars. By pulling these critical
                        # sections forward, the LLM can find GSE/GitHub/DOI references.
                        _supp_marker = '\n\nSUPPLEMENTARY_SECTIONS:\n'
                        _supp_idx = sn_html.find(_supp_marker)
                        if _supp_idx >= 0:
                            _supp_part = sn_html[_supp_idx + len(_supp_marker):]
                            _main_part = sn_html[:_supp_idx]
                            text += f"\n\nKEY_SECTIONS:\n{_supp_part}\n\nFULL_TEXT:\n{_main_part}"
                        else:
                            text += f"\n\nFULL_TEXT:\n{sn_html}"
                except Exception as exc:
                    logger.warning('Springer Nature HTML fetch failed for %s: %s', doi, exc)
                if not _has_enrichment(text):
                    try:
                        doi_text = fetch_full_text_by_doi(doi)
                        if doi_text:
                            text += f"\n\nDOI_PAGE_CONTENT:\n{doi_text[:80000]}"
                    except Exception:
                        pass
            elif source == 'arxiv' and candidate_id:
                # arXiv has its own full-text via PDF download
                if not _has_enrichment(text):
                    try:
                        arxiv_data = fetch_arxiv_article(candidate_id)
                        if arxiv_data:
                            full = f"Title: {arxiv_data.get('title', '')}\n\nAbstract: {arxiv_data.get('abstract', '')}"
                            ft = arxiv_data.get('full_text', '')
                            if ft:
                                full += f"\n\nFULL_TEXT:\n{ft}"
                            if len(full) > len(text):
                                text = full
                    except Exception:
                        pass
                # Also try DOI page if arXiv has a DOI
                if not _has_enrichment(text) and doi:
                    try:
                        doi_text = fetch_full_text_by_doi(doi)
                        if doi_text:
                            text += f"\n\nDOI_PAGE_CONTENT:\n{doi_text[:80000]}"
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

            # Quick regex pre-filter: check for obvious GSE/SRA/Zenodo before LLM
            _raw = text.lower()
            _has_gse_hint = bool(re.search(r'gse\d{4,}', _raw))
            _has_sra_hint = bool(re.search(r'(srp|prjna|prjeb|srr|err|drp)\d{4,}', _raw))
            _has_gh_hint = bool(re.search(r'github\.com/[\w.-]+/[\w.-]+', _raw))
            _has_zen_hint = bool(re.search(r'zenodo\.\d+|10\.5281/zenodo', _raw))
            _has_cellx_hint = bool(re.search(r'cellxgene|cz cellxgene', _raw))
            _has_array_hint = bool(re.search(r'e-(?:mtab|geod|mexp|tabm)-\d{4,}', _raw))
            _has_any_hint = _has_gse_hint or _has_sra_hint or _has_gh_hint or _has_zen_hint or _has_cellx_hint or _has_array_hint

            # Skip if text is too short (likely a failed fetch — redirect page)
            if len(text.split()) < 50:
                continue

            # Check for review/survey keywords (reviews never have original deposited data)
            _first_3k = _raw[:3000]
            _is_review = bool(re.search(
                r'\b(review|survey|perspective|opinion|overview|mini.?review)\b',
                _first_3k
            ))

            # Check if this mentions single-cell technology at all
            _has_sc_mention = bool(re.search(
                r'single.?cell|sc(?:rna|seq|atac)|sn(?:rna|seq)|single.?nucleus|10x\s*genomics|'
                r'scrna.?seq|scc|single.?cell.?omics|scmulti.?omics',
                _first_3k
            ))

            # Check for technologies that are NOT scRNA-seq (likely irrelevant)
            _is_wrong_tech = bool(re.search(
                r'\b(dna methylation|microarray|proteom(?:ics|ic)|mass.?spectrom|western blot|'
                r'chip.?seq(?!\s*arch)|epigenom|seir\s+model|herd immunity|'
                r'calcium imaging|confocal)\b',
                _first_3k
            ))

            # Skip LLM extraction when clearly irrelevant AND no data hints
            _is_method = any(kw in _raw[:2000] for kw in [
                'we propose', 'we introduce', 'our method', 'novel algorithm',
                'state-of-the-art performance', 'our framework',
            ])

            if not _has_any_hint:
                # Method papers with no data → useless
                if _is_method:
                    continue
                # Pure reviews with no data → useless
                if _is_review:
                    continue
                # Wrong technology + no sc mention + no data → irrelevant
                if _is_wrong_tech and not _has_sc_mention:
                    continue
                # No single-cell mention at all AND no data hints → very likely irrelevant
                if not _has_sc_mention:
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

            # --- Detect method-paper Zenodo overlap ---
            # When the SAME Zenodo DOI is in both data and code lists,
            # and there's no other evidence (GSE/SRA/cellxgene/GitHub/Figshare),
            # the paper is likely a method/tool paper, not a data paper.
            # Downgrade: don't count Zenodo as "data" unless first_hand_data or high relevance.
            _zen_overlap = set(zenodo_data) & set(zenodo_code)
            _has_other_data = bool(gse or sra or cellxgene)
            _has_other_code = bool(github_repos or figshare_links)
            if not _has_other_data and not _has_other_code and _zen_overlap and len(_zen_overlap) == len(zenodo_data):
                # All data comes from Zenodo, and all Zenodo data is also in code
                _first_hand = llm_data.get('first_hand_data')
                if isinstance(_first_hand, str):
                    _first_hand = _first_hand.lower() in ('true', 'yes', '1')
                if not _first_hand and relevance < 7:
                    has_data = False  # downgrade: Zenodo overlap is likely tool/data not real dataset

            # --- Annotation-specific gate: require per-cell cell-type labels ---
            # For cell-type annotation benchmarks the data must carry ground-truth
            # labels. If the LLM explicitly says has_cell_type_labels=false, the
            # dataset is unusable for annotation regardless of GSE/code presence.
            _has_labels = llm_data.get('has_cell_type_labels')
            if isinstance(_has_labels, str):
                _has_labels = _has_labels.lower() in ('true', 'yes', '1')
            _annot_gate_blocked = False
            if benchmark_type == 'annotation' and _has_labels is False and has_data:
                _annot_gate_blocked = True
                acceptance = 'REJECTED'

            # --- Tiered acceptance ---
            if not _annot_gate_blocked:
                if has_data and has_code:
                    acceptance = 'FULLY_ACCEPTED'
                elif has_data and not has_code:
                    acceptance = 'DATA_ONLY'
                elif not has_data and has_code:
                    acceptance = 'CODE_ONLY'
                else:
                    # No explicit accessions — check LLM's first_hand_data signal
                    _first_hand = llm_data.get('first_hand_data')
                    if isinstance(_first_hand, str):
                        _first_hand = _first_hand.lower() in ('true', 'yes', '1')
                    if _first_hand and relevance >= 7:
                        acceptance = 'DATA_ONLY'
                        # Mark inferred — accessions likely in supplementary/full text
                        if not gse:
                            gse = ['INFERRED_DATA']
                    else:
                        acceptance = 'REJECTED'

            skip_reason_parts = []
            if _annot_gate_blocked:
                skip_reason_parts.append('annotation benchmark: dataset has no per-cell cell-type labels (has_cell_type_labels=false)')
            if not has_data:
                skip_reason_parts.append('no dataset accessions (GSE/SRA/cellxgene/Zenodo data) found')
            if not has_code:
                skip_reason_parts.append('no code repositories (GitHub/Zenodo code/Figshare) found')
            skip_reason = '; '.join(skip_reason_parts) if skip_reason_parts else 'none'
            if acceptance == 'DATA_ONLY' and 'INFERRED_DATA' in gse:
                skip_reason += ' (inferred from first_hand_data + high relevance score)'

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
                'other_code_urls': llm_data.get('other_code_urls', []),
                'relevance_score': relevance,
                'organism': organism,
                'tissue': tissue,
                'technology': technology,
                'reason': llm_data.get('reason', ''),
                'methods_summary': llm_data.get('methods_summary', ''),
                # Keep the full text for downstream deep extraction (experimental_protocol.json)
                'raw_text': text,
                'full_text': text,
            }
            round_results.append(result_entry)

        # --- Round completion: recovery + merge ---
        _recover_almost_accepted(round_results)
        all_results.extend(round_results)

        n_fully = sum(1 for r in all_results if r.get('acceptance') == 'FULLY_ACCEPTED')
        n_data = sum(1 for r in all_results if r.get('acceptance') == 'DATA_ONLY')
        n_code = sum(1 for r in all_results if r.get('acceptance') == 'CODE_ONLY')
        _round_elapsed = _time_mod.time() - _round_start
        print(
            f"  ✅ Round {round_idx + 1} done in {_round_elapsed:.0f}s — "
            f"{len(all_results)} total: "
            f"{n_fully} FULLY_ACCEPTED, {n_data} DATA_ONLY, {n_code} CODE_ONLY"
        )
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

    n_fully = sum(1 for r in all_results if r.get('acceptance') == 'FULLY_ACCEPTED')
    n_data = sum(1 for r in all_results if r.get('acceptance') == 'DATA_ONLY')
    n_code = sum(1 for r in all_results if r.get('acceptance') == 'CODE_ONLY')
    n_rejected = sum(1 for r in all_results if r.get('acceptance') == 'REJECTED')
    print(
        f"\n  ════════════════════════════════════\n"
        f"  📊 Literature collection complete!\n"
        f"     Total: {len(all_results)} candidates\n"
        f"     {n_fully} FULLY_ACCEPTED · {n_data} DATA_ONLY · {n_code} CODE_ONLY · {n_rejected} REJECTED\n"
        f"  ════════════════════════════════════\n"
    )

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


def llm_extract_experimental_protocol(
    paper_text: str,
    benchmark_type: str,
) -> Optional[Dict[str, Any]]:
    """Deep-read a FULLY_ACCEPTED paper to extract its experimental protocol.

    Unlike ``llm_extract_paper_details`` which focuses on dataset accessions,
    this function extracts the step-by-step analysis pipeline, software
    versions, key parameters, and expected outputs.  The result is saved as
    ``experimental_protocol.json`` in the paper's ``benchmark_data`` folder
    and reused by Stage 2 (reproduce) and Stage 4 (benchmark evaluation) to
    avoid re-reading the full paper text each time.

    Returns a dict with keys:
        overview, data_generation, processing_pipeline (list of dicts),
        key_parameters, software_versions, expected_outputs, code_dispatch
    """
    # ── Smart truncation: prioritise the Methods & Results sections ──
    # The paper text can be very long (80K+ chars). Instead of blindly
    # taking the first 16000 chars, we try to find the Methods section
    # and include its full content + a tail section for Results.
    max_chars = 24000
    truncated = paper_text[:max_chars]
    methods_idx = paper_text.lower().find('methods')
    if methods_idx >= 0:
        # Take from Methods onward, capped at max_chars
        truncated = paper_text[methods_idx:methods_idx + max_chars]
        # Also include the first 2000 chars (title + abstract) for context
        truncated = paper_text[:2000] + '\n[--- Abstract truncated ---]\n' + truncated
    paper_excerpt = truncated[:max_chars]

    prompt = (
        f"You are an expert in extracting computational biology protocols from "
        f"scientific papers.  Read the following paper text and extract a "
        f"detailed, structured experimental & analysis protocol suitable for "
        f"reproducing the {benchmark_type} analysis.\n\n"
        f"Paper text (may be truncated at ~{max_chars} chars):\n{paper_excerpt}\n\n"
        f"Return a JSON object with these keys:\n\n"
        f'{{\n'
        f'  "overview": "one-sentence summary of the experimental design and goal",\n'
        f'  "data_generation": {{\n'
        f'    "samples": ["sample_1 description (including sample size, conditions)"],\n'
        f'    "platform": "10x Genomics 3\' v3 / Smart-seq2 / 10x Visium / ...",\n'
        f'    "sequencing": "NovaSeq 6000, PE150 / ...",\n'
        f'    "quality_control": "Cell Ranger v7.1, filtering criteria, doublet removal..."\n'
        f'  }},\n'
        f'  "processing_pipeline": [\n'
        f'    {{\n'
        f'      "step": 1, "step_name": "Quality control",\n'
        f'      "tool": "Seurat::CreateSeuratObject / scanpy.pp.filter_cells",\n'
        f'      "version": "5.0.1 / 1.9.3",\n'
        f'      "input": "raw count matrix / .mtx / .h5ad",\n'
        f'      "output": "filtered AnnData / Seurat object",\n'
        f'      "action": "Filter cells with <200 genes, >20% mitochondrial reads"\n'
        f'    }},\n'
        f'    {{\n'
        f'      "step": 2, "step_name": "Normalization",\n'
        f'      "tool": "...", "version": "...",\n'
        f'      "input": "...", "output": "...", "action": "..."\n'
        f'    }},\n'
        f'    ...\n'
        f'  ],\n'
        f'  "key_parameters": {{\n'
        f'    "filtering": "min_genes=200, max_mt=20%, min_cells=3",\n'
        f'    "normalization": "SCTransform / log-normalize target_sum=10000",\n'
        f'    "integration": "Harmony on \'sample\' covariate, dims=1:30",\n'
        f'    "clustering": "resolution=0.8, dims=1:30, algorithm=Leiden",\n'
        f'    "differential_expression": "MAST / Wilcoxon / DESeq2, log2FC>0.25, p_adj<0.05",\n'
        f'    "cell_annotation": "SingleR / manual marker genes / Azimuth reference mapping"\n'
        f'  }},\n'
        f'  "software_versions": {{\n'
        f'    "R": "4.3.0", "Python": "3.10", "Seurat": "5.0.1", "scanpy": "1.9.3",\n'
        f'    "Harmony": "0.1.1", "scVI": "1.0.4", "...": "..."\n'
        f'  }},\n'
        f'  "expected_outputs": [\n'
        f'    "UMAP embedding with batch-corrected coordinates",\n'
        f'    "DEG list per cell type between conditions",\n'
        f'    "Clustering labels for ~N cells"\n'
        f'  ],\n'
        f'  "code_dispatch": {{\n'
        f'    "entry_point": "main.R / run_pipeline.py / ...",\n'
        f'    "key_scripts": ["script1.R", "script2.py"],\n'
        f'    "expected_runtime": "2-4 hours on GPU / ...",\n'
        f'    "hardware_notes": "GPU recommended for scVI training"\n'
        f'  }}\n'
        f'}}\n\n'
        f'Rules:\n'
        f'- Extract ONLY information explicitly stated in the paper text.\n'
        f'- If a tool/workflow is mentioned but its version is not given,\n'
        f'  set version="" (do NOT guess).\n'
        f'- If a section is completely absent, use "" for string fields,\n'
        f'  [] for arrays, {{}} for objects.\n'
        f'- The "processing_pipeline" should list steps in order (step 1, 2, ...).\n'
        f'- Focus on the single-cell processing and {benchmark_type}-relevant steps.\n'
        f'- Return ONLY valid JSON, no markdown wrappers, no explanation.'
    )

    raw = _call_llm(
        prompt,
        system_prompt="You are a skilled scientific protocol extractor. Output ONLY valid JSON.",
        temperature=0.15,
        call_type='extract_experimental_protocol',
        llm_model=_MODEL_FLASH,
    )
    if not raw:
        return None

    result = _parse_llm_json(raw)
    _audit_record_parsed(result)
    if not isinstance(result, dict):
        return None
    return result

