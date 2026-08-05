"""Stage 1 AgentCurator — LLM-driven data format detection & conversion plan.

Unlike the old ``_find_sc_data_files()`` hardcoded rules, AgentCurator lets
the LLM inspect each dataset's file listing and produce a curation_plan.json
that describes exactly how to convert raw data into a standard AnnData.

Architecture:
    AgentCurator.curate() → curation_plan.json
      ↓
    curation_steps are then executed by a deterministic runner
      ↓
    data/{gse_id}/curated.h5ad
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM call wrapper (shared with agent_preflight)
# ---------------------------------------------------------------------------

def _ensure_api_key_loaded() -> None:
    """Load DEEPSEEK_API_KEY from Windows User env vars if not set."""
    import os
    if os.environ.get("DEEPSEEK_API_KEY", "").strip():
        return
    try:
        import subprocess
        key = subprocess.check_output(
            'powershell -c "[Environment]::GetEnvironmentVariable('
            "'DEEPSEEK_API_KEY', 'User')" '"',
            shell=True, text=True,
        ).strip()
        if key:
            os.environ["DEEPSEEK_API_KEY"] = key
    except Exception:
        pass


_DEFAULT_LLM_MODEL = "deepseek-v4-flash"


def _call_llm(prompt: str, system_prompt: str = "",
              temperature: float = 0.1,
              model: str = "") -> Optional[str]:
    """Call the OmicsClaw LLM. Returns None if unavailable.

    Parameters
    ----------
    model:
        Explicit model name override (e.g. "deepseek-v4-flash" or
        "deepseek-v4-pro"). When empty, uses ``_DEFAULT_LLM_MODEL``.
    """
    _ensure_api_key_loaded()
    try:
        from omicsclaw.autoagent.llm_client import call_llm
    except Exception as exc:
        logger.warning("LLM client import failed: %s", exc)
        return None

    # Retry a few times — DeepSeek can intermittently return empty/rate-limit.
    for attempt in range(1, 4):
        try:
            resp = call_llm(
                prompt,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=4096,
                llm_model=model or _DEFAULT_LLM_MODEL,
            )
            if resp and resp.strip():
                return resp
            logger.warning("LLM returned empty response (attempt %d/3)", attempt)
        except Exception as exc:
            logger.warning("LLM call failed (attempt %d/3): %s", attempt, exc)
    return None


def _parse_llm_json(raw: str) -> Optional[Dict[str, Any]]:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
    if raw.endswith("```"):
        raw = raw.rsplit("```", 1)[0]
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Data directory scanning (similar to scanner but curator needs more detail)
# ---------------------------------------------------------------------------

def _scan_datasets(data_root: Path, max_files_per: int = 15) -> Dict[str, Any]:
    """Build a lightweight inventory of all datasets under data_root.

    For each subdirectory (dataset), list file names, extensions, and
    whether key marker files exist (barcodes, features, matrix, etc.).
    """
    datasets = {}
    for entry in sorted(data_root.iterdir()):
        if not entry.is_dir():
            continue
        files_info = []
        extensions = set()
        total_size = 0
        all_files = sorted(entry.rglob("*"))
        for f in all_files:
            if f.is_file():
                rel = str(f.relative_to(entry))
                sz = f.stat().st_size
                ext = f.suffix.lower()
                extensions.add(ext)
                total_size += sz
                if len(files_info) < max_files_per:
                    files_info.append({"name": rel, "size_mb": round(sz / (1024 * 1024), 2),
                                       "extension": ext})

        # Detect key marker files
        marker_signals = {
            "has_barcodes": any("barcode" in f["name"].lower() for f in files_info),
            "has_features": any("feature" in f["name"].lower() or "gene" in f["name"].lower()
                                for f in files_info),
            "has_mtx": ".mtx" in extensions,
            "has_h5ad": ".h5ad" in extensions,
            "has_h5": ".h5" in extensions,
            "has_rds": ".rds" in extensions,
            "has_cel": ".cel" in extensions,
            "has_bgx": ".bgx" in extensions,
            "has_tar": ".tar" in extensions,
            "has_gz": ".gz" in extensions,
            "has_csv": ".csv" in extensions,
            "has_tsv": ".tsv" in extensions,
            "has_txt": ".txt" in extensions,
            "has_xlsx": ".xlsx" in extensions,
            "has_broadpeak": ".broadpeak" in extensions or ".bw" in extensions,
        }

        datasets[entry.name] = {
            "file_count": len(all_files),
            "total_size_mb": round(total_size / (1024 * 1024), 1),
            "extensions": sorted(extensions),
            "markers": marker_signals,
            "sample_files": files_info,
        }
    return datasets


# ---------------------------------------------------------------------------
# AgentCurator
# ---------------------------------------------------------------------------

class AgentCurator:
    """LLM-driven data curator: produces curation_plan.json for one paper.

    Reads the paper's execution_plan.json (from Stage 0.5) for context about
    what each dataset should be, then inspects file listings to determine
    the correct conversion path for each dataset.
    """

    # ── Recognized format → conversion strategy mapping (LLM hint) ──
    FORMAT_HINTS = {
        "10X_mtx": "scanpy.read_10x_mtx — .mtx + barcodes.tsv + features.tsv",
        "10X_h5": "scanpy.read_10x_h5 — filtered_feature_bc_matrix.h5",
        "h5ad": "scanpy.read_h5ad — directly loadable AnnData",
        "rds_seurat": "pyreadr or SeuratDisk (R) to convert to .h5ad",
        "bulk_count_txt": "pandas.read_csv + transpose + AnnData(X=...)",
        "bulk_count_csv": "pandas.read_csv + transpose + AnnData(X=...)",
        "cel_affymetrix": "oligo::read.celfiles (R) or pyAffy — microarray, NOT scRNA",
        "bgx_illumina": "limma::neqc (R) — Illumina BeadChip, NOT scRNA",
        "series_matrix": "GEOparse — GEO SOFT format, need to reconstruct count matrix",
        "broadpeak": "pybedtools — ChIP-seq/CUT&Tag peak files, NOT expression data",
        "bigwig": "pyBigWig — genomic coverage track, NOT expression data",
        "tsv_de_results": "pandas.read_csv — precomputed differential expression table",
        "unknown_tar": "tar -xf then re-scan contents",
    }

    # ── Available OmicsClaw tools for downstream analysis ──
    # Key: data_type → {tool_name: command_template}
    DOWNSTREAM_TOOLS = {
        "bulk_RNA": {
            "bulkrna-de": "python skills/bulkrna/bulkrna-de/bulkrna_de.py --input {input} --output {output_dir} --control-prefix {ctrl} --treat-prefix {treat}",
            "bulkrna-qc": "python skills/bulkrna/bulkrna-qc/bulkrna_qc.py --input {input} --output {output_dir}",
        },
    }

    def __init__(self, paper_dir: Path, execution_plan: Optional[Dict[str, Any]] = None) -> None:
        self.paper_dir = paper_dir
        self.slug = paper_dir.name
        # Use Stage 0.5 execution_plan for context on what each dataset IS
        self.execution_plan = execution_plan or {}

    def curate(self) -> Dict[str, Any]:
        """Generate curation_plan.json via LLM + write it to disk."""
        # 1. Find data source dir
        up = self.paper_dir / "unpacked_data"
        data_root = up if up.exists() else (self.paper_dir / "data")
        if not data_root.exists():
            return {"paper": self.slug, "error": "no_data_dir", "datasets": []}

        # 2. Scan
        datasets = _scan_datasets(data_root)
        if not datasets:
            return {"paper": self.slug, "error": "no_datasets_found", "datasets": []}

        # 3. Read protocol (for what the paper says about its data)
        protocol = {}
        pp = self.paper_dir / "experimental_protocol.json"
        if pp.exists():
            protocol = json.loads(pp.read_text(encoding="utf-8"))

        # 4. Build prompt & call LLM
        # If too many datasets, batch them to avoid token overflow
        if len(datasets) > 10:
            plan = self._curate_batched(datasets, protocol)
        else:
            prompt = self._build_prompt(datasets, protocol)
            raw = _call_llm(prompt, self._system_prompt())
            plan = _parse_llm_json(raw) if raw else None

        if not plan:
            plan = self._fallback_plan(datasets)

        plan.setdefault("paper", self.slug)
        plan.setdefault("curated_at", self._now_iso())

        # 5. Write
        out = self.paper_dir / "curation_plan.json"
        out.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Curation plan written: %s", out)
        return plan

    def _build_prompt(self, datasets: Dict[str, Any], protocol: Dict[str, Any]) -> str:
        # Protocol excerpt (data section only)
        proto_snippet = ""
        if protocol:
            steps = protocol.get("steps", [])
            data_steps = [s for s in steps if "data" in s.get("step_name", "").lower()
                          or "download" in s.get("step_name", "").lower()
                          or "preprocess" in s.get("step_name", "").lower()]
            if data_steps:
                proto_snippet = json.dumps(data_steps, indent=2, ensure_ascii=False)
        if len(proto_snippet) > 4000:
            proto_snippet = proto_snippet[:4000] + "\n... (truncated)"

        # Execution plan context (expected format per dataset)
        ep_context = ""
        matched = self.execution_plan.get("matched_data", {})
        if matched:
            ep_context = json.dumps({
                ds_id: {
                    "format": info.get("format", "?"),
                    "suggested_loader": info.get("suggested_loader", "?"),
                    "preprocessing_notes": info.get("preprocessing_notes", ""),
                }
                for ds_id, info in matched.items()
            }, indent=2, ensure_ascii=False)
        if len(ep_context) > 3000:
            ep_context = ep_context[:3000] + "\n... (truncated)"

        # Dataset listings
        ds_listing = []
        for ds_id, info in datasets.items():
            ds_listing.append(f"\n### {ds_id}")
            ds_listing.append(f"  Files: {info['file_count']}, Size: {info['total_size_mb']}MB")
            ds_listing.append(f"  Extensions: {info['extensions']}")
            ds_listing.append(f"  Markers: {json.dumps(info['markers'])}")
            for sf in info.get("sample_files", [])[:10]:
                ds_listing.append(f"    {sf['name']}  ({sf['size_mb']}MB)")
        ds_text = "\n".join(ds_listing)

        # Format hints
        hints_text = "\n".join(f"  {k}: {v}" for k, v in self.FORMAT_HINTS.items())

        return f"""You are a single-cell bioinformatics data curator.

