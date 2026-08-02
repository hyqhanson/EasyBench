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
# benchmark_data is under the project root (D:\HYQ\EasyBench)
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
    save_stage0_quality_report,
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
from skills.agents.agent_preflight.runner import run_agent_preflight
from skills.agents.agent_curator.curator_runner import run_agent_curator
from skills.agents.agent_curator.executor import CurationExecutor
from skills.agents.agent_curator.validator import validate_curated_h5ad
from skills.agents.agent_reproduce.runner import run_agent_reproduce as run_agent_reproduce_v2
from skills.processor.processor import run_processor
from skills.orchestrator.benchmark_evaluation.benchmark_evaluation import run_benchmark_evaluation

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
    print(f'Checkpoint saved: {cp.name}')


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
    search_only: bool = False,
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
        dispatch_dir, no_download or search_only, use_llm=use_llm,
    )

    # Save FULLY_ACCEPTED papers to per-benchmark, per-paper folder structure
    _benchmark_data_root = _OMICSCLAW_ROOT / 'benchmark_data'
    if search_only:
        accepted_summary = {
            'status': 'skipped',
            'reason': '--search-only',
            'saved_count': 0,
            'benchmark_type': benchmark_type,
            'papers': [],
        }
        print('  🔎 Search-only mode: skipping accepted-paper save, data download, and code clone.')
    else:
        accepted_summary = save_accepted_papers(
            collected_data,
            _benchmark_data_root,
            benchmark_type,
            download_data=not no_download,
            output_name=output_dir.name,
        )
    collected_data['accepted_papers_summary'] = accepted_summary

    workflow_plan = generate_workflow_plan(benchmark_type, collected_data)
    save_results(workflow_plan, collected_data, dispatch_dir)
    quality_summary = save_stage0_quality_report(
        collected_data,
        dispatch_dir,
        accepted_summary,
        search_only=search_only,
    )

    summary['stages']['00_benchmark_dispatch'] = {
        'status': 'completed',
        'output_dir': str(dispatch_dir),
        'search_only': search_only,
        'literature_count': len(collected_data.get('literature_results', [])),
        'raw_literature_count': collected_data.get('raw_literature_count', len(collected_data.get('literature_results', []))),
        'filtered_out_no_dataset_count': collected_data.get('filtered_out_no_dataset_count', 0),
        'datasets_found': {
            'geo': len(collected_data.get('datasets', {}).get('geo', [])),
            'sra': len(collected_data.get('datasets', {}).get('sra', [])),
            'cellxgene': len(collected_data.get('datasets', {}).get('cellxgene', [])),
        },
        'total_relevance_score': collected_data.get('total_relevance_score', 0),
        'accepted_papers_saved': accepted_summary.get('saved_count', 0),
        'accepted_papers_status': accepted_summary.get('status', 'completed'),
        'accepted_papers_dir': '' if search_only else str(_benchmark_data_root / f'{benchmark_type}_{output_dir.name}'),
        'quality_summary': {
            'path': str(dispatch_dir / 'stage0_quality_summary.json'),
            'fully_accepted_count': quality_summary.get('fully_accepted_count', 0),
            'quality_flag_counts': quality_summary.get('quality_flag_counts', {}),
        },
    }
    save_summary(output_dir, summary)

    return collected_data



def run_stage_preflight(
    benchmark_type: str,
    output_dir: Path,
    summary: Dict[str, Any],
    use_llm: bool = True,
) -> Dict[str, Any]:
    """Stage 1: AgentScanner — match protocol + code + data for each paper.

    Reads experimental_protocol.json, scans data/ and benchmark_code/
    directories, and calls the LLM to produce execution_plan.json.
    """
    print(f'\n{"=" * 60}')
    print(f'  Stage 1: Agent Preflight [{benchmark_type}]')
    print(f'{"=" * 60}')

    data_root = _OMICSCLAW_ROOT / 'benchmark_data'
    code_root = _OMICSCLAW_ROOT / 'benchmark_code'
    data_dir = data_root / f'{benchmark_type}_{output_dir.name}'
    code_dir = code_root / f'{benchmark_type}_{output_dir.name}'

    if not data_dir.exists():
        print(f'  ⚠️  Data dir not found: {data_dir}')
        summary['stages']['01_agent_preflight'] = {
            'status': 'skipped', 'reason': 'No data dir',
        }
        save_summary(output_dir, summary)
        return {'status': 'skipped'}

    from skills.agents.agent_preflight.runner import run_agent_preflight

    result = run_agent_preflight(
        benchmark_type=f'{benchmark_type}_{output_dir.name}',
        data_root=data_root,
        code_root=code_root,
        use_llm=use_llm,
    )

    summary['stages']['01_agent_preflight'] = {
        'status': 'completed',
        'total_papers': result.get('total_papers', 0),
        'status_counts': result.get('status_counts', {}),
    }
    save_summary(output_dir, summary)

    # ── Copy execution_plan.json per paper to output folder ──
    preflight_out = output_dir / '01_preflight'
    preflight_out.mkdir(parents=True, exist_ok=True)
    pre_summary_src = data_dir / '_preflight_summary.json'
    if pre_summary_src.exists():
        import shutil
        shutil.copy2(str(pre_summary_src), str(preflight_out / '_preflight_summary.json'))
    for paper_path in sorted(data_dir.iterdir()):
        if not paper_path.is_dir() or paper_path.name.startswith('_'):
            continue
        ep_src = paper_path / 'execution_plan.json'
        if ep_src.exists():
            slug_out = preflight_out / paper_path.name
            slug_out.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(ep_src), str(slug_out / 'execution_plan.json'))
    print(f'  📋 Preflight outputs copied to: {preflight_out}')

    return result


