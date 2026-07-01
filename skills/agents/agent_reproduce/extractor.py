"""Stage 2b — Result Extractor: parse reproduce output and extract quantitative results.

Extracts from Rmd knitr output (.md):
  - Package loading errors (missing packages)
  - File path resolution errors (here() issues)
  - Chunk-level errors/warnings
  - Quantitative values: cell counts, cluster numbers, DEG counts
  - Plot generation success/failure
  - Session info for reproducibility audit
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional


def extract_results(reproduce_dir: Path) -> Dict[str, Any]:
    """Parse all output files in reproduce_dir and extract quantitative results.

    Returns dict with:
      - status: "success" | "partial" | "failed"
      - summary: human-readable summary of what happened
      - chunks: list of chunk execution records
      - missing_packages: list of R packages that failed to load
      - file_errors: list of file path resolution errors
      - metrics: dict of extracted numeric values
      - outputs: list of output files produced
    """
    result: Dict[str, Any] = {
        "status": "unknown",
        "summary": "",
        "chunks": [],
        "missing_packages": [],
        "file_errors": [],
        "metrics": {},
        "outputs": [],
    }

    # Scan for .md knitr output files
    md_files = sorted(reproduce_dir.glob("*.md"))
    for md_file in md_files:
        text = md_file.read_text(encoding="utf-8", errors="replace")
        chunk_result = _parse_knitr_output(text, md_file.stem)
        result["chunks"].append(chunk_result)
        result["missing_packages"].extend(chunk_result.get("missing_packages", []))
        result["file_errors"].extend(chunk_result.get("file_errors", []))
        result["metrics"].update(chunk_result.get("metrics", {}))

    # Scan for other output files
    for f in reproduce_dir.iterdir():
        if f.suffix.lower() in (".csv", ".tsv", ".rds", ".rdata", ".h5ad",
                                 ".png", ".pdf", ".jpg", ".jpeg", ".svg"):
            result["outputs"].append(str(f.name))

    # Determine overall status
    if result["missing_packages"] or result["file_errors"]:
        result["status"] = "partial"
        result["summary"] = (
            f"Ran with {len(result['missing_packages'])} missing package(s) and "
            f"{len(result['file_errors'])} file error(s)"
        )
    elif result["chunks"] and all(
        c.get("status") == "ok" for c in result["chunks"]
    ):
        result["status"] = "success"
        result["summary"] = "All chunks executed successfully"
    else:
        result["status"] = "success"
        result["summary"] = "Executed with warnings"

    result["missing_packages"] = list(set(result["missing_packages"]))
    result["file_errors"] = list(set(result["file_errors"]))
    return result


def _parse_knitr_output(text: str, script_name: str) -> Dict[str, Any]:
    """Parse knitr (.md) output for errors, warnings, and metrics."""
    chunk: Dict[str, Any] = {
        "script": script_name,
        "status": "ok",
        "errors": [],
        "warnings": [],
        "missing_packages": [],
        "file_errors": [],
        "metrics": {},
    }

    # Parse line by line
    in_error_block = False
    current_error_lines = []

    for line in text.splitlines():
        stripped = line.strip()

        # Detect package load failures
        m_pkg = re.search(
            r"there is no package called ['\"]([^'\"]+)['\"]", stripped
        )
        if m_pkg:
            chunk["missing_packages"].append(m_pkg.group(1))
            chunk["status"] = "error"

        # Detect file not found errors
        m_file = re.search(
            r"cannot open file ['\"]([^'\"]+)['\"]", stripped
        )
        if m_file:
            chunk["file_errors"].append(m_file.group(1))

        # Detect here() path warnings
        m_here = re.search(r"here\(\) starts at (.+)", stripped)
        if m_here:
            chunk["warnings"].append(
                f"here() resolved to: {m_here.group(1)}"
            )

        # Detect chunk progress
        m_chunk = re.search(r"(\d+)/(\d+) \[([^\]]*)\]", stripped)
        if m_chunk:
            chunk_idx = int(m_chunk.group(1))
            chunk_name = m_chunk.group(3).strip()
            if chunk_name and chunk_name != chunk_name.strip():
                chunk.setdefault("chunk_names", []).append(chunk_name)

        # Extract numeric metrics
        _extract_numeric_metrics(stripped, chunk["metrics"])

    return chunk


def _extract_numeric_metrics(line: str, metrics: Dict[str, Any]) -> None:
    """Try to extract quantitative metrics from output lines."""
    # n cells x n genes
    m_dim = re.search(r"(\d+)\s*(?:cells|barcodes|observations)", line, re.I)
    if m_dim:
        val = int(m_dim.group(1))
        if "n_cells" not in metrics or val > metrics["n_cells"]:
            metrics["n_cells"] = val

    m_gene = re.search(r"(\d+)\s*(?:genes|features|variables)", line, re.I)
    if m_gene:
        val = int(m_gene.group(1))
        if "n_genes" not in metrics or val > metrics["n_genes"]:
            metrics["n_genes"] = val

    # Cluster count: "X clusters"
    m_clust = re.search(r"(\d+)\s+clusters?", line, re.I)
    if m_clust:
        metrics["n_clusters"] = int(m_clust.group(1))

    # PCA dimensions
    m_pca = re.search(r"(\d+)\s*(?:PC|principal\s+component)s?", line, re.I)
    if m_pca:
        metrics["n_pcs"] = int(m_pca.group(1))

    # Resolution
    m_res = re.search(r"resolution[=:\s]+([\d.]+)", line, re.I)
    if m_res:
        metrics["resolution"] = float(m_res.group(1))

    # DEG count
    m_deg = re.search(
        r"(\d+)\s*(?:DE|differential(?:ly\s+)?expressed|marker)\s*(?:genes?)?",
        line, re.I
    )
    if m_deg:
        metrics["n_degs"] = int(m_deg.group(1))

    # UMAP coordinates
    m_umap = re.search(r"(?:UMAP|tSNE)\s*(?:done|complete|computed)", line, re.I)
    if m_umap:
        metrics["dimensionality_reduction"] = True