Given a paper's dataset file listings and context from the experimental
protocol, determine the correct format of each dataset and produce a
step-by-step conversion plan to standardize everything into AnnData (.h5ad).

## Execution Plan Context (expected format per dataset)
```json
{ep_context}
```

## Protocol (relevant steps only)
```json
{proto_snippet}
```

## Dataset File Listings
{ds_text}

## Known Format → Conversion Strategies
{hints_text}

## Available Downstream Tools (OmicsClaw)
- **bulk RNA-seq DE**: ``bulkrna-de`` — ``python skills/bulkrna/bulkrna-de/bulkrna_de.py --input <counts.csv> --output <dir> --control-prefix <ctrl> --treat-prefix <treat>``
- **bulk RNA-seq QC**: ``bulkrna-qc`` — ``python skills/bulkrna/bulkrna-qc/bulkrna_qc.py --input <input> --output <dir>``
- **Single-cell preprocessing**: ``sc-preprocessing`` — standard scanpy/Seurat workflow

## Task
Return a JSON object (curation_plan) with these keys:

{{
  "datasets": [
    {{
      "id": "GSE12345",
      "detected_format": "10X_mtx"|"10X_h5"|"h5ad"|"rds_seurat"|"bulk_count_txt"
                         |"bulk_count_csv"|"cel_affymetrix"|"bgx_illumina"
                         |"series_matrix"|"broadpeak"|"bigwig"|"tsv_de_results"
                         |"unknown_tar"|"unusable"|"other",
      "format_rationale": "one sentence why this format was chosen",
      "is_expression_data": true/false,
      "curation_steps": [
        {{"tool": "gunzip"|"tar"|"python"|"R"|"manual",
          "command": "exact command to run or Python/R code snippet"}}
      ],
      "output_format": "h5ad"|"tsv"|"none",
      "estimated_cells": null,
      "estimated_genes": null,
      "confidence": 0-100,
      "best_path": {{
        "type": "sc_preprocess"|"paper_own_code"|"omicsclaw_tool"|"cannot_automate",
        "tool": "analysis_main.R / bulkrna-de / ...",
        "command": "exact command to run this data through the tool",
        "why": "brief reason why this path was chosen"
      }}
    }}
  ],
  "uncuratable": [
    {{"id": "...", "reason": "..."}}
  ],
  "total_datasets": <int>,
  "expression_datasets": <int>,
  "non_expression_datasets": <int>,
  "env_requirements": {{
    "python": ["scanpy", "pandas", "numpy", "anndata"],
    "R": ["Seurat", "SeuratDisk", "oligo", "limma"],
    "system": ["gunzip", "tar"]
  }}
}}