def run_stage_curate(
    benchmark_type: str,
    output_dir: Path,
    data_root: Path,
    summary: Dict[str, Any],
    use_llm: bool = True,
) -> Dict[str, Any]:
    """Stage 2: AgentCurator — LLM-driven data format detection & curation plan.
    Plus AgentCuratorExecutor + AgentCuratorValidator.

    Reads execution_plan.json + data file listings for each paper,
    produces curation_plan.json, executes conversion, and validates.
    """
    print(f'\n{"=" * 60}')
    print(f'  Stage 2: Agent Curator — Detect & Convert & Validate')
    print(f'{"=" * 60}')

    # 1. LLM detection → curation_plan.json
    result = run_agent_curator(
        benchmark_type=f'{benchmark_type}_{output_dir.name}',
        data_root=data_root,
        use_llm=use_llm,
    )

    # 2. Deterministic execution → curated.h5ad (per paper)
    data_dir = data_root / f'{benchmark_type}_{output_dir.name}'
    paper_slugs = [d.name for d in sorted(data_dir.iterdir())
                   if d.is_dir() and not d.name.startswith('_') and (d / 'curation_plan.json').exists()]

    executor_curated = 0
    executor_failed = 0
    for slug in paper_slugs:
        paper_dir = data_dir / slug
        ep_file = paper_dir / 'curation_plan.json'
        if not ep_file.exists():
            continue
        try:
            ep = json.loads(ep_file.read_text(encoding='utf-8'))
            executor = CurationExecutor(paper_dir=paper_dir, execution_plan=ep)
            exc_result = executor.run()
            if exc_result.get('status') == 'completed':
                executor_curated += 1
            else:
                executor_failed += 1
        except Exception as exc:
            print(f'  ⚠️  [{slug[:40]}] Executor error: {exc}')
            executor_failed += 1

    # 3. Anti-hallucination validation (per paper)
    validated = 0
    validation_errors = 0
    for slug in paper_slugs:
        paper_dir = data_dir / slug
        curated_file = paper_dir / 'unpacked_data' / 'curated.h5ad'
        if not curated_file.exists():
            curated_file = paper_dir / 'curated.h5ad'
        if not curated_file.exists():
            continue
        try:
            v_result = validate_curated_h5ad(str(curated_file))
            if v_result.get('valid', False):
                validated += 1
            else:
                validation_errors += 1
        except Exception as exc:
            print(f'  ⚠️  [{slug[:40]}] Validator error: {exc}')
            validation_errors += 1

    summary['stages']['02_agent_curator'] = {
        'status': 'completed',
        'total_papers': result.get('total_papers', 0),
        'papers_with_plan': result.get('papers_with_plan', 0),
        'curated': executor_curated,
        'curated_failed': executor_failed,
        'validated': validated,
        'validation_errors': validation_errors,
    }
    save_summary(output_dir, summary)

    # ── Copy curation_plan.json per paper to output folder ──
    data_dir = data_root / f'{benchmark_type}_{output_dir.name}'
    curator_out = output_dir / '02_curator'
    curator_out.mkdir(parents=True, exist_ok=True)
    cur_summary_src = data_dir / '_curation_summary.json'
    if cur_summary_src.exists():
        import shutil
        shutil.copy2(str(cur_summary_src), str(curator_out / '_curation_summary.json'))
    for slug in paper_slugs:
        cp_src = data_dir / slug / 'curation_plan.json'
        if cp_src.exists():
            slug_out = curator_out / slug
            slug_out.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(cp_src), str(slug_out / 'curation_plan.json'))
    print(f'  📋 Curator outputs copied to: {curator_out}')

    return {'curated': executor_curated, 'failed': executor_failed,
            'validated': validated}


