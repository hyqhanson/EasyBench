"""AgentEvaluator — LLM-driven reproducibility evaluation.

Replaces the hardcoded ``evaluator.py`` weights with an LLM that:
  1. Reads the benchmark_type-specific scoring criteria (from llm_collector.py's _analysis_context)
  2. Examines the reproduce_result.json + extracted metrics
  3. Produces a scored evaluation with human-readable reasoning

This makes evaluation adaptive to different benchmark types (integration,
clustering, annotation, spatial, trajectory, etc.) rather than using
a fixed set of weights for every paper.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── benchmark_type 评估标准（轻量级前端规则，LLM 做深度判断） ──

BENCHMARK_EVAL_CRITERIA: Dict[str, Dict[str, Any]] = {
    "integration": {
        "name": "Batch Integration",
        "description": "Multi-sample integration quality",
        "key_aspects": [
            "data_sources: ≥2 batches/samples present",
            "output_files: integrated object (h5ad/RDS) produced",
            "cell_counts: expected cell count within 20% of paper's claim",
            "batch_effects: script attempts batch correction/harmonization",
            "packages: Seurat/Harmony/scanorama/BBKNN available",
        ],
        "critical_flags": [
            "NO_DATA: no data files found",
            "SINGLE_BATCH: only one batch/sample — integration impossible",
            "SCRIPT_ERROR: entry script crashed before producing output",
        ],
    },
    "clustering": {
        "name": "Cell Clustering",
        "description": "Unsupervised cell grouping quality",
        "key_aspects": [
            "output_files: cluster assignments produced",
            "n_clusters: matches paper's claimed number (±50%)",
            "resolution: clustering resolution parameter used",
            "visualization: UMAP/tSNE plots generated",
            "packages: Seurat/scanpy clustering available",
        ],
        "critical_flags": [
            "NO_DATA: no data files found",
            "NO_CLUSTERS: zero clusters produced",
            "SCRIPT_ERROR: entry script crashed",
        ],
    },
    "annotation": {
        "name": "Cell Type Annotation",
        "description": "Cell type labeling quality",
        "key_aspects": [
            "output_files: cell type labels produced",
            "n_cell_types: matches paper's claimed types",
            "marker_genes: marker gene lists used",
            "reference: reference-based annotation attempted",
            "packages: SingleR/Azimuth/scType available",
        ],
        "critical_flags": [
            "NO_DATA: no data files found",
            "NO_LABELS: no cell type labels produced",
            "SCRIPT_ERROR: entry script crashed",
        ],
    },
    "spatial": {
        "name": "Spatial Transcriptomics",
        "description": "Spatial data processing quality",
        "key_aspects": [
            "data_format: spatial data (Visium/MERFISH/Xenium) found",
            "output_files: spatial plots/maps produced",
            "tissue_sections: tissue section images processed",
            "niche_clusters: spatial niche/cluster detection",
            "packages: Seurat/Squidpy/Giotto available",
        ],
        "critical_flags": [
            "NO_DATA: no spatial data files found",
            "NO_SPATIAL_COORDS: coordinates missing",
            "SCRIPT_ERROR: entry script crashed",
        ],
    },
    "trajectory": {
        "name": "Trajectory Inference",
        "description": "Pseudotime/trajectory analysis quality",
        "key_aspects": [
            "output_files: trajectory plots produced",
            "pseudotime: pseudotime values computed",
            "branches: branching points identified",
            "lineage_genes: lineage-specific genes reported",
            "packages: Monocle/slingshot/scVelo available",
        ],
        "critical_flags": [
            "NO_DATA: no data files found",
            "NO_TRAJECTORY: no trajectory computed",
            "SCRIPT_ERROR: entry script crashed",
        ],
    },
    "generic": {
        "name": "Generic Reproducibility",
        "description": "Standard script execution quality",
        "key_aspects": [
            "scripts_executed: all entry scripts ran to completion",
            "packages_available: all R/Python packages installed",
            "output_files: any output files produced",
            "file_paths: no path resolution errors (here()/setwd())",
            "error_free: no critical runtime errors",
        ],
        "critical_flags": [
            "NO_DATA: no data files found",
            "ALL_SCRIPTS_FAILED: all entry scripts failed",
            "SCRIPT_ERROR: entry script crashed",
        ],
    },
}


def evaluate_with_llm(
    reproduce_result: Dict[str, Any],
    benchmark_type: str,
    paper_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Evaluate reproducibility using LLM with benchmark_type-specific criteria.

    Args:
        reproduce_result: Output from run_agent_reproduce()
        benchmark_type: e.g. "integration", "clustering", "annotation"
        paper_dir: Paper directory for reading additional files

    Returns:
        Dict with keys: score (0-100), breakdown, gaps, llm_reasoning
    """
    # 1. Determine criteria
    criteria = BENCHMARK_EVAL_CRITERIA.get(benchmark_type, BENCHMARK_EVAL_CRITERIA["generic"])

    # 2. Build evaluation prompt
    prompt = _build_eval_prompt(reproduce_result, criteria, paper_dir)
    if not prompt:
        return _fallback_evaluation(reproduce_result, criteria)

    # 3. Call LLM
    try:
        from skills.agents.agent_preflight.scanner import _call_llm, _parse_llm_json
        raw = _call_llm(
            prompt,
            system_prompt=(
                "You are an expert evaluator of computational biology reproducibility. "
                "Analyze reproduction results and output a JSON score. "
                "Be strict: partial success should score 40-70, not 80+."
            ),
        )
        if not raw:
            return _fallback_evaluation(reproduce_result, criteria)

        result = _parse_llm_json(raw)
        if isinstance(result, dict) and "score" in result:
            return result
        return _fallback_evaluation(reproduce_result, criteria)
    except Exception as exc:
        return _fallback_evaluation(reproduce_result, criteria, error=str(exc))


