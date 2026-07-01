"""AgentScanner — LLM-driven paper-code-data matching engine.

Reads experimental_protocol.json, scans data/ and benchmark_code/ directory  
structures, and calls the LLM to produce an execution_plan.json for each  
FULLY_ACCEPTED paper.  

Usage (standalone)::
size_bytes
    python scanner.py --benchmark-type integration --output-dir benchmark_data/integration_e2e_test
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pure-Python helpers (no LLM needed)
# ---------------------------------------------------------------------------

_MAX_FILE_TREE_DEPTH = 5
_MAX_FILES_PER_DIR = 50
_MAX_KEY_FILE_BYTES = 4096

_SCRIPT_EXTS = {'.py', '.R', '.r', '.sh', '.bash', '.ipynb', '.rmd', '.Rmd'}
_KEY_FILENAMES = {'readme.md', 'makefile', 'dockerfile', 'environment.yml',
                  'requirements.txt', 'setup.py', 'run.sh', 'run.py',
                  'main.R', 'main.py', 'snakemake', 'nextflow.config'}


def _build_file_tree(root: Path, depth: int = 0) -> Dict[str, Any]:
    """Build a lightweight directory tree (no file contents beyond names/sizes)."""
    if depth > _MAX_FILE_TREE_DEPTH:
        return {"name": root.name, "type": "dir", "truncated": True, "children": []}
    if root.is_file():
        return {"name": root.name, "type": "file", "size_bytes": root.stat().st_size,
                "extension": root.suffix.lower()}
    children = []
    total_size = 0
    try:
        entries = sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except PermissionError:
        return {"name": root.name, "type": "dir", "error": "permission_denied", "children": []}
    for entry in entries[:_MAX_FILES_PER_DIR]:
        child = _build_file_tree(entry, depth + 1)
        children.append(child)
        total_size += child.get("size_total", child.get("size_bytes", 0))
    return {"name": root.name, "type": "dir", "children": children, "size_total": total_size,
            "file_count": sum(1 for c in children if c.get("type") == "file")}


def _find_key_files(repo_root: Path) -> List[Dict[str, Any]]:
    """Find README / Makefile / main scripts in a code repository root."""
    key_files = []
    for path in repo_root.rglob("*"):
        if not path.is_file(): continue
        name_lower = path.name.lower()
        is_key = name_lower in _KEY_FILENAMES or path.suffix.lower() in _SCRIPT_EXTS
        if not is_key: continue
        rel = str(path.relative_to(repo_root))
        preview = ""
        try:
            preview = path.read_text(encoding="utf-8", errors="replace")[:_MAX_KEY_FILE_BYTES]
        except Exception: pass
        key_files.append({"path": rel, "name": path.name, "extension": path.suffix.lower(),
                          "size_bytes": path.stat().st_size, "content_preview": preview})
    key_files.sort(key=lambda f: (0 if f["name"].lower().startswith("readme") else
                                   1 if f["extension"] in _SCRIPT_EXTS else 2, f["name"].lower()))
    return key_files[:15]


def _scan_data_dir(data_root: Path) -> Dict[str, Any]:
    """Scan the data/ directory and return a summary for LLM."""
    if not data_root.exists():
        return {"error": "data_dir_not_found"}
    datasets = {}
    for entry in sorted(data_root.iterdir()):
        if not entry.is_dir(): continue
        files_list = []
        total_size = 0
        for f in sorted(entry.rglob("*")):
            if f.is_file():
                rel = str(f.relative_to(entry)); sz = f.stat().st_size
                files_list.append({"name": rel, "size_bytes": sz, "extension": f.suffix.lower()})
                total_size += sz
        datasets[entry.name] = {"file_count": len(files_list),
                                "total_size_mb": round(total_size / (1024 * 1024), 1),
                                "files": [f["name"] for f in files_list[:30]],
                                "extensions": sorted(set(f["extension"] for f in files_list))}
    return datasets


# ---------------------------------------------------------------------------
# LLM call wrapper (shared with agent_curator)
# ---------------------------------------------------------------------------

def _ensure_api_key_loaded() -> None:
    """Load DEEPSEEK_API_KEY from Windows User env vars if not set.

    This makes LLM calls work even when running outside of
    ``_run_full_preflight.py`` or when the terminal session
    doesn't inherit the User-level environment variable.
    """
    import os
    if os.environ.get("DEEPSEEK_API_KEY", "").strip():
        return  # already set
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
_PEEK_BYTES = 2048  # max bytes to preview per file


def _peek_file(path: Path, max_bytes: int = _PEEK_BYTES) -> str:
    """Read the first ``max_bytes`` of a file as text (best-effort)."""
    try:
        # gzipped files: decompress header
        if path.suffix == ".gz":
            import gzip
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
                return f.read(max_bytes)
        # binary files: try reading as text, fallback to hex dump
        with open(path, "rb") as f:
            raw = f.read(max_bytes)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return f"<binary: {len(raw)} bytes, first 8 hex = {raw[:8].hex()}>"
    except Exception:
        return ""


def _peek_data_files(data_root: Path, max_files: int = 5) -> Dict[str, str]:
    """Read the first few lines of the largest data files in each dataset.

    Returns a dict ``{dataset_name: content_preview}`` so the LLM can see
    actual column headers, gene names, and sparse matrix dimensions.
    """
    peeks = {}
    for entry in sorted(data_root.iterdir()):
        if not entry.is_dir():
            continue
        # Find the largest (most informative) files
        text_candidates = []
        for f in sorted(entry.rglob("*")):
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            # Skip binary formats and tiny metadata files
            if ext in (".broadpeak", ".bw", ".png", ".jpg", ".pdf", ".rds"):
                continue
            if f.stat().st_size < 100:
                continue
            text_candidates.append(f)
        # Sort by size (largest first — usually the expression matrix)
        text_candidates.sort(key=lambda p: -p.stat().st_size)
        previews = []
        for f in text_candidates[:max_files]:
            content = _peek_file(f)
            if content:
                name = str(f.relative_to(entry))
                previews.append(f"--- {name} ({f.stat().st_size // 1024}KB) ---\n{content[:800]}")
        if previews:
            peeks[entry.name] = "\n\n".join(previews)
    return peeks


def _call_llm(prompt: str, system_prompt: str = "",
              temperature: float = 0.2,
              model: str = "",
              max_tokens: int = 8192) -> Optional[str]:
    """Call the OmicsClaw LLM. Returns None if unavailable.

    Parameters
    ----------
    model:
        Explicit model name override (e.g. "deepseek-v4-flash" or
        "deepseek-v4-pro"). When empty, uses ``_DEFAULT_LLM_MODEL``.
    max_tokens:
        Maximum tokens for the LLM response. Increased to 8192 for
        large prompts (e.g. papers with 30+ datasets) to prevent
        truncation-induced parse failures.
    """
    import time as _time_mod

    # Auto-load API key if not present (supports Windows User env vars)
    _ensure_api_key_loaded()

    t0 = _time_mod.time()
    # Retry once on empty result (transient LLM issues)
    for attempt in (1, 2):
        try:
            from omicsclaw.autoagent.llm_client import call_llm
            result = call_llm(
                prompt,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                llm_model=model or _DEFAULT_LLM_MODEL,
            )
            if result and str(result).strip():
                return result
            # Empty/none result — log and retry once
            elapsed = _time_mod.time() - t0
            logger.warning(
                "LLM returned empty result (attempt %d/2) after %.1fs",
                attempt, elapsed,
            )
        except Exception as exc:
            elapsed = _time_mod.time() - t0
            logger.warning(
                "LLM call failed (attempt %d/2) after %.1fs: %s",
                attempt, elapsed, exc,
            )

    # Both attempts failed
    return None


def _parse_llm_json(raw: str) -> Optional[Dict[str, Any]]:
    raw = (raw or "").strip()
    if raw.startswith("```"): raw = raw.split("\n", 1)[-1]
    if raw.endswith("```"): raw = raw.rsplit("```", 1)[0]
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1: return None
    try: return json.loads(raw[start:end + 1])
    except json.JSONDecodeError: return None


# ---------------------------------------------------------------------------
# AgentScanner
# ---------------------------------------------------------------------------

class AgentScanner:
    """Stage 0.5 agent that produces execution_plan.json for one paper."""

    def __init__(self, paper_dir: Path, code_dir: Path) -> None:
        self.paper_dir = paper_dir
        self.code_dir = code_dir
        self.slug = paper_dir.name

    def analyze(self) -> Dict[str, Any]:
        protocol = self._read_protocol(); data_s = self._scan_data(); code_s = self._scan_code()
        prompt = self._build_prompt(protocol, data_s, code_s)
        # Determine appropriate max_tokens based on prompt size
        prompt_tokens = len(prompt) // 4
        # Need at least as many output tokens as prompt tokens for complex responses
        max_tokens = max(8192, prompt_tokens)  # at least 8K, scale with prompt
        max_tokens = min(max_tokens, 32000)    # cap at 32K
        raw = _call_llm(prompt, self._system_prompt(), max_tokens=max_tokens)
        plan = _parse_llm_json(raw) if raw else None
        if not plan:
            plan = self._fallback_plan(protocol, data_s, code_s)
        plan.setdefault("paper", self.slug); plan.setdefault("scanned_at", self._now_iso())
        out = self.paper_dir / "execution_plan.json"
        out.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
        return plan

    def _read_protocol(self) -> Dict[str, Any]:
        p = self.paper_dir / "experimental_protocol.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def _scan_data(self) -> Dict[str, Any]:
        # Priority: unpacked_data/ > data/
        dd = self.paper_dir / "unpacked_data"
        if not dd.exists():
            dd = self.paper_dir / "data"
        pm = {}
        mp = self.paper_dir / "paper_metadata.json"
        if mp.exists(): pm = json.loads(mp.read_text(encoding="utf-8"))
        s = _scan_data_dir(dd)
        s["_paper_meta"] = {"gse_ids": pm.get("gse_ids", []), "sra_ids": pm.get("sra_ids", []),
                            "zenodo_data": pm.get("zenodo_data", []),
                            "downloaded_data": pm.get("downloaded_data", [])}
        s["_scan_source"] = str(dd)
        return s

    def _scan_code(self) -> Dict[str, Any]:
        if not self.code_dir.exists(): return {"error": "code_dir_not_found"}
        repos = []
        for entry in sorted(self.code_dir.iterdir()):
            if not entry.is_dir(): continue
            repos.append({"repo_name": entry.name, "has_git": (entry / ".git").exists(),
                          "file_tree": _build_file_tree(entry),
                          "key_files": _find_key_files(entry),
                          # Include a flat summary of all .Rmd/.py/.R files with their paths
                          # so LLM can see the full analysis pipeline structure
                          "script_list": [str(f.relative_to(entry)) for f in sorted(entry.rglob("*"))
                                         if f.is_file() and f.suffix.lower() in ('.r', '.py', '.rmd', '.ipynb')
                                         and '.git' not in str(f.relative_to(entry))][:30]})
        return {"repos": repos, "repo_count": len(repos)}

    def _build_prompt(self, protocol, data_summary, code_summary) -> str:
        pj = json.dumps(protocol, indent=2, ensure_ascii=False)
        if len(pj) > 6000: pj = pj[:6000] + "\n... (truncated)"

        # ── Data summary (file names + sizes) ──
        dids = [d for d in data_summary if not d.startswith("_")][:20]
        dl = []
        for did in dids:
            ds = data_summary[did]; ex = ds.get("extensions", []); fc = ds.get("file_count", 0)
            mb = ds.get("total_size_mb", 0)
            dl.append(f"- {did}: {fc} files, {mb}MB, extensions={ex}")
            for fn in ds.get("files", [])[:5]: dl.append(f"    {fn}")
        data_str = "\n".join(dl) or "(no data found)"

        # ── Data content peeks (first lines of key files) ──
        # For papers with many datasets, limit peeks to avoid token overflow
        dd = data_summary.get("_scan_source", "")
        peeks = {}
        if dd:
            peeks = _peek_data_files(Path(dd))
        n_datasets = len([k for k in data_summary if not k.startswith("_")])
        max_peek_datasets = 5 if n_datasets > 15 else 10  # big paper → fewer peeks
        max_peek_chars_per = 800 if n_datasets > 15 else 1500  # big paper → shorter peeks

        peek_str = ""
        for ds_id, content in list(peeks.items())[:max_peek_datasets]:
            peek_str += f"\n### {ds_id}\n{content[:max_peek_chars_per]}\n"
        if not peek_str:
            peek_str = "(no file contents to preview)"
        if n_datasets > max_peek_datasets:
            peek_str += (
                f"\n(Note: {n_datasets - max_peek_datasets} more datasets not previewed"
                f" — their file listings are in the Data section above.)"
            )
        cr = code_summary.get("repos", [])
        cb = []
        for repo in cr[:3]:
            kn = [kf["name"] for kf in repo.get("key_files", [])[:8]]
            cb.append(f"- {repo['repo_name']}: {kn}")
            # Include content preview for key scripts so LLM can understand them
            for kf in repo.get("key_files", [])[:5]:
                preview = kf.get("content_preview", "")
                if len(preview) > 50:
                    cb.append(f"    [{kf['name']}] preview:")
                    for line in preview.split("\n")[:40]:
                        stripped = line.strip()
                        if stripped:
                            cb.append(f"      {stripped[:120]}")
            # Include the full script list (paths only) so LLM sees the pipeline structure
            sl = repo.get("script_list", [])
            if sl:
                cb.append(f"    All scripts ({len(sl)} files):")
                for s in sl[:20]:
                    cb.append(f"      {s}")
        code_str = "\n".join(cb) or "(no code repos found)"
        return f"""You are an expert single-cell bioinformatics pipeline planner.

