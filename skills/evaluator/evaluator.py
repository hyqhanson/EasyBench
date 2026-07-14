"""EasyBench Benchmark Evaluator — compute benchmark metrics from processed.h5ad.

Uses pure scanpy + numpy for metric computation, no OmicsClaw autoagent
dependency. Organized by benchmark type.

Usage:
    python -m skills.evaluator.evaluator --benchmark-type integration \\
        --input-dir e2e_test2/03_process_data \\
        --output-dir e2e_test2/06_benchmark_evaluation
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import scanpy as sc
from sklearn.metrics import silhouette_score

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------

def _silhouette_score(adata: sc.AnnData, label_key: str) -> float:
    """Mean silhouette width for a label column in adata.obs."""
    if label_key not in adata.obs.columns:
        return 0.0
    labels = adata.obs[label_key].values
    if len(set(labels)) < 2:
        return 0.0
    try:
        X = adata.obsm["X_pca"][:, :20] if "X_pca" in adata.obsm else adata.X
        if hasattr(X, "toarray"):
            X = X.toarray()
        return float(silhouette_score(X, labels, random_state=42))
    except Exception:
        return 0.0


def _n_pcs(adata: sc.AnnData) -> int:
    return int(adata.obsm["X_pca"].shape[1]) if "X_pca" in adata.obsm else 0


def _n_cells(adata: sc.AnnData) -> int:
    return int(adata.n_obs)


def _n_genes(adata: sc.AnnData) -> int:
    return int(adata.n_vars)


def _n_hvg(adata: sc.AnnData) -> int:
    if "highly_variable" in adata.var.columns:
        return int(adata.var["highly_variable"].sum())
    return 0


# ---------------------------------------------------------------------------
# Benchmark-type metric registries
# ---------------------------------------------------------------------------

BENCHMARK_REGISTRY: Dict[str, Dict[str, Any]] = {
    "integration": {
        "label": "Integration Benchmark",
        "metrics": [
            ("n_cells", _n_cells, "Total cells", "size"),
            ("n_genes", _n_genes, "Total genes", "size"),
            ("n_hvg", _n_hvg, "Highly variable genes", "quality"),
            ("n_pcs", _n_pcs, "PCA components", "dimred"),
            ("batch_silhouette", lambda a: _silhouette_score(a, "batch"), "Batch silhouette width", "batch"),
        ],
    },
    "clustering": {
        "label": "Clustering Benchmark",
        "metrics": [
            ("n_cells", _n_cells, "Total cells", "size"),
            ("n_genes", _n_genes, "Total genes", "size"),
            ("n_hvg", _n_hvg, "Highly variable genes", "quality"),
            ("n_pcs", _n_pcs, "PCA components", "dimred"),
        ],
    },
    "annotation": {
        "label": "Cell Type Annotation Benchmark",
        "metrics": [
            ("n_cells", _n_cells, "Total cells", "size"),
            ("n_genes", _n_genes, "Total genes", "size"),
        ],
    },
    "generic": {
        "label": "Generic Benchmark",
        "metrics": [
            ("n_cells", _n_cells, "Total cells", "size"),
            ("n_genes", _n_genes, "Total genes", "size"),
            ("n_hvg", _n_hvg, "Highly variable genes", "quality"),
            ("n_pcs", _n_pcs, "PCA components", "dimred"),
        ],
    },
}


# ---------------------------------------------------------------------------
# Evaluation logic
# ---------------------------------------------------------------------------

def evaluate_one(
    h5ad_path: Path,
    benchmark_type: str,
) -> Dict[str, Any]:
    """Compute metrics for one processed.h5ad file."""
    logger.info("Loading: %s", h5ad_path)
    adata = sc.read_h5ad(str(h5ad_path))

    registry = BENCHMARK_REGISTRY.get(benchmark_type, BENCHMARK_REGISTRY["generic"])
    raw: Dict[str, float] = {}
    metadata: Dict[str, Any] = {}

    for name, func, desc, category in registry["metrics"]:
        try:
            value = func(adata)
            raw[name] = float(value) if isinstance(value, (int, float, np.integer, np.floating)) else 0.0
        except Exception as exc:
            logger.warning("  Metric %s failed: %s", name, exc)
            raw[name] = 0.0
        metadata[name] = {"description": desc, "category": category}

    composite = sum(raw.values()) / len(raw) if raw else 0.0

    return {
        "file": str(h5ad_path),
        "slug": h5ad_path.parent.name,
        "dataset_id": h5ad_path.stem,
        "composite_score": round(composite, 4),
        "raw_metrics": raw,
        "metadata": metadata,
    }


def run_benchmark(
    benchmark_type: str,
    input_dir: Path,
    output_dir: Path,
) -> Dict[str, Any]:
    """Find all processed.h5ad files and evaluate them."""
    input_dir = Path(input_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    h5ad_files = sorted(input_dir.rglob("*.processed.h5ad"))
    logger.info("Found %d processed file(s) in %s", len(h5ad_files), input_dir)

    evaluations = []
    for h5 in h5ad_files:
        logger.info("Evaluating: %s", h5)
        result = evaluate_one(h5, benchmark_type)
        evaluations.append(result)

    # Summary
    metrics_names = list(evaluations[0]["raw_metrics"].keys()) if evaluations else []
    per_metric = {}
    for mname in metrics_names:
        values = [e["raw_metrics"].get(mname, 0.0) for e in evaluations]
        per_metric[mname] = {
            "mean": round(float(np.mean(values)), 4),
            "std": round(float(np.std(values)), 4),
            "min": round(float(np.min(values)), 4),
            "max": round(float(np.max(values)), 4),
        }

    summary = {
        "benchmark_type": benchmark_type,
        "n_datasets": len(evaluations),
        "per_metric": per_metric,
        "composite_across_datasets": round(
            float(np.mean([e["composite_score"] for e in evaluations])), 4
        ) if evaluations else 0.0,
    }

    # Write outputs
    output = {
        "benchmark_type": benchmark_type,
        "evaluations": evaluations,
        "summary": summary,
    }
    metrics_path = output_dir / "benchmark_metrics.json"
    metrics_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")

    report_lines = [
        f"# Benchmark Evaluation Report ({benchmark_type})\n",
        f"Datasets evaluated: {len(evaluations)}\n",
    ]
    for e in evaluations:
        report_lines.append(f"\n## {e['slug']} / {e['dataset_id']}\n")
        report_lines.append(f"- File: {e['file']}\n")
        report_lines.append(f"- Composite score: {e['composite_score']}\n")
        for k, v in e["raw_metrics"].items():
            meta = e["metadata"].get(k, {})
            report_lines.append(f"  - **{k}** ({meta.get('description', '')}): {v}\n")
    report_lines.append(f"\n## Summary\n")
    for k, v in summary.get("per_metric", {}).items():
        report_lines.append(f"- **{k}**: mean={v['mean']}, std={v['std']}, range=[{v['min']}, {v['max']}]\n")
    report_lines.append(f"\n**Composite across datasets**: {summary['composite_across_datasets']}\n")

    report_path = output_dir / "benchmark_report.md"
    report_path.write_text("".join(report_lines), encoding="utf-8")

    logger.info("Results: %s", metrics_path)
    logger.info("Report:  %s", report_path)

    return output


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="EasyBench Benchmark Evaluator")
    parser.add_argument("--benchmark-type", required=True, help="integration / clustering / annotation / generic")
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    result = run_benchmark(
        benchmark_type=args.benchmark_type,
        input_dir=args.input_dir,
        output_dir=args.output_dir,
    )
    print(json.dumps(result["summary"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
