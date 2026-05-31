#!/usr/bin/env python3
"""Paper reproduction automation skill for OmicsClaw.

Wraps a literature reproducibility workflow:
- parse paper text or metadata
- discover repository links (LLM-assisted + regex + metadata from llm_collector)
- build environment artifacts
- map paper methodology steps to reproducible commands
- execute paper-specific reproduction (not just pytest)
- verify outputs against expected artefacts
- save plan and result logs
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Add root and skills path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / 'skills'))

from literature.core.extractor import extract_metadata
from literature.core.parser import parse_input
from literature.core.steps import extract_paper_steps


# ---------------------------------------------------------------------------
# Regex fallback patterns for repository URL discovery
# ---------------------------------------------------------------------------

GITHUB_PATTERN = r'https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:[\.\s/]|$)'
GITLAB_PATTERN = r'https?://gitlab\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:[\.\s/]|$)'
BITBUCKET_PATTERN = r'https?://bitbucket\.org/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:[\.\s/]|$)'

# ---------------------------------------------------------------------------
# LLM helpers (graceful fallback when LLM unavailable)
# ---------------------------------------------------------------------------


def _call_llm(directive: str, system_prompt: str, temperature: float = 0.3) -> Optional[str]:
    try:
        from omicsclaw.autoagent.llm_client import call_llm as _call
        return _call(directive, system_prompt=system_prompt, temperature=temperature, max_tokens=2048)
    except Exception:
        return None


def _parse_llm_json(raw: str) -> Optional[Any]:
    raw = raw.strip()
    if raw.startswith('```'):
        raw = raw.split('\n', 1)[1]
    if raw.endswith('```'):
        raw = raw.rsplit('```', 1)[0]
    start = min((p for p in (raw.find('{'), raw.find('[')) if p != -1), default=-1)
    if start == -1:
        try:
            return json.loads(raw)
        except Exception:
            return None
    open_char = raw[start]
    close_char = '}' if open_char == '{' else ']'
    depth = 0
    for idx in range(start, len(raw)):
        ch = raw[idx]
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start:idx + 1])
                except Exception:
                    return None
    return None


def run_reproduce(
    input_value: str,
    input_type: str = 'auto',
    repo_url: Optional[str] = None,
    benchmark_type: Optional[str] = None,
    paper_metadata: Optional[Dict[str, Any]] = None,
    output: Path | str = '.',
    no_clone: bool = False,
    no_install: bool = False,
    no_run: bool = False,
    clone_depth: int = 1,
) -> Dict[str, object]:
    """Run the full paper reproduction workflow.

    Parameters
    ----------
    input_value:
        Paper text, DOI, PubMed ID, URL, etc.
    repo_url:
        Explicit repository URL (takes priority over discovery).
    paper_metadata:
        Metadata dict from llm_collector (includes ``github_repos``,
        ``methods_summary``, ``code_snippets``, etc.).  Used to
        augment repository discovery and reproduction planning.
    """
    output_dir = Path(output).resolve()
    reproducibility_dir = output_dir / 'reproducibility'
    reproducibility_dir.mkdir(parents=True, exist_ok=True)

    logs_dir = reproducibility_dir / 'logs'
    logs_dir.mkdir(parents=True, exist_ok=True)

    # ----- parse input -------------------------------------------------
    text, detected_type = parse_input(input_value, input_type)
    if not text or text.startswith('Error'):
        raise RuntimeError(f"Failed to parse input: {text}")

    metadata = extract_metadata(text, benchmark_type)
    extracted_steps = extract_paper_steps(text)

    # ----- discover repositories (LLM + metadata + regex) ---------------
    metadata = metadata or {}
    paper_metadata = paper_metadata or {}
    repositories = discover_repositories(text, repo_url, paper_metadata)

    # ----- build a paper-aware reproduction plan ------------------------
    repro_plan = build_reproduction_plan(
        text=text,
        repositories=repositories,
        extracted_steps=extracted_steps,
        paper_metadata=paper_metadata,
        benchmark_type=benchmark_type,
    )

    plan = {
        'input': input_value,
        'input_type': detected_type,
        'benchmark_type': benchmark_type,
        'repositories': repositories,
        'datasets': metadata,
        'extracted_steps': extracted_steps,
        'reproduction_plan': repro_plan.get('commands', []),
        'expected_outputs': repro_plan.get('expected_outputs', []),
        'steps': [
            'parse_paper',
            'discover_repositories',
            'build_environment',
            'execute_reproduction',
            'verify_outputs',
        ],
        'environment': {
            'no_clone': no_clone,
            'no_install': no_install,
            'no_run': no_run,
            'clone_depth': clone_depth,
        },
    }

    result: Dict[str, object] = {
        'plan': plan,
        'statuses': {
            'parsed': True,
            'repositories_found': bool(repositories),
            'clone_success': False,
            'install_success': False,
            'run_success': False,
            'outputs_verified': False,
            'failure_phase': None,
            'failure_details': [],
        },
        'output_verification': {},
        'logs': {},
        'repository_results': [],
        'extracted_steps': extracted_steps,
    }

    commands: List[str] = []
    clones: List[Dict] = []

    # ----- Stage A: clone ---------------------------------------------------
    if not repositories:
        result['statuses']['failure_phase'] = 'no_repository'
        result['statuses']['failure_details'].append(
            'No repository URLs discovered from input, metadata, or explicit URL.'
        )

    if repositories and not no_clone:
        clones = clone_repositories(
            repositories,
            reproducibility_dir / 'cloned_repos',
            clone_depth,
            logs_dir,
        )
        result['statuses']['clone_success'] = any(c['cloned'] for c in clones)
        result['repository_results'] = clones
        commands.extend(command for c in clones for command in c.get('commands', []))
        if not result['statuses']['clone_success']:
            result['statuses']['failure_phase'] = 'clone'
            result['statuses']['failure_details'].extend(
                c.get('error') for c in clones if c.get('error')
            )
    elif repositories and no_clone:
        result['statuses']['failure_details'].append('Clone step skipped by configuration.')

    # ----- Stage B: build environment ---------------------------------------
    env_results = build_environment_artifacts(
        clones, reproducibility_dir, logs_dir, no_install,
    )
    result['statuses']['install_success'] = any(
        env.get('install_success', False) or (no_install and env.get('env_files'))
        for env in env_results
    )
    if result['statuses']['clone_success'] and not result['statuses']['install_success'] and env_results:
        result['statuses']['failure_phase'] = result['statuses']['failure_phase'] or 'install'
        result['statuses']['failure_details'].extend(
            err['stderr'] for env in env_results
            for err in env.get('install_results', [])
            if err.get('returncode') != 0
        )
    result['environment'] = env_results
    commands.extend(command for env in env_results for command in env.get('commands', []))

    for env in env_results:
        for repo in clones:
            if repo['repo_url'] == env['repo_url']:
                repo['environment'] = env
                break

    # ----- Stage C: execute paper-specific reproduction ---------------------
    run_results: List[Dict] = []
    if not no_run and clones:
        run_results = execute_reproduction(
            clones=clones,
            repro_plan=repro_plan,
            logs_dir=logs_dir,
        )
        result['statuses']['run_success'] = any(
            r['executed'] and r.get('returncode') == 0 for r in run_results
        )
        result['statuses']['test_executed'] = any(r['executed'] for r in run_results)
        commands.extend(command for r in run_results for command in r.get('commands', []))

        for run in run_results:
            for repo in clones:
                if repo['repo_url'] == run['repo_url']:
                    repo['run'] = run
                    break
        if result['statuses']['test_executed'] and not result['statuses']['run_success']:
            result['statuses']['failure_phase'] = result['statuses']['failure_phase'] or 'run'
            result['statuses']['failure_details'].extend(
                run.get('reason', '') for run in run_results if run.get('returncode') != 0
            )
    elif no_run:
        result['statuses']['failure_details'].append('Run step skipped by configuration.')

    # ----- Stage D: verify expected outputs ---------------------------------
    if result['statuses']['run_success']:
        verification = verify_outputs(
            clones=clones,
            expected_outputs=repro_plan.get('expected_outputs', []),
        )
        result['output_verification'] = verification
        result['statuses']['outputs_verified'] = verification.get('all_found', False)

    # Only mark as success when outputs were actually verified
    if result['statuses']['run_success'] and not result['statuses']['outputs_verified']:
        result['statuses']['failure_phase'] = result['statuses']['failure_phase'] or 'verify'
        result['statuses']['failure_details'].append(
            'Reproduction ran but expected outputs were not found.'
        )

    result['repository_results'] = clones

    save_plan(reproducibility_dir, plan)
    save_result(reproducibility_dir, result)
    write_commands_script(reproducibility_dir, commands)
    write_report(reproducibility_dir, plan, result)

    return {
        'plan_path': str(reproducibility_dir / 'plan.json'),
        'result_path': str(reproducibility_dir / 'result.json'),
        'commands_path': str(reproducibility_dir / 'commands.sh'),
        'report_path': str(reproducibility_dir / 'report.md'),
        'result': result,
        'repo_url': repo_url or (repositories[0] if repositories else None),
        'status': 'success' if result['statuses']['failure_phase'] is None else 'failure',
    }


def main():
    parser = argparse.ArgumentParser(description='Automate paper reproduction workflow')
    parser.add_argument('--input', required=True,
                        help='URL, DOI, PubMed ID, PDF path, or paper text')
    parser.add_argument('--input-type', default='auto',
                        choices=['auto', 'url', 'doi', 'pubmed', 'file', 'text'])
    parser.add_argument('--repo-url', help='Explicit code repository URL for reproduction')
    parser.add_argument('--benchmark-type', help='Optional benchmark type for context')
    parser.add_argument('--output', required=True, help='Output directory')
    parser.add_argument('--no-clone', action='store_true', help='Skip repository cloning')
    parser.add_argument('--no-install', action='store_true', help='Skip environment installation')
    parser.add_argument('--paper-metadata', help='Path to JSON file with paper metadata from llm_collector')
    parser.add_argument('--benchmark-type', help='Optional benchmark type for context')
    parser.add_argument('--output', required=True, help='Output directory')
    parser.add_argument('--no-clone', action='store_true', help='Skip repository cloning')
    parser.add_argument('--no-install', action='store_true', help='Skip environment installation')
    parser.add_argument('--no-run', action='store_true', help='Skip reproduction execution')
    parser.add_argument('--clone-depth', type=int, default=1, help='Git clone depth for repository checkout')

    args = parser.parse_args()

    # Load paper metadata if provided
    paper_metadata = None
    if args.paper_metadata:
        metadata_path = Path(args.paper_metadata)
        if metadata_path.exists():
            paper_metadata = json.loads(metadata_path.read_text())

    result = run_reproduce(
        args.input,
        args.input_type,
        args.repo_url,
        args.benchmark_type,
        paper_metadata=paper_metadata,
        output=args.output,
        no_clone=args.no_clone,
        no_install=args.no_install,
        no_run=args.no_run,
        clone_depth=args.clone_depth,
    )

    print(f"Reproducibility plan written to: {result['plan_path']}")
    print(f"Reproducibility result written to: {result['result_path']}")
    print(f"Reproducibility commands written to: {result['commands_path']}")
    print(f"Reproducibility report written to: {result['report_path']}")


# ---------------------------------------------------------------------------
# Repository discovery (LLM-assisted + metadata + regex fallback)
# ---------------------------------------------------------------------------


def discover_repositories(
    text: str,
    explicit_url: Optional[str] = None,
    paper_metadata: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Discover code repositories from paper text using multiple strategies.

    1. Explicit URL (CLI argument)
    2. ``paper_metadata`` from llm_collector (``github_repos``, ``arxiv_ids``, ``zenodo_records``)
    3. LLM extraction: ask the model to find repos from the full paper text
    4. Regex fallback: scan for github.com/gitlab.com/bitbucket.org URLs
    """
    repos: List[str] = []

    # 1. Explicit URL
    if explicit_url:
        repos.append(explicit_url.strip().rstrip('/'))

    # 2. Metadata from llm_collector
    pmeta = paper_metadata or {}
    for key in ('github_repos', 'arxiv_ids', 'zenodo_records'):
        entries = pmeta.get(key, [])
        if isinstance(entries, str):
            entries = [entries]
        for entry in entries:
            entry = str(entry).strip()
            if entry.startswith('http'):
                repos.append(entry.rstrip('/'))
            elif '/' in entry and entry.count('/') == 1:
                repos.append(f'https://github.com/{entry}')

    # 3. LLM-assisted discovery from full text
    if len(text) > 200:
        llm_repos = _llm_discover_repos(text)
        repos.extend(llm_repos)

    # 4. Regex fallback
    repos.extend(f'https://github.com/{match}' for match in re.findall(GITHUB_PATTERN, text))
    repos.extend(f'https://gitlab.com/{match}' for match in re.findall(GITLAB_PATTERN, text))
    repos.extend(f'https://bitbucket.org/{match}' for match in re.findall(BITBUCKET_PATTERN, text))

    # Deduplicate, strip trailing slashes
    seen = set()
    unique = []
    for r in repos:
        r = r.rstrip('/').rstrip('.')
        if r and r not in seen:
            seen.add(r)
            unique.append(r)
    return unique