def _build_eval_prompt(
    result: Dict[str, Any],
    criteria: Dict[str, Any],
    paper_dir: Optional[Path],
) -> Optional[str]:
    """Build the LLM evaluation prompt from reproduce results."""
    status = result.get("status", "unknown")
    scripts_completed = result.get("scripts_completed", 0)
    total_scripts = result.get("total_scripts", 0)
    missing_packages = result.get("missing_packages", [])
    output_files = result.get("output_files", [])
    extracted = result.get("extracted_metrics", {})

    aspects = "\n".join(f"  - {a}" for a in criteria["key_aspects"])
    flags = "\n".join(f"  - {f}" for f in criteria["critical_flags"])

    prompt = f"""Evaluate the reproducibility of this {criteria['name']} paper reproduction.

BENCHMARK TYPE: {criteria['name']}
DESCRIPTION: {criteria['description']}

REPRODUCTION RESULTS:
  - Overall status: {status}
  - Scripts completed: {scripts_completed}/{total_scripts}
  - Missing packages: {missing_packages if missing_packages else 'None'}
  - Output files produced: {output_files if output_files else 'None'}
  - Extracted metrics: {json.dumps(extracted, indent=4)}

KEY ASPECTS TO EVALUATE:
{aspects}

CRITICAL FAILURE FLAGS (if any apply, score ≤20):
{flags}

OUTPUT FORMAT — return ONLY valid JSON:
{{
  "score": <integer 0-100>,
  "breakdown": {{
    "scripts_execution": <0-25>,
    "data_quality": <0-25>,
    "package_availability": <0-25>,
    "output_completeness": <0-25>
  }},
  "gaps": [
    {{
      "category": "<category_name>",
      "severity": "critical|high|warning",
      "message": "<specific issue found>",
      "fix_hint": "<how to fix>"
    }}
  ],
  "summary": "<one-sentence evaluation summary>",
  "needs_improvement": <true|false>
}}

Rules:
- Score 0-25: reproduction failed entirely (no scripts ran, critical errors)
- Score 26-50: partial execution with significant gaps (missing packages, path errors)
- Score 51-75: scripts ran but with notable issues (incomplete analysis, wrong data)
- Score 76-100: scripts ran successfully with all key outputs produced
- Be STRICT. A score of 80+ requires ALL scripts completed AND meaningful outputs.
- If missing_packages is non-empty, deduct significantly from package_availability.
- If output_files is empty but scripts_completed > 0, note that outputs may be in other formats.
"""

    # Add paper-specific context if available
    if paper_dir:
        produce_md = list(Path(paper_dir).glob("reproduce/*.md"))
        if produce_md:
            # Read last 2000 chars of first .md for error summary
            md_text = produce_md[0].read_text(encoding="utf-8", errors="replace")[-2000:]
            prompt += f"\n\nOUTPUT FILE SNIPPET (last 2000 chars of {produce_md[0].name}):\n{md_text[:2000]}\n"

    return prompt


def _fallback_evaluation(
    result: Dict[str, Any],
    criteria: Dict[str, Any],
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """Fallback when LLM is unavailable — use lightweight heuristics."""
    status = result.get("status", "unknown")
    scripts_ok = result.get("scripts_completed", 0)
    total = result.get("total_scripts", 1)
    missing = result.get("missing_packages", [])
    outputs = result.get("output_files", [])

    # Heuristic scoring
    execution_score = min(25, int((scripts_ok / max(total, 1)) * 25))

    pkg_score = max(0, 25 - len(missing) * 8)

    output_score = min(25, len(outputs) * 5)
    if not outputs and scripts_ok > 0:
        output_score = 10  # partial credit for script execution

    data_score = 15 if status == "success" else (5 if status == "partial" else 0)

    total_score = execution_score + pkg_score + output_score + data_score

    gaps = []
    if missing:
        gaps.append({
            "category": "missing_packages",
            "severity": "high",
            "message": f"Missing packages: {', '.join(missing)}",
        })
    if not outputs:
        gaps.append({
            "category": "output_files",
            "severity": "warning",
            "message": "No output files detected",
        })

    return {
        "score": total_score,
        "breakdown": {
            "scripts_execution": execution_score,
            "data_quality": data_score,
            "package_availability": pkg_score,
            "output_completeness": output_score,
        },
        "gaps": gaps,
        "summary": (
            f"Score: {total_score}/100 — "
            f"{'LLM unavailable, used heuristic fallback' if error else 'Heuristic evaluation'}"
        ),
        "needs_improvement": total_score < 70,
        "_method": "heuristic_fallback",
        "_error": error,
    }
