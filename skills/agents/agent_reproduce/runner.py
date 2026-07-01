"""Stage 2 AgentReproduce — Monitor-Fix loop for paper reproduction.

Public API:
    run_agent_reproduce(paper_slug, data_root, code_root, benchmark_type)
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .monitor import analyze_log, classify_for_llm
from .fix import AgentFix
from .extractor import extract_results
from .agent_evaluator import evaluate_with_llm
from skills.agents.agent_preflight.scanner import _call_llm, _parse_llm_json


def run_agent_reproduce(
    paper_slug: str,
    data_root: Path = Path("benchmark_data"),
    code_root: Path = Path("benchmark_code"),
    benchmark_type: str = "integration",
    max_fix_attempts: int = 3,
) -> Dict[str, Any]:
    """Run AgentMonitor + AgentFix loop for one paper.

    Steps:
      1. Read execution_plan.json → get entry_point + env info
      2. Run the entry point script
      3. AgentMonitor observes output
      4. On error → AgentFix diagnoses and repairs
      5. Loop up to max_fix_attempts
    """
    paper_dir = data_root / f"{benchmark_type}_e2e_test" / paper_slug
    code_dir = code_root / f"{benchmark_type}_e2e_test" / paper_slug

    ep_file = paper_dir / "execution_plan.json"
    if not ep_file.exists():
        return {"paper": paper_slug, "status": "no_execution_plan"}

    ep = json.loads(ep_file.read_text(encoding="utf-8"))
    entry_point = ep.get("entry_point", "")
    env_guess = ep.get("env_guess", {})
    matched_scripts = ep.get("matched_scripts", {})
    matched_data = ep.get("matched_data", {})

    # Determine ALL scripts to run (in dependency order)
    target_scripts = _pick_entry_scripts(matched_scripts, entry_point)
    if not target_scripts:
        return {"paper": paper_slug, "status": "no_entry_script",
                "message": "execution_plan has no suitable script"}

    print(f"  Scripts to reproduce ({len(target_scripts)}):")
    for t in target_scripts:
        print(f"    - {t['script_name']}  ({t.get('purpose', '')[:60]})")

    # Prepare reproduce directory (outputs only, no script copies)
    reproduce_dir = paper_dir / "reproduce"
    reproduce_dir.mkdir(exist_ok=True)

    # Write a manifest.json recording what we're reproducing
    manifest = {
        "paper": paper_slug,
        "benchmark_type": benchmark_type,
        "reproduced_at": __import__("datetime").datetime.now().isoformat(),
        "scripts": [],
    }

    repo_dir = _find_code_repo(code_dir)
    if not repo_dir:
        return {"paper": paper_slug, "status": "no_code_repo",
                "message": f"no repo dir under {code_dir}"}

    # Run each script in sequence
    fixer = AgentFix(paper_slug, paper_dir)
    all_output = ""
    consecutive_errors = 0
    final_decision: Dict[str, Any] = {"action": "ABORT", "message": "unknown"}
    returncode = 0
    total_scripts = len(target_scripts)

    for script_idx, target in enumerate(target_scripts):
        script_name = target["script_name"]
        print(f"\n  === Script {script_idx+1}/{total_scripts}: {script_name} ===")

        script_path = _find_script_file(script_name, repo_dir)
        if not script_path:
            print(f"  [SKIP] script not found: {script_name}")
            continue

        print(f"  Source: {script_path.relative_to(repo_dir)}")
        print(f"  Outputs -> reproduce/")

        # Build command to run the script in its original location,
        # with outputs redirected to reproduce/
        cmd = _build_command_for_script(script_path, reproduce_dir, repo_dir,
                                         data_dir=paper_dir / "unpacked_data")

        if not cmd:
            print(f"  [SKIP] unsupported script type: {script_name}")
            continue

        manifest["scripts"].append({
            "script_name": script_name,
            "source_path": str(script_path.relative_to(repo_dir)),
            "language": target.get("language", "unknown"),
            "purpose": target.get("purpose", ""),
            "command": " ".join(cmd[:3]) + " ...",
        })

        print(f"  Command: {' '.join(cmd[:3])} ...")

        # Run with Monitor loop

        # Run with Monitor loop
        for attempt in range(1, max_fix_attempts + 1):
            print(f"  [Attempt {attempt}/{max_fix_attempts}] {paper_slug[:40]}")

            stdout, stderr, returncode = _run_command(cmd, reproduce_dir)
            output = stdout + "\n" + stderr
            all_output += f"\n=== {script_name} ===\n{output}"

            result = analyze_log(output, consecutive_errors)
            final_decision = result

            if returncode == 0:
                # Check output for hidden errors (R/knitr may return 0 even on error)
                has_hidden_error = any(p in output.lower() for p in [
                    "error in library", "there is no package",
                    "cannot open the connection", "error in file",
                    "error in source", "error in h()",
                ])
                if has_hidden_error:
                    print(f"  [WARN] Hidden error in output, triggering fix...")
                    # Treat as FALLBACK: run fix + retry
                    fix_result = fixer.diagnose_and_fix(output, reproduce_dir)
                    if fix_result.get("fixed") and attempt < max_fix_attempts:
                        fix_cmds = fix_result.get("commands_run", [])
                        for fix_cmd in fix_cmds:
                            import subprocess as _sp
                            _sp.run(fix_cmd, shell=True, capture_output=True,
                                    text=False, timeout=300)
                        print(f"  [RETRY after fix] Attempt {attempt+1}")
                        continue
                print(f"  [OK] {script_name} completed")
                consecutive_errors = 0
                break

            if result["action"] == "CONTINUE":
                consecutive_errors = 0
            elif result["action"] in ("RETRY", "WARN"):
                consecutive_errors += 1
                if attempt < max_fix_attempts:
                    print(f"  [RETRY] {result.get('message', '')}")
                    continue
            elif result["action"] == "FALLBACK":
                fix_result = fixer.diagnose_and_fix(output, reproduce_dir)
                if fix_result.get("fixed") and attempt < max_fix_attempts:
                    # Execute the fix command before retrying
                    fix_cmds = fix_result.get("commands_run", [])
                    for fix_cmd in fix_cmds:
                        print(f"  [FIX] {fix_cmd[:80]}...")
                        import subprocess as _sp
                        _sp.run(fix_cmd, shell=True, capture_output=True, text=False,
                                timeout=300)
                    print(f"  [RETRY after fix] Attempt {attempt+1}")
                    continue
            elif result["action"] == "ABORT":
                break

        if returncode != 0:
            break  # stop pipeline on failure

    # Save manifest
    manifest["exit_code"] = returncode
    manifest["scripts_completed"] = script_idx + 1 if returncode == 0 else script_idx
    manifest["total_scripts"] = total_scripts
    (reproduce_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # After all scripts: extract + evaluate results
    extract_result = extract_results(reproduce_dir)

    # Build result dict first, then evaluate
    result = {
        "paper": paper_slug,
        "status": "success" if returncode == 0 else "failed",
        "scripts_completed": script_idx + 1 if returncode == 0 else script_idx,
        "total_scripts": total_scripts,
        "attempts": attempt,
        "max_attempts": max_fix_attempts,
        "monitor_decision": final_decision,
        "output": all_output[-5000:],
        "fix_attempts": fixer.fix_log,
        "entry_scripts": [t["script_name"] for t in target_scripts],
        "env_guess": env_guess,
    }

    # AgentEvaluator: LLM-driven, benchmark_type-aware scoring
    # Infer benchmark_type from env_guess if available
    bm_type = env_guess.get("benchmark_type", "generic")
    evaluation = evaluate_with_llm(
        result,
        benchmark_type=bm_type,
        paper_dir=paper_dir,
    )

    # Add Stage 2b fields
    result["reproducibility"] = {
        "score": evaluation["score"],
        "breakdown": evaluation.get("breakdown", {}),
        "gaps": evaluation.get("gaps", []),
        "summary": evaluation.get("summary", ""),
        "needs_improvement": evaluation.get("needs_improvement", True),
        "method": evaluation.get("_method", "agent_evaluator"),
    }
    result["extracted_metrics"] = extract_result["metrics"]
    result["missing_packages"] = extract_result["missing_packages"]
    result["output_files"] = extract_result["outputs"]

    # Save reproduce result
    result_file = reproduce_dir / "reproduce_result.json"
    result_file.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    fixer.save_fix_log(reproduce_dir / "fix_attempts.json")

    return result


def _pick_entry_scripts(matched_scripts: Dict[str, Any], entry_point: str) -> List[Dict[str, Any]]:
    """Pick ALL scripts to run in dependency order.

    Returns ordered list of dicts with {"script_name", "language", "purpose"}.
    Only returns actual executable scripts (Rmd, R, py), not helper files.
    """
    EXECUTABLE_EXTS = {'.rmd', '.rmarkdown'}
    entry_scripts = []
    other_scripts = []

    for name, info in matched_scripts.items():
        # Only Rmd/Rmarkdown files are entry points; .R helpers are sourced
        ext = Path(name).suffix.lower()
        if ext not in EXECUTABLE_EXTS:
            continue
        if info.get("can_use_with_data", False):
            entry = {"script_name": name, **info}
            if info.get("is_entry_point", False):
                entry_scripts.append(entry)
            else:
                other_scripts.append(entry)

    return entry_scripts + other_scripts


def _find_script_file(script_name: str, repo_dir: Path) -> Optional[Path]:
    """Find a script by name recursively in the repo."""
    for f in repo_dir.rglob(script_name):
        if f.is_file():
            return f
    return None


def _find_code_repo(code_dir: Path) -> Optional[Path]:
    """Find the first repo directory with actual code files."""
    if not code_dir.exists():
        return None
    for d in code_dir.iterdir():
        if d.is_dir() and (d / ".git").exists():
            return d
    for d in code_dir.iterdir():
        if d.is_dir():
            return d
    return None


def _find_script_file(script_name: str, repo_dir: Path) -> Optional[Path]:
    """Find a script by name recursively in the repo."""
    for f in repo_dir.rglob(script_name):
        if f.is_file():
            return f
    return None


def _prepare_script_in_reproduce(script_path: Path, reproduce_dir: Path) -> Path:
    """Copy script to reproduce dir so outputs are isolated from original code."""
    rel = script_path.name  # same filename, in reproduce dir
    dest = reproduce_dir / rel
    # Only copy if not already there (or source is newer)
    if not dest.exists() or script_path.stat().st_mtime > dest.stat().st_mtime:
        dest.write_text(script_path.read_text(encoding="utf-8", errors="replace"),
                        encoding="utf-8")
    return dest


def _build_command_for_script(
    script_path: Path, reproduce_dir: Path, repo_dir: Path,
    data_dir: Optional[Path] = None,
) -> Optional[List[str]]:
    """Build shell command to run a script in its original location,
    with outputs redirected to reproduce/.

    Before running, links data files into the script directory so that
    here()/readRDS() can find them (addresses the common pattern of
    readRDS(here('output', 'file.rds')) or similar).

    Key principle: scripts run in-place (source() paths, here() resolution,
    relative imports all work), but output files go to reproduce/.
    """
    ext = script_path.suffix.lower()
    if ext == '.rmd' or ext == '.rmarkdown':
        # Rmd: run knitr in the original script directory so here() and
        # source() resolve correctly; output goes to reproduce/
        # Use forward slashes for R (Windows R accepts them)
        wp = str(script_path.resolve()).replace('\\', '/')
        wd = str(script_path.parent.resolve()).replace('\\', '/')
        out_md = str((reproduce_dir / (script_path.stem + ".md")).resolve()).replace('\\', '/')

        # Build a data-linking prefix: if data_dir has files, copy them
        # into the script's working directory (e.g., output/ subfolder)
        data_prefix = ""
        if data_dir and data_dir.exists():
            # Find the most relevant data files (rds, h5ad, etc.)
            data_files = []
            for ext in ('*.rds', '*.h5ad', '*.mtx', '*.tsv', '*.csv'):
                data_files.extend(data_dir.rglob(ext))
            if data_files:
                # Create output/ in working directory and copy files
                data_src = str(data_dir.resolve()).replace('\\', '/')
                data_prefix = (
                    "if(!dir.exists('output')) dir.create('output'); "
                    "invisible(file.copy(Sys.glob(file.path('%s', '*', '*.rds')), "
                    "'output/', overwrite=TRUE, copy.mode=FALSE)); "
                    "invisible(file.copy(Sys.glob(file.path('%s', '*', '*', '*.rds')), "
                    "'output/', overwrite=TRUE, copy.mode=FALSE)); " % (data_src, data_src)
                )
        return ["R", "-e",
                "setwd('%s'); %s knitr::knit('%s', output='%s')" % (wd, data_prefix, wp, out_md)]
    if ext == '.r' or ext == '.rdata':
        return ["Rscript", str(script_path)]
    if ext == '.py':
        return [sys.executable, str(script_path)]
    return None


def _run_command(cmd: List[str], cwd: Optional[Path]) -> Tuple[str, str, int]:
    """Run a command and capture output."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=False,
                          timeout=1800, cwd=str(cwd) if cwd else None)
        stdout = (r.stdout or b"").decode("utf-8", errors="replace")
        stderr = (r.stderr or b"").decode("utf-8", errors="replace")
        return stdout, stderr, r.returncode
    except subprocess.TimeoutExpired:
        return "", "TIMEOUT after 1800s", -1
    except Exception as exc:
        return "", str(exc), -1