def _llm_discover_repos(text: str) -> List[str]:
    """Use LLM to extract repository URLs from paper text."""
    prompt = (
        'Extract ALL code repository URLs (GitHub, GitLab, Bitbucket, Zenodo) '
        'from the following paper text. Return a JSON array of URL strings.\n'
        'If none found, return [].\n\n'
        f'{text[:8000]}'
    )
    raw = _call_llm(prompt, system_prompt='You output only valid JSON arrays.', temperature=0.1)
    if not raw:
        return []
    parsed = _parse_llm_json(raw)
    if isinstance(parsed, list) and all(isinstance(u, str) for u in parsed):
        return [u.strip() for u in parsed if u.strip().startswith('http')]
    return []


# ---------------------------------------------------------------------------
# Paper-aware reproduction plan
# ---------------------------------------------------------------------------


def build_reproduction_plan(
    text: str,
    repositories: List[str],
    extracted_steps: Dict[str, List[str]],
    paper_metadata: Dict[str, Any],
    benchmark_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a reproduction plan that maps paper methodology to commands.

    Returns a dict with:
        commands: list of shell commands to reproduce the paper
        expected_outputs: list of file patterns / names to verify
        notes: human-readable explanation
    """
    plan: Dict[str, Any] = {
        'commands': [],
        'expected_outputs': [],
        'notes': '',
    }

    methods_text = paper_metadata.get('methods_summary', '')
    code_snippets_text = paper_metadata.get('code_snippets', '')

    # Try LLM to generate a reproduction plan from the paper text
    if len(text) > 200:
        llm_plan = _llm_build_plan(
            text, methods_text, code_snippets_text, benchmark_type,
        )
        if llm_plan:
            plan = llm_plan

    # Always include a fallback: basic repo testing
    if not plan.get('commands'):
        plan['commands'] = ['pytest -q --maxfail=1 || python -m pytest -q --maxfail=1']
        plan['expected_outputs'] = ['tests/*.py', 'result.json']
        plan['notes'] = 'Fallback plan: basic test execution.'

    return plan


def _llm_build_plan(
    text: str,
    methods_summary: str,
    code_snippets: str,
    benchmark_type: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Ask the LLM to generate a reproduction plan from paper content."""
    snippet = f'{methods_summary}\n{code_snippets}' if methods_summary or code_snippets else text[:4000]
    prompt = (
        f'You are a bioinformatics reproducibility expert.\n'
        f'Given the following paper content, generate a JSON reproduction plan '
        f'with these exact keys:\n'
        f'  {{"commands": ["cmd1", "cmd2", ...], '
        f'"expected_outputs": ["*.png", "results.csv", ...], '
        f'"notes": "..."}}\n\n'
        f'Commands should reproduce a key result from the paper '
        f'(e.g. run a specific notebook, script, or analysis pipeline). '
        f'Use python or bash commands.\n'
        f'If the paper is about {benchmark_type or "omics analysis"}, '
        f'tailor commands accordingly.\n\n'
        f'Paper content:\n{snippet}'
    )
    raw = _call_llm(prompt, system_prompt='You output only valid JSON.', temperature=0.2)
    if not raw:
        return None
    parsed = _parse_llm_json(raw)
    if isinstance(parsed, dict) and 'commands' in parsed:
        return parsed
    return None


# ---------------------------------------------------------------------------
# Paper-specific reproduction execution
# ---------------------------------------------------------------------------


def execute_reproduction(
    clones: List[Dict],
    repro_plan: Dict[str, Any],
    logs_dir: Path,
) -> List[Dict]:
    """Execute the paper-aware reproduction plan on each cloned repository.

    Tries the LLM-generated commands first, falls back to plain test execution.
    """
    results = []
    for clone in clones:
        if not clone.get('cloned'):
            results.append({
                'repo_url': clone['repo_url'],
                'executed': False,
                'reason': 'Not cloned',
                'commands': [],
            })
            continue

        repo_path = Path(clone['clone_path'])
        plan_commands = repro_plan.get('commands', [])

        if not plan_commands:
            results.append({
                'repo_url': clone['repo_url'],
                'executed': False,
                'reason': 'No reproduction commands available',
                'commands': [],
            })
            continue

        # Execute each planned command in sequence
        executed = False
        all_commands = []
        last_returncode = 0
        last_reason = ''

        for cmd in plan_commands:
            cmd = cmd.strip()
            if not cmd:
                continue
            all_commands.append(cmd)
            log_path = logs_dir / f'reproduce_{repo_path.name}_{len(all_commands)}.log'
            run_info = run_command(cmd, repo_path, log_path)
            executed = True
            last_returncode = run_info['returncode']
            if last_returncode != 0:
                last_reason = run_info.get('stderr', 'Unknown error')[:300]
                break  # stop on first failure

        results.append({
            'repo_url': clone['repo_url'],
            'executed': executed,
            'command': ' && '.join(all_commands) if all_commands else '',
            'returncode': last_returncode,
            'reason': 'success' if (executed and last_returncode == 0) else (last_reason or 'failure'),
            'commands': all_commands,
        })

    return results


# ---------------------------------------------------------------------------
# Output verification
# ---------------------------------------------------------------------------


def verify_outputs(
    clones: List[Dict],
    expected_outputs: List[str],
) -> Dict[str, Any]:
    """Check whether expected output files/patterns exist in each cloned repo."""
    results: Dict[str, Any] = {'per_repo': {}, 'all_found': False}

    for clone in clones:
        if not clone.get('cloned'):
            continue
        repo_path = Path(clone['clone_path'])
        found = []
        missing = []
        for pattern in expected_outputs:
            matches = list(repo_path.rglob(pattern))
            if matches:
                found.extend(str(m.relative_to(repo_path)) for m in matches[:5])
            else:
                missing.append(pattern)
        results['per_repo'][clone['repo_url']] = {
            'found': found,
            'missing': missing,
            'all_ok': len(missing) == 0,
        }

    # All repos pass when at least one has all expected outputs
    any_repo_ok = any(v['all_ok'] for v in results['per_repo'].values())
    results['all_found'] = any_repo_ok
    return results


def clone_repositories(repos: List[str], clone_root: Path, depth: int, logs_dir: Path) -> List[Dict]:
    results = []
    clone_root.mkdir(parents=True, exist_ok=True)

    for repo_url in repos:
        name = repo_url.split('/')[-1]
        dest = clone_root / name
        command = ['git', 'clone', '--depth', str(depth), repo_url, str(dest)]
        result = {
            'repo_url': repo_url,
            'clone_path': str(dest),
            'cloned': False,
            'commands': [' '.join(command)],
            'error': None,
        }

        try:
            log_file = logs_dir / f'clone_{name}.log'
            completed = subprocess.run(command, capture_output=True, text=True, timeout=600)
            log_file.write_text(completed.stdout + '\n' + completed.stderr)
            if completed.returncode == 0:
                result['cloned'] = True
            else:
                result['error'] = completed.stderr.strip() or 'Unknown clone failure'
        except FileNotFoundError as exc:
            result['error'] = str(exc)
        except subprocess.TimeoutExpired as exc:
            result['error'] = f"Clone timeout: {exc}"

        results.append(result)

    return results


def build_environment_artifacts(clones: List[Dict], reproducibility_dir: Path, logs_dir: Path, no_install: bool) -> List[Dict]:
    env_results = []
    conda_cmd = find_conda_command()

    for clone in clones:
        if not clone.get('cloned'):
            continue

        repo_path = Path(clone['clone_path'])
        env_dir = reproducibility_dir / 'environments' / repo_path.name
        env_dir.mkdir(parents=True, exist_ok=True)

        repo_env = {
            'repo_url': clone['repo_url'],
            'env_files': [],
            'commands': [],
            'install_results': [],
            'install_success': False,
        }

        requirements_file = locate_environment_file(repo_path)
        if requirements_file:
            target = env_dir / requirements_file.name
            shutil.copyfile(requirements_file, target)
            repo_env['env_files'].append(str(target))

            if not no_install:
                command = create_install_command(target, conda_cmd)
                if command:
                    repo_env['commands'].append(command)
                    install_result = run_command(command, repo_path, logs_dir / f'install_{repo_path.name}.log')
                    repo_env['install_results'].append(install_result)
                    repo_env['install_success'] = install_result['returncode'] == 0
                else:
                    repo_env['install_results'].append({
                        'command': '',
                        'returncode': -1,
                        'stdout': '',
                        'stderr': 'No install command available for this environment file.',
                    })
        else:
            repo_env['commands'].append('No environment file found')

        env_results.append(repo_env)

    return env_results


def find_conda_command() -> Optional[str]:
    conda_exe = os.environ.get('CONDA_EXE')
    if conda_exe:
        return conda_exe
    for command in ('mamba', 'conda'):
        if shutil.which(command):
            return command
    return None


def locate_environment_file(repo_path: Path) -> Optional[Path]:
    candidates = ['environment.yml', 'requirements.txt', 'pyproject.toml', 'setup.py']
    for name in candidates:
        candidate = repo_path / name
        if candidate.exists():
            return candidate
    return None


def create_install_command(env_file: Path, conda_cmd: Optional[str]) -> str:
    if env_file.name == 'environment.yml':
        if conda_cmd:
            return f'{conda_cmd} env create -f "{env_file}"'
        return ''
    if env_file.name == 'requirements.txt':
        return f'pip install -r "{env_file}"'
    if env_file.name == 'pyproject.toml':
        return f'pip install -e "{env_file.parent}"'
    if env_file.name == 'setup.py':
        return f'pip install -e "{env_file.parent}"'
    return ''


def run_command(command: str, cwd: Path, log_path: Path) -> Dict:
    if not command:
        log_path.write_text('No command provided.\n')
        return {
            'command': command,
            'returncode': -1,
            'stdout': '',
            'stderr': 'No command provided.',
        }

    try:
        completed = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=1800)
        log_path.write_text(completed.stdout + '\n' + completed.stderr)
        return {
            'command': command,
            'returncode': completed.returncode,
            'stdout': completed.stdout,
            'stderr': completed.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        log_path.write_text(f"Timeout: {exc}\n")
        return {
            'command': command,
            'returncode': -1,
            'stdout': '',
            'stderr': f"Timeout: {exc}",
        }


def save_plan(reproducibility_dir: Path, plan: Dict) -> None:
    (reproducibility_dir / 'plan.json').write_text(json.dumps(plan, indent=2))


def save_result(reproducibility_dir: Path, result: Dict) -> None:
    (reproducibility_dir / 'result.json').write_text(json.dumps(result, indent=2))


def write_commands_script(reproducibility_dir: Path, commands: List[str]) -> None:
    script = reproducibility_dir / 'commands.sh'
    script.write_text('#!/usr/bin/env bash\n\n' + '\n'.join(commands) + '\n')


def write_report(reproducibility_dir: Path, plan: Dict, result: Dict) -> None:
    report = [
        f"# Reproducibility Report",
        f"",
        f"## Input",
        f"- {plan['input']} ({plan['input_type']})",
        f"- Benchmark Type: {plan['benchmark_type']}",
        f"",
        f"## Repositories",
    ]
    if plan['repositories']:
        report.extend(f"- {repo}" for repo in plan['repositories'])
    else:
        report.append('- None found')

    report.extend([
        '',
        '## Datasets',
        f"- GEO: {plan['datasets'].get('geo_accessions', {}).get('gse', [])}",
        f"- SRA: {plan['datasets'].get('sra_accessions', [])}",
        f"- cellxgene: {plan['datasets'].get('cellxgene_accessions', [])}",
        '',
        '## Extracted Steps',
    ])

    method_sections = plan['extracted_steps'].get('method_sections', [])
    code_snippets = plan['extracted_steps'].get('code_snippets', [])

    if method_sections:
        report.append('### Method Sections')
        report.extend(f"- {section[:300]}..." for section in method_sections)
    else:
        report.append('- No method sections extracted.')

    if code_snippets:
        report.append('### Code Snippets')
        report.extend(f"- {snippet[:300]}..." for snippet in code_snippets)
    else:
        report.append('- No code snippets extracted.')

    report.extend([
        '',
        '## Status',
        f"- Parsed: {result['statuses']['parsed']}",
        f"- Repositories found: {result['statuses']['repositories_found']}",
        f"- Clone success: {result['statuses']['clone_success']}",
        f"- Install success: {result['statuses']['install_success']}",
        f"- Run success: {result['statuses']['run_success']}",
        f"- Failure phase: {result['statuses']['failure_phase']}",
        f"- Failure details: {result['statuses']['failure_details']}",
        '',
        '## Repository Results',
    ])

    for repo in result['repository_results']:
        env = repo.get('environment', {})
        run = repo.get('run', {})
        report.append(
            f"- {repo['repo_url']}: cloned={repo.get('cloned', False)}, "
            f"install_success={env.get('install_success', False)}, "
            f"run_returncode={run.get('returncode', 'n/a')}, "
            f"reason={run.get('reason', env.get('error', 'unknown'))}"
        )

    (reproducibility_dir / 'report.md').write_text('\n'.join(report) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
