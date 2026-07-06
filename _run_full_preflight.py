"""Run full agent_preflight on all integration_e2e_test papers with LLM."""
import os, sys
from pathlib import Path

# Ensure project root is on sys.path (Critical when running from arbitrary CWD)
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / 'skills'))

# Load API key from user environment
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

from pathlib import Path
from skills.agents.agent_preflight.runner import run_agent_preflight

result = run_agent_preflight(
    benchmark_type='integration_e2e_test',
    data_root=Path('benchmark_data'),
    code_root=Path('benchmark_code'),
    use_llm=True,
)

print()
print('=== PREFLIGHT SUMMARY ===')
print('Total papers:', result['total_papers'])
status_counts = result.get('status_counts', {})
for status, count in sorted(status_counts.items()):
    print(f'  {status}: {count}')
print()
for p in result['papers']:
    slug = p['slug'][:50]
    status = p['status']
    conf = p['confidence']
    feasible = p.get('feasible_analysis', '')[:80]
    print(f'  [{status:18s}] {slug} | conf={conf}')
    if feasible:
        print(f'    → {feasible}')
