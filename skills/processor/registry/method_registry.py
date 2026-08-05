"""Method Registry — loads YAML configs and dispatches methods per benchmark type.

Usage:
    reg = MethodRegistry()
    methods = reg.get_methods("integration")  # returns list of MethodSpec
    reg.dispatch(adata, methods, batch_key)
"""

from __future__ import annotations

import importlib
import logging
import sys
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

class MethodSpec:
    """A single method specification loaded from YAML."""

    __slots__ = ("name", "module", "import_test", "install", "validator",
                 "validator_code", "description", "known_failures",
                 "requires_label")

    def __init__(self, raw: dict):
        self.name = raw["name"]
        self.module = raw.get("module", "")
        self.import_test = raw.get("import_test", "true")
        self.install = raw.get("install", "")
        self.validator = raw.get("validator", "True")
        self.validator_code = raw.get("validator_code", "")
        self.description = raw.get("description", "")
        self.known_failures = raw.get("known_failures", [])
        self.requires_label = bool(raw.get("requires_label", False))

    @property
    def is_available(self) -> bool:
        """Check import_test — can be 'true' or a module name like 'harmonypy'."""
        test = self.import_test.strip()
        if test.lower() == "true":
            return True
        try:
            importlib.import_module(test)
            return True
        except ImportError:
            return False

    def ensure_deps(self) -> bool:
        """Install dependencies if missing. Returns True if now available."""
        if self.is_available:
            return True
        if not self.install:
            return False
        logger.info("Installing %s: %s", self.name, self.install)
        try:
            subprocess.check_call(
                self.install.split(),
                stdout=sys.stderr,
                stderr=sys.stderr,
            )
            return self.is_available
        except Exception:
            return False

    def validate(self, adata, batch_key: str = "batch") -> bool:
        """Run validator code (can be simple expression or code block)."""
        if self.validator_code:
            try:
                local_ns: Dict[str, Any] = {}
                exec(self.validator_code, {"adata": adata, "batch_key": batch_key}, local_ns)
                validate_fn = local_ns.get("validate")
                if validate_fn:
                    return bool(validate_fn(adata, batch_key))
            except Exception:
                return False
        # Simple expression eval
        try:
            return bool(eval(self.validator, {"adata": adata, "batch_key": batch_key}))
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class MethodRegistry:
    """Loads all benchmark-type YAML configs and dispatches methods."""

    def __init__(self, config_root: Optional[Path] = None):
        if config_root is None:
            config_root = Path(__file__).resolve().parent / "configs"
        self.config_root = Path(config_root)
        self._cache: Dict[str, List[MethodSpec]] = {}

    def get_methods(self, benchmark_type: str) -> List[MethodSpec]:
        """Get all configured methods for a benchmark type."""
        if benchmark_type in self._cache:
            return self._cache[benchmark_type]

        config_path = self.config_root / f"{benchmark_type}.yaml"
        if not config_path.exists():
            logger.warning("No config for benchmark_type=%s, using defaults", benchmark_type)
            return self._load_defaults()

        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        methods = [MethodSpec(raw) for raw in data.get("methods", [])]
        self._cache[benchmark_type] = methods
        return methods

    def _load_defaults(self) -> List[MethodSpec]:
        """Fallback when no YAML config exists."""
        return [MethodSpec({"name": "none", "import_test": "true", "description": "Baseline (no integration)"})]

    def get_available_methods(self, benchmark_type: str) -> List[MethodSpec]:
        """Return only methods whose dependencies are satisfied."""
        return [m for m in self.get_methods(benchmark_type) if m.is_available]

    def available_method_names(self, benchmark_type: str) -> List[str]:
        """Return list of method names that are currently importable."""
        return [m.name for m in self.get_methods(benchmark_type) if m.is_available]

    def dispatch(
        self,
        adata,
        benchmark_type: str,
        *,
        batch_key: str = "batch",
        methods: Optional[List[str]] = None,
        **method_kwargs,
    ) -> Dict[str, Any]:
        """Run all available methods (or a subset) and return {method_name: result}.

        The actual method functions live in their modules (e.g. skills.processor.integration.methods).
        This dispatcher imports each module and calls run_all_methods().
        Extra kwargs (e.g. label_key) are forwarded to run_all_methods.
        """
        specs = self.get_methods(benchmark_type)
        if methods:
            specs = [s for s in specs if s.name in methods]

        # Label-based filtering: skip methods that REQUIRE a ground-truth
        # label column when none is provided (annotation benchmark pattern,
        # mirroring how integration skips methods without a batch column).
        label_key = method_kwargs.get("label_key")
        if label_key is None:
            specs = [s for s in specs if not s.requires_label]

        # Group by module
        by_module: Dict[str, List[MethodSpec]] = {}
        for spec in specs:
            if not spec.is_available:
                continue
            by_module.setdefault(spec.module, []).append(spec)

        all_results: Dict[str, Any] = {}
        for module_name, module_specs in by_module.items():
            try:
                mod = importlib.import_module(module_name)
                names = [s.name for s in module_specs]
                results = mod.run_all_methods(
                    adata.copy(), batch_key=batch_key, methods=names, **method_kwargs
                )
                all_results.update(results)
            except Exception as exc:
                logger.error("Module %s failed: %s", module_name, exc)

        return all_results
