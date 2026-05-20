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
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / 'skills'))

from orchestrator.benchmark_dispatch.benchmark_dispatch import (
    collect_benchmark_data,
    generate_search_queries,
    download_collected_datasets,
    generate_workflow_plan,
    save_results,
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

    if not no_download:
        print('\n  Downloading candidate datasets...')
        collected_data['download_results'] = download_collected_datasets(
            collected_data, dispatch_dir / 'downloaded_data',
        )

    workflow_plan = generate_workflow_plan(benchmark_type, collected_data)
    save_results(workflow_plan, collected_data, dispatch_dir)

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
    """Stage 1: process downloaded datasets through OmicsClaw sc tools."""
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

    # Find downloaded h5ad/mtx files
    dispatch_dir = stage_dir(output_dir, 0, 'benchmark_dispatch')
    data_dirs = list(dispatch_dir.glob('downloaded_data/**/*.h5ad'))
    data_dirs += list(dispatch_dir.glob('downloaded_data/**/*.mtx'))
    data_dirs += list(dispatch_dir.glob('downloaded_data/**/matrix.mtx*'))

    if not data_dirs:
        print('  No downloaded data files found. Run without --no-download first.')
        summary['stages']['01_process_data'] = {'status': 'skipped', 'reason': 'No data files'}
        save_summary(output_dir, summary)
        return {'processed': [], 'status': 'skipped'}

    processed = []
    for data_path in data_dirs[:3]:  # process up to 3 datasets
        dataset_name = data_path.parent.name
        dataset_out = process_dir / dataset_name
        dataset_out.mkdir(parents=True, exist_ok=True)

        # Determine which OmicsClaw skill to use based on file type
        suffix = data_path.suffix.lower()
        if suffix == '.h5ad':
            # Try running sc-preprocessing on the h5ad
            print(f'  Processing: {data_path.name}')
            skill_result = _run_omicsclaw_skill(
                'sc-preprocessing',
                input_path=str(data_path),
                output_dir=str(dataset_out),
            )
            processed.append({
                'file': str(data_path),
                'skill': 'sc-preprocessing',
                'success': skill_result.get('success', False),
                'output': str(dataset_out),
            })

            # If preprocessing succeeded, try benchmark-relevant skill
            if skill_result.get('success'):
                processed_h5ad = dataset_out / 'processed.h5ad'
                if processed_h5ad.exists():
                    # Determine which skill based on benchmark type
                    bm_type = summary.get('metadata', {}).get('benchmark_type', '')
                    analysis_skill = _benchmark_to_skill(bm_type)
                    if analysis_skill:
                        analysis_out = dataset_out / analysis_skill
                        analysis_out.mkdir(exist_ok=True)
                        print(f'    → Running {analysis_skill}...')
                        _run_omicsclaw_skill(
                            analysis_skill,
                            input_path=str(processed_h5ad),
                            output_dir=str(analysis_out),
                        )

        elif suffix == '.mtx' or 'matrix.mtx' in str(data_path):
            print(f'  Found mtx data (needs conversion): {data_path.parent.name}')
            processed.append({
                'file': str(data_path),
                'skill': None,
                'success': False,
                'note': 'MTX format requires scanpy read before processing',
            })

    summary['stages']['01_process_data'] = {
        'status': 'completed',
        'output_dir': str(process_dir),
        'datasets_processed': len(processed),
        'results': processed,
    }
    save_summary(output_dir, summary)
    return {'processed': processed, 'status': 'completed'}


def _run_omicsclaw_skill(skill_name: str, input_path: str, output_dir: str) -> Dict:
    """Run an OmicsClaw skill via subprocess (same pattern as omicsclaw.py)."""
    import subprocess as sp
    import sys as _sys
    try:
        cmd = [_sys.executable, '-m', f'skills.singlecell.scrna.{skill_name}.{skill_name.replace("-", "_")}',
               '--input', input_path, '--output', output_dir]
        completed = sp.run(cmd, capture_output=True, text=True, timeout=1800)
        return {
            'success': completed.returncode == 0,
            'stdout': completed.stdout[-500:],
            'stderr': completed.stderr[-500:],
        }
    except Exception as exc:
        return {'success': False, 'error': str(exc)}


def _benchmark_to_skill(benchmark_type: str) -> Optional[str]:
    """Map benchmark type to OmicsClaw analysis skill."""
    mapping = {
        'integration': 'sc-batch-integration',
        'clustering': 'sc-clustering',
        'annotation': 'sc-cell-annotation',
        'trajectory': 'sc-pseudotime',
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
