"""Test Stage 2 AgentReproduce on autoinhibitory-feedback paper."""
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

print('\n=== REPRODUCE RESULT ===')
print('Status:', result.get('status'))
print('Monitor decision:', result.get('monitor_decision', {}).get('action'))
print('Attempts:', result.get('attempts'))
print('Entry point:', str(result.get('entry_point', ''))[:100])

if result.get('stdout'):
    print('\nOutput (last 500 chars):')
    print(result['stdout'][-500:])
