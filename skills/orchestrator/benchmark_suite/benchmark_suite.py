#!/usr/bin/env python3
"""Benchmark Suite Pipeline — end-to-end resumable benchmark workflow.

Chains benchmark_dispatch → reproduce_paper → benchmark_evaluation into a
single pipeline with checkpoint-based resumption and human interrupt points.

Usage:
    python benchmark_suite.py --benchmark-type integration --output ./results
    python benchmark_suite.py --benchmark-type spatial --output ./results --resume
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
# OmicsClaw repo root (one level above skills/)
_OMICSCLAW_ROOT = _PROJECT_ROOT.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / 'skills'))

from orchestrator.benchmark_dispatch.benchmark_dispatch import (
    collect_benchmark_data,
    generate_search_queries,
    generate_workflow_plan,
    save_results,
    save_accepted_papers,
)
from orchestrator.reproduce_paper.reproduce_paper import run_reproduce
from orchestrator.reproducibility_evaluation.reproducibility_evaluation import (
    load_metrics_catalog,
    compute_metrics_for_result,
    build_report,
)
from orchestrator.benchmark_evaluation.benchmark_evaluation import (
    run_benchmark_evaluation,
)

# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

CHECKPOINT_PREFIX = '.checkpoint_'


def checkpoint_path(output_dir: Path, stage: int) -> Path:
    return output_dir / f'{CHECKPOINT_PREFIX}{stage:02d}'


def is_stage_completed(output_dir: Path, stage: int) -> bool:
    return checkpoint_path(output_dir, stage).exists()


def mark_stage_completed(output_dir: Path, stage: int) -> None:
    cp = checkpoint_path(output_dir, stage)
    cp.write_text(f'Stage {stage} completed at {datetime.now(timezone.utc).isoformat()}\n')
    print(f'  ✓ Checkpoint saved: {cp.name}')


def stage_dir(output_dir: Path, stage: int, label: str) -> Path:
    return output_dir / f'{stage:02d}_{label}'


# ---------------------------------------------------------------------------
# Session / summary helpers
# ---------------------------------------------------------------------------

SUMMARY_FILE = 'benchmark_suite_summary.json'
REPORT_FILE = 'benchmark_suite_report.md'


def load_summary(output_dir: Path) -> Dict[str, Any]:
    path = output_dir / SUMMARY_FILE
    if path.exists():
        return json.loads(path.read_text())
    return {
        'pipeline': 'benchmark-suite',
        'stages': {},
        'completed_at': None,
        'metadata': {},
    }


def save_summary(output_dir: Path, summary: Dict[str, Any]) -> Path:
    path = output_dir / SUMMARY_FILE
    path.write_text(json.dumps(summary, indent=2))
    return path


def save_report(output_dir: Path, report_text: str) -> Path:
    path = output_dir / REPORT_FILE
    path.write_text(report_text)
    return path


# ---------------------------------------------------------------------------
# Stage runners
# ---------------------------------------------------------------------------

def run_stage_dispatch(
    benchmark_type: str,
    query: Optional[str],
    specific_input: Optional[str],
    output_dir: Path,
    no_download: bool,
    summary: Dict[str, Any],
    use_llm: bool = False,
) -> Dict[str, Any]:
    """Stage 0: benchmark dispatch — collect literature & datasets."""
    dispatch_dir = stage_dir(output_dir, 0, 'benchmark_dispatch')
    dispatch_dir.mkdir(parents=True, exist_ok=True)

    print(f'\n{"=" * 60}')
    print(f'  Stage 0: Benchmark Dispatch [{benchmark_type}]')
    print(f'{"=" * 60}')

    search_queries = generate_search_queries(benchmark_type, query)

    collected_data = collect_benchmark_data(
        search_queries, benchmark_type, specific_input,
        dispatch_dir, no_download, use_llm=use_llm,
    )

    workflow_plan = generate_workflow_plan(benchmark_type, collected_data)
    save_results(workflow_plan, collected_data, dispatch_dir)

    # Save FULLY_ACCEPTED papers to per-benchmark, per-paper folder structure
    _benchmark_data_root = _OMICSCLAW_ROOT / 'benchmark_data'
    accepted_summary = save_accepted_papers(
        collected_data,
        _benchmark_data_root,
        benchmark_type,
        download_data=not no_download,
    )
    collected_data['accepted_papers_summary'] = accepted_summary

    summary['stages']['00_benchmark_dispatch'] = {
        'status': 'completed',
        'output_dir': str(dispatch_dir),
        'literature_count': len(collected_data.get('literature_results', [])),
        'datasets_found': {
            'geo': len(collected_data.get('datasets', {}).get('geo', [])),
            'sra': len(collected_data.get('datasets', {}).get('sra', [])),
            'cellxgene': len(collected_data.get('datasets', {}).get('cellxgene', [])),
        },
        'total_relevance_score': collected_data.get('total_relevance_score', 0),
        'accepted_papers_saved': accepted_summary.get('saved_count', 0),
        'accepted_papers_dir': str(_benchmark_data_root / benchmark_type),
    }
    save_summary(output_dir, summary)

    return collected_data


def run_stage_reproduce(
    benchmark_type: str,
    specific_input: Optional[str],
    repo_url: Optional[str],
    collected_data: Dict[str, Any],
    output_dir: Path,
    no_clone: bool,
    no_install: bool,
    no_reproduce_run: bool,
    clone_depth: int,
    summary: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Stage 2: reproduce paper — clone, install, execute, verify."""
    reproduce_dir = stage_dir(output_dir, 2, 'reproduce')
    reproduce_dir.mkdir(parents=True, exist_ok=True)

    print(f'\n{"=" * 60}')
    print(f'  Stage 2: Reproduce Paper')
    print(f'{"=" * 60}')

    # Determine input text for reproduce
    reproduce_input = specific_input or repo_url or None
    if reproduce_input is None and collected_data.get('literature_results'):
        first = collected_data['literature_results'][0]
        reproduce_input = first.get('raw_text') or first.get('input')

    if not reproduce_input:
        print('  No suitable input for reproduction — skipping stage.')
        summary['stages']['01_reproduce'] = {
            'status': 'skipped',
            'reason': 'No input text or repository URL available.',
        }
        save_summary(output_dir, summary)
        return None

    # Gather paper metadata from llm_collector results
    paper_metadata = None
    for lr in collected_data.get('literature_results', []):
        if lr.get('metadata') and lr.get('source') in ('llm_collector', 'pubmed', 'arxiv', 'github', 'google_scholar'):
            paper_metadata = lr.get('metadata', {})
            break

    reproduce_result = run_reproduce(
        reproduce_input,
        'auto',
        repo_url,
        benchmark_type,
        paper_metadata=paper_metadata,
        output=reproduce_dir,
        no_clone=no_clone,
        no_install=no_install,
        no_run=no_reproduce_run,
        clone_depth=clone_depth,
    )

    result_statuses = reproduce_result.get('result', {}).get('statuses', {})
    summary['stages']['01_reproduce'] = {
        'status': 'completed',
        'output_dir': str(reproduce_dir),
        'clone_success': result_statuses.get('clone_success', False),
        'install_success': result_statuses.get('install_success', False),
        'run_success': result_statuses.get('run_success', False),
        'failure_phase': result_statuses.get('failure_phase'),
        'reproduce_status': reproduce_result.get('status'),
    }
    save_summary(output_dir, summary)

    return reproduce_result


