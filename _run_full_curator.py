"""Run full AgentCurator on all integration_e2e_test papers with LLM."""
import os, sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / 'skills'))

# Load API key
_key = os.environ.get('DEEPSEEK_API_KEY', '')
if not _key:
    try:
        from subprocess import check_output
        _key = check_output(
            'powershell -c "[Environment]::GetEnvironmentVariable(\'DEEPSEEK_API_KEY\', \'User\')"',
            shell=True, text=True
        ).strip()
        if _key:
            os.environ['DEEPSEEK_API_KEY'] = _key
    except Exception:
        pass

from skills.agents.agent_curator.curator_runner import run_agent_curator

result = run_agent_curator(
    benchmark_type='integration_e2e_test',
    data_root=Path('benchmark_data'),
    use_llm=True,
    execute=True,   # 🔧 Also run CurationExecutor to produce curated.h5ad
)

print()
print('=== CURATION SUMMARY ===')
print('Total papers:', result['total_papers'])
print('Papers with plan:', result['papers_with_plan'])
print('Papers with error:', result['papers_with_error'])
print()
for p in result['papers']:
    slug = p['slug'][:50]
    err = p.get('error') or 'ok'
    ds = p['total_datasets']
    expr = p['expression_datasets']
    print(f'  [{err:4s}] {slug} | {ds} datasets ({expr} expression)')
