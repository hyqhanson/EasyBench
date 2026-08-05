"""Thin adapters for cell-type annotation methods.

Wraps mature, community-maintained annotation tools so that EasyBench can
run ``--benchmark-type annotation`` through the same MethodRegistry dispatch
as integration. Each function takes an AnnData and returns an AnnData with
predicted labels added to ``adata.obs``.

Methods:
  - celltypist   (reference-based, immune models)
  - logreg       (logistic regression on labeled cells)
  - randomforest (random forest on labeled cells)
  - majority     (majority-class baseline)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Label column discovery ─────────────────────────────────────────────

def _find_label_col(adata) -> Optional[str]:
    """Find the ground-truth label column in adata.obs."""
    for candidate in ("cell_type", "celltype", "cell_ontology_class",
                      "annotation", "label", "labels", "cell_identity", "cluster"):
        if candidate in adata.obs.columns and adata.obs[candidate].nunique() >= 2:
            return candidate
    return None


# ── Gene symbol normalization ──────────────────────────────────────────

def _looks_like_ensembl(adata, sample: int = 50) -> bool:
    """Heuristic: do var_names look like Ensembl IDs (e.g. ENSG..., ENSMUSG...)?"""
    import re
    names = list(adata.var_names[:sample])
    if not names:
        return False
    pat = re.compile(r"^(ENSG|ENSMUSG|ENSRNOG|ENSDARG|ENSCAFG|ENSBTAG|ENSGG|ENSSSCG)\d+")
    hits = sum(1 for n in names if pat.match(str(n)))
    return hits >= max(3, len(names) // 2)


def _convert_ensembl_to_symbol(adata, species: str = "auto") -> bool:
    """Convert Ensembl var_names to gene symbols in place (best-effort).

    Returns True if a conversion was applied. Uses mygene (online) and
    falls back to a local alias map if mygene is unavailable.
    """
    try:
        import mygene
    except ImportError:
        logger.warning("mygene not installed — cannot convert Ensembl IDs to symbols")
        return False

    names = list(adata.var_names)
    if not names:
        return False

    if species == "auto":
        # Guess from first ID prefix
        first = str(names[0])
        species = "mouse" if first.startswith("ENSMUSG") else \
                  "human" if first.startswith("ENSG") else None

    scopes = "ensembl.gene"
    try:
        mg = mygene.MyGeneInfo()
        res = mg.querymany(names, scopes=scopes, fields="symbol",
                           species=species, verbose=False)
    except Exception as exc:
        logger.warning("mygene query failed: %s", exc)
        return False

    mapping = {}
    for item in res:
        q = item.get("query")
        sym = item.get("symbol")
        if q and sym:
            mapping[q] = sym

    if not mapping:
        logger.warning("No gene symbol mapping found")
        return False

    new_names = [mapping.get(n, n) for n in names]
    adata.var_names = new_names
    adata.var_names_make_unique()
    logger.info("Converted %d/%d Ensembl IDs → gene symbols",
                sum(1 for a, b in zip(names, new_names) if a != b), len(names))
    return True


def _ensure_gene_symbols(adata) -> None:
    """Convert Ensembl var_names to symbols so reference tools can match.

    Priority:
      1. Use an existing symbol column (gene_symbol / gene_short_names /
         gene_name / symbol) if present — avoids an online mygene round-trip.
      2. Else, if var_names look like Ensembl IDs, convert via mygene.
    """
    import numpy as np

    # ── Case 1: a symbol column already exists ──
    for col in ("gene_symbol", "gene_short_names", "gene_name", "symbol", "features"):
        if col in adata.var.columns:
            syms = adata.var[col].astype(str).to_numpy()
            non_empty = np.array([s not in ("", "nan", "None") for s in syms])
            if non_empty.sum() >= max(10, adata.n_vars // 2):
                # Keep existing symbols for mapped genes; fall back to var_names
                new_names = np.where(non_empty, syms, np.asarray(adata.var_names, dtype=str))
                # Skip if conversion would not actually change anything meaningful
                changed = int((np.asarray(adata.var_names, dtype=str) != new_names).sum())
                if changed > 0:
                    adata.var_names = new_names.tolist()
                    adata.var_names_make_unique()
                    logger.info("Used existing '%s' column → gene symbols (%d changed)",
                                col, changed)
                return

    # ── Case 2: convert via mygene ──
    if _looks_like_ensembl(adata):
        _convert_ensembl_to_symbol(adata)


# ── CellTypist ─────────────────────────────────────────────────────────

# Species keyword hints → model name substrings (ranked by preference).
_SPECIES_MODEL_HINTS = {
    "mouse": ["Developing_Mouse_Brain", "Mouse_Whole_Brain", "Adult_Mouse_Gut",
              "Mouse_Dentate_Gyrus", "Mouse_Dendritic_Subtypes", "Mouse_Isocortex_Hippocampus",
              "Mouse_Postnatal_DentateGyrus", "Developing_Mouse_Hippocampus",
              "Healthy_Mouse_Liver", "Adult_Mouse_OlfactoryBulb"],
    "human": ["Developing_Human_Brain", "Adult_Human_MTG", "Developing_Human_Hippocampus",
              "Adult_Human_PrefrontalCortex", "Adult_Human_PancreaticIslet",
              "Adult_Human_Skin", "Adult_Human_Vascular", "Healthy_Adult_Heart",
              "Healthy_Human_Liver", "Human_Lung_Atlas", "Fetal_Human_Retina",
              "Human_Endometrium_Atlas", "Human_Colorectal_Cancer", "Cells_Human_Tonsil",
              "Cells_Adult_Breast", "Cells_Intestinal_Tract", "Cells_Lung_Airway"],
    "immune": ["Immune_All_High", "Immune_All_Low", "Healthy_COVID19_PBMC",
               "Adult_COVID19_PBMC", "COVID19_Immune_Landscape", "PaediatricAdult_COVID19_PBMC",
               "COVID19_HumanChallenge_Blood", "Autopsy_COVID19_Lung", "Lethal_COVID19_Lung",
               "Thymus_Allograft_PBMC", "Human_IPF_Lung", "Human_PF_Lung", "Nuclei_Lung_Airway"],
    "pan_fetal": ["Pan_Fetal_Human", "Developing_Human_Organs", "Developing_Human_Thymus",
                  "Developing_Human_Gonads", "Fetal_Human_AdrenalGlands", "Fetal_Human_Pancreas",
                  "Fetal_Human_Pituitary", "Fetal_Human_Skin", "Human_Embryonic_YolkSac",
                  "Human_Developmental_Retina", "Human_Placenta_Decidua", "Nuclei_Human_InnerEar"],
    "general": ["Immune_All_High"],
}


def _detect_species(adata) -> str:
    """Detect species from var_names (Ensembl prefixes) or fall back to symbols."""
    import re
    names = list(adata.var_names[:200])
    mouse = sum(1 for n in names if re.match(r"^ENSMUSG\d+", str(n)))
    human = sum(1 for n in names if re.match(r"^ENSG\d+", str(n)))
    rat = sum(1 for n in names if re.match(r"^ENSRNOG\d+", str(n)))
    # Mouse symbols are TitleCase (e.g. Pld5); human symbols are UPPERCASE (e.g. TSPAN6)
    title_case = sum(1 for n in names if re.match(r"^[A-Z][a-z]+\d*$", str(n)))
    upper = sum(1 for n in names if re.match(r"^[A-Z][A-Z0-9]+$", str(n)))
    if mouse and mouse >= max(3, human):
        return "mouse"
    if human and human > mouse:
        return "human"
    if rat:
        return "rat"
    if title_case > upper and title_case >= 10:
        return "mouse"
    return "human"


def _best_celltypist_model(adata, species: str) -> str:
    """Pick the celltypist model with the most gene overlap with adata.

    Uses species-ranked candidate lists, then scores by number of shared
    genes with the model's ``features``. Falls back to Immune_All_High.
    """
    import os
    from celltypist import models

    try:
        models.download_models(force_update=False)
    except Exception:
        pass

    m_dir = os.path.expanduser("~/.celltypist/data/models")
    if not os.path.isdir(m_dir):
        return "Immune_All_High"

    gene_set = set(adata.var_names)
    # Load candidate lists in preference order
    hints = _SPECIES_MODEL_HINTS.get(species, []) + _SPECIES_MODEL_HINTS["general"]
    best_name, best_overlap = None, -1
    for name in hints:
        pkl = os.path.join(m_dir, f"{name}.pkl")
        if not os.path.exists(pkl):
            continue
        try:
            m = models.Model.load(model=pkl)
            feats = set(m.features)
        except Exception:
            continue
        overlap = len(gene_set & feats)
        if overlap > best_overlap:
            best_overlap, best_name = overlap, name
        if best_overlap >= 2000:  # good enough
            break
    if best_name is None or best_overlap < 50:
        # No species model has meaningful overlap → fall back to the immune
        # atlas (broad coverage) or a generic default; also log a hint.
        logger.warning("  [celltypist] best model '%s' only overlaps %d genes — "
                       "falling back to Immune_All_High", best_name, best_overlap)
        return "Immune_All_High"
    logger.info("  [celltypist] auto-selected model=%s (species=%s, %d genes overlap)",
                best_name, species, best_overlap)
    return best_name


def _run_celltypist(adata, label_key: Optional[str] = None, **kwargs) -> Dict[str, Any]:
    """CellTypist reference-based annotation.

    Auto-selects a reference model based on detected species + gene overlap
    (unless a model is explicitly supplied via ``model=`` kwarg). Adds
    ``celltypist_labels`` and ``celltypist_scores`` to adata.obs.
    """
    import celltypist
    from celltypist import models

    # Convert Ensembl IDs → symbols BEFORE selecting the model, so the
    # gene-overlap scoring sees symbols (the models store symbols).
    _ensure_gene_symbols(adata)

    model_name = kwargs.pop("model", None)
    if model_name is None:
        species = _detect_species(adata)
        model_name = _best_celltypist_model(adata, species)
    n_jobs = kwargs.pop("n_jobs", -1)

    logger.info("CellTypist annotation with model=%s (%d cells)",
                model_name, adata.n_obs)

    # CellTypist requires log1p-normalized expression (~10k counts/cell).
    # Use .raw if present, else normalize in place.
    if adata.raw is not None and adata.raw.X is not None:
        work = adata.raw.to_adata()
    else:
        work = adata.copy()
    try:
        import scanpy as sc
        import numpy as np
        from scipy.sparse import issparse
        import scipy.sparse as sp
        if work.X is None:
            work.X = work.layers.get("counts")
        X = work.X.toarray() if issparse(work.X) else np.asarray(work.X)
        has_neg = float(X.min()) < 0
        max_v = float(X.max())
        # Three cases:
        #  1. raw counts (all >= 0, integers, large)            → normalize_total + log1p
        #  2. already log1p (>= 0, max < ~15)                   → keep as-is
        #  3. z-score / scaled (has negatives, max maybe huge)  → expm1 to "counts"
        #     then normalize_total + log1p so CellTypist's check passes.
        if not has_neg and max_v < 15 and (isinstance(X, np.ndarray) and (X % 1 != 0).any()):
            # already log1p-normalized → keep
            pass
        else:
            if has_neg:
                # undo scaling: expm1 (z-score data was log1p then scaled)
                work.X = np.expm1(np.clip(X, -10, 15))
                if not issparse(work.X):
                    work.X = sp.csr_matrix(work.X)
            sc.pp.normalize_total(work, target_sum=1e4)
            sc.pp.log1p(work)
    except Exception as exc:
        logger.warning("CellTypist normalization step failed (using as-is): %s", exc)

    # Ensure the model is available (celltypist auto-downloads if missing).
    try:
        models.download_models(force_update=False)
    except Exception as exc:
        logger.warning("CellTypist model download check failed: %s", exc)

    try:
        # Explicitly load the model object. Bare name fails in some versions;
        # fall back to the on-disk path under ~/.celltypist/data/models/.
        try:
            model_obj = models.Model.load(model=model_name)
        except Exception:
            import os
            m_dir = os.path.expanduser("~/.celltypist/data/models")
            pkl = os.path.join(m_dir, f"{model_name}.pkl")
            if not os.path.exists(pkl):
                # Download single model file by name
                models.download_models(model=[model_name], force_update=False)
            model_obj = models.Model.load(model=pkl)
        predictions = celltypist.annotate(
            work,
            model=model_obj,
            majority_voting=False,
            mode="prob match",
        )
        pred_adata = predictions.to_adata()
        # CellTypist stores predicted labels in obs["predicted_labels"]
        label_col = "predicted_labels"
        if label_col not in pred_adata.obs.columns and "majority_voting" in pred_adata.obs.columns:
            label_col = "majority_voting"
        if label_col in pred_adata.obs.columns:
            adata.obs["celltypist_labels"] = pred_adata.obs[label_col].astype(str)
        else:
            adata.obs["celltypist_labels"] = "unknown"
        # Confidence score
        for cand in ("conf_score", "max_probability", "prob_match"):
            if cand in pred_adata.obs.columns:
                adata.obs["celltypist_scores"] = pred_adata.obs[cand].astype(float)
                break
        else:
            adata.obs["celltypist_scores"] = 1.0
        return {"method": "celltypist", "embedding_key": "X_pca", "adata": adata}
    except Exception as exc:
        logger.error("CellTypist annotate failed: %s", exc)
        return {"method": "celltypist", "error": str(exc)}


# ── sklearn classifiers (reference-based, supervised) ──────────────────

def _run_sklearn_classifier(adata, method: str, label_key: Optional[str] = None,
                            **kwargs) -> Dict[str, Any]:
    """Train a sklearn classifier on labeled cells, predict unlabeled.

    Requires a ground-truth label column. Uses a simple train/test split:
      - 80% labeled cells train, 20% held out to report accuracy.
    Adds ``{method}_labels`` to adata.obs.
    """
    import numpy as np
    import scanpy as sc
    from sklearn.model_selection import train_test_split

    label_key = label_key or _find_label_col(adata)
    if label_key is None or label_key not in adata.obs.columns:
        logger.warning("%s needs a ground-truth label column (cell_type) — skipping", method)
        return {"method": method, "error": "no label column"}

    # Ensure we have PCA embedding
    if "X_pca" not in adata.obsm:
        sc.pp.pca(adata, n_comps=50, svd_solver="arpack")

    X = np.asarray(adata.obsm["X_pca"])
    y = adata.obs[label_key].astype(str).to_numpy()

    # Drop rows with missing / unknown labels
    valid = np.array([
        lab not in ("nan", "NA", "unknown", "Unknown", "")
        for lab in y
    ])

    if valid.sum() < 30:
        logger.warning("%s: too few labeled cells (%d) — skipping", method, valid.sum())
        return {"method": method, "error": "too few labeled cells"}

    X_lab, y_lab = X[valid], y[valid]

    # Stratified split only when every class has >=2 members (train_test_split
    # with stratify raises "least populated classes have only 1 member").
    from collections import Counter
    counts = Counter(y_lab)
    can_stratify = len(np.unique(y_lab)) > 1 and all(c >= 2 for c in counts.values())
    X_tr, X_te, y_tr, y_te = train_test_split(
        X_lab, y_lab, test_size=0.2, random_state=0,
        stratify=y_lab if can_stratify else None,
    )

    if method == "logreg":
        from sklearn.linear_model import LogisticRegression
        clf = LogisticRegression(max_iter=1000, C=kwargs.get("C", 1.0))
    else:  # randomforest
        from sklearn.ensemble import RandomForestClassifier
        clf = RandomForestClassifier(
            n_estimators=kwargs.get("n_estimators", 200),
            random_state=0, n_jobs=-1,
        )

    clf.fit(X_tr, y_tr)
    acc = float(clf.score(X_te, y_te))
    logger.info("%s train accuracy=%.3f on %d labeled cells", method, acc, valid.sum())

    col_name = f"{method}_labels"
    adata.obs[col_name] = clf.predict(X)
    # Record held-out accuracy in uns for the evaluator to read
    adata.uns[f"{method}_train_acc"] = acc
    return {"method": method, "embedding_key": "X_pca", "adata": adata}


def _run_logreg(adata, label_key: Optional[str] = None, **kwargs) -> Dict[str, Any]:
    return _run_sklearn_classifier(adata, "logreg", label_key, **kwargs)


def _run_randomforest(adata, label_key: Optional[str] = None, **kwargs) -> Dict[str, Any]:
    return _run_sklearn_classifier(adata, "randomforest", label_key, **kwargs)


# ── Majority baseline ──────────────────────────────────────────────────

def _run_majority(adata, label_key: Optional[str] = None, **kwargs) -> Dict[str, Any]:
    """Assign the most common label to all cells (baseline)."""
    import numpy as np

    label_key = label_key or _find_label_col(adata)
    if label_key is None or label_key not in adata.obs.columns:
        return {"method": "majority", "error": "no label column"}

    labels = adata.obs[label_key].astype(str)
    majority = labels.mode()[0] if not labels.mode().empty else "unknown"
    adata.obs["majority_labels"] = majority
    return {"method": "majority", "embedding_key": "X_pca", "adata": adata}


# ── Unified entry point (matches MethodRegistry.dispatch contract) ─────

def run_all_methods(adata, batch_key: str = "batch", methods: Optional[List[str]] = None,
                    label_key: Optional[str] = None, **kwargs) -> Dict[str, str | Dict[str, Any]]:
    """Run all annotation methods, returning {method_name: result}.

    Note: annotation methods need a ground-truth ``label_key`` (cell_type).
    The ``label_key`` can be passed explicitly or auto-detected.
    """
    registry = {
        "celltypist": _run_celltypist,
        "logreg": _run_logreg,
        "randomforest": _run_randomforest,
        "majority": _run_majority,
    }
    results: Dict[str, Any] = {}
    for method in methods or list(registry):
        fn = registry.get(method)
        if fn is None:
            logger.warning("Unknown annotation method: %s", method)
            continue
        logger.info("Running annotation method: %s", method)
        try:
            result = fn(adata.copy(), label_key=label_key, **kwargs)
            results[method] = result
        except Exception as exc:
            logger.error("Annotation method %s failed: %s", method, exc)
    return results
