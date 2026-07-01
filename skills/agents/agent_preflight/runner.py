"""Stage 0.5 runner — iterate all FULLY_ACCEPTED papers and produce execution plans.

Public API:
    run_agent_preflight(benchmark_type, data_root, code_root, use_llm=True)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from .scanner import AgentScanner


def run_agent_preflight(
    benchmark_type: str = "integration",
    data_root: Path = Path("benchmark_data"),
    code_root: Path = Path("benchmark_code"),
    use_llm: bool = True,
) -> Dict[str, Any]:
    """Run AgentScanner for every paper with protocol + data + code.

    Returns a summary dict (also written to ``_preflight_summary.json``).
    """
    data_dir = data_root / benchmark_type  # e.g. benchmark_data/integration_e2e_test
    code_dir = code_root / benchmark_type

    if not data_dir.exists():
        print(f"  ⚠️  Data dir not found: {data_dir}")
        return {"status": "no_data_dir", "papers": []}

    results = []
    for paper_path in sorted(data_dir.iterdir()):
        if not paper_path.is_dir() or paper_path.name.startswith("_"):
            continue

        protocol = paper_path / "experimental_protocol.json"
        if not protocol.exists():
            continue

        code_path = code_dir / paper_path.name
        scanner = AgentScanner(paper_path, code_path)

        if use_llm:
            print(f"  🔍 [{paper_path.name[:45]}] AgentScanner analyzing...")
            plan = scanner.analyze()
        else:
            # Dry-run: just build prompt without calling LLM
            protocol_data = scanner._read_protocol()
            data_s = scanner._scan_data()
            code_s = scanner._scan_code()
            plan = scanner._fallback_plan(protocol_data, data_s, code_s)

        results.append({
            "slug": paper_path.name,
            "status": plan.get("status", "unknown"),
            "feasible_analysis": plan.get("feasible_analysis", ""),
            "confidence": plan.get("confidence", 0),
            "warnings": plan.get("warnings", []),
            "missing": plan.get("missing", []),
        })

    # Status counting — all current status values from pragmatic prompt
    status_counts: Dict[str, int] = {}
    for r in results:
        s = r["status"]
        status_counts[s] = status_counts.get(s, 0) + 1

    summary = {
        "benchmark_type": benchmark_type,
        "total_papers": len(results),
        "status_counts": status_counts,
        "papers": results,
    }

    # Write summary
    summary_path = data_dir / "_preflight_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  📊 Preflight summary saved to: {summary_path}")
    return summary