def run_stage_process_data(
    collected_data: Dict[str, Any],
    output_dir: Path,
    summary: Dict[str, Any],
    no_process: bool = False,
) -> Dict[str, Any]:
    """Stage 3: QC + preprocessing on curated.h5ad from Stage 2.

    Takes Stage 2's curated.h5ad output, runs sc-preprocessing
    (QC + normalize + HVG + PCA) to produce clean AnnData ready
    for downstream benchmark analysis.

    Formula: Stage 2 curated.h5ad  →  sc-preprocessing  →  processed.h5ad

    The benchmark analysis (e.g. sc-batch-integration) runs in Stage 6.
    """
    process_dir = stage_dir(output_dir, 3, 'process_data')
    process_dir.mkdir(parents=True, exist_ok=True)

    print(f'\n{"=" * 60}')
    print(f'  Stage 3: Preprocess Curated Data')
    print(f'{"=" * 60}')

    if no_process:
        print('  Data processing skipped (--no-process).')
        summary['stages']['03_process_data'] = {'status': 'skipped', 'reason': '--no-process flag'}
        save_summary(output_dir, summary)
        return {'processed': [], 'status': 'skipped'}

    bm_type = summary.get('metadata', {}).get('benchmark_type', '')
    benchmark_data_root = _OMICSCLAW_ROOT / 'benchmark_data' / f'{bm_type}_{output_dir.name}'

    if not benchmark_data_root.exists():
        print(f'  No benchmark data found at: {benchmark_data_root}')
        summary['stages']['03_process_data'] = {'status': 'skipped', 'reason': 'No benchmark data dir'}
        save_summary(output_dir, summary)
        return {'processed': [], 'status': 'skipped'}

# ── Use Processor (scanpy) to preprocess curated.h5ad → processed.h5ad ──
    bm_full = f'{bm_type}_{output_dir.name}'
    process_out = output_dir / '03_process_data'
    process_out.mkdir(parents=True, exist_ok=True)

    result = run_processor(
        benchmark_type=bm_full,
        data_root=_OMICSCLAW_ROOT / 'benchmark_data',
        output_dir=process_out,
    )

    processed = result.get('results', [])
    status = result.get('status', 'error')
    print(f'  Processor: {len(processed)} file(s) preprocessed → {process_out}')

    summary['stages']['03_process_data'] = {
        'status': 'completed',
        'output_dir': str(process_dir),
        'preprocessed': len(processed),
        'results': processed,
    }
    save_summary(output_dir, summary)
    return {'processed': processed, 'status': 'completed'}

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
    """Stage 4: reproduce paper — clone, install, execute, verify.

    Uses agent_reproduce for script-level reproduction (Stage 2a)
    and agent_curator's executor for h5ad→RDS bridge.
    """
    reproduce_dir = stage_dir(output_dir, 4, 'reproduce')
    reproduce_dir.mkdir(parents=True, exist_ok=True)

    print(f'\n{"=" * 60}')
    print(f'  Stage 4: Reproduce Paper')
    print(f'{"=" * 60}')

    # Step 1: Old-style reproduce (clone + install) — skip execution, keep for env setup
    reproduce_input = specific_input or repo_url or None
    if reproduce_input is None and collected_data.get('literature_results'):
        first = collected_data['literature_results'][0]
        reproduce_input = first.get('raw_text') or first.get('input')

    paper_metadata = None
    for lr in collected_data.get('literature_results', []):
        if lr.get('metadata') and lr.get('source') in ('llm_collector', 'pubmed', 'arxiv', 'github', 'google_scholar'):
            paper_metadata = lr.get('metadata', {})
            break

    # Only keep clone + install from old pipeline
    reproduce_result = run_reproduce(
        reproduce_input or '',
        'auto',
        repo_url,
        benchmark_type,
        paper_metadata=paper_metadata,
        output=reproduce_dir,
        no_clone=no_clone,
        no_install=no_install,
        no_run=True,  # skip old execution; use agent_reproduce instead
        clone_depth=clone_depth,
    )

    result_statuses = reproduce_result.get('result', {}).get('statuses', {})

    # Step 2: AgentReproduce for script-level execution + evaluation
    bm_type = summary.get('metadata', {}).get('benchmark_type', benchmark_type)
    # Use the same full path pattern as save_accepted_papers: {type}_{output_name}
    data_root = _OMICSCLAW_ROOT / 'benchmark_data'
    code_root = _OMICSCLAW_ROOT / 'benchmark_code'
    data_dir = data_root / f'{bm_type}_{output_dir.name}'

    # Discover all papers with execution_plan.json
    paper_slugs = []
    if data_dir.exists():
        for pdir in sorted(data_dir.iterdir()):
            if pdir.is_dir() and not pdir.name.startswith('_'):
                ep = pdir / 'execution_plan.json'
                if ep.exists():
                    paper_slugs.append(pdir.name)

    agent_results = []
    for paper_slug in paper_slugs:
        print(f'\n  ── AgentReproduce: {paper_slug[:55]} ──')
        from skills.agents.agent_reproduce.runner import run_agent_reproduce as run_agent_reproduce_v2

        ar_result = run_agent_reproduce_v2(
            paper_slug=paper_slug,
            data_root=data_root,
            code_root=code_root,
            benchmark_type=bm_type,
            max_fix_attempts=3,
        )
        agent_results.append(ar_result)
        score = ar_result.get('reproducibility', {}).get('score', 'N/A')
        pkg_count = len(ar_result.get('missing_packages', []))
        print(f'  → Score: {score}/100, Missing packages: {pkg_count}')

    summary['stages']['04_reproduce'] = {
        'status': 'completed',
        'output_dir': str(reproduce_dir),
        'clone_success': result_statuses.get('clone_success', False),
        'install_success': result_statuses.get('install_success', False),
        'agent_papers': len(paper_slugs),
        'agent_results': [
            {
                'paper': r.get('paper'),
                'status': r.get('status'),
                'score': r.get('reproducibility', {}).get('score'),
                'scripts': r.get('scripts_completed', 0),
                'total_scripts': r.get('total_scripts', 0),
            }
            for r in agent_results
        ],
    }
    save_summary(output_dir, summary)

    return reproduce_result