def run_stage_process_data(
    collected_data: Dict[str, Any],
    output_dir: Path,
    summary: Dict[str, Any],
    no_process: bool = False,
) -> Dict[str, Any]:
    """Stage 1: process downloaded datasets through OmicsClaw sc tools.

    Reads paper_metadata.json for each FULLY_ACCEPTED paper in
    benchmark_data/{type}/, discovers data files of all supported formats
    (.h5ad, .mtx, 10X directories, .csv, .txt.gz), and runs:

        1. sc-standardize-input  → canonical AnnData
        2. sc-preprocessing      → QC + normalize + HVG + PCA

    Output: ``01_process_data/{paper_slug}/{dataset}/standardize/processed.h5ad``
            and ``.../preprocess/processed.h5ad``

    The benchmark analysis (e.g. sc-batch-integration) runs in Stage 4,
    not here — Stage 1 only prepares clean, preprocessed data.
    """
    process_dir = stage_dir(output_dir, 1, 'process_data')
    process_dir.mkdir(parents=True, exist_ok=True)

    print(f'\n{"=" * 60}')
    print(f'  Stage 1: Process Downloaded Data')
    print(f'{"=" * 60}')

    if no_process:
        print('  Data processing skipped (--no-process).')
        summary['stages']['01_process_data'] = {'status': 'skipped', 'reason': '--no-process flag'}
        save_summary(output_dir, summary)
        return {'processed': [], 'status': 'skipped'}

    bm_type = summary.get('metadata', {}).get('benchmark_type', '')
    benchmark_data_root = _OMICSCLAW_ROOT / 'benchmark_data' / bm_type

    if not benchmark_data_root.exists():
        print(f'  No benchmark data found at: {benchmark_data_root}')
        summary['stages']['01_process_data'] = {'status': 'skipped', 'reason': 'No benchmark data dir'}
        save_summary(output_dir, summary)
        return {'processed': [], 'status': 'skipped'}

    # ── Discover papers ──
    papers = _discover_papers(benchmark_data_root)
    if not papers:
        print('  No papers with data found.')
        summary['stages']['01_process_data'] = {'status': 'skipped', 'reason': 'No papers'}
        save_summary(output_dir, summary)
        return {'processed': [], 'status': 'skipped'}

    print(f'  Found {len(papers)} paper(s) with data to process.\n')

    processed = []
    for paper in papers:
        paper_result = _process_one_paper(
            paper, process_dir, bm_type, output_dir, summary
        )
        processed.append(paper_result)

    summary['stages']['01_process_data'] = {
        'status': 'completed',
        'output_dir': str(process_dir),
        'papers_processed': len(processed),
        'results': processed,
    }
    save_summary(output_dir, summary)
    return {'processed': processed, 'status': 'completed'}


