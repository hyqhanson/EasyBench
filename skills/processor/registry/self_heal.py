"""Self-Healing Agent for Stage 3 method execution.

When a method fails, this agent:
1. Captures the traceback and method context
2. Asks LLM to diagnose the issue and propose a fix
3. Applies the fix (monkey-patch) and retries
4. Records the failure for future runs

Usage:
    healer = SelfHealAgent()
    healer.register_method("scanorama", skills.processor.integration.methods)
    result = healer.safe_run("scanorama", adata, batch_key="batch")
"""

from __future__ import annotations

import importlib
import json
import logging
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── LLM client path setup ──────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    from omicsclaw.autoagent.llm_client import call_llm
except ImportError:
    call_llm = None


# ── Agent ───────────────────────────────────────────────────────────────

class SelfHealAgent:
    """LLM-powered self-healing for failed integration methods."""

    MAX_RETRIES = 2
    LLM_MODEL = "deepseek-v4-flash"

    def __init__(self):
        self._failure_log: List[Dict[str, Any]] = []
        self._patches: Dict[str, str] = {}  # method_name -> patched source

    # ── public ──────────────────────────────────────────────────────

    def safe_run(
        self,
        method_name: str,
        module: Any,
        adata,
        batch_key: str = "batch",
    ) -> Optional[Dict[str, Any]]:
        """Run a method with automatic retry + LLM self-healing."""
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                # Apply any cached patches first
                if method_name in self._patches:
                    self._apply_patch(module, method_name)

                fn = getattr(module, f"run_{method_name}", None)
                if fn is None:
                    logger.error("Method function run_%s not found in module", method_name)
                    return None

                return fn(adata.copy(), batch_key=batch_key)

            except Exception as exc:
                tb = traceback.format_exc()
                logger.warning("Method %s failed (attempt %d/%d): %s",
                               method_name, attempt, self.MAX_RETRIES, exc)

                if attempt >= self.MAX_RETRIES:
                    self._log_failure(method_name, tb)
                    return None

                # Try LLM-driven self-heal
                fix = self._ask_llm_to_fix(method_name, module, tb)
                if fix:
                    self._patches[method_name] = fix
                    self._apply_patch(module, method_name)
                    logger.info("Applied LLM patch for %s, retrying...", method_name)
                else:
                    logger.warning("No LLM fix available for %s, giving up", method_name)
                    self._log_failure(method_name, tb)
                    return None

        return None

    # ── internal ────────────────────────────────────────────────────

    def _ask_llm_to_fix(self, method_name: str, module: Any, tb: str) -> Optional[str]:
        """Ask LLM to generate a fix for the failed method."""
        if call_llm is None:
            logger.debug("LLM client not available, skipping self-heal")
            return None

        fn = getattr(module, f"run_{method_name}", None)
        source = self._get_function_source(fn) if fn else "(source not found)"

        prompt = f"""You are debugging a Python integration method for single-cell data.

Method name: run_{method_name}
Current source code:
```python
{source}
```

The method failed with this traceback:
```
{tb[:3000]}
```

Analyze the error and propose a fix. Return ONLY the corrected Python function (full run_{method_name} function), nothing else.
The fix should handle edge cases (shape mismatches, missing keys, NaN values, etc.).
"""
        try:
            response = call_llm(
                prompt,
                model=self.LLM_MODEL,
                temperature=0.1,
                max_tokens=2000,
            )
            # Extract just the function
            if "def run_" in response:
                return response
            return None
        except Exception as exc:
            logger.warning("LLM self-heal call failed: %s", exc)
            return None

    @staticmethod
    def _get_function_source(fn: Callable) -> str:
        """Get source code of a function (best effort)."""
        try:
            import inspect
            return inspect.getsource(fn)
        except Exception:
            return str(fn)

    def _apply_patch(self, module: Any, method_name: str) -> bool:
        """Monkey-patch the function in the module."""
        patch = self._patches.get(method_name)
        if not patch:
            return False
        try:
            local_ns: Dict[str, Any] = {}
            exec(patch, module.__dict__, local_ns)
            fn_name = f"run_{method_name}"
            if fn_name in local_ns:
                setattr(module, fn_name, local_ns[fn_name])
                return True
        except Exception as exc:
            logger.error("Failed to apply patch for %s: %s", method_name, exc)
        return False

    def _log_failure(self, method_name: str, tb: str) -> None:
        self._failure_log.append({
            "method": method_name,
            "traceback": tb[:5000],
            "patched": method_name in self._patches,
        })

    # ── persistence ─────────────────────────────────────────────────

    def save_failure_log(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._failure_log, indent=2, ensure_ascii=False),
                        encoding="utf-8")

    def load_failure_log(self, path: Path) -> None:
        if path.exists():
            self._failure_log = json.loads(path.read_text(encoding="utf-8"))
