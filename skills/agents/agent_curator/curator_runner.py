"""Stage 1 AgentCurator runner — plan + execute to produce curated.h5ad files.

Public API:
    run_agent_curator(benchmark_type, data_root, use_llm=True, execute=True)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from .curator import AgentCurator
from .executor import CurationExecutor


def run_agent_curator(
    benchmark_type: str = "integration",
    data_root: Path = Path("benchmark_data"),
    use_llm: bool = True,
    execute: bool = True,
) -> Dict[str, Any]:
    """Run AgentCurator for every paper that has data.

    Two-phase pipeline:
      1. Plan (LLM):  curation_plan.json — what format, what steps
      2. Execute:      curated.h5ad per dataset — actual conversion

    Args:
        execute: If True, also run CurationExecutor to produce .h5ad files.

    Returns a summary dict (also written to ``_curation_summary.json``).
    """
    data_dir = data_root / benchmark_type

    if not data_dir.exists():
        print(f"  ⚠️  Data dir not found: {data_dir}")
        return {"status": "no_data_dir", "papers": []}

    results = []
    total_curated = 0
    total_failed = 0

    for paper_path in sorted(data_dir.iterdir()):
        if not paper_path.is_dir() or paper_path.name.startswith("_"):
            continue

        # ── Phase 1: Plan ──
        ep = paper_path / "execution_plan.json"
        exec_plan = {}
        if ep.exists():
            exec_plan = json.loads(ep.read_text(encoding="utf-8"))

        curator = AgentCurator(paper_path, execution_plan=exec_plan)

        if use_llm:
            print(f"  🔬 [{paper_path.name[:45]}] AgentCurator planning...")
            plan = curator.curate()
        else:
            up = paper_path / "unpacked_data"
            data_root_p = up if up.exists() else (paper_path / "data")
            from .curator import _scan_datasets
            datasets = _scan_datasets(data_root_p) if data_root_p.exists() else {}
            plan = curator._fallback_plan(datasets)
            # Write fallback plan to disk so CurationExecutor can find it
            out = paper_path / "curation_plan.json"
            out.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")

        has_error = "error" in plan
        ds_count = len(plan.get("datasets", []))
        uncuratable = len(plan.get("uncuratable", []))

        # ── Phase 1.5: Benchmark normalization (if execute and curated.h5ad exists) ──
        if execute and not has_error:
            from .curator import normalize_curated_for_benchmark
            bm_type = benchmark_type.split("_")[0] if "_" in benchmark_type else benchmark_type
            norm_result = normalize_curated_for_benchmark(paper_path, benchmark_type=bm_type)
            if norm_result.get("status") == "completed" and norm_result.get("mappings"):
                print(f"  📋 [{paper_path.name[:45]}] Benchmark obs standardized")
                for m in norm_result.get("mappings", []):
                    print(f"     {m.get('mapping', {})}")

        # ── Phase 2: Execute ──
        exec_result = None
        if execute and not has_error:
            print(f"  🔧 [{paper_path.name[:45]}] CurationExecutor running...")
            executor = CurationExecutor(paper_path, execution_plan=exec_plan)
            exec_result = executor.run()
            cur = exec_result.get("datasets_curated", 0)
            fail = exec_result.get("datasets_failed", 0)
            total_curated += cur
            total_failed += fail
            print(f"     → {cur} curated, {fail} failed")

        results.append({
            "slug": paper_path.name,
            "error": plan.get("error"),
            "total_datasets": plan.get("total_datasets", ds_count),
            "expression_datasets": plan.get("expression_datasets", 0),
            "non_expression_datasets": plan.get("non_expression_datasets", 0),
            "uncuratable_count": uncuratable,
            "execution": exec_result,
        })

    ok_count = sum(1 for r in results if not r.get("error"))
    err_count = sum(1 for r in results if r.get("error"))

    summary = {
        "benchmark_type": benchmark_type,
        "total_papers": len(results),
        "papers_with_plan": ok_count,
        "papers_with_error": err_count,
        "total_datasets_curated": total_curated,
        "total_datasets_failed": total_failed,
        "papers": results,
    }

    summary_path = data_dir / "_curation_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  📊 Curation summary saved to: {summary_path}")
    return summary
