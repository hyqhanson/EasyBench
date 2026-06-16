"""Run Stage 1 (process_data) directly against benchmark_data/."""
import sys
sys.path.insert(0, '.')
sys.path.insert(0, 'skills')

from pathlib import Path
from orchestrator.benchmark_suite.benchmark_suite import (
    stage_dir, _discover_papers, _process_one_paper,
    save_summary, load_summary,
)

# ── Config ──
BENCHMARK_TYPE = 'integration'
OUTPUT_DIR = Path('stage1_run')

# ── Run ──
output_dir = OUTPUT_DIR.resolve()
output_dir.mkdir(parents=True, exist_ok=True)
process_dir = stage_dir(output_dir, 1, 'process_data')
process_dir.mkdir(parents=True, exist_ok=True)

summary = load_summary(output_dir)
summary['metadata'] = {'benchmark_type': BENCHMARK_TYPE}

benchmark_data_root = Path('benchmark_data') / BENCHMARK_TYPE
papers = _discover_papers(benchmark_data_root)

print(f'Papers with data: {len(papers)}')
for i, p in enumerate(papers):
    print(f'  {i+1}. {p["slug"][:60]} — {len(p["data_files"])} data file(s)')

if not papers:
    print('No papers found. Aborting.')
    sys.exit(0)

print(f'\nProcessing all {len(papers)} papers...\n')

processed = []
for paper in papers:
    result = _process_one_paper(
        paper, process_dir, BENCHMARK_TYPE, output_dir, summary
    )
    processed.append(result)

summary['stages']['01_process_data'] = {
    'status': 'completed',
    'output_dir': str(process_dir),
    'papers_processed': len(processed),
}
save_summary(output_dir, summary)

print(f'\nDone. Output: {process_dir}')