# ---------------------------------------------------------------------------
# Paper discovery & per-paper processing
# ---------------------------------------------------------------------------

def _discover_papers(benchmark_data_root: Path) -> List[Dict[str, Any]]:
    """Find all papers in benchmark_data/{type}/ that have data files."""
    papers = []
    for paper_dir in sorted(benchmark_data_root.iterdir()):
        if not paper_dir.is_dir() or paper_dir.name.startswith('_'):
            continue
        meta_path = paper_dir / 'paper_metadata.json'
        if not meta_path.exists():
            continue

        metadata = json.loads(meta_path.read_text(encoding='utf-8'))
        data_dir = paper_dir / 'data'
        if not data_dir.exists():
            continue

        # Find all data files that smart_load can handle
        data_files = _find_sc_data_files(data_dir)
        if not data_files:
            continue

        papers.append({
            'slug': paper_dir.name,
            'dir': paper_dir,
            'data_dir': data_dir,
            'metadata': metadata,
            'data_files': data_files,
        })
    return papers


def _find_sc_data_files(data_dir: Path) -> List[Path]:
    """Find single-cell data files/directories that OmicsClaw can load.

    Priority order:
      1. .h5ad files
      2. 10X mtx directories (containing matrix.mtx + barcodes + features)
      3. Standalone .mtx/.mtx.gz files (with companion files)
      4. .h5 files (10X HDF5)
      5. .csv/.tsv count matrices
      6. .txt.gz count matrices
      7. .tar archives (will be extracted later)
    """
    found = []

    # 1. .h5ad — best format
    for f in data_dir.rglob('*.h5ad'):
        if f.stat().st_size > 1024:
            found.append(f)

    # 2. 10X mtx directories — detect matrix.mtx + barcodes + features
    for matrix_file in data_dir.rglob('matrix.mtx*'):
        parent = matrix_file.parent
        has_barcodes = any(parent.glob('barcodes.tsv*'))
        has_features = any(parent.glob('features.tsv*')) or any(parent.glob('genes.tsv*'))
        if has_barcodes and has_features:
            found.append(parent)  # Directory path
        elif '_fixed_10x' in str(parent):
            found.append(parent)

    # 3. Standalone .mtx.gz with companion annotation files
    for mtx_file in data_dir.rglob('*.mtx*'):
        if mtx_file.stat().st_size > 1024:
            found.append(mtx_file)

    # 4. .h5 files (10X HDF5)
    for f in data_dir.rglob('*.h5'):
        if f.stat().st_size > 1024:
            found.append(f)

    # 5. .rds files (Seurat R objects)
    for f in data_dir.rglob('*.rds'):
        if f.stat().st_size > 1024:
            found.append(f)

    # 6. .csv/.tsv/.txt expression matrices
    for f in data_dir.rglob('*_matrix_expression_*.csv*'):
        if f.stat().st_size > 1024:
            found.append(f)
    for f in data_dir.rglob('*_raw_count*.txt*'):
        if f.stat().st_size > 1024:
            found.append(f)
    for f in data_dir.rglob('*_count*.csv*'):
        if f.stat().st_size > 1024:
            found.append(f)

    # 7. .tar archives (GEO RAW tars containing extracted data)
    for f in data_dir.rglob('*_RAW.tar'):
        # Prefer extracted contents if available
        extracted_dir = f.parent / 'extracted'
        if extracted_dir.exists() and any(extracted_dir.iterdir()):
            mtx_in_extracted = list(extracted_dir.rglob('*.mtx*'))
            if mtx_in_extracted:
                # Use the extracted 10X-style directories
                for mtx in mtx_in_extracted:
                    p = mtx.parent
                    if any(p.glob('barcodes*')) or any(p.glob('genes*')):
                        if p not in found:
                            found.append(p)
                continue
        found.append(f)

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for f in found:
        key = str(f.resolve())
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def _process_one_paper(
    paper: Dict[str, Any],
    process_dir: Path,
    bm_type: str,
    output_dir: Path,
    summary: Dict[str, Any],
) -> Dict[str, Any]:
    """Process one paper's data through OmicsClaw pipeline."""
    slug = paper['slug']
    data_files = paper['data_files']
    metadata = paper['metadata']

    paper_out = process_dir / slug
    paper_out.mkdir(parents=True, exist_ok=True)

    print(f'  📄 [{slug[:55]}]')
    print(f'     Data files: {len(data_files)}')

    results = []
    for data_path in data_files:
        result = _process_one_dataset(
            data_path, paper_out, bm_type, metadata
        )
        results.append(result)

    # Save per-paper result
    result_path = paper_out / 'stage1_result.json'
    result_path.write_text(json.dumps({
        'paper': slug,
        'datasets_processed': len(results),
        'results': results,
    }, indent=2))

    return {
        'paper': slug,
        'datasets_processed': len(results),
        'results': results,
    }