Rules:
- For .CEL files: use oligo::read.celfiles in R, output a matrix then convert.
- For .bgx files: use limma::neqc in R (Illumina BeadChip).
- For .rds files: try pyreadr first; if it's a Seurat object, suggest SeuratDisk.
- For .broadpeak/.bw files: mark as non-expression, skip conversion.
- For .tsv/.csv without barcodes/features markers: treat as bulk count matrix.
- For .mtx WITH barcodes+features: standard 10X, scanpy.read_10x_mtx.
- If a dataset is too large (>50GB), suggest reading only the first N rows for
  format detection before full conversion.
- Return ONLY valid JSON, no markdown fences, no explanations.
"""

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You are an expert bioinformatics data curator. Your role is to inspect "
            "file listings and determine the correct format + conversion strategy for "
            "each dataset. Be precise about file formats and conversion commands. "
            "Output ONLY valid JSON, no markdown fences, no explanations."
        )

    def _curate_batched(self, datasets: Dict[str, Any], protocol: Dict[str, Any]) -> Dict[str, Any]:
        """Handle papers with many datasets by batching into groups of 8.

        Each batch gets its own LLM call, then results are merged.
        This prevents token overflow for papers like universal-hallmarks (33 datasets).
        """
        all_ids = list(datasets.keys())
        batch_size = 8
        all_ds = []
        uncuratable = []
        total_expr = 0
        total_non_expr = 0

        for i in range(0, len(all_ids), batch_size):
            batch_ids = all_ids[i:i + batch_size]
            batch_data = {k: datasets[k] for k in batch_ids}

            prompt = self._build_prompt(batch_data, protocol)
            raw = _call_llm(prompt, self._system_prompt())
            plan = _parse_llm_json(raw) if raw else None

            if plan:
                all_ds.extend(plan.get("datasets", []))
                uncuratable.extend(plan.get("uncuratable", []))
                total_expr += plan.get("expression_datasets", 0)
                total_non_expr += plan.get("non_expression_datasets", 0)
            else:
                # Fallback for this batch
                fb = self._fallback_plan(batch_data)
                all_ds.extend(fb.get("datasets", []))
                total_expr += fb.get("expression_datasets", 0)
                total_non_expr += fb.get("non_expression_datasets", 0)

        return {
            "datasets": all_ds,
            "uncuratable": uncuratable,
            "total_datasets": len(datasets),
            "expression_datasets": total_expr,
            "non_expression_datasets": total_non_expr,
            "env_requirements": {"python": ["scanpy", "pandas", "numpy", "anndata"],
                                 "R": ["Seurat", "oligo", "limma"], "system": ["gunzip", "tar"]},
        }

    def _fallback_plan(self, datasets: Dict[str, Any]) -> Dict[str, Any]:
        """Minimal fallback when LLM is unavailable."""
        ds_list = []
        expr_count = 0
        non_expr_count = 0
        for ds_id, info in datasets.items():
            m = info.get("markers", {})

            # DEFINITELY non-expression:
            if m.get("has_broadpeak"):
                non_expr_count += 1
                continue  # ChIP/CUT&Tag peaks — not expression

            # DEFINITELY expression:
            if m.get("has_mtx") or m.get("has_h5ad") or m.get("has_h5") or m.get("has_rds"):
                is_expr = True
            elif m.get("has_csv") or m.get("has_tsv"):
                is_expr = True
            # Microarray: .CEL / .bgx — expression but NOT scRNA (different processing)
            elif m.get("has_cel") or m.get("has_bgx"):
                is_expr = True
            # Text files / gzipped — assume expression (executor has fallback)
            elif m.get("has_txt") or m.get("has_gz"):
                is_expr = True
            else:
                is_expr = True  # default: try — deterministic fallback is the safety net

            expr_count += 1
            ds_list.append({
                "id": ds_id,
                "detected_format": "unknown",
                "format_rationale": "fallback — LLM unavailable",
                "is_expression_data": True,
                "curation_steps": [],
                "output_format": "h5ad",
                "confidence": 10,
            })

        return {
            "datasets": ds_list,
            "uncuratable": [],
            "total_datasets": len(datasets),
            "expression_datasets": expr_count,
            "non_expression_datasets": non_expr_count,
            "env_requirements": {"python": ["scanpy", "pandas"], "R": [], "system": []},
        }

    @staticmethod
    def _now_iso() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Benchmark normalization — standardize obs columns across papers
# ---------------------------------------------------------------------------

_BENCHMARK_OBS_RULES = {
    "integration": {
        "required": ["batch"],
        "column_hints": [
            # (column_name_pattern, llm_role_description, rename_to)
            ("batch", "batch identifier for multi-sample integration", "batch"),
            ("sample", "sample / patient / donor identifier (if only one column identifies separate samples, rename to batch)", "batch"),
            ("donor", "donor/patient identifier", "batch"),
            ("orig.ident", "Seurat original identity (often experiment/sample)", "batch"),
            ("replicate", "biological replicate identifier", "batch"),
            ("experiment", "experiment or run identifier", "batch"),
            ("group", "experimental group/condition identifier (if multiple groups, this is batch)", "batch"),
            ("condition", "experimental condition", "batch"),
            ("perturbation", "perturbation/treatment group", "batch"),
            ("genotype", "genetic background or genotype", "batch"),
            ("tissue", "tissue of origin (if multiple tissues, this is batch)", "batch"),
            ("patient", "patient identifier", "batch"),
            ("subject", "subject/patient identifier", "batch"),
        ],
    },
}


def normalize_curated_for_benchmark(
    paper_dir: Path,
    benchmark_type: str = "integration",
) -> Dict[str, Any]:
    """Post-curation step: rename obs columns to standard names for benchmark.

    Scans curated.h5ad files, uses LLM to identify batch/cell_type columns,
    and renames them. Also writes a ``_benchmark_obs_map.json`` recording
    the mapping for downstream processing.

    This runs AFTER CurationExecutor, BEFORE Stage 3 Processor.
    """
    if benchmark_type not in _BENCHMARK_OBS_RULES:
        return {"paper": paper_dir.name, "status": "no_rules", "mappings": []}

    rules = _BENCHMARK_OBS_RULES[benchmark_type]
    mappings = []

    for h5_path in sorted(paper_dir.rglob("curated.h5ad")):
        if "._" in str(h5_path) or "_tmp" in str(h5_path):
            continue
        logger.info("Benchmark-normalizing: %s", h5_path)

        try:
            import scanpy as sc
            adata = sc.read_h5ad(str(h5_path), backed="r")
            obs_cols = list(adata.obs.columns)
            del adata  # close backed file
        except Exception as exc:
            logger.warning("Cannot read %s: %s", h5_path, exc)
            continue

        # Build prompt: list obs columns, ask LLM to map
        col_list = "\n".join(f"  - {c}" for c in obs_cols)
        required = rules["required"]
        hints = rules["column_hints"]

        prompt = f"""You are standardizing single-cell metadata columns for a {benchmark_type} benchmark.

