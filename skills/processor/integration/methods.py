"""Integration methods for benchmarking.

Each function takes a curated AnnData and returns integrated AnnData.
Lightweight — no OmicsClaw skill dependencies, no R requirements.

Available methods (auto-detected at runtime):
  none        — baseline PCA + UMAP (always available)
  mnn_ingest  — scanpy ingest-based MNN (always available)
  scanorama   — panoramic stitching (requires: pip install scanorama)
  harmony     — requires: pip install harmonypy
  bbknn       — requires: pip install bbknn
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Dict, List, Optional

import numpy as np
import scanpy as sc

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Method interface
# ---------------------------------------------------------------------------

IntegrationResult = Dict[str, Any]
"""Return type: {"method": str, "embedding_key": str, "adata": AnnData}"""


def _check_dep(name: str) -> bool:
    """Check if an optional dependency is available."""
    try:
        importlib.import_module(name)
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# No integration (baseline)
# ---------------------------------------------------------------------------

def run_no_integration(adata, batch_key: str = "batch", **kwargs) -> IntegrationResult:
    """Baseline — PCA + UMAP without any batch correction."""
    logger.info("No integration — PCA + UMAP on raw merged data")
    sc.pp.neighbors(adata, n_pcs=kwargs.get("n_pcs", 50))
    sc.tl.umap(adata)
    return {"method": "none", "embedding_key": "X_pca", "adata": adata}


# ---------------------------------------------------------------------------
# Harmony (Python)
# ---------------------------------------------------------------------------

def run_harmony(adata, batch_key: str = "batch", **kwargs) -> IntegrationResult:
    """Harmony batch correction via harmonypy."""
    if not _check_dep("harmonypy"):
        raise ImportError("harmonypy not installed.")

    logger.info("Harmony integration on %d batches", adata.obs[batch_key].nunique())

    # Harmony needs PCA first
    if "X_pca" not in adata.obsm:
        sc.tl.pca(adata, n_comps=kwargs.get("n_pcs", 50), svd_solver="arpack")

    meta = adata.obs[[batch_key]].copy()
    ho = harmonypy.run_harmony(
        adata.obsm["X_pca"], meta, vars_use=[batch_key],
        max_iter_harmony=kwargs.get("max_iter", 10),
        theta=kwargs.get("theta", 2.0),
        nclust=kwargs.get("n_clusters", 50),
    )
    adata.obsm["X_harmony"] = ho.Z_corr.T
    sc.pp.neighbors(adata, use_rep="X_harmony", n_pcs=ho.Z_corr.shape[0])
    sc.tl.umap(adata)
    return {"method": "harmony", "embedding_key": "X_harmony", "adata": adata}


# ---------------------------------------------------------------------------
# BBKNN (Python)
# ---------------------------------------------------------------------------

def run_bbknn(adata, batch_key: str = "batch", **kwargs) -> IntegrationResult:
    """BBKNN — batch-balanced k-nearest neighbors."""
    if not _check_dep("bbknn"):
        raise ImportError("bbknn not installed.")

    logger.info("BBKNN on %d batches", adata.obs[batch_key].nunique())
    if "X_pca" not in adata.obsm:
        sc.tl.pca(adata, n_comps=kwargs.get("n_pcs", 50), svd_solver="arpack")
    bbknn.bbknn(adata, batch_key=batch_key, neighbors_within_batch=kwargs.get("neighbors_within_batch", 3))
    sc.tl.umap(adata)
    return {"method": "bbknn", "embedding_key": "X_pca", "adata": adata}


# ---------------------------------------------------------------------------
# Scanorama (Python)
# ---------------------------------------------------------------------------

def run_scanorama(adata, batch_key: str = "batch", **kwargs) -> IntegrationResult:
    """Scanorama — panoramic stitching."""
    try:
        import scanorama
    except ImportError:
        raise ImportError("scanorama not installed. Run: pip install scanorama")

    logger.info("Scanorama on %d batches", adata.obs[batch_key].nunique())
    # Split by batch
    batches = {}
    for b in adata.obs[batch_key].unique():
        subset = adata[adata.obs[batch_key] == b].copy()
        sc.pp.filter_genes(subset, min_cells=1)
        batches[b] = subset

    corrected = scanorama.correct_scanpy(list(batches.values()), return_dimred=True)
    adata.obsm["X_scanorama"] = corrected[0]  # merged corrected embedding
    sc.pp.neighbors(adata, use_rep="X_scanorama")
    sc.tl.umap(adata)
    return {"method": "scanorama", "embedding_key": "X_scanorama", "adata": adata}


# ---------------------------------------------------------------------------
# MinHash (lightweight alternative to fastMNN — no R needed)
# ---------------------------------------------------------------------------

def run_minhash(adata, batch_key: str = "batch", **kwargs) -> IntegrationResult:
    """MNN-based correction using scanpy's own ingest (no R required).

    A simple MNN-inspired approach: merge all data, compute PCA,
    then use scanpy's ingesting to correct.
    """
    logger.info("MNN-merge (ingest) on %d batches", adata.obs[batch_key].nunique())
    # Reference is the largest batch
    batch_counts = adata.obs[batch_key].value_counts()
    ref_batch = batch_counts.index[0]
    ref = adata[adata.obs[batch_key] == ref_batch].copy()
    query = adata[adata.obs[batch_key] != ref_batch].copy()

    if "X_pca" not in ref.obsm:
        sc.tl.pca(ref, n_comps=kwargs.get("n_pcs", 50), svd_solver="arpack")
    sc.pp.neighbors(ref)
    sc.tl.umap(ref)

    sc.tl.ingest(query, ref, obs=batch_key)
    # Merge back
    corrected = ref.concatenate(query, batch_key="merge_batch")
    adata.obsm["X_ingest"] = corrected.obsm["X_pca"]
    sc.pp.neighbors(adata, use_rep="X_ingest")
    sc.tl.umap(adata)
    return {"method": "mnn_ingest", "embedding_key": "X_ingest", "adata": adata}


# ---------------------------------------------------------------------------
# Registry (auto-detected)
# ---------------------------------------------------------------------------

_AVAILABLE_METHODS: List[str] = ["none", "mnn_ingest"]
if _check_dep("scanorama"):
    _AVAILABLE_METHODS.append("scanorama")
if _check_dep("harmonypy"):
    _AVAILABLE_METHODS.append("harmony")
if _check_dep("bbknn"):
    _AVAILABLE_METHODS.append("bbknn")

DEFAULT_METHODS = ["none", "scanorama"] if "scanorama" in _AVAILABLE_METHODS else ["none", "mnn_ingest"]


def run_all_methods(adata, batch_key: str = "batch", methods: Optional[List[str]] = None) -> Dict[str, IntegrationResult]:
    """Run all integration methods on the same adata, returning per-method results."""
    results = {}
    methods = methods or DEFAULT_METHODS
    registry = {
        "none": run_no_integration,
        "harmony": run_harmony,
        "bbknn": run_bbknn,
        "scanorama": run_scanorama,
        "mnn_ingest": run_minhash,
    }
    for method in methods:
        fn = registry.get(method)
        if fn is None:
            logger.warning("Unknown method: %s", method)
            continue
        logger.info("Running method: %s", method)
        try:
            result = fn(adata.copy(), batch_key=batch_key)
            results[method] = result
        except Exception as exc:
            logger.error("Method %s failed: %s", method, exc)
    return results