def _process_one_dataset(
    data_path: Path,
    paper_out: Path,
    bm_type: str,
    metadata: Dict[str, Any],
) -> Dict[str, Any]:
    """Process a single dataset through standardize → preprocess.

    Outputs a clean ``processed.h5ad`` ready for downstream stages.
    The benchmark analysis (e.g. sc-batch-integration) runs in Stage 4.
    """
    name = data_path.name if data_path.is_file() else data_path.parent.name
    dataset_out = paper_out / _sanitize(name)
    dataset_out.mkdir(parents=True, exist_ok=True)

    result = {
        'input': str(data_path),
        'input_type': 'directory' if data_path.is_dir() else 'file',
        'steps': {},
    }

    # ── Step 1: sc-standardize-input (canonical AnnData) ──
    std_dir = dataset_out / 'standardize'
    std_dir.mkdir(exist_ok=True)
    print(f'     → sc-standardize-input: {data_path.name if data_path.is_file() else data_path.parent.name}')

    skill_result = _run_omicsclaw_skill(
        'sc-standardize-input',
        input_path=str(data_path),
        output_dir=str(std_dir),
    )
    result['steps']['standardize'] = skill_result

    if not skill_result.get('success'):
        result['status'] = 'failed_standardize'
        return result

    processed_h5ad = std_dir / 'processed.h5ad'
    if not processed_h5ad.exists():
        result['status'] = 'no_output_h5ad'
        return result

    # ── Step 2: sc-preprocessing (QC + normalize + HVG + PCA) ──
    pre_dir = dataset_out / 'preprocess'
    pre_dir.mkdir(exist_ok=True)
    print(f'     → sc-preprocessing (scanpy)')

    skill_result = _run_omicsclaw_skill(
        'sc-preprocessing',
        input_path=str(processed_h5ad),
        output_dir=str(pre_dir),
    )
    result['steps']['preprocess'] = skill_result

    if not skill_result.get('success'):
        result['status'] = 'failed_preprocess'
        return result

    result['status'] = 'completed'
    return result