Given a paper's experimental protocol, the downloaded data files (with
the actual file contents previewed below), and the code repository
structure, produce an execution_plan.json that matches each dataset to
the correct analysis script.

## Experimental Protocol
```json
{pj}
```

## Downloaded Data (file names only, not contents)
{data_str}

## Data File Contents (first 2KB of key files — use this to determine exact format)
{peek_str}

## Code Repository Structure
{code_str}

## Task
Return a JSON object (execution_plan) with these keys:

{{
  "status": "ready"|"needs_conversion"|"partial_data"|"code_only",
  "feasible_analysis": "one sentence describing what analysis we CAN do with current data+code",
  "matched_data": {{
    "<dataset_id>": {{
      "format": "10X mtx"|"h5ad"|"rds"|"csv"|"txt.gz"|"tar"|"unknown",
      "suggested_loader": "scanpy.read_10x_mtx"|"scanpy.read_h5ad"|"pandas.read_csv"|"unknown",
      "preprocessing_needed": true/false,
      "preprocessing_notes": "e.g. need to gunzip + transpose + add gene symbols",
      "matches_protocol": true/false,
      "can_use_for": "what kind of analysis this data supports",
      "n_rows": <int or null>,
      "n_cols": <int or null>,
      "has_gene_names": true/false,
      "has_cell_barcodes": true/false,
      "data_type": "scRNA"|"bulk_RNA"|"spatial"|"CUTandTag"|"chip_seq"|"array"|"other"
    }}
  }},
  "matched_scripts": {{
    "<script_name>": {{
      "purpose": "...",
      "can_use_with_data": true/false,
      "why_not": "if false, explain briefly",
      "language": "python"|"R"|"bash",
      "is_entry_point": true/false,
      "input_format": "what data format this script expects",
      "output_format": "what this script produces"
    }}
  }},
  "entry_point": "the main command to run",
  "env_guess": {{ "python": "...", "R": "...", "key_packages": [...] }},
  "missing": ["things we genuinely don't have"],
  "warnings": ["things to watch out for"],
  "confidence": 0-100
}}

