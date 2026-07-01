"""AgentMonitor — Stage 2 process supervisor.

Observes the reproduce process in real-time, detecting deadlocks,
timeouts, infinite loops, and common error patterns.

Monitor actions:
  - CONTINUE  → everything OK
  - WARN      → non-fatal warning
  - RETRY     → transient error, retry
  - FALLBACK  → current path failed, try alternative
  - ABORT     → unrecoverable, mark paper as blocked
"""

from __future__ import annotations

import re as _re
from typing import Any, Dict, List, Optional, Tuple

# ── Known error signatures (hardcoded patterns) ──

_ERROR_SIGNATURES: List[Tuple[str, str, str]] = [
    # (pattern, action, message)
    # Package installation issues
    (r"pip.*Read timed out",                  "RETRY",  "pip network timeout"),
    (r"CondaHTTPError",                        "RETRY",  "conda HTTP error"),
    (r"UnsatisfiableError",                    "FALLBACK","conda dependency conflict"),
    (r"Could not satisfy constraints",         "FALLBACK","conda constraint conflict"),
    (r"No module named ['\"]?(\w+)['\"]?",     "FALLBACK","missing Python module"),

    # R package issues
    (r"there is no package called ['\"]?(\w+)['\"]?", "FALLBACK", "missing R package"),
    (r"installation of package.*had non-zero exit status", "FALLBACK", "R package install failed"),

    # GPU / CUDA
    (r"CUDA (not available|out of memory)",    "WARN",   "CUDA unavailable/OOM"),
    (r"out of memory",                         "FALLBACK","OOM — reduce batch size"),
    (r"Killed",                                "ABORT",  "process killed (OOM?)"),

    # IO / Filesystem
    (r"(No such file|FileNotFound|cannot find)", "ABORT", "missing file"),
    (r"Permission denied",                     "ABORT",  "permission error"),

    # Timeout
    (r"timed? ?out|timeout",                   "RETRY",  "operation timeout"),

    # Generic / unclassified
    (r"Segmentation fault",                    "ABORT",  "segfault"),
    (r"NullPointer|IndexError|KeyError",       "ABORT",  "code bug"),
    (r"SyntaxError",                           "ABORT",  "code syntax error"),

    # Reproduce-specific
    (r"Error: git clone failed",               "RETRY",  "git clone failed"),
    (r"Error: install failed",                 "FALLBACK","env install failed"),
    (r"Error: run script failed",              "FALLBACK","run script failed"),
    (r"Error: verify output.*missing",         "FALLBACK","expected output missing"),
]


def analyze_log(log_text: str, consecutive_errors: int = 0) -> Dict[str, Any]:
    """Analyze a chunk of reproduce output and return a monitor action.

    Args:
        log_text: Recent reproduce output text.
        consecutive_errors: Number of consecutive errors seen so far.

    Returns:
        {"action": str, "message": str, "details": dict}
    """
    if not log_text or not log_text.strip():
        # No output → might be stuck
        return {"action": "CONTINUE", "message": "no output yet", "details": {}}

    for pattern, action, msg in _ERROR_SIGNATURES:
        match = _re.search(pattern, log_text, _re.IGNORECASE | _re.MULTILINE)
        if match:
            details = {"pattern": pattern, "match": match.group(0)[:100]}
            # Escalate if same error keeps happening
            if action == "RETRY" and consecutive_errors >= 3:
                action = "FALLBACK"
            return {"action": action, "message": msg, "details": details}

    # Check for consecutive error escalation
    if consecutive_errors >= 5:
        return {"action": "FALLBACK", "message": "too many consecutive errors",
                "details": {"consecutive_errors": consecutive_errors}}

    return {"action": "CONTINUE", "message": "normal progress", "details": {}}


def classify_for_llm(log_text: str) -> Dict[str, Any]:
    """Return a compact error summary for LLM-based diagnosis."""
    errors = []
    for pattern, action, msg in _ERROR_SIGNATURES:
        matches = _re.findall(pattern, log_text, _re.IGNORECASE | _re.MULTILINE)
        for m in matches:
            errors.append({"type": msg, "action": action, "detail": str(m)[:80]})
    return {"total_errors": len(errors), "errors": errors[:10]}