def _sanitize(name: str, max_len: int = 40) -> str:
    """Sanitize a name for use as a directory name."""
    import re as _re
    name = _re.sub(r'[^\w\-.]', '_', name)
    return name[:max_len].rstrip('_')


# ---------------------------------------------------------------------------
# Skill runner
# ---------------------------------------------------------------------------

# Map skill-name → actual Python module file name
_SKILL_MODULE_MAP: Dict[str, str] = {
    'sc-ambient-removal':       'sc_ambient',
    'sc-batch-integration':     'sc_integrate',
    'sc-cell-annotation':       'sc_annotate',
    'sc-cell-communication':    'sc_cell_communication',
    'sc-clustering':            'sc_cluster',
    'sc-count':                 'sc_count',
    'sc-cytotrace':             'sc_cytotrace',
    'sc-de':                    'sc_de',
    'sc-differential-abundance':'sc_differential_abundance',
    'sc-doublet-detection':     'sc_doublet',
    'sc-drug-response':         'sc_drug_response',
    'sc-enrichment':            'sc_enrichment',
    'sc-fastq-qc':              'sc_fastq_qc',
    'sc-filter':                'sc_filter',
    'sc-gene-programs':         'sc_gene_programs',
    'sc-grn':                   'sc_grn',
    'sc-in-silico-perturbation':'sc_in_silico_perturbation',
    'sc-markers':               'sc_markers',
    'sc-metacell':              'sc_metacell',
    'sc-multi-count':           'sc_multi_count',
    'sc-pathway-scoring':       'sc_pathway_scoring',
    'sc-perturb':               'sc_perturb',
    'sc-perturb-prep':          'sc_perturb_prep',
    'sc-preprocessing':         'sc_preprocess',
    'sc-pseudotime':            'sc_pseudotime',
    'sc-qc':                    'sc_qc',
    'sc-standardize-input':     'sc_standardize_input',
    'sc-velocity':              'sc_velocity',
    'sc-velocity-prep':         'sc_velocity_prep',
}


def _run_omicsclaw_skill(skill_name: str, input_path: str, output_dir: str) -> Dict:
    """Run an OmicsClaw skill via subprocess.

    Uses the _SKILL_MODULE_MAP to find the correct Python module name
    for each skill (skill directory names differ from .py file names).
    """
    import subprocess as sp
    import sys as _sys

    module_name = _SKILL_MODULE_MAP.get(skill_name)
    if module_name is None:
        return {'success': False, 'error': f'Unknown skill: {skill_name}'}

    module_path = f'skills.singlecell.scrna.{skill_name}.{module_name}'

    try:
        cmd = [
            _sys.executable, '-m', module_path,
            '--input', input_path,
            '--output', output_dir,
        ]
        completed = sp.run(cmd, capture_output=True, text=True, timeout=1800)
        return {
            'success': completed.returncode == 0,
            'stdout': completed.stdout[-500:] if completed.stdout else '',
            'stderr': completed.stderr[-500:] if completed.stderr else '',
        }
    except sp.TimeoutExpired:
        return {'success': False, 'error': f'Timeout (1800s) running {skill_name}'}
    except Exception as exc:
        return {'success': False, 'error': str(exc)}


def _benchmark_to_skill(benchmark_type: str) -> Optional[str]:
    """Map benchmark type to OmicsClaw analysis skill."""
    mapping = {
        'integration': 'sc-batch-integration',
        'clustering': 'sc-clustering',
        'annotation': 'sc-cell-annotation',
        'trajectory': 'sc-pseudotime',
        'doublet_detection': 'sc-doublet-detection',
        'de': 'sc-de',
        'grn': 'sc-grn',
        'cell_communication': 'sc-cell-communication',
    }
    return mapping.get(benchmark_type)


