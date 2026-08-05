"""Thin adapter layer for benchmark metrics.

Wraps mature, community-maintained metric libraries (scIB, sklearn, scanpy)
so that the EasyBench evaluator uses standardized metrics instead of
re-implementing them. Each function takes an AnnData + column names and
returns a float metric value.

Backends (auto-detected):
  - scib-metrics  (scIB standard: iLISI, cLISI, batch ASW, cell-type ASW, ...)
  - sklearn       (silhouette, ARI, NMI, ...)
  - scanpy        (clustering, neighbors, ...)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Dependency detection ───────────────────────────────────────────────
_SCIB_AVAILABLE = False
try:
    import scib_metrics as _scib
    _SCIB_AVAILABLE = True
except ImportError:
    _scib = None


def _scib_neighbors(adata, embedding_key: Optional[str] = None):
    """Build scIB NeighborsResults from an AnnData embedding."""
    import numpy as np
    from scib_metrics.nearest_neighbors import NeighborsResults

    key = embedding_key or _pick_embedding(adata)
    if key is None:
        return None
    emb = np.asarray(adata.obsm[key]).astype(np.float32)
    n = emb.shape[0]
    k = min(90, max(10, int(0.2 * n)))
    k = min(k, n - 1)
    if k < 2:
        return None

    from sklearn.neighbors import NearestNeighbors
    model = NearestNeighbors(n_neighbors=k + 1, metric="euclidean")
    model.fit(emb)
    # fit includes self-neighbor; drop first column (self)
    indices = model.kneighbors(emb, n_neighbors=k + 1, return_distance=False)[:, 1:]
    distances = model.kneighbors(emb, n_neighbors=k + 1, return_distance=True)[0][:, 1:]
    return NeighborsResults(indices.astype(np.int64), distances.astype(np.float32))

_SKLEARN_AVAILABLE = True
try:
    from sklearn.metrics import silhouette_score, adjusted_rand_score, normalized_mutual_info_score
except ImportError:
    _SKLEARN_AVAILABLE = False


# ── Batch-correction metrics (scIB standard) ───────────────────────────

def ilisi(adata, batch_key: str = "batch", label_key: Optional[str] = None, **kwargs) -> Optional[float]:
    """Integration LISI — measures batch mixing (higher = better).

    Uses scIB's implementation when available; falls back to a local
    kNN-based computation otherwise.
    """
    import numpy as np

    if batch_key not in adata.obs.columns or adata.obs[batch_key].nunique() < 2:
        return None

    if not _SCIB_AVAILABLE or not hasattr(_scib, "ilisi_knn"):
        return _ilisi_fallback(adata, batch_key)
    try:
        nbrs = _scib_neighbors(adata, kwargs.pop("embedding_key", None))
        if nbrs is None:
            return _ilisi_fallback(adata, batch_key)
        batches = adata.obs[batch_key].astype(str).to_numpy()
        return float(_scib.ilisi_knn(nbrs, batches, **kwargs))
    except Exception as exc:
        logger.warning("scIB ilisi failed (%s), using fallback", exc)
        return _ilisi_fallback(adata, batch_key)


def clisi(adata, batch_key: str = "batch", label_key: str = "cell_type", **kwargs) -> Optional[float]:
    """Cell-type LISI — measures cell-type purity preservation (higher = better)."""
    import numpy as np

    if batch_key not in adata.obs.columns or label_key not in adata.obs.columns:
        return None
    if not _SCIB_AVAILABLE or not hasattr(_scib, "clisi_knn"):
        return None
    try:
        nbrs = _scib_neighbors(adata, kwargs.pop("embedding_key", None))
        if nbrs is None:
            return None
        labels = adata.obs[label_key].astype(str).to_numpy()
        return float(_scib.clisi_knn(nbrs, labels, **kwargs))
    except Exception as exc:
        logger.warning("scIB clisi failed: %s", exc)
        return None


def batch_asw(adata, batch_key: str = "batch", label_key: Optional[str] = None, **kwargs) -> Optional[float]:
    """Batch silhouette width (higher = better, range [-1, 1]).

    Uses sklearn's silhouette_score on the method's embedding, which is
    the standard batch ASW definition (scIB's own silhouette_batch has
    edge-case bugs when labels==batch).
    """
    import numpy as np
    from sklearn.metrics import silhouette_score

    if batch_key not in adata.obs.columns or adata.obs[batch_key].nunique() < 2:
        return None
    emb_key = kwargs.pop("embedding_key", None) or _pick_embedding(adata)
    if emb_key is None:
        return None
    try:
        emb = np.asarray(adata.obsm[emb_key]).astype(np.float32)
        batches = adata.obs[batch_key].to_numpy()
        n = adata.n_obs
        if n > 20000:
            rng = np.random.default_rng(0)
            idx = rng.choice(n, size=20000, replace=False)
            emb, batches = emb[idx], batches[idx]
        return float(silhouette_score(emb, batches))
    except Exception as exc:
        logger.warning("batch_asw failed (%s), using fallback", exc)
        return _silhouette_batch_fallback(adata, batch_key, label_key)


def celltype_asw(adata, batch_key: str = "batch", label_key: str = "cell_type", **kwargs) -> Optional[float]:
    """Cell-type silhouette width — biology preservation (higher = better)."""
    return batch_asw(adata, batch_key=batch_key, label_key=label_key, **kwargs)


# ── Clustering / annotation metrics ────────────────────────────────────

def annotation_f1(labels_true, labels_pred) -> Optional[float]:
    """F1-macro between ground-truth and predicted cell-type labels."""
    if not _SKLEARN_AVAILABLE:
        return None
    try:
        from sklearn.metrics import f1_score
        return float(f1_score(labels_true, labels_pred, average="macro", zero_division=0))
    except Exception:
        return None


def annotation_accuracy(labels_true, labels_pred) -> Optional[float]:
    """Classification accuracy between ground-truth and predicted labels."""
    if not _SKLEARN_AVAILABLE:
        return None
    try:
        from sklearn.metrics import accuracy_score
        return float(accuracy_score(labels_true, labels_pred))
    except Exception:
        return None


def cluster_ari(labels_true, labels_pred) -> Optional[float]:
    """Adjusted Rand Index between two clusterings."""
    if not _SKLEARN_AVAILABLE:
        return None
    try:
        return float(adjusted_rand_score(labels_true, labels_pred))
    except Exception:
        return None


def cluster_nmi(labels_true, labels_pred) -> Optional[float]:
    """Normalized Mutual Information between two clusterings."""
    if not _SKLEARN_AVAILABLE:
        return None
    try:
        return float(normalized_mutual_info_score(labels_true, labels_pred))
    except Exception:
        return None


def silhouette(embedding, labels) -> Optional[float]:
    """Silhouette score of a labeling on an embedding."""
    if not _SKLEARN_AVAILABLE:
        return None
    try:
        return float(silhouette_score(embedding, labels))
    except Exception:
        return None


# ── Quality / QC metrics (scanpy-based) ────────────────────────────────

def n_cells(adata) -> int:
    return int(adata.n_obs)


def n_genes(adata) -> int:
    return int(adata.n_vars)


def n_hvg(adata) -> int:
    if "highly_variable" in adata.var.columns:
        return int(adata.var.highly_variable.sum())
    return 0


def n_pcs(adata, embedding_key: Optional[str] = None) -> int:
    key = embedding_key or "X_pca"
    if key in adata.obsm:
        return int(adata.obsm[key].shape[1])
    return 0


def n_batches(adata, batch_key: str = "batch") -> int:
    if batch_key in adata.obs.columns:
        return int(adata.obs[batch_key].nunique())
    return 0


# ── Fallbacks (when scIB is unavailable) ───────────────────────────────

def _ilisi_fallback(adata, batch_key: str) -> Optional[float]:
    """Local iLISI approximation via kNN batch entropy."""
    try:
        import numpy as np
        import scanpy as sc
        from sklearn.neighbors import NearestNeighbors

        if batch_key not in adata.obs.columns or adata.obs[batch_key].nunique() < 2:
            return None

        emb_key = _pick_embedding(adata)
        if emb_key is None:
            return None
        emb = np.asarray(adata.obsm[emb_key])

        k = min(90, max(10, int(0.1 * adata.n_obs)))
        nbrs = NearestNeighbors(n_neighbors=k + 1).fit(emb)
        _, indices = nbrs.kneighbors(emb)
        batch_arr = adata.obs[batch_key].astype(str).to_numpy()

        scores = []
        for i in range(adata.n_obs):
            neigh = batch_arr[indices[i]]
            counts = np.bincount(
                np.unique(neigh, return_inverse=True)[1]
            )
            p = counts / counts.sum()
            p = p[p > 0]
            scores.append(-np.sum(p * np.log(p)))
        return float(np.mean(scores))
    except Exception as exc:
        logger.warning("ilisi fallback failed: %s", exc)
        return None


def _silhouette_batch_fallback(adata, batch_key: str, label_key: Optional[str]) -> Optional[float]:
    """Local batch silhouette: average silhouette of batch labels, normalized."""
    try:
        import numpy as np
        from sklearn.metrics import silhouette_score

        if batch_key not in adata.obs.columns or adata.obs[batch_key].nunique() < 2:
            return None
        emb_key = _pick_embedding(adata)
        if emb_key is None:
            return None
        emb = np.asarray(adata.obsm[emb_key])
        n = min(adata.n_obs, 20000)  # sample for speed
        rng = np.random.default_rng(0)
        idx = rng.choice(adata.n_obs, size=n, replace=False)
        return float(silhouette_score(emb[idx], adata.obs[batch_key].to_numpy()[idx]))
    except Exception as exc:
        logger.warning("silhouette_batch fallback failed: %s", exc)
        return None


def _pick_embedding(adata) -> Optional[str]:
    """Pick the best embedding present (method-specific embeddings first)."""
    for key in ("X_harmony", "X_ingest", "X_scanorama", "X_pca"):
        if key in adata.obsm:
            return key
    return None


# ── Composite registry ─────────────────────────────────────────────────

# Map metric name → (function, requires label_key)
METRIC_REGISTRY: Dict[str, Dict[str, Any]] = {
    "mean_ilisi": {"fn": ilisi, "needs_label": False, "higher_better": True},
    "mean_clisi": {"fn": clisi, "needs_label": True, "higher_better": True},
    "batch_asw": {"fn": batch_asw, "needs_label": False, "higher_better": True},
    "celltype_asw": {"fn": celltype_asw, "needs_label": True, "higher_better": True},
    "n_cells": {"fn": n_cells, "needs_label": False, "higher_better": None},
    "n_genes": {"fn": n_genes, "needs_label": False, "higher_better": None},
    "n_hvg": {"fn": n_hvg, "needs_label": False, "higher_better": None},
    "n_pcs": {"fn": n_pcs, "needs_label": False, "higher_better": None},
    "n_batches": {"fn": n_batches, "needs_label": False, "higher_better": None},
}


def compute_standard_metrics(
    adata,
    batch_key: str = "batch",
    label_key: Optional[str] = None,
    embedding_key: Optional[str] = None,
) -> Dict[str, float]:
    """Compute a standardized set of integration metrics for an AnnData.

    This is the recommended replacement for the hand-written
    ``omicsclaw.autoagent.metrics_compute.compute_metrics_from_adata``.
    """
    results: Dict[str, float] = {}
    for name, spec in METRIC_REGISTRY.items():
        fn = spec["fn"]
        try:
            if name == "n_pcs":
                val = fn(adata, embedding_key)
            elif spec["needs_label"]:
                val = fn(adata, batch_key=batch_key, label_key=label_key)
            else:
                val = fn(adata, batch_key=batch_key)
            if val is not None:
                results[name] = float(val)
        except Exception as exc:
            logger.debug("Metric %s failed: %s", name, exc)
    return results


def compute_standard_annotation_metrics(
    adata,
    label_key: str = "cell_type",
    method_label_col: str = None,
) -> Dict[str, float]:
    """Compute annotation accuracy metrics for a labeled AnnData.

    Compares ground-truth ``label_key`` against a method's predicted
    labels column (e.g. ``logreg_labels``, ``celltypist_labels``).

    Also always reports (no ground-truth needed):
      - ``n_cell_types``: number of distinct predicted cell types
      - ``mean_confidence``: mean of the ``*_scores`` column (if present)

    Args:
        adata: AnnData with ground-truth labels + method predicted labels.
        label_key: ground-truth column name.
        method_label_col: predicted-labels column; if None, auto-detect
            the first ``*_labels`` column (excluding label_key).
    """
    import numpy as np

    results: Dict[str, float] = {}

    # Auto-detect predicted label column if not given
    if method_label_col is None:
        for col in adata.obs.columns:
            if col.endswith("_labels") and col != label_key:
                method_label_col = col
                break

    if method_label_col is None or method_label_col not in adata.obs.columns:
        return results

    y_pred = adata.obs[method_label_col].astype(str).to_numpy()

    # ── n_cell_types: distinct predicted labels (exclude unassigned placeholders) ──
    _UNASSIGNED = {"", "nan", "na", "unknown", "unassigned", "u", "unlabeled", "none"}
    known = np.array([p for p in y_pred if str(p).strip().lower() not in _UNASSIGNED])
    results["n_cell_types"] = float(len(np.unique(known))) if known.size else 0.0

    # ── mean_confidence: average of the matching *_scores column, if present ──
    scores_col = None
    base = method_label_col.rsplit("_labels", 1)[0]
    for col in (f"{base}_scores", f"{base}_confidence", "celltypist_scores", "max_probability"):
        if col in adata.obs.columns:
            scores_col = col
            break
    if scores_col is not None:
        try:
            s = np.asarray(adata.obs[scores_col], dtype=float)
            s = s[~np.isnan(s)]
            if s.size:
                results["mean_confidence"] = float(np.mean(s))
        except (TypeError, ValueError):
            pass

    # ── Accuracy / F1 / ARI only when ground-truth labels exist ──
    if label_key not in adata.obs.columns:
        return {k: v for k, v in results.items() if v is not None}

    y_true = adata.obs[label_key].astype(str).to_numpy()

    # Only compare rows where ground truth is known
    valid = np.array([
        t not in ("nan", "NA", "unknown", "Unknown", "", "Unassigned")
        for t in y_true
    ])
    if valid.sum() == 0:
        return {k: v for k, v in results.items() if v is not None}
    y_true_v, y_pred_v = y_true[valid], y_pred[valid]

    acc = annotation_accuracy(y_true_v, y_pred_v)
    f1 = annotation_f1(y_true_v, y_pred_v)
    ari = cluster_ari(y_true_v, y_pred_v)

    results["annotation_accuracy"] = acc if acc is not None else None
    results["annotation_f1"] = f1 if f1 is not None else None
    results["annotation_ari"] = ari if ari is not None else None
    results["n_labeled_cells"] = float(valid.sum())
    # Keep the method's own train accuracy if recorded
    train_acc = adata.uns.get(f"{method_label_col.split('_')[0]}_train_acc")
    if train_acc is not None:
        results["train_accuracy"] = float(train_acc)

    return {k: v for k, v in results.items() if v is not None}