Rules:
- The ONLY truly blocked case is when there is NO data AND NO code.
- Everything else gets a practical path — data needs conversion = suggest how,
  code is incomplete = specify what's missing and what CAN still be done.
- "status":"ready" = everything looks usable as-is with minimal work.
- "status":"needs_conversion" = data format needs work but is possible.
- "status":"partial_data" = some datasets missing but we can still do something.
- "status":"code_only" = only code exists, no data — document what the code does.
- Return ONLY valid JSON, no markdown fences, no explanations.
"""

    @staticmethod
    def _system_prompt() -> str:
        return ("You are a pragmatic single-cell bioinformatics workflow planner. "
                "Your goal is to find a feasible path to reproduce part of the paper's "
                "results from available data+code. Be constructive, not conservative. "
                "Output ONLY valid JSON, no markdown fences, no explanations.")

    def _fallback_plan(self, protocol, data_summary, code_summary) -> Dict[str, Any]:
        dids = [d for d in data_summary if not d.startswith("_")]
        has_data = len(dids) > 0
        has_code = code_summary.get("repo_count", 0) > 0
        if has_data and has_code:
            status = "ready"
        elif has_data:
            status = "partial_data"
        elif has_code:
            status = "code_only"
        else:
            status = "blocked"
        return {"paper": self.slug, "status": status,
                "feasible_analysis": "fallback: LLM unavailable, manual review needed",
                "matched_data": {did: {"format": "unknown"} for did in dids},
                "matched_scripts": {}, "entry_point": "",
                "env_guess": protocol.get("software_versions", {}),
                "missing": [], "warnings": ["LLM unavailable — fallback plan generated"],
                "confidence": 0, "scanned_at": self._now_iso()}

    @staticmethod
    def _now_iso() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()