def run_stage_evaluate(
    benchmark_type: str,
    reproduce_result: Optional[Dict[str, Any]],
    output_dir: Path,
    catalog_path: Path,
    include_suggestions: bool,
    summary: Dict[str, Any],
) -> Dict[str, Any]:
    """Stage 3: reproducibility evaluation — compute metrics & generate report."""
    eval_dir = stage_dir(output_dir, 3, 'reproducibility_evaluation')
    eval_dir.mkdir(parents=True, exist_ok=True)

    print(f'\n{"=" * 60}')
    print(f'  Stage 3: Reproducibility Evaluation')
    print(f'{"=" * 60}')

    catalog = load_metrics_catalog(catalog_path)

    evaluated_results: List[Dict[str, Any]] = []

    if reproduce_result:
        result_data = reproduce_result.get('result') or reproduce_result
        evaluated = compute_metrics_for_result(result_data, catalog)
        evaluated_results.append(evaluated)

    if not evaluated_results:
        print('  No results to evaluate — generating empty evaluation.')
        evaluated_results.append({
            'benchmark_type': benchmark_type,
            'repository_count': 0,
            'repository_found': False,
            'clone_success': False,
            'install_success': False,
            'run_success': False,
            'reproducibility_score': None,
            'failure_phase': 'no_data',
            'failure_details': ['No reproduce data available for evaluation.'],
            'dataset_counts': {'total': 0, 'types_present': 0},
            'method_sections_count': 0,
            'code_snippets_count': 0,
            'commands_count': 0,
            'environment_file_count': 0,
            'plan_steps_count': 0,
            'baseline_metrics': [],
            'computed_metrics': {},
            'missing_baseline_metrics': [],
            'metric_suggestions': [],
        })

    metrics_path = eval_dir / 'reproducibility_metrics.json'
    metrics_path.write_text(json.dumps({'results': evaluated_results}, indent=2))

    report_text = build_report(evaluated_results, catalog, include_suggestions=include_suggestions)
    report_path = eval_dir / 'reproducibility_report.md'
    report_path.write_text(report_text)

    summary['stages']['03_reproducibility_evaluation'] = {
        'status': 'completed',
        'output_dir': str(eval_dir),
        'result_count': len(evaluated_results),
        'benchmark_type': benchmark_type,
    }
    save_summary(output_dir, summary)

    # Also write the pipeline-level report
    save_report(output_dir, report_text)

    return {'metrics_path': str(metrics_path), 'report_path': str(report_path)}


