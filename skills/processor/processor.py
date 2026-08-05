"""Standalone scanpy preprocessing — no LLM, no OmicsClaw skills.

Reads curated.h5ad from Stage 2, runs QC + normalize + HVG + PCA,
writes processed.h5ad to a specified output directory.

For "integration" benchmark type, also runs multiple integration
methods (Harmony, BBKNN, Scanorama, etc.) and saves per-method results.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import scanpy as sc
import scipy.sparse as sp

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# Benchmark-type specific imports (lazy)
_registry = None
_healer = None


def _get_registry():
    global _registry
    if _registry is None:
        try:
            from skills.processor.registry import MethodRegistry
            _registry = MethodRegistry()
        except ImportError:
            _registry = None
    return _registry


def _get_healer():
    global _healer
    if _healer is None:
        try:
            from skills.processor.registry import SelfHealAgent
            _healer = SelfHealAgent()
        except ImportError:
            _healer = None
    return _healer


def _detect_batch_key(adata) -> str:
    """Auto-detect the batch column in adata.obs."""
    for candidate in ["batch", "sample", "donor", "orig.ident", "replicate", "Study_name", "LibraryID"]:
        if candidate in adata.obs.columns and adata.obs[candidate].nunique() >= 2:
            return candidate
    return "batch"


def _detect_label_key(adata) -> Optional[str]:
    """Auto-detect the cell-type / annotation column in adata.obs."""
    for candidate in ["cell_type", "celltype", "cell_ontology_class", "annotation", "label", "labels", "cell_identity", "celltype_annotation", "cluster"]:
        if candidate in adata.obs.columns and adata.obs[candidate].nunique() >= 2:
            return candidate
    return None


_GENE_LIKE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]*$")
_BARCODE_LIKE_RE = re.compile(
    r"^[A-Za-z0-9]+_[A-Za-z0-9]+$|"          # e.g. lbm1_AAACCTG..., SMZL1_ATGG...
    r"^[ACGTN]{6,}(-[0-9]+)?$"              # e.g. AAACCTGCAGGTGGAT-1
)


def _looks_transposed(adata, sample: int = 500, min_frac: float = 0.6) -> bool:
    """Detect if a matrix is transposed (obs=genes, var=cell barcodes).

    Heuristic: gene names never contain '_' and are not long ACGT runs;
    cell barcodes contain '_' or are long ACGT runs (often with -1 suffix).
    """
    if adata.n_obs < 50 or adata.n_vars < 50:
        return False
    rng = np.random.default_rng(0)
    obs_s = np.asarray(adata.obs_names)[rng.choice(adata.n_obs, size=min(sample, adata.n_obs), replace=False)]
    var_s = np.asarray(adata.var_names)[rng.choice(adata.n_vars, size=min(sample, adata.n_vars), replace=False)]

    def is_barcode(name):
        s = str(name)
        return "_" in s or bool(_BARCODE_LIKE_RE.fullmatch(s))

    def is_gene(name):
        s = str(name)
        return "_" not in s and bool(_GENE_LIKE_RE.fullmatch(s)) and not bool(_BARCODE_LIKE_RE.fullmatch(s))

    obs_gene_frac = float(np.mean([is_gene(g) for g in obs_s]))
    var_bc_frac = float(np.mean([is_barcode(g) for g in var_s]))
    logger.info("  transpose heuristic: obs_gene_frac=%.2f var_barcode_frac=%.2f", obs_gene_frac, var_bc_frac)
    return obs_gene_frac >= min_frac and var_bc_frac >= min_frac


def _looks_already_normalized(adata, sample: int = 5000, n_samples: int = 200) -> bool:
    """Detect data that is already log1p-transformed (+ possibly scaled).

    Counts are non-negative integers; log1p data is float with non-integer
    values and a max typically < ~20. Scaled data additionally has negatives.
    """
    if sp.issparse(adata.X):
        X = adata.X.tocsr()[:n_samples].toarray()
    else:
        X = np.asarray(adata.X[:n_samples])
    if X.size == 0:
        return False
    vals = np.unique(X[~np.isnan(X)][:sample])
    if vals.size == 0:
        return False
    non_integer = np.mean(~np.isclose(vals, np.round(vals))) > 0.3
    has_negative = float(np.nanmin(X)) < -1e-6
    max_val = float(np.nanmax(X))
    # counts: all integers, max often huge. log1p: floats, max < ~20. scaled: negatives.
    if has_negative:
        return True
    if non_integer and max_val < 25:
        return True
    return False


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

    # ── Subsample FIRST (before any dense operation) ──
    # filter_cells/filter_genes on a huge sparse matrix (e.g. 2.45e9 nonzeros)
    # allocate a dense float array per row → OOM. Subsample while still sparse.
    if adata.n_obs > 100000:
        logger.warning("Large dataset (%d cells), subsampling to 100000 for PCA", adata.n_obs)
        sc.pp.subsample(adata, n_obs=100000, random_state=42)

    # ── Auto-fix transposed matrices (obs=genes, var=cell barcodes) ──
    if _looks_transposed(adata):
        logger.warning("Detected transposed matrix — transposing to cells x genes")
        adata = adata.T.copy()
        # After transpose var_names are the original obs_names (gene symbols)
        if not adata.var_names.is_unique:
            adata.var_names_make_unique()
        if "highly_variable" in adata.var.columns:
            adata.var = adata.var.drop(columns=["highly_variable"], errors="ignore")

    # Guard against duplicate/NaN observation names (breaks boolean indexing)
    if not adata.obs_names.is_unique or adata.obs_names.hasnans:
        adata.obs_names_make_unique()

    # Guard against NaN/Inf in the matrix before any math.
    # For huge sparse matrices, checking every element allocates a dense bool
    # array (e.g. 2.45e9 elements → 18 GB) and OOMs. Sample instead.
    if sp.issparse(adata.X):
        _d = adata.X.data
        if _d.size > 5_000_000:
            _rng = np.random.default_rng(0)
            _idx = _rng.choice(_d.size, size=5_000_000, replace=False)
            _has_bad = bool(np.isnan(_d[_idx]).any() or np.isinf(_d[_idx]).any())
        else:
            _has_bad = bool(np.isnan(_d).any() or np.isinf(_d).any())
    else:
        Xarr = np.asarray(adata.X)
        _has_bad = bool(np.isnan(Xarr).any() or np.isinf(Xarr).any())
    if _has_bad:
        logger.warning("Matrix contains NaN/Inf — replacing with 0 (preprocessed artifact)")
        if sp.issparse(adata.X):
            adata.X.data = np.nan_to_num(adata.X.data, nan=0.0, posinf=0.0, neginf=0.0)
        else:
            adata.X = np.nan_to_num(adata.X, nan=0.0, posinf=0.0, neginf=0.0)

    already_norm = _looks_already_normalized(adata)
    if already_norm:
        logger.warning("Input looks already log1p-transformed/scaled — skipping normalize/log1p")

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

    adata.var["mt"] = adata.var_names.str.startswith(("MT-", "mt-"))
    sc.pp.calculate_qc_metrics(adata, qc_vars=["mt"], percent_top=None, log1p=False, inplace=True)
    # Guard against NaN in pct_counts_mt (some datasets have no MT genes → NaN)
    if "pct_counts_mt" in adata.obs.columns:
        adata.obs["pct_counts_mt"] = adata.obs["pct_counts_mt"].fillna(0.0)
    adata = adata[adata.obs.pct_counts_mt < max_mt_pct, :].copy()
    if not already_norm:
        sc.pp.normalize_total(adata, target_sum=target_sum)
        sc.pp.log1p(adata)
    # Guard right before HVG (double-safety for exotic preprocessed inputs):
    # 1) replace BOTH NaN and Inf — pd.cut (used by highly_variable_genes)
    #    throws "cannot specify integer bins when input data contains infinity".
    # 2) clip extreme values — z-scored data with huge outliers (e.g. max 489)
    #    makes scanpy's seurat-HVG expm1() overflow → inf → same crash. log1p
    #    values never exceed ~9.2, so clipping to [-10, 15] is safe.
    if sp.issparse(adata.X):
        _d = adata.X.data
        _chk = _d if _d.size <= 5_000_000 else _d[np.random.default_rng(0).choice(_d.size, size=5_000_000, replace=False)]
        _has_bad = bool(np.isnan(_chk).any() or np.isinf(_chk).any())
    else:
        _arr = np.asarray(adata.X)
        _chk = _arr.ravel()
        if _chk.size > 5_000_000:
            _chk = _chk[np.random.default_rng(0).choice(_chk.size, size=5_000_000, replace=False)]
        _has_bad = bool(np.isnan(_chk).any() or np.isinf(_chk).any())
    if _has_bad:
        logger.warning("NaN/Inf still present before HVG — replacing with 0")
        if sp.issparse(adata.X):
            adata.X.data = np.nan_to_num(adata.X.data, nan=0.0, posinf=0.0, neginf=0.0)
        else:
            adata.X = np.nan_to_num(adata.X, nan=0.0, posinf=0.0, neginf=0.0)
    if already_norm:
        # Clip extreme outliers to a log1p-plausible range to avoid expm1 overflow
        if sp.issparse(adata.X):
            adata.X.data = np.clip(adata.X.data, -10.0, 15.0)
        else:
            adata.X = np.clip(adata.X, -10.0, 15.0)
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

            # Annotation: convert Ensembl gene IDs → symbols BEFORE preprocessing,
            # so HVG selection and reference-based annotation (celltypist) match genes.
            base_type = benchmark_type.split("_")[0]
            if base_type == "annotation":
                try:
                    from skills.processor.tools.annotation import _ensure_gene_symbols
                    _ensure_gene_symbols(adata)
                except Exception as exc:
                    logger.warning("Gene symbol conversion skipped: %s", exc)

            # Step 1: Basic QC + preprocessing
            adata = preprocess_scanpy(adata)
            if adata is None:
                logger.warning("Skipped: %s (below quality thresholds)", h5_path)
                continue

            # Step 2: Run methods via registry for this benchmark type
            dataset_id = h5_path.parent.name if h5_path.parent.name != "unpacked_data" else "dataset"
            paper_out.mkdir(parents=True, exist_ok=True)

            # "integration_e2e_test2" → "integration"; generic for all types
            reg = _get_registry()
            methods = reg.available_method_names(base_type) if reg else ["none"]

            # Detect batch_key (integration) / label_key (annotation)
            batch_key = _detect_batch_key(adata)
            label_key = _detect_label_key(adata)

            method_results = reg.dispatch(
                adata, base_type,
                batch_key=batch_key,
                methods=methods,
                label_key=label_key,
            ) if reg else {}

            for method, result in method_results.items():
                # Skip methods that failed (returned {"error": ...} without adata)
                if not isinstance(result, dict) or "adata" not in result:
                    logger.warning("  [%s] Skipped (no adata in result): %s",
                                   method, result.get("error", "unknown error") if isinstance(result, dict) else result)
                    continue
                integrated_adata = result["adata"]
                out_path = paper_out / f"{dataset_id}.{method}.processed.h5ad"
                integrated_adata.write_h5ad(str(out_path))
                logger.info("  [%s] Saved: %s (%d cells)", method, out_path, integrated_adata.n_obs)
                results.append({
                    "paper": paper_path.name,
                    "method": method,
                    "n_cells": int(integrated_adata.n_obs),
                    "n_genes": int(integrated_adata.n_vars),
                    "n_hvg": int(integrated_adata.var.highly_variable.sum()) if "highly_variable" in integrated_adata.var.columns else 0,
                    "output": str(out_path),
                })

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
