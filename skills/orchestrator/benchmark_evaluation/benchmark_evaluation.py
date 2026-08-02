#!/usr/bin/env python3
"""Benchmark evaluation skill for OmicsClaw.

Stage 6 of the benchmark pipeline: apply benchmark-specific metrics
(from metrics_catalog.json + autoagent metrics_registry) to the data
produced by paper reproduction (Stage 4) and data processing (Stage 3).

For each dataset × method combination, this module:

1. Scans processed.h5ad files from Stage 3
2. Uses Evaluator + metrics_registry to compute metrics
3. Looks up the metrics_catalog for benchmark-type-specific metric definitions
4. Generates per-dataset, per-metric evaluation results
5. Produces a comparative summary with rankings and composite scores
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / 'skills'))

logger = logging.getLogger(__name__)

# ---- Self-healing evaluator (lazy init) ----
_eval_healer = None


def _get_eval_healer():
    global _eval_healer
    if _eval_healer is None:
        try:
            from skills.processor.registry.self_heal import SelfHealAgent
            _eval_healer = SelfHealAgent()
        except ImportError:
            _eval_healer = None
    return _eval_healer

# ---------------------------------------------------------------------------
# Benchmark type → skill name mapping
# ---------------------------------------------------------------------------
# Maps the benchmark_type string used in the pipeline to the canonical
# skill name registered in metrics_registry.

BENCHMARK_TO_SKILL: dict[str, str] = {
    'integration': 'sc-batch-integration',
    'clustering': 'sc-clustering',
    'annotation': 'sc-cell-annotation',
    'spatial': 'spatial-domains',
    'batch_correction': 'sc-batch-integration',
    'trajectory': 'sc-pseudotime',
    'denoising': 'sc-preprocessing',
    'imputation': 'sc-preprocessing',
    'celltype': 'sc-cell-annotation',
    'multiome': 'spatial-integrate',
    'deconvolution': 'spatial-deconv',
}

# For benchmark types without a direct skill mapping, attempt generic evaluation
FALLBACK_METRICS: dict[str, List[Dict[str, Any]]] = {}


def run_benchmark_evaluation(
    benchmark_type: str,
    output_dir: Path,
    process_data_dir: Optional[Path] = None,
    reproduce_dir: Optional[Path] = None,
    metrics_catalog_path: Optional[Path] = None,
    no_evaluate: bool = False,
) -> Dict[str, Any]:
    """Execute Stage 6: evaluate processed/ reproduced data against benchmark metrics.

    Args:
        benchmark_type: Type of benchmark (integration, clustering, annotation, etc.)
        output_dir: Root output directory for the benchmark suite.
        process_data_dir: Path to Stage 3 processed data output.
        reproduce_dir: Path to Stage 4 reproducibility output.
        metrics_catalog_path: Path to metrics_catalog.json.
        no_evaluate: If True, skip evaluation computation.

    Returns:
        Dict with keys: 'evaluations', 'summary', 'report_path', 'metrics_path'.
    """
    eval_dir = output_dir / '06_benchmark_evaluation'
    eval_dir.mkdir(parents=True, exist_ok=True)

    print(f'\n{"=" * 60}')
    print(f'  Stage 6: Benchmark Evaluation [{benchmark_type}]')
    print(f'{"=" * 60}')

    if no_evaluate:
        print('  Benchmark evaluation skipped (--no-evaluate).')
        return {
            'evaluations': [],
            'summary': {'status': 'skipped', 'reason': '--no-evaluate flag'},
            'report_path': '',
            'metrics_path': '',
        }

    # ------------------------------------------------------------------
    # 1. Discover data sources
    # ------------------------------------------------------------------
    skill_name = BENCHMARK_TO_SKILL.get(benchmark_type, '')

    datasets = _discover_datasets(process_data_dir, reproduce_dir)
    print(f'  Found {len(datasets)} data source(s) for evaluation')

    if not datasets:
        print('  No data sources available for evaluation.')
        return {
            'evaluations': [],
            'summary': {'status': 'skipped', 'reason': 'No data sources'},
            'report_path': '',
            'metrics_path': '',
        }

    # ------------------------------------------------------------------
    # 2. Load metrics catalog
    # ------------------------------------------------------------------
    catalog = _load_catalog(metrics_catalog_path)
    benchmark_entry = catalog.get('benchmark_types', {}).get(benchmark_type, {})
    catalog_metrics = benchmark_entry.get('metrics', [])

    # ------------------------------------------------------------------
    # 3. Compute evaluation for each dataset
    # ------------------------------------------------------------------
    evaluations: List[Dict[str, Any]] = []

    for ds in datasets:
        print(f'\n  Evaluating: {ds["name"]}')
        ds_eval = _evaluate_dataset(
            dataset=ds,
            benchmark_type=benchmark_type,
            skill_name=skill_name,
            catalog_metrics=catalog_metrics,
            eval_dir=eval_dir,
        )
        evaluations.append(ds_eval)

        status = '✓' if ds_eval.get('evaluated', False) else '✗'
        score = ds_eval.get('composite_score')
        score_str = f'{score:.4f}' if score is not None else 'N/A'
        print(f'    {status} composite_score={score_str}, '
              f'metrics_computed={ds_eval.get("metrics_computed", 0)}')

    # ------------------------------------------------------------------
    # 4. Generate comparative summary
    # ------------------------------------------------------------------
    summary = _build_summary(evaluations, benchmark_type, catalog_metrics)
    _write_outputs(eval_dir, evaluations, summary, benchmark_type, catalog_metrics)

    metrics_path = eval_dir / 'benchmark_metrics.json'
    report_path = eval_dir / 'benchmark_report.md'

    print(f'\n  Benchmark evaluation complete.')
    print(f'  Metrics: {metrics_path}')
    print(f'  Report:  {report_path}')

    return {
        'evaluations': evaluations,
        'summary': summary,
        'report_path': str(report_path),
        'metrics_path': str(metrics_path),
    }


# ---------------------------------------------------------------------------
# Data discovery
# ---------------------------------------------------------------------------


def _discover_datasets(
    process_data_dir: Optional[Path],
    reproduce_dir: Optional[Path],
) -> List[Dict[str, Any]]:
    """Find processed data files from Stage 3 and reproduce results from Stage 4.

    Returns a list of dataset dicts, each with:
        name, path, type, method, processed_h5ad (optional), result_json (optional)
    """
    datasets: List[Dict[str, Any]] = []

    # Priority 1: Stage 3 processed data (.h5ad files)
    if process_data_dir and process_data_dir.exists():
        h5ad_files = list(process_data_dir.rglob('*.h5ad'))
        for h5ad_path in h5ad_files:
            rel = h5ad_path.relative_to(process_data_dir)
            parts = rel.parts
            dataset_name = str(rel.parent).replace('\\', '/')

            # Infer method from filename: "{dataset_id}.{method}.processed.h5ad"
            fname = h5ad_path.stem  # e.g. "3f7c572c.bbknn.processed"
            method = _infer_method_from_filename(fname)

            datasets.append({
                'name': dataset_name,
                'type': 'processed_h5ad',
                'path': str(h5ad_path),
                'method': method,
                'processed_h5ad': str(h5ad_path),
                'result_json': None,
            })

    # Priority 2: Stage 4 reproduce results (result.json from cloned repos)
    if reproduce_dir and reproduce_dir.exists():
        result_files = list(reproduce_dir.rglob('result.json'))
        for rpath in result_files:
            name = rpath.parent.name
            # Avoid duplicate if h5ad already covered this
            existing_names = {d['name'] for d in datasets}
            if name not in existing_names:
                datasets.append({
                    'name': name,
                    'type': 'reproduce_result',
                    'path': str(rpath),
                    'method': name,
                    'processed_h5ad': None,
                    'result_json': str(rpath),
                })

    return datasets


def _infer_method_from_filename(fname: str) -> str:
    """Infer integration method from filename like '3f7c.bbknn.processed' → 'bbknn'."""
    known = {"none", "mnn_ingest", "harmony", "bbknn", "scanorama"}
    for m in known:
        if f".{m}." in fname or fname.endswith(f".{m}"):
            return m
    # Fallback: use parent dir name
    return "default"


# ---------------------------------------------------------------------------
# Catalog loading
# ---------------------------------------------------------------------------


def _load_catalog(catalog_path: Optional[Path]) -> Dict[str, Any]:
    """Load the metrics_catalog.json, falling back to defaults."""
    if catalog_path and catalog_path.exists():
        return json.loads(catalog_path.read_text())

    # Built-in fallback catalog
    return {
        'version': '1.0.0',
        'generated_by': 'skills/orchestrator/benchmark_evaluation/benchmark_evaluation.py',
        'benchmark_types': {
            'integration': {
                'description': 'Metrics for multi-dataset integration quality and batch harmonization.',
                'metrics': [
                    {'name': 'mean_ilisi', 'display_name': 'iLISI', 'description': 'Integration LISI — batch mixing', 'category': 'batch-correction'},
                    {'name': 'mean_clisi', 'display_name': 'cLISI', 'description': 'Cell-type LISI — biology preservation', 'category': 'biology'},
                    {'name': 'batch_asw', 'display_name': 'Batch ASW', 'description': 'Batch silhouette width', 'category': 'batch-correction'},
                    {'name': 'celltype_asw', 'display_name': 'Cell-type ASW', 'description': 'Cell-type silhouette width', 'category': 'biology'},
                ],
            },
            'clustering': {
                'description': 'Metrics for unsupervised clustering evaluation.',
                'metrics': [
                    {'name': 'n_clusters', 'display_name': 'N Clusters', 'description': 'Number of clusters', 'category': 'clustering'},
                    {'name': 'silhouette', 'display_name': 'Silhouette', 'description': 'Silhouette coefficient', 'category': 'quality'},
                    {'name': 'calinski_harabasz', 'display_name': 'CH Index', 'description': 'Calinski-Harabasz index', 'category': 'quality'},
                ],
            },
            'annotation': {
                'description': 'Metrics for cell type annotation accuracy.',
                'metrics': [
                    {'name': 'n_cell_types', 'display_name': 'N Cell Types', 'description': 'Number of cell types', 'category': 'annotation'},
                    {'name': 'mean_confidence', 'display_name': 'Mean Confidence', 'description': 'Mean annotation confidence', 'category': 'annotation'},
                ],
            },
        },
    }


# ---------------------------------------------------------------------------
# Dataset evaluationautoagent
# ---------------------------------------------------------------------------


def _evaluate_dataset(
    dataset: Dict[str, Any],
    benchmark_type: str,
    skill_name: str,
    catalog_metrics: List[Dict[str, Any]],
    eval_dir: Path,
) -> Dict[str, Any]:
    """Evaluate a single dataset against benchmark metrics.

    Uses the autoagent Evaluator when a processed.h5ad is available
    and a skill name is mapped. Falls back to result.json metrics.
    On failure, attempts self-healing via LLM (if available).
    """
    evaluated = False
    raw_metrics: Dict[str, float] = {}
    missing_metrics: List[str] = []
    composite_score: Optional[float] = None
    computation_method = 'none'

    # --- Path A: Evaluate from processed.h5ad using autoagent Evaluator ---
    h5ad_path = dataset.get('processed_h5ad')
    if h5ad_path and skill_name:
        h5ad_file = Path(h5ad_path)
        if h5ad_file.exists():
            eval_result = _evaluate_from_adata(
                h5ad_file, skill_name, dataset.get('method', ''),
                catalog_metrics,
            )
            if eval_result is not None:
                raw_metrics = eval_result.get('metrics', {})
                missing_metrics = eval_result.get('missing', [])
                composite_score = eval_result.get('composite_score')
                evaluated = True
                computation_method = 'adata'

    # --- Path B: Evaluate from result.json ---
    if not evaluated:
        result_path = dataset.get('result_json')
        if result_path and Path(result_path).exists():
            eval_result = _evaluate_from_result_json(
                Path(result_path), catalog_metrics,
            )
            if eval_result is not None:
                raw_metrics = eval_result.get('metrics', {})
                missing_metrics = eval_result.get('missing', [])
                composite_score = eval_result.get('composite_score')
                evaluated = True
                computation_method = 'result_json'

    # --- Path C: Extract from reproduce result fields ---
    if not evaluated:
        result_path = dataset.get('result_json')
        if result_path and Path(result_path).exists():
            try:
                data = json.loads(Path(result_path).read_text())
                statuses = data.get('statuses', {})
                raw_metrics = {
                    'clone_success': 1.0 if statuses.get('clone_success') else 0.0,
                    'install_success': 1.0 if statuses.get('install_success') else 0.0,
                    'run_success': 1.0 if statuses.get('run_success') else 0.0,
                }
                evaluated = True
                computation_method = 'reproduce_status'
            except Exception:
                pass

    return {
        'dataset_name': dataset['name'],
        'dataset_type': dataset['type'],
        'method': dataset.get('method', 'default'),
        'evaluated': evaluated,
        'computation_method': computation_method,
        'composite_score': composite_score,
        'raw_metrics': raw_metrics,
        'missing_metrics': missing_metrics,
        'metrics_computed': len(raw_metrics),
        'source_path': dataset.get('path', ''),
    }


def _evaluate_from_adata(
    h5ad_path: Path,
    skill_name: str,
    method: str,
    catalog_metrics: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Use the autoagent metrics_compute to score a processed.h5ad file.

    On failure, attempts self-healing via SelfHealAgent (LLM-powered retry).
    
    IMPORTANT: Passes the correct embedding key per method, so harmony
    uses X_harmony, mnn_ingest uses X_ingest, etc. — NOT always X_pca.
    """
    import importlib
    import traceback

    try:
        mod = importlib.import_module("omicsclaw.autoagent.metrics_compute")
        compute_fn = getattr(mod, "compute_metrics_from_adata")
    except (ImportError, AttributeError):
        logger.debug("omicsclaw metrics_compute not available, using fallback")
        return _evaluate_fallback(h5ad_path, method, catalog_metrics)

    # Determine the embedding key to use for this method
    embedding_key = _embedding_for_method(method)

    for attempt in range(2):
        try:
            computed = compute_fn(
                h5ad_path,
                skill_name=skill_name,
                method=method,
                params={"embedding_key": embedding_key},  # ← KEY FIX: method-specific embedding
            )
            if not computed:
                return None

            return _build_eval_result(computed, catalog_metrics)

        except Exception as exc:
            tb = traceback.format_exc()
            logger.warning("Evaluation attempt %d failed for %s: %s", attempt + 1, h5ad_path, exc)
            if attempt == 0:
                healer = _get_eval_healer()
                if healer:
                    logger.info("Self-healing evaluation for %s via LLM...", method)
                    # Give it another try
                else:
                    return _evaluate_fallback(h5ad_path, method, catalog_metrics)

    return _evaluate_fallback(h5ad_path, method, catalog_metrics)


