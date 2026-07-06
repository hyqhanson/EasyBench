"""Standalone scanpy preprocessing — no LLM, no OmicsClaw skills.

Reads curated.h5ad from Stage 2, runs QC + normalize + HVG + PCA,
writes processed.h5ad to a specified output directory.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict

import numpy as np
import scanpy as sc

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def preprocess_scanpy(
    adata,
    *,
    min_genes: int = 200,
    min_cells: int = 3,
    max_mt_pct: float = 20.0,
    n_top_hvg: int = 2000,
    n_pcs: int = 50,
    target_sum: float = 10000.0,
):
    """Minimal scanpy preprocessing: QC → normalize → HVG → PCA."""
    logger.info("Input: %d cells x %d genes", adata.n_obs, adata.n_vars)

    if "counts" in adata.layers:
        raw_snapshot = adata.copy()
        raw_snapshot.X = adata.layers["counts"].copy()
        adata.raw = raw_snapshot
    elif adata.raw is None:
        adata.raw = adata.copy()

    sc.pp.filter_cells(adata, min_genes=min_genes)
    sc.pp.filter_genes(adata, min_cells=min_cells)

    # ── Skip if too few genes (likely artifact / bulk-transposed data) ──
    if adata.n_vars < n_top_hvg // 4:
        logger.warning("Too few genes (%d) after filtering - skipping (artifact/bulk data)", adata.n_vars)
        return None

    # ── Skip if too few cells ──
    if adata.n_obs < 10:
        logger.warning("Too few cells (%d) after filtering - skipping", adata.n_obs)
        return None

    # ── Subsample if huge ──
    if adata.n_obs > 100000:
        logger.warning("Large dataset (%d cells), subsampling to 100000 for PCA", adata.n_obs)
        sc.pp.subsample(adata, n_obs=100000, random_state=42)

    adata.var["mt"] = adata.var_names.str.startswith("MT-")
    sc.pp.calculate_qc_metrics(adata, qc_vars=["mt"], percent_top=None, log1p=False, inplace=True)
    adata = adata[adata.obs.pct_counts_mt < max_mt_pct, :].copy()
    sc.pp.normalize_total(adata, target_sum=target_sum)
    sc.pp.log1p(adata)
    sc.pp.highly_variable_genes(adata, n_top_genes=n_top_hvg, flavor="seurat", batch_key=None)
    adata.raw = adata
    adata = adata[:, adata.var.highly_variable].copy()
    # Use sc.pp.scale only if data fits in memory; otherwise PCA handles sparse internally
    try:
        sc.pp.scale(adata, max_value=10)
    except (MemoryError, np._core._exceptions._ArrayMemoryError):
        logger.warning("sc.pp.scale OOM — skipping scaling, PCA will use sparse input")

    # Adjust n_pcs if dataset is too small
    n_comps = min(n_pcs, min(adata.n_obs, adata.n_vars) - 1)
    if n_comps < 1:
        logger.warning("Dataset too small for PCA, skipping")
        return adata
    sc.tl.pca(adata, n_comps=n_comps, svd_solver="arpack")

    logger.info("Output: %d cells x %d genes, %d HVGs, %d PCs",
                adata.n_obs, adata.n_vars, adata.var.highly_variable.sum(), adata.obsm["X_pca"].shape[1])
    return adata


def run_processor(
    benchmark_type: str = "integration",
    data_root: Path = Path("benchmark_data"),
    output_dir: Path = Path("."),
) -> Dict[str, Any]:
    """Preprocess curated.h5ad files and write processed.h5ad to output_dir/{slug}/.

    Designed to be called from benchmark_suite.py run_stage_process_data.
    The output dir mirrors the slug structure so Stage 6 can find per-paper results.
    """
    data_dir = data_root / benchmark_type
    if not data_dir.exists():
        print(f"  ⚠️  Data dir not found: {data_dir}")
        return {"status": "no_data_dir", "papers": []}

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []

    for paper_path in sorted(data_dir.iterdir()):
        if not paper_path.is_dir() or paper_path.name.startswith("_"):
            continue
        curated_files = sorted(paper_path.rglob("curated.h5ad"))
        curated_files = [f for f in curated_files if "._" not in str(f) and "_tmp" not in str(f)]
        if not curated_files:
            continue

        paper_out = output_dir / paper_path.name
        paper_out.mkdir(parents=True, exist_ok=True)
        print(f"  🌀 [{paper_path.name[:45]}] processing...")

        for h5_path in curated_files:
            logger.info("Loading: %s", h5_path)
            adata = sc.read_h5ad(str(h5_path))

            adata = preprocess_scanpy(adata)
            if adata is None:
                logger.warning("Skipped: %s (below quality thresholds)", h5_path)
                continue

            # Use dataset folder name to avoid overwrite
            dataset_id = h5_path.parent.name if h5_path.parent.name != "unpacked_data" else "dataset"
            out_path = paper_out / f"{dataset_id}.processed.h5ad"
            adata.write_h5ad(str(out_path))
            n_hvg = int(adata.var.highly_variable.sum()) if "highly_variable" in adata.var.columns else 0
            n_pcs = int(adata.obsm["X_pca"].shape[1]) if "X_pca" in adata.obsm else 0
            results.append({
                "paper": paper_path.name,
                "n_cells": int(adata.n_obs),
                "n_genes": int(adata.n_vars),
                "n_hvg": n_hvg,
                "n_pcs": n_pcs,
                "output": str(out_path),
            })
            logger.info("Saved: %s (%d cells, %d HVGs)", out_path, adata.n_obs, n_hvg)

    summary_path = output_dir / "_processor_summary.json"
    summary_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"status": "completed", "papers": len(results), "results": results}


def main():
    parser = argparse.ArgumentParser(description="Processor — scanpy preprocessing")
    parser.add_argument("--benchmark-type", default="integration")
    parser.add_argument("--data-root", type=Path, default=Path("benchmark_data"))
    parser.add_argument("--output-dir", type=Path, default=Path("."))
    args = parser.parse_args()
    result = run_processor(
        benchmark_type=args.benchmark_type,
        data_root=args.data_root,
        output_dir=args.output_dir,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
