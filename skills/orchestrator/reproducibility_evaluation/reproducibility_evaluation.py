#!/usr/bin/env python3
"""Reproducibility evaluation skill for OmicsClaw.

Evaluates reproduce-paper results, generates comparison
reports, and saves benchmark metric catalog artifacts for each supported
benchmark type.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add root and skills path for imports if needed
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / 'skills'))

METRICS_CATALOG_NAME = 'metrics_catalog.json'


def load_metrics_catalog(catalog_path: Path) -> Dict[str, Any]:
    if not catalog_path.exists():
        raise FileNotFoundError(f'Metrics catalog not found: {catalog_path}')
    return json.loads(catalog_path.read_text())


def save_metrics_catalog(output_path: Path, catalog: Dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(catalog, indent=2))


def load_reproducibility_result(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text())
    if isinstance(data, dict) and 'result' in data and isinstance(data['result'], dict):
        data = data['result']
    return data


def safe_bool(value: Any) -> bool:
    return bool(value) if value is not None else False


def compute_reproducibility_score(statuses: Dict[str, Any]) -> Optional[float]:
    weights = {
        'clone_success': 0.35,
        'install_success': 0.35,
        'run_success': 0.30,
    }
    score = 0.0
    total = 0.0
    for name, weight in weights.items():
        if name in statuses:
            total += weight
            score += weight * (1.0 if safe_bool(statuses.get(name)) else 0.0)
    if total <= 0:
        return None
    return round(score / total, 4)


def collect_commands_count(result: Dict[str, Any]) -> int:
    total = 0
    for env in result.get('environment', []) or []:
        total += len(env.get('commands', []))
    for repo in result.get('repository_results', []) or []:
        total += len(repo.get('commands', []))
        run_info = repo.get('run')
        if isinstance(run_info, dict):
            total += len(run_info.get('commands', []))
    return total


def collect_env_file_count(result: Dict[str, Any]) -> int:
    total = 0
    for env in result.get('environment', []) or []:
        total += len(env.get('env_files', []))
    return total


def collect_dataset_counts(plan: Dict[str, Any]) -> Dict[str, int]:
    datasets = plan.get('datasets', {}) or {}
    geo = len(datasets.get('geo_accessions', {}).get('gse', []))
    sra = len(datasets.get('sra_accessions', []))
    cellxgene = len(datasets.get('cellxgene_accessions', []))
    return {
        'geo': geo,
        'sra': sra,
        'cellxgene': cellxgene,
        'total': geo + sra + cellxgene,
        'types_present': sum(1 for count in (geo, sra, cellxgene) if count > 0),
    }


def compute_metrics_for_result(result: Dict[str, Any], catalog: Dict[str, Any]) -> Dict[str, Any]:
    plan = result.get('plan', {}) or {}
    statuses = result.get('statuses', {}) or {}
    extracted_steps = plan.get('extracted_steps', {}) or {}

    dataset_counts = collect_dataset_counts(plan)
    repo_count = len(plan.get('repositories', []) or [])
    method_sections = len(extracted_steps.get('method_sections', []) or [])
    code_snippets = len(extracted_steps.get('code_snippets', []) or [])
    commands_count = collect_commands_count(result)
    env_files_count = collect_env_file_count(result)

    benchmark_type = plan.get('benchmark_type') or 'generic'
    benchmark_entry = catalog.get('benchmark_types', {}).get(benchmark_type, {})
    baseline_metrics = benchmark_entry.get('metrics', [])

    metrics = {
        'benchmark_type': benchmark_type,
        'benchmark_description': benchmark_entry.get('description', 'Generic benchmark evaluation'),
        'repository_count': repo_count,
        'repository_found': repo_count > 0,
        'clone_success': safe_bool(statuses.get('clone_success')),
        'install_success': safe_bool(statuses.get('install_success')),
        'run_success': safe_bool(statuses.get('run_success')),
        'reproducibility_score': compute_reproducibility_score(statuses),
        'failure_phase': statuses.get('failure_phase'),
        'failure_details': statuses.get('failure_details', []),
        'dataset_counts': dataset_counts,
        'method_sections_count': method_sections,
        'code_snippets_count': code_snippets,
        'commands_count': commands_count,
        'environment_file_count': env_files_count,
        'plan_steps_count': len(plan.get('steps', []) or []),
        'baseline_metrics': [m['name'] for m in baseline_metrics],
    }

    metrics['computed_metrics'] = {
        'repository_found': metrics['repository_found'],
        'clone_success': metrics['clone_success'],
        'install_success': metrics['install_success'],
        'run_success': metrics['run_success'],
        'reproducibility_score': metrics['reproducibility_score'],
        'dataset_total': dataset_counts['total'],
        'dataset_type_count': dataset_counts['types_present'],
        'method_sections_count': method_sections,
        'code_snippets_count': code_snippets,
        'commands_count': commands_count,
        'environment_file_count': env_files_count,
        'plan_steps_count': metrics['plan_steps_count'],
    }

    metrics['missing_baseline_metrics'] = [
        name for name in metrics['baseline_metrics'] if name not in metrics['computed_metrics']
    ]
    metrics['metric_suggestions'] = suggest_new_metrics(benchmark_type, metrics, catalog)

    return metrics


def suggest_new_metrics(benchmark_type: str, metrics: Dict[str, Any], catalog: Dict[str, Any]) -> List[Dict[str, Any]]:
    suggestions: List[Dict[str, Any]] = []
    benchmark_entry = catalog.get('benchmark_types', {}).get(benchmark_type, {})
    baseline = benchmark_entry.get('metrics', [])
    baseline_names = {metric['name'] for metric in baseline}

    missing = [m for m in baseline if m['name'] not in metrics['computed_metrics']]
    for metric in missing:
        suggestions.append({
            'name': metric['name'],
            'reason': f"Not computed from reproducibility result; relevant for {benchmark_type} benchmarks.",
            'description': metric.get('description'),
        })

    if not metrics['repository_found']:
        suggestions.append({
            'name': 'repository_discoverability',
            'reason': 'The reproducibility output did not include a discovered repository URL.',
            'description': 'Measure the ability to locate a code repository from a paper or metadata source.',
        })

    if metrics['clone_success'] is False and metrics['repository_found']:
        suggestions.append({
            'name': 'clone_reliability',
            'reason': 'Repository was identified but cloning failed.',
            'description': 'Track the ability to clone target repositories across network and access control conditions.',
        })

    if metrics['install_success'] is False:
        suggestions.append({
            'name': 'environment_build_success',
            'reason': 'Environment installation did not succeed or was skipped.',
            'description': 'Track whether the repository environment can be built reproducibly from declared dependencies.',
        })

    if metrics['run_success'] is False:
        suggestions.append({
            'name': 'execution_coverage',
            'reason': 'Test execution did not complete successfully.',
            'description': 'Measure how much of the published reproduction workflow can be executed end-to-end.',
        })

    if metrics['dataset_counts']['total'] == 0:
        suggestions.append({
            'name': 'data_availability',
            'reason': 'No dataset accessions were discovered in the reproducibility input.',
            'description': 'Track whether published benchmark datasets can be automatically identified and retrieved.',
        })

    return suggestions


def build_report(results: List[Dict[str, Any]], catalog: Dict[str, Any], include_suggestions: bool) -> str:
    report: List[str] = [
        '# Benchmark Evaluation Report',
        '',
    ]

    for index, metrics in enumerate(results, start=1):
        report.extend([
            f'## Result {index}: {metrics.get("benchmark_type", "generic").title()} Benchmark',
            '',
            f'- **Repository count**: {metrics["repository_count"]}',
            f'- **Repository found**: {metrics["repository_found"]}',
            f'- **Clone success**: {metrics["clone_success"]}',
            f'- **Install success**: {metrics["install_success"]}',
            f'- **Run success**: {metrics["run_success"]}',
            f'- **Reproducibility score**: {metrics["reproducibility_score"]}',
            f'- **Datasets found**: {metrics["dataset_counts"]["total"]}',
            f'- **Dataset types present**: {metrics["dataset_counts"]["types_present"]}',
            f'- **Method sections extracted**: {metrics["method_sections_count"]}',
            f'- **Code snippets extracted**: {metrics["code_snippets_count"]}',
            f'- **Environment files discovered**: {metrics["environment_file_count"]}',
            f'- **Commands collected**: {metrics["commands_count"]}',
            f'- **Plan steps**: {metrics["plan_steps_count"]}',
            f'- **Failure phase**: {metrics["failure_phase"]}',
            '',
            '### Baseline Metrics',
        ])

        if metrics['baseline_metrics']:
            report.extend(f'- {name}' for name in metrics['baseline_metrics'])
        else:
            report.append('- No benchmark-specific baseline metrics available.')

        if include_suggestions and metrics['metric_suggestions']:
            report.append('')
            report.append('### Suggested New Metrics')
            for suggestion in metrics['metric_suggestions']:
                report.extend([
                    f'- **{suggestion["name"]}**: {suggestion["reason"]}',
                    f'  - {suggestion["description"]}',
                ])
        report.append('')

    if len(results) > 1:
        report.append('## Comparison Summary')
        report.append('')
        report.append('| Result | Type | Reproducibility | Clone | Install | Run | Datasets |')
        report.append('|---|---|---|---|---|---|---|')
        for index, metrics in enumerate(results, start=1):
            report.append(
                '| {} | {} | {} | {} | {} | {} | {} |'.format(
                    index,
                    metrics.get('benchmark_type', 'generic'),
                    metrics.get('reproducibility_score'),
                    metrics.get('clone_success'),
                    metrics.get('install_success'),
                    metrics.get('run_success'),
                    metrics['dataset_counts']['total'],
                )
            )
        report.append('')

    return '\n'.join(report)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Reproducibility evaluation skill')
    parser.add_argument('--result-files', nargs='+', required=False,
                        help='One or more reproduce-paper result.json files to evaluate')
    parser.add_argument('--output', required=True, help='Output directory for benchmark evaluation results')
    parser.add_argument('--generate-metrics-catalog', action='store_true',
                        help='Write the internal metrics_catalog.json to the output directory')
    parser.add_argument('--include-suggestions', action='store_true',
                        help='Include new metric suggestions in the generated report')
    parser.add_argument('--catalog-path', default=None,
                        help='Optional path to the metrics catalog file')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    catalog_path = Path(args.catalog_path) if args.catalog_path else Path(__file__).resolve().parent / METRICS_CATALOG_NAME
    catalog = load_metrics_catalog(catalog_path)

    output_catalog_path = output_dir / METRICS_CATALOG_NAME
    save_metrics_catalog(output_catalog_path, catalog)

    if args.generate_metrics_catalog:
        print(f'Generated metrics catalog at: {output_catalog_path}')
        return 0

    if not args.result_files:
        raise ValueError('At least one --result-files path must be provided')

    results = [load_reproducibility_result(Path(path)) for path in args.result_files]
    evaluated = [compute_metrics_for_result(result, catalog) for result in results]

    metrics_path = output_dir / 'benchmark_metrics.json'
    report_path = output_dir / 'benchmark_report.md'
    metrics_path.write_text(json.dumps({'results': evaluated}, indent=2))
    report_path.write_text(build_report(evaluated, catalog, include_suggestions=args.include_suggestions))

    print(f'Benchmark evaluation metrics written to: {metrics_path}')
    print(f'Benchmark evaluation report written to: {report_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