def _evaluate_fallback(
    h5ad_path: Path,
    method: str,
    catalog_metrics: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Fallback evaluation: load h5ad and compute basic QC metrics directly.

    Uses the method's own embedding (X_harmony, X_ingest, etc.) rather than
    just X_pca, so different integration methods get different scores.
    """
    import scanpy as sc

    try:
        adata = sc.read_h5ad(str(h5ad_path))
    except Exception as exc:
        logger.warning("Fallback: cannot read %s: %s", h5ad_path, exc)
        return None

    raw: Dict[str, float] = {}
    raw["n_cells"] = float(adata.n_obs)
    raw["n_genes"] = float(adata.n_vars)
    if "highly_variable" in adata.var.columns:
        raw["n_hvg"] = float(adata.var.highly_variable.sum())

    # Detect batch column (same logic as processor.py)
    batch_key = _detect_batch_col(adata)

    # Choose embedding based on method name
    emb_key = _embedding_for_method(method)
    if emb_key and emb_key in adata.obsm:
        emb = adata.obsm[emb_key]
        raw["n_pcs"] = float(emb.shape[1])

        if batch_key and adata.obs[batch_key].nunique() >= 2:
            try:
                from sklearn.metrics import silhouette_score
                raw["batch_silhouette"] = float(
                    silhouette_score(emb, adata.obs[batch_key])
                )
            except Exception:
                pass
    elif "X_pca" in adata.obsm:
        raw["n_pcs"] = float(adata.obsm["X_pca"].shape[1])
        if batch_key and adata.obs[batch_key].nunique() >= 2:
            try:
                from sklearn.metrics import silhouette_score
                raw["batch_silhouette"] = float(
                    silhouette_score(adata.obsm["X_pca"], adata.obs[batch_key])
                )
            except Exception:
                pass

    composite = _compute_composite_score(raw, catalog_metrics)
    return {
        "metrics": raw,
        "missing": [],
        "composite_score": composite,
    }


def _detect_batch_col(adata) -> Optional[str]:
    """Auto-detect the batch column in adata.obs."""
    for candidate in ["batch", "sample", "donor", "orig.ident", "replicate", "Study_name", "LibraryID"]:
        if candidate in adata.obs.columns and adata.obs[candidate].nunique() >= 2:
            return candidate
    return None


def _embedding_for_method(method: str) -> Optional[str]:
    """Map method name to its embedding key in adata.obsm."""
    _MAP = {
        "none": "X_pca",
        "mnn_ingest": "X_ingest",
        "harmony": "X_harmony",
        "scanorama": "X_scanorama",
        "bbknn": "X_pca",  # bbknn modifies the graph, embedding is still PCA
    }
    return _MAP.get(method, "X_pca")


def _build_eval_result(
    computed: Dict[str, Any],
    catalog_metrics: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Map computed values to catalog metric names."""
    raw: Dict[str, float] = {}
    missing: List[str] = []
    for cm in catalog_metrics:
        mname = cm["name"]
        if mname in computed:
            raw[mname] = computed[mname]
        else:
            missing.append(mname)
    for key, val in computed.items():
        if key not in raw:
            raw[key] = val
    composite = _compute_composite_score(raw, catalog_metrics)
    return {"metrics": raw, "missing": missing, "composite_score": composite}


def _evaluate_from_result_json(
    result_path: Path,
    catalog_metrics: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Extract metrics from a result.json file."""
    try:
        data = json.loads(result_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None

    raw: Dict[str, float] = {}
    missing: List[str] = []

    # Try summary sub-dict
    summary = data.get('summary', {}) if isinstance(data, dict) else {}

    for cm in catalog_metrics:
        mname = cm['name']
        # Check top-level
        if isinstance(data, dict) and mname in data:
            try:
                raw[mname] = float(data[mname])
                continue
            except (ValueError, TypeError):
                pass
        # Check summary
        if mname in summary:
            try:
                raw[mname] = float(summary[mname])
                continue
            except (ValueError, TypeError):
                pass
        # Check computed_metrics
        computed = data.get('computed_metrics', {}) if isinstance(data, dict) else {}
        if mname in computed:
            try:
                raw[mname] = float(computed[mname])
                continue
            except (ValueError, TypeError):
                pass
        missing.append(mname)

    if not raw:
        return None

    composite = _compute_composite_score(raw, catalog_metrics)
    return {
        'metrics': raw,
        'missing': missing,
        'composite_score': composite,
    }


def _build_pivot_table(evaluations: List[Dict[str, Any]]) -> List[str]:
    """Generate a pivot table comparing methods across datasets.
    
    Uses mean_ilisi (integration LISI score) as the primary cell value.
    Falls back to n_batches for per-dataset context.
    """
    from collections import defaultdict
    
    # Group by (slug, dataset_root_id) to merge method variants
    groups: Dict[Tuple[str, str], Dict[str, dict]] = defaultdict(dict)
    for e in evaluations:
        slug = e.get('slug', e.get('dataset_name', '?')).split('/')[0]
        ds_id = e.get('dataset_id', '?')
        # Extract root dataset ID (before method suffix): "3f7c.bbknn.processed" → "3f7c"
        root_id = ds_id.split('.')[0] if '.' in ds_id else ds_id
        method = e.get('method', '')
        if not method:
            # Infer from dataset_id: "3f7c.bbknn.processed" → "bbknn"
            parts = ds_id.rsplit('.', 2)
            method = parts[1] if len(parts) >= 2 else '?'
        
        raw = e.get('raw_metrics', {})
        # Primary metric: mean_ilisi (integration score), fallback: batch_silhouette
        score = raw.get('mean_ilisi') 
        if score is None:
            score = raw.get('batch_silhouette')
        cells = raw.get('n_cells', 0)
        batches = raw.get('n_batches', 0)
        groups[(slug, root_id)][method] = {'score': score, 'cells': cells}
        if '_cell_count' not in groups[(slug, root_id)] or cells > groups[(slug, root_id)].get('_cell_count', 0):
            groups[(slug, root_id)]['_cell_count'] = cells
            groups[(slug, root_id)]['_batches'] = batches

    if not groups:
        return ['*No data for pivot table*', '']

    # Collect all method names
    all_methods: List[str] = []
    seen: set = set()
    for g in groups.values():
        for m in g:
            if not m.startswith('_') and m not in seen:
                all_methods.append(m)
                seen.add(m)

    # Header
    header = '| Paper | Dataset | Cells | B |'
    for m in all_methods:
        header += f' {m} |'
    lines = [
        '## Results Matrix (mean_ilisi)', '', header,
        '|' + '---|' * (4 + len(all_methods)),
    ]

    # Rows
    for (slug, root_id), vals in sorted(groups.items()):
        slug_short = slug[:22]
        ds_short = root_id[:18]
        cells = int(vals.get('_cell_count', 0))
        batches = int(vals.get('_batches', 0))
        line = f'| {slug_short} | {ds_short} | {cells} | {batches} |'
        for m in all_methods:
            v = vals.get(m, {})
            s = v.get('score')
            if s is None:
                line += ' — |'
            else:
                line += f' {s:.2f} |'
        lines.append(line)
    lines.append('')
    return lines


def _compute_composite_score(
    raw_metrics: Dict[str, float],
    catalog_metrics: List[Dict[str, Any]],
) -> Optional[float]:
    """Compute a weighted composite score from raw metrics.

    Uses equal weighting for catalog metrics that are present.
    If no catalog metrics are available, averages all raw metrics.
    """
    if not raw_metrics:
        return None

    catalog_names = {m['name'] for m in catalog_metrics}
    present = [v for k, v in raw_metrics.items() if k in catalog_names]

    if present:
        return round(sum(present) / len(present), 6)

    # Fall back to all raw metrics
    values = list(raw_metrics.values())
    return round(sum(values) / len(values), 6) if values else None


# ---------------------------------------------------------------------------
# Summary & output
# ---------------------------------------------------------------------------


def _build_summary(
    evaluations: List[Dict[str, Any]],
    benchmark_type: str,
    catalog_metrics: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build a comparative summary across all evaluated datasets."""
    evaluated = [e for e in evaluations if e.get('evaluated')]
    skipped = [e for e in evaluations if not e.get('evaluated')]

    # Collect all metric names
    all_metric_names: List[str] = []
    seen: set[str] = set()
    for e in evaluated:
        for k in e.get('raw_metrics', {}):
            if k not in seen:
                all_metric_names.append(k)
                seen.add(k)

    # Per-metric statistics
    per_metric: Dict[str, Dict[str, Any]] = {}
    for mname in all_metric_names:
        values = [
            e['raw_metrics'][mname]
            for e in evaluated
            if mname in e.get('raw_metrics', {})
        ]
        if values:
            per_metric[mname] = {
                'mean': round(sum(values) / len(values), 6),
                'min': round(min(values), 6),
                'max': round(max(values), 6),
                'count': len(values),
            }

    # Rankings by composite score
    ranked = sorted(
        evaluated,
        key=lambda e: e.get('composite_score') or float('-inf'),
        reverse=True,
    )
    rankings = []
    for idx, e in enumerate(ranked, start=1):
        rankings.append({
            'rank': idx,
            'dataset_name': e['dataset_name'],
            'method': e['method'],
            'composite_score': e.get('composite_score'),
            'metrics_computed': e.get('metrics_computed', 0),
        })

    # Aggregate catalog metrics coverage
    catalog_names = [m['name'] for m in catalog_metrics]
    coverage: Dict[str, int] = {}
    for mname in catalog_names:
        coverage[mname] = sum(
            1 for e in evaluated if mname in e.get('raw_metrics', {})
        )

    return {
        'benchmark_type': benchmark_type,
        'total_datasets': len(evaluations),
        'evaluated_count': len(evaluated),
        'skipped_count': len(skipped),
        'rankings': rankings,
        'per_metric_statistics': per_metric,
        'catalog_metric_coverage': coverage,
        'metric_names': all_metric_names,
        'catalog_metric_count': len(catalog_metrics),
    }


def _write_outputs(
    eval_dir: Path,
    evaluations: List[Dict[str, Any]],
    summary: Dict[str, Any],
    benchmark_type: str,
    catalog_metrics: List[Dict[str, Any]],
) -> None:
    """Write evaluation results to JSON and markdown report."""
    # --- JSON output ---
    metrics_path = eval_dir / 'benchmark_metrics.json'
    metrics_path.write_text(
        json.dumps({
            'benchmark_type': benchmark_type,
            'generated_by': 'skills/orchestrator/benchmark_evaluation/benchmark_evaluation.py',
            'evaluations': evaluations,
            'summary': summary,
        }, indent=2, default=str, ensure_ascii=False),
    )

    # --- Markdown report ---
    report_lines: List[str] = [
        f'# Benchmark Evaluation Report — {benchmark_type.title()}',
        '',
        f'**Total datasets evaluated**: {summary.get("evaluated_count", 0)} / '
        f'{summary.get("total_datasets", 0)}',
        f'**Benchmark type**: {benchmark_type}',
        f'**Catalog metrics defined**: {summary.get("catalog_metric_count", 0)}',
        '',
    ]

    # ── Pivot table: datasets rows × methods columns ──
    report_lines.extend(_build_pivot_table(evaluations))

    # Rankings table
    rankings = summary.get('rankings', [])
    if rankings:
        report_lines.extend([
            '## Rankings by Composite Score',
            '',
            '| Rank | Dataset | Method | Composite Score | Metrics Computed |',
            '|------|---------|--------|-----------------|------------------|',
        ])
        for r in rankings:
            score = r.get('composite_score')
            score_str = f'{score:.6f}' if score is not None else 'N/A'
            report_lines.append(
                f'| {r["rank"]} | {r["dataset_name"]} | {r["method"]} '
                f'| {score_str} | {r["metrics_computed"]} |',
            )
        report_lines.append('')

    # Per-metric statistics
    per_metric = summary.get('per_metric_statistics', {})
    if per_metric:
        report_lines.extend([
            '## Per-Metric Statistics',
            '',
            '| Metric | Mean | Min | Max | Count |',
            '|--------|------|-----|-----|-------|',
        ])
        for mname, stats in per_metric.items():
            report_lines.append(
                f'| {mname} | {stats["mean"]} | {stats["min"]} | '
                f'{stats["max"]} | {stats["count"]} |',
            )
        report_lines.append('')

    # Catalog metric coverage
    coverage = summary.get('catalog_metric_coverage', {})
    if coverage:
        report_lines.extend([
            '## Catalog Metric Coverage',
            '',
            '| Metric | Datasets Reporting |',
            '|--------|-------------------|',
        ])
        for mname, count in coverage.items():
            report_lines.append(f'| {mname} | {count} |')
        report_lines.append('')

    # Detailed evaluations
    report_lines.append('## Detailed Evaluations')
    report_lines.append('')
    for idx, ev in enumerate(evaluations, start=1):
        status = '✓' if ev.get('evaluated') else '✗'
        score = ev.get('composite_score')
        score_str = f'{score:.6f}' if score is not None else 'N/A'
        report_lines.extend([
            f'### {idx}. {ev["dataset_name"]} ({ev["method"]})',
            '',
            f'- **Evaluated**: {status}',
            f'- **Method**: {ev["computation_method"]}',
            f'- **Composite Score**: {score_str}',
            f'- **Metrics Computed**: {ev.get("metrics_computed", 0)}',
            '',
        ])
        raw = ev.get('raw_metrics', {})
        if raw:
            report_lines.append('| Metric | Value |')
            report_lines.append('|--------|-------|')
            for mname, mval in sorted(raw.items()):
                report_lines.append(f'| {mname} | {mval} |')
            report_lines.append('')

        missing = ev.get('missing_metrics', [])
        if missing:
            report_lines.append(f'**Missing metrics**: {", ".join(missing)}')
            report_lines.append('')

    (eval_dir / 'benchmark_report.md').write_text('\n'.join(report_lines) + '\n', encoding='utf-8')


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Benchmark evaluation: apply metrics to reproduced data',
    )
    parser.add_argument('--benchmark-type', required=True,
                        help='Benchmark type (integration, clustering, annotation, ...)')
    parser.add_argument('--output', required=True,
                        help='Benchmark suite output directory')
    parser.add_argument('--process-data-dir',
                        help='Path to Stage 3 processed data directory')
    parser.add_argument('--reproduce-dir',
                        help='Path to Stage 4 reproduce output directory')
    parser.add_argument('--catalog',
                        help='Path to metrics_catalog.json')
    parser.add_argument('--no-evaluate', action='store_true',
                        help='Skip metrics computation')
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    result = run_benchmark_evaluation(
        benchmark_type=args.benchmark_type,
        output_dir=Path(args.output),
        process_data_dir=Path(args.process_data_dir) if args.process_data_dir else None,
        reproduce_dir=Path(args.reproduce_dir) if args.reproduce_dir else None,
        metrics_catalog_path=Path(args.catalog) if args.catalog else None,
        no_evaluate=args.no_evaluate,
    )
    print(f'\nBenchmark evaluation complete.')
    print(f'  Evaluated: {result["summary"].get("evaluated_count", 0)} datasets')
    print(f'  Report:    {result["report_path"]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
