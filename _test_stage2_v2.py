"""Test Stage 2 with full script pipeline and reproduce isolation."""
import sys, json
sys.path.insert(0, 'skills')
from pathlib import Path
from skills.agents.agent_reproduce.runner import run_agent_reproduce

result = run_agent_reproduce(
    paper_slug='autoinhibitory-feedback-preserves-intestinal-stem-cell-s44318-026-00808-x',
    data_root=Path('benchmark_data'),
    code_root=Path('benchmark_code'),
    benchmark_type='integration',
    max_fix_attempts=3,
)

print()
print('=== REPRODUCE RESULT ===')
print('Status:', result.get('status'))
print('Monitor:', result.get('monitor_decision', {}).get('action'))
print('Scripts:', result.get('entry_scripts'))
print('Completed:', result.get('scripts_completed'), '/', result.get('total_scripts'))

output = result.get('output', '')
if output:
    print('\nOutput (last 800 chars):')
    print(output[-800:])
    print('\nFix attempts:', len(fixes))
