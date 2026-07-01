"""AgentFix — diagnostic & repair engine for reproduce failures.

Two-layer strategy:
  1. Look up known error signatures in fix_skill_library
  2. If no match, invoke LLM to diagnose and propose a fix
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Fix skill library (hardcoded known fixes) ──

FIX_SKILL_LIBRARY: Dict[str, str] = {
    "pip_timeout": (
        "pip install --default-timeout=120 --index-url "
        "https://mirrors.aliyun.com/pypi/simple/ {pkg}"
    ),
    "conda_solve": (
        "conda install -c conda-forge --override-channels {pkg}"
    ),
    "r_package_missing": (
        "Rscript -e "
        "'if (!requireNamespace(\"BiocManager\", quietly=TRUE)) "
        "install.packages(\"BiocManager\")); "
        "BiocManager::install(\"{pkg}\")'"
    ),
    "disk_full": "[HUMAN_INTERVENTION] 磁盘不足，请清理空间",
    "permission": "chmod -R +x {path}",
}


class AgentFix:
    """Diagnose and fix reproduce failures."""

    def __init__(self, paper_slug: str, work_dir: Path) -> None:
        self.slug = paper_slug
        self.work_dir = work_dir
        self.fix_log: List[Dict[str, Any]] = []

    def diagnose_and_fix(
        self,
        error_text: str,
        reproduce_dir: Path,
        max_attempts: int = 3,
    ) -> Dict[str, Any]:
        """Main entry: try to diagnose and fix an error.

        Args:
            error_text: The error output from reproduce.
            reproduce_dir: Working directory for the reproduce.
            max_attempts: Max fix attempts before abort.

        Returns:
            {"fixed": bool, "attempts": int, "commands_run": [...], "llm_diagnosis": ...}
        """
        self.fix_log.append({"error": error_text[:500], "attempt": len(self.fix_log) + 1})

        # ── Step 1: Try fix_skill_library ──
        for skill_key, fix_cmd in FIX_SKILL_LIBRARY.items():
            if skill_key in error_text.lower():
                logger.info("[%s] Matched fix_skill: %s", self.slug[:30], skill_key)
                return {
                    "fixed": True,
                    "attempts": 1,
                    "commands_run": [fix_cmd],
                    "method": "fix_skill_library",
                }

        # ── Step 2: Try LLM diagnosis ──
        if len(self.fix_log) <= max_attempts:
            llm_result = self._llm_diagnose(error_text)
            self.fix_log[-1]["llm_result"] = llm_result
            if llm_result.get("suggested_fix"):
                return {
                    "fixed": True,
                    "attempts": len(self.fix_log),
                    "commands_run": [llm_result["suggested_fix"]],
                    "method": "llm",
                    "llm_diagnosis": llm_result,
                }

        return {
            "fixed": False,
            "attempts": len(self.fix_log),
            "commands_run": [],
            "method": "failed",
        }

    def save_fix_log(self, fix_log_path: Path) -> None:
        """Write fix_attempts.json."""
        fix_log_path.write_text(
            json.dumps({"paper": self.slug, "attempts": self.fix_log},
                       indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _llm_diagnose(self, error_text: str) -> Dict[str, Any]:
        """Call LLM to diagnose error and suggest fix."""
        try:
            prompt = (
                f"You are a bioinformatics DevOps engineer. "
                f"Analyze this error from reproducing paper '{self.slug}' "
                f"and suggest a concrete fix command (bash/Python/R):\n\n"
                f"Error:\n{error_text[:2000]}\n\n"
                f"Return a JSON object with keys:\n"
                f'  "diagnosis": "what went wrong (one sentence)",\n'
                f'  "suggested_fix": "exact command to fix it",\n'
                f'  "confidence": 0-100\n'
                f"Return ONLY valid JSON, no explanation."
            )
            from skills.agents.agent_preflight.scanner import _call_llm
            raw = _call_llm(prompt, system_prompt="You fix bioinformatics code.", model="")
            if raw:
                from skills.agents.agent_preflight.scanner import _parse_llm_json
                return _parse_llm_json(raw) or {"diagnosis": "parse_failed", "suggested_fix": ""}
        except Exception as exc:
            logger.warning("LLM diagnose failed: %s", exc)
        return {"diagnosis": "llm_unavailable", "suggested_fix": ""}