def run_stage_benchmark_evaluate(
    benchmark_type: str,
    output_dir: Path,
    summary: Dict[str, Any],
    no_evaluate: bool = False,
) -> Dict[str, Any]:
    """Stage 4: benchmark evaluation — apply benchmark metrics to reproduced data."""
    eval_dir = stage_dir(output_dir, 4, 'benchmark_evaluation')
    eval_dir.mkdir(parents=True, exist_ok=True)

    print(f'\n{"=" * 60}')
    print(f'  Stage 4: Benchmark Evaluation [{benchmark_type}]')
    print(f'{"=" * 60}')

    if no_evaluate:
        print('  Benchmark evaluation skipped (--no-evaluate).')
        summary['stages']['04_benchmark_evaluation'] = {
            'status': 'skipped', 'reason': '--no-evaluate flag',
        }
        save_summary(output_dir, summary)
        return {'status': 'skipped'}

    # Determine input directories from previous stages
    process_dir = stage_dir(output_dir, 1, 'process_data')
    reproduce_dir = stage_dir(output_dir, 2, 'reproduce')

    catalog_path = (
        _PROJECT_ROOT / 'orchestrator' / 'reproducibility_evaluation' / 'metrics_catalog.json'
    )

    result = run_benchmark_evaluation(
        benchmark_type=benchmark_type,
        output_dir=output_dir,
        process_data_dir=process_dir if process_dir.exists() else None,
        reproduce_dir=reproduce_dir / 'reproducibility' if (reproduce_dir / 'reproducibility').exists() else None,
        metrics_catalog_path=catalog_path if catalog_path.exists() else None,
        no_evaluate=no_evaluate,
    )

    # Copy the report to the pipeline-level report if none exists
    pipeline_report = output_dir / REPORT_FILE
    if result.get('report_path') and not pipeline_report.exists():
        report_text = Path(result['report_path']).read_text()
        save_report(output_dir, report_text)

    summary['stages']['04_benchmark_evaluation'] = {
        'status': 'completed',
        'output_dir': str(eval_dir),
        'evaluated_count': result.get('summary', {}).get('evaluated_count', 0),
        'total_datasets': result.get('summary', {}).get('total_datasets', 0),
        'metrics_path': result.get('metrics_path', ''),
        'report_path': result.get('report_path', ''),
    }
    save_summary(output_dir, summary)

    return result


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_benchmark_suite(
    benchmark_type: str,
    query: Optional[str] = None,
    specific_input: Optional[str] = None,
    repo_url: Optional[str] = None,
    output: Path | str = '.',
    *,
    resume: bool = False,
    no_download: bool = False,
    no_process: bool = False,
    no_reproduce_clone: bool = False,
    no_reproduce_install: bool = False,
    no_reproduce_run: bool = False,
    clone_depth: int = 1,
    include_suggestions: bool = True,
    use_llm: bool = False,
    no_evaluate: bool = False,
) -> Dict[str, Any]:
    """Run the full benchmark pipeline with checkpoint resumption."""
    output_dir = Path(output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = (
        _PROJECT_ROOT / 'orchestrator' / 'reproducibility_evaluation' / 'metrics_catalog.json'
    )

    summary = load_summary(output_dir)
    summary['metadata'] = {
        'benchmark_type': benchmark_type,
        'query': query,
        'resume': resume,
        'started_at': datetime.now(timezone.utc).isoformat(),
    }

    collected_data: Dict[str, Any] = {}
    reproduce_result: Optional[Dict[str, Any]] = None

    # --- Stage 0: Dispatch ---
    if resume and is_stage_completed(output_dir, 0):
        print(f'  ↪ Stage 0 already completed, resuming at stage 1.')
    else:
        collected_data = run_stage_dispatch(
            benchmark_type, query, specific_input,
            output_dir, no_download, summary, use_llm=use_llm,
        )
        mark_stage_completed(output_dir, 0)
        # HUMAN CHECKPOINT
        print(f'\n  🛑 CHECKPOINT: Stage 0 complete. Review artifacts at:')
        print(f'     {stage_dir(output_dir, 0, "benchmark_dispatch")}')
        print(f'     Run with --resume to skip to Stage 1.\n')

    # --- Stage 1: Process downloaded data ---
    if resume and is_stage_completed(output_dir, 1):
        print(f'  ↪ Stage 1 already completed, resuming at stage 2.')
    else:
        run_stage_process_data(
            collected_data, output_dir, summary, no_process=no_process,
        )
        mark_stage_completed(output_dir, 1)
        print(f'\n  🛑 CHECKPOINT: Stage 1 complete. Review artifacts at:')
        print(f'     {stage_dir(output_dir, 1, "process_data")}')
        print(f'     Run with --resume to skip to Stage 2.\n')

    # --- Stage 2: Reproduce Paper ---
    if resume and is_stage_completed(output_dir, 2):
        print(f'  ↪ Stage 2 already completed, resuming at stage 3.')
    else:
        # Load dispatch results if not already in memory
        if not collected_data:
            dispatch_dir = stage_dir(output_dir, 0, 'benchmark_dispatch')
            plan_file = dispatch_dir / 'workflow_plan.json'
            summary_file = dispatch_dir / 'data_summary.md'
            collected_data = {
                'literature_results': [],
                'datasets': {'geo': [], 'sra': [], 'cellxgene': []},
            }
            if plan_file.exists():
                try:
                    plan = json.loads(plan_file.read_text())
                    collected_data['literature_results'] = [{'raw_text': str(plan)}]
                except Exception:
                    pass

        reproduce_result = run_stage_reproduce(
            benchmark_type, specific_input, repo_url,
            collected_data, output_dir,
            no_reproduce_clone, no_reproduce_install, no_reproduce_run,
            clone_depth, summary,
        )
        mark_stage_completed(output_dir, 2)
        print(f'\n  🛑 CHECKPOINT: Stage 2 complete. Review artifacts at:')
        print(f'     {stage_dir(output_dir, 2, "reproduce")}')
        print(f'     Run with --resume to skip to Stage 3.\n')

    # --- Stage 3: Reproducibility Evaluation ---
    if resume and is_stage_completed(output_dir, 3):
        print(f'  ↪ Stage 3 already completed.')
    else:
        # Load reproduce result from disk if needed
        if reproduce_result is None and is_stage_completed(output_dir, 2):
            reproduce_dir = stage_dir(output_dir, 2, 'reproduce')
            result_file = reproduce_dir / 'reproducibility' / 'result.json'
            if result_file.exists():
                reproduce_result = {'result': json.loads(result_file.read_text())}
            plan_file = reproduce_dir / 'reproducibility' / 'plan.json'
            if plan_file.exists() and reproduce_result:
                reproduce_result['plan'] = json.loads(plan_file.read_text())

        run_stage_evaluate(
            benchmark_type, reproduce_result,
            output_dir, catalog_path, include_suggestions, summary,
        )
        mark_stage_completed(output_dir, 3)
        print(f'\n  🛑 CHECKPOINT: Stage 3 complete. Review artifacts at:')
        print(f'     {stage_dir(output_dir, 3, "reproducibility_evaluation")}')
        print(f'     Run with --resume to skip to Stage 4.\n')

    # --- Stage 4: Benchmark Evaluation ---
    if resume and is_stage_completed(output_dir, 4):
        print(f'  ↪ Stage 4 already completed.')
    else:
        run_stage_benchmark_evaluate(
            benchmark_type=benchmark_type,
            output_dir=output_dir,
            summary=summary,
            no_evaluate=no_evaluate,
        )
        mark_stage_completed(output_dir, 4)

    # --- Finalize ---
    summary['completed_at'] = datetime.now(timezone.utc).isoformat()
    save_summary(output_dir, summary)

    final_report = output_dir / REPORT_FILE
    print(f'\n{"=" * 60}')
    print(f'  ✅ Benchmark Suite Complete')
    print(f'{"=" * 60}')
    print(f'  Summary: {output_dir / SUMMARY_FILE}')
    print(f'  Report:  {final_report}')
    print(f'  Stages completed:')
    for stage_key, stage_info in sorted(summary.get('stages', {}).items()):
        status = stage_info.get('status', 'unknown')
        emoji = '✅' if status == 'completed' else '⏭️' if status == 'skipped' else '❌'
        print(f'    {emoji} {stage_key}: {status}')

    return {
        'summary_path': str(output_dir / SUMMARY_FILE),
        'report_path': str(final_report),
        'output_dir': str(output_dir),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='End-to-end benchmark pipeline with checkpoint resumption',
    )
    parser.add_argument('--benchmark-type', required=True,
                        help='Benchmark type (e.g. integration, spatial, clustering)')
    parser.add_argument('--query', help='Search query for literature/data discovery')
    parser.add_argument('--input', help='Specific input (URL, DOI, PDF, or paper text)')
    parser.add_argument('--repo-url', help='Explicit repository URL for reproduction')
    parser.add_argument('--output', required=True, help='Output directory')
    parser.add_argument('--resume', action='store_true',
                        help='Resume a previously interrupted pipeline run')
    parser.add_argument('--no-download', action='store_true',
                        help='Skip dataset download')
    parser.add_argument('--no-process', action='store_true',
                        help='Skip data processing through OmicsClaw sc tools')
    parser.add_argument('--no-reproduce-clone', action='store_true',
                        help='Skip repository cloning during reproduction')
    parser.add_argument('--no-reproduce-install', action='store_true',
                        help='Skip environment installation during reproduction')
    parser.add_argument('--no-reproduce-run', action='store_true',
                        help='Skip execution during reproduction')
    parser.add_argument('--clone-depth', type=int, default=1,
                        help='Git clone depth for reproduction checkout')
    parser.add_argument('--no-suggestions', action='store_true',
                        help='Exclude metric suggestions from evaluation report')
    parser.add_argument('--use-llm', action='store_true',
                        help='Use LLM for intelligent literature search and dataset extraction')
    parser.add_argument('--no-evaluate', action='store_true',
                        help='Skip benchmark evaluation stage')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = run_benchmark_suite(
            args.benchmark_type,
            query=args.query,
            specific_input=args.input,
            repo_url=args.repo_url,
            output=args.output,
            resume=args.resume,
            no_download=args.no_download,
            no_process=args.no_process,
            no_reproduce_clone=args.no_reproduce_clone,
            no_reproduce_install=args.no_reproduce_install,
            no_reproduce_run=args.no_reproduce_run,
            clone_depth=args.clone_depth,
            include_suggestions=not args.no_suggestions,
            use_llm=args.use_llm,
            no_evaluate=args.no_evaluate,
        )
        return 0
    except Exception as exc:
        print(f'Error: {exc}', file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
