"""Thin adapter layer wrapping mature single-cell tools (scIB, sklearn, scanpy)."""
from skills.processor.tools.metrics import (
    METRIC_REGISTRY,
    compute_standard_metrics,
    compute_standard_annotation_metrics,
    ilisi,
    clisi,
    batch_asw,
    celltype_asw,
    silhouette,
    cluster_ari,
    cluster_nmi,
    annotation_f1,
    annotation_accuracy,
)
from skills.processor.tools import annotation  # noqa: F401  (annotation methods)