The dataset has these obs columns:
{col_list}

We need to identify columns that should be renamed to standard names for benchmark.
Required standard names: {required}

CRITICAL RULE: If the dataset has ONLY ONE column that identifies separate batches/samples/donors (e.g. 'sample', 'donor_id', 'orig.ident', etc.), that column IS the batch column. Rename it to 'batch'.

Common column patterns and their standard names:
{chr(10).join(f'  "{pattern}" → rename to "{std}" ({desc})' for pattern, desc, std in hints)}

For each required standard name, find the best matching column in the dataset.
Return a JSON object with:
{{"mapping": {{"original_name": "standard_name", ...}}, "reason": "brief explanation"}}

If no column matches a required name, return empty mapping with a reason.
Return ONLY valid JSON, no markdown fences.
"""
        raw = _call_llm(prompt, temperature=0.05)
        result = _parse_llm_json(raw) if raw else None

        if result and "mapping" in result:
            # Apply rename using h5ad backed mode + rewrite
            rename_map = result["mapping"]
            logger.info("  Renaming: %s", rename_map)
            try:
                adata = sc.read_h5ad(str(h5_path))
                for orig, std in rename_map.items():
                    if orig in adata.obs.columns and orig != std:
                        adata.obs[std] = adata.obs[orig].copy()
                        if std not in _BENCHMARK_OBS_RULES.get(benchmark_type, {}).get("required", []):
                            adata.obs.drop(columns=[orig], inplace=True)
                adata.write_h5ad(str(h5_path))
                del adata
                mappings.append({
                    "file": str(h5_path),
                    "mapping": rename_map,
                })
            except Exception as exc:
                logger.warning("  Rename failed: %s", exc)
        else:
            logger.warning("  LLM returned no valid mapping for %s", h5_path)

    result = {"paper": paper_dir.name, "status": "completed", "mappings": mappings}
    out_path = paper_dir / "_benchmark_obs_map.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result
