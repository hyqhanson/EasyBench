"""CuratorValidator — post-execution quality checks to prevent hallucinations.

Embeds into CurationExecutor.run() to verify every curated.h5ad meets
minimum sanity criteria. Catches errors like:
  - ZERO_GENES: matrix was read as metadata instead of expression
  - TRANSPOSED: cells and genes are swapped
  - SINGLE_GENE: only one column was parsed
  - ALL_ZEROS: no expression values in the matrix
  - TOO_MANY_CELLS: unrealistic cell count
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import anndata as ad
import numpy as np


# ---------------------------------------------------------------------------
# Validation thresholds
# ---------------------------------------------------------------------------

# A valid single-cell / bulk expression matrix should have:
VALIDATION_RULES = {
    "min_cells": 3,          # at least 3 cells/samples
    "min_genes": 2,          # at least 2 genes (relaxed for bulk data)
    "max_cells": 5_000_000,  # realistic upper bound for scRNA
    "min_nonzero_pct": 0.0005, # at least 0.05% nonzero values (relaxed for sparse scRNA)
    "max_nonzero_pct": 1.0,   # at most 100% (should always be true)
}

# Cross-reference: compare with protocol expectations
# If protocol says "10X scRNA-seq, 8000 cells", and we got 2M cells, flag it.


def validate_curated_h5ad(h5ad_path: Path) -> Dict[str, Any]:
    """Validate a single curated.h5ad file.

    Returns:
        {
          "valid": bool,
          "n_cells": int, "n_genes": int,
          "flags": [...],       # list of warning/error flags
          "checks": {...},       # detailed check results
          "warnings": [...],     # non-fatal issues
          "errors": [...],       # fatal issues (hallucination-level)
        }
    """
    result = {
        "path": str(h5ad_path),
        "valid": False,
        "n_cells": 0,
        "n_genes": 0,
        "size_mb": 0,
        "flags": [],
        "checks": {},
        "warnings": [],
        "errors": [],
    }

    if not h5ad_path.exists():
        result["errors"].append("FILE_NOT_FOUND")
        return result

    result["size_mb"] = round(h5ad_path.stat().st_size / (1024 * 1024), 2)

    try:
        a = ad.read_h5ad(h5ad_path)
    except Exception as exc:
        result["errors"].append(f"CANNOT_READ: {str(exc)[:100]}")
        return result

    n_cells = a.n_obs
    n_genes = a.n_vars
    result["n_cells"] = n_cells
    result["n_genes"] = n_genes

    # --- Check 1: Non-trivial dimensions ---
    if n_cells < VALIDATION_RULES["min_cells"]:
        result["errors"].append(f"TOO_FEW_CELLS({n_cells} < {VALIDATION_RULES['min_cells']})")
        result["flags"].append("TOO_FEW_CELLS")
    if n_genes < VALIDATION_RULES["min_genes"]:
        result["errors"].append(f"TOO_FEW_GENES({n_genes} < {VALIDATION_RULES['min_genes']})")
        result["flags"].append("TOO_FEW_GENES")
    if n_cells > VALIDATION_RULES["max_cells"]:
        result["warnings"].append(f"MANY_CELLS({n_cells:,})")
        result["flags"].append("MANY_CELLS")

    # --- Check 2: Expression data sparsity ---
    is_sparse = hasattr(a.X, 'nnz')
    if is_sparse:
        total_elements = n_cells * n_genes
        nnz = a.X.nnz if total_elements > 0 else 0
        sparsity = nnz / total_elements if total_elements > 0 else 0
    else:
        # Dense matrix
        nnz = int(np.count_nonzero(a.X)) if n_cells * n_genes > 0 else 0
        sparsity = nnz / (n_cells * n_genes) if n_cells * n_genes > 0 else 0

    result["checks"]["sparsity"] = round(sparsity, 6)
    result["checks"]["nnz"] = nnz
    result["checks"]["is_sparse"] = is_sparse

    if sparsity < VALIDATION_RULES["min_nonzero_pct"]:
        result["errors"].append(f"ALL_OR_NEARLY_ZERO(sparsity={sparsity:.6f})")
        result["flags"].append("ALL_OR_NEARLY_ZERO")
    elif sparsity > VALIDATION_RULES["max_nonzero_pct"]:
        result["warnings"].append(f"DENSE_MATRIX(sparsity={sparsity:.2f})")

    # --- Check 3: Data type sanity ---
    dtype = a.X.dtype
    result["checks"]["dtype"] = str(dtype)
    if dtype not in (np.float32, np.float64, np.int32, np.int64):
        result["warnings"].append(f"UNEXPECTED_DTYPE({dtype})")

    # --- Check 4: Gene/variable names are meaningful ---
    var_names = list(a.var_names[:10]) if n_genes > 0 else []
    result["checks"]["sample_var_names"] = var_names[:5]
    if n_genes > 0:
        # Check if gene names look like actual gene symbols (not row numbers)
        numeric_names = sum(1 for v in var_names if str(v).replace('.', '').isdigit())
        if numeric_names > len(var_names) * 0.5:
            result["warnings"].append("GENE_NAMES_LOOK_NUMERIC(maybe row indices)")

    # --- Check 5: Cell/obs names are meaningful ---
    obs_names = list(a.obs_names[:10]) if n_cells > 0 else []
    result["checks"]["sample_obs_names"] = obs_names[:5]

    # --- Check 6: Transposed matrix detection ---
    # In scRNA: n_cells (obs) < n_genes (var) typically, but not always.
    # In bulk: n_samples < n_genes typically.
    # If n_cells >> n_genes and n_cells > 1000, it might be transposed.
    if n_cells > 1000 and n_genes > 0 and n_cells / max(n_genes, 1) > 100:
        result["warnings"].append(f"CELLS_TO_GENES_RATIO_HIGH({n_cells}/{n_genes}={n_cells/max(n_genes,1):.0f})")
        result["flags"].append("POSSIBLY_TRANSPOSED")

    # --- Check 7: Cross-reference with execution_plan expectations ---
    # (This is done externally by the runner)

    # --- Final verdict ---
    result["valid"] = len(result["errors"]) == 0
    return result


def cross_validate_with_protocol(
    h5ad_result: Dict[str, Any],
    execution_plan: Dict[str, Any],
    ds_id: str,
) -> Dict[str, Any]:
    """Cross-validate curated.h5ad against what the execution_plan expected.

    Example: If execution_plan says "scRNA-seq, ~8000 cells" but we got
    2M cells, something is wrong.
    """
    cross = {"matched": True, "mismatches": []}

    matched = execution_plan.get("matched_data", {}).get(ds_id, {})
    if not matched:
        return cross

    expected_format = matched.get("format", "")
    expected_loader = matched.get("suggested_loader", "")

    n_cells = h5ad_result.get("n_cells", 0)
    n_genes = h5ad_result.get("n_genes", 0)

    # If data was expected to be scRNA (10X mtx/h5ad), check cell count
    if "mtx" in expected_format.lower() or "h5ad" in expected_format.lower():
        if n_cells > 2_000_000:
            cross["mismatches"].append(f"Expected scRNA (~100k cells max), got {n_cells:,} cells")
            cross["matched"] = False
        if n_genes == 0:
            cross["mismatches"].append("Expected gene expression matrix, got 0 genes")
            cross["matched"] = False

    # If data was expected to be bulk (txt/csv), transpose check
    if "bulk" in expected_format.lower() or "csv" in expected_format.lower() or "txt" in expected_format.lower():
        if n_cells > 10000 and n_genes < 50:
            cross["mismatches"].append(f"Bulk data looks transposed: {n_cells} rows x {n_genes} cols")
            cross["matched"] = False

    # If data was non-expression (broadpeak/bigwig), it shouldn't be here
    if "broadpeak" in expected_format.lower() or "bigwig" in expected_format.lower():
        cross["mismatches"].append("Non-expression data was curated as expression!")
        cross["matched"] = False

    return cross