# ---------------------------------------------------------------------------
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
        # sc-preprocessing requires confirmed-preflight since we run non-interactively
        if skill_name == 'sc-preprocessing':
            cmd.append('--confirmed-preflight')
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
    """Stage 5: reproducibility evaluation — compute metrics & generate report."""
    eval_dir = stage_dir(output_dir, 5, 'reproducibility_evaluation')
    eval_dir.mkdir(parents=True, exist_ok=True)

    print(f'\n{"=" * 60}')
    print(f'  Stage 5: Reproducibility Evaluation')
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

    summary['stages']['05_reproducibility_evaluation'] = {
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
    """Stage 6: benchmark evaluation — apply benchmark metrics to reproduced data."""
    eval_dir = stage_dir(output_dir, 6, 'benchmark_evaluation')
    eval_dir.mkdir(parents=True, exist_ok=True)

    print(f'\n{"=" * 60}')
    print(f'  Stage 6: Benchmark Evaluation [{benchmark_type}]')
    print(f'{"=" * 60}')

    if no_evaluate:
        print('  Benchmark evaluation skipped (--no-evaluate).')
        summary['stages']['06_benchmark_evaluation'] = {
            'status': 'skipped', 'reason': '--no-evaluate flag',
        }
        save_summary(output_dir, summary)
        return {'status': 'skipped'}

    # Determine input directories from previous stages
    process_dir = stage_dir(output_dir, 3, 'process_data')
    reproduce_dir = stage_dir(output_dir, 4, 'reproduce')

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

    summary['stages']['06_benchmark_evaluation'] = {
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
    search_only: bool = False,
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
        'search_only': search_only,
        'started_at': datetime.now(timezone.utc).isoformat(),
    }

    collected_data: Dict[str, Any] = {}
    reproduce_result: Optional[Dict[str, Any]] = None

    # --- Stage 0: Dispatch ---
    if resume and is_stage_completed(output_dir, 0):
        print(f'  → Stage 0 already completed, resuming at stage 1.')
    else:
        collected_data = run_stage_dispatch(
            benchmark_type, query, specific_input,
            output_dir, no_download, summary, use_llm=use_llm,
            search_only=search_only,
        )
        mark_stage_completed(output_dir, 0)
        print(f'\nCHECKPOINT: Stage 0 (Dispatch) complete.')
        print(f'     Run with --resume to skip to Stage 1.\n')

    if search_only:
        summary['completed_at'] = datetime.now(timezone.utc).isoformat()
        save_summary(output_dir, summary)

        print(f'\n{"=" * 60}')
        print(f'  *** Stage 0 Search Complete')
        print(f'{"=" * 60}')
        print(f'  Summary: {output_dir / SUMMARY_FILE}')
        print(f'  Quality: {stage_dir(output_dir, 0, "benchmark_dispatch") / "stage0_quality_summary.md"}')
        return {
            'summary_path': str(output_dir / SUMMARY_FILE),
            'quality_summary_path': str(stage_dir(output_dir, 0, 'benchmark_dispatch') / 'stage0_quality_summary.json'),
            'output_dir': str(output_dir),
            'search_only': True,
        }

    # --- Stage 1: Process downloaded data ---
    # --- Stage 1: Preflight / AgentScanner ---
    if resume and is_stage_completed(output_dir, 1):
        print(f'  → Stage 1 already completed, resuming at stage 2.')
    else:
        data_root = _OMICSCLAW_ROOT / 'benchmark_data'
        run_stage_preflight(benchmark_type, output_dir, summary, use_llm=use_llm)
        mark_stage_completed(output_dir, 1)
        print(f'\nCHECKPOINT: Stage 1 (Preflight) complete.')
        print(f'     Run with --resume to skip to Stage 2.\n')

    # --- Stage 2: Curator (detect + convert + validate) ---
    if resume and is_stage_completed(output_dir, 2):
        print(f'  → Stage 2 already completed, resuming at stage 3.')
    else:
        data_root = _OMICSCLAW_ROOT / 'benchmark_data'
        run_stage_curate(
            benchmark_type, output_dir, data_root, summary, use_llm=use_llm,
        )
        mark_stage_completed(output_dir, 2)
        print(f'\nCHECKPOINT: Stage 2 (Curator) complete.')
        print(f'     Run with --resume to skip to Stage 3.\n')

    # --- Stage 3: Process downloaded data (sc-standardize + sc-preprocessing) ---
    if resume and is_stage_completed(output_dir, 3):
        print(f' → Stage 3 already completed, resuming at stage 4.')
    else:
        run_stage_process_data(
            collected_data, output_dir, summary, no_process=no_process,
        )
        mark_stage_completed(output_dir, 3)
        print(f'\nCHECKPOINT: Stage 3 (Process Data) complete.')
        print(f'     Run with --resume to skip to Stage 4.\n')

    # --- Stage 4: Reproduce Paper ---
    if resume and is_stage_completed(output_dir, 4):
        print(f'  → Stage 4 already completed, resuming at stage 5.')
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
        mark_stage_completed(output_dir, 4)
        print(f'\nCHECKPOINT: Stage 4 (Reproduce) complete.')
        print(f'     Run with --resume to skip to Stage 5.\n')

    # --- Stage 5: Reproducibility Evaluation ---
    if resume and is_stage_completed(output_dir, 5):
        print(f'  → Stage 5 already completed.')
    else:
        # Load reproduce result from disk if needed
        if reproduce_result is None and is_stage_completed(output_dir, 4):
            reproduce_dir = stage_dir(output_dir, 4, 'reproduce')
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
        mark_stage_completed(output_dir, 5)
        print(f'\nCHECKPOINT: Stage 5 (Eval) complete.')
        print(f'     Run with --resume to skip to Stage 6.\n')

    # --- Stage 6: Benchmark Evaluation ---
    if resume and is_stage_completed(output_dir, 6):
        print(f'  → Stage 6 already completed.')
    else:
        process_dir = output_dir / '03_process_data'
        eval_out = output_dir / '06_benchmark_evaluation'
        if process_dir.exists():
            run_benchmark_evaluation(
                benchmark_type=summary.get('metadata', {}).get('benchmark_type', benchmark_type),
                output_dir=output_dir,
                process_data_dir=process_dir,
            )
            mark_stage_completed(output_dir, 6)

    # --- Finalize ---
    summary['completed_at'] = datetime.now(timezone.utc).isoformat()
    save_summary(output_dir, summary)

    final_report = output_dir / REPORT_FILE
    print(f'\n{"=" * 60}')
    print(f'  *** Benchmark Suite Complete')
    print(f'{"=" * 60}')
    print(f'  Summary: {output_dir / SUMMARY_FILE}')
    print(f'  Report:  {final_report}')
    print(f'  Stages completed:')
    for stage_key, stage_info in sorted(summary.get('stages', {}).items()):
        status = stage_info.get('status', 'unknown')
        emoji = '***' if status == 'completed' else '⏭️' if status == 'skipped' else '❌'
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
    parser.add_argument('--search-only', action='store_true',
                        help='Only run Stage 0 search/reporting; do not save accepted papers, clone code, or run later stages')
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
            search_only=args.search_only,
        )
        return 0
    except Exception as exc:
        print(f'Error: {exc}', file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
